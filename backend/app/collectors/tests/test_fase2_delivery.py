"""Comprehensive delivery layer tests.

Covers:
  CHUNK A — secret resolution, TransientDeliveryError in _RETRYABLE.
  CHUNK B — routed fan-out (_enqueue_dispatch via a route → dest;
            wazuh-default is a normal destination in the uniform fan-out).
  CHUNK C — partial-batch DLQ (E1 idempotent, E2 partial, E3 schema_rejected).
  CHUNK D — circuit breaker (open/close/half-open, BreakerOpen NOT in _RETRYABLE).
  E5 — chaos isolation (two destinations, A timeouts, B delivers).

Fixture strategy: shared StaticPool engine so dispatcher's SessionLocal sees
seeded rows (avoids the StaticPool gotcha).
Celery is exercised via apply_async mocks (no broker needed).
Circuit breaker uses fakeredis.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.collectors.output.base import DeliveryResult, RejectedEvent
from backend.app.collectors.output.destinations.registry import DestinationConfig
from backend.app.db.database import Base
from backend.app.db import models


# ── Shared StaticPool DB fixture ──────────────────────────────────────────────


@pytest.fixture()
def static_db():
    """In-memory SQLite with StaticPool so all callers share the same DB.

    Returns (TestingSessionLocal, engine).  Patches database.SessionLocal so
    delivery.py and pipeline.py see the seeded rows.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(
        autocommit=False, autoflush=False, bind=engine
    )
    Base.metadata.create_all(bind=engine)

    import backend.app.db.database as db_module

    original = db_module.SessionLocal
    db_module.SessionLocal = TestingSessionLocal
    yield TestingSessionLocal, engine
    db_module.SessionLocal = original
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def seeded_destination(static_db):
    """Seed one enabled splunk_hec destination and return its id."""
    TestingSessionLocal, _ = static_db
    dest_id = "splunk-test-001"
    with TestingSessionLocal() as session:
        row = models.Destination(
            id=dest_id,
            name="Splunk Test",
            kind="splunk_hec",
            enabled=True,
            config='{"url": "https://splunk:8088", "sourcetype": "centralops"}',
            secret_ref="hec_token_ref",
            delivery="{}",
            config_version="v1",
            organization_id=None,
        )
        session.add(row)
        session.commit()
    return dest_id


@pytest.fixture()
def seeded_org_destination(static_db):
    """Seed one enabled destination scoped to org_id=42."""
    TestingSessionLocal, _ = static_db
    dest_id = "splunk-org42-001"
    with TestingSessionLocal() as session:
        row = models.Destination(
            id=dest_id,
            name="Splunk Org42",
            kind="splunk_hec",
            enabled=True,
            config='{"url": "https://splunk:8088", "sourcetype": "centralops"}',
            secret_ref=None,
            delivery="{}",
            config_version="v1",
            organization_id=42,
        )
        session.add(row)
        session.commit()
    return dest_id


def _make_envelope(event_id: str = "evt-001", org_id: int | None = 1) -> dict:
    return {
        "_centralops": {
            "event_id": event_id,
            "organization_id": org_id,
            "vendor": "sophos",
        },
        "normalized": {},
        "raw": {},
    }


def _seed_route(SessionLocal, **kw) -> None:
    """Seed a Route via the production repository (a destination
    only receives events when a route points at it). A ``condition={}`` route is a
    broadcast (matches everything)."""
    from backend.app.db import repository

    with SessionLocal() as s:
        repository.RouteRepository(s).add(**kw)


# ── CHUNK A: TransientDeliveryError in _RETRYABLE ─────────────────────────────


def test_transient_delivery_error_in_retryable() -> None:
    """TransientDeliveryError must be in tasks._RETRYABLE (CHUNK A)."""
    from backend.app.collectors.delivery import TransientDeliveryError
    from backend.app.collectors.tasks import _RETRYABLE

    assert TransientDeliveryError in _RETRYABLE


def test_transient_delivery_error_carries_destination_id() -> None:
    from backend.app.collectors.delivery import TransientDeliveryError

    exc = TransientDeliveryError("dest-xyz")
    assert exc.destination_id == "dest-xyz"
    assert "dest-xyz" in str(exc)


# ── CHUNK A: secret resolution in dispatch_batch_to_destination ───────────────


@pytest.mark.asyncio
async def test_secret_resolution_calls_get_default_backend(
    static_db, seeded_destination
) -> None:
    """When dest has secret_ref, get_default_backend is called and passed to
    get_destination factory (CHUNK A).

    Late imports inside dispatch_batch_to_destination are patched at their
    source modules (not at pipeline), since they are imported via
    ``from module import name`` inside the function body.
    """
    dest_id = seeded_destination

    fake_backend = MagicMock()
    fake_backend.decrypt.return_value = "tok-secret"

    fake_target = AsyncMock()
    fake_target.send_batch.return_value = DeliveryResult(accepted=1)

    import fakeredis.aioredis as fakeredis_aio

    fake_redis = fakeredis_aio.FakeRedis(decode_responses=True)

    # Patch at source modules because dispatch_batch_to_destination uses late imports.
    with (
        patch(
            "backend.app.core.secrets.get_default_backend",
            return_value=fake_backend,
        ),
        patch(
            "backend.app.collectors.output.destination_cache.get_destination",
            new_callable=AsyncMock,
            return_value=fake_target,
        ) as mock_get_dest,
        patch(
            "backend.app.collectors.celery_app.get_worker_redis",
            return_value=fake_redis,
        ),
    ):
        from backend.app.collectors.pipeline import dispatch_batch_to_destination

        batch = [_make_envelope("evt-1")]
        await dispatch_batch_to_destination(dest_id, batch)

    # get_destination was called with a secrets backend (not None).
    call_args = mock_get_dest.call_args
    _, secrets_arg = call_args.args
    assert secrets_arg is fake_backend


@pytest.mark.asyncio
async def test_transient_delivery_error_raised_on_retryable(
    static_db, seeded_destination
) -> None:
    """result.retryable=True → TransientDeliveryError raised (CHUNK A)."""
    dest_id = seeded_destination

    fake_backend = MagicMock()
    fake_target = AsyncMock()
    fake_target.send_batch.return_value = DeliveryResult(
        accepted=0, rejected=[], retryable=True
    )

    import fakeredis.aioredis as fakeredis_aio

    fake_redis = fakeredis_aio.FakeRedis(decode_responses=True)

    from backend.app.collectors.delivery import TransientDeliveryError

    with (
        patch(
            "backend.app.core.secrets.get_default_backend",
            return_value=fake_backend,
        ),
        patch(
            "backend.app.collectors.output.destination_cache.get_destination",
            new_callable=AsyncMock,
            return_value=fake_target,
        ),
        patch(
            "backend.app.collectors.celery_app.get_worker_redis",
            return_value=fake_redis,
        ),
    ):
        from backend.app.collectors.pipeline import dispatch_batch_to_destination

        with pytest.raises(TransientDeliveryError) as exc_info:
            await dispatch_batch_to_destination(dest_id, [_make_envelope()])

    assert exc_info.value.destination_id == dest_id


@pytest.mark.asyncio
async def test_dispatch_records_audit_ring_on_success(
    static_db, seeded_destination
) -> None:
    """A successful dispatch records the batch into the per-org audit
    ring so the /config 'Auditoria' panel can show the wire payloads. The write-path
    was lost in the data-plane split (record_batch existed but was no longer
    called); this asserts dispatch_batch_to_destination re-wires it. Best-effort, so a
    delivery with accepted>0 must leave exactly the dispatched events in the ring."""
    dest_id = seeded_destination

    fake_backend = MagicMock()
    fake_target = AsyncMock()
    fake_target.send_batch.return_value = DeliveryResult(accepted=1)

    import fakeredis.aioredis as fakeredis_aio

    fake_redis = fakeredis_aio.FakeRedis(decode_responses=True)

    with (
        patch(
            "backend.app.core.secrets.get_default_backend",
            return_value=fake_backend,
        ),
        patch(
            "backend.app.collectors.output.destination_cache.get_destination",
            new_callable=AsyncMock,
            return_value=fake_target,
        ),
        patch(
            "backend.app.collectors.celery_app.get_worker_redis",
            return_value=fake_redis,
        ),
    ):
        from backend.app.collectors import audit_buffer
        from backend.app.collectors.pipeline import dispatch_batch_to_destination

        # org_id=1 (the _make_envelope default) matches the seeded destination scope.
        batch = [_make_envelope("evt-audit-1")]
        await dispatch_batch_to_destination(dest_id, batch)

        recent = await audit_buffer.read_recent(fake_redis, 1)

    assert len(recent) == 1
    assert recent[0]["event"]["_centralops"]["event_id"] == "evt-audit-1"


# ── CHUNK B: routed fan-out in _enqueue_dispatch (multi-destino GA) ────────────


def test_wazuh_plus_destinations_enqueued(static_db, seeded_destination) -> None:
    """Both the broadcast destination AND
    wazuh-default flow through the SAME uniform ``dispatch_to_destination`` fan-out
    (default EVENT_DATAPLANE=celery). wazuh-default no longer has a dedicated lane —
    the routing fan-out routes EVERY destination through dispatch_to_destination."""
    SessionLocal, _ = static_db
    from backend.app.collectors import pipeline

    # A broadcast clone+continue route (condition={}, is_final=False) makes the
    # dest receive events; an explicit catch-all to wazuh-default keeps the Wazuh
    # lane fed (the additive "wazuh + dest" shape now expressed as two routes).
    _seed_route(
        SessionLocal,
        name="broadcast",
        condition={},
        destination_ids=[seeded_destination],
        is_final=False,
        priority=10,
        organization_id=None,
    )
    _seed_route(
        SessionLocal,
        name="catch-all-wazuh",
        condition={},
        destination_ids=["wazuh-default"],
        is_final=True,
        priority=100,
        organization_id=None,
    )

    batch = [_make_envelope(org_id=None)]  # global destination matches

    with patch(
        "backend.app.collectors.tasks.dispatch_to_destination.apply_async"
    ) as mock_dest:
        pipeline._enqueue_dispatch(batch)

    # Uniform path: there is no dedicated Wazuh lane anymore — both destinations
    # (splunk + wazuh-default) go via the same dispatch_to_destination fan-out.
    assert mock_dest.call_count == 2

    from backend.app.collectors.queues import dispatch_dest_shard_queue

    by_dest = {
        c.kwargs["kwargs"]["destination_id"]: c.kwargs
        for c in mock_dest.call_args_list
    }
    assert set(by_dest) == {seeded_destination, "wazuh-default"}

    # E5 bulkhead: each destination routed to its own stable shard queue.
    splunk_kwargs = by_dest[seeded_destination]
    assert splunk_kwargs["queue"] == dispatch_dest_shard_queue(seeded_destination)
    assert splunk_kwargs["queue"].startswith("dispatch.destination.")

    wazuh_kwargs = by_dest["wazuh-default"]
    assert wazuh_kwargs["queue"] == dispatch_dest_shard_queue("wazuh-default")
    assert wazuh_kwargs["queue"].startswith("dispatch.destination.")


def test_wazuh_default_in_uniform_fan_out(static_db) -> None:
    """wazuh-default is now a NORMAL destination.
    When a route targets it, it flows through the uniform sharded
    ``dispatch_to_destination`` fan-out (default EVENT_DATAPLANE=celery) — there is
    no longer any dedicated Wazuh delivery lane; every destination is uniform."""
    TestingSessionLocal, _ = static_db
    with TestingSessionLocal() as session:
        row = models.Destination(
            id="wazuh-default",
            name="Wazuh Default",
            kind="wazuh_syslog",
            enabled=True,
            config="{}",
            delivery="{}",
            config_version="v0",
            organization_id=None,
        )
        session.add(row)
        session.commit()

    # A route that explicitly names wazuh-default as a destination is now served
    # by the uniform sharded destination lane like any other destination.
    _seed_route(
        TestingSessionLocal,
        name="broadcast-wazuh",
        condition={},
        destination_ids=["wazuh-default"],
        is_final=True,
        priority=100,
        organization_id=None,
    )

    from backend.app.collectors import pipeline

    batch = [_make_envelope()]

    with patch(
        "backend.app.collectors.tasks.dispatch_to_destination.apply_async"
    ) as mock_dest:
        pipeline._enqueue_dispatch(batch)

    # wazuh-default IS now in the uniform dispatch_to_destination fan-out, on its
    # own stable shard queue (no dedicated Wazuh lane exists).
    from backend.app.collectors.queues import dispatch_dest_shard_queue

    dest_ids = [c.kwargs["kwargs"]["destination_id"] for c in mock_dest.call_args_list]
    assert "wazuh-default" in dest_ids, (
        "wazuh-default must now flow through the uniform multi-dest fan-out"
    )
    wazuh_call = next(
        c for c in mock_dest.call_args_list
        if c.kwargs["kwargs"]["destination_id"] == "wazuh-default"
    )
    assert wazuh_call.kwargs["queue"] == dispatch_dest_shard_queue("wazuh-default")
    assert wazuh_call.kwargs["queue"].startswith("dispatch.destination.")


def test_org_scoped_destination_matches(
    static_db, seeded_org_destination
) -> None:
    """A route scoped to org_id=42 (→ org-42 destination) fires only for org-42
    batches; an org-99 batch sees no org-42 route and falls through to wazuh-default."""
    SessionLocal, _ = static_db
    from backend.app.collectors import pipeline

    # Org-scoped broadcast route: only org-42 batches load it (routes are loaded
    # per-org), so the org-42 destination is reachable only from org-42 traffic.
    _seed_route(
        SessionLocal,
        name="org42-broadcast",
        condition={},
        destination_ids=[seeded_org_destination],
        is_final=False,
        priority=100,
        organization_id=42,
    )

    batch_org42 = [_make_envelope(org_id=42)]
    batch_other = [_make_envelope(org_id=99)]

    with patch(
        "backend.app.collectors.tasks.dispatch_to_destination.apply_async"
    ) as mock_dest:
        pipeline._enqueue_dispatch(batch_org42)
    assert mock_dest.call_count == 1

    with patch(
        "backend.app.collectors.tasks.dispatch_to_destination.apply_async"
    ) as mock_dest2:
        pipeline._enqueue_dispatch(batch_other)
    assert mock_dest2.call_count == 0


# ── CHUNK C (E2): partial-batch → exactly 1 DLQ row, accepted not re-queued ──


@pytest.mark.asyncio
async def test_dispatch_partial_batch_e2(static_db, seeded_destination) -> None:
    """2 accepted + 1 rejected → exactly 1 DLQ row; accepted NOT re-dispatched."""
    TestingSessionLocal, _ = static_db
    dest_id = seeded_destination

    rejected_item = RejectedEvent(
        event_id="evt-rej-001",
        reason="payload too large",
        error_kind="payload_too_large",
        retryable=False,
    )
    fake_result = DeliveryResult(
        accepted=2,
        rejected=[rejected_item],
        retryable=False,
    )

    fake_target = AsyncMock()
    fake_target.send_batch.return_value = fake_result

    import fakeredis.aioredis as fakeredis_aio

    fake_redis = fakeredis_aio.FakeRedis(decode_responses=True)

    batch = [
        _make_envelope("evt-ok-001"),
        _make_envelope("evt-ok-002"),
        _make_envelope("evt-rej-001"),
    ]

    with (
        patch("backend.app.core.secrets.get_default_backend", return_value=None),
        patch(
            "backend.app.collectors.output.destination_cache.get_destination",
            new_callable=AsyncMock,
            return_value=fake_target,
        ),
        patch(
            "backend.app.collectors.celery_app.get_worker_redis",
            return_value=fake_redis,
        ),
    ):
        from backend.app.collectors.pipeline import dispatch_batch_to_destination

        await dispatch_batch_to_destination(dest_id, batch)

    with TestingSessionLocal() as session:
        dlq_rows = (
            session.query(models.DestinationDeadLetter)
            .filter(models.DestinationDeadLetter.destination_id == dest_id)
            .all()
        )

    assert len(dlq_rows) == 1
    assert dlq_rows[0].event_id == "evt-rej-001"
    assert dlq_rows[0].error_kind == "payload_too_large"


# ── CHUNK C (E3): schema_rejected → DLQ row with correct error_kind ───────────


@pytest.mark.asyncio
async def test_poison_pill_e3(static_db, seeded_destination) -> None:
    """schema_rejected error_kind → DLQ row has destination_id+org_id+error_kind."""
    TestingSessionLocal, _ = static_db
    dest_id = seeded_destination

    rejected_item = RejectedEvent(
        event_id="evt-poison-001",
        reason="schema validation failed",
        error_kind="schema_rejected",
        retryable=False,
    )
    fake_result = DeliveryResult(
        accepted=1,
        rejected=[rejected_item],
        retryable=False,
    )

    fake_target = AsyncMock()
    fake_target.send_batch.return_value = fake_result

    import fakeredis.aioredis as fakeredis_aio

    fake_redis = fakeredis_aio.FakeRedis(decode_responses=True)

    batch = [
        _make_envelope("evt-ok-002", org_id=7),
        _make_envelope("evt-poison-001", org_id=7),
    ]

    with (
        patch("backend.app.core.secrets.get_default_backend", return_value=None),
        patch(
            "backend.app.collectors.output.destination_cache.get_destination",
            new_callable=AsyncMock,
            return_value=fake_target,
        ),
        patch(
            "backend.app.collectors.celery_app.get_worker_redis",
            return_value=fake_redis,
        ),
    ):
        from backend.app.collectors.pipeline import dispatch_batch_to_destination

        await dispatch_batch_to_destination(dest_id, batch)

    with TestingSessionLocal() as session:
        row = (
            session.query(models.DestinationDeadLetter)
            .filter(
                models.DestinationDeadLetter.destination_id == dest_id,
                models.DestinationDeadLetter.event_id == "evt-poison-001",
            )
            .first()
        )

    assert row is not None
    assert row.error_kind == "schema_rejected"
    assert row.destination_id == dest_id


# ── CHUNK C (E1): DLQ idempotent ─────────────────────────────────────────────


def test_dlq_idempotent_e1(static_db) -> None:
    """persist_rejected_to_dlq twice for same (destination_id, event_id) → 1 row."""
    TestingSessionLocal, _ = static_db

    from backend.app.collectors.delivery import persist_rejected_to_dlq
    from backend.app.collectors.output.destinations.registry import DestinationConfig

    dest_cfg = DestinationConfig(
        destination_id="dest-idem",
        kind="splunk_hec",
        organization_id=None,
    )
    rej = RejectedEvent(
        event_id="evt-idem-001",
        reason="test",
        error_kind="unknown",
        retryable=False,
    )
    batch = [_make_envelope("evt-idem-001")]

    # Call twice.
    persist_rejected_to_dlq(dest_cfg, [rej], batch)
    persist_rejected_to_dlq(dest_cfg, [rej], batch)

    with TestingSessionLocal() as session:
        count = (
            session.query(models.DestinationDeadLetter)
            .filter(
                models.DestinationDeadLetter.destination_id == "dest-idem",
                models.DestinationDeadLetter.event_id == "evt-idem-001",
            )
            .count()
        )

    assert count == 1, "Idempotent: duplicate (destination_id, event_id) must not create second row"


# ── CHUNK D: circuit breaker ──────────────────────────────────────────────────


@pytest_asyncio.fixture()
async def fake_redis():
    import fakeredis.aioredis as fakeredis_aio

    client = fakeredis_aio.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.mark.asyncio
async def test_circuit_breaker_opens_after_threshold(fake_redis) -> None:
    """After threshold failures:
    - First check() acquires the half-open probe slot and passes.
    - Second check() finds no probe slot available and raises BreakerOpen.
    The destination_id is carried by BreakerOpen.
    """
    from backend.app.collectors.circuit_breaker import (
        BreakerOpen,
        check,
        record_failure,
    )

    dest_id = "breaker-test-001"
    threshold = 3

    for _ in range(threshold):
        await record_failure(
            fake_redis,
            dest_id,
            threshold=threshold,
            cooldown_s=30,
            window_s=60,
        )

    # First call: half-open probe acquired → should NOT raise.
    await check(fake_redis, dest_id)

    # Second call: probe slot taken → must raise BreakerOpen.
    with pytest.raises(BreakerOpen) as exc_info:
        await check(fake_redis, dest_id)

    assert exc_info.value.destination_id == dest_id


@pytest.mark.asyncio
async def test_circuit_breaker_closes_on_success(fake_redis) -> None:
    """record_success() closes the breaker; subsequent check() passes."""
    from backend.app.collectors.circuit_breaker import (
        check,
        record_failure,
        record_success,
    )

    dest_id = "breaker-test-002"

    # Open the breaker.
    for _ in range(5):
        await record_failure(fake_redis, dest_id, threshold=5, cooldown_s=30, window_s=60)

    # Close it.
    await record_success(fake_redis, dest_id)

    # Should not raise.
    await check(fake_redis, dest_id)


@pytest.mark.asyncio
async def test_circuit_breaker_half_open_allows_one_probe(fake_redis) -> None:
    """When OPEN, the first check() acquires a probe and passes (half-open).
    The second concurrent check() raises BreakerOpen."""
    from backend.app.collectors.circuit_breaker import (
        BreakerOpen,
        check,
        record_failure,
    )

    dest_id = "breaker-test-003"

    for _ in range(5):
        await record_failure(fake_redis, dest_id, threshold=5, cooldown_s=30, window_s=60)

    # First call: probe acquired → should NOT raise.
    await check(fake_redis, dest_id)

    # Second call: probe slot taken → must raise BreakerOpen.
    with pytest.raises(BreakerOpen):
        await check(fake_redis, dest_id)


def test_breaker_open_not_in_retryable() -> None:
    """BreakerOpen MUST NOT be in _RETRYABLE (terminal — goes to DLQ, not retry)."""
    from backend.app.collectors.circuit_breaker import BreakerOpen
    from backend.app.collectors.tasks import _RETRYABLE

    assert BreakerOpen not in _RETRYABLE, (
        "BreakerOpen is terminal: retrying would defeat the circuit breaker"
    )


@pytest.mark.asyncio
async def test_breaker_open_propagates_to_dlq(static_db, seeded_destination) -> None:
    """BreakerOpen raised by check() must propagate out of
    dispatch_batch_to_destination uncaught (reaching DLQ handler in task).

    We pre-fill the probe slot so the second check() raises BreakerOpen.
    """
    dest_id = seeded_destination

    import fakeredis.aioredis as fakeredis_aio

    fake_redis = fakeredis_aio.FakeRedis(decode_responses=True)
    from backend.app.collectors.circuit_breaker import BreakerOpen, check, record_failure

    # Open the breaker.
    for _ in range(5):
        await record_failure(fake_redis, dest_id, threshold=5, cooldown_s=30, window_s=60)

    # Consume the probe slot so the next check() in dispatch raises BreakerOpen.
    await check(fake_redis, dest_id)  # acquires probe slot

    with (
        patch("backend.app.core.secrets.get_default_backend", return_value=None),
        patch(
            "backend.app.collectors.celery_app.get_worker_redis",
            return_value=fake_redis,
        ),
    ):
        from backend.app.collectors.pipeline import dispatch_batch_to_destination

        with pytest.raises(BreakerOpen):
            await dispatch_batch_to_destination(dest_id, [_make_envelope()])


# ── E5: chaos isolation ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_chaos_isolation_e5(static_db) -> None:
    """Cross-destination isolation under CONCURRENCY: A and B are
    dispatched together via asyncio.gather. A hangs and times out (TimeoutError,
    ∈ _RETRYABLE); B must deliver successfully *while A is still stuck* — proving
    one slow/failing destination cannot stall or contaminate another.

    Asserted here: B's events never enter the DLQ; B's target is awaited exactly
    once; A surfaces a retryable TimeoutError. (Wazuh fan-out isolation is
    covered by the CHUNK B tests above; _RETRYABLE membership by CHUNK A.)
    """
    TestingSessionLocal, _ = static_db

    dest_a = "dest-slow-a"
    dest_b = "dest-fast-b"

    with TestingSessionLocal() as session:
        for dest_id in (dest_a, dest_b):
            row = models.Destination(
                id=dest_id,
                name=f"Test {dest_id}",
                kind="splunk_hec",
                enabled=True,
                config='{"url": "https://splunk:8088", "sourcetype": "centralops"}',
                secret_ref=None,
                delivery="{}",
                config_version="v1",
                organization_id=None,
            )
            session.add(row)
        session.commit()

    import fakeredis.aioredis as fakeredis_aio

    fake_redis_a = fakeredis_aio.FakeRedis(decode_responses=True)
    fake_redis_b = fakeredis_aio.FakeRedis(decode_responses=True)

    a_started = asyncio.Event()

    async def slow_send(*_: Any, **__: Any) -> DeliveryResult:
        a_started.set()  # signal A is mid-flight
        await asyncio.sleep(1000)  # will be interrupted by timeout
        return DeliveryResult(accepted=1)  # unreachable

    fast_target = AsyncMock()
    fast_target.send_batch.return_value = DeliveryResult(accepted=2)

    slow_target = AsyncMock()
    slow_target.send_batch.side_effect = slow_send

    async def fake_get_dest(cfg: DestinationConfig, secrets: Any = None) -> Any:
        if cfg.destination_id == dest_a:
            return slow_target
        return fast_target

    redis_iter = iter([fake_redis_a, fake_redis_b])

    with (
        patch("backend.app.core.secrets.get_default_backend", return_value=None),
        patch(
            "backend.app.collectors.output.destination_cache.get_destination",
            side_effect=fake_get_dest,
        ),
        patch(
            "backend.app.collectors.celery_app.get_worker_redis",
            side_effect=lambda: next(redis_iter),
        ),
    ):
        from backend.app.collectors.pipeline import dispatch_batch_to_destination

        batch_a = [_make_envelope("evt-a-001")]
        batch_b = [_make_envelope("evt-b-001"), _make_envelope("evt-b-002")]

        # CONCURRENT: A (hangs → times out at 0.05s) and B run together. B must
        # complete on its own while A is still blocked in send_batch.
        results = await asyncio.gather(
            asyncio.wait_for(
                dispatch_batch_to_destination(dest_a, batch_a), timeout=0.05
            ),
            dispatch_batch_to_destination(dest_b, batch_b),
            return_exceptions=True,
        )

    # A timed out with a retryable TimeoutError; B returned cleanly (None).
    assert a_started.is_set(), "A must have entered send_batch (real concurrency)"
    assert isinstance(results[0], (asyncio.TimeoutError, TimeoutError)), (
        f"A must surface a retryable TimeoutError, got {results[0]!r}"
    )
    assert results[1] is None, (
        f"B must deliver successfully while A is hung, got {results[1]!r}"
    )

    # B delivered 2 events — DLQ must be empty for dest_b.
    with TestingSessionLocal() as session:
        b_dlq = (
            session.query(models.DestinationDeadLetter)
            .filter(models.DestinationDeadLetter.destination_id == dest_b)
            .count()
        )
    assert b_dlq == 0, "B's events must NOT be in DLQ after successful delivery"

    # fast_target was called exactly once (for B).
    fast_target.send_batch.assert_awaited_once()


# ── resolve_destination_ids: fail-safe ────────────────────────────────────────


def test_resolve_destination_ids_returns_empty_on_exception() -> None:
    """DB error → returns [] and logs (fail-safe, never raises)."""
    import backend.app.db.database as db_module

    original = db_module.SessionLocal

    class _Exploding:
        def __enter__(self):
            raise RuntimeError("DB unreachable")

        def __exit__(self, *_):
            pass

    db_module.SessionLocal = _Exploding  # type: ignore[assignment]
    try:
        from backend.app.collectors.delivery import resolve_destination_ids

        result = resolve_destination_ids(org_id=1)
        assert result == []
    finally:
        db_module.SessionLocal = original


# ── Review HIGH: Redis-outage in breaker must NOT dead-letter healthy traffic ─


class _OutageRedis:
    """Async redis stub whose every breaker op raises redis.exceptions.ConnectionError
    (NOT the Python builtin — exactly the type that slipped past _RETRYABLE)."""

    def __init__(self) -> None:
        import redis.exceptions as _re

        self._exc = _re.ConnectionError("redis down")

    async def exists(self, *_a: Any) -> int:
        raise self._exc

    async def set(self, *_a: Any, **_k: Any) -> Any:
        raise self._exc

    async def delete(self, *_a: Any) -> int:
        raise self._exc

    def pipeline(self) -> Any:
        raise self._exc

    async def aclose(self) -> None:  # closed in dispatcher's finally
        return None


@pytest.mark.asyncio
async def test_breaker_redis_outage_does_not_dead_letter(
    static_db, seeded_destination
) -> None:
    """A Redis blip during breaker check/record must FAIL OPEN — the batch is
    delivered, NOT dead-lettered. redis.exceptions.ConnectionError
    is not a builtin ConnectionError; fakeredis can't surface this, so we inject
    a stub that raises it on every breaker op."""
    TestingSessionLocal, _ = static_db
    dest_id = seeded_destination

    fake_target = AsyncMock()
    fake_target.send_batch.return_value = DeliveryResult(accepted=1)

    with (
        patch("backend.app.core.secrets.get_default_backend", return_value=None),
        patch(
            "backend.app.collectors.output.destination_cache.get_destination",
            new_callable=AsyncMock,
            return_value=fake_target,
        ),
        patch(
            "backend.app.collectors.celery_app.get_worker_redis",
            return_value=_OutageRedis(),
        ),
    ):
        from backend.app.collectors.pipeline import dispatch_batch_to_destination

        # Must NOT raise (no TransientDeliveryError, no BreakerOpen, no bubbling
        # redis.exceptions.ConnectionError → would otherwise hit except Exception
        # → DLQ "exhausted" with zero retries).
        await dispatch_batch_to_destination(dest_id, [_make_envelope("evt-r-1")])

    # The send actually happened (fail-open allowed it).
    fake_target.send_batch.assert_awaited_once()

    # And nothing was dead-lettered.
    with TestingSessionLocal() as session:
        dlq = (
            session.query(models.DestinationDeadLetter)
            .filter(models.DestinationDeadLetter.destination_id == dest_id)
            .count()
        )
    assert dlq == 0, "a Redis blip must not dead-letter a delivered batch"


# ── Review MEDIUM: DLQ persist failure must signal (retry), not silently lose ─


@pytest.mark.asyncio
async def test_dlq_persist_failure_retries_not_silent_loss(
    static_db, seeded_destination
) -> None:
    """When persist_rejected_to_dlq FAILS (returns False), the dispatcher raises
    TransientDeliveryError so the batch is retried instead of being silently
    acked under acks_late."""
    dest_id = seeded_destination

    rejected_item = RejectedEvent(
        event_id="evt-lost-001",
        reason="schema validation failed",
        error_kind="schema_rejected",
        retryable=False,
    )
    fake_target = AsyncMock()
    fake_target.send_batch.return_value = DeliveryResult(
        accepted=0, rejected=[rejected_item], retryable=False
    )

    import fakeredis.aioredis as fakeredis_aio

    fake_redis = fakeredis_aio.FakeRedis(decode_responses=True)

    from backend.app.collectors.delivery import TransientDeliveryError

    with (
        patch("backend.app.core.secrets.get_default_backend", return_value=None),
        patch(
            "backend.app.collectors.output.destination_cache.get_destination",
            new_callable=AsyncMock,
            return_value=fake_target,
        ),
        patch(
            "backend.app.collectors.celery_app.get_worker_redis",
            return_value=fake_redis,
        ),
        # Simulate a DLQ write failure (DB down / serialization error).
        patch(
            "backend.app.collectors.delivery.persist_rejected_to_dlq",
            return_value=False,
        ) as mock_persist,
    ):
        from backend.app.collectors.pipeline import dispatch_batch_to_destination

        with pytest.raises(TransientDeliveryError) as exc:
            await dispatch_batch_to_destination(dest_id, [_make_envelope("evt-lost-001")])

    assert exc.value.destination_id == dest_id
    mock_persist.assert_called_once()


# ── Review MEDIUM: half-open → CLOSED end-to-end recovery (was untested) ──────


@pytest.mark.asyncio
async def test_circuit_breaker_half_open_recovers_to_closed(
    static_db, seeded_destination
) -> None:
    """Open the breaker, then drive a SUCCESSFUL batch through the dispatcher:
    check_for_config grants the half-open probe, the clean delivery calls
    record_success_for_config, and a subsequent check() finds the breaker CLOSED
    (open/probe/fail keys cleared). Exercises the dispatcher success→record_success
    wiring and the OPEN→half→CLOSED edge — neither covered before."""
    import fakeredis
    import fakeredis.aioredis as fakeredis_aio

    from backend.app.collectors.circuit_breaker import (
        check,
        record_failure,
    )

    dest_id = seeded_destination
    server = fakeredis.FakeServer()

    def _client() -> Any:
        return fakeredis_aio.FakeRedis(decode_responses=True, server=server)

    # Open the breaker (shared server so state survives client aclose()).
    opener = _client()
    for _ in range(5):
        await record_failure(opener, dest_id, threshold=5, cooldown_s=30, window_s=60)
    await opener.aclose()

    fake_target = AsyncMock()
    fake_target.send_batch.return_value = DeliveryResult(accepted=2)

    with (
        patch("backend.app.core.secrets.get_default_backend", return_value=None),
        patch(
            "backend.app.collectors.output.destination_cache.get_destination",
            new_callable=AsyncMock,
            return_value=fake_target,
        ),
        patch(
            "backend.app.collectors.celery_app.get_worker_redis",
            side_effect=_client,
        ),
    ):
        from backend.app.collectors.pipeline import dispatch_batch_to_destination

        # Breaker is OPEN → dispatcher acquires the probe, send succeeds,
        # record_success closes it. Must not raise.
        await dispatch_batch_to_destination(dest_id, [_make_envelope("evt-rec-1")])

    fake_target.send_batch.assert_awaited_once()

    # Breaker is now CLOSED: a fresh check() passes and the keys are gone.
    verify = _client()
    await check(verify, dest_id)  # no BreakerOpen
    assert await verify.exists(f"breaker:{dest_id}:open") == 0
    assert await verify.exists(f"breaker:{dest_id}:fail") == 0
    await verify.aclose()


# ── Review LOW: deterministic per-event rejects must NOT trip the breaker ─────


@pytest.mark.asyncio
async def test_breaker_not_tripped_by_deterministic_rejects(
    static_db, seeded_destination
) -> None:
    """A steady trickle of deterministic (retryable=False) rejections on an
    otherwise-deliverable destination must NOT open the breaker — those are bad
    EVENTS (already DLQ'd), not a bad destination."""
    import fakeredis
    import fakeredis.aioredis as fakeredis_aio

    from backend.app.collectors.circuit_breaker import BreakerOpen, check

    dest_id = seeded_destination
    server = fakeredis.FakeServer()

    def _client() -> Any:
        return fakeredis_aio.FakeRedis(decode_responses=True, server=server)

    fake_target = AsyncMock()
    # accepted>0 AND one deterministic rejection per batch — the exact "poison
    # pill on a healthy sink" shape that previously tripped the breaker.
    fake_target.send_batch.return_value = DeliveryResult(
        accepted=1,
        rejected=[
            RejectedEvent(
                event_id="evt-bad",
                reason="bad",
                error_kind="schema_rejected",
                retryable=False,
            )
        ],
        retryable=False,
    )

    with (
        patch("backend.app.core.secrets.get_default_backend", return_value=None),
        patch(
            "backend.app.collectors.output.destination_cache.get_destination",
            new_callable=AsyncMock,
            return_value=fake_target,
        ),
        patch(
            "backend.app.collectors.celery_app.get_worker_redis",
            side_effect=_client,
        ),
    ):
        from backend.app.collectors.pipeline import dispatch_batch_to_destination

        # Dispatch well past the default threshold (5). Distinct event_ids avoid
        # DLQ dedup noise; the breaker must never open.
        for i in range(8):
            await dispatch_batch_to_destination(
                dest_id, [_make_envelope(f"evt-det-{i}")]
            )

    verify = _client()
    await check(verify, dest_id)  # must NOT raise BreakerOpen
    assert await verify.exists(f"breaker:{dest_id}:open") == 0
    assert await verify.exists(f"breaker:{dest_id}:fail") == 0
    await verify.aclose()


# ── Review LOW: task-level BreakerOpen → DLQ "breaker_open", no retry ─────────


def test_task_breaker_open_routes_to_dlq_no_retry(static_db, seeded_destination) -> None:
    """The dispatch_to_destination TASK must route a BreakerOpen to the DLQ with
    error_kind='breaker_open' and NOT retry/re-raise (terminal). Previously only
    the inner coroutine's raise was tested, never the task's handler."""
    from backend.app.collectors import tasks
    from backend.app.collectors.circuit_breaker import BreakerOpen

    dest_id = seeded_destination

    async def _raise_breaker(destination_id: str, batch: list) -> None:
        raise BreakerOpen(destination_id)

    with (
        patch.object(tasks, "dispatch_batch_to_destination", _raise_breaker),
        patch.object(tasks.dispatch_to_dlq, "apply_async") as mock_dlq,
    ):
        result = tasks.dispatch_to_destination.apply(args=[dest_id, [_make_envelope()]])

    # Task completed without raising (terminal — no autoretry).
    assert result.successful(), f"task must not fail/retry on BreakerOpen: {result.traceback}"
    mock_dlq.assert_called_once()
    kwargs = mock_dlq.call_args.kwargs["kwargs"]
    assert kwargs["destination_id"] == dest_id
    assert kwargs["error_kind"] == "breaker_open"
    assert mock_dlq.call_args.kwargs["queue"] == "dispatch.dlq"


# ── Review LOW: DLQ (destination_id, event_id) unique constraint at DB level ──


def test_dlq_unique_constraint_db_level(static_db) -> None:
    """The destination_dlq table enforces UNIQUE (destination_id, event_id) so
    concurrent redeliveries can't create duplicate forensic rows."""
    from sqlalchemy.exc import IntegrityError

    TestingSessionLocal, _ = static_db

    with TestingSessionLocal() as session:
        session.add(
            models.DestinationDeadLetter(
                destination_id="dest-uq",
                event_id="evt-uq-1",
                organization_id=None,
                error_kind="schema_rejected",
                error_detail="first",
            )
        )
        session.commit()

    with TestingSessionLocal() as session:
        session.add(
            models.DestinationDeadLetter(
                destination_id="dest-uq",
                event_id="evt-uq-1",  # same (destination_id, event_id)
                organization_id=None,
                error_kind="schema_rejected",
                error_detail="duplicate",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
