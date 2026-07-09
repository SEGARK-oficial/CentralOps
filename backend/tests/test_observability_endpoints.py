"""Tests for the observability READ endpoints:

  - GET /api/collectors/destinations/health   — batch destination health
  - GET /api/collectors/routes/topology        — flow graph w/ throughput

Mirrors the conventions of test_destinations_router.py / test_routes_router.py:
single StaticPool engine shared across all clients in a test, bootstrap admin,
seed via the API. Org-scoping is exercised by overriding ``require_admin_user``
with a non-global admin bound to a specific org (admins are otherwise always
global-scoped, so the override is the only way to drive the org-scope path at
the endpoint layer — same property the repository-level scope tests assert).
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

from backend.app.core import auth as app_auth
from backend.app.db.database import Base, get_session
from backend.app.main import app


# ── Fixture ───────────────────────────────────────────────────────────


@pytest.fixture()
def client_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_session():
        db = TestingSessionLocal()
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

    yield factory, TestingSessionLocal

    for c in clients:
        c.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


# ── Auth / seed helpers ───────────────────────────────────────────────


def _bootstrap_admin(client: TestClient) -> None:
    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPassword123!", "display_name": "Admin"},
    )
    assert r.status_code == 200, r.text


def _create_org(client: TestClient, name: str) -> int:
    r = client.post(
        "/api/organizations",
        json={"name": name, "slug": name.lower().replace(" ", "-")},
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _seed_destination(
    client: TestClient, *, name: str, organization_id: int | None = None, auto_route: bool = False
) -> str:
    body: dict[str, Any] = {
        "name": name,
        "kind": "syslog_rfc3164",
        "config": {"host": "h", "port": 514},
        "auto_route": auto_route,
    }
    if organization_id is not None:
        body["organization_id"] = organization_id
    r = client.post("/api/collectors/destinations", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _seed_route(
    client: TestClient, *, name: str, dest_id: str, organization_id: int | None = None
) -> str:
    body: dict[str, Any] = {
        "name": name,
        "condition": {},
        "destination_ids": [dest_id],
    }
    if organization_id is not None:
        body["organization_id"] = organization_id
    r = client.post("/api/collectors/routes", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


class _FakeUser:
    """Minimal AppUser stand-in for a NON-global admin bound to one org.

    ``has_global_scope`` returns False for role!=admin without is_global, so this
    drives the org-scope path at the endpoint layer (real admins are global)."""

    def __init__(self, organization_id: int) -> None:
        self.organization_id = organization_id
        self.role = "operator"
        self.is_global = False
        self.username = "scoped-operator"


def _override_user_to_org(org_id: int) -> None:
    app.dependency_overrides[app_auth.require_admin_user] = lambda: _FakeUser(org_id)


# ── Entregável 1: batch destination health ────────────────────────────


def test_batch_health_returns_all_visible_destinations(client_factory) -> None:
    """One call returns the health of every destination visible to the caller."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    d1 = _seed_destination(client, name="Dest One")
    d2 = _seed_destination(client, name="Dest Two")

    r = client.get("/api/collectors/destinations/health")
    assert r.status_code == 200, r.text
    body = r.json()
    ids = {item["destination_id"] for item in body["items"]}
    assert {d1, d2}.issubset(ids)
    assert body["total"] == len(body["items"])

    # Item shape parity with GET /{id}/health + name/kind.
    item = next(i for i in body["items"] if i["destination_id"] == d1)
    for field in (
        "destination_id", "name", "kind", "status", "enabled",
        "breaker_state", "dlq_total", "dlq_24h", "eps", "bytes_per_min",
    ):
        assert field in item, f"missing {field} in batch health item"
    assert item["name"] == "Dest One"
    assert item["kind"] == "syslog_rfc3164"
    # No live Redis in the test env → breaker unknown, status unknown (enabled).
    assert item["status"] in {"healthy", "degraded", "unhealthy", "unknown"}


def test_batch_health_item_matches_single_health_endpoint(client_factory) -> None:
    """The batch item for a destination equals the single /{id}/health payload
    (proves the shared compute helper — no divergent logic)."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    d1 = _seed_destination(client, name="Parity Dest")

    single = client.get(f"/api/collectors/destinations/{d1}/health")
    assert single.status_code == 200, single.text
    single_body = single.json()

    batch = client.get("/api/collectors/destinations/health").json()
    item = next(i for i in batch["items"] if i["destination_id"] == d1)

    for field in ("status", "enabled", "breaker_state", "dlq_total", "dlq_24h", "eps", "bytes_per_min"):
        assert item[field] == single_body[field], f"divergence on {field}"


def test_batch_health_route_does_not_collide_with_id(client_factory) -> None:
    """FastAPI routing: GET /destinations/health hits the BATCH handler, not the
    /{destination_id} handler treating 'health' as an id (which would 404)."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    _seed_destination(client, name="Some Dest")

    r = client.get("/api/collectors/destinations/health")
    assert r.status_code == 200, r.text
    # Batch shape (has items/total) — NOT a single-destination 404 nor a
    # DestinationRead for an id literally named "health".
    body = r.json()
    assert "items" in body and "total" in body
    # And a bogus id still 404s through the /{id} handler (sanity).
    assert client.get("/api/collectors/destinations/health/health").status_code in (404, 405)


def test_batch_health_org_scoping(client_factory) -> None:
    """A non-global caller from org A sees only org A's destinations + global
    (NULL) rows — never org B's. Same visibility as list_destinations."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    org_a = _create_org(client, "Health Org A")
    org_b = _create_org(client, "Health Org B")

    da = _seed_destination(client, name="A Dest", organization_id=org_a)
    db_ = _seed_destination(client, name="B Dest", organization_id=org_b)
    dg = _seed_destination(client, name="Global Dest")  # organization_id NULL

    try:
        _override_user_to_org(org_a)
        r = client.get("/api/collectors/destinations/health")
        assert r.status_code == 200, r.text
        ids = {i["destination_id"] for i in r.json()["items"]}
        assert da in ids, "org A's own destination must be visible"
        assert dg in ids, "global (NULL) destination must be visible"
        assert db_ not in ids, "org B's destination must NOT be visible to org A"
    finally:
        app.dependency_overrides.pop(app_auth.require_admin_user, None)


# ── Entregável 2: routing topology ────────────────────────────────────


def test_topology_returns_routes_and_destinations(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    d1 = _seed_destination(client, name="Topo Dest")
    rid = _seed_route(client, name="Topo Route", dest_id=d1)

    r = client.get("/api/collectors/routes/topology")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "destinations" in body and "routes" in body

    dest_ids = {d["id"] for d in body["destinations"]}
    assert d1 in dest_ids
    dest = next(d for d in body["destinations"] if d["id"] == d1)
    for field in ("id", "name", "kind", "status", "eps", "bytes_per_min"):
        assert field in dest, f"missing {field} in topology destination"

    route_ids = {rt["id"] for rt in body["routes"]}
    assert rid in route_ids
    route = next(rt for rt in body["routes"] if rt["id"] == rid)
    for field in (
        "id", "name", "action", "destination_ids",
        "matched_per_min", "routed_per_min", "drop_per_min", "enabled", "is_system",
    ):
        assert field in route, f"missing {field} in topology route"
    assert route["destination_ids"] == [d1]
    assert route["is_system"] is False
    # Throughput defaults to 0.0 with no recorded series.
    assert route["matched_per_min"] == 0.0
    assert route["routed_per_min"] == 0.0
    assert route["drop_per_min"] == 0.0


def test_topology_route_does_not_collide_with_id(client_factory) -> None:
    """GET /routes/topology hits the topology handler, not GET /{route_id}
    treating 'topology' as a route id (which would 404)."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    _seed_destination(client, name="x", auto_route=False)

    r = client.get("/api/collectors/routes/topology")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "routes" in body and "destinations" in body


def test_topology_org_scoping(client_factory) -> None:
    """Topology is org-scoped: org A's caller never sees org B's routes/dests."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    org_a = _create_org(client, "Topo Org A")
    org_b = _create_org(client, "Topo Org B")

    da = _seed_destination(client, name="A Topo Dest", organization_id=org_a)
    db_ = _seed_destination(client, name="B Topo Dest", organization_id=org_b)
    ra = _seed_route(client, name="A Topo Route", dest_id=da, organization_id=org_a)
    rb = _seed_route(client, name="B Topo Route", dest_id=db_, organization_id=org_b)

    try:
        _override_user_to_org(org_a)
        r = client.get("/api/collectors/routes/topology")
        assert r.status_code == 200, r.text
        body = r.json()
        dest_ids = {d["id"] for d in body["destinations"]}
        route_ids = {rt["id"] for rt in body["routes"]}
        assert da in dest_ids and ra in route_ids, "org A's own rows visible"
        assert db_ not in dest_ids, "org B's destination leaked into topology"
        assert rb not in route_ids, "org B's route leaked into topology"
    finally:
        app.dependency_overrides.pop(app_auth.require_admin_user, None)


# ── Defesa-em-profundidade: rota não pode referenciar destino cross-org ──


def test_route_create_rejects_cross_org_destination(client_factory) -> None:
    """Um caller NÃO-global (org A) não pode CRIAR uma rota referenciando o destino
    de OUTRA org (B) — 422 "not found" (mesma resposta de inexistente: não revela
    a existência cross-org, fecha a enumeração). Pode referenciar o próprio + o
    global. O caller GLOBAL pode referenciar B (comportamento atual preservado)."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    org_a = _create_org(client, "Route Org A")
    org_b = _create_org(client, "Route Org B")

    da = _seed_destination(client, name="A Route Dest", organization_id=org_a)
    db_ = _seed_destination(client, name="B Route Dest", organization_id=org_b)
    dg = _seed_destination(client, name="Global Route Dest")  # organization_id NULL

    # Caller global (admin real) PODE referenciar B — não regredir.
    rg = client.post(
        "/api/collectors/routes",
        json={"name": "global-refs-b", "condition": {}, "destination_ids": [db_]},
    )
    assert rg.status_code == 201, rg.text

    try:
        _override_user_to_org(org_a)
        # Org A → destino da org B: 422 "not found" (sem revelar existência).
        r_bad = client.post(
            "/api/collectors/routes",
            json={"name": "a-refs-b", "condition": {}, "destination_ids": [db_]},
        )
        assert r_bad.status_code == 422, r_bad.text
        assert r_bad.json()["error"]["code"] == "route.destination_not_found"
        assert r_bad.json()["error"]["details"]["destination_id"] == db_
        # Org A → próprio destino + global: ok.
        r_ok = client.post(
            "/api/collectors/routes",
            json={"name": "a-refs-a", "condition": {}, "destination_ids": [da, dg]},
        )
        assert r_ok.status_code == 201, r_ok.text
    finally:
        app.dependency_overrides.pop(app_auth.require_admin_user, None)


def test_route_update_rejects_cross_org_destination(client_factory) -> None:
    """O mesmo escopo vale no UPDATE: org A não pode repontar sua rota para o
    destino da org B."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    org_a = _create_org(client, "Upd Org A")
    org_b = _create_org(client, "Upd Org B")
    da = _seed_destination(client, name="A Upd Dest", organization_id=org_a)
    db_ = _seed_destination(client, name="B Upd Dest", organization_id=org_b)
    ra = _seed_route(client, name="A Upd Route", dest_id=da, organization_id=org_a)

    try:
        _override_user_to_org(org_a)
        r = client.put(
            f"/api/collectors/routes/{ra}",
            json={"destination_ids": [db_]},
        )
        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == "route.destination_not_found"
        assert r.json()["error"]["details"]["destination_id"] == db_
    finally:
        app.dependency_overrides.pop(app_auth.require_admin_user, None)
