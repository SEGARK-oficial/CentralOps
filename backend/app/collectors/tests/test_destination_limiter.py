"""global cross-process concurrency cap per destination.

Mirrors ``test_domain_limiter`` over the ``DestinationLimiter`` leaky-lease
(Redis Sorted Set + Lua). Covers the four contract requirements:
  1. a real cap (>cap concurrent acquirers serialise — proven via the lease
     directly and via two limiters sharing one Redis = two "processes");
  2. fail-OPEN when Redis is unreachable (delivery never blocks);
  3. release guaranteed under try/finally even on exception/timeout;
  4. the ceiling is the caller-supplied ``concurrency`` (DeliveryConfig.concurrency).
"""

from __future__ import annotations

import asyncio

import pytest

from ..output import destination_limiter as dl
from ..output.destination_limiter import DestinationLimiter


# ── (1) real cap, cross-process ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_respects_concurrency_cap(redis_client) -> None:
    """5 concurrent acquirers on cap=2 → at most 2 hold a slot at any instant."""
    limiter = DestinationLimiter(redis_client, max_wait_seconds=5.0)

    current = 0
    max_seen = 0

    async def worker(idx: int) -> None:
        nonlocal current, max_seen
        async with limiter.slot("splunk-prod-001", concurrency=2):
            current += 1
            max_seen = max(max_seen, current)
            await asyncio.sleep(0.05)
            current -= 1

    await asyncio.gather(*[worker(i) for i in range(5)])
    assert max_seen == 2, f"cap=2 violado, máximo simultâneo observado={max_seen}"
    # All slots released → ZSET empty.
    assert await redis_client.zcard("dest_concurrency:splunk-prod-001") == 0


@pytest.mark.asyncio
async def test_cap_is_enforced_across_two_limiters_one_redis(redis_client) -> None:
    """Two limiter instances over ONE Redis simulate two prefork processes —
    the cap is GLOBAL, not per-instance."""
    a = DestinationLimiter(redis_client, max_wait_seconds=5.0)
    b = DestinationLimiter(redis_client, max_wait_seconds=5.0)

    current = 0
    max_seen = 0

    async def worker(limiter: DestinationLimiter) -> None:
        nonlocal current, max_seen
        async with limiter.slot("dest-shared", concurrency=1):
            current += 1
            max_seen = max(max_seen, current)
            await asyncio.sleep(0.05)
            current -= 1

    # 3 coroutines on each "process" with a global cap of 1.
    await asyncio.gather(
        *[worker(a) for _ in range(3)], *[worker(b) for _ in range(3)]
    )
    assert max_seen == 1, f"cap global=1 violado entre instâncias: {max_seen}"


@pytest.mark.asyncio
async def test_concurrency_below_one_clamped_to_one(redis_client) -> None:
    """concurrency<1 is clamped to 1 (never a zero/negative ceiling that deadlocks)."""
    limiter = DestinationLimiter(redis_client, max_wait_seconds=2.0)
    async with limiter.slot("dest-clamp", concurrency=0):
        assert await redis_client.zcard("dest_concurrency:dest-clamp") == 1


# ── (2) fail-OPEN on infra fault ─────────────────────────────────────────────


class _BrokenRedis:
    """Redis stand-in that raises on every call (simulates Redis down)."""

    async def eval(self, *_a, **_k):
        raise ConnectionError("redis down")

    async def zrem(self, *_a, **_k):  # pragma: no cover - never reached (held=False)
        raise ConnectionError("redis down")


@pytest.mark.asyncio
async def test_fail_open_when_redis_down() -> None:
    """Redis error on acquire → slot() proceeds WITHOUT a lease (never blocks)."""
    limiter = DestinationLimiter(_BrokenRedis(), max_wait_seconds=5.0)
    entered = False
    async with limiter.slot("dest-down", concurrency=1):
        entered = True
    assert entered, "fail-open: a entrega deve prosseguir mesmo com Redis fora"


@pytest.mark.asyncio
async def test_fail_open_does_not_call_zrem_when_no_lease() -> None:
    """When no lease was held (Redis down) the finally must NOT touch Redis —
    a broken zrem would otherwise mask the original work."""

    class _CountingBroken(_BrokenRedis):
        zrem_calls = 0

        async def zrem(self, *_a, **_k):
            type(self).zrem_calls += 1
            raise ConnectionError("redis down")

    r = _CountingBroken()
    limiter = DestinationLimiter(r, max_wait_seconds=1.0)
    async with limiter.slot("dest-x", concurrency=1):
        pass
    assert r.zrem_calls == 0, "sem lease adquirido, release não deve chamar zrem"


# ── (2b) fail-OPEN on saturation timeout ─────────────────────────────────────


@pytest.mark.asyncio
async def test_fail_open_on_saturation_timeout(redis_client) -> None:
    """Cap saturated past max_wait → fail open (over-limit beats a stuck batch)."""
    # Pre-fill the only slot with a long-lived holder, then a new acquirer with a
    # tiny wait budget must give up and proceed WITHOUT a lease.
    holder = DestinationLimiter(redis_client, max_wait_seconds=5.0)
    waiter = DestinationLimiter(redis_client, max_wait_seconds=0.2)

    proceeded = False

    async def occupy() -> None:
        async with holder.slot("dest-sat", concurrency=1):
            await asyncio.sleep(0.6)

    async def try_enter() -> None:
        nonlocal proceeded
        async with waiter.slot("dest-sat", concurrency=1):
            proceeded = True

    occupier = asyncio.create_task(occupy())
    await asyncio.sleep(0.05)  # let occupier grab the slot
    await asyncio.wait_for(try_enter(), timeout=2.0)
    assert proceeded, "fail-open no timeout: a entrega deve seguir mesmo saturado"
    await occupier


# ── (3) release guaranteed under exception ───────────────────────────────────


@pytest.mark.asyncio
async def test_slot_released_on_exception(redis_client) -> None:
    """An exception inside the slot still releases the lease (try/finally)."""
    limiter = DestinationLimiter(redis_client, max_wait_seconds=5.0)
    key = "dest_concurrency:dest-boom"

    with pytest.raises(RuntimeError):
        async with limiter.slot("dest-boom", concurrency=1):
            assert await redis_client.zcard(key) == 1
            raise RuntimeError("send_batch exploded")

    assert await redis_client.zcard(key) == 0, "lease deve ser liberado mesmo em erro"


@pytest.mark.asyncio
async def test_slot_released_on_cancel(redis_client) -> None:
    """Cancellation/timeout inside the slot still releases the lease."""
    limiter = DestinationLimiter(redis_client, max_wait_seconds=5.0)
    key = "dest_concurrency:dest-cancel"

    async def hold_forever() -> None:
        async with limiter.slot("dest-cancel", concurrency=1):
            await asyncio.sleep(10)

    task = asyncio.create_task(hold_forever())
    await asyncio.sleep(0.05)
    assert await redis_client.zcard(key) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await redis_client.zcard(key) == 0, "lease deve liberar no cancel"


# ── (4) lease auto-expiry of a dead worker's slot ────────────────────────────


@pytest.mark.asyncio
async def test_dead_worker_slot_self_heals(redis_client, monkeypatch) -> None:
    """A slot from a worker that died (never ran ZREM) ages out of the window so
    the cap self-heals — no permanent deadlock."""
    # Stale member as if a now-dead worker registered ~2× the lease ago.
    key = "dest_concurrency:dest-dead"
    stale_ms = int((__import__("time").time() * 1000) - 2 * dl.LEASE_MS)
    await redis_client.zadd(key, {"dead-worker-token": stale_ms})
    assert await redis_client.zcard(key) == 1

    # A fresh acquire on cap=1 must succeed: the Lua ZREMRANGEBYSCORE evicts the
    # expired member before counting, so there is room.
    limiter = DestinationLimiter(redis_client, max_wait_seconds=2.0)
    async with limiter.slot("dest-dead", concurrency=1):
        # The stale token is gone; only our live token remains.
        members = await redis_client.zrange(key, 0, -1)
        assert "dead-worker-token" not in members
        assert len(members) == 1
