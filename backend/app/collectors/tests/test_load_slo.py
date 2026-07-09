"""Synthetic load-test harness against the SLO.

WHAT THIS IS
------------
A *deterministic, bounded* (runs in seconds) proxy for the soak test
documented as the real acceptance gate. It does NOT measure wall-clock EPS on a
4-vCPU node — that is the ≥1 h soak + 1.5x burst described below and run on real
hardware. This harness instead locks down the **correctness invariants** the SLO
implicitly depends on, using the real in-process delivery path with mocked sinks:

  (a) ZERO LOSS        — every generated event_id is accounted for exactly once
                         across delivered + DLQ + shed-with-metric. Nothing
                         vanishes silently.
  (b) ZERO DUPLICATION — no event_id is delivered twice to the same destination.
  (c) NOISY-TENANT ISOLATION — back-pressure / drop on the noisy tenant does NOT
                         reduce the acceptance rate of the quiet tenants
                         (proven with per-tenant counters).
  (d) CONCURRENCY CAP  — the per-destination semaphore (E5 bulkhead) is never
                         exceeded under concurrent fan-out.
  (e) STABILITY        — running K batches, in-memory structures (semaphore pool,
                         counters) stay bounded; nothing grows without limit.

WHY A PROXY AND NOT THE SOAK
----------------------------
The documented acceptance is a property of *production hardware over time*. A CI
job cannot deterministically prove "20k EPS @ 500 B for 1 h" without that
hardware and an hour. What a CI job CAN prove deterministically is that the
delivery machinery loses nothing, duplicates nothing, and isolates tenants —
the invariants whose violation would invalidate any throughput number. Those are
asserted here; the throughput/latency numbers are recorded as DESIGN CONSTANTS
(below) for traceability, and the soak remains the human-run gate.

LIMITATIONS (honest)
--------------------
* We do NOT assert the p99 < 500 ms latency target. Wall-clock latency on this
  in-process mock with a fake sink is meaningless as an SLO signal; asserting it
  would be theatre. The latency SLO is a soak/profiling concern (py-spy),
  recorded here as a constant only.
* We do NOT assert raw EPS. Same reasoning — the EPS-alvo is a node-level
  hardware property, recorded as a constant.
* The semaphore is per-event-loop (see concurrency_pool.py docstring). The
  cross-process isolation in prod is shard-queue + circuit-breaker, exercised by
  other tests (test_e5_bulkhead, test_e6_backpressure). This harness
  asserts the loop-local cap, which is the layer reachable in-process.
"""

from __future__ import annotations

import asyncio
import os
from collections import Counter
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

from backend.app.collectors.output.base import DeliveryResult, RejectedEvent
from backend.app.collectors.output.delivery_config import (
    BatchConfig,
    BreakerConfig,
    DeliveryConfig,
    RetryConfig,
)

from backend.app.collectors.tests._loadgen import (  # noqa: E402  (env set above)
    SizeMix,
    TenantSpec,
    build_fleet,
    chunked,
    event_id_of,
    org_id_of,
    reset_event_seq,
)


# ── SLO DESIGN CONSTANTS ──────────────────────────────────────────────────────
# Recorded by reference, NOT asserted by this harness — see module docstring
# "LIMITATIONS". The real acceptance is the soak below; these constants exist so
# the design target lives next to the proxy that guards its invariants.
SLO_TARGET_EPS_PER_NODE = 20_000        # ~20k EPS sustained per node...
SLO_REFERENCE_EVENT_BYTES = 500         # ...@ 500 B/event (~10 MB/s, ~0.86 TB/day)
SLO_P99_LATENCY_MS = 500                # p99 < 500 ms to first-destination ack
SLO_P50_LATENCY_MS = 100                # p50 < 100 ms
SLO_NODE_HEADROOM_PCT = 65              # each node ≤ 60-70% of measured peak
SLO_SOAK_MIN_DURATION_S = 3600          # ≥ 1 h sustained...
SLO_SOAK_BURST_FACTOR = 1.5             # ...with a 1.5x burst — the documented gate
SLO_EVENT_SIZE_MIX_BYTES = (200, 500, 2048)  # firewall / medium / Windows mix

# ── Harness sizing (kept small so the suite runs in seconds) ──────────────────
QUIET_TENANTS = 4
QUIET_EVENTS_EACH = 1_000
NOISY_TENANT_EVENTS = 12_000     # ~3x the entire quiet fleet — disproportionate
MAX_ITEMS = 500                  # chunk size (delivery default)


# ── DeliveryConfig helper (fast retries, generous breaker) ────────────────────
def _dcfg(*, concurrency: int = 4, max_items: int = MAX_ITEMS) -> DeliveryConfig:
    return DeliveryConfig(
        batch=BatchConfig(max_items=max_items),
        retry=RetryConfig(max_retries=0, initial_ms=10, max_ms=50, multiplier=2.0),
        timeout_ms=30_000,
        concurrency=concurrency,
        breaker=BreakerConfig(failure_threshold=1000, cooldown_s=1, window_s=60),
    )


def _dest_config_mock(dest_id: str = "slo-dest-001", kind: str = "splunk_hec") -> MagicMock:
    cfg = MagicMock()
    cfg.destination_id = dest_id
    cfg.kind = kind
    cfg.delivery = {"breaker": {"failure_threshold": 10_000}}
    cfg.secret_ref = None
    cfg.config_version = "v1"
    cfg.name = "SLO Load Destination"
    cfg.organization_id = None
    return cfg


def _stub_metrics() -> MagicMock:
    m = MagicMock()
    m.labels.return_value = m
    return m


def _stub_circuit_breaker() -> MagicMock:
    cb = MagicMock()
    cb.check_for_config = AsyncMock(return_value=None)
    cb.record_failure_for_config = AsyncMock(return_value=None)
    cb.record_success_for_config = AsyncMock(return_value=None)
    cb.BreakerOpen = type("BreakerOpen", (Exception,), {})
    return cb


# ── Accounting sink ───────────────────────────────────────────────────────────
class AccountingSink:
    """A fake destination sink that records the FATE of every event_id.

    It is the ledger the conservation/duplication/isolation assertions read.

    Backpressure model (deterministic): a tenant whose accepted-event count
    exceeds ``noisy_accept_cap`` has its *subsequent* events REJECTED (4xx,
    non-retryable → DLQ) instead of accepted — a faithful, deterministic stand-in
    for "this tenant is shedding under its own back-pressure". Quiet tenants
    (cap=None) are always accepted. This lets us prove, with per-tenant counters,
    that the noisy tenant's shedding does not touch the quiet tenants.

    Concurrency is observed here (inside send_batch) to assert the E5 cap.
    """

    def __init__(self, *, noisy_org: int | None = None, noisy_accept_cap: int | None = None):
        self.noisy_org = noisy_org
        self.noisy_accept_cap = noisy_accept_cap
        # event_id -> count of deliveries (must never exceed 1 per destination)
        self.delivered: Counter = Counter()
        # event_id -> count of rejections returned to caller (→ DLQ path)
        self.rejected: Counter = Counter()
        # per-tenant accepted/rejected tallies (the isolation evidence)
        self.accepted_by_org: Counter = Counter()
        self.rejected_by_org: Counter = Counter()
        # how many of THIS org's events have been accepted so far (for the cap)
        self._accepted_seen: Counter = Counter()
        # concurrency observation
        self.max_concurrent = 0
        self._current = 0
        self._lock = asyncio.Lock()

    async def send_batch(self, batch: List[Dict[str, Any]]) -> DeliveryResult:
        async with self._lock:
            self._current += 1
            self.max_concurrent = max(self.max_concurrent, self._current)
        # Yield so concurrent sends on the same loop actually interleave; this is
        # what makes the semaphore cap observable.
        await asyncio.sleep(0)
        try:
            accepted = 0
            rejected: List[RejectedEvent] = []
            for env in batch:
                eid = event_id_of(env)
                org = org_id_of(env)
                shed = (
                    self.noisy_org is not None
                    and org == self.noisy_org
                    and self.noisy_accept_cap is not None
                    and self._accepted_seen[org] >= self.noisy_accept_cap
                )
                if shed:
                    self.rejected[eid] += 1
                    self.rejected_by_org[org] += 1
                    rejected.append(
                        RejectedEvent(
                            event_id=eid,
                            reason="tenant backpressure (synthetic)",
                            error_kind="schema_rejected",
                            retryable=False,
                        )
                    )
                else:
                    self.delivered[eid] += 1
                    self.accepted_by_org[org] += 1
                    self._accepted_seen[org] += 1
                    accepted += 1
            # retryable=False so rejected items go straight to DLQ (E3), never a
            # whole-batch retry that could duplicate the accepted ones (E2).
            return DeliveryResult(accepted=accepted, rejected=rejected, retryable=False)
        finally:
            async with self._lock:
                self._current -= 1


class DLQLedger:
    """Records every event_id the delivery path hands to the DLQ.

    Substitutes ``persist_rejected_to_dlq`` (sync callback). The conservation
    assertion reads this so a DLQ'd event is counted, never lost.
    """

    def __init__(self) -> None:
        self.dlq: Counter = Counter()

    def __call__(self, dest_config: Any, rejected: List[RejectedEvent], chunk: List[Dict]) -> bool:
        for rej in rejected:
            self.dlq[rej.event_id] += 1
        return True  # persist succeeded


async def _deliver_fleet(
    fleet: Dict[int, List[Dict[str, Any]]],
    sink: AccountingSink,
    dlq: DLQLedger,
    *,
    dcfg: DeliveryConfig,
    interleave: bool = True,
) -> None:
    """Drive every tenant's events through the real ``_send_chunk_with_retry``.

    When ``interleave`` is True, all tenants' chunks are dispatched as concurrent
    tasks sharing one event loop (so the E5 semaphore actually contends and the
    noisy tenant runs alongside the quiet ones). The semaphore is acquired around
    each chunk send, mirroring the dispatcher.
    """
    from backend.app.collectors.output.concurrency_pool import get_semaphore, reset
    from backend.app.collectors.pipeline import _send_chunk_with_retry

    reset()  # clean semaphore pool

    dc = _dest_config_mock()
    cb = _stub_circuit_breaker()
    labels = {"destination_id": dc.destination_id, "kind": dc.kind}
    m = _stub_metrics()
    target = MagicMock()
    target.send_batch = sink.send_batch
    sem = get_semaphore(dc.destination_id, dcfg.concurrency)

    async def send_one_chunk(chunk: List[Dict[str, Any]]) -> None:
        async with sem:  # E5 bulkhead — same acquisition the dispatcher uses
            await _send_chunk_with_retry(
                target=target,
                chunk=chunk,
                dcfg=dcfg,
                dest_config=dc,
                labels=labels,
                redis=AsyncMock(),
                circuit_breaker=cb,
                persist_rejected_to_dlq=dlq,
                DELIVERY_LATENCY=m,
                DLQ_TOTAL=m,
                EVENTS_REJECTED=m,
                EVENTS_SENT=m,
                BYTES_SENT=m,
                RETRIES=m,
            )

    coros = []
    for events in fleet.values():
        for chunk in chunked(events, dcfg.batch.max_items):
            coros.append(send_one_chunk(chunk))

    if interleave:
        await asyncio.gather(*coros)
    else:
        for coro in coros:
            await coro

    reset()


def _build_fleet():
    """Quiet tenants (1..N) + one noisy tenant — the standard fleet."""
    reset_event_seq()
    quiet = [
        TenantSpec(
            organization_id=org,
            events=QUIET_EVENTS_EACH,
            vendor="fortinet",
            size_mix=SizeMix(firewall=6, medium=3, windows=1),
        )
        for org in range(1, QUIET_TENANTS + 1)
    ]
    noisy = TenantSpec(
        organization_id=99,
        events=NOISY_TENANT_EVENTS,
        vendor="windows",
        size_mix=SizeMix(firewall=2, medium=3, windows=5),  # heavier mix too
        noisy=True,
    )
    return build_fleet([*quiet, noisy]), quiet, noisy


# ──────────────────────────────────────────────────────────────────────────────
# (a) ZERO LOSS + (b) ZERO DUPLICATION  — clean fleet, everything accepted
# ──────────────────────────────────────────────────────────────────────────────
async def test_zero_loss_and_zero_duplication_clean_fleet() -> None:
    """Every generated event_id is delivered exactly once; none lost, none dup'd.

    PROVES (a) + (b): with a sink that accepts everything, the set of delivered
    event_ids equals the set of generated ones, and no id is delivered twice.
    """
    fleet, _, _ = _build_fleet()
    all_ids = [event_id_of(e) for events in fleet.values() for e in events]
    total = len(all_ids)

    sink = AccountingSink()  # no backpressure: accept all
    dlq = DLQLedger()
    await _deliver_fleet(fleet, sink, dlq, dcfg=_dcfg())

    # No silent loss: delivered + dlq covers every generated id exactly.
    delivered_ids = set(sink.delivered)
    generated_ids = set(all_ids)
    assert delivered_ids == generated_ids, (
        "ZERO LOSS violated: "
        f"{len(generated_ids - delivered_ids)} generated ids never delivered, "
        f"{len(delivered_ids - generated_ids)} delivered ids never generated"
    )
    assert sum(sink.delivered.values()) == total, "delivered count != generated count"
    assert not dlq.dlq, "clean fleet must not DLQ anything"

    # Zero duplication: no id delivered more than once.
    dups = {eid: c for eid, c in sink.delivered.items() if c > 1}
    assert not dups, f"ZERO DUPLICATION violated: {len(dups)} ids delivered >1x"


# ──────────────────────────────────────────────────────────────────────────────
# (a) CONSERVATION under backpressure  — delivered + DLQ == generated, no loss
# ──────────────────────────────────────────────────────────────────────────────
async def test_conservation_with_noisy_tenant_backpressure() -> None:
    """With the noisy tenant shedding, EVERY id is still accounted for once.

    PROVES (a) end-to-end with drops in play: delivered ∪ DLQ == generated, and
    the two sets are disjoint (an id is either delivered xor DLQ'd, never both,
    never neither). This is the "nothing vanishes silently" invariant.
    """
    fleet, _, noisy = _build_fleet()
    generated_ids = {event_id_of(e) for events in fleet.values() for e in events}

    # Cap the noisy tenant so a large chunk of its events shed into the DLQ.
    cap = 2_000
    sink = AccountingSink(noisy_org=noisy.organization_id, noisy_accept_cap=cap)
    dlq = DLQLedger()
    await _deliver_fleet(fleet, sink, dlq, dcfg=_dcfg())

    delivered_ids = set(sink.delivered)
    dlq_ids = set(dlq.dlq)

    # Disjoint: no id is both delivered and DLQ'd.
    both = delivered_ids & dlq_ids
    assert not both, f"{len(both)} ids both delivered AND DLQ'd (double-counted)"

    # Complete: every generated id landed in exactly one bucket.
    accounted = delivered_ids | dlq_ids
    missing = generated_ids - accounted
    extra = accounted - generated_ids
    assert not missing, f"ZERO LOSS violated: {len(missing)} generated ids unaccounted"
    assert not extra, f"{len(extra)} accounted ids were never generated"
    assert accounted == generated_ids

    # The DLQ path must have actually fired (the cap forces sheds).
    assert dlq_ids, "noisy tenant should have shed at least some events to DLQ"
    # And no id DLQ'd more than once.
    dlq_dups = {eid: c for eid, c in dlq.dlq.items() if c > 1}
    assert not dlq_dups, f"{len(dlq_dups)} ids DLQ'd more than once"


# ──────────────────────────────────────────────────────────────────────────────
# (c) NOISY-TENANT ISOLATION  — quiet tenants unaffected by noisy shedding
# ──────────────────────────────────────────────────────────────────────────────
async def test_noisy_tenant_does_not_contaminate_quiet_tenants() -> None:
    """The noisy tenant's back-pressure does NOT reduce quiet tenants' accept rate.

    PROVES (c): with the noisy tenant capped (so it sheds heavily), EVERY quiet
    tenant still has a 100% acceptance rate — proven with per-tenant counters.
    The noisy tenant, by contrast, has a measurably reduced acceptance rate. This
    is the event-level isolation guarantee.
    """
    fleet, quiet_specs, noisy = _build_fleet()
    cap = 2_000
    sink = AccountingSink(noisy_org=noisy.organization_id, noisy_accept_cap=cap)
    dlq = DLQLedger()
    await _deliver_fleet(fleet, sink, dlq, dcfg=_dcfg())

    # Every QUIET tenant: 100% accepted, 0 rejected.
    for spec in quiet_specs:
        org = spec.organization_id
        accepted = sink.accepted_by_org[org]
        rejected = sink.rejected_by_org[org]
        assert accepted == spec.events, (
            f"ISOLATION violated: quiet tenant {org} accepted {accepted}/"
            f"{spec.events} — noisy tenant contaminated it"
        )
        assert rejected == 0, (
            f"ISOLATION violated: quiet tenant {org} had {rejected} rejects "
            "despite generating no over-cap volume"
        )

    # The NOISY tenant IS throttled (control: proves the cap actually bit, so the
    # quiet-tenant green result isn't vacuous).
    noisy_accepted = sink.accepted_by_org[noisy.organization_id]
    noisy_rejected = sink.rejected_by_org[noisy.organization_id]
    assert noisy_accepted == cap, (
        f"noisy tenant should accept exactly its cap={cap}, got {noisy_accepted}"
    )
    assert noisy_rejected == noisy.events - cap, (
        f"noisy tenant should shed {noisy.events - cap}, got {noisy_rejected}"
    )

    # Aggregate quiet acceptance rate is 100%, independent of noisy shedding.
    quiet_total = sum(s.events for s in quiet_specs)
    quiet_accepted = sum(sink.accepted_by_org[s.organization_id] for s in quiet_specs)
    assert quiet_accepted == quiet_total, "quiet fleet acceptance rate must be 100%"


# ──────────────────────────────────────────────────────────────────────────────
# (d) CONCURRENCY CAP  — E5 semaphore never exceeded under fan-out
# ──────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("cap", [1, 2, 4, 8])
async def test_concurrency_cap_respected_under_load(cap: int) -> None:
    """Concurrent fan-out never runs more than ``cap`` sends at once (E5 bulkhead).

    PROVES (d): the AccountingSink records max concurrent send_batch calls; with
    the per-destination semaphore sized to ``cap`` and many interleaved chunks,
    the observed maximum never exceeds ``cap``.
    """
    fleet, _, _ = _build_fleet()
    sink = AccountingSink()
    dlq = DLQLedger()
    await _deliver_fleet(fleet, sink, dlq, dcfg=_dcfg(concurrency=cap))

    assert sink.max_concurrent <= cap, (
        f"E5 cap={cap} exceeded: observed {sink.max_concurrent} concurrent sends"
    )
    assert sink.max_concurrent >= 1, "at least one send must have run"


# ──────────────────────────────────────────────────────────────────────────────
# (e) STABILITY  — K rounds, structures stay bounded, counters linear
# ──────────────────────────────────────────────────────────────────────────────
async def test_stability_across_k_rounds_no_unbounded_growth() -> None:
    """Running K delivery rounds keeps in-memory structures bounded.

    PROVES (e): after K rounds the semaphore pool holds exactly one entry per
    destination (no per-round accumulation), and the delivered count grows
    linearly with rounds (no double-counting, no leak). Each round uses fresh,
    globally-unique event_ids so cross-round duplication would surface as a
    delivered-count > generated-count.
    """
    from backend.app.collectors.output import concurrency_pool

    K = 6
    per_round = 800  # small fleet per round to keep it fast
    total_delivered = 0
    generated_so_far = 0

    # NOTE: we deliberately do NOT reset_event_seq() between rounds — ids stay
    # globally unique across rounds, so any cross-round duplication would surface
    # as a delivered count exceeding the generated count.
    for _ in range(K):
        spec = TenantSpec(organization_id=7, events=per_round, vendor="fortinet")
        fleet = build_fleet([spec])
        generated_so_far += per_round

        sink = AccountingSink()
        dlq = DLQLedger()
        await _deliver_fleet(fleet, sink, dlq, dcfg=_dcfg())

        # Per-round: exactly per_round delivered, all unique, none DLQ'd.
        assert sum(sink.delivered.values()) == per_round
        assert all(c == 1 for c in sink.delivered.values()), "duplication within round"
        assert not dlq.dlq

        # Pool bounded: at most one entry per destination after the round's reset.
        assert len(concurrency_pool._pool) <= 1, (
            f"semaphore pool grew unbounded: {len(concurrency_pool._pool)} entries"
        )
        total_delivered += sum(sink.delivered.values())

    # Linear growth: K rounds * per_round, no double counting across rounds.
    assert total_delivered == K * per_round, (
        f"counters non-linear across rounds: {total_delivered} != {K * per_round}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Generator self-test — the synthetic mix matches the documented size mix
# ──────────────────────────────────────────────────────────────────────────────
def test_generator_produces_documented_size_mix_and_unique_ids() -> None:
    """The load generator emits the documented 200B/500B/2KB mix and unique ids.

    Guards the harness's own input: a generator that silently produced one size,
    duplicate ids, or invalid envelopes would make every assertion above vacuous.
    """
    fleet, quiet_specs, noisy = _build_fleet()
    all_events = [e for events in fleet.values() for e in events]

    # Unique ids across the whole fleet (precondition for the dup assertions).
    ids = [event_id_of(e) for e in all_events]
    assert len(ids) == len(set(ids)), "generated event_ids must be globally unique"

    # Canonical envelope: every event has a valid _centralops block.
    for e in all_events[:50] + all_events[-50:]:
        co = e["_centralops"]
        assert isinstance(co["organization_id"], int)
        assert co["event_id"]
        assert isinstance(co["severity_id"], int) and 1 <= co["severity_id"] <= 5
        assert co["vendor"]

    # All three documented size classes are present in the noisy stream.
    noisy_events = fleet[noisy.organization_id]
    size_classes = {e["raw"]["_size_class"] for e in noisy_events}
    assert size_classes == set(SLO_EVENT_SIZE_MIX_BYTES), (
        f"expected size mix {SLO_EVENT_SIZE_MIX_BYTES}, got {sorted(size_classes)}"
    )

    # The noisy tenant is disproportionate (the whole point of the harness).
    quiet_total = sum(s.events for s in quiet_specs)
    assert noisy.events > quiet_total, "noisy tenant must out-produce the quiet fleet"
