"""Write-path de drift — record_unknown_fields.

Regressões cobertas:
  * upsert ATÔMICO: reobservar o mesmo campo incrementa occurrence_count sem
    perder o registro (o SELECT-depois-INSERT anterior perdia o lote inteiro
    quando dois workers colidiam no índice único);
  * fail-closed em organization_id None (unicidade não protege NULL em Postgres;
    purga/erase por org nunca alcança rows órfãs — LGPD);
  * o desfecho do analista (status ignored/mapped) e o primeiro sample_value
    sobrevivem a reobservações.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.collectors.normalize import drift
from backend.app.db import database as db_module
from backend.app.db import models
from backend.app.db.database import Base

ORG = 1


@pytest.fixture
def isolated_db(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(db_module, "SessionLocal", sessionmaker(bind=engine))
    return engine


def _rows(engine, **filt):
    with sessionmaker(bind=engine)() as db:
        q = select(models.UnknownField)
        for k, v in filt.items():
            q = q.where(getattr(models.UnknownField, k) == v)
        return db.scalars(q).all()


def test_reobserving_a_field_increments_instead_of_duplicating(isolated_db):
    raw = {"alert": {"newField": "x"}}
    for _ in range(3):
        drift.record_unknown_fields(
            vendor="sophos", event_type="sophos.alert", organization_id=ORG,
            raw=raw, consumed_paths=[],
        )
    rows = _rows(isolated_db, field_path="alert.newField", organization_id=ORG)
    assert len(rows) == 1
    assert rows[0].occurrence_count == 3


def test_write_is_skipped_when_org_is_none(isolated_db):
    written = drift.record_unknown_fields(
        vendor="sophos", event_type="sophos.alert", organization_id=None,
        raw={"alert": {"orphan": "x"}}, consumed_paths=[],
    )
    assert written == 0
    assert _rows(isolated_db) == []


def test_upsert_preserves_status_and_first_sample(isolated_db):
    raw = {"alert": {"f": "first"}}
    drift.record_unknown_fields(
        vendor="sophos", event_type="sophos.alert", organization_id=ORG,
        raw=raw, consumed_paths=[],
    )
    # analista marca como ignorado
    with sessionmaker(bind=isolated_db)() as db:
        row = db.scalars(select(models.UnknownField)).one()
        row.status = "ignored"
        first_sample = row.sample_value
        db.commit()

    # campo reaparece — o upsert não pode ressuscitar status "new" nem trocar a amostra
    drift.record_unknown_fields(
        vendor="sophos", event_type="sophos.alert", organization_id=ORG,
        raw={"alert": {"f": "second"}}, consumed_paths=[],
    )
    rows = _rows(isolated_db, field_path="alert.f", organization_id=ORG)
    assert len(rows) == 1
    assert rows[0].status == "ignored"
    assert rows[0].occurrence_count == 2
    assert rows[0].sample_value == first_sample


def test_same_field_coexists_across_orgs(isolated_db):
    raw = {"alert": {"shared": "v"}}
    for org in (1, 2):
        drift.record_unknown_fields(
            vendor="sophos", event_type="sophos.alert", organization_id=org,
            raw=raw, consumed_paths=[],
        )
    assert len(_rows(isolated_db, field_path="alert.shared")) == 2
