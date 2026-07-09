"""Schema dos models de normalização.

Valida que ``Base.metadata.create_all`` cria as 5 tabelas novas com as
constraints esperadas. O seed em si é executado em
``_run_lightweight_migrations`` — testá-lo end-to-end exigiria
interceptar o engine global; o smoke test de boot do app cobre.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import models
from backend.app.db.database import Base


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_all_five_normalize_tables_created(session) -> None:
    inspector = inspect(session.get_bind())
    table_names = set(inspector.get_table_names())
    expected = {
        "mapping_definitions",
        "mapping_versions",
        "unknown_fields",
        "quarantine_events",
        "mapping_audit_log",
    }
    assert expected.issubset(table_names)


def test_mapping_definition_unique_vendor_event_type(session) -> None:
    a = models.MappingDefinition(
        vendor="sophos",
        event_type="sophos.alert",
        ocsf_class_uid=2004,
        description="first",
    )
    session.add(a)
    session.commit()

    b = models.MappingDefinition(
        vendor="sophos",
        event_type="sophos.alert",
        ocsf_class_uid=2004,
        description="dup",
    )
    session.add(b)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_mapping_definition_can_have_multiple_event_types_per_vendor(session) -> None:
    session.add_all([
        models.MappingDefinition(
            vendor="sophos", event_type="sophos.alert", ocsf_class_uid=2004
        ),
        models.MappingDefinition(
            vendor="sophos", event_type="sophos.case", ocsf_class_uid=2005
        ),
    ])
    session.commit()

    rows = session.query(models.MappingDefinition).filter_by(vendor="sophos").all()
    assert {r.event_type for r in rows} == {"sophos.alert", "sophos.case"}


def test_mapping_version_unique_per_definition(session) -> None:
    definition = models.MappingDefinition(
        vendor="ninjaone",
        event_type="ninjaone.activity",
        ocsf_class_uid=6003,
    )
    session.add(definition)
    session.flush()

    v1 = models.MappingVersion(
        definition_id=definition.id,
        version_number=1,
        rules=json.dumps([{"target": "normalized.class_uid", "const": 6003}]),
        commit_message="initial",
    )
    v2 = models.MappingVersion(
        definition_id=definition.id,
        version_number=2,
        rules="[]",
        commit_message="iterate",
    )
    session.add_all([v1, v2])
    session.commit()

    dup = models.MappingVersion(
        definition_id=definition.id,
        version_number=1,
        rules="[]",
        commit_message="conflict",
    )
    session.add(dup)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_unknown_field_unique_path(session) -> None:
    """A unicidade do drift inclui ``organization_id`` —
    ``uq_unknown_field_vendor_event_org`` (vendor, event_type, field_path, org).
    Antes era SEM tenant, vazando campos desconhecidos entre clientes do mesmo
    vendor. Duas rows IGUAIS no MESMO tenant colidem; a mesma path em tenants
    DIFERENTES coexiste (isolamento)."""
    a = models.UnknownField(
        vendor="sophos",
        event_type="sophos.alert",
        field_path="alert.threat.details.hash",
        organization_id=1,
        sample_value="abc",
        sample_type="string",
    )
    session.add(a)
    session.commit()

    # Mesma (vendor, event_type, field_path) + MESMO org → viola a unicidade.
    dup = models.UnknownField(
        vendor="sophos",
        event_type="sophos.alert",
        field_path="alert.threat.details.hash",
        organization_id=1,
        sample_value="other",
        sample_type="string",
    )
    session.add(dup)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    # Isolamento: mesma path, org DIFERENTE → coexiste, sem colisão.
    other_tenant = models.UnknownField(
        vendor="sophos",
        event_type="sophos.alert",
        field_path="alert.threat.details.hash",
        organization_id=2,
        sample_value="tenant2",
        sample_type="string",
    )
    session.add(other_tenant)
    session.commit()  # não levanta — tenants isolados


def test_quarantine_event_persists_with_required_fields(session) -> None:
    now = datetime.utcnow()
    event = models.QuarantineEvent(
        vendor="sophos",
        raw_payload=json.dumps({"id": "abc", "severity": "high"}),
        error_kind="map",
        error_detail="required field 'createdAt' missing",
        expires_at=now + timedelta(days=7),
    )
    session.add(event)
    session.commit()

    fetched = session.query(models.QuarantineEvent).first()
    assert fetched is not None
    assert fetched.error_kind == "map"
    assert fetched.reprocessed_at is None
    assert json.loads(fetched.raw_payload)["id"] == "abc"


def test_mapping_audit_log_append_only_shape(session) -> None:
    # Sem updated_at — apenas created_at. Convenção append-only.
    columns = {c.name for c in models.MappingAuditLog.__table__.columns}
    assert "created_at" in columns
    assert "updated_at" not in columns

    entry = models.MappingAuditLog(
        action="create_definition",
        username="admin",
        user_role="admin",
        detail="seed",
    )
    session.add(entry)
    session.commit()
    assert entry.id is not None
