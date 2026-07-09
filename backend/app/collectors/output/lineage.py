"""Event lineage recorder.

Registers a positive delivery record per (event_id, destination) in Redis.
This fills the observability gap: before this module only DLQ (failure)
and idempotence (dedupe) traces existed — there was NO positive record of
where a successfully delivered event went.

Design constraints
------------------
- **Retention-limited, NOT compliance archive.** Lineage is queryable for
  recent events (default 7 days via ``LINEAGE_TTL_S``).  Older events are
  gone from Redis; for long-term compliance use the JSONL/Elasticsearch sink.
- **Org-scoped keys.** Key scheme ``lineage:{org_id}:{event_id}`` ensures
  cross-tenant isolation (same principle as ``collector:audit:{org_id}:recent``
  in audit_buffer.py).  A query without ``org_id`` cannot leak across tenants.
- **Fail-open.** A Redis error NEVER raises into the delivery hot-path.  The
  delivery is already complete when lineage is recorded; losing the trace is
  preferable to failing the delivery.
- **Gated.** Recording only happens when ``LINEAGE_ENABLED=True``.  Multi-
  destination dispatch is GA, so lineage is gated solely by
  its own flag.  With it OFF the module is a no-op.

Redis key schema
----------------
  lineage:{org_id}:{event_id}   LIST  (newest-first, JSON entries)

Each entry in the list::

    {
        "destination_id": "uuid",
        "kind": "splunk_hec",
        "status": "delivered",
        "ts": 1718000000.123
    }

The list is LTRIM-capped at ``_LINEAGE_LIST_MAX`` (50) so a high-fanout
event with many destinations does not grow unboundedly.  TTL is set on every
write (sliding).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────

_DEFAULT_TTL_S: int = 7 * 24 * 3600  # 7 days
_LINEAGE_LIST_MAX: int = 50  # max entries per (org, event_id)

# ── Redis client (fork-safe, mirrors observability_store pattern) ─────

_client = None
_client_pid: Optional[int] = None
_lock = threading.Lock()


def _redis() -> Any:
    """Fork-safe cached SYNC redis client to the STATE redis."""
    global _client, _client_pid
    pid = os.getpid()
    if _client is not None and _client_pid == pid:
        return _client
    with _lock:
        if _client is None or _client_pid != pid:
            import redis as redis_sync

            from ...core.config import settings

            _client = redis_sync.from_url(
                settings.REDIS_URL or "redis://localhost:6379/0",
                decode_responses=True,
            )
            _client_pid = pid
    return _client


def _lineage_key(org_id: int, event_id: str) -> str:
    """Org-scoped lineage key — ``org_id`` in the key guarantees tenant isolation."""
    return f"lineage:{org_id}:{event_id}"


def _ttl() -> int:
    """Resolve TTL from settings at call-time (tests can override via env)."""
    from ...core.config import settings

    return int(getattr(settings, "LINEAGE_TTL_S", _DEFAULT_TTL_S))


def _is_enabled() -> bool:
    """True when LINEAGE_ENABLED. Multi-destination dispatch is always active
    now (GA), so lineage is gated solely by its own flag."""
    from ...core.config import settings

    return bool(getattr(settings, "LINEAGE_ENABLED", False))


# ── Write ──────────────────────────────────────────────────────────────


def record_delivery(
    *,
    org_id: int,
    event_id: str,
    destination_id: str,
    kind: str,
    ts: Optional[float] = None,
) -> None:
    """Record a successful delivery of ``event_id`` to ``destination_id``.

    Fail-open: any Redis error is swallowed and logged at DEBUG.
    Gated: no-op when ``LINEAGE_ENABLED`` or the dispatch flags are off.
    """
    if not _is_enabled():
        return
    if not event_id:
        return

    entry: Dict[str, Any] = {
        "destination_id": destination_id,
        "kind": kind,
        "status": "delivered",
        "ts": ts if ts is not None else time.time(),
    }
    try:
        key = _lineage_key(org_id, event_id)
        r = _redis()
        pipe = r.pipeline(transaction=True)
        pipe.lpush(key, json.dumps(entry, default=str))
        pipe.ltrim(key, 0, _LINEAGE_LIST_MAX - 1)
        pipe.expire(key, _ttl())
        pipe.execute()
    except Exception:
        logger.debug(
            "lineage.record_delivery falhou (org=%s event=%s dest=%s)",
            org_id,
            event_id,
            destination_id,
            exc_info=True,
        )


# ── Read ───────────────────────────────────────────────────────────────


def query_lineage(org_id: int, event_id: str) -> List[Dict[str, Any]]:
    """Return delivery entries for ``event_id`` scoped to ``org_id``.

    Returns an empty list on any error (best-effort reads).
    """
    try:
        key = _lineage_key(org_id, event_id)
        raw: List[str] = _redis().lrange(key, 0, _LINEAGE_LIST_MAX - 1) or []
    except Exception:
        logger.debug(
            "lineage.query_lineage falhou (org=%s event=%s)",
            org_id,
            event_id,
            exc_info=True,
        )
        return []

    out: List[Dict[str, Any]] = []
    for item in raw:
        try:
            out.append(json.loads(item))
        except (TypeError, ValueError):
            continue
    return out


# ── Test seam ─────────────────────────────────────────────────────────


def reset() -> None:
    """Drop the cached client (test seam — mirrors observability_store.reset)."""
    global _client, _client_pid
    _client = None
    _client_pid = None
