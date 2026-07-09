"""Tests for server-side search, geography filter, and pagination on
GET /api/integrations/{id}/sophos-tenants.

Covers:
- 250 rows: page=1&size=10 → 10 items + total=250
- page=25&size=10 → last 10 rows
- ?search=acme case-insensitive (name_snapshot + external_id)
- ?geography=EU (data_geography_snapshot, case-insensitive)
- ?size=2000 → 422 (exceeds le=1000)
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

from backend.app.db import database as _db_module
from backend.app.db import models  # noqa: F401  — register tables
from backend.app.db.database import Base, get_session
from backend.app.main import app


# ── Fixtures ──────────────────────────────────────────────────────────────


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


# ── Helpers ──────────────────────────────────────────────────────────────


def _engine_from_session_local():
    return _db_module.SessionLocal.kw["bind"]


def _bootstrap_and_login(client: TestClient) -> None:
    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPassword123!", "display_name": "Admin"},
    )
    assert r.status_code == 200, r.text
    r = client.post("/api/auth/login", json={"username": "admin", "password": "AdminPassword123!"})
    assert r.status_code == 200, r.text


def _seed_partner(engine, *, auto_approve: bool = False) -> int:
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
        db.commit()
        row = db.execute(text("SELECT id FROM integrations WHERE kind='partner'")).fetchone()
        return row.id


def _seed_selections(
    engine,
    parent_id: int,
    rows: list[dict],
) -> None:
    """Insert raw rows into integration_tenant_selections.

    Each dict may have: external_id, name_snapshot, data_geography_snapshot, state.
    """
    Session = sessionmaker(bind=engine)
    with Session() as db:
        for r in rows:
            db.execute(
                text(
                    "INSERT INTO integration_tenant_selections "
                    "(parent_integration_id, external_id, state, name_snapshot, "
                    "region_snapshot, data_geography_snapshot, api_host_snapshot, "
                    "last_seen_at, created_at, updated_at) "
                    "VALUES (:p, :e, :s, :name, 'eu03', :geo, 'api.sophos.com', "
                    "datetime('now'), datetime('now'), datetime('now'))"
                ),
                {
                    "p": parent_id,
                    "e": r["external_id"],
                    "s": r.get("state", "pending"),
                    "name": r.get("name_snapshot", f"Tenant {r['external_id']}"),
                    "geo": r.get("data_geography_snapshot", "US"),
                },
            )
        db.commit()


# ── Tests ─────────────────────────────────────────────────────────────────


def test_pagination_250_rows_page1_size10(client_factory):
    """250 selections → page=1&size=10 retorna 10 items e total=250."""
    client = client_factory()
    _bootstrap_and_login(client)
    engine = _engine_from_session_local()
    partner_id = _seed_partner(engine)

    rows = [{"external_id": f"t-{i:03d}", "name_snapshot": f"Tenant {i:03d}"} for i in range(250)]
    _seed_selections(engine, partner_id, rows)

    r = client.get(f"/api/integrations/{partner_id}/sophos-tenants?page=1&size=10")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 250
    assert body["page"] == 1
    assert body["size"] == 10
    assert len(body["items"]) == 10


def test_pagination_250_rows_page25_size10(client_factory):
    """250 selections → page=25&size=10 retorna os últimos 10 items (241-250)."""
    client = client_factory()
    _bootstrap_and_login(client)
    engine = _engine_from_session_local()
    partner_id = _seed_partner(engine)

    # Names alfabéticos garantem ordenação determinística.
    rows = [{"external_id": f"t-{i:03d}", "name_snapshot": f"Tenant {i:03d}"} for i in range(250)]
    _seed_selections(engine, partner_id, rows)

    r = client.get(f"/api/integrations/{partner_id}/sophos-tenants?page=25&size=10")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 250
    assert body["page"] == 25
    assert len(body["items"]) == 10
    # Última página deve conter t-240 … t-249 (ordenação name_snapshot asc).
    external_ids = {it["external_id"] for it in body["items"]}
    assert external_ids == {f"t-{i:03d}" for i in range(240, 250)}


@pytest.mark.parametrize("needle,expected_match", [
    ("acme", {"acme-corp", "acme-ltd", "ext-acme"}),
    ("ACME", {"acme-corp", "acme-ltd", "ext-acme"}),
    ("corp", {"acme-corp", "beta-corp"}),
    ("zeta", set()),
])
def test_search_filter(client_factory, needle: str, expected_match: set[str]):
    """?search= filtra por name_snapshot OU external_id, case-insensitive."""
    client = client_factory()
    _bootstrap_and_login(client)
    engine = _engine_from_session_local()
    partner_id = _seed_partner(engine)

    _seed_selections(engine, partner_id, [
        {"external_id": "acme-corp", "name_snapshot": "ACME Corporation"},
        {"external_id": "acme-ltd", "name_snapshot": "Acme Ltd"},
        {"external_id": "ext-acme", "name_snapshot": "Other"},       # match por external_id
        {"external_id": "beta-corp", "name_snapshot": "Beta Corp"},
        {"external_id": "gamma", "name_snapshot": "Gamma LLC"},
    ])

    r = client.get(
        f"/api/integrations/{partner_id}/sophos-tenants",
        params={"search": needle, "size": 100},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    returned = {it["external_id"] for it in body["items"]}
    assert returned == expected_match
    assert body["total"] == len(expected_match)


@pytest.mark.parametrize("geo_param,expected_ids", [
    ("EU", {"eu-1", "eu-2"}),
    ("eu", {"eu-1", "eu-2"}),       # case-insensitive
    ("US", {"us-1"}),
    ("JP", set()),
])
def test_geography_filter(client_factory, geo_param: str, expected_ids: set[str]):
    """?geography= filtra por data_geography_snapshot, case-insensitive."""
    client = client_factory()
    _bootstrap_and_login(client)
    engine = _engine_from_session_local()
    partner_id = _seed_partner(engine)

    _seed_selections(engine, partner_id, [
        {"external_id": "eu-1", "name_snapshot": "EU Tenant 1", "data_geography_snapshot": "EU"},
        {"external_id": "eu-2", "name_snapshot": "EU Tenant 2", "data_geography_snapshot": "EU"},
        {"external_id": "us-1", "name_snapshot": "US Tenant 1", "data_geography_snapshot": "US"},
    ])

    r = client.get(
        f"/api/integrations/{partner_id}/sophos-tenants",
        params={"geography": geo_param, "size": 100},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    returned = {it["external_id"] for it in body["items"]}
    assert returned == expected_ids
    assert body["total"] == len(expected_ids)


def test_size_over_max_returns_422(client_factory):
    """?size=2000 excede le=1000 → 422 Unprocessable Entity."""
    client = client_factory()
    _bootstrap_and_login(client)
    engine = _engine_from_session_local()
    partner_id = _seed_partner(engine)

    r = client.get(f"/api/integrations/{partner_id}/sophos-tenants?size=2000")
    assert r.status_code == 422, r.text


def test_search_and_geography_combined(client_factory):
    """search + geography combinados filtram por ambos (AND)."""
    client = client_factory()
    _bootstrap_and_login(client)
    engine = _engine_from_session_local()
    partner_id = _seed_partner(engine)

    _seed_selections(engine, partner_id, [
        {"external_id": "eu-acme", "name_snapshot": "Acme EU", "data_geography_snapshot": "EU"},
        {"external_id": "us-acme", "name_snapshot": "Acme US", "data_geography_snapshot": "US"},
        {"external_id": "eu-beta", "name_snapshot": "Beta EU", "data_geography_snapshot": "EU"},
    ])

    r = client.get(
        f"/api/integrations/{partner_id}/sophos-tenants",
        params={"search": "acme", "geography": "EU", "size": 100},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["external_id"] == "eu-acme"


def test_default_size_is_10(client_factory):
    """Sem ?size=, o default agora é 10 (não 200)."""
    client = client_factory()
    _bootstrap_and_login(client)
    engine = _engine_from_session_local()
    partner_id = _seed_partner(engine)

    rows = [{"external_id": f"t-{i:03d}"} for i in range(30)]
    _seed_selections(engine, partner_id, rows)

    r = client.get(f"/api/integrations/{partner_id}/sophos-tenants")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["size"] == 10
    assert len(body["items"]) == 10
    assert body["total"] == 30


def test_no_filter_params_retains_compat(client_factory):
    """Clientes sem search/geography recebem comportamento anterior."""
    client = client_factory()
    _bootstrap_and_login(client)
    engine = _engine_from_session_local()
    partner_id = _seed_partner(engine)

    _seed_selections(engine, partner_id, [
        {"external_id": "t1"},
        {"external_id": "t2"},
        {"external_id": "t3"},
    ])

    r = client.get(f"/api/integrations/{partner_id}/sophos-tenants?size=100")
    assert r.status_code == 200
    assert r.json()["total"] == 3
    assert len(r.json()["items"]) == 3
