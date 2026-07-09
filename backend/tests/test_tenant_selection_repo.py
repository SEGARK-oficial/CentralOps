"""Unit tests for ``IntegrationTenantSelectionRepository``.

Cobre find/list/count/upsert_snapshot/set_state idempotência e ordenação.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import models  # noqa: F401  — registers tables on Base
from backend.app.db import repository
from backend.app.db.database import Base


@pytest.fixture
def fresh_db():
    """In-memory SQLite com schema completo (Base.metadata.create_all)."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    with TestingSession() as db:
        # Seed minimal: 1 user + 1 partner integration.
        db.execute(text(
            "INSERT INTO organizations(name, slug, is_active, created_at, updated_at, auto_managed) "
            "VALUES ('Org', 'org', 1, datetime('now'), datetime('now'), 0)"
        ))
        db.execute(text(
            "INSERT INTO app_users(uuid, username, password_hash, role, is_active, created_at, updated_at) "
            "VALUES ('u-uuid', 'admin', 'hash', 'admin', 1, datetime('now'), datetime('now'))"
        ))
        db.execute(text(
            "INSERT INTO integrations(organization_id, name, platform, is_active, kind, "
            "auth_status, created_at, updated_at, auto_managed, auto_approve_new_tenants) "
            "VALUES (1, 'Partner Acme', 'sophos', 1, 'partner', 'unknown', "
            "datetime('now'), datetime('now'), 0, 0)"
        ))
        db.commit()
    yield TestingSession
    Base.metadata.drop_all(bind=engine)


def test_upsert_creates_when_missing(fresh_db):
    with fresh_db() as db:
        repo = repository.IntegrationTenantSelectionRepository(db)
        row, created = repo.upsert_snapshot(
            parent_id=1,
            external_id="tenant-1",
            name_snapshot="Tenant One",
            region_snapshot="eu03",
            api_host_snapshot="api-eu03.central.sophos.com",
            default_state="pending",
        )
        assert created is True
        assert row.state == "pending"
        assert row.name_snapshot == "Tenant One"
        assert row.last_seen_at is not None
        # 2nd call no diff: created=False, no exception.
        row2, created2 = repo.upsert_snapshot(
            parent_id=1,
            external_id="tenant-1",
            name_snapshot="Tenant One",
            region_snapshot="eu03",
            api_host_snapshot="api-eu03.central.sophos.com",
            default_state="pending",
        )
        assert created2 is False
        assert row2.id == row.id


def test_upsert_preserves_state_on_update(fresh_db):
    with fresh_db() as db:
        repo = repository.IntegrationTenantSelectionRepository(db)
        row, _ = repo.upsert_snapshot(
            parent_id=1,
            external_id="t1",
            name_snapshot="Old",
            default_state="pending",
        )
        # Operador aprovou.
        repo.set_state(parent_id=1, external_ids=["t1"], state="approved", decided_by_user_id=1)
        # Re-sync: snapshot atualiza, mas estado persiste.
        row2, _ = repo.upsert_snapshot(
            parent_id=1,
            external_id="t1",
            name_snapshot="New Name",
            default_state="pending",
        )
        assert row2.state == "approved"
        assert row2.name_snapshot == "New Name"


def test_set_state_marks_decided_by_and_at(fresh_db):
    with fresh_db() as db:
        repo = repository.IntegrationTenantSelectionRepository(db)
        repo.upsert_snapshot(parent_id=1, external_id="t1", name_snapshot="A")
        repo.upsert_snapshot(parent_id=1, external_id="t2", name_snapshot="B")
        before = datetime.utcnow() - timedelta(seconds=1)
        rows = repo.set_state(
            parent_id=1, external_ids=["t1", "t2"], state="approved", decided_by_user_id=1
        )
        assert len(rows) == 2
        for r in rows:
            assert r.state == "approved"
            assert r.decided_by_user_id == 1
            assert r.decided_at is not None and r.decided_at >= before


def test_set_state_invalid_raises(fresh_db):
    with fresh_db() as db:
        repo = repository.IntegrationTenantSelectionRepository(db)
        with pytest.raises(ValueError):
            repo.set_state(parent_id=1, external_ids=["t1"], state="weird", decided_by_user_id=1)


def test_set_state_dedupes_to_existing_only(fresh_db):
    with fresh_db() as db:
        repo = repository.IntegrationTenantSelectionRepository(db)
        repo.upsert_snapshot(parent_id=1, external_id="t1", name_snapshot="A")
        # External_id "ghost" não tem row — não cria, não erra.
        rows = repo.set_state(
            parent_id=1, external_ids=["t1", "ghost"], state="approved", decided_by_user_id=1
        )
        assert len(rows) == 1
        assert rows[0].external_id == "t1"


def test_list_paginated_and_filtered(fresh_db):
    with fresh_db() as db:
        repo = repository.IntegrationTenantSelectionRepository(db)
        for i in range(5):
            repo.upsert_snapshot(parent_id=1, external_id=f"t{i}", name_snapshot=f"Name {i:02d}")
        repo.set_state(parent_id=1, external_ids=["t0", "t1"], state="approved", decided_by_user_id=1)

        all_rows = repo.list(1)
        assert len(all_rows) == 5
        # Ordenação por nome asc.
        assert [r.name_snapshot for r in all_rows] == [
            "Name 00", "Name 01", "Name 02", "Name 03", "Name 04"
        ]

        approved = repo.list(1, state="approved")
        assert len(approved) == 2

        page = repo.list(1, limit=2, offset=2)
        assert [r.name_snapshot for r in page] == ["Name 02", "Name 03"]

        assert repo.count(1) == 5
        assert repo.count(1, state="approved") == 2
        assert repo.count(1, state="pending") == 3


def test_list_external_ids_state_filter(fresh_db):
    with fresh_db() as db:
        repo = repository.IntegrationTenantSelectionRepository(db)
        repo.upsert_snapshot(parent_id=1, external_id="a", name_snapshot="a")
        repo.upsert_snapshot(parent_id=1, external_id="b", name_snapshot="b")
        repo.upsert_snapshot(parent_id=1, external_id="c", name_snapshot="c")
        repo.set_state(parent_id=1, external_ids=["a", "b"], state="excluded", decided_by_user_id=1)
        excluded = repo.list_external_ids(1, state="excluded")
        assert excluded == {"a", "b"}
        all_ids = repo.list_external_ids(1)
        assert all_ids == {"a", "b", "c"}


def test_find_returns_none_for_unknown(fresh_db):
    with fresh_db() as db:
        repo = repository.IntegrationTenantSelectionRepository(db)
        assert repo.find(1, "nope") is None
        assert repo.find(1, "") is None


def test_upsert_invalid_state_raises(fresh_db):
    with fresh_db() as db:
        repo = repository.IntegrationTenantSelectionRepository(db)
        with pytest.raises(ValueError):
            repo.upsert_snapshot(parent_id=1, external_id="t1", default_state="weird")
        with pytest.raises(ValueError):
            repo.upsert_snapshot(parent_id=1, external_id="", default_state="pending")
