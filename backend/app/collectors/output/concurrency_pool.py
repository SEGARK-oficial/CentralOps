"""Per-destination concurrency bulkhead — SECOND layer.

An ``asyncio.Semaphore`` per ``(destination_id, event_loop)``, sized from
``DeliveryConfig.concurrency``, acquired around ``send_batch`` so a single
destination cannot monopolise a loop's in-flight send capacity at the expense
of other destinations sharing that loop.

SCOPE — read carefully (prefork landmine):
  This semaphore is **per event-loop / per worker process**, NOT global. Under
  the Celery prefork pool each task runs ``asyncio.run`` (or submits to the
  per-process persistent loop) and a child process executes ONE task at a time,
  so within a single process the semaphore rarely contends. It becomes
  meaningful when multiple ``send_batch`` coroutines share one loop (the
  ``DISPATCH_PERSISTENT_LOOP`` runtime, future async-executor models, or tests).
  Left alone it would let ``W`` workers run ``W × concurrency`` concurrent sends
  against one sink — which is exactly why it is NOT the only layer.

  The cross-process ceiling is
  ``destination_limiter.DestinationLimiter`` — a Redis leaky-lease that caps
  concurrent ``send_batch`` for one destination across ALL worker processes and
  hosts. ``dispatch_batch_to_destination`` acquires the Redis lease FIRST and
  this semaphore SECOND, so this module is the cheap intra-loop fan-out bound
  underneath the global cap. Defence-in-depth alongside **hash-routing to shard
  queues** (``queues.dispatch_dest_shard_queue``) + per-shard worker deployment
  (OS-level bulkhead) + the **circuit breaker** (a hung destination trips OPEN
  and fast-fails instead of holding worker slots).

  The semaphore is event-loop-bound (``asyncio.Semaphore`` cannot be awaited
  across loops) — keyed by ``(destination_id, id(loop))`` mirroring
  ``destination_cache``'s loop tracking, so a fork/new-loop never reuses a
  stale semaphore.
"""

from __future__ import annotations

import asyncio
from typing import Dict, Tuple

# destination_id → (semaphore, limit, loop). Keyed by destination ALONE and
# SELF-HEALING (mirrors destination_cache): a new running loop (the default
# asyncio.run-per-task model closes its loop each task) or a changed concurrency
# REPLACES the entry, so the pool stays at one entry per destination and never
# accumulates stale closed-loop semaphores. We hold the actual loop object (not
# ``id(loop)``, which can be reused after GC) for an exact identity check.
_pool: Dict[str, Tuple[asyncio.Semaphore, int, asyncio.AbstractEventLoop]] = {}


def get_semaphore(destination_id: str, concurrency: int) -> asyncio.Semaphore:
    """Return the per-destination semaphore for the CURRENT loop, sized to
    ``concurrency``.

    Self-heals: if the cached entry was bound to a different (now-closed) loop,
    or ``concurrency`` changed, a fresh semaphore replaces it. In-flight holders
    keep their own reference and drain on the old object (a brief transient
    over-limit on a rare config bump is acceptable). Must be called from within a
    running event loop.
    """
    if concurrency < 1:
        concurrency = 1
    loop = asyncio.get_running_loop()
    existing = _pool.get(destination_id)
    if existing is not None and existing[1] == concurrency and existing[2] is loop:
        return existing[0]
    sem = asyncio.Semaphore(concurrency)
    _pool[destination_id] = (sem, concurrency, loop)
    return sem


def reset() -> None:
    """Drop all semaphores — test seam (mirrors destination_cache.reset)."""
    _pool.clear()
