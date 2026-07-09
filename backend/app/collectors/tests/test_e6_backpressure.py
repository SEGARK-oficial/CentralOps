"""backpressure / load-shedding.

Producer-side queue-depth ceiling: when a destination's shard queue is over the
ceiling and its policy is drop_newest, NEW batches are shed before enqueue —
bounding broker growth. Fail-open (broker unreadable → never shed). Gated behind
BACKPRESSURE_E6_ENABLED (default OFF).

A destination only receives events when a ROUTE points at it,
so the _enqueue_dispatch integration tests seed a broadcast route ``{} → [dest]``.
"""

from __future__ import annotations

import os
from unittest.mock import patch

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.collectors import load_shedder
from backend.app.db import models
from backend.app.db.database import Base


# ── static DB fixture (mirrors test_fase2_delivery) ───────────────────────


@pytest.fixture()
def static_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    import backend.app.db.database as db_module

    original = db_module.SessionLocal
    db_module.SessionLocal = TestingSessionLocal
    yield TestingSessionLocal, engine
    db_module.SessionLocal = original
    Base.metadata.drop_all(bind=engine)


class _FakeBroker:
    def __init__(self, depth):
        self._depth = depth

    def llen(self, _queue):
        if isinstance(self._depth, Exception):
            raise self._depth
        return self._depth


# ── load_shedder ───────────────────────────────────────────────────────────


def test_should_shed_below_ceiling() -> None:
    with patch.object(load_shedder, "_get_broker_redis", return_value=_FakeBroker(3)):
        shed, depth = load_shedder.should_shed("dispatch.destination.0", ceiling=10)
    assert shed is False
    assert depth == 3


def test_should_shed_at_or_over_ceiling() -> None:
    with patch.object(load_shedder, "_get_broker_redis", return_value=_FakeBroker(10)):
        shed, depth = load_shedder.should_shed("dispatch.destination.0", ceiling=10)
    assert shed is True
    assert depth == 10


def test_should_shed_disabled_when_ceiling_zero() -> None:
    # ceiling <= 0 → never even queries the broker
    with patch.object(load_shedder, "_get_broker_redis", side_effect=AssertionError):
        shed, depth = load_shedder.should_shed("q", ceiling=0)
    assert shed is False
    assert depth is None


def test_should_shed_fail_open_on_broker_error() -> None:
    with patch.object(
        load_shedder, "_get_broker_redis", return_value=_FakeBroker(RuntimeError("down"))
    ):
        shed, depth = load_shedder.should_shed("q", ceiling=5)
    assert shed is False  # fail-open
    assert depth is None


# ── _should_shed_dispatch gating ──────────────────────────────────────────


def _item(**over):
    base = {
        "destination_id": "d1",
        "kind": "splunk_hec",
        "shard_queue": "dispatch.destination.3",
        "queue_ceiling": 5,
        "backpressure": "drop_newest",
    }
    base.update(over)
    return base


def test_shed_dispatch_off_by_default() -> None:
    from backend.app.collectors import pipeline
    from backend.app.core.config import settings

    # flag OFF (default) → never sheds, never touches the broker
    with patch.object(settings, "BACKPRESSURE_E6_ENABLED", False):
        assert pipeline._should_shed_dispatch(_item(), 10) is False


def test_shed_dispatch_only_drop_newest_policy() -> None:
    from backend.app.collectors import pipeline
    from backend.app.core.config import settings

    with patch.object(settings, "BACKPRESSURE_E6_ENABLED", True):
        # persistent_queue policy never sheds
        assert pipeline._should_shed_dispatch(_item(backpressure="persistent_queue"), 10) is False


def test_shed_dispatch_sheds_when_over_ceiling() -> None:
    from backend.app.collectors import pipeline
    from backend.app.core.config import settings

    with (
        patch.object(settings, "BACKPRESSURE_E6_ENABLED", True),
        patch.object(load_shedder, "should_shed", return_value=(True, 99)),
    ):
        assert pipeline._should_shed_dispatch(_item(), 10) is True


def test_shed_dispatch_records_native_gauges() -> None:
    """The observed queue depth + backpressure state are recorded to
    the native store (for the UI gauges)."""
    from backend.app.collectors import observability_store as obs
    from backend.app.collectors import pipeline
    from backend.app.core.config import settings

    with (
        patch.object(settings, "BACKPRESSURE_E6_ENABLED", True),
        patch.object(load_shedder, "should_shed", return_value=(True, 99)),
        patch.object(obs, "set_gauge") as mock_gauge,
    ):
        pipeline._should_shed_dispatch(_item(), 10)

    recorded = {c.args[2]: c.args[3] for c in mock_gauge.call_args_list}
    assert recorded.get("queue_depth") == 99
    assert recorded.get("backpressure_state") == "shedding"


def test_shed_dispatch_passes_when_under_ceiling() -> None:
    from backend.app.collectors import pipeline
    from backend.app.core.config import settings

    with (
        patch.object(settings, "BACKPRESSURE_E6_ENABLED", True),
        patch.object(load_shedder, "should_shed", return_value=(False, 1)),
    ):
        assert pipeline._should_shed_dispatch(_item(), 10) is False


def test_shed_increments_only_shed_counter_not_events_rejected() -> None:
    """A shed drop increments DISPATCH_SHED_TOTAL but NOT EVENTS_REJECTED (which
    is DLQ-bound: every increment must map to a DLQ row)."""
    from backend.app.collectors import pipeline
    from backend.app.core.config import settings

    with (
        patch.object(settings, "BACKPRESSURE_E6_ENABLED", True),
        patch.object(load_shedder, "should_shed", return_value=(True, 99)),
        patch("backend.app.collectors.metrics.DISPATCH_SHED_TOTAL") as shed_metric,
        patch("backend.app.collectors.metrics.EVENTS_REJECTED") as rejected_metric,
        patch("backend.app.collectors.metrics.QUEUE_DEPTH"),
    ):
        assert pipeline._should_shed_dispatch(_item(), 10) is True

    shed_metric.labels.assert_called_once()
    rejected_metric.labels.assert_not_called()


def test_shed_dispatch_global_ceiling_fallback() -> None:
    """A destination with queue_ceiling=0 uses the global DISPATCH_QUEUE_CEILING."""
    from backend.app.collectors import pipeline
    from backend.app.core.config import settings

    captured = {}

    def _fake_should_shed(queue, ceiling):
        captured["ceiling"] = ceiling
        return (False, 0)

    with (
        patch.object(settings, "BACKPRESSURE_E6_ENABLED", True),
        patch.object(settings, "DISPATCH_QUEUE_CEILING", 1234),
        patch.object(load_shedder, "should_shed", side_effect=_fake_should_shed),
    ):
        pipeline._should_shed_dispatch(_item(queue_ceiling=0), 10)
    assert captured["ceiling"] == 1234


# ── _enqueue_dispatch integration: shed skips the enqueue ─────────────────


def _seed_dest(SessionLocal, dest_id="d-shed", delivery="{}"):
    with SessionLocal() as s:
        s.add(
            models.Destination(
                id=dest_id,
                name=dest_id,
                kind="splunk_hec",
                enabled=True,
                config='{"url": "https://x:8088"}',
                delivery=delivery,
                config_version="v1",
                organization_id=None,
            )
        )
        s.commit()


def _seed_broadcast_route(SessionLocal, dest_id="d-shed"):
    """The destination only receives events through a route. Seed a
    broadcast clone+continue route ``{} → [dest]`` (so the dest is fed and shedding runs)
    plus an explicit catch-all to wazuh-default.

    wazuh-default is NO LONGER a special lane — it
    flows through the SAME uniform dispatch path as any other destination
    (``dispatch_to_destination`` in celery mode). Because it is not seeded as a
    Destination row here, it is absent from the shed plan and therefore never
    shed — it dispatches independently of the per-destination shed decision."""
    from backend.app.db import repository

    with SessionLocal() as s:
        repo = repository.RouteRepository(s)
        repo.add(
            name=f"broadcast-{dest_id}",
            condition={},
            destination_ids=[dest_id],
            is_final=False,
            priority=10,
            organization_id=None,
        )
        repo.add(
            name="catch-all-wazuh",
            condition={},
            destination_ids=["wazuh-default"],
            is_final=True,
            priority=100,
            organization_id=None,
        )


def _envelope():
    return {"_centralops": {"event_id": "e1", "organization_id": None}, "normalized": {}, "raw": {}}


def test_enqueue_sheds_over_ceiling_destination(static_db) -> None:
    """Shedding bounds the broker for an over-ceiling destination, while the
    uniform fan-out still delivers other destinations.

    ``should_shed`` is patched to True, but only destinations in
    the shed plan (resolve_dispatch_plan → seeded Destination rows) are checked.
    ``d-shed`` IS in the plan → shed (no dispatch). ``wazuh-default`` is NOT seeded
    as a Destination row → absent from the plan → never shed → it flows through the
    SAME uniform ``dispatch_to_destination`` path (no legacy ``dispatch_to_wazuh``
    lane). The byte-identity guarantee is preserved by the uniform path, not by an
    exemption from shedding."""
    SessionLocal, _ = static_db
    _seed_dest(SessionLocal, delivery='{"backpressure": "drop_newest", "queue_ceiling": 2}')
    _seed_broadcast_route(SessionLocal)

    from backend.app.collectors import pipeline
    from backend.app.core.config import settings

    with (
        patch.object(settings, "BACKPRESSURE_E6_ENABLED", True),
        patch.object(load_shedder, "should_shed", return_value=(True, 50)),
        patch("backend.app.collectors.tasks.dispatch_to_destination.apply_async") as mock_dest,
    ):
        pipeline._enqueue_dispatch([_envelope()])

    # ``d-shed`` shed (in plan, over ceiling); ``wazuh-default`` delivered via the
    # SAME uniform ``dispatch_to_destination`` path (not in plan → not shed). There
    # is no legacy dedicated Wazuh lane anymore.
    mock_dest.assert_called_once()
    assert mock_dest.call_args.kwargs["kwargs"]["destination_id"] == "wazuh-default"


def test_enqueue_passes_under_ceiling_destination(static_db) -> None:
    """Under the ceiling, the uniform fan-out delivers BOTH destinations via the
    SAME ``dispatch_to_destination`` path — ``wazuh-default`` is no longer a
    special-case lane."""
    SessionLocal, _ = static_db
    _seed_dest(SessionLocal, delivery='{"backpressure": "drop_newest", "queue_ceiling": 2}')
    _seed_broadcast_route(SessionLocal)

    from backend.app.collectors import pipeline
    from backend.app.core.config import settings

    with (
        patch.object(settings, "BACKPRESSURE_E6_ENABLED", True),
        patch.object(load_shedder, "should_shed", return_value=(False, 0)),
        patch("backend.app.collectors.tasks.dispatch_to_destination.apply_async") as mock_dest,
    ):
        pipeline._enqueue_dispatch([_envelope()])

    # Both ``d-shed`` and ``wazuh-default`` dispatched via the uniform sharded path —
    # there is no legacy dedicated Wazuh lane anymore.
    assert mock_dest.call_count == 2
    dispatched = {c.kwargs["kwargs"]["destination_id"] for c in mock_dest.call_args_list}
    assert dispatched == {"d-shed", "wazuh-default"}
    for c in mock_dest.call_args_list:
        assert c.kwargs["queue"].startswith("dispatch.destination.")
