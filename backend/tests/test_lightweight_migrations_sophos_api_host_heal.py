"""Regression for Fix B.1 — heal of fantasma ``api_host`` in integrations.

Setup pre-fix: tenants discovered while ``sync_sophos_partner`` was using
the wrong Celery broker (Erro A) ended up persisted with ``api_host``
derived from a geo-code (e.g. ``api-EU.central.sophos.com``). That
hostname does not exist in DNS and produces NXDOMAIN on every collection.

Fix: ``_run_lightweight_migrations()`` NULL-outs every Sophos integration
row whose ``api_host`` matches the geo-code-derived pattern. The next
``sync_sophos_partner`` run repopulates ``api_host`` verbatim from the
``apiHost`` field of the Sophos /partner/v1/tenants payload. The daily
Beat job ensures self-heal even if the operator never clicks
"sync tenants" manually.

These tests verify:

1. Geo-code-derived hosts (``api-EU...``, ``api-US...``, etc) are wiped.
2. Real datacenter slugs (``api-eu03...``, ``api-us02...``) are preserved.
3. Non-Sophos rows are not touched.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import database as _db_module
from backend.app.db import models as _models  # noqa: F401  — registers tables
from backend.app.db.database import Base


@pytest.fixture
def fresh_db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(_db_module, "SessionLocal", TestingSession)
    # The migration helper reads the module-level ``engine``; rebind it too.
    monkeypatch.setattr(_db_module, "engine", engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


def _seed_integrations(engine) -> None:
    """Insert a mix of rows: Sophos with fantasma host, Sophos with real
    slug host, Sophos with NULL host, and a Wazuh row that must not be
    touched by a Sophos-specific heal."""
    with sessionmaker(bind=engine)() as db:
        db.execute(text(
            "INSERT INTO organizations(name, slug, is_active, created_at, updated_at) "
            "VALUES ('Org Heal', 'org-heal', 1, datetime('now'), datetime('now'))"
        ))
        # 1: Sophos child with fantasma geo-code host (should be wiped).
        db.execute(text(
            "INSERT INTO integrations(organization_id, name, platform, is_active, "
            "kind, client_id, client_secret, external_id, api_host, "
            "auth_status, created_at, updated_at) "
            "VALUES (1, 'Sophos EU fantasma', 'sophos', 1, 'tenant', 'cid1', "
            "'enc::sec1', 'tEU', 'api-EU.central.sophos.com', 'unknown', "
            "datetime('now'), datetime('now'))"
        ))
        # 2: Sophos child with US geo-code host (should be wiped).
        db.execute(text(
            "INSERT INTO integrations(organization_id, name, platform, is_active, "
            "kind, client_id, client_secret, external_id, api_host, "
            "auth_status, created_at, updated_at) "
            "VALUES (1, 'Sophos US fantasma', 'sophos', 1, 'tenant', 'cid2', "
            "'enc::sec2', 'tUS', 'api-US.central.sophos.com', 'unknown', "
            "datetime('now'), datetime('now'))"
        ))
        # 3: Sophos child with REAL slug host (must be preserved).
        db.execute(text(
            "INSERT INTO integrations(organization_id, name, platform, is_active, "
            "kind, client_id, client_secret, external_id, api_host, "
            "auth_status, created_at, updated_at) "
            "VALUES (1, 'Sophos EU03 real', 'sophos', 1, 'tenant', 'cid3', "
            "'enc::sec3', 'tEU03', 'api-eu03.central.sophos.com', 'unknown', "
            "datetime('now'), datetime('now'))"
        ))
        # 4: Sophos child with NULL api_host (should remain NULL — no-op).
        db.execute(text(
            "INSERT INTO integrations(organization_id, name, platform, is_active, "
            "kind, client_id, client_secret, external_id, "
            "auth_status, created_at, updated_at) "
            "VALUES (1, 'Sophos sem host', 'sophos', 1, 'tenant', 'cid4', "
            "'enc::sec4', 'tNoHost', 'unknown', "
            "datetime('now'), datetime('now'))"
        ))
        # 5: Wazuh row with similar-looking host (must NOT be touched —
        # heal targets Sophos only). Wazuh doesn't actually use that
        # hostname format but the test guards against an over-broad UPDATE.
        db.execute(text(
            "INSERT INTO integrations(organization_id, name, platform, is_active, "
            "kind, api_host, "
            "auth_status, created_at, updated_at) "
            "VALUES (1, 'Wazuh dummy', 'wazuh', 1, 'tenant', "
            "'api-EU.central.sophos.com', 'unknown', "
            "datetime('now'), datetime('now'))"
        ))
        db.commit()


def test_migration_nulls_out_geo_code_derived_api_host(fresh_db):
    """Run the migration block and verify the heal targets only Sophos
    rows whose ``api_host`` matches the geo-code-derived pattern."""
    _seed_integrations(fresh_db)
    # Trigger the lightweight migration (idempotent — safe to re-run on
    # a schema already created by Base.metadata.create_all).
    _db_module._run_lightweight_migrations()

    with sessionmaker(bind=fresh_db)() as db:
        rows = db.execute(text(
            "SELECT name, platform, api_host FROM integrations ORDER BY id"
        )).fetchall()
        # Index by name for readable assertions.
        by_name = {r.name: r for r in rows}

        # Sophos fantasma rows → wiped.
        assert by_name["Sophos EU fantasma"].api_host is None
        assert by_name["Sophos US fantasma"].api_host is None

        # Sophos real slug → preserved.
        assert by_name["Sophos EU03 real"].api_host == "api-eu03.central.sophos.com"

        # Sophos NULL → still NULL (no-op).
        assert by_name["Sophos sem host"].api_host is None

        # Non-Sophos row with the same host string → NOT touched.
        # (Defense-in-depth: heal must not over-match by host alone.)
        assert by_name["Wazuh dummy"].api_host == "api-EU.central.sophos.com"


def test_migration_is_idempotent(fresh_db):
    """Run the migration twice — second run must be a no-op (no errors)."""
    _seed_integrations(fresh_db)
    _db_module._run_lightweight_migrations()
    # Second run should not raise and should leave state unchanged.
    _db_module._run_lightweight_migrations()
    with sessionmaker(bind=fresh_db)() as db:
        count_null = db.execute(text(
            "SELECT COUNT(*) FROM integrations "
            "WHERE platform = 'sophos' AND api_host IS NULL"
        )).scalar()
        # Rows 1, 2 (wiped) + row 4 (started NULL) = 3 NULLs in Sophos.
        assert count_null == 3
