"""REST endpoints para a configuração runtime do Collector.

Endpoints (todos admin-only):

- ``GET /api/collectors/config`` — retorna snapshot atual + meta
  (``is_persisted``, ``config_version``).
- ``PUT /api/collectors/config`` — update parcial; invalida cache Redis
  para propagar aos workers em até 30s.

Gerencia os mesmos parâmetros que o ``.env`` segue — mas com UI e sem
restart de container.

**Removido em ago/2026: ``POST /config/test``.** Ele sondava
``wazuh_syslog_host``/``wazuh_dispatch_mode``, que nenhum formulário expõe e
nenhum despachante lê desde que a saída passou a ser o sistema de Destinos
(``collectors/output/destinations/``). Numa instalação nova esses campos são
NULL, então o botão respondia "wazuh_syslog_host não configurado" em 100% dos
cliques, sempre. Testar o caminho de saída de verdade é
``POST /api/destinations/{id}/test``, que sonda o destino configurado.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

import redis.asyncio as redis_async
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..api import schemas
from ..core import auth as app_auth
from ..core import tenant
from ..core.config import settings
from ..core.errors import ApiError
from ..db import database, models, repository
from ..collectors import capture_session
from ..collectors.config_loader import (
    CollectorConfigSnapshot,
    invalidate_collector_config,
    load_from_db_session,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/collectors/config", tags=["collector-config"])


# ── Helpers ───────────────────────────────────────────────────────────


def _get_repo(db: Session = Depends(database.get_session)) -> repository.CollectorConfigRepository:
    return repository.CollectorConfigRepository(db)


def _snapshot_to_read(snapshot: CollectorConfigSnapshot) -> schemas.CollectorConfigRead:
    return schemas.CollectorConfigRead(
        id=1,
        is_persisted=snapshot.is_persisted,
        config_version=snapshot.config_version,
        wazuh_syslog_host=snapshot.wazuh_syslog_host,
        wazuh_syslog_port=snapshot.wazuh_syslog_port,
        wazuh_syslog_use_tls=snapshot.wazuh_syslog_use_tls,
        wazuh_ca_bundle=snapshot.wazuh_ca_bundle,
        wazuh_dispatch_mode=snapshot.wazuh_dispatch_mode,
        wazuh_syslog_format=snapshot.wazuh_syslog_format,
        collector_jsonl_dir=snapshot.collector_jsonl_dir,
        collector_batch_size=snapshot.collector_batch_size,
        collector_batch_flush_seconds=snapshot.collector_batch_flush_seconds,
        dedupe_ttl_days=snapshot.dedupe_ttl_days,
        dedupe_ttl_seconds=snapshot.effective_dedupe_ttl_seconds,
        domain_concurrency_limits=snapshot.domain_concurrency_limits,
        rate_limits_by_vendor=snapshot.rate_limits_by_vendor,
        updated_at=None,  # preenchido abaixo se tiver row
    )


async def _redis_client() -> redis_async.Redis:
    return redis_async.from_url(
        settings.REDIS_URL or "redis://localhost:6379/0",
        decode_responses=True,
    )


# ── GET ───────────────────────────────────────────────────────────────


@router.get("", response_model=schemas.CollectorConfigRead)
def get_config(
    _: models.AppUser = Depends(app_auth.require_admin_user),
    db: Session = Depends(database.get_session),
    repo: repository.CollectorConfigRepository = Depends(_get_repo),
) -> schemas.CollectorConfigRead:
    """Lê config atual. Não usa cache Redis aqui (é admin UI — quer estado real do DB)."""
    row = repo.get()
    snapshot = load_from_db_session(db)
    result = _snapshot_to_read(snapshot)
    if row is not None:
        result.updated_at = row.updated_at
    return result


# ── PUT ───────────────────────────────────────────────────────────────


@router.put("", response_model=schemas.CollectorConfigRead)
async def update_config(
    payload: schemas.CollectorConfigUpdate,
    current_user: models.AppUser = Depends(app_auth.require_admin_user),
    db: Session = Depends(database.get_session),
    repo: repository.CollectorConfigRepository = Depends(_get_repo),
) -> schemas.CollectorConfigRead:
    """Partial update + invalida cache Redis. Workers reflete em até 30s."""
    # Config do coletor é PLATAFORMA (rate limits, dedupe TTL, concorrência —
    # vale p/ todos os tenants) — só admin global.
    tenant.require_global_scope(current_user)
    data = payload.model_dump(exclude_unset=True)
    row = repo.update(**data)

    # Invalida cache best-effort.
    redis = await _redis_client()
    try:
        await invalidate_collector_config(redis)
    finally:
        await redis.aclose()

    snapshot = load_from_db_session(db)
    result = _snapshot_to_read(snapshot)
    result.updated_at = row.updated_at
    logger.info(
        "collector_config: updated by admin; version=%s",
        snapshot.config_version,
    )
    return result


# ── Captura ao vivo / "listening" (sessões de captura sob demanda) ────────────


def _capture_effective_org(org_id: Optional[int], user: models.AppUser) -> Optional[int]:
    effective = org_id if org_id is not None else user.organization_id
    # admin escopado só captura a PRÓPRIA subárvore — org_id
    # explícito de outra org seria leitura cross-tenant de tráfego ao vivo. O
    # check central cobre os 5 endpoints de capture-session. Global bypassa.
    if effective is not None:
        tenant.require_subtree_access(user, effective)
    return effective


# ── Escopo HIERÁRQUICO da captura ────────────────────────────────────────────
#
# O gate de autorização (``require_subtree_access``) é SUBTREE-aware: um admin da
# org PAI pode abrir captura na org FILHA. A gravação (``capture_session.record``),
# porém, indexa a sessão pela org EXATA do evento — logo, uma sessão aberta no PAI
# nunca via o tráfego dos FILHOS (o admin lia "capturei nada" com tráfego correndo
# na subárvore que ele legitimamente enxerga).
#
# Correção (a mais simples e segura): no START, a sessão é indexada em TODAS as
# orgs do escopo — ``subárvore(org efetiva) ∩ orgs acessíveis ao usuário``. Assim o
# tap de ``record()`` (que continua olhando só o índice da org do evento) encontra a
# mesma sessão e escreve num ÚNICO ring; a leitura já sai agregada, sem fan-in no
# read-path. O escopo coberto fica EXPLÍCITO na sessão (``scope_org_ids``, gravado no
# meta e devolvido pela API) — o operador vê exatamente o que a sessão cobre.
#
# Isolamento: o escopo é sempre INTERSECTADO com ``tenant.accessible_org_ids`` — nunca
# alcança uma org que o usuário já não podia ver. Falha de resolução ⇒ fail-closed
# (escopo = só a org efetiva).
#
# Trade-off conhecido: sessões do PAI ocupam slot no índice do FILHO, então contam
# para o teto ``MAX_SESSIONS_PER_ORG`` do filho. Preferimos isso a um segundo índice
# (que exigiria mudar o hot path de ``record()``).


def _org_subtree_ids(db: Session, root_org_id: int) -> Set[int]:
    """IDs da subárvore de ``root_org_id`` (inclusive), via
    ``Organization.parent_organization_id``.

    Em Community a hierarquia é FLAT (parent sempre ``None``) ⇒ ``{root_org_id}``;
    em Enterprise as colunas são materializadas e o walk devolve os descendentes.
    Fail-closed: qualquer erro ⇒ só a própria org.
    """
    try:
        rows = db.query(
            models.Organization.id, models.Organization.parent_organization_id
        ).all()
    except Exception as exc:  # pragma: no cover — defensivo
        logger.warning("capture: falha ao resolver subárvore de org=%s: %s", root_org_id, exc)
        return {root_org_id}

    children: Dict[Optional[int], List[int]] = {}
    for org_id, parent_id in rows:
        children.setdefault(parent_id, []).append(org_id)

    out: Set[int] = {root_org_id}
    frontier = [root_org_id]
    while frontier:
        nxt: List[int] = []
        for org_id in frontier:
            for child in children.get(org_id, ()):
                if child not in out:
                    out.add(child)
                    nxt.append(child)
        frontier = nxt
    return out


def _capture_scope_org_ids(
    user: models.AppUser, db: Session, effective_org: int
) -> List[int]:
    """Orgs cobertas por uma sessão aberta em ``effective_org``.

    ``subárvore(effective_org) ∩ acessíveis(user)`` — admin global não tem filtro de
    acesso (``accessible_org_ids`` ⇒ ``None``), então cobre a subárvore inteira. A org
    efetiva SEMPRE entra (o gate de autorização já a validou)."""
    subtree = _org_subtree_ids(db, effective_org)
    try:
        accessible = tenant.accessible_org_ids(user, db)
    except Exception as exc:  # pragma: no cover — defensivo, fail-closed
        logger.warning("capture: falha ao resolver escopo do usuário: %s", exc)
        accessible = set()
    if accessible is not None:
        subtree &= set(accessible)
    subtree.add(effective_org)
    return sorted(subtree)


# TTL fixo do índice — espelha ``capture_session.start_session`` (não regride).
_CAPTURE_INDEX_TTL = capture_session.MAX_DURATION_SECONDS + capture_session.GRACE_SECONDS
_SCOPE_META_FIELD = "scope_org_ids"
# Convenção OPCIONAL de contadores por desfecho no meta (``outcome:dropped`` etc.).
# Se o engine passar a mantê-los (``hincrby`` ao lado de ``event_count``), a API os
# expõe automaticamente; até lá o campo sai vazio.
_OUTCOME_META_PREFIX = "outcome:"


async def _index_session_in_scope(
    redis: redis_async.Redis,
    session_id: str,
    scope_org_ids: List[int],
    owner_org_id: int,
) -> None:
    """Indexa a sessão nas orgs do escopo (além da dona, já feita pelo engine) e
    persiste o escopo no meta. Best-effort: falhar aqui degrada a sessão para
    "só a org dona" — nunca derruba o start."""
    try:
        pipe = redis.pipeline()
        for org_id in scope_org_ids:
            if org_id == owner_org_id:
                continue
            key = capture_session._org_index_key(org_id)
            pipe.sadd(key, session_id)
            pipe.expire(key, _CAPTURE_INDEX_TTL)
        pipe.hset(
            capture_session._meta_key(session_id),
            _SCOPE_META_FIELD,
            ",".join(str(o) for o in scope_org_ids),
        )
        await pipe.execute()
        # O tap memoiza "org sem sessão" (cache NEGATIVO); as orgs recém-incluídas
        # no escopo precisam sair dele para não perder eventos da janela inicial.
        for org_id in scope_org_ids:
            capture_session.reset_session_cache(org_id)
    except Exception as exc:  # pragma: no cover — não-fatal
        logger.warning("capture: fan-out de escopo falhou para sessão %s: %s", session_id, exc)


async def _session_extras(
    redis: redis_async.Redis, session_id: str, owner_org_id: Optional[int]
) -> tuple[List[int], Dict[str, int]]:
    """``(scope_org_ids, outcome_counts)`` num único ``HGETALL`` do meta.

    ``outcome_counts`` são os contadores POR DESFECHO da sessão inteira, se o engine
    os mantiver (campos ``outcome:<nome>`` no meta, ao lado de ``event_count``). Enquanto
    não existirem, sai ``{}`` — a UI já trata como opcional e cai no breakdown da página
    de eventos. Escopo ausente (sessão anterior a esta versão) ⇒ só a org dona."""
    meta: Dict[str, Any] = {}
    try:
        raw_meta = await redis.hgetall(capture_session._meta_key(session_id))
        meta = {capture_session._s(k): capture_session._s(v) for k, v in (raw_meta or {}).items()}
    except Exception:  # pragma: no cover — não-fatal
        meta = {}

    ids: Set[int] = set()
    for part in (meta.get(_SCOPE_META_FIELD) or "").split(","):
        try:
            ids.add(int(part.strip()))
        except ValueError:
            continue
    if owner_org_id is not None:
        ids.add(int(owner_org_id))

    counts: Dict[str, int] = {}
    for key, value in meta.items():
        if not key.startswith(_OUTCOME_META_PREFIX):
            continue
        try:
            counts[key[len(_OUTCOME_META_PREFIX):]] = int(value)
        except (TypeError, ValueError):
            continue
    return sorted(ids), counts


async def _unindex_session_from_scope(
    redis: redis_async.Redis, session_id: str, scope_org_ids: List[int]
) -> None:
    """Remove o id da sessão dos índices do escopo (o engine só limpa o da dona)."""
    try:
        pipe = redis.pipeline()
        for org_id in scope_org_ids:
            pipe.srem(capture_session._org_index_key(org_id), session_id)
        await pipe.execute()
    except Exception as exc:  # pragma: no cover — não-fatal
        logger.warning("capture: limpeza de índice falhou para sessão %s: %s", session_id, exc)


# ── Response models (locais: estendem o contrato base com escopo/contadores) ──


class CaptureSessionScoped(schemas.CaptureSession):
    """Sessão + as orgs que ela realmente cobre (subárvore autorizada).

    ``outcome_counts`` é o total POR DESFECHO da sessão inteira quando o engine mantém
    esses contadores (ver ``_OUTCOME_META_PREFIX``); vazio caso contrário — nunca um
    palpite. ``event_count`` continua sendo o total geral."""

    scope_org_ids: List[int] = Field(default_factory=list)
    outcome_counts: Dict[str, int] = Field(default_factory=dict)


class CaptureSessionScopedList(BaseModel):
    count: int
    sessions: List[CaptureSessionScoped] = Field(default_factory=list)


class CaptureEventDetail(schemas.CaptureEvent):
    """Evento capturado + de qual org veio e QUAL FOI O DESFECHO.

    ``outcome`` vem do tap de ciclo de vida (``capture_session.OUTCOMES``);
    ``destination_id``/``detail`` só existem nos desfechos que os têm (entrega por
    destino, motivo do drop/quarentena) — é o par "como entrou / como saiu"."""

    organization_id: Optional[int] = None
    outcome: str = "unknown"
    destination_id: Optional[str] = None
    # Rota responsável pelo desfecho (estruturada). Presente nos desfechos que o
    # engine atribui por evento — dropped/sampled_out — respondendo "em qual rota
    # bateu" e "por que foi dropado" sem parsear texto livre.
    route_id: Optional[str] = None
    detail: Optional[str] = None
    # ── jornal (v2) ────────────────────────────────────────────────────
    # TODOS opcionais com default: registros v1 continuam no ring por até
    # 3.900 s após o deploy, e ``capture_session.normalize_entry`` preenche os
    # defaults na leitura. Sem migração e sem backfill.
    event_id: Optional[str] = None
    #: ``collected`` | ``routed`` | ``delivered`` — QUAL transformação o payload
    #: gravado já sofreu. Ortogonal a ``outcome``.
    stage: str = "routed"
    payload_kind: str = "envelope"
    #: ``False`` nos registros PRÉ-entrega. Não é detalhe: a redação de PII é
    #: por rota e alcança o bloco ``raw``, então um evento dropado mostra em
    #: claro o que o destino teria recebido redigido.
    pii_redacted: bool = False
    destination_kind: Optional[str] = None
    #: Versão da config do destino NO MOMENTO DA ENTREGA — permite a UI sinalizar
    #: drift quando o preview sob demanda usar a config ATUAL.
    dest_config_version: Optional[str] = None
    #: ``{fidelity, encoding, note, text?, bytes, truncated}``. Ausente quando a
    #: sessão não pediu wire.
    wire: Optional[Dict[str, Any]] = None
    #: Metadados do TAP (truncamento, blocos descartados). Fora de ``event``
    #: de propósito: o export mascara ``event``, e isto não é dado do vendor.
    capture_meta: Optional[Dict[str, Any]] = Field(default=None, alias="_capture")

    model_config = ConfigDict(populate_by_name=True)


class CaptureEventPage(BaseModel):
    """Página de eventos + contadores que deixam a UI honesta.

    ``total_captured`` é o contador da SESSÃO INTEIRA (inclui o que já saiu do ring
    por trim) — é ele que distingue "sessão ativa e nada aconteceu" (0) de "houve
    tráfego, mas fora da janela lida". ``outcome_counts`` é o breakdown por desfecho
    (entregue/drop/sem-destino/quarentena/…) **dos eventos desta página**."""

    count: int
    session_id: str
    session_status: str = "active"
    total_captured: int = 0
    scope_org_ids: List[int] = Field(default_factory=list)
    outcome_counts: Dict[str, int] = Field(default_factory=dict)
    events: List[CaptureEventDetail] = Field(default_factory=list)


# ── Catálogo de vendors capturáveis (derivado do registry, sem hardcode) ──────


class CaptureVendor(BaseModel):
    vendor: str
    display_name: str
    transport: str  # "pull" | "push"
    streams: List[str] = Field(default_factory=list)


class CaptureVendorList(BaseModel):
    count: int
    vendors: List[CaptureVendor] = Field(default_factory=list)


def _capture_vendor_catalog() -> List[CaptureVendor]:
    """Vendors que podem aparecer em ``_centralops.vendor`` — TODOS os transportes.

    União do registry de collectors (``all_registrations`` — pull **e** push, ex.:
    ``fortinet_fortigate``/``windows_event_log`` via ``/api/ingest``) com o catálogo de
    plataformas (``all_platforms`` — inclui plataformas sem collector próprio, como as
    variantes de partner). Zero hardcode: registrar um vendor novo o faz aparecer aqui.
    """
    try:  # import tardio (mesmo motivo do router de collectors: evita Celery/aiohttp)
        from ..collectors import registry
    except Exception as exc:  # pragma: no cover — defensivo
        logger.warning("capture: registry indisponível: %s", exc)
        return []

    streams_by_platform: Dict[str, Set[str]] = {}
    try:
        for reg in registry.all_registrations():
            streams_by_platform.setdefault(reg.platform, set()).add(reg.stream)
        catalog = {p.platform: p for p in registry.all_platforms()}
    except Exception as exc:  # pragma: no cover — defensivo
        logger.warning("capture: leitura do registry falhou: %s", exc)
        return []

    out: List[CaptureVendor] = []
    for platform in sorted(set(streams_by_platform) | set(catalog)):
        meta = catalog.get(platform)
        out.append(
            CaptureVendor(
                vendor=platform,
                display_name=getattr(meta, "display_name", None) or platform,
                transport=getattr(meta, "transport", None) or "pull",
                streams=sorted(streams_by_platform.get(platform, ())),
            )
        )
    return out


@router.get("/capture-vendors", response_model=CaptureVendorList)
def list_capture_vendors(
    _: models.AppUser = Depends(app_auth.require_admin_user),
) -> CaptureVendorList:
    """Catálogo de vendors para o filtro da captura — pull **e** push."""
    vendors = _capture_vendor_catalog()
    return CaptureVendorList(count=len(vendors), vendors=vendors)


def _to_capture_schema(
    meta: Dict[str, Any],
    scope_org_ids: Optional[List[int]] = None,
    outcome_counts: Optional[Dict[str, int]] = None,
) -> CaptureSessionScoped:
    org_id = meta.get("organization_id")
    return CaptureSessionScoped(
        id=meta["id"],
        organization_id=org_id,
        vendor=meta.get("vendor"),
        created_at=meta.get("created_at"),
        expires_at=meta.get("expires_at"),
        status=meta.get("status", "active"),
        event_count=meta.get("event_count", 0),
        scope_org_ids=(
            scope_org_ids
            if scope_org_ids is not None
            else ([int(org_id)] if org_id is not None else [])
        ),
        outcome_counts=outcome_counts or {},
    )


async def _owned_capture_or_404(
    redis: redis_async.Redis, session_id: str, effective_org: Optional[int]
) -> Dict[str, Any]:
    """Isolamento de tenant: a sessão só é acessível pelo próprio org (fail-closed)."""
    meta = await capture_session.get_session(redis, session_id)
    if (
        meta is None
        or effective_org is None
        or meta.get("organization_id") != effective_org
    ):
        raise ApiError(
            "collector_config.capture_session_not_found",
            404,
            messages={
                "pt": "Sessão de captura não encontrada.",
                "en": "Capture session not found.",
                "es": "Sesión de captura no encontrada.",
            },
        )
    return meta


@router.post("/capture-sessions", response_model=CaptureSessionScoped, status_code=201)
async def start_capture_session(
    body: schemas.CaptureSessionStartRequest,
    org_id: Optional[int] = None,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    db: Session = Depends(database.get_session),
) -> CaptureSessionScoped:
    """Inicia uma sessão de captura (o "botão listening"): por uma janela, grava tudo o
    que for despachado para o tenant — opcionalmente filtrado por vendor — p/
    troubleshooting. Escopo de tenant idêntico ao da auditoria.

    A sessão cobre a subárvore AUTORIZADA da org (ver ``_capture_scope_org_ids``): quem
    pode abrir captura nos filhos também VÊ o tráfego deles, num ring único. O escopo
    efetivo volta em ``scope_org_ids``."""
    effective_org = _capture_effective_org(org_id, user)
    if effective_org is None:
        raise ApiError(
            "collector_config.org_id_required",
            400,
            messages={
                "pt": "org_id é obrigatório para admin global",
                "en": "org_id is required for a global admin",
                "es": "org_id es obligatorio para un administrador global",
            },
        )
    scope_org_ids = _capture_scope_org_ids(user, db, effective_org)
    redis = await _redis_client()
    try:
        meta = await capture_session.start_session(
            redis,
            effective_org,
            vendor=body.vendor,
            duration_seconds=body.duration_seconds,
            ring_size=body.ring_size,
        )
        await _index_session_in_scope(
            redis, meta["id"], scope_org_ids, effective_org
        )
    except capture_session.CaptureLimitReached as exc:
        raise ApiError(
            "collector_config.capture_limit_reached",
            429,
            messages={
                "pt": "limite de {limit} sessões de captura simultâneas atingido",
                "en": "limit of {limit} concurrent capture sessions reached",
                "es": "límite de {limit} sesiones de captura simultáneas alcanzado",
            },
            params={"limit": capture_session.MAX_SESSIONS_PER_ORG},
        ) from exc
    finally:
        await redis.aclose()
    return _to_capture_schema(meta, scope_org_ids)


@router.get("/capture-sessions", response_model=CaptureSessionScopedList)
async def list_capture_sessions(
    org_id: Optional[int] = None,
    user: models.AppUser = Depends(app_auth.require_admin_user),
) -> CaptureSessionScopedList:
    """Lista as sessões DA ORG (as que ela iniciou).

    O índice da org também guarda sessões de um ANCESTRAL cujo escopo a alcança (é
    assim que o pai captura o tráfego do filho) — essas são filtradas aqui: só o dono
    lista/para/apaga a própria sessão, o filho não enxerga a captura do pai."""
    effective_org = _capture_effective_org(org_id, user)
    if effective_org is None:
        return CaptureSessionScopedList(count=0)
    redis = await _redis_client()
    try:
        sessions = await capture_session.list_sessions(redis, effective_org)
        owned = [m for m in sessions if m.get("organization_id") == effective_org]
        items = []
        for m in owned:
            scope_org_ids, outcome_counts = await _session_extras(
                redis, m["id"], m.get("organization_id")
            )
            items.append(_to_capture_schema(m, scope_org_ids, outcome_counts))
    finally:
        await redis.aclose()
    return CaptureSessionScopedList(count=len(items), sessions=items)


def _event_outcome(entry: Dict[str, Any]) -> str:
    """Desfecho do evento: ``outcome`` do envelope de captura, ou o carimbado em
    ``_centralops``. Ausente ⇒ ``"unknown"`` (honesto: entradas de taps antigos não
    sabem o desfecho — melhor do que assumir "entregue")."""
    meta = (entry.get("event") or {}).get("_centralops") or {}
    raw = entry.get("outcome") or (meta.get("outcome") if isinstance(meta, dict) else None)
    return str(raw) if raw else "unknown"


def _event_org_id(entry: Dict[str, Any]) -> Optional[int]:
    meta = (entry.get("event") or {}).get("_centralops") or {}
    if not isinstance(meta, dict):
        return None
    try:
        return int(meta.get("organization_id"))
    except (TypeError, ValueError):
        return None


def _validate_capture_filters(outcome: Optional[str], stage: Optional[str]) -> None:
    """422 para valor fora do vocabulário — NUNCA lista vazia em silêncio.

    O filtro é igualdade exata, então um valor inexistente devolveria 200 com
    zero eventos e o operador leria isso como "não houve tráfego". É a mesma
    classe de bug que o filtro de auditoria de mappings tinha: três das quatro
    opções ofereciam ações que o backend nunca grava.
    """
    if outcome and outcome not in capture_session.OUTCOMES:
        raise ApiError(
            "capture.unknown_outcome",
            422,
            messages={
                "pt": "desfecho desconhecido: {value}. Válidos: {allowed}",
                "en": "unknown outcome: {value}. Valid: {allowed}",
                "es": "desenlace desconocido: {value}. Válidos: {allowed}",
            },
            params={"value": outcome, "allowed": ", ".join(sorted(capture_session.OUTCOMES))},
        )
    if stage and stage not in capture_session.STAGES:
        raise ApiError(
            "capture.unknown_stage",
            422,
            messages={
                "pt": "estágio desconhecido: {value}. Válidos: {allowed}",
                "en": "unknown stage: {value}. Valid: {allowed}",
                "es": "etapa desconocida: {value}. Válidos: {allowed}",
            },
            params={"value": stage, "allowed": ", ".join(sorted(capture_session.STAGES))},
        )


def _matches_capture_filters(
    entry: Dict[str, Any], outcome: Optional[str], stage: Optional[str]
) -> bool:
    if outcome and (entry.get("outcome") or "unknown") != outcome:
        return False
    if stage and (entry.get("stage") or "routed") != stage:
        return False
    return True


@router.get(
    "/capture-sessions/{session_id}/events",
    response_model=CaptureEventPage,
)
async def get_capture_events(
    session_id: str,
    limit: int = Query(default=200, ge=1, le=20000),
    org_id: Optional[int] = None,
    outcome: Optional[str] = Query(default=None),
    stage: Optional[str] = Query(default=None),
    user: models.AppUser = Depends(app_auth.require_admin_user),
) -> CaptureEventPage:
    """Eventos da sessão + contadores.

    A UI precisa distinguir "sessão ativa e NADA aconteceu" de "houve tráfego":
    ``total_captured`` (contador da sessão inteira, sobrevive ao trim do ring) responde
    isso, e ``outcome_counts`` mostra o breakdown por desfecho da página lida — é o que
    revela tráfego que entrou mas NÃO foi entregue (drop/sem-destino/quarentena/…).
    O ring é único para toda a subárvore coberta (``scope_org_ids``); cada evento traz
    o ``organization_id`` de origem."""
    _validate_capture_filters(outcome, stage)
    effective_org = _capture_effective_org(org_id, user)
    redis = await _redis_client()
    try:
        meta = await _owned_capture_or_404(redis, session_id, effective_org)
        events = await capture_session.read_events(redis, session_id, limit=limit)
        scope_org_ids, _ = await _session_extras(
            redis, session_id, meta.get("organization_id")
        )
    finally:
        await redis.aclose()

    outcome_counts: Dict[str, int] = {}
    items: List[CaptureEventDetail] = []
    for e in events:
        # ``outcome_counts`` conta a PÁGINA INTEIRA, antes do filtro: é o
        # breakdown que diz ao operador o que existe para filtrar. Contá-lo
        # depois faria o painel mostrar só a faceta já selecionada.
        ev_outcome = _event_outcome(e)
        outcome_counts[ev_outcome] = outcome_counts.get(ev_outcome, 0) + 1
        if not _matches_capture_filters(e, outcome, stage):
            continue
        items.append(
            CaptureEventDetail(
                event=e.get("event") or {},
                vendor=e.get("vendor"),
                captured_at=e.get("captured_at"),
                organization_id=_event_org_id(e),
                outcome=ev_outcome,
                destination_id=e.get("destination_id"),
                route_id=e.get("route_id"),
                detail=e.get("detail"),
                # ``read_events`` já passou cada entrada por ``normalize_entry``,
                # então os defaults do v1 chegam aqui preenchidos.
                event_id=e.get("event_id"),
                stage=e.get("stage") or "routed",
                payload_kind=e.get("payload_kind") or "envelope",
                pii_redacted=bool(e.get("pii_redacted")),
                destination_kind=e.get("destination_kind"),
                dest_config_version=e.get("dest_config_version"),
                wire=e.get("wire"),
                capture_meta=e.get("_capture"),
            )
        )
    return CaptureEventPage(
        count=len(items),
        session_id=session_id,
        session_status=meta.get("status", "active"),
        total_captured=int(meta.get("event_count") or 0),
        scope_org_ids=scope_org_ids,
        outcome_counts=outcome_counts,
        events=items,
    )


class CaptureTrajectory(BaseModel):
    """Todos os registros de UM evento, ordenados no tempo — a trajetória.

    ``complete`` é o campo que permite a UI dizer "o bruto saiu da janela do
    ring" em vez de mostrar um buraco mudo: o registro ``collected`` é o mais
    VELHO do grupo e, portanto, a primeira vítima da poda.
    """

    event_id: str
    session_id: str
    count: int
    complete: bool
    stages_present: List[str] = Field(default_factory=list)
    events: List[CaptureEventDetail] = Field(default_factory=list)


@router.get(
    "/capture-sessions/{session_id}/events/{event_id}",
    response_model=CaptureTrajectory,
)
async def get_capture_trajectory(
    session_id: str,
    event_id: str,
    org_id: Optional[int] = None,
    user: models.AppUser = Depends(app_auth.require_admin_user),
) -> CaptureTrajectory:
    """Junta a trajetória de um evento a partir do ring.

    O ring é UMA lista FIFO por sessão, compartilhada por todos os estágios e
    destinos: com fan-out N, um evento gera 1+N entradas INTERCALADAS com as de
    todos os outros. Ler por página (o caminho normal da UI) faz a junção
    degradar exatamente no volume em que ela é necessária — daí este endpoint,
    que varre o ring inteiro filtrando por ``event_id``.

    Varredura O(ring) por clique do operador, no processo da API. Aceitável
    porque é sob demanda; se medir mal em produção, o follow-up é um hash
    auxiliar por ``event_id`` — otimizar antes seria adicionar uma chave nova
    (e mais superfície de expiração) sem evidência.
    """
    effective_org = _capture_effective_org(org_id, user)
    redis = await _redis_client()
    try:
        await _owned_capture_or_404(redis, session_id, effective_org)
        found: List[Dict[str, Any]] = []
        async for entry in capture_session.iter_events(redis, session_id):
            if (entry.get("event_id") or "") == event_id:
                found.append(entry)
    finally:
        await redis.aclose()

    # Mais ANTIGO primeiro: a trajetória se lê no sentido do pipeline
    # (collected → routed → delivered), não no do ring (que é LIFO).
    found.sort(key=lambda e: float(e.get("captured_at") or 0))
    stages = [s for s in ("collected", "routed", "delivered")
              if any((e.get("stage") or "routed") == s for e in found)]
    return CaptureTrajectory(
        event_id=event_id,
        session_id=session_id,
        count=len(found),
        # Sem um registro ``collected`` não há "como era antes" — e a UI precisa
        # DIZER isso, em vez de renderizar um painel vazio.
        complete="collected" in stages,
        stages_present=stages,
        events=[
            CaptureEventDetail(
                event=e.get("event") or {},
                vendor=e.get("vendor"),
                captured_at=e.get("captured_at"),
                organization_id=_event_org_id(e),
                outcome=_event_outcome(e),
                destination_id=e.get("destination_id"),
                route_id=e.get("route_id"),
                detail=e.get("detail"),
                event_id=e.get("event_id"),
                stage=e.get("stage") or "routed",
                payload_kind=e.get("payload_kind") or "envelope",
                pii_redacted=bool(e.get("pii_redacted")),
                destination_kind=e.get("destination_kind"),
                dest_config_version=e.get("dest_config_version"),
                wire=e.get("wire"),
                capture_meta=e.get("_capture"),
            )
            for e in found
        ],
    )


# Alinhado ao teto do ring. Com 50.000 o branch de truncamento era INALCANÇÁVEL
# pelo caminho HTTP — ``iter_events`` já clampa em ``MAX_RING_SIZE``, então o
# header dizia "não truncado" por coincidência, não por contrato.
_EXPORT_MAX_ROWS = capture_session.MAX_RING_SIZE


@router.get("/capture-sessions/{session_id}/export")
async def export_capture_events(
    session_id: str,
    request: Request,
    fmt: str = Query(default="csv", pattern="^(csv|ndjson)$"),
    org_id: Optional[int] = None,
    mask: bool = Query(default=True),
    user: models.AppUser = Depends(app_auth.require_admin_user),
) -> StreamingResponse:
    """Exporta os eventos capturados de UMA sessão como planilha (CSV) ou NDJSON,
    para o analista de SOC abrir no Excel / anexar num ticket.

    STREAMING (páginas de ``EXPORT_PAGE_SIZE`` via LRANGE) — não materializa o ring
    na RAM. Escopo SEMPRE a uma sessão (teto natural = ring ≤ 20k) e à org do
    usuário (mesmo gate ``require_admin_user`` dos demais endpoints de captura; a
    chamada é auditada pelo middleware ``audit_api_requests`` — o path traz o
    session_id). ``mask`` (default True) redige PII no serializador, porque o dado
    está SAINDO do sistema; os SEGREDOS já foram scrubbados na gravação do ring."""
    from ..collectors import capture_export

    if not mask:
        # Ver na tela (inspetor in-app, escopado à própria org) e EXTRAIR um
        # arquivo sem máscara são coisas diferentes: o arquivo sai do sistema e
        # sobrevive a ele. Reusa o escopo global em vez de criar uma Permission
        # nova — uma Permission dedicada é mais precisa e fica como follow-up.
        tenant.require_global_scope(user)
    effective_org = _capture_effective_org(org_id, user)
    redis = await _redis_client()
    # Valida posse ANTES de abrir o stream (404 vira corpo de erro limpo, não um
    # CSV meia-boca). O client de leitura das páginas é o mesmo, reusado no gerador.
    try:
        await _owned_capture_or_404(redis, session_id, effective_org)
    except Exception:
        await redis.aclose()
        raise

    separator = capture_export.csv_separator_for_locale(
        request.headers.get("accept-language")
    )

    async def _stream():
        # Serializa item a item conforme as páginas chegam do Redis — pico de
        # memória = uma página do ring + uma linha, nunca o dataset inteiro.
        try:
            written = 0
            if fmt == "csv":
                yield capture_export.csv_header(separator).encode("utf-8")
            async for entry in capture_session.iter_events(
                redis, session_id, max_events=_EXPORT_MAX_ROWS
            ):
                if written >= _EXPORT_MAX_ROWS:
                    notice = (
                        capture_export.csv_truncation_notice(_EXPORT_MAX_ROWS)
                        if fmt == "csv"
                        else capture_export.ndjson_truncation_notice(_EXPORT_MAX_ROWS)
                    )
                    yield notice.encode("utf-8")
                    break
                line = (
                    capture_export.csv_row(entry, mask=mask, separator=separator)
                    if fmt == "csv"
                    else capture_export.ndjson_line(entry, mask=mask)
                )
                yield line.encode("utf-8")
                written += 1
        finally:
            await redis.aclose()

    if fmt == "csv":
        media = "text/csv; charset=utf-8"
        filename = f"capture-{session_id}.csv"
    else:
        media = "application/x-ndjson"
        filename = f"capture-{session_id}.ndjson"

    return StreamingResponse(
        _stream(),
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-CentralOps-Export-Max-Rows": str(_EXPORT_MAX_ROWS),
            # O arquivo sai mascarado por default. Sem este header, quem consome
            # o export por script não tem como saber se está lendo dado real ou
            # ``[PII]`` — e mascarado é indistinguível de "o vendor mandou isso".
            "X-CentralOps-Export-Masked": "true" if mask else "false",
        },
    )


@router.post("/capture-sessions/{session_id}/stop", status_code=204)
async def stop_capture_session(
    session_id: str,
    org_id: Optional[int] = None,
    user: models.AppUser = Depends(app_auth.require_admin_user),
) -> None:
    effective_org = _capture_effective_org(org_id, user)
    redis = await _redis_client()
    try:
        await _owned_capture_or_404(redis, session_id, effective_org)
        await capture_session.stop_session(redis, session_id, int(effective_org))
    finally:
        await redis.aclose()


@router.delete("/capture-sessions/{session_id}", status_code=204)
async def delete_capture_session(
    session_id: str,
    org_id: Optional[int] = None,
    user: models.AppUser = Depends(app_auth.require_admin_user),
) -> None:
    effective_org = _capture_effective_org(org_id, user)
    redis = await _redis_client()
    try:
        meta = await _owned_capture_or_404(redis, session_id, effective_org)
        owner_org = int(meta["organization_id"])
        # Lê o escopo ANTES do delete (o meta some junto com o campo).
        scope_org_ids, _ = await _session_extras(redis, session_id, owner_org)
        await capture_session.delete_session(redis, session_id, owner_org)
        # O engine só limpa o índice da org dona; as demais do escopo saem aqui
        # (sem isso ficariam ids órfãos até o ``record()`` podá-los).
        await _unindex_session_from_scope(redis, session_id, scope_org_ids)
    finally:
        await redis.aclose()
