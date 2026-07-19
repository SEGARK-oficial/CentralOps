"""Orquestração async: collect → normalize → dedupe → batch → dispatch.

Este módulo é o coração do worker de coleta. Um ciclo (``run_collection_once``)
é **stateless** (RNF01): tudo que ele precisa lembrar entre invocações
vive em Redis / Postgres.

O pipeline aplica mapping versionado
e produz o envelope canônico ``{_centralops, normalized, raw}``. Eventos
que falham normalização (mapping ausente, regra ``required`` resolvendo
para None, customer_id faltando) vão para ``QuarantineEvent`` (RF2.6) em
vez de seguir para o Wazuh.

Desacoplamento de ingestão (RNF03): o buffer acumula envelopes e
publica lotes na queue ``dispatch.wazuh``; se o Wazuh estiver
indisponível, os lotes ficam enfileirados no broker e a coleta
continua avançando.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, Dict, Optional, Tuple

import aiohttp
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..core.config import settings
from ..db import database, models

from . import quarantine
from .auth.oauth_cache import get_or_refresh_token, invalidate as invalidate_token
from .base import CollectorContext
from .config_loader import get_collector_config
from .domain_limiter import DomainLimiter
from .metrics import (
    CURSOR_LAG,
    DEDUPE_DROPS,
    EVENTS_TOTAL,
    NORMALIZE_LATENCY,
    OCSF_INVALID,
    OCSF_VALID,
    OCSF_VALIDATE_LATENCY,
    QUARANTINE_TOTAL,
)
from . import ocsf_policy
from .normalize import drift, sample_reservoir
from .normalize.ocsf import validator as ocsf_validator
from .reduction import metering
from .normalize.engine import (
    MappingError,
    MappingRequiredFieldError,
    default_engine,
)
from .normalize.envelope import EnvelopeContext, build_envelope, has_customer_id
from .rate_limit_redis import RedisRateLimiter
from .registry import get as registry_get, has as registry_has
from .state.cursor import CursorStore
from .state.dedupe import claim, compute_message_id

logger = logging.getLogger(__name__)

# Conjunto vazio de regras em voo: valor inicial ANTES do try de
# ``_run_collection_once`` (o ``finally`` referencia o nome). Constante de módulo
# para não alocar um objeto por ciclo em orgs sem regra — o caso majoritário.
from .inflight.matcher import CompiledRuleSet as _CompiledRuleSet

_EMPTY_RULESET = _CompiledRuleSet(rules=(), share_paths=False)


class VendorAuthError(Exception):
    """Levantada quando o vendor responde ``401`` — sinaliza que o cache
    de token foi invalidado e que a task Celery deve fazer retry (novo
    ciclo vai pegar token fresco via ``oauth_cache``).
    """

    def __init__(self, integration_id: int, platform: str) -> None:
        self.integration_id = integration_id
        self.platform = platform
        super().__init__(
            f"vendor auth error integration={integration_id} platform={platform}"
        )


@asynccontextmanager
async def _aiohttp_session() -> AsyncIterator[aiohttp.ClientSession]:
    # RNF06: TLS 1.2+ estrito, certificação validada.
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    connector = aiohttp.TCPConnector(
        ssl=ssl_ctx,
        limit=100,
        limit_per_host=20,
    )
    timeout = aiohttp.ClientTimeout(total=60, connect=10, sock_read=30)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        yield session


def _headers_for(platform: str, integration: models.Integration, access_token: str) -> Dict[str, str]:
    base = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": "centralops-collector/1.0",
    }
    if platform == "sophos":
        # Bug: para Partner-managed children, ``tenant_id`` no banco às vezes
        # foi populado com o UUID do PARTNER (em vez do tenant) — o que causa
        # 403 silencioso em POST /detections (endpoint estrito) enquanto
        # GET /alerts e GET /cases tolerar o erro.  ``external_id`` é o
        # tenant UUID canônico desde Partner sync; preferir ele com
        # ``tenant_id`` como fallback (mesma heurística que o provider usa
        # em ``_effective_tenant_external_id``).
        tenant_header = (
            getattr(integration, "external_id", None) or integration.tenant_id or ""
        ).strip()
        if tenant_header:
            base["X-Tenant-ID"] = tenant_header
        if integration.region:
            base["X-Region"] = integration.region  # lido por SophosAlertsCollector
        # Fonte de verdade pro endpoint Sophos quando disponível (Partner
        # mode populou ``apiHost`` direto da resposta da Sophos). Collectors
        # preferem essa string ao invés de derivar ``f"api-{region}..."``,
        # evitando NXDOMAIN quando a Sophos retorna geo-code (``EU``) em vez
        # de slug do datacenter (``eu03``).
        if integration.api_host:
            base["X-Api-Host"] = integration.api_host
    return base


def _load_current_mapping(
    vendor: str, event_type: str
) -> Optional[Tuple[str, Any, int]]:
    """Resolve o ``MappingVersion`` ativo para um (vendor, event_type).

    Devolve ``(mapping_version_id, rules, dsl_version)`` ou ``None`` se a
    definição não existe ou não tem versão atual configurada (caso típico
    no seed inicial).

    ``dsl_version`` padrão 1 quando NULL no banco (legado).

    Síncrono — chamado uma vez por ciclo via ``asyncio.to_thread`` para
    não bloquear o event loop.
    """
    with database.SessionLocal() as db:
        defn = db.scalar(
            select(models.MappingDefinition).where(
                models.MappingDefinition.vendor == vendor,
                models.MappingDefinition.event_type == event_type,
            )
        )
        if defn is None or not defn.current_version_id:
            return None
        version = db.scalar(
            select(models.MappingVersion).where(
                models.MappingVersion.id == defn.current_version_id
            )
        )
        if version is None:
            return None
        try:
            rules = json.loads(version.rules)
        except (TypeError, ValueError):
            logger.error(
                "pipeline: rules malformadas em mapping_version_id=%s",
                version.id,
            )
            return None
        # lê dsl_version da linha; default 1 para legado (NULL).
        dsl_version: int = getattr(version, "dsl_version", 1) or 1
        return version.id, rules, dsl_version


def _capture_sync(
    batch: list,
    org_id: Optional[int],
    outcome: str,
    *,
    destination_id: Optional[str] = None,
    detail: Optional[str] = None,
    sessions: Optional[list] = None,
) -> None:
    """Registra o DESFECHO de um lote nas sessões de captura ativas (tap de ciclo de
    vida). BEST-EFFORT ABSOLUTO: engole TUDO — a captura nunca altera o resultado do
    dispatch/coleta (mesma garantia do bloco histórico em ``dispatch``).

    Fail-closed em ``org_id is None`` (nunca escreve num bucket compartilhado). Curto-
    circuita barato quando o org não tem sessão ativa (cache negativo em
    ``capture_session``), então chamar isto no hot path é seguro."""
    if not batch or org_id is None:
        return
    try:
        from . import capture_session

        capture_session.record_sync(
            batch,
            org_id,
            outcome=outcome,
            destination_id=destination_id,
            detail=detail,
            sessions=sessions,
        )
    except Exception:  # noqa: BLE001 — captura nunca quebra o hot path
        logger.debug("capture: falha ao registrar outcome=%s (não-fatal)", outcome, exc_info=True)


async def _capture_outcome(
    batch: list,
    org_id: Optional[int],
    outcome: str,
    *,
    destination_id: Optional[str] = None,
    detail: Optional[str] = None,
) -> None:
    """:func:`_capture_sync` fora do event loop (o cliente de captura é síncrono).
    Best-effort — nunca levanta."""
    if not batch or org_id is None:
        return
    # Short-circuit ANTES do hop de thread-pool. Sem sessão ativa conhecida, despachar
    # para a thread custaria ~50µs/chamada só em troca de contexto (130× a chamada
    # direta) — e a SUPRESSÃO chama isto POR EVENTO dentro do laço de coleta, que é
    # justamente o caminho de alto volume. A sonda é em memória (zero I/O) e
    # conservadora: se não souber, segue o caminho normal.
    from .capture_session import likely_no_session

    if likely_no_session(org_id):
        return
    try:
        await asyncio.to_thread(
            _capture_sync,
            batch,
            org_id,
            outcome,
            destination_id=destination_id,
            detail=detail,
        )
    except Exception:  # noqa: BLE001 — captura nunca quebra o hot path
        logger.debug("capture: falha ao registrar outcome=%s (não-fatal)", outcome, exc_info=True)


async def _capture_delivery_failed(
    batch: list, destination_id: Optional[str], detail: str
) -> None:
    """Atalho do desfecho ``delivery_failed`` (destino ausente, cross-tenant, breaker
    aberto, exceção de envio). Best-effort — nunca levanta nem mascara o erro real."""
    try:
        from .capture_session import OUTCOME_DELIVERY_FAILED

        await _capture_outcome(
            batch,
            _batch_org_id(batch),
            OUTCOME_DELIVERY_FAILED,
            destination_id=destination_id,
            detail=detail,
        )
    except Exception:  # noqa: BLE001 — captura nunca quebra o dispatch
        logger.debug("capture: falha ao registrar delivery_failed (não-fatal)", exc_info=True)


def _make_quarantine_budget(
    integration_id: Any, platform: Any
) -> Callable[[str, int], bool]:
    """Orçamento de ESCRITA de quarentena por razão, para UM ciclo de coleta.

    Sob uma regressão sistêmica (mapping deletado, customer_id que parou de
    resolver, vendor mudando o schema) TODO evento do ciclo vira quarentena. Sem
    teto isso amplifica a escrita no DB pelo tamanho do backlog inteiro — a mesma
    forma do poison-loop de coletor já vivido em produção (drenar o backlog inteiro
    num run → soft-timeout → rollback → não coleta).

    Teto POR RAZÃO, não orçamento compartilhado: uma enxurrada de
    ``missing_mapping`` não pode consumir o orçamento e esconder o único ``map`` do
    ciclo — cada razão mantém representação diagnóstica. O total fica limitado a
    (nº de razões × teto), que é bounded e pequeno.

    Fail-LOUD ao estourar: loga uma vez por razão por ciclo. Silêncio aqui seria o
    pior caso — o operador veria a fila de quarentena parar de crescer e concluiria
    que o problema cessou, quando na verdade escalou.

    A MÉTRICA fica FORA deste teto, no caller: ``QUARANTINE_TOTAL`` conta o que foi
    quarentenado, não o que coube no orçamento de escrita.

    Fábrica module-level (e não closure inline) para ser testável isoladamente —
    ver ``backend/tests/test_adr0015_quarantine_budget.py``.
    """
    writes: dict[str, int] = {}

    def _ok(kind: str, cap: int) -> bool:
        n = writes.get(kind, 0)
        if n >= cap:
            if n == cap:  # 1ª rejeição desta razão — loga e marca como logada
                writes[kind] = n + 1
                logger.warning(
                    "quarentena: teto de escrita atingido (kind=%s cap=%d "
                    "integration=%s vendor=%s) — eventos seguem NÃO despachados e "
                    "contados na métrica, mas as escritas subsequentes deste ciclo "
                    "são puladas. Teto sistêmico costuma indicar regressão de "
                    "mapping/config, não eventos ruins isolados.",
                    kind, cap, integration_id, platform,
                )
            return False
        writes[kind] = n + 1
        return True

    return _ok


async def _quarantine_async(*, capture_org_id: Optional[int] = None, **kwargs: Any) -> None:
    """Wrapper que roda o write síncrono em thread auxiliar.

    ``capture_org_id`` NÃO é repassado à quarentena — serve só para o tap de captura
    saber o tenant (vários call-sites não passam ``organization_id`` para a quarentena,
    e mudar isso alteraria as linhas gravadas na tabela). A captura roda DEPOIS do
    write e é best-effort: nunca afeta a quarentena."""
    await asyncio.to_thread(quarantine.send_to_quarantine, **kwargs)
    _org = capture_org_id if capture_org_id is not None else kwargs.get("organization_id")
    if _org is None:
        return
    # Pseudo-envelope: o evento quarentenado morreu ANTES de virar envelope roteável,
    # mas o tap precisa do vendor (filtro da sessão) e do raw ("como entrou").
    from .capture_session import OUTCOME_QUARANTINED

    _pseudo = {
        "_centralops": {
            "vendor": kwargs.get("vendor"),
            "event_type": kwargs.get("event_type"),
            "organization_id": _org,
            "integration_id": kwargs.get("integration_id"),
        },
        "raw": kwargs.get("raw"),
    }
    await _capture_outcome(
        [_pseudo],
        _org,
        OUTCOME_QUARANTINED,
        detail=f"{kwargs.get('error_kind') or 'quarantine'}: {kwargs.get('error_detail') or ''}",
    )


async def _maybe_suppress(redis: Any, envelope: dict, suppress_routes: list) -> Optional[str]:
    """Decide a supressão por assinatura de UM evento.

    A 1ª ``suppress_route`` (pré-filtrada: já tem ``suppress_key`` + ``allow>0``) cuja
    condição casa DECIDE (short-circuit): acima do limite na janela → retorna o
    ``route_id`` que suprimiu (o caller DESCARTA o evento); dentro do limite → decora o
    liberado com ``_centralops.suppress_count`` (preserva a contagem p/ detecção) e
    retorna ``None`` (entregar). **Fail-OPEN:** qualquer erro (Redis lento/indisponível)
    → ``None`` = entrega (supressão é otimização de custo, jamais correção — nunca
    derruba nem perde evento por causa dela)."""
    from .routing.engine import event_labels, matches
    from .state.dedupe import claim_suppress, suppress_signature

    labels = event_labels(envelope)
    for r in suppress_routes:
        if not matches(r.condition, labels):
            continue
        try:
            sig = suppress_signature(labels, r.suppress_key)
            keep, count = await claim_suppress(
                redis, r.id, sig, r.suppress_allow, r.suppress_window_s
            )
        except Exception:  # noqa: BLE001 — fail-open: erro de Redis → entrega
            logger.debug("suppress: claim_suppress falhou (route=%s) — fail-open", r.id, exc_info=True)
            return None
        if not keep:
            return r.id
        envelope.setdefault("_centralops", {})["suppress_count"] = count
        return None
    return None


async def run_collection_once(integration_id: int, stream: str) -> None:
    """Executa um ciclo completo de coleta para (integration_id, stream).

    Abre o span RAIZ ``collect.cycle`` do trace distribuído. Os
    ``_enqueue_dispatch`` rodam DENTRO deste escopo, então ``tracing.carrier()``
    captura este contexto e a task de dispatch vira filha do ciclo (cross-process).
    No-op quando OTEL_ENABLED off.
    """
    from . import tracing

    with tracing.span(
        "collect.cycle",
        **{
            "centralops.integration_id": integration_id,
            "centralops.stream": stream,
        },
    ):
        await _run_collection_once(integration_id, stream)


async def _run_collection_once(integration_id: int, stream: str) -> None:
    # Cada task cria e fecha seu próprio cliente Redis efêmero.
    # Pool compartilhado foi revertido: ConnectionPool async
    # vincula coroutines ao loop de criação; tasks Celery prefork abrem
    # um loop por task via asyncio.run() → "Event loop is closed".
    from .celery_app import get_worker_redis

    redis = get_worker_redis()
    redis_owns = True  # sempre efêmero — fechar no finally é obrigatório

    cursor_store = CursorStore(redis)
    cursor_before: Optional[Dict[str, Any]] = None
    events_count = 0
    # Hardening data-plane: no modo kafka a claim de dedupe (Redis) e o hand-off
    # durável (produce no Kafka) são sistemas SEPARADOS. Se o run falhar, as
    # claims tomadas são SOLTAS no except final para o retry re-reclamar —
    # senão um produce que falhou deixaria o evento reclamado-mas-não-entregue
    # e o reprocesso o descartaria como "duplicado" (perda silenciosa).
    # Definidos AQUI (antes do try): o except final os referencia — exceção nos
    # passos 1–3 (antes do loop de coleta) virava UnboundLocalError e mascarava
    # o erro original (incidente jul/2026).
    # ADR-0015 Fase 2 — compensação de claim de dedupe, agora INCONDICIONAL.
    # Antes era gated por ``EVENT_DATAPLANE == "kafka"``, o que deixava o
    # data-plane DEFAULT sem nenhuma compensação: um run que falhasse vazava
    # TODAS as claims tomadas até o TTL, e o retry re-via os eventos, ``claim``
    # devolvia False e eles eram descartados como "duplicados" — PERDA
    # SILENCIOSA de log de segurança. A claim é um risco do PIPELINE, não do
    # transporte.
    #
    # Guarda apenas o NÃO-LIQUIDADO: cada id sai do conjunto assim que
    # ``_enqueue_dispatch`` retorna (hand-off durável feito). Num run
    # bem-sucedido o conjunto termina VAZIO e o release é no-op. Memória
    # limitada a ~``collector_batch_size``.
    unsettled_claims: set[str] = set()
    batch_msg_ids: list[str] = []
    # metering IN batched (ADR-0011): acumula (eventos, bytes) por
    # (org, integração) e faz flush a cada 500 eventos/15s — o record_in
    # por-evento fazia 4 pipelines Redis SÍNCRONOS por evento e bloqueava o
    # event loop (~0,8ms/evento). Instanciado ANTES do try (padrão
    # unsettled_claims): o finally faz o flush FINAL best-effort mesmo em
    # exceção/soft-timeout, sem mascarar o erro original.
    _metering_in = metering.InVolumeAccumulator()
    # ADR-0015 Fase 1 — MESMO padrão e MESMO motivo de ``unsettled_claims`` acima:
    # o ``finally`` referencia estes nomes, e uma exceção nos passos 1-3 (antes
    # da carga das regras) os deixaria unbound, transformando o flush num
    # ``UnboundLocalError`` que MASCARARIA o erro original. Foi exatamente essa
    # a forma do incidente de jul/2026 registrado no comentário acima.
    _inflight_rules = _EMPTY_RULESET
    _inflight_acc = None
    _inflight_logged = False
    _inflight_org_id: Optional[int] = None
    from .inflight.runtime import flush_inflight

    try:
        # ── 1. Carrega Integration (session efêmera) ──────────────────
        with database.SessionLocal() as db:
            integration = db.scalar(
                select(models.Integration)
                .where(models.Integration.id == integration_id)
                .options(selectinload(models.Integration.organization))
            )
            if not integration or not integration.is_active:
                logger.warning(
                    "integration inativa ou inexistente",
                    extra={
                        "event": "collection.skip_inactive",
                        "integration_id": integration_id,
                    },
                )
                return
            # Partner/Organization são agregadores — não possuem streams
            # tenant-scoped (alerts/cases/detections). Defesa-em-profundidade:
            # mesmo que um caller upstream falhe em checar kind, esta guarda
            # impede coleta acidental de dados cruzados.
            if integration.kind in ("partner", "organization"):
                logger.info(
                    "collection: pulando partner/org integration_id=%s kind=%s stream=%s",
                    integration_id,
                    integration.kind,
                    stream,
                    extra={
                        "event": "collection.skip_partner_org",
                        "integration_id": integration_id,
                        "stream": stream,
                    },
                )
                return
            platform = integration.platform
            organization_id = integration.organization_id
            organization_name: Optional[str] = (
                integration.organization.name
                if integration.organization is not None
                else None
            )
            # data_geography da integração (Sophos dataRegion ou
            # campo manual). Propagada no envelope para enforcement de residência.
            integration_data_geography: Optional[str] = getattr(
                integration, "data_geography", None
            )
            # ``customer_id`` do envelope é o ``Organization.id``
            # INTERNO — a entrega de eventos NÃO depende mais da identidade do
            # IRIS. O mapeamento Organization → customer id externo (IRIS/SOAR)
            # vive em ``destination_customer_mappings`` e é resolvido só na borda
            # do connector daquele destino. Sem org → None → quarentena legítima
            # (evento sem tenant), não mais "missing IRIS id".
            envelope_customer_id: Optional[int] = organization_id
            db.expunge(integration)

        # resolve as rotas UMA vez por ciclo OFF do event
        # loop (a query sync nunca bloqueia a coleta). O ciclo é mono-tenant
        # (uma integração), então ``organization_id`` é autoritativo. Roteamento
        # é o único modelo: sem rotas extras, tudo cai no catch-all wazuh-default
        # (byte-idêntico). Fail-safe: ``_load_routes_for_org`` → [] em erro.
        dispatch_routes: list[Any] = await asyncio.to_thread(
            _load_routes_for_org, organization_id
        )

        # resolve a política de enforcement OCSF UMA vez por ciclo
        # (o ciclo é mono-tenant → org fixa; espelha _load_routes_for_org). No-op quando
        # a validação está OFF. Fail-safe interno → default global (tag_and_pass).
        _ocsf_enforcement: Optional[str] = (
            await asyncio.to_thread(
                ocsf_policy.resolve_enforcement_mode, organization_id
            )
            if settings.OCSF_VALIDATION_ENABLED
            else None
        )
        # ── Orçamento de ESCRITA de quarentena por ciclo (ADR-0015, Fase 0) ──
        #
        # Sob uma regressão sistêmica (mapping deletado, customer_id que parou de
        # resolver, vendor mudando o schema) TODO evento do ciclo vira quarentena.
        # Sem teto isso amplifica a escrita no DB pelo tamanho do backlog inteiro —
        # a mesma forma do poison-loop de coletor já vivido em produção (drenar o
        # backlog inteiro num run → soft-timeout → rollback → não coleta).
        # Antes desta ADR só o caminho de validate-OCSF tinha teto; os quatro
        # restantes (missing-mapping, map ×2, missing-customer-id) não tinham.
        #
        # Teto POR RAZÃO, não orçamento compartilhado: uma enxurrada de
        # ``missing_mapping`` não pode consumir o orçamento e esconder o único
        # ``map`` do ciclo — cada razão mantém representação diagnóstica. O total
        # fica limitado a (nº de razões × teto), que é bounded e pequeno.
        _quarantine_budget_ok = _make_quarantine_budget(integration_id, platform)

        # pré-filtra as rotas com supressão CONFIGURADA (uma vez por
        # ciclo). Gated pelas flags: sem elas, lista vazia → o check por-evento é pulado
        # (hot path byte-idêntico). Só reduz se também estiver medindo (COST_METERING).
        _suppress_routes = (
            [
                r for r in dispatch_routes
                if getattr(r, "suppress_key", None) and int(getattr(r, "suppress_allow", 0) or 0) > 0
            ]
            if (settings.REDUCTION_SUPPRESS_ENABLED and settings.COST_METERING_ENABLED)
            else []
        )

        # ── Classificação em voo (ADR-0015 Fase 1) ───────────────────────
        # Carga e compilação 1x por ciclo, OFF-LOOP (não há sessão de DB aberta
        # no laço de eventos). Import lazy: uma org sem regras em voo não paga
        # nem o custo de resolver o módulo. Fail-safe para () — um problema aqui
        # nunca pode impedir a COLETA, que é o produto que se vende.
        from .inflight.matcher import evaluate_ruleset
        from .inflight.runtime import InflightAccumulator, load_inflight_rules_for_org

        _inflight_org_id = organization_id
        _inflight_rules = await asyncio.to_thread(
            load_inflight_rules_for_org, organization_id
        )
        # Instanciado SÓ quando há regra: com a tupla vazia o hot path fica
        # byte-idêntico ao anterior (R2) e o flush é curto-circuitado.
        _inflight_acc = InflightAccumulator() if _inflight_rules.rules else None

        if not registry_has(platform, stream):
            logger.error(
                "collector não registrado",
                extra={
                    "event": "collection.unregistered_collector",
                    "platform": platform,
                    "stream": stream,
                },
            )
            return
        registration = registry_get(platform, stream)
        collector_cls = registration.collector_cls
        event_type = collector_cls.event_type

        # ── 2. Token OAuth com cache Redis + lock distribuído (RF08) ──
        access_token = await get_or_refresh_token(
            redis,
            integration_id=integration_id,
            refresh_fn=registration.refresh_fn,
            vendor=platform,
        )
        headers = _headers_for(platform, integration, access_token)

        # ── 3. Carrega config + mapping atual ─────────────────────────
        config = await get_collector_config(redis)
        mapping_current = await asyncio.to_thread(
            _load_current_mapping, platform, event_type
        )

        rate_limiter = RedisRateLimiter(redis, config.rate_limits_by_vendor)
        domain_limiter = DomainLimiter(redis, config.domain_concurrency_limits)
        cursor_before = await cursor_store.load(integration_id, stream)

        # ── 4. Loop async de coleta ───────────────────────────────────
        batch: list[Dict[str, Any]] = []
        last_flush = time.monotonic()
        # (unsettled_claims/batch_msg_ids são inicializados ANTES do try — o
        # except final os referencia; ver comentário na inicialização.)

        async with _aiohttp_session() as session:
            ctx = CollectorContext(
                integration_id=integration_id,
                organization_id=organization_id,
                platform=platform,
                headers=headers,
                session=session,
                cursor=cursor_before,
                domain_limiter=domain_limiter,
                rate_limiter=rate_limiter,
                redis=redis,
            )
            collector = collector_cls(ctx)

            try:
                async for raw_event in collector.collect():
                    msg_id = collector.extract_message_id(raw_event)
                    if not msg_id:
                        msg_id = compute_message_id(raw_event)

                    # RNF07 — idempotência. Aplicada SOBRE o raw, antes
                    # da normalização, para evitar reprocessar evento
                    # já visto mesmo que o mapping tenha mudado.
                    if not await claim(
                        redis,
                        integration_id,
                        msg_id,
                        ttl_days=config.dedupe_ttl_days,
                    ):
                        DEDUPE_DROPS.labels(vendor=platform, stream=stream).inc()
                        continue
                    unsettled_claims.add(msg_id)

                    # RF3.7 — sample reservoir alimenta dry-run da UI.
                    # fire-and-forget — best-effort não pode bloquear
                    # o hot path de coleta. asyncio.ensure_future agenda a
                    # coroutine sem await imediato; falhas são silenciosas
                    # (intencionais para este path).
                    asyncio.ensure_future(
                        sample_reservoir.push(
                            redis, organization_id, platform, event_type, raw_event
                        )
                    )

                    # metering de volume IN (eventos/bytes que
                    # entraram, pós-dedupe), BATCHED: acumula localmente e grava
                    # no Redis 1×/500 eventos ou 15s (flush final no finally do
                    # ciclo). No-op imediato quando COST_METERING_ENABLED off —
                    # sem serialização extra; nunca dropa nem afeta a coleta.
                    _metering_in.add(organization_id, integration_id, raw_event)

                    # ── Normalização ──────────────────────────────────
                    if mapping_current is None:
                        # Mapping ainda não configurado para este event_type.
                        # Vai pra quarentena com kind explícito.
                        # Métrica SEMPRE (fidelidade); escrita sob orçamento.
                        QUARANTINE_TOTAL.labels(
                            vendor=platform,
                            event_type=event_type,
                            error_kind=quarantine.ERROR_KIND_MISSING_MAPPING,
                        ).inc()
                        if _quarantine_budget_ok(
                            quarantine.ERROR_KIND_MISSING_MAPPING,
                            settings.QUARANTINE_MAX_PER_KIND_PER_RUN,
                        ):
                            await _quarantine_async(
                                capture_org_id=organization_id,
                                integration_id=integration_id,
                                vendor=platform,
                                event_type=event_type,
                                raw=raw_event,
                                error_kind=quarantine.ERROR_KIND_MISSING_MAPPING,
                                error_detail="no current MappingVersion configured",
                            )
                        continue

                    mapping_version_id, rules, dsl_version = mapping_current
                    normalize_started = time.monotonic()
                    try:
                        applied = default_engine.apply(
                            mapping_version_id, rules, raw_event,
                            dsl_version=dsl_version,
                            # timestamp_t do OCSF é em MILISSEGUNDOS.
                            ingest_time_epoch=int(time.time() * 1000),
                        )
                    except MappingRequiredFieldError as exc:
                        # Métrica SEMPRE (fidelidade); escrita sob orçamento.
                        QUARANTINE_TOTAL.labels(
                            vendor=platform,
                            event_type=event_type,
                            error_kind=quarantine.ERROR_KIND_MAP,
                        ).inc()
                        if _quarantine_budget_ok(
                            quarantine.ERROR_KIND_MAP,
                            settings.QUARANTINE_MAX_PER_KIND_PER_RUN,
                        ):
                            await _quarantine_async(
                                capture_org_id=organization_id,
                                integration_id=integration_id,
                                vendor=platform,
                                event_type=event_type,
                                raw=raw_event,
                                error_kind=quarantine.ERROR_KIND_MAP,
                                error_detail=str(exc),
                                mapping_version_id=mapping_version_id,
                            )
                        continue
                    except MappingError as exc:
                        logger.warning(
                            "mapping error não-fatal",
                            extra={
                                "event": "collection.mapping_error",
                                "vendor": platform,
                                "event_type": event_type,
                                "error_detail": str(exc),
                            },
                        )
                        # Métrica SEMPRE (fidelidade); escrita sob orçamento.
                        QUARANTINE_TOTAL.labels(
                            vendor=platform,
                            event_type=event_type,
                            error_kind=quarantine.ERROR_KIND_MAP,
                        ).inc()
                        if _quarantine_budget_ok(
                            quarantine.ERROR_KIND_MAP,
                            settings.QUARANTINE_MAX_PER_KIND_PER_RUN,
                        ):
                            await _quarantine_async(
                                capture_org_id=organization_id,
                                integration_id=integration_id,
                                vendor=platform,
                                event_type=event_type,
                                raw=raw_event,
                                error_kind=quarantine.ERROR_KIND_MAP,
                                error_detail=str(exc),
                                mapping_version_id=mapping_version_id,
                            )
                        continue

                    envelope_ctx = EnvelopeContext(
                        vendor=platform,
                        integration_id=integration_id,
                        # = Organization.id interno (resolvido acima).
                        customer_id=envelope_customer_id,
                        customer_name=organization_name,
                        stream=stream,
                        event_type=event_type,
                        mapping_version_id=mapping_version_id,
                        # id interno do tenant para roteamento +
                        # isolamento event-level (auditoria/lineage por org).
                        organization_id=organization_id,
                        # data_geography para enforcement de residência.
                        data_geography=integration_data_geography,
                    )
                    # Despacha o raw reduzido (raw_reduction do mapping) quando
                    # houver — cabe no limite do destino (ex.: Wazuh ~64 KiB).
                    # O event_id permanece estável (vendor_msg_id no full raw).
                    envelope = build_envelope(
                        applied.reduced_raw or raw_event,
                        applied.output,
                        envelope_ctx,
                        vendor_msg_id=msg_id,
                    )
                    # contabiliza os bytes evitados pelo trimming
                    # (raw_reduction). No-op (zero serialização) quando as flags
                    # REDUCTION_TRIM_ENABLED/COST_METERING_ENABLED estão off.
                    metering.record_trim_saving(
                        organization_id, raw_event, applied.reduced_raw
                    )
                    # Proveniência: campos preenchidos pelo fallback de ingestão
                    # (timestamp aproximado) ficam marcados para o analista.
                    if applied.ingest_fallback_targets:
                        envelope["_centralops"]["degraded_fields"] = list(
                            applied.ingest_fallback_targets
                        )
                    NORMALIZE_LATENCY.labels(
                        vendor=platform, event_type=event_type
                    ).observe(time.monotonic() - normalize_started)

                    # RF3.6 — drift detection com sampling + janela de aprendizado.
                    # Best-effort, rodado em thread auxiliar para não bloquear I/O. A
                    # janela força 100% nos 1ºs eventos de uma fonte NOVA (auto-discovery
                    # à la Cribl/Axoflow) p/ syslog recém-apontado aparecer no Drift
                    # Explorer de imediato; depois cai na amostragem estacionária.
                    if drift.should_capture(
                        platform,
                        event_type,
                        organization_id,
                        settings.DRIFT_SAMPLE_RATE,
                        learning_events=settings.DRIFT_LEARNING_EVENTS,
                    ):
                        await asyncio.to_thread(
                            drift.record_unknown_fields,
                            vendor=platform,
                            event_type=event_type,
                            raw=raw_event,
                            consumed_paths=applied.consumed_paths,
                            # isolamento de inferência por tenant.
                            organization_id=organization_id,
                        )

                    # validação OCSF (structural gate + política por-org).
                    # No-op de custo zero com a flag OFF (fail-open, comportamento atual).
                    # Com ON: valida ``normalized`` contra o manifest OCSF vendorado (~µs,
                    # puro-Python), emite métricas de conformidade e ETIQUETA
                    # ``_centralops.ocsf_valid``. A AÇÃO em inválido segue a política da org
                    # (``_ocsf_enforcement``, resolvida 1×/ciclo): tag_and_pass só etiqueta e
                    # despacha (NUNCA descarta); quarantine → ERROR_KIND_VALIDATE (não
                    # despacha, recuperável); fail_closed → descarta sem quarentena.
                    # out_of_scope (classe OCSF-válida não-vendorada) SEMPRE passa (graceful).
                    # O counter de inválidos é SEM amostragem; só a ESCRITA de
                    # quarentena tem teto por ciclo. NB: ``_centralops`` NÃO é
                    # stripado no dispatch — sinks de envelope-inteiro (syslog/
                    # Splunk/Elastic/...) recebem a etiqueta no wire (e alguns a
                    # usam de propósito, ex. elastic indexa organization_id);
                    # só os sinks normalized-only (Datadog/Chronicle/Security
                    # Lake) a omitem. Auditoria jul/2026 corrigiu este comentário
                    # que afirmava o strip.
                    if settings.OCSF_VALIDATION_ENABLED:
                        _ocsf_reg = ocsf_validator.get_registry(
                            settings.OCSF_VALIDATION_VERSION
                        )
                        _ocsf_t0 = time.monotonic()
                        _ocsf = ocsf_validator.structural_gate(
                            envelope["normalized"], _ocsf_reg
                        )
                        OCSF_VALIDATE_LATENCY.labels(vendor=platform).observe(
                            time.monotonic() - _ocsf_t0
                        )
                        _cc = envelope["_centralops"]
                        if _ocsf.valid:
                            OCSF_VALID.labels(
                                vendor=platform, event_type=event_type
                            ).inc()
                            _cc["ocsf_valid"] = True
                        else:
                            # Counter SEMPRE (fidelidade), inclusive out_of_scope.
                            OCSF_INVALID.labels(
                                vendor=platform,
                                event_type=event_type,
                                reason=_ocsf.reason,
                            ).inc()
                            _action = ocsf_policy.decide(
                                valid=_ocsf.valid,
                                in_scope=_ocsf.in_scope,
                                mode=_ocsf_enforcement,
                            )
                            if _action == ocsf_policy.ACTION_QUARANTINE:
                                # Métrica SEMPRE, FORA do teto. ``config.py`` já
                                # documentava que "o counter de métrica segue SEM
                                # amostragem (fidelidade)", mas o ``.inc()`` estava
                                # DENTRO do if do teto — acima do teto a métrica
                                # parava de contar, e a enxurrada ficava invisível
                                # exatamente na situação em que se quer vê-la
                                # (ADR-0015, Fase 0: doc e código agora concordam).
                                QUARANTINE_TOTAL.labels(
                                    vendor=platform,
                                    event_type=event_type,
                                    error_kind=quarantine.ERROR_KIND_VALIDATE,
                                ).inc()
                                if _quarantine_budget_ok(
                                    quarantine.ERROR_KIND_VALIDATE,
                                    settings.OCSF_QUARANTINE_MAX_PER_RUN,
                                ):
                                    await _quarantine_async(
                                        capture_org_id=organization_id,
                                        integration_id=integration_id,
                                        vendor=platform,
                                        event_type=event_type,
                                        raw=raw_event,
                                        # reason é enum FECHADO → sem PII no detail.
                                        error_kind=quarantine.ERROR_KIND_VALIDATE,
                                        error_detail=f"ocsf invalid: {_ocsf.reason}",
                                        mapping_version_id=mapping_version_id,
                                        organization_id=organization_id,
                                    )
                                # Acima do teto: evento segue NÃO despachado (honra o
                                # modo quarantine/fail_closed); só a escrita é pulada.
                                continue
                            if _action == ocsf_policy.ACTION_DROP:
                                continue
                            # ACTION_PASS (tag_and_pass ou out_of_scope): etiqueta e segue.
                            _cc["ocsf_valid"] = False if _ocsf.in_scope else None
                            _cc["ocsf_reason"] = _ocsf.reason

                    # RF4.2 — customer_id obrigatório.
                    if not has_customer_id(envelope):
                        # Métrica SEMPRE (fidelidade); escrita sob orçamento.
                        QUARANTINE_TOTAL.labels(
                            vendor=platform,
                            event_type=event_type,
                            error_kind=quarantine.ERROR_KIND_MISSING_CUSTOMER_ID,
                        ).inc()
                        if _quarantine_budget_ok(
                            quarantine.ERROR_KIND_MISSING_CUSTOMER_ID,
                            settings.QUARANTINE_MAX_PER_KIND_PER_RUN,
                        ):
                            await _quarantine_async(
                                capture_org_id=organization_id,
                                integration_id=integration_id,
                                vendor=platform,
                                event_type=event_type,
                                raw=raw_event,
                                error_kind=quarantine.ERROR_KIND_MISSING_CUSTOMER_ID,
                                error_detail="customer_id resolved to empty",
                                mapping_version_id=mapping_version_id,
                            )
                        continue

                    # suppression por assinatura (rate-limit
                    # Number-to-Allow). A 1ª suppress-route que casa decide; acima do
                    # limite na janela → o evento é SUPRIMIDO (não entra no batch). A 1ª
                    # ocorrência sempre passa (preserva detecção). Fail-OPEN em erro Redis.
                    if _suppress_routes:
                        _suppressed_by = await _maybe_suppress(redis, envelope, _suppress_routes)
                        if _suppressed_by is not None:
                            from .metrics import SUPPRESSED
                            from . import observability_store as _obs_sup

                            SUPPRESSED.labels(route_id=_suppressed_by).inc()
                            _obs_sup.record_counter("route", _suppressed_by, "suppressed", 1.0)
                            # volume evitado pela supressão → bytes_saved{reason=suppress}
                            # (Evitado/Redução). Best-effort; o helper é gated
                            # (REDUCTION_SUPPRESS + COST_METERING) e fail-closed em org.
                            from .reduction import metering as _metering_sup

                            _metering_sup.record_suppress_saving(
                                (envelope.get("_centralops") or {}).get("organization_id"),
                                envelope,
                            )
                            # tap de ciclo de vida: o evento suprimido NUNCA chegava à
                            # captura (morria antes do dispatch) — o operador via um
                            # buraco sem explicação. Best-effort, curto-circuitado
                            # quando não há sessão ativa.
                            from .capture_session import OUTCOME_SUPPRESSED

                            await _capture_outcome(
                                [envelope],
                                organization_id,
                                OUTCOME_SUPPRESSED,
                                detail=f"route={_suppressed_by}",
                            )
                            continue

                    # ── Classificação em voo ─────────────────────────────
                    # Posição deliberada: DEPOIS da supressão (que preserva a 1ª
                    # ocorrência por assinatura — pipeline.py, comentário do
                    # bloco de suppress — logo um evento suprimido é repetição
                    # de um já classificado) e ANTES do roteamento, que é onde
                    # vive a ação `drop`. Classificar depois do drop seria
                    # falso-negativo silencioso; classificar antes da supressão
                    # produziria Detection sobre evento que nunca chega ao SIEM.
                    #
                    # R3 — FAIL-OPEN NA ENTREGA: nada aqui tem `continue`,
                    # `return` ou mutação do envelope. Uma regra que explode
                    # custa um log e o evento segue no batch. O detector é
                    # observador, nunca porteiro.
                    if _inflight_acc is not None:
                        try:
                            for _rule in evaluate_ruleset(envelope, _inflight_rules):
                                _inflight_acc.add(
                                    _rule, envelope, organization_id, integration_id
                                )
                        except Exception:  # noqa: BLE001
                            # Rate-limit de log por ciclo: sem isso, uma regra
                            # ruim trocaria degradação de detecção por
                            # amplificação de escrita no log — a mesma classe de
                            # dano que o teto de quarentena existe para evitar.
                            if not _inflight_logged:
                                _inflight_logged = True
                                logger.exception(
                                    "inflight: matcher falhou (o evento segue "
                                    "no batch; avaliação abortada neste evento)"
                                )
                            _inflight_acc.errors["matcher"] = (
                                _inflight_acc.errors.get("matcher", 0) + 1
                            )

                    batch.append(envelope)
                    # Paralelo 1:1 com ``batch``: permite liquidar as claims
                    # exatamente dos eventos que foram entregues, sem mutar o
                    # envelope (que é serializado para o data-plane).
                    batch_msg_ids.append(msg_id)
                    events_count += 1

                    if (
                        len(batch) >= config.collector_batch_size
                        or (time.monotonic() - last_flush)
                        >= config.collector_batch_flush_seconds
                    ):
                        _enqueue_dispatch(batch, dispatch_routes)
                        EVENTS_TOTAL.labels(
                            vendor=platform,
                            tenant=str(organization_id),
                            stream=stream,
                        ).inc(len(batch))
                        # LIQUIDAÇÃO — só DEPOIS do hand-off durável. Liquidar
                        # antes converteria uma falha de enqueue em perda
                        # silenciosa: a claim ficaria de pé e o retry
                        # descartaria o evento como duplicado.
                        unsettled_claims.difference_update(batch_msg_ids)
                        batch_msg_ids = []
                        batch = []
                        last_flush = time.monotonic()
            except aiohttp.ClientResponseError as exc:
                # Recovery de 401 in-flight: invalida cache OAuth e propaga
                # como ``VendorAuthError`` para o retry do Celery pegar token
                # fresco. Sem isso, um token velho fica no cache do Redis até
                # expirar o TTL (~1h) e causa 401 contínuo.
                if exc.status == 401:
                    logger.warning(
                        "vendor retornou 401; invalidando cache OAuth",
                        extra={
                            "event": "collection.vendor_auth_error",
                            "integration_id": integration_id,
                            "stream": stream,
                        },
                    )
                    try:
                        await invalidate_token(redis, integration_id)
                    except Exception:  # pragma: no cover
                        logger.exception("pipeline: falha ao invalidar cache oauth")
                    raise VendorAuthError(integration_id, platform) from exc
                raise

            if batch:
                _enqueue_dispatch(batch, dispatch_routes)
                EVENTS_TOTAL.labels(
                    vendor=platform,
                    tenant=str(organization_id),
                    stream=stream,
                ).inc(len(batch))
                # Dreno TERMINAL: uma integração que devolve poucos eventos e
                # encerra a página nunca atinge collector_batch_size nem o
                # gatilho de tempo (avaliado DENTRO do corpo do laço). Sem
                # liquidar aqui, as claims desses eventos seriam soltas pelo
                # finally e o retry os reprocessaria — duplicata, não perda,
                # mas ruído evitável.
                unsettled_claims.difference_update(batch_msg_ids)
                batch_msg_ids = []

        # ── 5. Persiste cursor final (RF02) ───────────────────────────
        # flow-view: instrumentação de volume de source no store nativo.
        # Best-effort — record_counter já engole exceções internamente; nunca afeta
        # a coleta. Alimenta obs:source:{integration_id}:ingested (forward-looking:
        # o endpoint /flow usa pipeline-health como fonte primária, mas esta série
        # permite futura consistência com o store nativo de rotas/destinos).
        if events_count > 0:
            try:
                from . import observability_store as _obs_src
                _obs_src.record_counter("source", str(integration_id), "ingested", float(events_count))
            except Exception:  # pragma: no cover — jamais bloqueia a coleta
                pass

        await cursor_store.save(
            integration_id,
            stream,
            ctx.cursor or {},
            events_collected=events_count,
            error=None,
        )
        CURSOR_LAG.labels(
            integration_id=str(integration_id), stream=stream
        ).set(0.0)
        logger.info(
            "collection ok",
            extra={
                "event": "collection.complete",
                "integration_id": integration_id,
                "stream": stream,
                "events_count": events_count,
            },
        )

    except Exception as exc:
        logger.exception(
            "collection falhou",
            extra={
                "event": "collection.error",
                "integration_id": integration_id,
                "stream": stream,
            },
        )
        # Hardening data-plane: só em kafka. O run falhou e o
        # cursor NÃO avança (será reprocessado). Solta TODAS as claims tomadas
        # neste run para que o retry re-reclame e re-produza — senão um produce
        # que falhou deixaria o evento reclamado-mas-não-entregue, e o reprocesso
        # o descartaria como "duplicado" (perda silenciosa). Eventos que já foram
        # produzidos serão reentregues e o dedupe-no-destino (event_id) os absorve
        # (at-least-once). Best-effort: erro de Redis aqui não mascara a original.
        try:
            await cursor_store.save(
                integration_id,
                stream,
                cursor_before or {},
                events_collected=0,
                error=str(exc)[:1000],
            )
        except Exception:  # pragma: no cover
            logger.exception("collection: falha ao persistir cursor de erro")
        raise
    finally:
        # flush FINAL do metering IN: grava o parcial acumulado mesmo quando o
        # ciclo falhou (exceção/soft-timeout). Best-effort — flush() engole tudo
        # internamente e NUNCA mascara o erro original em voo.
        _metering_in.flush()
        # Flush ÚNICO da classificação em voo, no ``finally`` — cobre o caminho
        # feliz E o de exceção. Isso é obrigatório, não zelo: no data-plane
        # default uma exceção no meio do ciclo NÃO solta as claims de dedupe, o
        # retry re-busca os eventos e ``claim`` os descarta como duplicados —
        # eles nunca seriam reclassificados. Sem flush aqui os matches morreriam
        # em memória, sem Detection, sem log e sem métrica.
        # Envolto em try/except próprio para JAMAIS mascarar a exceção original.
        try:
            await flush_inflight(_inflight_acc, _inflight_org_id)
        except Exception:  # noqa: BLE001
            logger.exception("inflight: flush falhou no encerramento do ciclo")
        # Solta as claims NÃO LIQUIDADAS (ADR-0015 Fase 2). Num run bem-sucedido
        # o conjunto está vazio — todo id saiu no ``_enqueue_dispatch`` — então
        # isto é no-op. No ``finally`` e não no ``except`` de propósito: cobre
        # também ``WorkerShutdown``, que deriva de ``BaseException`` e escapa de
        # ``except Exception``. Sem isto, um run interrompido deixava as claims
        # de pé até o TTL e o retry descartava os eventos como duplicados —
        # perda silenciosa. OBRIGATORIAMENTE antes do ``aclose`` abaixo: o DEL
        # iria contra um cliente fechado.
        if unsettled_claims:
            try:
                from .state.dedupe import release_many as _release_many

                _n = await _release_many(redis, integration_id, unsettled_claims)
                logger.warning(
                    "dedupe: %d claim(s) não liquidada(s) solta(s) p/ replay seguro "
                    "(integration=%s stream=%s) — os eventos serão recoletados em vez "
                    "de descartados como duplicados",
                    _n, integration_id, stream,
                    extra={
                        "event": "dedupe.claims_released",
                        "integration_id": integration_id,
                        "stream": stream,
                        "released": _n,
                    },
                )
            except Exception:  # noqa: BLE001 — jamais mascara a exceção original
                logger.exception("dedupe: falha ao soltar claims não liquidadas")
        # Fecha conexão apenas se for efêmera (sem pool compartilhado do worker).
        # Fechar client de pool com aclose() devolve conexões ao pool — ok,
        # mas como sinal explícito de intenção: só faz aclose no cliente efêmero.
        if redis_owns:
            await redis.aclose()


def _enqueue_dispatch(
    batch: list[Dict[str, Any]],
    routes: Optional[list[Any]] = None,
) -> None:
    """Publica lote pelo ROTEAMENTO (modelo único).

    Roteamento é o único caminho de despacho (sem flag): ``_enqueue_routed``
    avalia cada evento contra as rotas (first-match, ``is_final`` stop vs
    clone+continue fan-out, ``drop``) e enfileira sub-lotes por destino. TODOS
    os destinos (incl. wazuh-default) vão pela MESMA via —
    ``dispatch_to_destination`` shardeado (ou Kafka quando EVENT_DATAPLANE=kafka).
    Vendor-neutro: evento sem rota casada vai ao destino ``is_default`` se houver,
    senão à DLQ (``unrouted``). Os produtores chamam este helper.

    ``routes`` é a lista compilada pré-resolvida do ciclo de coleta; ``None``
    → resolve inline (callers bulk). Fail-safe: ``_load_routes_for_org`` → [].
    """
    if routes is None:
        routes = _load_routes_for_org(_batch_org_id(batch))
    _enqueue_routed(batch, routes)


def _compile_route_row(row: Any) -> Any:
    """DB ``Route`` row → ``CompiledRoute`` (parses the JSON condition/dests)."""
    from .routing import CompiledRoute, compile_pii_redaction

    # compile per-route PII redaction. FAIL-CLOSED (o INVERSO do
    # raw_reduction, que é fail-open): a spec armazenada já é validada no CRUD
    # (422 no spec ruim), então uma falha de compile em runtime é corrupção — e
    # NÃO podemos entregar PII em claro. Deixamos PROPAGAR: _load_routes_for_org
    # captura → retorna [] → tudo cai no wazuh-default INTERNO (sem vazamento
    # externo, zero perda). NUNCA engolir para () (isso seria fail-open = PII em
    # claro no SIEM). Gated por PII_REDACTION_ENABLED (default OFF → byte-idêntico
    # quando NENHUMA rota tem spec de redação, mesmo com o roteamento GA ativo).
    _raw_red = getattr(row, "pii_redaction", None)
    if _raw_red:
        if not settings.PII_REDACTION_ENABLED:
            # FAIL-CLOSED: a rota TEM spec de redação mas a
            # feature está desligada. NÃO entregar cleartext ao destino externo —
            # PROPAGA → _load_routes_for_org cai p/ wazuh-default interno. (Byte-
            # idêntico só vale quando NENHUMA rota tem spec — o estado default.)
            from .routing import PiiRedactionError

            raise PiiRedactionError(
                "rota tem pii_redaction mas PII_REDACTION_ENABLED=OFF — fail-closed"
            )
        _redaction = compile_pii_redaction(json.loads(_raw_red))
    else:
        _redaction = ()

    return CompiledRoute(
        id=str(row.id),
        name=str(row.name),
        priority=int(row.priority),
        condition=json.loads(row.condition or "{}"),
        action=str(row.action),
        destination_ids=tuple(json.loads(row.destination_ids or "[]")),
        is_final=bool(row.is_final),
        enabled=bool(row.enabled),
        # Coerce only None/missing → 100; NEVER falsy-zero: a 0%
        # (paused) canary must stay 0, not invert to a 100% full rollout.
        canary_percent=int(
            _cp if (_cp := getattr(row, "canary_percent", None)) is not None else 100
        ),
        # só None/ausente vira o default seguro (True = protege). Um
        # False explícito do operador é preservado (opt-out consciente da proteção).
        protect_detection=bool(
            _pd if (_pd := getattr(row, "protect_detection", None)) is not None else True
        ),
        # só None/ausente → default 100 (sem amostragem); um 0
        # explícito (rota drena tudo por sampling) é preservado, como no canary.
        sample_percent=int(
            _sp if (_sp := getattr(row, "sample_percent", None)) is not None else 100
        ),
        # suppression por assinatura (defaults = desligado).
        suppress_key=(getattr(row, "suppress_key", None) or None),
        suppress_allow=int(
            _sa if (_sa := getattr(row, "suppress_allow", None)) is not None else 0
        ),
        suppress_window_s=int(
            _sw if (_sw := getattr(row, "suppress_window_s", None)) is not None else 30
        ),
        redaction=_redaction,
    )


def _load_routes_for_org(org_id: Optional[int]) -> list[Any]:
    """Compiled enabled routes for ``org_id`` (sync DB). FAIL-SAFE: returns ``[]``
    on any error → ``route_batch`` then sends EVERYTHING to wazuh-default (zero
    silent loss, back-compat). Resolved once per cycle off-loop by the caller."""
    try:
        from ..db import repository

        with database.SessionLocal() as session:
            rows = repository.RouteRepository(session).list_enabled_for_org(org_id)
            return [_compile_route_row(r) for r in rows]
    except Exception:
        logger.exception(
            "routing: falha ao carregar rotas (org=%s) — fallback: tudo p/ wazuh-default",
            org_id,
        )
        return []


def _load_destination_residency(dest_ids: "set[str]") -> "dict[str, Optional[str]]":
    """Carrega o mapa {destination_id → data_residency} para o conjunto de ids.

    Best-effort: falha silenciosa → retorna {} (sem enforcement, conservador
    no sentido de nunca bloquear por erro de infraestrutura).
    """
    if not dest_ids:
        return {}
    try:
        from sqlalchemy import select as _select

        with database.SessionLocal() as _db:
            rows = _db.execute(
                _select(models.Destination.id, models.Destination.data_residency).where(
                    models.Destination.id.in_(dest_ids)
                )
            ).fetchall()
        return {str(r.id): (str(r.data_residency) if r.data_residency else None) for r in rows}
    except Exception:
        logger.warning("routing: falha ao carregar residency de destinos — enforcement desativado")
        return {}


_SYSLOG_DEST_KINDS = ("syslog_rfc3164", "syslog_rfc5424")


def _bare_host(value: "Optional[str]") -> "Optional[str]":
    """Hostname nu (sem scheme/credencial/porta/path), lowercased. None/'' → None."""
    if not value:
        return None
    v = str(value).strip()
    if "://" in v:
        v = v.split("://", 1)[1]
    v = v.split("/", 1)[0]
    if "@" in v:
        v = v.rsplit("@", 1)[1]
    if v.startswith("["):  # IPv6 bracketed [::1]:514
        v = v[1:].split("]", 1)[0]
    elif v.count(":") == 1:  # host:port (1 colon) — corta a porta
        v = v.rsplit(":", 1)[0]
    # 2+ colons sem bracket = IPv6 literal sem porta → mantém (não corta).
    return v.lower() or None


def _load_fallback_destination_id(org_id: Optional[int]) -> Optional[str]:
    """Vendor-neutro: id do Destination marcado ``is_default`` (catch-all).

    Resolução: o default da PRÓPRIA org tem precedência; senão um default GLOBAL
    (org IS NULL) compartilhado; senão ``None`` (→ não-roteados vão à DLQ/quarentena).
    Substitui o ``wazuh-default`` hardcoded — qualquer destino pode ser o fallback.
    Best-effort: erro/coluna ausente → ``None`` (fail-safe, sem inventar sink)."""
    try:
        from sqlalchemy import select as _select

        with database.SessionLocal() as _db:
            # default da org (precedência) → default global → None
            for _scope in ([org_id] if org_id is not None else []) + [None]:
                row = _db.execute(
                    _select(models.Destination.id)
                    .where(
                        models.Destination.is_default.is_(True),
                        models.Destination.organization_id.is_(None)
                        if _scope is None
                        else models.Destination.organization_id == _scope,
                    )
                    .limit(1)
                ).scalar_one_or_none()
                if row:
                    return str(row)
    except Exception:  # pragma: no cover — fail-safe: sem fallback inventado
        logger.debug("routing: falha ao resolver destino default (org=%s)", org_id, exc_info=True)
    return None


def _load_wazuh_loop_destination_ids(dest_ids: "set[str]") -> "frozenset[str]":
    """Ids de destinos que entregam de VOLTA ao manager Wazuh.

    Qualquer syslog dest cujo host casa (exato, normalizado) o host de uma
    integração Wazuh fecha o loop fonte↔destino — não só o sentinela
    ``wazuh-default``. Match EXATO: um host que não casa nunca é suprimido (sem
    risco de perda). Best-effort: erro → frozenset() (comportamento legado)."""
    if not dest_ids:
        return frozenset()
    try:
        from sqlalchemy import select as _select

        with database.SessionLocal() as _db:
            wz_rows = _db.execute(
                _select(
                    models.Integration.api_host,
                    models.Integration.base_url,
                    models.Integration.indexer_url,
                    # o syslog dest faz loopback para o
                    # MANAGER, não para o indexer — e na topologia canônica esses
                    # hosts DIFEREM. Sem manager_url, o match falhava no deploy padrão.
                    models.Integration.manager_url,
                ).where(models.Integration.platform == "wazuh")
            ).fetchall()
            wazuh_hosts = {
                bh
                for r in wz_rows
                for bh in (
                    _bare_host(r.api_host),
                    _bare_host(r.base_url),
                    _bare_host(r.indexer_url),
                    _bare_host(r.manager_url),
                )
                if bh
            }
            if not wazuh_hosts:
                return frozenset()
            dest_rows = _db.execute(
                _select(models.Destination.id, models.Destination.config).where(
                    models.Destination.id.in_(dest_ids),
                    models.Destination.kind.in_(_SYSLOG_DEST_KINDS),
                )
            ).fetchall()
        loopy: set[str] = set()
        for r in dest_rows:
            cfg = r.config or {}
            if isinstance(cfg, str):
                import json as _json

                try:
                    cfg = _json.loads(cfg)
                except Exception:
                    cfg = {}
            host = _bare_host((cfg or {}).get("host"))
            if host and host in wazuh_hosts:
                loopy.add(str(r.id))
        if loopy:
            logger.info("routing: %d syslog dest(s) apontam ao manager Wazuh — anti-loop", len(loopy))
        return frozenset(loopy)
    except Exception:
        logger.warning("routing: falha ao computar destinos-loop Wazuh — só o sentinela wazuh-default")
        return frozenset()


def _capture_outcomes(org_id: Optional[int], result: Any) -> None:
    """Escreve na captura os desfechos NÃO-entregues acumulados pelo routing engine.

    Um evento entregue a N destinos gera N registros ``delivered`` (no dispatch); aqui
    tratamos o que morreu ANTES: ``dropped`` (rota action=drop), ``unrouted`` (sem rota
    e sem default → DLQ), ``loop_blocked`` (anti-loop Wazuh), ``residency_blocked``
    (por par evento×destino) e ``sampled_out`` (redução). BEST-EFFORT integral: engole
    tudo — nem a captura nem a resolução de sessões podem afetar o enfileiramento.

    Os ``getattr`` são defensivos: os testes fazem monkeypatch de ``route_batch`` com
    resultados duck-typed que não têm os campos novos."""
    if org_id is None:
        return
    try:
        from . import capture_session

        # 1 resolução por lote, reusada em todos os desfechos (evita reabrir o índice
        # do org a cada bucket). [] ⇒ nada a fazer (caso comum, curto-circuitado).
        sessions = capture_session.active_sessions_sync(org_id)
        if not sessions:
            return

        def _emit(events: list, outcome: str) -> None:
            if events:
                _capture_sync(events, org_id, outcome, sessions=sessions)

        # (envelope, route_id) → detalhe = a rota responsável pelo drop.
        for _env, _rid in getattr(result, "dropped_events", None) or ():
            _capture_sync(
                [_env], org_id, capture_session.OUTCOME_DROPPED,
                detail=f"route={_rid}" if _rid else None, sessions=sessions,
            )
        _emit(list(getattr(result, "unrouted_events", None) or ()), capture_session.OUTCOME_UNROUTED)
        for _env, _reason in getattr(result, "loop_blocked_events", None) or ():
            _capture_sync(
                [_env], org_id, capture_session.OUTCOME_LOOP_BLOCKED,
                detail=_reason, sessions=sessions,
            )
        for _env, _dest in getattr(result, "residency_blocked_events", None) or ():
            _capture_sync(
                [_env], org_id, capture_session.OUTCOME_RESIDENCY_BLOCKED,
                destination_id=_dest, sessions=sessions,
            )
        for _env, _dest, _rid in getattr(result, "sampled_events", None) or ():
            _capture_sync(
                [_env], org_id, capture_session.OUTCOME_SAMPLED_OUT,
                destination_id=_dest, detail=f"route={_rid}" if _rid else None,
                sessions=sessions,
            )
    except Exception:  # noqa: BLE001 — captura nunca quebra o roteamento
        logger.debug("capture: falha ao registrar desfechos do roteamento", exc_info=True)


def _enqueue_routed(batch: list[Dict[str, Any]], routes: list[Any]) -> None:
    """Split the batch per-event via the routing engine and
    enqueue one (sub-)batch per resolved destination. ALL destinations (incl. the
    Wazuh syslog destination) go through the SAME path — Kafka data-plane when
    ``EVENT_DATAPLANE=kafka``, else the sharded ``dispatch_to_destination``. There is
    no longer a wazuh-default special-case (it is a normal syslog_rfc3164 dest)."""
    from . import tracing
    from .metrics import ROUTE_EVENTS, ROUTING_DECISIONS
    from .queues import dispatch_dest_shard_queue
    from .routing import route_batch
    from .tasks import dispatch_to_destination

    # traceparent corrente; VAZIO ⇒ kwargs byte-idêntico (tracing off).
    tp = tracing.carrier()

    # build residency map for all destination ids referenced in routes.
    # Loaded once per batch (not per-event) for efficiency.
    _all_dest_ids: set[str] = set()
    for r in routes:
        _all_dest_ids.update(r.destination_ids)
    _destination_residency = _load_destination_residency(_all_dest_ids)
    # destinos syslog que voltam ao manager Wazuh.
    _wazuh_loop_ids = _load_wazuh_loop_destination_ids(_all_dest_ids)
    # vendor-neutro: fallback = Destination marcado is_default p/ a org
    # (NÃO mais o wazuh-default hardcoded). None → não-roteados vão à DLQ/quarentena.
    _org_id = _batch_org_id(batch)
    _fallback_id = _load_fallback_destination_id(_org_id)

    # sampling de redução resolvido das flags (não se reduz sem
    # medir: exige COST_METERING_ENABLED). Default OFF ⇒ route_batch byte-idêntico.
    from .routing import SamplingConfig

    _sampling = SamplingConfig(
        enabled=bool(settings.REDUCTION_SAMPLE_ENABLED and settings.COST_METERING_ENABLED),
        protect_detection_enforced=bool(settings.REDUCTION_SAMPLE_PROTECT_DETECTION),
    )

    result = route_batch(
        batch,
        routes,
        fallback_destination_id=_fallback_id,
        destination_residency=_destination_residency,
        wazuh_loop_destination_ids=_wazuh_loop_ids,
        sampling=_sampling,
        # drop não tem flag REDUCTION_* (é config de rota, sempre ativa) — a medição
        # segue só o metering de custo. Off ⇒ nenhuma serialização no ramo de drop.
        measure_drop_bytes=bool(settings.COST_METERING_ENABLED),
    )

    # ── Tap de captura: DESFECHO de cada evento que NÃO foi entregue ────
    # O engine é puro e só ACUMULA os eventos por desfecho; a escrita é aqui. Antes,
    # o único tap ficava atrás da guarda ``accepted_total > 0`` do dispatch — ou seja,
    # drop/unrouted/loop/residency/sample eram INVISÍVEIS para quem estava "escutando".
    # Resolve as sessões UMA vez por lote (não por evento nem por desfecho).
    _capture_outcomes(_org_id, result)

    # Vendor-neutro: eventos sem rota E sem fallback configurado → DLQ/quarentena
    # (zero perda, visível, replayável) em vez de um sink hardcoded.
    if result.unrouted_events:
        from .delivery import persist_batch_dlq

        logger.warning(
            "routing: %d evento(s) sem rota e sem destino default (org=%s) — DLQ "
            "(error_kind=unrouted). Configure uma rota catch-all (condition={}) ou "
            "marque um destino como default.",
            len(result.unrouted_events), _org_id,
        )
        try:
            persist_batch_dlq(
                result.unrouted_events,
                destination_id="__unrouted__",
                error_kind="unrouted",
                organization_id=_org_id,
            )
        except Exception:  # pragma: no cover — DLQ best-effort, nunca derruba a coleta
            logger.exception("routing: falha ao persistir não-roteados na DLQ")

    for outcome, count in (
        ("routed", result.routed),
        ("dropped", result.dropped),
        ("fallback", result.fallback),
        ("unrouted", result.unrouted),
        ("residency_blocked", result.residency_blocked),
        # fonte wazuh suprimida do catch-all (loop).
        ("loop_blocked", result.loop_blocked),
    ):
        if count:
            ROUTING_DECISIONS.labels(outcome=outcome).inc(count)

    # per-route counters (action looked up from the compiled routes) —
    # OTel-native export (ops) + native observability store (self-sufficient UI).
    if result.per_route:
        from . import observability_store as _obs

        action_by_id = {r.id: r.action for r in routes}
        for route_id, count in result.per_route.items():
            action = action_by_id.get(route_id, "route")
            ROUTE_EVENTS.labels(route_id=route_id, action=action).inc(count)
            _obs.record_counter("route", route_id, "matched", count)
            # route|drop split: os eventos casados vão para o bucket da AÇÃO da rota
            # — a UI /flow lê ``routed_per_min`` da série ``route`` e ``drop_per_min``
            # da série ``drop``.
            _obs.record_counter("route", route_id, action, count)

    # eventos amostrados PARA FORA (redução) por rota. OTel
    # (collector_events_dropped_total{reason=sample}) + série nativa da UI. getattr
    # defensivo: resiliente a um result duck-typed (mocks de teste de route_batch).
    _sampled_per_route = getattr(result, "sampled_per_route", None)
    if _sampled_per_route:
        from .metrics import EVENTS_DROPPED
        from . import observability_store as _obs_s

        for route_id, count in _sampled_per_route.items():
            EVENTS_DROPPED.labels(route_id=route_id, reason="sample").inc(count)
            _obs_s.record_counter("route", route_id, "events_dropped", count)

    # volume evitado por AMOSTRAGEM: bytes do envelope medidos no engine (por par
    # evento×destino amostrado, MESMO serializador da entrega), agregados por org →
    # bytes_saved{reason=sample} (alimenta Evitado/Redução na /cost-summary). O engine
    # só mede quando o sampling está ativo; o helper de metering é gated
    # (REDUCTION_SAMPLE + COST_METERING) e fail-closed em org. Best-effort.
    _sampled_bytes = getattr(result, "sampled_bytes_per_org", None)
    if _sampled_bytes:
        from .reduction import metering as _metering_sample

        for _s_org, _s_bytes in _sampled_bytes.items():
            _metering_sample.record_sample_saving(_s_org, _s_bytes)

    # volume evitado por rotas ``action=drop``. Mesma base do sampling (envelope,
    # serializador da entrega), medida no engine só quando ``measure_drop_bytes``.
    # Gated APENAS por COST_METERING_ENABLED — drop é config de rota, sempre ativa,
    # não uma alavanca REDUCTION_*. Passa a EXIBIR economia que antes era invisível.
    _dropped_bytes = getattr(result, "dropped_bytes_per_org", None)
    if _dropped_bytes:
        from .reduction import metering as _metering_drop

        for _d_org, _d_bytes in _dropped_bytes.items():
            _metering_drop.record_drop_saving(_d_org, _d_bytes)

    # backpressure (drop_newest): resolve o plano de entrega POR DESTINO só
    # quando a feature está ON (sem custo de DB no default). Mapeia destination_id
    # → item {shard_queue, queue_ceiling, backpressure} para a decisão de shed.
    _shed_lookup: dict[str, Dict[str, Any]] = {}
    if settings.BACKPRESSURE_E6_ENABLED:
        from .delivery import resolve_dispatch_plan

        _shed_lookup = {
            p["destination_id"]: p
            for p in resolve_dispatch_plan(_batch_org_id(batch))
        }

    for dest_id, events in result.sub_batches.items():
        if not events:
            continue
        # Vendor-neutro: TODOS os destinos fluem uniformemente
        # pela MESMA via — wazuh-default não é mais special-case. Ele é um
        # ``Destination`` syslog_rfc3164 normal, entregue por
        # ``dispatch_batch_to_destination`` (mesmo wire RFC3164), com backpressure
        # E6 e durabilidade Kafka como qualquer outro destino.
        # descarta o sub-lote NOVO se a shard queue do destino passou do teto
        # (drop_newest) — protege o broker de OOM.
        _item = _shed_lookup.get(dest_id)
        if _item is not None and _should_shed_dispatch(_item, len(events)):
            continue
        if settings.EVENT_DATAPLANE == "kafka":
            # data-plane durável: sub-lote → tópico Kafka ``deliver`` (key=dest_id),
            # consumido pelo role dispatcher.
            from .dataplane import produce_delivery

            produce_delivery(dest_id, events, tp or None)
        else:
            dispatch_to_destination.apply_async(
                kwargs={"destination_id": dest_id, "batch": events, **tp},
                queue=dispatch_dest_shard_queue(dest_id),
            )


def _record_dest_observability(
    destination_id: str,
    accepted: int,
    rejected_count: int,
    latency_s: float,
    batch: list,
) -> None:
    """Native time-series rollups + live data-tap for a destination
    (Redis). Best-effort — the store swallows its own errors; this never affects
    delivery."""
    from . import observability_store as obs

    if accepted:
        obs.record_counter("dest", destination_id, "sent", accepted)
        # bytes (wire-proxy) entregues → série ``bytes`` no store
        # nativo, alimenta o ``bytes_per_min`` da health (eps_last_* AxoSyslog).
        nbytes = 0
        try:
            from .output._fastjson import dumps_bytes

            nbytes = sum(len(dumps_bytes(e)) for e in batch[:accepted])
            if nbytes:
                obs.record_counter("dest", destination_id, "bytes", nbytes)
        except Exception:  # pragma: no cover — best-effort, nunca afeta entrega
            pass
        # rollup de volume OUT por ORG, REUSANDO o nbytes já somado
        # (sem re-serializar). No-op quando o flag está off. Só atribui quando o lote
        # é single-org (o caso desta pipeline per-integration); lote multi-org ou sem
        # org → skip best-effort (o série per-destino acima já foi gravado). TODO o
        # bloco é best-effort: um envelope malformado (não-dict) NUNCA pode escapar e
        # falhar o dispatch (que já entregou) — mesma garantia do bloco de bytes acima.
        try:
            if metering.enabled():
                _org_ids = {
                    (e.get("_centralops") or {}).get("organization_id")
                    for e in batch[:accepted]
                    if isinstance(e, dict)
                }
                _org_ids.discard(None)
                if len(_org_ids) == 1:
                    metering.record_out(next(iter(_org_ids)), accepted, float(nbytes))
        except Exception:  # pragma: no cover — best-effort, nunca afeta entrega
            pass
    if rejected_count:
        obs.record_counter("dest", destination_id, "rejected", rejected_count)
    obs.record_latency("dest", destination_id, latency_s)
    # Live data-tap: redacted recent envelopes flowing to THIS destination.
    obs.record_tap(destination_id, batch)


def _record_lineage_for_batch(
    batch: list[Dict[str, Any]],
    destination_id: str,
    kind: str,
) -> None:
    """Write positive delivery lineage per (event_id, destination).

    Iterates the batch and calls ``lineage.record_delivery`` for each envelope
    that has a resolvable (org_id, event_id). Best-effort: the lineage module
    is fail-open; any individual failure is swallowed there. This function
    itself must never raise.
    """
    try:
        from .output.lineage import record_delivery

        now = time.time()
        for envelope in batch:
            meta = envelope.get("_centralops") or {}
            event_id: Optional[str] = meta.get("event_id") or None
            org_id_raw = meta.get("organization_id")
            if not event_id or org_id_raw is None:
                continue
            try:
                org_id = int(org_id_raw)
            except (TypeError, ValueError):
                continue
            record_delivery(
                org_id=org_id,
                event_id=event_id,
                destination_id=destination_id,
                kind=kind,
                ts=now,
            )
    except Exception:
        logger.debug(
            "_record_lineage_for_batch: erro inesperado (dest=%s) — ignorado",
            destination_id,
            exc_info=True,
        )


# Rate-limit do log de shed: 1 a cada N descartes por destino (evita spam).
_SHED_LOG_EVERY = 100
_shed_log_counter: Dict[str, int] = {}


def _should_shed_dispatch(item: Dict[str, Any], batch_len: int) -> bool:
    """E6: True se o lote deve ser DESCARTADO antes do enqueue (drop_newest).

    Só descarta quando: a flag E6 está ON, a política do destino é
    ``drop_newest``, há teto efetivo (>0) e a profundidade da shard queue passou
    do teto. Fail-open (broker inacessível → não descarta). Publica a métrica de
    profundidade observada sempre que conseguir lê-la.
    """
    if not settings.BACKPRESSURE_E6_ENABLED:
        return False
    if item.get("backpressure") != "drop_newest":
        return False

    ceiling = int(item.get("queue_ceiling") or 0) or settings.DISPATCH_QUEUE_CEILING
    if ceiling <= 0:
        return False

    from . import load_shedder
    from .metrics import DISPATCH_SHED_TOTAL, QUEUE_DEPTH

    shed, depth = load_shedder.should_shed(item["shard_queue"], ceiling)
    if depth is not None:
        QUEUE_DEPTH.labels(queue=item["shard_queue"]).set(depth)
        # native gauges for the UI (per destination) — queue depth +
        # current backpressure state (ok vs shedding at the ceiling).
        from . import observability_store as _obs

        _obs.set_gauge("dest", item["destination_id"], "queue_depth", depth)
        _obs.set_gauge(
            "dest", item["destination_id"], "backpressure_state", "shedding" if shed else "ok"
        )
    if not shed:
        return False

    dest_id = item["destination_id"]
    # DISPATCH_SHED_TOTAL is the AUTHORITATIVE shed counter. We deliberately do
    # NOT touch EVENTS_REJECTED here: that series is DLQ-bound (every
    # increment maps to a DestinationDeadLetter row) and a shed drop never writes
    # the DLQ — conflating them would over-report DLQ rejections by shed volume.
    DISPATCH_SHED_TOTAL.labels(destination_id=dest_id, reason="queue_ceiling").inc()

    count = _shed_log_counter.get(dest_id, 0) + 1
    _shed_log_counter[dest_id] = count
    if count % _SHED_LOG_EVERY == 1:
        logger.warning(
            "E6 load-shedding: destino %s acima do teto (depth=%s ceiling=%s) — "
            "descartando lote NOVO (drop_newest); %d descartes acumulados",
            dest_id, depth, ceiling, count,
        )
    return True


def _batch_org_id(batch: list[Dict[str, Any]]) -> Optional[int]:
    """organization_id do lote. Um batch é mono-integração,
    logo o ``_centralops.organization_id`` do 1º envelope é autoritativo.
    Retorna ``None`` se ausente (envelopes legados pré-S2 / fluxos sem org).

    COERÇÃO SEGURA: o ``isinstance(org_id, int)`` puro devolvia ``None`` quando o
    envelope trazia o org como STRING (``"1"``) — o que acontece depois de um
    round-trip JSON/Kafka ou num push-ingest. E ``None`` aqui faz a CAPTURA, o
    audit-ring e a LINHAGEM serem pulados em silêncio (sem log, sem métrica).
    Agora aceitamos int, string decimal e float INTEGRAL; qualquer outra coisa
    (``"abc"``, ``1.5``, ``True``) continua ``None`` — mas com log de debug, não mudo."""
    if not batch:
        return None
    meta = batch[0].get("_centralops") or {}
    org_id = meta.get("organization_id")
    if org_id is None:
        return None
    # bool é subclasse de int — True viraria org 1. Rejeita explicitamente.
    if isinstance(org_id, bool):
        pass
    elif isinstance(org_id, int):
        return org_id
    elif isinstance(org_id, float):
        if org_id.is_integer():
            return int(org_id)
    elif isinstance(org_id, str):
        try:
            return int(org_id.strip())
        except ValueError:
            pass
    logger.debug(
        "pipeline: organization_id não-coercível no envelope (type=%s) — "
        "captura/auditoria/linhagem serão puladas para este lote",
        type(org_id).__name__,
    )
    return None


def _load_destination_config(destination_id: str):
    """Carrega a config de UM destino do DB (sync). Retorna ``None`` se
    ausente ou desabilitado. Usado pelo dispatcher multi-destino."""
    from .output.destinations.registry import DestinationConfig

    with database.SessionLocal() as session:
        row = session.get(models.Destination, destination_id)
        if row is None or not row.enabled:
            return None
        try:
            cfg = json.loads(row.config or "{}")
        except json.JSONDecodeError:
            logger.warning("destino %s: config JSON corrompido, usando {}", destination_id)
            cfg = {}
        try:
            deliv = json.loads(row.delivery or "{}")
        except json.JSONDecodeError:
            deliv = {}
        return DestinationConfig(
            destination_id=row.id,
            kind=row.kind,
            config=cfg,
            delivery=deliv,
            secret_ref=row.secret_ref,
            config_version=row.config_version,
            name=row.name,
            organization_id=row.organization_id,
        )


def _chunk_batch(
    batch: list[Dict[str, Any]], max_items: int, max_bytes: int
) -> list[list[Dict[str, Any]]]:
    """Fatia o lote em chunks, fechando por ``max_items`` OU ``max_bytes`` —
    o que vier primeiro.

    ``max_bytes <= 0`` desativa o teto de bytes (fatia só por ``max_items``). O
    tamanho de um evento é estimado pelo seu JSON serializado (orjson, wire-proxy).
    Um evento isolado maior que ``max_bytes`` vira o seu próprio chunk — não há
    como fatiar abaixo de 1 evento; o sink/poison-pill (E3) lida com o excesso
    via DLQ. Sempre retorna ao menos 1 chunk (lista vazia inclusa) para o
    dispatcher exercer breaker/observability mesmo em lote vazio.
    """
    if not batch:
        return [[]]
    from .output._fastjson import dumps_bytes

    use_bytes = max_bytes > 0
    chunks: list[list[Dict[str, Any]]] = []
    cur: list[Dict[str, Any]] = []
    cur_bytes = 0
    for ev in batch:
        ev_bytes = len(dumps_bytes(ev)) if use_bytes else 0
        if cur and (
            len(cur) >= max_items
            or (use_bytes and cur_bytes + ev_bytes > max_bytes)
        ):
            chunks.append(cur)
            cur = []
            cur_bytes = 0
        cur.append(ev)
        cur_bytes += ev_bytes
    if cur:
        chunks.append(cur)
    return chunks or [[]]


async def _send_chunk_with_retry(
    *,
    target: Any,
    chunk: list[Dict[str, Any]],
    dcfg: Any,
    dest_config: Any,
    labels: Dict[str, str],
    redis: Any,
    circuit_breaker: Any,
    persist_rejected_to_dlq: Any,
    DELIVERY_LATENCY: Any,
    DLQ_TOTAL: Any,
    EVENTS_REJECTED: Any,
    EVENTS_SENT: Any,
    BYTES_SENT: Any,
    RETRIES: Any,
) -> Any:
    """Send ONE chunk with exponential-backoff retry.

    Enforces:
      - ``timeout_ms`` per attempt via ``asyncio.wait_for``.
      - ``retry.enabled`` — when False, delivers once (no backoff retries).
      - ``retry.max_retries`` — after exhaustion, raises ``TransientDeliveryError``
        for the outer Celery autoretry to handle.
      - ``retry.max_elapsed_ms`` — caps the TOTAL time spent retrying this chunk;
        once exceeded it stops retrying even if ``max_retries`` is not reached.
      - Retry only on retryable failures (5xx / 429 / timeout).
        4xx / poison-pill (``result.retryable=False``) → DLQ immediately, no retry.

    Emits ``EVENTS_SENT``/``EVENTS_REJECTED``/``BYTES_SENT``/``DELIVERY_LATENCY``
    and ``RETRIES`` (one per retry attempt). Returns the last ``DeliveryResult``.
    """
    from .delivery import TransientDeliveryError
    from .output._fastjson import dumps_bytes
    from .output.delivery_config import backoff_delay_s

    timeout_s = dcfg.timeout_ms / 1000.0
    retry_cfg = dcfg.retry
    # retry.enabled=False → entrega 1x (max_retries efetivo 0).
    max_retries = retry_cfg.max_retries if retry_cfg.enabled else 0
    # teto de tempo TOTAL re-tentando este chunk (None = sem teto).
    max_elapsed_s = (
        retry_cfg.max_elapsed_ms / 1000.0 if retry_cfg.max_elapsed_ms > 0 else None
    )
    loop_start = time.monotonic()
    # Bytes do chunk (wire-proxy) para BYTES_SENT — calculado uma vez.
    chunk_nbytes = sum(len(dumps_bytes(ev)) for ev in chunk)

    for attempt in range(max_retries + 1):  # attempt 0 = initial try
        if attempt > 0:
            # teto de tempo total — para de re-tentar ao exceder.
            if (
                max_elapsed_s is not None
                and (time.monotonic() - loop_start) >= max_elapsed_s
            ):
                logger.warning(
                    "dispatch_to_destination: max_elapsed_ms (%d) excedido dest=%s "
                    "no attempt=%d — interrompe retries (vai à DLQ/autoretry)",
                    retry_cfg.max_elapsed_ms,
                    dest_config.destination_id,
                    attempt,
                )
                raise TransientDeliveryError(dest_config.destination_id)
            RETRIES.labels(**labels).inc()
            delay = backoff_delay_s(retry_cfg, attempt - 1)
            logger.info(
                "dispatch_to_destination: retry attempt=%d/%d dest=%s — "
                "aguardando %.3fs (exponential backoff)",
                attempt,
                max_retries,
                dest_config.destination_id,
                delay,
            )
            await asyncio.sleep(delay)

        # Check breaker before each retry (state may have changed).
        await circuit_breaker.check_for_config(redis, dest_config)

        started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                target.send_batch(chunk),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - started
            DELIVERY_LATENCY.labels(**labels).observe(elapsed)
            await circuit_breaker.record_failure_for_config(redis, dest_config)
            logger.warning(
                "dispatch_to_destination: timeout (%.0f ms) dest=%s attempt=%d/%d",
                dcfg.timeout_ms,
                dest_config.destination_id,
                attempt,
                max_retries,
            )
            if attempt >= max_retries:
                raise TransientDeliveryError(dest_config.destination_id)
            continue  # retry

        elapsed = time.monotonic() - started
        DELIVERY_LATENCY.labels(**labels).observe(elapsed)

        EVENTS_SENT.labels(**labels).inc(result.accepted)
        if result.accepted > 0:
            # bytes (wire-proxy) efetivamente entregues ao destino.
            BYTES_SENT.labels(**labels).inc(chunk_nbytes)
        for rej in result.rejected:
            EVENTS_REJECTED.labels(error_kind=rej.error_kind, **labels).inc()

        # 4xx / poison-pill: DLQ immediately, never retry.
        if result.rejected and not result.retryable:
            persist_ok = await asyncio.to_thread(
                persist_rejected_to_dlq, dest_config, result.rejected, chunk
            )
            if persist_ok:
                for rej in result.rejected:
                    DLQ_TOTAL.labels(
                        destination_id=dest_config.destination_id,
                        kind=dest_config.kind,
                        error_kind=rej.error_kind,
                    ).inc()
            elif result.rejected:
                # DLQ write failed — treat as transient so acks_late catches it.
                logger.error(
                    "dispatch_to_destination: DLQ persist FAILED destination_id=%s "
                    "(%d rejeitados) — re-tentando lote (E3 durability)",
                    dest_config.destination_id,
                    len(result.rejected),
                )
                raise TransientDeliveryError(dest_config.destination_id)
            # No further retry for deterministic rejects — return result.
            if result.accepted > 0 and not result.retryable:
                await circuit_breaker.record_success_for_config(redis, dest_config)
            return result

        # Transient / retryable result (5xx / 429).
        if result.retryable:
            await circuit_breaker.record_failure_for_config(redis, dest_config)

            # Guard: if the sender says retryable=True but already accepted/rejected
            # events, a whole-batch retry would DUPLICATE them.
            if result.accepted or result.rejected:
                logger.error(
                    "dispatch_to_destination: sender kind=%s returned retryable=True "
                    "with accepted=%d rejected=%d — whole-batch retry would "
                    "DUPLICATE accepted events (E2). Needs per-item retry before "
                    "this sender is enabled.",
                    dest_config.kind,
                    result.accepted,
                    len(result.rejected),
                )
                raise TransientDeliveryError(dest_config.destination_id)

            if attempt >= max_retries:
                raise TransientDeliveryError(dest_config.destination_id)
            continue  # retry with backoff

        # Clean delivery — heal the breaker.
        if result.accepted > 0 and not result.rejected:
            await circuit_breaker.record_success_for_config(redis, dest_config)
        return result

    # Should not reach here (loop always returns or raises), but satisfy mypy.
    raise TransientDeliveryError(dest_config.destination_id)  # pragma: no cover


async def dispatch_batch_to_destination(
    destination_id: str, batch: list[Dict[str, Any]]
) -> None:
    """Worker de dispatch **por destino**.

    Totalmente funcional.  Resolve o destino, decifra secrets,
    verifica circuit breaker, fatia o lote em chunks
    de ``batch.max_items``, envia cada chunk com timeout e retry
    exponencial, persiste rejeições parciais na DLQ e
    propaga erros transitórios como ``TransientDeliveryError`` para o
    autoretry do Celery.

    Enforcement:
      - ``batch.max_items``  → chunk size enforced here.
      - ``timeout_ms``       → asyncio.wait_for per send.
      - ``retry``            → exponential backoff per chunk, max_retries honoured.

    Este caminho ENTREGA TODOS os destinos, INCLUSIVE o
    ``wazuh-default`` (kind syslog_rfc3164 — mesmo wire RFC3164). Não há mais lane
    dedicada Wazuh; este é o único caminho de entrega por destino.
    """
    from ..core.secrets import get_default_backend
    from . import circuit_breaker
    from .celery_app import get_worker_redis
    from .delivery import persist_rejected_to_dlq
    from .metrics import (
        BYTES_SENT,
        DELIVERY_LATENCY,
        DLQ_TOTAL,
        EVENTS_REJECTED,
        EVENTS_SENT,
        RETRIES,
    )
    from .output.concurrency_pool import get_semaphore  # per-loop, 2nd layer
    from .output.delivery_config import parse_delivery_lenient
    from .output.destination_cache import get_destination
    from .output.destination_limiter import DestinationLimiter  # cross-process

    dest_config = await asyncio.to_thread(_load_destination_config, destination_id)
    if dest_config is None:
        # Destino deletado/desabilitado DEPOIS do enqueue. No fan-out
        # aditivo o Wazuh já tem o lote inteiro, então uma cópia perdida era
        # aceitável. Mas no roteamento AUTORITATIVO estes eventos podem
        # não ter outra cópia — descartar seria PERDA SILENCIOSA. Preservamos na
        # DLQ (recuperável) em vez de no-op, mantendo a garantia de zero perda.
        from .delivery import persist_batch_dlq
        from .metrics import DISPATCH_FAILURES

        logger.warning(
            "dispatch_to_destination: destino %s ausente/desabilitado — "
            "persistindo lote na DLQ (error_kind=destination_missing, zero perda)",
            destination_id,
        )
        await asyncio.to_thread(
            persist_batch_dlq,
            batch,
            destination_id=destination_id,
            error_kind="destination_missing",
            organization_id=_batch_org_id(batch),
        )
        DISPATCH_FAILURES.labels(
            target="destination", reason="destination_missing", destination_id=destination_id
        ).inc()
        await _capture_delivery_failed(
            batch, destination_id, "destino ausente/desabilitado (DLQ)"
        )
        return

    # Tenancy invariant: cross-tenant fail-closed guard.
    # Uma rota mal-escopada/global pode nomear um Destination de OUTRO tenant; entregar
    # vazaria os eventos do tenant A para o sink do tenant B. O consumer do data-plane
    # despacha puramente por destination_id, então este é o chokepoint que torna o
    # organization_id (que já sobrevive ao hop Kafka) DECISIVO no lado de consumo.
    # Destinos globais (org=NULL) seguem permitidos (wazuh-default, sinks globais
    # compartilhados). Cobre AMBAS as lanes (consumer Kafka + worker Celery chamam aqui).
    _batch_org = _batch_org_id(batch)
    if (
        dest_config.organization_id is not None
        and _batch_org is not None
        and dest_config.organization_id != _batch_org
    ):
        from .delivery import persist_batch_dlq
        from .metrics import DISPATCH_FAILURES

        logger.error(
            "dispatch_to_destination: CROSS-TENANT recusado dest=%s dest_org=%s batch_org=%s "
            "— lote p/ DLQ (error_kind=cross_tenant_destination, fail-closed)",
            destination_id, dest_config.organization_id, _batch_org,
        )
        await asyncio.to_thread(
            persist_batch_dlq,
            batch,
            destination_id=destination_id,
            error_kind="cross_tenant_destination",
            organization_id=_batch_org,
        )
        DISPATCH_FAILURES.labels(
            target="destination", reason="cross_tenant_destination", destination_id=destination_id
        ).inc()
        await _capture_delivery_failed(
            batch, destination_id, "destino de outro tenant (fail-closed, DLQ)"
        )
        return

    # parse the validated delivery policy once (breaker/concurrency/
    # backpressure/shadow). Lenient — never raises on a bad row.
    dcfg = parse_delivery_lenient(dest_config.kind, dest_config.delivery)

    # agregação log→métrica ANTES do chunk (torna o rollup por-destino
    # real). Opt-in por-destino (aggregate.group_by não-vazio) + gated pelas flags. Fail-
    # open anti-OOM dentro de coalesce. Detecção nunca é agregada (é opt-in por-destino —
    # quem alimenta detecção não recebe aggregate). record_saving atribui os bytes evitados.
    if (
        settings.REDUCTION_AGGREGATE_ENABLED
        and settings.COST_METERING_ENABLED
        and dcfg.aggregate.group_by
    ):
        from .reduction import metering as _agg_metering
        from .reduction.aggregate import coalesce as _coalesce

        batch, _agg_bytes, _agg_events = _coalesce(
            batch, dcfg.aggregate.group_by, dcfg.aggregate.max_groups
        )
        if _agg_events:
            _agg_metering.record_saving(
                dest_config.organization_id, destination_id, "aggregate", bytes_=float(_agg_bytes)
            )
            logger.info(
                "aggregate: dest=%s colapsou %d evento(s) (%d bytes) por %s",
                destination_id, _agg_events, _agg_bytes, dcfg.aggregate.group_by,
            )

    # resolve secret_ref via secrets backend when present.
    secrets = (
        await asyncio.to_thread(get_default_backend)
        if dest_config.secret_ref
        else None
    )
    target = await get_destination(dest_config, secrets)
    labels = {"destination_id": dest_config.destination_id, "kind": dest_config.kind}

    # Shadow mode: format + measure, DO NOT deliver. No send, no breaker, no
    # DLQ, no Redis. A destination flagged ``delivery.shadow=true`` exercises its
    # formatter against real routed traffic (canary/preview) without emitting a
    # single byte to the sink — safe SIEM-to-SIEM cutover rehearsal.
    if dcfg.shadow:
        from .metrics import SHADOW_EVENTS, SHADOW_LATENCY

        started = time.monotonic()
        formatted = 0
        for envelope in batch:
            try:
                target.format(envelope)
                formatted += 1
            except NotImplementedError:
                # kind without a decoupled formatter — count as a shadow pass.
                formatted += 1
            except Exception:
                logger.debug(
                    "shadow: falha ao formatar 1 evento dest=%s (sinal de shadow)",
                    dest_config.destination_id,
                    exc_info=True,
                )
        SHADOW_LATENCY.labels(**labels).observe(time.monotonic() - started)
        SHADOW_EVENTS.labels(**labels).inc(formatted)
        logger.info(
            "E7 shadow: dest=%s formatou %d/%d eventos (SEM entrega)",
            dest_config.destination_id, formatted, len(batch),
        )
        return

    # circuit breaker guard + bulkhead + chunked send.
    redis = get_worker_redis()
    try:
        # fatia o lote por max_items E max_bytes (o que fechar
        # primeiro), de modo que um sink com teto próprio de tamanho não rejeite
        # o lote inteiro nem receba um payload maior que ``batch.max_bytes``.
        chunks: list[list[Dict[str, Any]]] = _chunk_batch(
            batch, dcfg.batch.max_items, dcfg.batch.max_bytes
        )

        # Bulkhead — two layers acquired ONCE around ALL chunk sends for this
        # destination (a slow sink must not starve others, nor be hammered by
        # N workers at N×cap):
        #   1. cross-process: a global Redis lease caps concurrent send_batch for
        #      this destination across ALL prefork workers/hosts (ceiling =
        #      dcfg.concurrency). Fail-open — a Redis fault NEVER blocks delivery.
        #   2. per-loop: the asyncio.Semaphore bounds intra-process fan-out.
        dest_limiter = DestinationLimiter(redis)
        sem = get_semaphore(dest_config.destination_id, dcfg.concurrency)
        async with dest_limiter.slot(
            dest_config.destination_id, dcfg.concurrency
        ), sem:
            last_result = None
            # Acumuladores cross-chunk para observability e lineage corretos.
            # Corrige sub-report de lotes multi-chunk.
            accepted_total: int = 0
            rejected_total: int = 0
            # IDs rejeitados em qualquer chunk com 4xx (retryable=False).
            # Usado para excluir esses eventos do registro de lineage.
            rejected_event_ids: set[str] = set()
            for chunk in chunks:
                last_result = await _send_chunk_with_retry(
                    target=target,
                    chunk=chunk,
                    dcfg=dcfg,
                    dest_config=dest_config,
                    labels=labels,
                    redis=redis,
                    circuit_breaker=circuit_breaker,
                    persist_rejected_to_dlq=persist_rejected_to_dlq,
                    DELIVERY_LATENCY=DELIVERY_LATENCY,
                    DLQ_TOTAL=DLQ_TOTAL,
                    EVENTS_REJECTED=EVENTS_REJECTED,
                    EVENTS_SENT=EVENTS_SENT,
                    BYTES_SENT=BYTES_SENT,
                    RETRIES=RETRIES,
                )
                accepted_total += last_result.accepted
                rejected_total += len(last_result.rejected)
                # Acumula event_ids rejeitados com 4xx (não-retryable) para
                # excluir do lineage — esses eventos foram para a DLQ, não
                # devem contar como "delivered".
                if last_result.rejected and not last_result.retryable:
                    for rej in last_result.rejected:
                        rejected_event_ids.add(rej.event_id)

        # native observability rollups — best-effort, off the event loop.
        # Usa os TOTAIS acumulados cross-chunk em vez de last_result (lotes
        # multi-chunk reportavam apenas o último chunk).
        if last_result is not None:
            await asyncio.to_thread(
                _record_dest_observability,
                dest_config.destination_id,
                accepted_total,
                rejected_total,
                0.0,  # elapsed already observed per-chunk above
                batch,
            )

        # Audit ring: record WHAT was dispatched so the /config
        # "Auditoria" panel (and the capture/listening mode) can show the wire
        # payloads for troubleshooting. The write-path was lost in the data-plane
        # split — record_batch() existed but was no longer called from
        # any dispatch path. Best-effort, gated on actual delivery, off the event
        # loop's critical section: NEVER affects the dispatch outcome.
        if redis is not None and last_result is not None and accepted_total > 0:
            from . import audit_buffer

            _audit_org = _batch_org_id(batch)
            if _audit_org is not None:
                _dkind = getattr(dest_config, "kind", None)
                _syslog_fmt = (
                    "rfc3164"
                    if _dkind == "syslog_rfc3164"
                    else "rfc5424"
                    if _dkind == "syslog_rfc5424"
                    else None
                )
                try:
                    await audit_buffer.record_batch(
                        redis, batch, _audit_org, syslog_format=_syslog_fmt
                    )
                except Exception:  # pragma: no cover — auditoria nunca quebra o dispatch
                    logger.debug(
                        "audit_buffer.record_batch falhou (não-fatal)", exc_info=True
                    )

        # Captura (/config "escuta"): DESFECHO por destino. Fora da guarda
        # ``accepted_total > 0`` de propósito — um lote 100% rejeitado/falho é
        # exatamente o que o operador precisa ver ("saiu ou morreu no sink?").
        # Auditoria e captura são INDEPENDENTEMENTE best-effort: uma falha numa não
        # impede a outra, e NENHUMA afeta o dispatch.
        # try/except PRÓPRIO (espelha o do audit_buffer acima): ``_capture_outcome`` já
        # é best-effort internamente, mas o código AQUI não era — as list-comprehensions
        # chamam ``e.get(...)`` e um envelope não-dict no lote levantaria AttributeError
        # dentro do try grande do dispatch, cujo handler RE-LEVANTA. Captura nunca pode
        # derrubar a entrega.
        try:
            _cap_org = _batch_org_id(batch)
            if _cap_org is not None:
                from .capture_session import OUTCOME_DELIVERED, OUTCOME_DELIVERY_FAILED

                _rejected_ids = rejected_event_ids if last_result is not None else set()
                if last_result is None:
                    # nenhum chunk chegou a ser enviado (lote vazio) — nada a registrar.
                    _accepted_envs, _failed_envs = [], []
                elif accepted_total <= 0:
                    _accepted_envs, _failed_envs = [], list(batch)
                elif _rejected_ids:
                    _accepted_envs = [
                        e for e in batch
                        if isinstance(e, dict)
                        and (e.get("_centralops") or {}).get("event_id") not in _rejected_ids
                    ]
                    _failed_envs = [
                        e for e in batch
                        if isinstance(e, dict)
                        and (e.get("_centralops") or {}).get("event_id") in _rejected_ids
                    ]
                else:
                    _accepted_envs, _failed_envs = list(batch), []
                await _capture_outcome(
                    _accepted_envs, _cap_org, OUTCOME_DELIVERED,
                    destination_id=dest_config.destination_id,
                )
                await _capture_outcome(
                    _failed_envs, _cap_org, OUTCOME_DELIVERY_FAILED,
                    destination_id=dest_config.destination_id,
                    detail="sink rejeitou/não aceitou o lote",
                )
        except Exception:  # noqa: BLE001 — captura nunca quebra a entrega
            logger.debug("capture: falha ao registrar desfecho de entrega", exc_info=True)

        # lineage — record positive delivery per (event_id, dest).
        # Best-effort (fail-open), gated by LINEAGE_ENABLED.  Runs only when
        # at least one event was accepted (last_result may be None for empty
        # batches; skip in that case to avoid pointless Redis round-trips).
        #
        # Correção: passa apenas os envelopes ACEITOS (exclui os
        # event_ids presentes em rejected_event_ids de chunks 4xx). Sem
        # granularidade por item no resultado, filtramos pelo conjunto de
        # IDs rejeitados que o sender devolveu nos RejectedEvent.
        if last_result is not None and accepted_total > 0:
            if rejected_event_ids:
                accepted_envelopes = [
                    env for env in batch
                    if (env.get("_centralops") or {}).get("event_id")
                    not in rejected_event_ids
                ]
            else:
                accepted_envelopes = batch
            await asyncio.to_thread(
                _record_lineage_for_batch,
                accepted_envelopes,
                dest_config.destination_id,
                dest_config.kind,
            )

    except circuit_breaker.BreakerOpen:
        # terminal — propagate out to the except-all in
        # dispatch_to_destination, which calls dispatch_to_dlq (no autoretry).
        # Registra o desfecho ANTES de propagar (best-effort: não mascara o erro) —
        # sem isto, "o breaker abriu" é justamente o caso invisível na escuta.
        await _capture_delivery_failed(batch, destination_id, "circuit breaker aberto")
        raise
    except Exception as _dispatch_exc:  # noqa: BLE001 — só observa e re-levanta
        await _capture_delivery_failed(
            batch, destination_id, f"falha de entrega: {type(_dispatch_exc).__name__}"
        )
        raise
    finally:
        # Best-effort close: a dead Redis client must not turn a delivered batch
        # into a task failure.
        try:
            await redis.aclose()
        except Exception:  # pragma: no cover - defensive
            logger.debug(
                "dispatch_to_destination: redis.aclose() falhou (ignorado)",
                exc_info=True,
            )
    # ``BYTES_SENT`` é incrementado em ``_send_chunk_with_retry`` com os
    # bytes (wire-proxy) do chunk efetivamente entregue.
