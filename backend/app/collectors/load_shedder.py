"""Load-shedding — broker queue-depth ceiling.

Producer-side admission control: before enqueuing a destination's batch, peek
the destination's shard-queue depth in the **broker** Redis (``LLEN``). If the
queue is at/over the ceiling and the destination's policy is ``drop_newest``,
the new batch is shed (not enqueued) — bounding broker growth so one tenant's
backed-up destination can't OOM the shared Redis.

Design notes:
  - **Fail-open.** Any error (broker unreachable, unexpected reply) → DO NOT shed.
    Shedding is a safety valve, never a delivery gate; a monitoring blip must not
    drop healthy traffic.
  - **Broker Redis, not state Redis.** Celery stores each queue as a Redis list
    keyed by the queue name in the BROKER db (``CELERY_BROKER_URL`` / db 1), not
    the state db. We connect there.
  - **Per-process, fork-safe sync client.** Cached with a pid guard so a forked
    child never reuses the parent's connection.
  - **Shard granularity.** With hash-routing, ``LLEN(shard)`` is the combined
    depth of all destinations on that shard — a conservative (early-shedding)
    proxy for a single destination's backlog. A true per-destination global cap
    (Redis lease) is not yet implemented.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_client = None
_client_pid: Optional[int] = None
_lock = threading.Lock()


def _get_broker_redis():
    """Lazily build a fork-safe sync Redis client to the Celery broker."""
    global _client, _client_pid
    pid = os.getpid()
    if _client is not None and _client_pid == pid:
        return _client
    with _lock:
        if _client is None or _client_pid != pid:
            import redis as redis_sync

            from .celery_app import _broker_url

            _client = redis_sync.from_url(_broker_url(), decode_responses=True)
            _client_pid = pid
    return _client


def queue_depth(queue_name: str) -> Optional[int]:
    """Current broker depth of ``queue_name``, or ``None`` on any error (fail-open)."""
    try:
        return int(_get_broker_redis().llen(queue_name))
    except Exception:
        logger.debug("load_shedder: LLEN falhou para %s (fail-open)", queue_name, exc_info=True)
        return None


def should_shed(queue_name: str, ceiling: int) -> Tuple[bool, Optional[int]]:
    """Return ``(shed, observed_depth)``.

    ``shed`` is True only when the depth is known AND >= ceiling. A ceiling <= 0
    or an unreadable depth never sheds (fail-open). The depth is returned (even
    when not shedding) so callers can publish a queue-depth gauge.
    """
    if ceiling <= 0:
        return (False, None)
    depth = queue_depth(queue_name)
    if depth is None:
        return (False, None)
    return (depth >= ceiling, depth)


def reset() -> None:
    """Drop the cached client — test seam."""
    global _client, _client_pid
    _client = None
    _client_pid = None
