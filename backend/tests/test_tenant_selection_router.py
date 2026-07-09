"""Integration tests dos endpoints novos de tenant selection.

  * GET /api/integrations/{id}/sophos-tenants (listing + refresh)
  * POST /api/integrations/{id}/tenants/select (bulk approve/exclude)
  * PATCH /api/integrations/{id}/auto-approve-policy
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import database as _db_module
from backend.app.db import models  # noqa: F401  — register tables
from backend.app.db.database import Base, get_session
from backend.app.main import app


@pytest.fixture()
def client_factory(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    # Reusada por sync helpers que rodam fora de FastAPI.
    monkeypatch.setattr(_db_module, "SessionLocal", TestingSession)

    def override_get_session():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_session] = override_get_session
    clients: list[TestClient] = []

    def factory() -> TestClient:
        c = TestClient(app)
        clients.append(c)
        return c

    yield factory

    for c in clients:
        c.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


def _bootstrap_and_login(client: TestClient) -> None:
    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPassword123!", "display_name": "Admin"},
    )
    assert r.status_code == 200, r.text
    r = client.post("/api/auth/login", json={"username": "admin", "password": "AdminPassword123!"})
    assert r.status_code == 200, r.text


def _seed_partner_via_db(engine, *, auto_approve: bool = False) -> int:
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.execute(text(
            "INSERT INTO organizations(name, slug, is_active, created_at, updated_at, auto_managed) "
            "VALUES ('Holding', 'holding', 1, datetime('now'), datetime('now'), 0)"
        ))
        db.execute(text(
            "INSERT INTO integrations(organization_id, name, platform, is_active, kind, "
            "client_id, client_secret, external_id, id_type, auth_status, created_at, "
            "updated_at, auto_managed, auto_approve_new_tenants) "
            "VALUES (1, 'Partner', 'sophos', 1, 'partner', 'cid', 'sec', "
            "'partner-uuid', 'partner', 'healthy', datetime('now'), datetime('now'), 0, :a)"
        ), {"a": 1 if auto_approve else 0})
        # bootstrap + login criam o admin user via /auth/bootstrap; o partner row aqui é o id 1.
        db.commit()
        partner_id = db.execute(text("SELECT id FROM integrations WHERE kind='partner'")).fetchone().id
    return partner_id


def _engine_from_session_local() -> Any:
    """Read the engine que o monkeypatched SessionLocal usa."""
    return _db_module.SessionLocal.kw["bind"]


def _seed_pending_selections(engine, parent_id: int, external_ids: list[str]) -> None:
    Session = sessionmaker(bind=engine)
    with Session() as db:
        for ext in external_ids:
            db.execute(text(
                "INSERT INTO integration_tenant_selections "
                "(parent_integration_id, external_id, state, name_snapshot, region_snapshot, "
                "api_host_snapshot, last_seen_at, created_at, updated_at) "
                "VALUES (:p, :e, 'pending', :name, 'eu03', 'api-eu03.central.sophos.com', "
                "datetime('now'), datetime('now'), datetime('now'))"
            ), {"p": parent_id, "e": ext, "name": f"Tenant {ext}"})
        db.commit()


# ── GET /sophos-tenants ────────────────────────────────────────────────


def test_list_sophos_tenants_no_refresh_returns_snapshot(client_factory):
    client = client_factory()
    _bootstrap_and_login(client)
    engine = _engine_from_session_local()
    partner_id = _seed_partner_via_db(engine)
    _seed_pending_selections(engine, partner_id, ["t-a", "t-b", "t-c"])

    r = client.get(f"/api/integrations/{partner_id}/sophos-tenants")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 3
    assert body["fetched_live"] is False
    assert body["auto_approve_new_tenants"] is False
    assert {it["external_id"] for it in body["items"]} == {"t-a", "t-b", "t-c"}
    assert all(it["selection_state"] == "pending" for it in body["items"])


def test_list_sophos_tenants_kind_must_be_partner(client_factory):
    client = client_factory()
    _bootstrap_and_login(client)
    engine = _engine_from_session_local()
    # Cria integration kind=tenant — endpoint deve retornar 409.
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.execute(text(
            "INSERT INTO organizations(name, slug, is_active, created_at, updated_at, auto_managed) "
            "VALUES ('o', 'o', 1, datetime('now'), datetime('now'), 0)"
        ))
        db.execute(text(
            "INSERT INTO integrations(organization_id, name, platform, is_active, kind, "
            "auth_status, created_at, updated_at, auto_managed) "
            "VALUES (1, 'tenant-only', 'sophos', 1, 'tenant', 'unknown', "
            "datetime('now'), datetime('now'), 0)"
        ))
        db.commit()
    r = client.get("/api/integrations/1/sophos-tenants")
    assert r.status_code == 409


def test_list_sophos_tenants_filter_by_state(client_factory):
    client = client_factory()
    _bootstrap_and_login(client)
    engine = _engine_from_session_local()
    partner_id = _seed_partner_via_db(engine)
    _seed_pending_selections(engine, partner_id, ["t1", "t2", "t3"])
    # Aprovar t1 manualmente na DB.
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.execute(text(
            "UPDATE integration_tenant_selections SET state='approved' WHERE external_id='t1'"
        ))
        db.commit()
    r = client.get(f"/api/integrations/{partner_id}/sophos-tenants?state=approved")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["external_id"] == "t1"

    r = client.get(f"/api/integrations/{partner_id}/sophos-tenants?state=pending")
    assert r.json()["total"] == 2


def test_list_sophos_tenants_with_refresh_calls_provider(client_factory, monkeypatch):
    client = client_factory()
    _bootstrap_and_login(client)
    engine = _engine_from_session_local()
    partner_id = _seed_partner_via_db(engine)
    # Pre-existing pending selection — to assert it's preserved.
    _seed_pending_selections(engine, partner_id, ["t-existing"])

    fake_provider = MagicMock()
    fake_provider.discover_tenants.return_value = [
        {"id": "t-existing", "name": "Existing", "apiHost": "https://api-eu03.central.sophos.com", "dataRegion": "eu03"},
        {"id": "t-new", "name": "Brand New", "apiHost": "https://api-us03.central.sophos.com", "dataRegion": "us03"},
    ]
    fake_provider.close.return_value = None
    # Disable Redis cache to force live call.
    with patch("backend.app.routers.integrations.get_provider", return_value=fake_provider), \
         patch("backend.app.routers.integrations._sophos_get_redis", return_value=None):
        r = client.get(f"/api/integrations/{partner_id}/sophos-tenants?refresh=true")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fetched_live"] is True
    assert body["total"] == 2
    states = {it["external_id"]: it["selection_state"] for it in body["items"]}
    assert states["t-existing"] == "pending"  # preservado
    assert states["t-new"] == "pending"  # auto_approve=False default


# ── POST /tenants/select ───────────────────────────────────────────────


def test_select_tenants_community_persists_state_enterprise_required(client_factory):
    """Community persists the approve/exclude DECISIONS (step 1)
    but child materialization is an Enterprise feature — so it reports
    ``enterprise_required`` with no children and no 500."""
    client = client_factory()
    _bootstrap_and_login(client)
    engine = _engine_from_session_local()
    partner_id = _seed_partner_via_db(engine)
    _seed_pending_selections(engine, partner_id, ["t1", "t2"])

    r = client.post(
        f"/api/integrations/{partner_id}/tenants/select",
        json={"external_ids": ["t1", "t2"], "state": "approved"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["processed"] == 2
    assert body["materialized"] == 0
    assert body["enterprise_required"] is True

    Session = sessionmaker(bind=engine)
    with Session() as db:
        # Decisions ARE persisted (step 1); no child materialized (Enterprise step 2).
        sels = db.execute(text(
            "SELECT state, decided_by_user_id FROM integration_tenant_selections "
            "WHERE parent_integration_id = :p"
        ), {"p": partner_id}).fetchall()
        assert all(s.state == "approved" and s.decided_by_user_id is not None for s in sels)
        children = (
            db.query(models.Integration)
            .filter(models.Integration.kind == "tenant")
            .all()
        )
        assert children == []


def test_select_tenants_delegates_to_registered_applier(client_factory):
    """With an Enterprise tenant-selection applier registered, select_tenants delegates
    synchronously and reports the applier's truthful counts (seam wiring)."""
    from backend.app.core import ee_hooks

    captured: dict = {}

    def _applier(db, integration, selections, state):
        captured["state"] = state
        captured["n"] = len(selections)
        captured["partner_id"] = integration.id
        return {"materialized": len(selections), "deactivated": 0, "pending": 0, "errors": []}

    ee_hooks.register_tenant_selection_applier(_applier)  # conftest resets after test

    client = client_factory()
    _bootstrap_and_login(client)
    engine = _engine_from_session_local()
    partner_id = _seed_partner_via_db(engine)
    _seed_pending_selections(engine, partner_id, ["t1", "t2"])

    r = client.post(
        f"/api/integrations/{partner_id}/tenants/select",
        json={"external_ids": ["t1", "t2"], "state": "approved"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["processed"] == 2
    assert body["materialized"] == 2
    assert body["enterprise_required"] is False
    assert captured == {"state": "approved", "n": 2, "partner_id": partner_id}


def test_select_tenants_unknown_external_id_returns_error_not_500(client_factory):
    client = client_factory()
    _bootstrap_and_login(client)
    engine = _engine_from_session_local()
    partner_id = _seed_partner_via_db(engine)
    # No selections seeded — endpoint deve retornar errors estruturados.
    r = client.post(
        f"/api/integrations/{partner_id}/tenants/select",
        json={"external_ids": ["never-discovered"], "state": "approved"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["processed"] == 0
    assert len(body["errors"]) == 1
    assert body["errors"][0]["external_id"] == "never-discovered"
    assert "not discovered" in body["errors"][0]["reason"]


def test_select_tenants_validation_min_max(client_factory):
    client = client_factory()
    _bootstrap_and_login(client)
    engine = _engine_from_session_local()
    partner_id = _seed_partner_via_db(engine)
    # Empty list: 422.
    r = client.post(
        f"/api/integrations/{partner_id}/tenants/select",
        json={"external_ids": [], "state": "approved"},
    )
    assert r.status_code == 422
    # Too many: 422.
    r = client.post(
        f"/api/integrations/{partner_id}/tenants/select",
        json={"external_ids": [f"id-{i}" for i in range(501)], "state": "approved"},
    )
    assert r.status_code == 422
    # Invalid state: 422.
    r = client.post(
        f"/api/integrations/{partner_id}/tenants/select",
        json={"external_ids": ["x"], "state": "weird"},
    )
    assert r.status_code == 422


# ── PATCH /auto-approve-policy ─────────────────────────────────────────


def test_update_auto_approve_policy_persists(client_factory):
    client = client_factory()
    _bootstrap_and_login(client)
    engine = _engine_from_session_local()
    partner_id = _seed_partner_via_db(engine, auto_approve=False)
    r = client.patch(
        f"/api/integrations/{partner_id}/auto-approve-policy",
        json={"auto_approve_new_tenants": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["auto_approve_new_tenants"] is True

    Session = sessionmaker(bind=engine)
    with Session() as db:
        partner = db.query(models.Integration).filter(models.Integration.id == partner_id).first()
        assert partner.auto_approve_new_tenants is True

    # Toggle back.
    r = client.patch(
        f"/api/integrations/{partner_id}/auto-approve-policy",
        json={"auto_approve_new_tenants": False},
    )
    assert r.status_code == 200
    assert r.json()["auto_approve_new_tenants"] is False


def test_update_auto_approve_policy_only_for_partner(client_factory):
    client = client_factory()
    _bootstrap_and_login(client)
    engine = _engine_from_session_local()
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.execute(text(
            "INSERT INTO organizations(name, slug, is_active, created_at, updated_at, auto_managed) "
            "VALUES ('o', 'o', 1, datetime('now'), datetime('now'), 0)"
        ))
        db.execute(text(
            "INSERT INTO integrations(organization_id, name, platform, is_active, kind, "
            "auth_status, created_at, updated_at, auto_managed) "
            "VALUES (1, 'tenant-only', 'sophos', 1, 'tenant', 'unknown', "
            "datetime('now'), datetime('now'), 0)"
        ))
        db.commit()
    r = client.patch("/api/integrations/1/auto-approve-policy", json={"auto_approve_new_tenants": True})
    assert r.status_code == 409


# ── POST /sync-tenants (edition signal) ────────────────────────────────


def test_sync_tenants_community_returns_enterprise_required(client_factory):
    """partner-sync é feature paga. Em Community (sem
    partner_sync_dispatcher registrado) o endpoint NÃO finge 'ok' — sinaliza
    enterprise_required honestamente, sem disparar nada e sem 500."""
    client = client_factory()
    _bootstrap_and_login(client)
    engine = _engine_from_session_local()
    partner_id = _seed_partner_via_db(engine)

    r = client.post(f"/api/integrations/{partner_id}/sync-tenants")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "enterprise_required"


def test_sync_tenants_dispatches_when_dispatcher_registered(client_factory):
    """Com o partner_sync_dispatcher do EE registrado, /sync-tenants despacha via
    provider.on_created() e reporta 'ok' (paridade com o seam de select_tenants)."""
    from backend.app.core import ee_hooks

    captured: dict = {}

    def _dispatch(integration_id: int) -> None:
        captured["integration_id"] = integration_id

    ee_hooks.register_partner_sync_dispatcher(_dispatch)  # conftest reseta após o teste

    client = client_factory()
    _bootstrap_and_login(client)
    engine = _engine_from_session_local()
    partner_id = _seed_partner_via_db(engine)

    r = client.post(f"/api/integrations/{partner_id}/sync-tenants")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"
    assert captured == {"integration_id": partner_id}
