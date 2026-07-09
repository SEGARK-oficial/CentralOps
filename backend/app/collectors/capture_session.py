"""On-demand, time-bounded traffic capture for troubleshooting.

A capture SESSION records every event dispatched for an organization — optionally
filtered by vendor — into a dedicated, short-lived Redis ring, for a bounded window.
Unlike the always-on audit ring (:mod:`audit_buffer`: last 500, 24h, all vendors), a
session is EXPLICIT (the operator starts/stops it), SCOPED (org + optional vendor) and
TIME-BOXED — so the operator captures exactly the traffic of a specific client/vendor
while troubleshooting ("press listening, watch what flows, filtered").

Reuses :func:`audit_buffer._redact` so PII/secrets never hit the ring. Best-effort on
the hot path (:func:`record`): a failure to record NEVER affects dispatch. Keys carry a
Redis TTL (window + grace) so abandoned sessions self-expire.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Dict, List, Mapping, Optional

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


async def record(
    redis: redis_async.Redis,
    batch: List[Dict[str, Any]],
    org_id: int,
) -> None:
    """Anexa o lote às sessões de captura ATIVAS do ``org_id``, filtrando cada evento
    pelo vendor da sessão (``_centralops.vendor``). Best-effort: NUNCA levanta (chamado
    do hot path de dispatch)."""
    if not batch:
        return
    try:
        ids = await redis.smembers(_org_index_key(org_id))
        if not ids:
            return
        now = time.time()
        for raw_id in ids:
            sid = _s(raw_id)
            meta = await redis.hgetall(_meta_key(sid))
            if not meta:
                await redis.srem(_org_index_key(org_id), sid)
                continue
            m = {_s(k): _s(v) for k, v in meta.items()}
            if m.get("status") != "active" or float(m.get("expires_at") or 0) < now:
                continue
            vfilter = (m.get("vendor") or "").strip()
            ring_size = int(m.get("ring_size") or DEFAULT_RING_SIZE)
            events = [
                ev
                for ev in batch
                if not vfilter
                or ((ev.get("_centralops") or {}).get("vendor") == vfilter)
            ]
            if not events:
                continue
            serialized = [
                json.dumps(
                    {
                        "event": _redact(ev),
                        "vendor": (ev.get("_centralops") or {}).get("vendor"),
                        "captured_at": now,
                    },
                    separators=(",", ":"),
                    default=str,
                )
                for ev in events
            ]
            # TTL próprio no ring de eventos (janela restante + graça) — senão o ring
            # vira órfão quando o meta expira. Renovado a cada gravação.
            evt_ttl = max(
                GRACE_SECONDS,
                int(float(m.get("expires_at") or now) - now) + GRACE_SECONDS,
            )
            pipe = redis.pipeline()
            pipe.lpush(_events_key(sid), *serialized)
            pipe.ltrim(_events_key(sid), 0, ring_size - 1)
            pipe.expire(_events_key(sid), evt_ttl)
            pipe.hincrby(_meta_key(sid), "event_count", len(events))
            await pipe.execute()
    except Exception as exc:  # pragma: no cover — captura nunca quebra o dispatch
        logger.debug("capture_session.record falhou (não-fatal): %s", exc)
