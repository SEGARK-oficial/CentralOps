"""Sinal ``license_required`` nas rotas partner (gate de licença dos seams EE).

Distinção do contrato (ADR-0013):
  * ``enterprise_required`` — artefato EE AUSENTE (Community): seam não registrado;
  * ``license_required``    — EE PRESENTE, licença sem a feature: o seam registrado
    recusou via ``ee_hooks.LicenseRequiredError`` (classe definida no Core).

Simula o EE registrando hooks fake que levantam ``LicenseRequiredError`` (mesmo
padrão dos testes de seam existentes — o conftest reseta os hooks após cada teste).
Cobre também a decisão 3: um sync RECUSADO persiste ``tenant_sync_status``
(``enterprise_required``/``license_required``) — o polling de /sync-status deixa de
devolver ``null`` para sempre.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core import ee_hooks
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


def _seed_partner_via_db(engine) -> int:
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
            "'partner-uuid', 'partner', 'healthy', datetime('now'), datetime('now'), 0, 0)"
        ))
        db.commit()
        partner_id = db.execute(text("SELECT id FROM integrations WHERE kind='partner'")).fetchone().id
    return partner_id


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


def _engine_from_session_local():
    return _db_module.SessionLocal.kw["bind"]


def _raising_dispatcher(integration_id: int) -> None:
    raise ee_hooks.LicenseRequiredError("multi_tenant")


def _raising_applier(db, integration, selections, state):
    raise ee_hooks.LicenseRequiredError("multi_tenant")


# ── POST /sync-tenants ─────────────────────────────────────────────────


def test_sync_tenants_license_required_when_dispatcher_refuses(client_factory):
    """EE presente + licença sem multi_tenant: o dispatcher levanta
    LicenseRequiredError → 200 status='license_required' (nem 500 nem 503),
    e o motivo é persistido em tenant_sync_status (decisão 3)."""
    ee_hooks.register_partner_sync_dispatcher(_raising_dispatcher)  # conftest reseta

    client = client_factory()
    _bootstrap_and_login(client)
    engine = _engine_from_session_local()
    partner_id = _seed_partner_via_db(engine)

    r = client.post(f"/api/integrations/{partner_id}/sync-tenants")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "license_required"

    # Polling passa a reportar o motivo (não mais null p/ sempre).
    r = client.get(f"/api/integrations/{partner_id}/sync-status")
    assert r.status_code == 200, r.text
    assert r.json()["tenant_sync_status"] == "license_required"


def test_sync_tenants_community_persists_enterprise_required_status(client_factory):
    """Decisão 3 (lado Community): sem dispatcher registrado, o sync recusado
    persiste tenant_sync_status='enterprise_required' — o polling reflete."""
    client = client_factory()
    _bootstrap_and_login(client)
    engine = _engine_from_session_local()
    partner_id = _seed_partner_via_db(engine)

    r = client.post(f"/api/integrations/{partner_id}/sync-tenants")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "enterprise_required"

    r = client.get(f"/api/integrations/{partner_id}/sync-status")
    assert r.status_code == 200, r.text
    assert r.json()["tenant_sync_status"] == "enterprise_required"

    # last_tenant_sync_at NÃO é tocado por um sync recusado (nenhum sync rodou).
    assert r.json()["last_tenant_sync_at"] is None


# ── POST /tenants/select ───────────────────────────────────────────────


def test_select_tenants_license_required_when_applier_refuses(client_factory):
    """EE presente + licença sem multi_tenant: o applier recusa ANTES de
    materializar → decisões persistidas (passo 1), license_required=True,
    zero children, pending=processed (paridade com o branch Community)."""
    ee_hooks.register_tenant_selection_applier(_raising_applier)  # conftest reseta

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
    assert body["pending"] == 2
    assert body["license_required"] is True
    assert body["enterprise_required"] is False

    Session = sessionmaker(bind=engine)
    with Session() as db:
        # Decisões persistidas; NENHUM child materializado.
        sels = db.execute(text(
            "SELECT state FROM integration_tenant_selections WHERE parent_integration_id = :p"
        ), {"p": partner_id}).fetchall()
        assert all(s.state == "approved" for s in sels)
        children = db.execute(text("SELECT id FROM integrations WHERE kind = 'tenant'")).fetchall()
        assert children == []


def test_select_tenants_community_license_required_defaults_false(client_factory):
    """Branch Community (sem applier): enterprise_required=True e o novo campo
    license_required permanece False (sinais mutuamente exclusivos)."""
    client = client_factory()
    _bootstrap_and_login(client)
    engine = _engine_from_session_local()
    partner_id = _seed_partner_via_db(engine)
    _seed_pending_selections(engine, partner_id, ["t1"])

    r = client.post(
        f"/api/integrations/{partner_id}/tenants/select",
        json={"external_ids": ["t1"], "state": "approved"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enterprise_required"] is True
    assert body["license_required"] is False
