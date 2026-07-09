"""Global cross-process concurrency cap per destination.

This is the **first layer** of the bulkhead: a true GLOBAL cap on the number
of concurrent ``send_batch`` operations against ONE destination, enforced across
ALL prefork worker processes (and hosts) — not just within a single event loop.

Why the per-loop ``asyncio.Semaphore`` in ``concurrency_pool`` is not enough:
  under the Celery prefork pool every child process owns its own loop, so a
  per-loop semaphore sized ``concurrency=N`` lets ``W`` workers run ``W × N``
  concurrent sends against a sick sink — exactly the hammering this layer
  prevents. ``concurrency_pool`` stays as a cheap second layer (intra-loop fan-out
  bounding); this module is the cross-process ceiling.

Mechanism — leaky-lease, mirrored from ``domain_limiter.DomainLimiter``:
  a Redis Sorted Set per ``destination_id`` holds one ``(timestamp_ms, token)``
  member per in-flight slot. A Lua script does the atomic
  ``evict-expired → count → add-if-room`` admission test. The lease TTL means a
  worker that dies mid-send NEVER permanently leaks its slot — its member ages
  out of the window and the slot self-heals. Release is best-effort ``ZREM`` of
  the token under ``try/finally`` at the caller.

Fail-OPEN by design (RNF — infra must never drop delivery):
  if Redis is unreachable or the script errors, ``slot()`` logs at DEBUG and
  proceeds WITHOUT a lease. The per-loop semaphore still bounds intra-process
  fan-out, so we degrade to per-loop-only bounding instead of blocking a healthy
  batch on an infra hiccup. Acquisition timeout (cap saturated) ALSO fails open:
  better a transient over-limit than a stuck delivery.

Out of scope (NOT here): AIMD / EWMA adaptive caps. This
module implements only the STATIC lease whose ceiling is ``DeliveryConfig.concurrency``.

Uniform path: ``wazuh-default`` no longer has a dedicated lane —
it flows through the same per-destination dispatch path as every other destination,
so it IS subject to this limiter like any other destination.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)

# Lease window. Must comfortably exceed the worst-case wall time of one
# ``send_batch`` (all chunks + retries under ``timeout_ms``) so a live, slow
# sender does not get its slot evicted out from under it. A dead worker's slot
# is reclaimed at most ``LEASE_MS`` after it stopped renewing.
LEASE_MS = 120_000

# How long a caller will poll for a free slot before giving up and failing open.
DEFAULT_MAX_WAIT_SECONDS = 30.0
POLL_SLEEP = 0.1

# KEYS[1] = sorted set; ARGV = now_ms, lease_ms, limit, token.
# Atomic: evict expired members → count live slots → admit if under the cap.
# PEXPIRE bounds key lifetime so an idle destination's key disappears on its own.
_ACQUIRE_LUA = """
local key   = KEYS[1]
local now   = tonumber(ARGV[1])
local lease = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local token = ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, 0, now - lease)
local count = redis.call('ZCARD', key)
if count < limit then
    redis.call('ZADD', key, now, token)
    redis.call('PEXPIRE', key, lease)
    return 1
end
return 0
"""


class DestinationLimiter:
    """Cross-process concurrency lease keyed by ``destination_id``.

    One instance wraps a Redis client; ``slot(destination_id, concurrency)``
    yields an async context manager that holds a global slot for the destination
    and releases it on exit (success, exception, or timeout). The ceiling is the
    caller-supplied ``concurrency`` (``DeliveryConfig.concurrency``).
    """

    def __init__(
        self,
        redis: Any,
        *,
        max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS,
    ) -> None:
        self.redis = redis
        self.max_wait_seconds = max_wait_seconds

    @staticmethod
    def _key(destination_id: str) -> str:
        return f"dest_concurrency:{destination_id}"

    async def _try_acquire(self, key: str, limit: int, token: str) -> bool:
        now_ms = int(time.time() * 1000)
        result = await self.redis.eval(
            _ACQUIRE_LUA, 1, key, now_ms, LEASE_MS, limit, token
        )
        return bool(result)

    @contextlib.asynccontextmanager
    async def slot(
        self, destination_id: str, concurrency: int
    ) -> AsyncIterator[None]:
        """Hold a global slot for ``destination_id`` (ceiling ``concurrency``).

        Fail-OPEN: any Redis error, or exhausting ``max_wait_seconds`` while the
        cap is saturated, proceeds WITHOUT a lease rather than blocking delivery.
        Release is guaranteed via ``try/finally`` whenever a lease was held.
        """
        limit = concurrency if concurrency >= 1 else 1
        key = self._key(destination_id)
        token = uuid.uuid4().hex
        held = False

        deadline = time.monotonic() + self.max_wait_seconds
        while True:
            try:
                held = await self._try_acquire(key, limit, token)
            except Exception:
                # Infra fault (Redis down / script error). Never block delivery:
                # degrade to the per-loop semaphore (concurrency_pool) only.
                logger.debug(
                    "destination_limiter: Redis indisponível acquire dest=%s — "
                    "fail-open (sem lease global)",
                    destination_id,
                    exc_info=True,
                )
                held = False
                break
            if held:
                break
            if time.monotonic() >= deadline:
                # Cap saturated longer than we are willing to wait. Fail open: a
                # transient over-limit beats a stuck batch. The breaker + per-loop
                # semaphore remain in force.
                logger.debug(
                    "destination_limiter: timeout aguardando slot dest=%s cap=%d — "
                    "fail-open (entrega segue)",
                    destination_id,
                    limit,
                )
                held = False
                break
            await asyncio.sleep(POLL_SLEEP)

        try:
            yield
        finally:
            if held:
                try:
                    await self.redis.zrem(key, token)
                except Exception:  # pragma: no cover - defensive
                    # Slot will age out of the window via the lease TTL; no leak.
                    logger.debug(
                        "destination_limiter: falha ao liberar slot dest=%s "
                        "(TTL recupera)",
                        destination_id,
                        exc_info=True,
                    )
