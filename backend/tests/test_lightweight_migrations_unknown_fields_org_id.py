"""Regressão — migração lightweight adiciona ``organization_id``
a ``unknown_fields``, faz backfill da integração mais antiga do vendor e
amplia a unique constraint. Idempotente.

Simula um banco LEGADO (tabela sem ``organization_id`` + índice único antigo
``uq_unknown_field_path``), roda ``_run_lightweight_migrations`` DUAS vezes e
verifica: coluna criada, backfill correto, índice novo presente, antigo some,
2ª execução é no-op.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import database as _db_module
from backend.app.db import models as _models  # noqa: F401  — registra tabelas
from backend.app.db.database import Base


def _recreate_legacy_unknown_fields(engine) -> None:
    """Substitui unknown_fields pela forma LEGADA (sem organization_id)."""
    with engine.begin() as c:
        c.execute(text("DROP TABLE unknown_fields"))
        c.execute(
            text(
                """
                CREATE TABLE unknown_fields (
                    id VARCHAR PRIMARY KEY,
                    vendor VARCHAR NOT NULL,
                    event_type VARCHAR NOT NULL,
                    field_path VARCHAR NOT NULL,
                    sample_value TEXT,
                    sample_type VARCHAR,
                    occurrence_count INTEGER NOT NULL DEFAULT 1,
                    first_seen TIMESTAMP NOT NULL,
                    last_seen TIMESTAMP NOT NULL,
                    status VARCHAR NOT NULL DEFAULT 'new'
                )
                """
            )
        )
        c.execute(
            text(
                "CREATE UNIQUE INDEX uq_unknown_field_path "
                "ON unknown_fields (vendor, event_type, field_path)"
            )
        )
        c.execute(text("CREATE INDEX ix_unknown_fields_vendor ON unknown_fields (vendor)"))


def _seed(engine) -> None:
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO organizations (id,name,slug,is_active,created_at,updated_at) "
                "VALUES (5,'Org5','org5',1,datetime('now'),datetime('now'))"
            )
        )
        # Integração mais ANTIGA do vendor sophos pertence à org 5.
        c.execute(
            text(
                "INSERT INTO integrations "
                "(id,organization_id,name,platform,is_active,kind,auth_status,created_at,updated_at) "
                "VALUES (10,5,'i','sophos',1,'tenant','unknown','2020-01-01 00:00:00',datetime('now'))"
            )
        )
        # Row legada sem organization_id.
        c.execute(
            text(
                "INSERT INTO unknown_fields "
                "(id,vendor,event_type,field_path,occurrence_count,first_seen,last_seen,status) "
                "VALUES ('u1','sophos','sophos.alert','a.b',1,datetime('now'),datetime('now'),'new')"
            )
        )


@pytest.fixture
def legacy_engine(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    _recreate_legacy_unknown_fields(engine)
    _seed(engine)
    monkeypatch.setattr(_db_module, "engine", engine)
    TestingSession = sessionmaker(bind=engine)
    monkeypatch.setattr(_db_module, "SessionLocal", TestingSession)
    yield engine


def test_migration_adds_org_column_and_backfills(legacy_engine):
    cols_before = {c["name"] for c in inspect(legacy_engine).get_columns("unknown_fields")}
    assert "organization_id" not in cols_before  # legado

    _db_module._run_lightweight_migrations()

    cols_after = {c["name"] for c in inspect(legacy_engine).get_columns("unknown_fields")}
    assert "organization_id" in cols_after

    # Backfill: row do vendor sophos → org 5 (integração mais antiga).
    with legacy_engine.connect() as c:
        org = c.execute(
            text("SELECT organization_id FROM unknown_fields WHERE id='u1'")
        ).scalar()
    assert org == 5


def test_migration_widens_unique_index(legacy_engine):
    _db_module._run_lightweight_migrations()
    idx = {i["name"] for i in inspect(legacy_engine).get_indexes("unknown_fields")}
    assert "uq_unknown_field_vendor_event_org" in idx
    assert "uq_unknown_field_path" not in idx, "índice único antigo deve sumir"


def test_migration_is_idempotent(legacy_engine):
    _db_module._run_lightweight_migrations()
    # 2ª execução: coluna já existe → bloco pulado, sem exceção.
    _db_module._run_lightweight_migrations()
    cols = {c["name"] for c in inspect(legacy_engine).get_columns("unknown_fields")}
    assert "organization_id" in cols
    # Ainda exatamente 1 row, org preservada.
    with legacy_engine.connect() as c:
        rows = c.execute(text("SELECT organization_id FROM unknown_fields")).fetchall()
    assert len(rows) == 1 and rows[0][0] == 5
