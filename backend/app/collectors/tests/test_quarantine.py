"""Quarantine writer — persistência de eventos com erro de normalização."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.collectors import quarantine
from backend.app.db import database, models
from backend.app.db.database import Base


@pytest.fixture()
def isolated_session_factory():
    """Substitui ``database.SessionLocal`` por uma fábrica em SQLite memory.

    O writer chama ``database.SessionLocal()`` diretamente — esse fixture
    intercepta para um engine dedicado por teste.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    original = database.SessionLocal
    database.SessionLocal = Session
    try:
        yield Session
    finally:
        database.SessionLocal = original
        Base.metadata.drop_all(bind=engine)


def test_send_to_quarantine_persists_event(isolated_session_factory) -> None:
    eid = quarantine.send_to_quarantine(
        integration_id=42,
        vendor="sophos",
        event_type="sophos.alert",
        raw={"id": "a", "severity": "high"},
        error_kind=quarantine.ERROR_KIND_MAP,
        error_detail="required field 'createdAt' missing",
        mapping_version_id="ver-1",
    )
    assert eid is not None

    Session = isolated_session_factory
    with Session() as db:
        rows = db.query(models.QuarantineEvent).all()
        assert len(rows) == 1
        ev = rows[0]
        assert ev.vendor == "sophos"
        assert ev.event_type == "sophos.alert"
        assert ev.error_kind == "map"
        assert ev.mapping_version_id == "ver-1"
        assert json.loads(ev.raw_payload) == {"id": "a", "severity": "high"}
        assert ev.expires_at > ev.created_at
        # Default retention: 7d ± alguns segundos
        delta = ev.expires_at - ev.created_at
        assert timedelta(days=6, hours=23) < delta <= timedelta(days=7, seconds=5)


def test_send_to_quarantine_clips_long_error_detail(isolated_session_factory) -> None:
    long_detail = "x" * 5000
    eid = quarantine.send_to_quarantine(
        integration_id=1,
        vendor="sophos",
        event_type="sophos.alert",
        raw={},
        error_kind=quarantine.ERROR_KIND_VALIDATE,
        error_detail=long_detail,
    )
    assert eid is not None

    Session = isolated_session_factory
    with Session() as db:
        ev = db.query(models.QuarantineEvent).first()
        assert ev is not None
        assert len(ev.error_detail) == 2000


def test_send_to_quarantine_truncates_oversize_payload(isolated_session_factory) -> None:
    # Payload acima do limite cai em modo truncado ESTRUTURADO: JSON válido,
    # escalares de topo preservados, campo grande clipado (não some), e o
    # resultado cabe no limite. Difere do antigo corte de string (JSON quebrado).
    from backend.app.core.config import settings

    huge = {
        "id": "evt-1",
        "sensorGeneratedAt": "2026-06-15T17:27:33Z",
        "big_field": "y" * (settings.QUARANTINE_RAW_MAX_BYTES + 50_000),
    }
    eid = quarantine.send_to_quarantine(
        integration_id=1,
        vendor="sophos",
        event_type=None,
        raw=huge,
        error_kind=quarantine.ERROR_KIND_PARSE,
    )
    assert eid is not None

    Session = isolated_session_factory
    with Session() as db:
        ev = db.query(models.QuarantineEvent).first()
        # json.loads NÃO levanta → JSON é válido (pré-requisito do reprocesso).
        parsed = json.loads(ev.raw_payload)
        assert parsed.get("_truncated") is True
        # Escalares de topo preservados para inspeção/diagnóstico.
        assert parsed.get("id") == "evt-1"
        assert parsed.get("sensorGeneratedAt") == "2026-06-15T17:27:33Z"
        # Campo grande clipado, mas presente (não removido).
        assert "big_field" in parsed
        # Cabe no limite configurado.
        assert len(ev.raw_payload) <= settings.QUARANTINE_RAW_MAX_BYTES


def test_send_to_quarantine_small_payload_passthrough(isolated_session_factory) -> None:
    # Payload dentro do limite é gravado intacto (sem marcador _truncated).
    raw = {"id": "x", "msg": "ok"}
    eid = quarantine.send_to_quarantine(
        integration_id=1,
        vendor="sophos",
        event_type="sophos.alert",
        raw=raw,
        error_kind=quarantine.ERROR_KIND_PARSE,
    )
    assert eid is not None

    Session = isolated_session_factory
    with Session() as db:
        ev = db.query(models.QuarantineEvent).first()
        parsed = json.loads(ev.raw_payload)
        assert parsed == raw
        assert "_truncated" not in parsed


def test_send_to_quarantine_handles_missing_event_type(isolated_session_factory) -> None:
    # Eventos podem falhar antes de classificar — event_type é nullable.
    eid = quarantine.send_to_quarantine(
        integration_id=1,
        vendor="sophos",
        event_type=None,
        raw={"id": "x"},
        error_kind=quarantine.ERROR_KIND_PARSE,
    )
    assert eid is not None


def test_send_to_quarantine_returns_none_on_db_error() -> None:
    """DB indisponível: writer loga mas não levanta — pipeline continua."""
    from sqlalchemy.exc import OperationalError

    class _BrokenSession:
        def __enter__(self):
            raise OperationalError("simulated", None, None)

        def __exit__(self, *args):
            return False

    with patch.object(database, "SessionLocal", lambda: _BrokenSession()):
        result = quarantine.send_to_quarantine(
            integration_id=1,
            vendor="sophos",
            event_type="sophos.alert",
            raw={"id": "x"},
            error_kind=quarantine.ERROR_KIND_MAP,
        )
    assert result is None


# ── Tenant da quarentena (organization_id) ───────────────

def test_quarantine_and_dlq_have_tenant_time_index() -> None:
    """Ambas as tabelas expõem o índice composto (organization_id, created_at)."""
    qcols = set(models.QuarantineEvent.__table__.columns.keys())
    assert "organization_id" in qcols, "quarantine_events deve ter organization_id"
    q_idx = {ix.name for ix in models.QuarantineEvent.__table__.indexes}
    assert "ix_quarantine_events_org_created" in q_idx

    dlq_idx = {ix.name for ix in models.DestinationDeadLetter.__table__.indexes}
    assert "ix_destination_dlq_org_created" in dlq_idx


def test_organization_id_explicit_is_persisted(isolated_session_factory) -> None:
    """org_id explícito é gravado (caso o caller já tenha o tenant resolvido)."""
    eid = quarantine.send_to_quarantine(
        integration_id=None,
        vendor="sophos",
        event_type=None,
        raw={"id": "y"},
        error_kind=quarantine.ERROR_KIND_PARSE,
        organization_id=99,
    )
    assert eid is not None
    Session = isolated_session_factory
    with Session() as db:
        ev = db.query(models.QuarantineEvent).filter_by(id=eid).one()
        assert ev.organization_id == 99


def test_organization_id_resolved_from_integration(isolated_session_factory) -> None:
    """Sem org_id explícito, resolve a partir de integration_id (org da integração)."""
    Session = isolated_session_factory
    with Session() as db:
        org = models.Organization(name="Acme", slug="acme")
        db.add(org)
        db.flush()
        integ = models.Integration(
            name="acme-int", organization_id=org.id, platform="sophos", kind="tenant"
        )
        db.add(integ)
        db.commit()
        integ_id, org_id = integ.id, org.id

    eid = quarantine.send_to_quarantine(
        integration_id=integ_id,
        vendor="sophos",
        event_type="sophos.alert",
        raw={"id": "z"},
        error_kind=quarantine.ERROR_KIND_MAP,
    )
    assert eid is not None
    with Session() as db:
        ev = db.query(models.QuarantineEvent).filter_by(id=eid).one()
        assert ev.organization_id == org_id


def test_organization_id_none_when_unresolvable(isolated_session_factory) -> None:
    """integration_id inexistente (ex.: missing_customer_id antes da resolução de
    tenant) → org_id fica None, sem levantar."""
    eid = quarantine.send_to_quarantine(
        integration_id=987654,  # não existe no DB
        vendor="sophos",
        event_type=None,
        raw={"id": "w"},
        error_kind=quarantine.ERROR_KIND_MISSING_CUSTOMER_ID,
    )
    assert eid is not None
    Session = isolated_session_factory
    with Session() as db:
        ev = db.query(models.QuarantineEvent).filter_by(id=eid).one()
        assert ev.organization_id is None
