"""per-destination bulkhead.

Two mechanisms:
  - hash-routing: each destination_id maps to a STABLE shard queue
    (dispatch.destination.0..N-1) so a slow destination saturates only its
    shard; an operator isolates it with a dedicated per-shard worker.
  - concurrency semaphore: caps concurrent send_batch for ONE destination on
    ONE loop (per-process; see concurrency_pool docstring). Proven here at the
    asyncio level where it is effective.
"""

from __future__ import annotations

import asyncio
import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest

from backend.app.collectors import queues
from backend.app.collectors.output import concurrency_pool


# ── Hash-routing ───────────────────────────────────────────────────────────


def test_shard_queue_is_stable() -> None:
    """Same destination_id always maps to the same shard (ordering/socket reuse)."""
    a = queues.dispatch_dest_shard_queue("splunk-prod-001")
    b = queues.dispatch_dest_shard_queue("splunk-prod-001")
    assert a == b
    assert a.startswith("dispatch.destination.")
    shard = int(a.rsplit(".", 1)[1])
    assert 0 <= shard < queues.DISPATCH_DEST_SHARDS


def test_shard_queue_distributes_across_shards() -> None:
    """A spread of destination ids hits more than one shard (not all in shard 0)."""
    seen = {
        queues.dispatch_dest_shard_queue(f"dest-{i:04d}")
        for i in range(200)
    }
    assert len(seen) >= queues.DISPATCH_DEST_SHARDS // 2
    assert seen <= set(queues.all_dispatch_dest_queues())


def test_all_shard_queues_registered_in_celery() -> None:
    """Every shard queue is declared so the worker can consume it."""
    from backend.app.collectors.celery_app import celery_app

    declared = {q.name for q in celery_app.conf.task_queues}
    for shard_q in queues.all_dispatch_dest_queues():
        assert shard_q in declared, f"{shard_q} not declared in task_queues"


def test_compose_dispatcher_consumes_all_shards() -> None:
    """The collector-dispatcher's -Q list in docker-compose MUST be a superset of
    all shard queues — else routed batches strand in the broker. This
    machine-enforces the constant↔compose contract so a shard-count bump can't
    silently drift."""
    import pathlib

    compose = (
        pathlib.Path(__file__).resolve().parents[4] / "compose" / "docker-compose.yml"
    )
    text = compose.read_text()
    # The real -Q value line (not the explanatory comment, which says "0..7").
    q_lines = [
        ln
        for ln in text.splitlines()
        if "dispatch.destination.0" in ln and not ln.lstrip().startswith("#")
    ]
    assert q_lines, "no dispatcher -Q line containing the destination shards found"
    line = q_lines[0]
    for shard_q in queues.all_dispatch_dest_queues():
        assert shard_q in line, (
            f"{shard_q} not consumed by the dispatcher -Q in docker-compose.yml — "
            f"shard-count drift would strand its batches"
        )


# ── Concurrency semaphore ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_semaphore_caps_concurrency() -> None:
    """concurrency=1 → two coroutines for the same destination serialize."""
    concurrency_pool.reset()
    order: list[str] = []

    async def worker(tag: str) -> None:
        sem = concurrency_pool.get_semaphore("dest-x", concurrency=1)
        async with sem:
            order.append(f"{tag}:enter")
            await asyncio.sleep(0.02)
            order.append(f"{tag}:exit")

    await asyncio.gather(worker("a"), worker("b"))
    # With cap=1 the two must not interleave: each enter is followed by its exit.
    assert order in (
        ["a:enter", "a:exit", "b:enter", "b:exit"],
        ["b:enter", "b:exit", "a:enter", "a:exit"],
    )


@pytest.mark.asyncio
async def test_semaphore_allows_parallel_up_to_limit() -> None:
    """concurrency=2 → two coroutines run concurrently (both enter before exit)."""
    concurrency_pool.reset()
    entered = 0
    max_concurrent = 0

    async def worker() -> None:
        nonlocal entered, max_concurrent
        sem = concurrency_pool.get_semaphore("dest-y", concurrency=2)
        async with sem:
            entered += 1
            max_concurrent = max(max_concurrent, entered)
            await asyncio.sleep(0.02)
            entered -= 1

    await asyncio.gather(*[worker() for _ in range(4)])
    assert max_concurrent == 2


@pytest.mark.asyncio
async def test_different_destinations_do_not_share_semaphore() -> None:
    """A slow destination's saturated semaphore must NOT block another dest."""
    concurrency_pool.reset()
    fast_done = asyncio.Event()

    async def slow() -> None:
        sem = concurrency_pool.get_semaphore("slow", concurrency=1)
        async with sem:
            await asyncio.sleep(0.2)

    async def fast() -> None:
        sem = concurrency_pool.get_semaphore("fast", concurrency=1)
        async with sem:
            fast_done.set()

    task_slow = asyncio.create_task(slow())
    await asyncio.sleep(0.01)  # let slow acquire first
    await asyncio.wait_for(fast(), timeout=0.1)  # fast must not be blocked by slow
    assert fast_done.is_set()
    await task_slow


@pytest.mark.asyncio
async def test_semaphore_resizes_on_concurrency_change() -> None:
    concurrency_pool.reset()
    s1 = concurrency_pool.get_semaphore("d", concurrency=2)
    s2 = concurrency_pool.get_semaphore("d", concurrency=2)
    assert s1 is s2  # same limit → same object
    s3 = concurrency_pool.get_semaphore("d", concurrency=4)
    assert s3 is not s1  # changed limit → fresh semaphore
