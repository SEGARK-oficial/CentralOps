"""Runtime do enriquecimento — tudo que toca o mundo (ADR-LOCAL-0002).

Contraparte de ``applier``: aqui mora carga de tabela, resolução em bulk, cache,
orçamento, guards de runtime e métricas. Nada disto roda por evento.

**Carga integral por ciclo.** As tabelas locais são carregadas INTEIRAS no início do
ciclo, fora do laço, no mesmo padrão que o pipeline já usa três vezes
(``_load_routes_for_org`` em ``pipeline.py:889``, ``resolve_enforcement_mode`` em
``:896``, ``load_inflight_rules_for_org`` em ``:956``). Isso é o que elimina o
caminho de *miss* do aplicador: a tabela está em memória, então "não achei" é um
``dict.get``, não uma ida ao Redis.

Só é viável por uma invariante: **o ciclo de coleta é mono-tenant**
(``pipeline.py:885``). Uma tabela por org × N orgs residentes seria O(orgs) por fork
(50k ativos ≈ 10 MB/org × 200 orgs × 8 forks ≈ 16 GB/container, contra `memory: 2g`);
uma tabela por CICLO é O(1). A invariante é verificada em :func:`assert_mono_tenant`,
não assumida — hoje ela é só um comentário no pipeline, e comentário não é contrato.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Mapping, MutableMapping, Optional, Sequence, Tuple

from ..normalize.dotpath import compile_source  # noqa: F401  (reexport p/ testes)
from .applier import ApplyStats, apply, evaluate_when, extract_key
from .cache import CacheState, EnrichCache, L1Cache, build_redis_client, cache_key
from .contract import EnrichContext, EnricherRegistration, LookupTable
from .dsl import CompiledEnrichRule, CompiledPolicy, compile_policy
from . import registry

logger = logging.getLogger(__name__)

__all__ = [
    "BulkResolution",
    "ChainResolution",
    "DictLookupTable",
    "EnrichRuntime",
    "TableResolution",
    "assert_mono_tenant",
    "estimate_bytes",
    "load_policy_for_org",
    "note_unenriched",
    "SKIP_REASONS",
]


class MonoTenantViolation(RuntimeError):
    """Um ciclo tocou mais de uma organização.

    Não é defensivo por precaução: TODA a conta de memória do enriquecimento local
    depende de a tabela residente ser a de UMA org. Se esta exceção aparecer, o
    modelo caiu e o enriquecimento local precisa ser desligado até o redesenho —
    falhar aqui é preferível a servir a tabela da Org A para a Org B.
    """


def assert_mono_tenant(seen_org_id: Optional[int], ctx_org_id: int) -> None:
    """Mecaniza a invariante de ``pipeline.py:885``."""
    if seen_org_id is not None and seen_org_id != ctx_org_id:
        raise MonoTenantViolation(
            f"ciclo tocou org {ctx_org_id} depois de org {seen_org_id}. "
            "O enriquecimento local pressupõe ciclo mono-tenant (pipeline.py:885); "
            "sem isso a tabela residente e a chave de cache vazam entre tenants."
        )


def estimate_bytes(entries: Mapping[str, Any]) -> int:
    """Estimativa barata de bytes residentes de uma tabela.

    Deliberadamente APROXIMADA e barata: medir de verdade exigiria varrer o grafo de
    objetos, e esta função roda 1×/carga sobre tabelas de dezenas de milhares de
    linhas. Serve ao teto de memória e ao gauge, não à contabilidade.
    """
    n = len(entries)
    if n == 0:
        return 0
    # Amostra as 32 primeiras entradas e extrapola — evita O(n) em tabela grande.
    sample = 0
    counted = 0
    for k, v in entries.items():
        sample += len(k) + (len(repr(v)) if not isinstance(v, str) else len(v))
        counted += 1
        if counted >= 32:
            break
    per_entry = (sample / counted) if counted else 0
    # Overhead de dict/str do CPython: ~100 B/entrada é conservador o bastante
    # para o teto não ser otimista (que é o erro caro aqui).
    return int(n * (per_entry + 100))


class DictLookupTable:
    """:class:`~.contract.LookupTable` sobre um dict simples. Pura por construção."""

    __slots__ = ("_rows", "_bytes")

    def __init__(self, rows: Mapping[str, Any]) -> None:
        self._rows = dict(rows)
        self._bytes = estimate_bytes(self._rows)

    def lookup(self, key: str) -> Optional[Mapping[str, Any]]:
        return self._rows.get(key)

    @property
    def entry_count(self) -> int:
        return len(self._rows)

    @property
    def approx_bytes(self) -> int:
        return self._bytes


class TableResolution:
    """:class:`~.contract.Resolution` do seam LOCAL — lê tabelas residentes."""

    __slots__ = ("_tables",)

    def __init__(self, tables: Mapping[str, LookupTable]) -> None:
        #: rule_id → tabela. Só as regras locais estão aqui.
        self._tables = tables

    def get(self, rule_id: str, key: str) -> Tuple[bool, Optional[Mapping[str, Any]]]:
        table = self._tables.get(rule_id)
        if table is None:
            return False, None  # regra remota vista pelo seam local — não é miss
        return True, table.lookup(key)


class BulkResolution:
    """:class:`~.contract.Resolution` do seam REMOTO — lê um mapa já resolvido."""

    __slots__ = ("_rows",)

    def __init__(self, rows: Mapping[Tuple[str, str], Optional[Mapping[str, Any]]]) -> None:
        #: (rule_id, key) → valor. A ausência da CHAVE significa "não resolvida
        #: neste lote" (orçamento estourado, erro) e é distinta de valor None,
        #: que é miss confirmado.
        self._rows = rows

    def get(self, rule_id: str, key: str) -> Tuple[bool, Optional[Mapping[str, Any]]]:
        probe = (rule_id, key)
        if probe not in self._rows:
            return False, None
        return True, self._rows[probe]


class ChainResolution:
    """Tenta várias resoluções em ordem; a primeira que responde vence."""

    __slots__ = ("_chain",)

    def __init__(self, *resolutions: Any) -> None:
        self._chain = resolutions

    def get(self, rule_id: str, key: str) -> Tuple[bool, Optional[Mapping[str, Any]]]:
        for res in self._chain:
            answered, value = res.get(rule_id, key)
            if answered:
                return True, value
        return False, None


@dataclass(slots=True)
class _CachedTable:
    table: LookupTable
    version: str


class EnrichRuntime:
    """Estado por PROCESSO (fork). Um por worker.

    O LRU é limitado por **BYTES**, não por número de entradas: o Celery é prefork e
    qualquer fork pega qualquer integração, então um fork vê organizações sortidas ao
    longo do tempo. Limitar por contagem deixaria 6 tabelas de 10 MB e 6 tabelas de
    200 MB ocuparem "o mesmo" espaço — que é como se descobre um teto pelo
    cgroup-OOM, e o HPA escala só por CPU (a memória não gera sinal nenhum).
    """

    def __init__(self, *, max_table_bytes: int, lru_bytes: int) -> None:
        self._max_table_bytes = int(max_table_bytes)
        self._lru_bytes = int(lru_bytes)
        self._cache: "OrderedDict[Tuple[str, int, Optional[str]], _CachedTable]" = OrderedDict()
        self._cache_bytes = 0
        self._disabled_this_cycle: set = set()
        self._seen_org_id: Optional[int] = None
        #: nome da fonte → (config, secret_ref), resolvido 1×/ciclo. Zerado em
        #: ``begin_cycle``/``end_cycle`` — credencial nunca atravessa ciclo.
        self._sources_this_cycle: Dict[str, Tuple[Mapping[str, Any], Optional[str]]] = {}
        #: Cache L1/L2 de VALORES resolvidos (remoto). Distinto de ``_cache``, que
        #: é o LRU de TABELAS residentes — reusar o mesmo nome apagaria o LRU.
        self._kv_cache: Optional[EnrichCache] = None

    # ── ciclo ────────────────────────────────────────────────────────────────
    def begin_cycle(self, organization_id: int) -> None:
        """Marca o início de um ciclo e valida a invariante mono-tenant."""
        assert_mono_tenant(self._seen_org_id, organization_id)
        self._seen_org_id = organization_id
        self._disabled_this_cycle = set()
        # Fontes configuradas são por ORG e por CICLO. Zerar aqui (e não só em
        # ``end_cycle``) garante que um ciclo novo nunca enxergue a credencial
        # resolvida do ciclo anterior, mesmo que ``end_cycle`` não tenha rodado
        # por causa de uma exceção no meio da coleta.
        self._sources_this_cycle = {}

    def end_cycle(self) -> None:
        self._seen_org_id = None
        self._disabled_this_cycle = set()
        self._sources_this_cycle = {}

    # ── fontes configuradas (instâncias de enricher escopadas à org) ─────────
    def _resolve_source(
        self, organization_id: int, source_name: str
    ) -> Tuple[Mapping[str, Any], Optional[str]]:
        """``(config, secret_ref)`` da ``EnrichmentSource`` DESTA org.

        **O filtro por ``organization_id`` é a propriedade de segurança**, não uma
        conveniência de query. ``secret_ref`` é o próprio ciphertext e o cofre
        (``core.secrets``) não conhece organização: resolver a fonte por nome
        global entregaria a credencial do vizinho a quem escrevesse o nome certo.
        Aqui o nome só é procurado dentro da org do ciclo — e o ciclo é
        mono-tenant por invariante (``assert_mono_tenant``).

        Cacheado por ciclo: é 1 SELECT por fonte por ciclo, nunca por lote.
        """
        cached = self._sources_this_cycle.get(source_name)
        if cached is not None:
            return cached

        import json as _json

        from ...db import models
        from ...db.database import SessionLocal

        config: Mapping[str, Any] = {}
        secret_ref: Optional[str] = None
        with SessionLocal() as db:
            # Join pela lista de orgs, NÃO por ``EnrichmentSource.organization_id``:
            # numa MSP a fonte pertence à matriz e atende as filhas escolhidas. A
            # dona também está na lista, então este join cobre os dois casos com
            # uma query só. O filtro por org continua sendo a propriedade de
            # segurança: sem ele, o nome da fonte viraria um espaço global e um
            # tenant usaria a credencial do vizinho.
            row = (
                db.query(models.EnrichmentSource)
                .join(
                    models.EnrichmentSourceOrg,
                    models.EnrichmentSourceOrg.source_id == models.EnrichmentSource.id,
                )
                .filter(
                    models.EnrichmentSourceOrg.organization_id == organization_id,
                    models.EnrichmentSource.name == source_name,
                    models.EnrichmentSource.enabled.is_(True),
                )
                .first()
            )
            if row is None:
                # Fail-closed e ALTO: a regra cita uma fonte que não existe nesta
                # org (apagada, renomeada, ou de outro tenant). Devolver config
                # vazia faria o enricher levantar um erro genérico bem longe daqui.
                raise LookupError(
                    f"fonte de enriquecimento {source_name!r} não existe (ou está "
                    f"desabilitada) na organização {organization_id}"
                )
            try:
                config = _json.loads(row.config or "{}")
            except Exception as exc:  # noqa: BLE001
                raise LookupError(
                    f"config da fonte {source_name!r} não é JSON válido"
                ) from exc
            secret_ref = row.secret_ref

        resolved = (config, secret_ref)
        self._sources_this_cycle[source_name] = resolved
        return resolved

    def _ctx_for_rule(
        self, ctx: EnrichContext, rule: CompiledEnrichRule
    ) -> EnrichContext:
        """Contexto da regra: tabela, config e credencial da fonte citada.

        A config e a credencial vêm da LINHA escopada à org, nunca do JSON da
        regra — ver ``models.EnrichmentSource``.
        """
        if not rule.source:
            return replace(ctx, table=rule.table)
        config, secret_ref = self._resolve_source(ctx.organization_id, rule.source)
        return replace(ctx, table=rule.table, config=config, secret_ref=secret_ref)

    # ── carga de tabela ──────────────────────────────────────────────────────
    async def load_tables(
        self, policy: CompiledPolicy, ctx: EnrichContext
    ) -> Dict[str, LookupTable]:
        """Carrega as tabelas das regras LOCAIS. 1×/ciclo, fora do laço.

        Falha de carga NUNCA propaga: a regra correspondente simplesmente não entra
        no mapa, e o aplicador a trata como "não respondida" (skipped), não como
        miss. Um feed de threat intel fora do ar não pode parar a coleta.
        """
        tables: Dict[str, LookupTable] = {}
        for rule in policy.local_rules():
            reg = registry.get(rule.enricher)
            if reg is None:
                logger.warning(
                    "enricher %r da regra %r não está registrado — regra ignorada",
                    rule.enricher, rule.rule_id,
                    extra={"event": "enrich.unknown_enricher", "enricher": rule.enricher},
                )
                continue
            try:
                table = await self._load_one(reg, rule, ctx)
            except Exception as exc:  # noqa: BLE001 — carga é best-effort
                _count_error(rule.enricher, _error_reason(exc))
                logger.warning(
                    "falha ao carregar tabela do enricher %r (regra %r): %s",
                    rule.enricher, rule.rule_id, exc,
                    extra={"event": "enrich.load_failed", "enricher": rule.enricher},
                    exc_info=True,
                )
                continue
            if table is not None:
                tables[rule.rule_id] = table
        return tables

    async def _load_one(
        self, reg: EnricherRegistration, rule: CompiledEnrichRule, ctx: EnrichContext
    ) -> Optional[LookupTable]:
        cache_key = (rule.enricher, ctx.organization_id, rule.table)
        cached = self._cache.get(cache_key)
        if cached is not None:
            self._cache.move_to_end(cache_key)
            return cached.table

        # A tabela vem da REGRA; a config e a credencial, da FONTE configurada
        # desta org. A mesma instância de ``table_cidr`` serve N tabelas numa
        # mesma política, e o mesmo ``opencti`` serve N instâncias distintas.
        rule_ctx = self._ctx_for_rule(ctx, rule)
        instance = reg.factory(dict(rule_ctx.config or {}))
        started = time.monotonic()
        table = await instance.load(rule_ctx)
        elapsed_ms = (time.monotonic() - started) * 1000.0
        _observe_resolve(rule.enricher, elapsed_ms / 1000.0)

        # Guard de RUNTIME contra ``mode="local"`` auto-declarado. O guard de CI
        # cobre os imports de ``applier.py``, não o corpo do ``load`` de um plugin
        # — que na edição Enterprise virá de fora deste repo. Aqui a mentira custa
        # o enricher, não a vazão do ciclo.
        budget_ms = max(reg.caps.p99_budget_ms * 10.0, 1000.0)
        if elapsed_ms > budget_ms:
            logger.warning(
                "enricher %r estourou 10x o orçamento na carga (%.0f ms > %.0f ms) — "
                "desabilitado neste ciclo",
                rule.enricher, elapsed_ms, budget_ms,
                extra={"event": "enrich.budget_exceeded", "enricher": rule.enricher},
            )
            _count_error(rule.enricher, "budget")
            self._disabled_this_cycle.add(rule.enricher)
            return None

        if table.approx_bytes > self._max_table_bytes:
            # Fail-LOUD e não silencioso: descobrir o teto por OOM é o modo de
            # falha que este número existe para evitar.
            logger.error(
                "tabela do enricher %r tem ~%d B (%d entradas), acima do teto de %d B "
                "por fork — NÃO carregada. Reduza a tabela ou aumente "
                "ENRICH_MAX_TABLE_BYTES ciente de que o custo é ×concurrency.",
                rule.enricher, table.approx_bytes, table.entry_count, self._max_table_bytes,
                extra={"event": "enrich.table_too_big", "enricher": rule.enricher},
            )
            _count_error(rule.enricher, "table_too_big")
            return None

        _set_table_gauges(rule.enricher, ctx.organization_id, table)
        self._cache[cache_key] = _CachedTable(table=table, version=str(rule.table or ""))
        self._cache_bytes += table.approx_bytes
        self._evict_until_fits()
        return table

    def _evict_until_fits(self) -> None:
        while self._cache_bytes > self._lru_bytes and self._cache:
            _, evicted = self._cache.popitem(last=False)
            self._cache_bytes -= evicted.table.approx_bytes

    # ── resolução remota ─────────────────────────────────────────────────────
    async def resolve_remote(
        self,
        policy: CompiledPolicy,
        batch: Sequence[Mapping[str, Any]],
        ctx: EnrichContext,
        *,
        budget_s: float,
    ) -> BulkResolution:
        """Resolve, em BULK, as chaves distintas do lote para as regras remotas.

        **Gate binário.** Se o orçamento não couber, o enriquecimento remoto não roda
        para NENHUM evento do lote — nunca para um prefixo arbitrário. Enriquecer os
        primeiros N e não o resto produz roteamento não-determinístico e é
        irreversível: o dedupe (``pipeline.py:1026``, TTL de 1 dia) grava a chave
        sobre o RAW antes de qualquer enriquecimento, então o evento não volta.
        """
        rows: Dict[Tuple[str, str], Optional[Mapping[str, Any]]] = {}
        remote_rules = policy.remote_rules()
        if not remote_rules or not batch:
            return BulkResolution(rows)

        # FAIL-CLOSED: sem cache L2 dedicado, o enriquecimento REMOTO não roda.
        # Sem cache compartilhado, cada worker repaga cada chave — a 4 req/min do
        # free tier do VirusTotal isso queima a cota do dia em minutos, e o
        # operador descobre pelo 429, não por aqui.
        if self._cache_for(ctx) is None:
            logger.info(
                "enrich: ENRICH_REDIS_URL não configurada — enriquecimento REMOTO "
                "desligado (o local segue funcionando). Configure uma instância "
                "Redis DEDICADA; reusar a principal coloca chaves com TTL junto do "
                "dedupe sob volatile-lru.",
                extra={"event": "enrich.remote_disabled_no_l2"},
            )
            return BulkResolution(rows)

        deadline = time.monotonic() + budget_s
        for rule in remote_rules:
            if rule.enricher in self._disabled_this_cycle:
                continue
            reg = registry.get(rule.enricher)
            if reg is None:
                continue

            keys = _distinct_keys(batch, rule)
            if not keys:
                continue

            # ── Cache L1/L2 ────────────────────────────────────────────────
            # A chave do cache carrega organization_id (anti cross-tenant) e a
            # versão da política, para que editar a política invalide o cache sem
            # precisar de purge manual.
            ttl = rule.ttl_s if rule.ttl_s is not None else reg.caps.suggested_ttl_s
            nttl = (
                rule.negative_ttl_s
                if rule.negative_ttl_s is not None
                else reg.caps.suggested_negative_ttl_s
            )
            cache = self._cache_for(ctx)
            ck = {
                k: cache_key(ctx.organization_id, rule.enricher, str(policy.version), k)
                for k in keys
            } if cache is not None else {}

            to_resolve = list(keys)
            if cache is not None:
                cached_values, cached_states, missing_ck = await cache.get_many(
                    [ck[k] for k in keys]
                )
                inv = {v: k for k, v in ck.items()}
                for cache_k, state in cached_states.items():
                    key = inv[cache_k]
                    if state is CacheState.HIT:
                        rows[(rule.rule_id, key)] = cached_values[cache_k]
                    else:  # MISS confirmado — não repergunta, não queima cota
                        rows[(rule.rule_id, key)] = None
                _count_cache(rule.enricher, "l1_l2", "hit", len(keys) - len(missing_ck))
                _count_cache(rule.enricher, "l1_l2", "miss", len(missing_ck))
                to_resolve = [inv[c] for c in missing_ck]

            if not to_resolve:
                continue

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _count_budget_exhausted(ctx.organization_id)
                logger.info(
                    "orçamento de enriquecimento remoto esgotado antes da regra %r — "
                    "o LOTE inteiro segue sem enriquecimento remoto (gate binário)",
                    rule.rule_id,
                    extra={"event": "enrich.budget_exhausted", "rule_id": rule.rule_id},
                )
                break

            # Single-flight POR CHAVE: quem não pega o lock espera um pouco pelo
            # resultado do par em vez de repagar a chamada. Travar por lote não
            # preveniria stampede nenhum (basta um evento diferir e o hash muda).
            owned = list(to_resolve)
            if cache is not None and len(to_resolve) > 0:
                owned = []
                for key in to_resolve:
                    if await cache.acquire_single_flight(ck[key]):
                        owned.append(key)
                        continue
                    state, value = await cache.wait_for_peer(ck[key])
                    if state is CacheState.HIT:
                        rows[(rule.rule_id, key)] = value
                    elif state is CacheState.MISS:
                        rows[(rule.rule_id, key)] = None
                    else:
                        # O par não terminou a tempo. Resolve mesmo assim: perder o
                        # enriquecimento é pior que uma chamada duplicada ocasional.
                        owned.append(key)
            if not owned:
                continue

            try:
                rule_ctx = self._ctx_for_rule(ctx, rule)
            except LookupError as exc:
                # Fonte inexistente/desabilitada nesta org. Desabilita o enricher
                # no ciclo em vez de tentar a cada lote — e o lote SEGUE, sem
                # enriquecimento, porque enriquecimento é observador.
                _count_error(rule.enricher, "config")
                self._disabled_this_cycle.add(rule.enricher)
                logger.warning(
                    "enrich: %s — regra %r desabilitada neste ciclo",
                    exc, rule.rule_id,
                    extra={"event": "enrich.source_unresolved"},
                )
                continue
            instance = reg.factory(dict(rule_ctx.config or {}))
            started = time.monotonic()
            try:
                resolved = await asyncio.wait_for(
                    instance.resolve(owned, rule_ctx), timeout=remaining
                )
            except asyncio.TimeoutError:
                _count_error(rule.enricher, "timeout")
                self._disabled_this_cycle.add(rule.enricher)
                logger.warning(
                    "enricher remoto %r estourou o orçamento (%.1fs) — desabilitado "
                    "neste ciclo; o lote segue sem ele",
                    rule.enricher, remaining,
                    extra={"event": "enrich.remote_timeout", "enricher": rule.enricher},
                )
                continue
            except Exception as exc:  # noqa: BLE001 — provedor fora do ar é normal
                _count_error(rule.enricher, _error_reason(exc))
                logger.warning(
                    "enricher remoto %r falhou: %s", rule.enricher, exc,
                    extra={"event": "enrich.remote_failed", "enricher": rule.enricher},
                    exc_info=True,
                )
                continue
            finally:
                _observe_resolve(rule.enricher, time.monotonic() - started)
                if cache is not None:
                    for key in owned:
                        await cache.release_single_flight(ck[key])

            for key in owned:
                # `in`, não `.get()`: chave que o provedor OMITIU do retorno é
                # UNKNOWN (cota 429, erro por chave, corte por max_keys_per_batch),
                # e gravá-la como None aqui a transformaria em miss CONFIRMADO —
                # o applier aplicaria `on_miss` e fabricaria veredito limpo com
                # proveniência falsa. Ausente do mapa ⇒ BulkResolution.get devolve
                # (False, None) ⇒ o applier pula sem contar miss. Mesmo predicado
                # da escrita do cache logo abaixo, pelo mesmo motivo.
                if key in resolved:
                    rows[(rule.rule_id, key)] = resolved[key]

            if cache is not None:
                # Só o que o provedor DE FATO respondeu. Chave ausente do retorno é
                # UNKNOWN (cota/timeout) e não pode virar MISS cacheado — seria
                # transformar indisponibilidade em "indicador limpo" por horas.
                await cache.put_many(
                    {ck[k]: resolved.get(k) for k in owned if k in resolved},
                    ttl_s=ttl,
                    negative_ttl_s=nttl,
                )

        return BulkResolution(rows)

    def _cache_for(self, ctx: EnrichContext):
        """Cache do ciclo, ou ``None`` quando o L2 não está configurado.

        **Fail-closed é o default.** Sem ``ENRICH_REDIS_URL`` explícita não há cache
        e — por decisão de ``resolve_remote`` — não há enriquecimento remoto. Cair no
        ``REDIS_URL`` principal seria colocar chaves com TTL na instância que roda
        ``volatile-lru`` junto do dedupe, cuja evicção é silenciosa e volta como
        reentrega no SIEM.
        """
        if self._kv_cache is not None:
            return self._kv_cache
        from ...core.config import settings as _s

        url = getattr(_s, "ENRICH_REDIS_URL", "") or ""
        if not url:
            return None
        client = build_redis_client(url)
        if client is None:
            return None
        self._kv_cache = EnrichCache(
            client,
            l1=L1Cache(int(getattr(_s, "ENRICH_L1_MAX_ENTRIES", 10_000))),
            singleflight_wait_ms=int(getattr(_s, "ENRICH_SINGLEFLIGHT_WAIT_MS", 50)),
        )
        return self._kv_cache


#: Razões pelas quais um lote pode sair SEM enriquecimento. Vocabulário FECHADO
#: pelo mesmo motivo de ``SAVING_REASONS``: o valor vai para o envelope entregue e
#: string livre ali vira cardinalidade descontrolada no destino.
SKIP_REASONS: Tuple[str, ...] = ("producer_unsupported", "no_policy", "service_role")


def note_unenriched(batch: Sequence[MutableMapping[str, Any]], reason: str) -> int:
    """Marca um lote que saiu SEM passar pelo enriquecimento. Devolve quantos marcou.

    **Por que isto existe.** O enriquecimento roda no laço de coleta
    (``_run_collection_once``), mas SETE produtores publicam lotes por
    ``_enqueue_dispatch``: backfill, replay de quarentena, reprocessamento,
    scheduler. Esses caminhos são SÍNCRONOS e, no caso do replay, rodam no processo
    da API — que nem carrega tabela. Fazê-los enriquecer exigiria disparar carga
    assíncrona de dentro de função síncrona chamada tanto de dentro quanto de fora
    de um event loop, que é a receita de um incidente.

    A decisão é não enriquecer nesses caminhos — mas **nunca em silêncio**. Um evento
    que chega ao destino sem contexto, indistinguível de um que foi enriquecido e não
    casou, é o modo de falha que faz o analista desconfiar da feature inteira. O
    campo ``_centralops.enrichment_skipped`` responde "por que este evento não tem
    contexto" no lugar onde a pergunta é feita: no próprio evento, no destino.

    No-op imediato com a flag desligada — o custo é uma comparação por lote.
    """
    from ...core.config import settings as _s

    if not getattr(_s, "ENRICHMENT_ENABLED", False) or not batch:
        return 0
    if reason not in SKIP_REASONS:
        reason = "producer_unsupported"
    marked = 0
    for env in batch:
        try:
            cc = env.get("_centralops")
            if not isinstance(cc, dict):
                continue
            # Já enriquecido (veio do laço de coleta) ⇒ não marcar. É o que impede
            # o lote re-despachado de ser rotulado como não-enriquecido.
            if "enrichment" in cc or "enrichment_skipped" in cc:
                continue
            cc["enrichment_skipped"] = reason
            marked += 1
        except Exception:  # noqa: BLE001 — marcação é best-effort; nunca derruba o despacho
            continue
    return marked


def load_policy_for_org(organization_id: Optional[int]) -> Optional[CompiledPolicy]:
    """Política ATIVA da org, compilada. **SÍNCRONA** — chamar via ``to_thread``.

    Espelha ``inflight.runtime.load_inflight_rules_for_org`` linha a linha, e pelas
    mesmas razões:

    - ``organization_id is None`` ⇒ ``None`` (**fail-CLOSED**: nunca enriquecer sem
      saber de quem é o evento — a chave de cache e a tabela são por org);
    - qualquer erro de DB ⇒ ``None``, com log. Um problema de banco não pode impedir
      a COLETA, que é o produto;
    - resolve pelo **ponteiro** ``current_version_id``, nunca por "maior
      version_number": rollback re-aponta para uma versão ANTIGA, e ordenar por
      número entregaria a versão errada em produção sem nenhum erro.
    """
    if organization_id is None:
        return None
    if not bool(getattr(_settings(), "ENRICHMENT_ENABLED", False)):
        return None

    import json as _json

    from ...db import database, models

    try:
        with database.SessionLocal() as db:
            policy_row = (
                db.query(models.EnrichmentPolicy)
                .filter(
                    models.EnrichmentPolicy.organization_id == organization_id,
                    models.EnrichmentPolicy.enabled.is_(True),
                )
                .order_by(models.EnrichmentPolicy.created_at.asc())
                .first()
            )
            if policy_row is None:
                return None
            if not policy_row.current_version_id:
                # Política habilitada sem versão vigente é erro de operação, e é
                # exatamente o caso que vira ticket de suporte "liguei e não faz
                # nada". Log explícito em vez de silêncio.
                logger.warning(
                    "enrich: política %r da org %s está habilitada mas não tem "
                    "current_version_id — nada será enriquecido",
                    policy_row.name, organization_id,
                    extra={"event": "enrich.policy_without_version"},
                )
                return None
            version_row = (
                db.query(models.EnrichmentPolicyVersion)
                .filter(models.EnrichmentPolicyVersion.id == policy_row.current_version_id)
                .first()
            )
            if version_row is None:
                logger.warning(
                    "enrich: current_version_id %r da política %r não existe",
                    policy_row.current_version_id, policy_row.name,
                    extra={"event": "enrich.dangling_version"},
                )
                return None
            raw_rules = version_row.rules
    except Exception:  # noqa: BLE001 — a coleta nunca cai por causa do enriquecimento
        logger.exception("enrich: falha carregando política (org %s)", organization_id)
        return None

    try:
        return compile_policy(_json.loads(raw_rules))
    except Exception:  # noqa: BLE001
        # A política foi validada no commit; chegar aqui significa que ela foi
        # gravada por um caminho que não valida, ou que o vocabulário mudou entre
        # versões do worker. Fail-safe para "sem enriquecimento", nunca meia-boca.
        logger.exception(
            "enrich: política da org %s não compila — enriquecimento DESLIGADO "
            "neste ciclo", organization_id,
            extra={"event": "enrich.policy_compile_failed"},
        )
        return None


def _settings():
    from ...core.config import settings

    return settings


def _distinct_keys(
    batch: Sequence[Mapping[str, Any]], rule: CompiledEnrichRule
) -> List[str]:
    """Chaves DISTINTAS do lote para uma regra, JÁ FILTRADAS pelo gate ``when``.

    É a economia central do seam remoto: 200 eventos de um mesmo host colapsam em
    UMA chave, logo UMA chamada. Sem dedup aqui, "bulk" seria só um laço com nome
    melhor. Ordem estável (dict preserva inserção) para o teste ser determinístico.

    O gate roda AQUI, não só no applier: no seam remoto o `when` é o controle de
    custo e de EGRESSO (ADR-LOCAL-0002 §4.1). Avaliá-lo apenas na escrita deixaria
    a chamada externa já ter acontecido — o indicador do cliente sairia para o
    terceiro e a cota seria gasta justamente com o que a política proibia consultar,
    sem nada aparecer no payload para o operador perceber.

    A chave entra se QUALQUER evento do lote passar o gate: dedup não pode descartar
    a chave de um evento que passa por causa de outro, com a mesma chave, que não passa.
    """
    seen: Dict[str, None] = {}
    for envelope in batch:
        if rule.when is not None:
            try:
                if not evaluate_when(rule.when, envelope):
                    continue
            except Exception:  # noqa: BLE001 — predicado nunca derruba o lote
                continue
        key = extract_key(envelope, rule)
        if key is not None:
            seen.setdefault(key, None)
    return list(seen)


# ── métricas: isoladas para o applier nunca precisar conhecer OTel ───────────
def _count_cache(enricher: str, layer: str, result: str, n: int) -> None:
    if n <= 0:
        return
    try:
        from .. import metrics

        metrics.ENRICH_CACHE.labels(enricher=enricher, layer=layer, result=result).inc(n)
    except Exception:  # noqa: BLE001
        logger.debug("enrich: falha ao emitir ENRICH_CACHE", exc_info=True)


def _count_error(enricher: str, reason: str) -> None:
    try:
        from .. import metrics

        metrics.ENRICH_ERRORS.labels(enricher=enricher, reason=reason).inc()
    except Exception:  # noqa: BLE001 — métrica nunca quebra a coleta
        logger.debug("enrich: falha ao emitir ENRICH_ERRORS", exc_info=True)


def _count_budget_exhausted(org_id: int) -> None:
    try:
        from .. import metrics

        metrics.ENRICH_BUDGET_EXHAUSTED.labels(org_id=str(org_id)).inc()
    except Exception:  # noqa: BLE001
        logger.debug("enrich: falha ao emitir ENRICH_BUDGET_EXHAUSTED", exc_info=True)


def _observe_resolve(enricher: str, seconds: float) -> None:
    try:
        from .. import metrics

        metrics.ENRICH_RESOLVE_LATENCY.labels(enricher=enricher).observe(seconds)
    except Exception:  # noqa: BLE001
        logger.debug("enrich: falha ao emitir ENRICH_RESOLVE_LATENCY", exc_info=True)


def _set_table_gauges(enricher: str, org_id: int, table: LookupTable) -> None:
    try:
        from .. import metrics

        metrics.ENRICH_TABLE_BYTES.labels(enricher=enricher, org_id=str(org_id)).set(
            float(table.approx_bytes)
        )
        metrics.ENRICH_TABLE_ENTRIES.labels(enricher=enricher, org_id=str(org_id)).set(
            float(table.entry_count)
        )
    except Exception:  # noqa: BLE001
        logger.debug("enrich: falha ao emitir gauges de tabela", exc_info=True)


def _error_reason(exc: BaseException) -> str:
    """Classifica a exceção numa causa de cardinalidade FECHADA.

    String livre virando label de métrica é explosão de cardinalidade no OTLP, que
    não tem TTL — o mesmo raciocínio de ``SAVING_REASONS``.
    """
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return "timeout"
    if "auth" in name or "unauthor" in name or "forbidden" in name:
        return "auth"
    if "ratelimit" in name or "toomany" in name:
        return "rate_limit"
    if "json" in name or "decode" in name or "parse" in name or "value" in name:
        return "parse"
    if "http" in name or "connect" in name or "network" in name:
        return "http"
    return "unknown"


def emit_apply_stats(stats: ApplyStats, org_id: int) -> None:
    """Traduz o que o aplicador acumulou em métricas. Chamado 1×/ciclo."""
    try:
        from .. import metrics

        for rule_id, n in stats.hits.items():
            metrics.ENRICH_EVENTS.labels(org_id=str(org_id), rule_id=rule_id).inc(n)
        for bucket, outcome in (
            (stats.hits, "hit"),
            (stats.misses, "miss"),
            (stats.skipped, "skipped"),
            (stats.errors, "error"),
        ):
            for rule_id, n in bucket.items():
                metrics.ENRICH_LOOKUPS.labels(enricher=rule_id, outcome=outcome).inc(n)
    except Exception:  # noqa: BLE001
        logger.debug("enrich: falha ao emitir stats de aplicação", exc_info=True)


__all__.append("apply")
