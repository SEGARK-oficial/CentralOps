"""Routing is the single dispatch path (always-on), and ALL destinations flow through
the SAME uniform lane.

``_enqueue_dispatch`` splits each batch per-event into per-destination sub-batches.
There is NO LONGER a wazuh-default special-case: every destination (including
wazuh-default) is dispatched via ``dispatch_to_destination`` (default/celery mode)
or ``produce_delivery`` (kafka mode). A destination only receives events when a
ROUTE points at it; with NO routes and NO default destination, events go to the DLQ
(error_kind=unrouted), not a hardcoded sink.
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

from backend.app.db import repository
from backend.app.db.database import Base


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


def _ev(sev, org=1, eid=None):
    return {
        "_centralops": {
            "event_id": eid or f"e-{sev}",
            "organization_id": org,
            "severity_id": sev,
            "vendor": "sophos",
        },
        "normalized": {},
        "raw": {},
    }


def _seed_route(SessionLocal, **kw):
    with SessionLocal() as s:
        repository.RouteRepository(s).add(**kw)


def test_routing_splits_batch_by_severity(static_db) -> None:
    """severity_id>=4 → dest; else → wazuh-default (end-to-end).

    BOTH destinations (d-siem AND wazuh-default) flow through the
    SAME uniform lane (``dispatch_to_destination``); wazuh-default is no longer a
    special-case (the legacy ``dispatch_to_wazuh`` lane was deleted)."""
    SessionLocal, _ = static_db
    _seed_route(SessionLocal, name="hi", condition={"severity_id": {"gte": 4}}, destination_ids=["d-siem"], is_final=True, priority=10, organization_id=None)
    _seed_route(SessionLocal, name="rest", condition={}, destination_ids=["wazuh-default"], is_final=True, priority=100, organization_id=None)

    from backend.app.collectors import pipeline

    with patch("backend.app.collectors.tasks.dispatch_to_destination.apply_async") as md:
        pipeline._enqueue_dispatch([_ev(5), _ev(2), _ev(4)])

    # Uniform fan-out: both sub-batches go via dispatch_to_destination.
    assert md.call_count == 2
    by_dest = {c.kwargs["kwargs"]["destination_id"]: c for c in md.call_args_list}
    # dest gets sev 5 + 4 (2 events) on its shard queue
    assert len(by_dest["d-siem"].kwargs["kwargs"]["batch"]) == 2
    assert by_dest["d-siem"].kwargs["queue"].startswith("dispatch.destination.")
    # wazuh-default gets sev 2 (1 event) via the same uniform path/shard queue
    assert len(by_dest["wazuh-default"].kwargs["kwargs"]["batch"]) == 1
    assert by_dest["wazuh-default"].kwargs["queue"].startswith("dispatch.destination.")


def test_routing_no_routes_no_fallback_goes_to_dlq(static_db) -> None:
    """vendor-neutro: sem rotas e sem destino default → eventos vão à DLQ
    (unrouted), NÃO a um sink hardcoded (wazuh-default)."""
    from backend.app.collectors import pipeline

    with (
        patch("backend.app.collectors.tasks.dispatch_to_destination.apply_async") as md,
        patch("backend.app.collectors.delivery.persist_batch_dlq") as mdlq,
    ):
        pipeline._enqueue_dispatch([_ev(5), _ev(2)])
    md.assert_not_called()
    mdlq.assert_called_once()
    assert len(mdlq.call_args.args[0]) == 2  # ambos eventos → DLQ
    assert mdlq.call_args.kwargs["error_kind"] == "unrouted"


def test_routing_drop_discards_events(static_db) -> None:
    SessionLocal, _ = static_db
    _seed_route(SessionLocal, name="noise", condition={"severity_id": {"lt": 1}}, action="drop", destination_ids=[], is_final=True, priority=5, organization_id=None)
    _seed_route(SessionLocal, name="rest", condition={}, destination_ids=["wazuh-default"], is_final=True, priority=100, organization_id=None)

    from backend.app.collectors import pipeline

    with patch("backend.app.collectors.tasks.dispatch_to_destination.apply_async") as md:
        pipeline._enqueue_dispatch([_ev(0), _ev(0), _ev(5)])
    # 2 dropped (sev 0), only sev 5 reaches wazuh-default — via the uniform lane.
    md.assert_called_once()
    assert md.call_args.kwargs["kwargs"]["destination_id"] == "wazuh-default"
    assert len(md.call_args.kwargs["kwargs"]["batch"]) == 1


@pytest.mark.asyncio
async def test_missing_destination_persists_to_dlq_not_silent_loss(static_db) -> None:
    """Routing zero-loss: an event routed to a since-deleted/disabled destination
    is preserved in the DLQ (error_kind=destination_missing), NOT silently lost."""
    SessionLocal, _ = static_db
    from backend.app.collectors.pipeline import dispatch_batch_to_destination
    from backend.app.db import models

    # No destination seeded → _load_destination_config returns None.
    batch = [_ev(5, eid="x1"), _ev(2, eid="x2")]
    await dispatch_batch_to_destination("ghost-dest", batch)

    with SessionLocal() as s:
        rows = (
            s.query(models.DestinationDeadLetter)
            .filter(models.DestinationDeadLetter.destination_id == "ghost-dest")
            .all()
        )
    assert len(rows) == 2  # both events preserved, none lost
    assert all(r.error_kind == "destination_missing" for r in rows)


def test_compile_route_row_preserves_canary_zero(static_db) -> None:
    """Review HIGH: a 0% (paused) canary must compile to 0, NOT invert to 100
    (the `or 100` falsy-zero bug). Tested through the PRODUCTION compile path."""
    SessionLocal, _ = static_db
    from backend.app.collectors import pipeline
    from backend.app.db import models

    with SessionLocal() as s:
        s.add(models.Route(
            name="paused", priority=10, condition="{}", action="route",
            destination_ids='["new-siem"]', is_final=True, canary_percent=0,
            enabled=True, organization_id=None))
        s.commit()
        row = s.query(models.Route).filter(models.Route.name == "paused").first()
        compiled = pipeline._compile_route_row(row)
    assert compiled.canary_percent == 0  # not inverted to 100


def test_routing_zero_canary_routes_nothing_to_canary(static_db) -> None:
    """End-to-end: a 0% canary route sends 0% to the canary dest; all fall through."""
    SessionLocal, _ = static_db
    _seed_route(SessionLocal, name="canary", condition={}, destination_ids=["new-siem"], is_final=True, priority=10, canary_percent=0, organization_id=None)
    _seed_route(SessionLocal, name="rest", condition={}, destination_ids=["wazuh-default"], is_final=True, priority=100, organization_id=None)

    from backend.app.collectors import pipeline

    with patch("backend.app.collectors.tasks.dispatch_to_destination.apply_async") as md:
        pipeline._enqueue_dispatch([_ev(5), _ev(2), _ev(9)])
    # 0% canary → nothing to new-siem; everything falls through to wazuh-default
    # via the uniform lane.
    md.assert_called_once()
    assert md.call_args.kwargs["kwargs"]["destination_id"] == "wazuh-default"
    assert len(md.call_args.kwargs["kwargs"]["batch"]) == 3


def test_load_routes_fail_safe_returns_empty() -> None:
    """A DB error loading routes → [] (route_batch then → fallback dest or DLQ)."""
    import backend.app.db.database as db_module
    from backend.app.collectors import pipeline

    original = db_module.SessionLocal

    class _Boom:
        def __enter__(self):
            raise RuntimeError("db down")

        def __exit__(self, *_):
            return False

    db_module.SessionLocal = _Boom  # type: ignore[assignment]
    try:
        assert pipeline._load_routes_for_org(1) == []
    finally:
        db_module.SessionLocal = original
