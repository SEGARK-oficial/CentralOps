"""On-demand, time-bounded traffic capture for troubleshooting.

A capture SESSION records the LIFECYCLE of every event of an organization — optionally
filtered by vendor — into a dedicated, short-lived Redis ring, for a bounded window.
Unlike the always-on audit ring (:mod:`audit_buffer`: last 500, 24h, all vendors), a
session is EXPLICIT (the operator starts/stops it), SCOPED (org + optional vendor) and
TIME-BOXED — so the operator captures exactly the traffic of a specific client/vendor
while troubleshooting ("press listening, watch what flows, filtered").

TAP DE CICLO DE VIDA (não só de entrega). Cada registro carrega um ``outcome`` — o
DESFECHO daquele evento (ver :data:`OUTCOMES`) — para o operador responder "como entrou
e como saiu aquele log". Antes deste tap, o único ponto de gravação ficava atrás da
guarda ``accepted_total > 0`` do dispatch: tudo que era coletado mas NÃO entregue
(drop, sem rota, quarentena, sink fora do ar, breaker, suppress, sample) era INVISÍVEL —
o operador via "capturei nada" sem distinguir "não houve tráfego" de "morreu antes do
tap". Um evento entregue a N destinos gera N registros ``delivered`` (desfecho POR
destino, desejável); o ring (``MAX_RING_SIZE``) e o TTL continuam limitando o volume.

Reuses :func:`audit_buffer._redact` so PII/secrets never hit the ring. Best-effort on
the hot path (:func:`record` / :func:`record_sync`): a failure to record NEVER affects
dispatch nor collection. Keys carry a Redis TTL (window + grace) so abandoned sessions
self-expire.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Mapping, Optional, Sequence

import redis.asyncio as redis_async

from .audit_buffer import _redact

logger = logging.getLogger(__name__)

DEFAULT_DURATION_SECONDS = 300
MAX_DURATION_SECONDS = 3600
DEFAULT_RING_SIZE = 5000
MAX_RING_SIZE = 20000
# Mantém os eventos legíveis um tempo após a janela fechar (operador revisa depois).
GRACE_SECONDS = 300
# Teto de sessões simultâneas POR ORG — anti-abuso (cada sessão tem um ring; sem teto,
# um admin poderia exaurir a memória do Redis e o loop de record() ficaria O(N)).
MAX_SESSIONS_PER_ORG = 5
# Teto do texto livre de ``detail`` (motivo curto) — o ring não é lugar de stacktrace.
MAX_DETAIL_CHARS = 200


# ── Desfechos (outcome) ────────────────────────────────────────────────
#: Entregue a UM destino (um registro por destino que aceitou).
OUTCOME_DELIVERED = "delivered"
#: O lote chegou ao dispatch mas NÃO foi entregue (destino ausente/desabilitado,
#: cross-tenant, rejeição 4xx do sink, breaker aberto, sink fora do ar).
OUTCOME_DELIVERY_FAILED = "delivery_failed"
#: Descartado por uma rota ``action=drop`` (filtro de ruído / controle de custo).
OUTCOME_DROPPED = "dropped"
#: Nenhuma rota casou e não há destino default → DLQ (``error_kind=unrouted``).
OUTCOME_UNROUTED = "unrouted"
#: Fonte Wazuh suprimida de um destino que voltaria ao próprio manager (anti-loop).
OUTCOME_LOOP_BLOCKED = "loop_blocked"
#: Par (evento, destino) excluído por conflito de residência de dados.
OUTCOME_RESIDENCY_BLOCKED = "residency_blocked"
#: Amostrado PARA FORA de um destino pela alavanca de redução (sample_percent).
OUTCOME_SAMPLED_OUT = "sampled_out"
#: Suprimido pelo rate-limit por assinatura (Number-to-Allow) antes do roteamento.
OUTCOME_SUPPRESSED = "suppressed"
#: Quarentenado na normalização/validação (mapping ausente, OCSF inválido, ...).
OUTCOME_QUARANTINED = "quarantined"

#: Vocabulário fechado dos desfechos (a UI pode filtrar/agrupar por ele).
OUTCOMES = frozenset(
    {
        OUTCOME_DELIVERED,
        OUTCOME_DELIVERY_FAILED,
        OUTCOME_DROPPED,
        OUTCOME_UNROUTED,
        OUTCOME_LOOP_BLOCKED,
        OUTCOME_RESIDENCY_BLOCKED,
        OUTCOME_SAMPLED_OUT,
        OUTCOME_SUPPRESSED,
        OUTCOME_QUARANTINED,
    }
)


class CaptureLimitReached(RuntimeError):
    """O org atingiu ``MAX_SESSIONS_PER_ORG`` sessões simultâneas."""


def _meta_key(session_id: str) -> str:
    return f"capture:session:{session_id}:meta"


def _events_key(session_id: str) -> str:
    return f"capture:session:{session_id}:events"


def _org_index_key(org_id: int) -> str:
    return f"capture:sessions:org:{org_id}"


def _s(value: Any) -> str:
    return value.decode() if isinstance(value, (bytes, bytearray)) else str(value)


# ── Short-circuit barato (cache NEGATIVO) ──────────────────────────────
# O tap agora é chamado em vários pontos do hot path (por lote no roteamento, por
# evento suprimido/quarentenado). O caso ESMAGADORAMENTE comum é "org sem nenhuma
# sessão ativa" — e descobrir isso custava um round-trip Redis por chamada.
# Memoizamos APENAS a ausência (nunca a presença) por uma janela curta: um org sem
# sessão pula o Redis inteiro; um org COM sessão relê sempre (nada de evento perdido
# por cache velho). ``start_session`` invalida o próprio processo; outros processos
# (API inicia a sessão, worker grava) convergem em ≤ ``_NO_SESSION_TTL_SECONDS``.
_NO_SESSION_TTL_SECONDS = 2.0
_NO_SESSION_CACHE_MAX = 10_000
_no_session_until: Dict[int, float] = {}


def _absent_cached(org_id: Any, now: float) -> bool:
    try:
        return _no_session_until.get(int(org_id), 0.0) > now
    except (TypeError, ValueError):
        return False


def _mark_absent(org_id: Any, now: float) -> None:
    try:
        key = int(org_id)
    except (TypeError, ValueError):
        return
    if len(_no_session_until) >= _NO_SESSION_CACHE_MAX:  # pragma: no cover — guarda
        _no_session_until.clear()
    _no_session_until[key] = now + _NO_SESSION_TTL_SECONDS


def reset_session_cache(org_id: Optional[int] = None) -> None:
    """Invalida o cache negativo (um org, ou tudo). Chamado por ``start_session`` e
    pelos testes — o cache é estado de módulo."""
    if org_id is None:
        _no_session_until.clear()
        return
    try:
        _no_session_until.pop(int(org_id), None)
    except (TypeError, ValueError):
        pass


# ── Cliente SYNC (produtor/roteamento) ─────────────────────────────────
# O tap de roteamento (``_enqueue_routed``) e o de quarentena rodam em contexto
# SÍNCRONO. Espelha o cliente fork-safe cacheado de ``observability_store`` (mesma
# ``REDIS_URL``, mesmo ``decode_responses``) para que produtor (sync), worker (async)
# e API compartilhem o MESMO ring.
_sync_client = None
_sync_client_pid: Optional[int] = None
_sync_lock = threading.Lock()


def _sync_redis():
    global _sync_client, _sync_client_pid
    pid = os.getpid()
    if _sync_client is not None and _sync_client_pid == pid:
        return _sync_client
    with _sync_lock:
        if _sync_client is None or _sync_client_pid != pid:
            import redis as redis_sync

            from ..core.config import settings

            _sync_client = redis_sync.from_url(
                settings.REDIS_URL or "redis://localhost:6379/0", decode_responses=True
            )
            _sync_client_pid = pid
    return _sync_client


# ── Partes PURAS compartilhadas pelos taps async e sync ────────────────


def _event_vendor(ev: Any) -> Optional[str]:
    """``_centralops.vendor`` do envelope, tolerante a payloads não-dict."""
    if not isinstance(ev, Mapping):
        return None
    labels = ev.get("_centralops")
    if not isinstance(labels, Mapping):
        return None
    vendor = labels.get("vendor")
    return None if vendor is None else str(vendor)


def _vendor_matches(vfilter: str, vendor: Optional[str]) -> bool:
    """Filtro de vendor da sessão, CASE-INSENSITIVE: uma sessão criada como "Sophos"
    casa eventos rotulados "sophos" (o operador digita o nome, o coletor emite o slug).
    Filtro vazio = casa tudo."""
    if not vfilter:
        return True
    if vendor is None:
        return False
    return vendor.strip().casefold() == vfilter.casefold()


def _session_is_active(m: Mapping[str, str], now: float) -> bool:
    return m.get("status") == "active" and float(m.get("expires_at") or 0) >= now


def _ring_params(m: Mapping[str, str], now: float) -> tuple:
    """(ring_size clampado, TTL do ring de eventos) da sessão."""
    try:
        ring_size = int(m.get("ring_size") or DEFAULT_RING_SIZE)
    except (TypeError, ValueError):
        ring_size = DEFAULT_RING_SIZE
    ring_size = max(1, min(ring_size, MAX_RING_SIZE))
    # TTL próprio no ring de eventos (janela restante + graça) — senão o ring vira
    # órfão quando o meta expira. Renovado a cada gravação.
    evt_ttl = max(
        GRACE_SECONDS,
        int(float(m.get("expires_at") or now) - now) + GRACE_SECONDS,
    )
    return ring_size, evt_ttl


def _entries_for(
    m: Mapping[str, str],
    batch: Sequence[Any],
    now: float,
    outcome: str,
    destination_id: Optional[str],
    detail: Optional[str],
) -> List[str]:
    """Serializa os eventos do lote que passam pelo filtro de vendor DESTA sessão.

    Formato COMPATÍVEL com o que a UI já lê (``event``/``vendor``/``captured_at``);
    ``outcome`` é sempre adicionado e ``destination_id``/``detail`` só quando aplicáveis
    (mantém o ring enxuto)."""
    vfilter = (m.get("vendor") or "").strip()
    out: List[str] = []
    for ev in batch:
        vendor = _event_vendor(ev)
        if not _vendor_matches(vfilter, vendor):
            continue
        payload: Dict[str, Any] = {
            "event": _redact(ev),
            "vendor": vendor,
            "captured_at": now,
            "outcome": outcome,
        }
        if destination_id is not None:
            payload["destination_id"] = str(destination_id)
        if detail:
            payload["detail"] = str(detail)[:MAX_DETAIL_CHARS]
        out.append(json.dumps(payload, separators=(",", ":"), default=str))
    return out


def _decode_meta(meta: Mapping[Any, Any]) -> Dict[str, Any]:
    """Normaliza o hash do Redis (bytes|str) para um dict tipado + status derivado."""
    m = {_s(k): _s(v) for k, v in meta.items()}
    now = time.time()
    expires_at = float(m.get("expires_at") or 0)
    raw_status = m.get("status") or "active"
    # 'active' só enquanto não expirou nem foi parado explicitamente.
    status = "expired" if (raw_status == "active" and expires_at < now) else raw_status
    return {
        "id": m.get("id", ""),
        "organization_id": int(m["org_id"]) if m.get("org_id") else None,
        "vendor": m.get("vendor") or None,
        "created_at": float(m["created_at"]) if m.get("created_at") else None,
        "expires_at": expires_at or None,
        "status": status,
        "event_count": int(m.get("event_count") or 0),
    }


async def start_session(
    redis: redis_async.Redis,
    org_id: int,
    *,
    vendor: Optional[str] = None,
    duration_seconds: int = DEFAULT_DURATION_SECONDS,
    ring_size: int = DEFAULT_RING_SIZE,
) -> Dict[str, Any]:
    """Inicia uma sessão de captura escopada a ``org_id`` (e opcionalmente ``vendor``)."""
    # Anti-abuso: teto de sessões simultâneas por org.
    active = await redis.scard(_org_index_key(org_id))
    if active >= MAX_SESSIONS_PER_ORG:
        raise CaptureLimitReached(
            f"limite de {MAX_SESSIONS_PER_ORG} sessões de captura simultâneas atingido"
        )
    duration = max(1, min(int(duration_seconds), MAX_DURATION_SECONDS))
    size = max(1, min(int(ring_size), MAX_RING_SIZE))
    now = time.time()
    session_id = uuid.uuid4().hex
    meta = {
        "id": session_id,
        "org_id": str(org_id),
        "vendor": (vendor or "").strip(),
        # str() (não repr()) garante string decimal parseável (contrato documentado).
        "created_at": str(now),
        "expires_at": str(now + duration),
        "ring_size": str(size),
        "status": "active",
        "event_count": "0",
    }
    ttl = duration + GRACE_SECONDS
    # TTL do índice é FIXO (não regride p/ a janela da última sessão) — senão uma
    # sessão curta encurtaria o índice e sumiria sessões longas dele.
    index_ttl = MAX_DURATION_SECONDS + GRACE_SECONDS
    pipe = redis.pipeline()
    pipe.hset(_meta_key(session_id), mapping=meta)
    pipe.expire(_meta_key(session_id), ttl)
    pipe.sadd(_org_index_key(org_id), session_id)
    pipe.expire(_org_index_key(org_id), index_ttl)
    await pipe.execute()
    # invalida o cache negativo DESTE processo (os demais convergem pelo TTL curto).
    reset_session_cache(org_id)
    logger.info(
        "capture: sessão iniciada id=%s org=%s vendor=%s duração=%ss",
        session_id, org_id, vendor or "*", duration,
    )
    return _decode_meta(meta)


async def get_session(redis: redis_async.Redis, session_id: str) -> Optional[Dict[str, Any]]:
    meta = await redis.hgetall(_meta_key(session_id))
    if not meta:
        return None
    return _decode_meta(meta)


async def list_sessions(redis: redis_async.Redis, org_id: int) -> List[Dict[str, Any]]:
    """Sessões (ativas/expiradas/paradas) do tenant, mais recentes primeiro. Poda ids
    cujo meta já expirou no Redis (TTL)."""
    ids = await redis.smembers(_org_index_key(org_id))
    out: List[Dict[str, Any]] = []
    for raw_id in ids:
        sid = _s(raw_id)
        meta = await redis.hgetall(_meta_key(sid))
        if not meta:
            await redis.srem(_org_index_key(org_id), sid)  # TTL expirou o meta
            continue
        out.append(_decode_meta(meta))
    out.sort(key=lambda m: m.get("created_at") or 0, reverse=True)
    return out


async def stop_session(
    redis: redis_async.Redis, session_id: str, org_id: int
) -> bool:
    """Marca a sessão como parada (mantém os eventos legíveis até o TTL). Verifica o
    org DONO no próprio engine (defense-in-depth: não confia só no gate HTTP) — uma
    sessão de outro tenant nunca é alterada."""
    meta = await redis.hgetall(_meta_key(session_id))
    if not meta:
        return False
    m = {_s(k): _s(v) for k, v in meta.items()}
    if int(m.get("org_id") or -1) != int(org_id):
        return False
    await redis.hset(_meta_key(session_id), "status", "stopped")
    return True


async def delete_session(redis: redis_async.Redis, session_id: str, org_id: int) -> None:
    """Remove a sessão + seus eventos imediatamente (dado sensível)."""
    pipe = redis.pipeline()
    pipe.delete(_meta_key(session_id))
    pipe.delete(_events_key(session_id))
    pipe.srem(_org_index_key(org_id), session_id)
    await pipe.execute()


async def read_events(
    redis: redis_async.Redis, session_id: str, *, limit: int = 200
) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), MAX_RING_SIZE))
    raw = await redis.lrange(_events_key(session_id), 0, limit - 1)
    events: List[Dict[str, Any]] = []
    for item in raw:
        try:
            events.append(json.loads(_s(item)))
        except Exception:  # pragma: no cover — entrada corrompida é ignorada
            continue
    return events


async def active_sessions(
    redis: redis_async.Redis, org_id: Any
) -> List[Dict[str, str]]:
    """Metas CRUAS das sessões ATIVAS do ``org_id`` (``[]`` = nada a capturar).

    Resolva UMA vez por lote e passe em ``sessions=`` quando for gravar vários
    desfechos do mesmo lote — evita reabrir o índice a cada desfecho. Best-effort:
    NUNCA levanta (devolve ``[]``). Usa o cache negativo (ver ``_no_session_until``)."""
    try:
        now = time.time()
        if _absent_cached(org_id, now):
            return []
        ids = await redis.smembers(_org_index_key(org_id))
        if not ids:
            _mark_absent(org_id, now)
            return []
        out: List[Dict[str, str]] = []
        for raw_id in ids:
            sid = _s(raw_id)
            meta = await redis.hgetall(_meta_key(sid))
            if not meta:
                await redis.srem(_org_index_key(org_id), sid)  # TTL expirou o meta
                continue
            m = {_s(k): _s(v) for k, v in meta.items()}
            if not _session_is_active(m, now):
                continue
            m.setdefault("id", sid)
            out.append(m)
        if not out:
            _mark_absent(org_id, now)
        return out
    except Exception as exc:  # pragma: no cover — nunca quebra o hot path
        # Redis fora do ar: memoiza a ausência para não tentar reconectar a CADA
        # lote/evento do hot path (a captura é diagnóstico, não pode virar custo).
        _mark_absent(org_id, time.time())
        logger.debug("capture_session.active_sessions falhou (não-fatal): %s", exc)
        return []


def likely_no_session(org_id: Any) -> bool:
    """Sonda EM MEMÓRIA (zero I/O) do cache negativo: True quando aprendemos há pouco
    que este org NÃO tem sessão de captura ativa.

    Existe para o caller pular o hop de thread-pool no hot path. Sem ela, um caminho
    de alto volume que grava desfecho POR EVENTO (supressão, no laço de coleta) paga
    ~50µs/evento só em troca de contexto para descobrir que não há nada a gravar —
    medido 130× mais caro que a chamada síncrona direta.

    CONSERVADORA: devolve False quando não sabemos (cache frio/expirado ou erro), aí o
    caminho normal decide. Nunca levanta."""
    try:
        return _absent_cached(org_id, time.time())
    except Exception:  # noqa: BLE001 — sonda best-effort; na dúvida, não pula
        return False


def active_sessions_sync(org_id: Any, *, redis: Any = None) -> List[Dict[str, str]]:
    """Versão SÍNCRONA de :func:`active_sessions` (produtor/roteamento). Best-effort."""
    try:
        now = time.time()
        if _absent_cached(org_id, now):
            return []
        r = redis if redis is not None else _sync_redis()
        ids = r.smembers(_org_index_key(org_id))
        if not ids:
            _mark_absent(org_id, now)
            return []
        out: List[Dict[str, str]] = []
        for raw_id in ids:
            sid = _s(raw_id)
            meta = r.hgetall(_meta_key(sid))
            if not meta:
                r.srem(_org_index_key(org_id), sid)
                continue
            m = {_s(k): _s(v) for k, v in meta.items()}
            if not _session_is_active(m, now):
                continue
            m.setdefault("id", sid)
            out.append(m)
        if not out:
            _mark_absent(org_id, now)
        return out
    except Exception as exc:  # pragma: no cover — nunca quebra a coleta/roteamento
        _mark_absent(org_id, time.time())  # ver :func:`active_sessions`
        logger.debug("capture_session.active_sessions_sync falhou (não-fatal): %s", exc)
        return []


async def record(
    redis: redis_async.Redis,
    batch: Sequence[Any],
    org_id: Any,
    *,
    outcome: str = OUTCOME_DELIVERED,
    destination_id: Optional[str] = None,
    detail: Optional[str] = None,
    sessions: Optional[Sequence[Mapping[str, str]]] = None,
) -> None:
    """Anexa o lote às sessões de captura ATIVAS do ``org_id`` com o DESFECHO
    ``outcome``, filtrando cada evento pelo vendor da sessão (case-insensitive).

    ``outcome`` default ``delivered`` (compatível com o call-site histórico do
    dispatch). ``destination_id`` identifica o destino quando o desfecho é por-destino
    (delivered / delivery_failed / residency_blocked / sampled_out); ``detail`` é um
    motivo CURTO (truncado em ``MAX_DETAIL_CHARS``) — juntos respondem "como entrou e
    como saiu". ``sessions`` reusa uma resolução prévia de :func:`active_sessions`.

    Best-effort: NUNCA levanta (chamado do hot path de dispatch/coleta)."""
    if not batch:
        return
    try:
        metas = (
            list(sessions)
            if sessions is not None
            else await active_sessions(redis, org_id)
        )
        if not metas:
            return
        now = time.time()
        for m in metas:
            sid = m.get("id") or ""
            if not sid:
                continue
            entries = _entries_for(m, batch, now, outcome, destination_id, detail)
            if not entries:
                continue
            ring_size, evt_ttl = _ring_params(m, now)
            pipe = redis.pipeline()
            pipe.lpush(_events_key(sid), *entries)
            pipe.ltrim(_events_key(sid), 0, ring_size - 1)
            pipe.expire(_events_key(sid), evt_ttl)
            pipe.hincrby(_meta_key(sid), "event_count", len(entries))
            # Contador POR DESFECHO no meta: sobrevive à poda do ring (o ltrim
            # descarta eventos antigos, o contador não), então a UI distingue
            # "sessão ativa e nada aconteceu" de "houve tráfego, mas rolou".
            pipe.hincrby(_meta_key(sid), f"outcome:{outcome}", len(entries))
            await pipe.execute()
    except Exception as exc:  # pragma: no cover — captura nunca quebra o dispatch
        logger.debug("capture_session.record falhou (não-fatal): %s", exc)


def record_sync(
    batch: Sequence[Any],
    org_id: Any,
    *,
    outcome: str = OUTCOME_DELIVERED,
    destination_id: Optional[str] = None,
    detail: Optional[str] = None,
    sessions: Optional[Sequence[Mapping[str, str]]] = None,
    redis: Any = None,
) -> None:
    """Versão SÍNCRONA de :func:`record` para os taps que rodam fora do event loop
    (roteamento no produtor, quarentena via ``asyncio.to_thread``). Mesmo ring, mesmo
    contrato best-effort — NUNCA levanta."""
    if not batch:
        return
    try:
        r = redis if redis is not None else _sync_redis()
        metas = (
            list(sessions)
            if sessions is not None
            else active_sessions_sync(org_id, redis=r)
        )
        if not metas:
            return
        now = time.time()
        for m in metas:
            sid = m.get("id") or ""
            if not sid:
                continue
            entries = _entries_for(m, batch, now, outcome, destination_id, detail)
            if not entries:
                continue
            ring_size, evt_ttl = _ring_params(m, now)
            pipe = r.pipeline()
            pipe.lpush(_events_key(sid), *entries)
            pipe.ltrim(_events_key(sid), 0, ring_size - 1)
            pipe.expire(_events_key(sid), evt_ttl)
            pipe.hincrby(_meta_key(sid), "event_count", len(entries))
            # Contador POR DESFECHO no meta: sobrevive à poda do ring (o ltrim
            # descarta eventos antigos, o contador não), então a UI distingue
            # "sessão ativa e nada aconteceu" de "houve tráfego, mas rolou".
            pipe.hincrby(_meta_key(sid), f"outcome:{outcome}", len(entries))
            pipe.execute()
    except Exception as exc:  # pragma: no cover — captura nunca quebra a coleta
        logger.debug("capture_session.record_sync falhou (não-fatal): %s", exc)
