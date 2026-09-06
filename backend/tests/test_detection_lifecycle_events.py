"""A triagem de uma Detection sai como 2004 de Update/Close.

Sem isto o SOAR que abriu caso no Create nunca ficava sabendo do Close. O
evento repete o ``finding_info.uid`` do achado original (= ``dedup_key``),
é best-effort e fica atrás de ``DETECTION_LIFECYCLE_EVENTS``.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.collectors import detection_events
from backend.app.collectors.normalize.ocsf import get_registry, structural_gate
from backend.app.core.config import settings
from backend.app.db import models, repository
from backend.app.db.models import Base
from backend.app.routers import detections as detections_router


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _detection(db, **overrides) -> models.Detection:
    params = dict(
        organization_id=8,
        source="scheduled_query",
        dedup_key="sched:2:integ:9:66ce5966769e1441",
        severity_id=3,
        rule_name="Hunt",
        integration_id=9,
        ocsf_ref="sched-2-539-row-66ce5966769e1441",
        search_result_id=None,
        count=2,
        status="open",
        first_seen=datetime(2026, 8, 24, 10, 0, 0),
        last_seen=datetime(2026, 8, 24, 22, 0, 0),
    )
    params.update(overrides)
    det = models.Detection(**params)
    db.add(det)
    db.commit()
    db.refresh(det)
    return det


# ── builder ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status, activity_id, status_id",
    [("ack", 2, 2), ("closed", 3, 4), ("open", 2, 1)],
)
def test_transition_maps_to_activity_and_status(db, status, activity_id, status_id) -> None:
    det = _detection(db, status=status)
    envelope = detection_events.build_status_event(
        det, previous_status="open", actor_user_id=5, integration=None, organization=None,
        now_ms=1_787_252_507_558,
    )
    n = envelope["normalized"]
    assert n["class_uid"] == 2004
    assert n["activity_id"] == activity_id
    assert n["type_uid"] == 2004 * 100 + activity_id
    assert n["status_id"] == status_id
    assert n["severity_id"] == 3
    assert n["count"] == 2
    # A identidade do achado original — é o que o consumidor casa.
    assert n["finding_info"]["uid"] == det.dedup_key
    assert n["unmapped"]["ocsf_ref"] == det.ocsf_ref
    assert n["unmapped"]["status"] == status
    assert n["unmapped"]["previous_status"] == "open"
    assert n["unmapped"]["triaged_by_user_id"] == 5
    assert n["unmapped"]["centralops_detection"] is True
    assert envelope["_centralops"]["event_type"] == "centralops.detection.status"
    assert envelope["_centralops"]["organization_id"] == 8
    assert envelope["raw"] == {}


def test_event_passes_the_structural_gate(db) -> None:
    for status in ("ack", "closed", "open"):
        det = _detection(db, status=status, dedup_key=f"k-{status}")
        envelope = detection_events.build_status_event(
            det, previous_status=None, actor_user_id=None, integration=None, organization=None,
        )
        verdict = structural_gate(envelope["normalized"], get_registry("1.8.0"))
        assert verdict.valid, (verdict.reason, verdict.missing_required)


def test_tenant_labels_come_from_integration_and_organization(db) -> None:
    det = _detection(db)
    envelope = detection_events.build_status_event(
        det,
        previous_status="open",
        actor_user_id=None,
        integration=SimpleNamespace(platform="sophos", data_geography="US"),
        organization=SimpleNamespace(name="Org", slug="org"),
    )
    cc = envelope["_centralops"]
    assert cc["platform"] == "sophos"
    assert cc["organization_slug"] == "org"
    assert cc["customer_name"] == "Org"
    assert cc["data_geography"] == "US"


def test_out_of_enum_severity_falls_back_instead_of_breaking_the_event(db) -> None:
    det = _detection(db, severity_id=42)
    envelope = detection_events.build_status_event(
        det, previous_status="open", actor_user_id=None, integration=None, organization=None,
    )
    assert envelope["normalized"]["severity_id"] == 4


# ── emissão ───────────────────────────────────────────────────────────


def test_flag_off_emits_nothing(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "DETECTION_LIFECYCLE_EVENTS", False)
    det = _detection(db, status="closed")
    with patch("backend.app.collectors.pipeline._enqueue_dispatch") as enqueue:
        assert detection_events.emit_status_event(db, det, previous_status="open", actor_user_id=1) is None
    enqueue.assert_not_called()


def test_flag_on_dispatches_one_event(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "DETECTION_LIFECYCLE_EVENTS", True)
    det = _detection(db, status="closed")
    with patch("backend.app.collectors.pipeline._enqueue_dispatch") as enqueue:
        envelope = detection_events.emit_status_event(db, det, previous_status="ack", actor_user_id=1)
    enqueue.assert_called_once()
    (batch,), _ = enqueue.call_args
    assert batch == [envelope]
    assert envelope["normalized"]["activity_id"] == 3


def test_dispatch_failure_is_logged_not_raised(db, monkeypatch, caplog) -> None:
    monkeypatch.setattr(settings, "DETECTION_LIFECYCLE_EVENTS", True)
    det = _detection(db, status="ack")
    with patch("backend.app.collectors.pipeline._enqueue_dispatch", side_effect=RuntimeError("broker")):
        assert detection_events.emit_status_event(db, det, previous_status="open", actor_user_id=1) is None
    assert any("falha ao emitir" in r.getMessage() for r in caplog.records)


# ── router ────────────────────────────────────────────────────────────


def _patch_status(db, det, status):
    with patch.object(detections_router.tenant, "accessible_org_ids", return_value=None):
        return detections_router.update_detection_status(
            det.id,
            detections_router.DetectionStatusUpdate(status=status),
            db=db,
            current_user=SimpleNamespace(id=7),
        )


def test_patch_emits_on_transition_and_not_on_noop(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "DETECTION_LIFECYCLE_EVENTS", True)
    det = _detection(db)
    with patch("backend.app.collectors.pipeline._enqueue_dispatch") as enqueue:
        read = _patch_status(db, det, "ack")
        assert read.status == "ack"
        assert enqueue.call_count == 1
        (batch,), _ = enqueue.call_args
        assert batch[0]["normalized"]["unmapped"]["previous_status"] == "open"
        assert batch[0]["normalized"]["unmapped"]["triaged_by_user_id"] == 7

        # Mesmo status de novo: nada muda, nada sai.
        _patch_status(db, det, "ack")
        assert enqueue.call_count == 1

        _patch_status(db, det, "closed")
        assert enqueue.call_count == 2
        (batch,), _ = enqueue.call_args
        assert batch[0]["normalized"]["activity_id"] == 3


def test_patch_still_persists_when_the_wire_fails(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "DETECTION_LIFECYCLE_EVENTS", True)
    det = _detection(db)
    with patch("backend.app.collectors.pipeline._enqueue_dispatch", side_effect=RuntimeError("broker")):
        read = _patch_status(db, det, "closed")
    assert read.status == "closed"
    assert repository.DetectionRepository(db).get(det.id).status == "closed"
