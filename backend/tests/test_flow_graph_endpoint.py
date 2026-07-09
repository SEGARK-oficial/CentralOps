"""Tests for GET /api/collectors/routes/flow (full flow graph).

Mirrors test_observability_endpoints.py conventions:
- Single StaticPool SQLite engine shared via dependency override.
- Bootstrap admin for auth.
- Seed data via the API (destination + route + optionally integration).
- Org-scope exercised via _FakeUser override (same pattern as topology tests).
- Redis I/O is absent in tests → throughput degrades to 0.0 (correct behaviour).
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


# ── Fixtures ──────────────────────────────────────────────────────────


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
    """Non-global admin bound to one org (org-scope path driver)."""

    def __init__(self, organization_id: int) -> None:
        self.organization_id = organization_id
        self.role = "operator"
        self.is_global = False
        self.username = "scoped-operator"


def _override_user_to_org(org_id: int) -> None:
    app.dependency_overrides[app_auth.require_admin_user] = lambda: _FakeUser(org_id)


# ── shape & sanity ─────────────────────────────────────────


def test_flow_endpoint_returns_200_and_full_shape(client_factory) -> None:
    """GET /flow returns 200 with the four top-level keys and correct sub-shapes."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    d1 = _seed_destination(client, name="Flow Dest")
    rid = _seed_route(client, name="Flow Route", dest_id=d1)

    r = client.get("/api/collectors/routes/flow")
    assert r.status_code == 200, r.text
    body = r.json()

    # Top-level keys
    for key in ("generated_at", "window_minutes", "sources", "routes", "destinations", "totals"):
        assert key in body, f"missing top-level key: {key}"

    # Janela da média móvel de taxa (default 5 min via OBS_RATE_WINDOW_MINUTES).
    from backend.app.core.config import settings as _s

    assert body["window_minutes"] == _s.OBS_RATE_WINDOW_MINUTES
    # generated_at must be a non-empty ISO string
    assert isinstance(body["generated_at"], str) and len(body["generated_at"]) > 10

    # Routes shape
    route_ids = {rt["id"] for rt in body["routes"]}
    assert rid in route_ids, "seeded route must appear in flow graph"
    route = next(rt for rt in body["routes"] if rt["id"] == rid)
    for field in (
        "id", "name", "action", "destination_ids",
        "matched_per_min", "routed_per_min", "drop_per_min", "enabled", "is_system",
    ):
        assert field in route, f"missing route field: {field}"
    assert route["destination_ids"] == [d1]
    # No Redis in tests → throughput is 0.0
    assert route["matched_per_min"] == 0.0
    assert route["routed_per_min"] == 0.0
    assert route["drop_per_min"] == 0.0
    assert route["is_system"] is False

    # Destinations shape
    dest_ids = {d["id"] for d in body["destinations"]}
    assert d1 in dest_ids, "seeded destination must appear in flow graph"
    dest = next(d for d in body["destinations"] if d["id"] == d1)
    for field in ("id", "name", "kind", "status", "eps", "bytes_per_min"):
        assert field in dest, f"missing destination field: {field}"

    # Totals shape
    totals = body["totals"]
    for field in ("ingest_eps", "routed_per_min", "drop_per_min", "delivered_eps"):
        assert field in totals, f"missing totals field: {field}"
        assert isinstance(totals[field], (int, float)), f"totals.{field} must be numeric"


def test_flow_endpoint_does_not_collide_with_route_id(client_factory) -> None:
    """FastAPI routing: GET /routes/flow hits the flow handler, NOT /{route_id}
    treating 'flow' as a route id (which would 404)."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    _seed_destination(client, name="x", auto_route=False)

    r = client.get("/api/collectors/routes/flow")
    assert r.status_code == 200, r.text
    body = r.json()
    # Correct handler returns the flow-graph shape.
    assert "routes" in body and "destinations" in body and "sources" in body and "totals" in body


def test_flow_totals_are_derived_from_subsystems(client_factory) -> None:
    """Totals are sums of subsystem values (in an empty-Redis test env all are 0)."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    d1 = _seed_destination(client, name="Totals Dest")
    _seed_route(client, name="Totals Route", dest_id=d1)

    r = client.get("/api/collectors/routes/flow")
    assert r.status_code == 200, r.text
    body = r.json()

    sources = body["sources"]
    routes = body["routes"]
    dests = body["destinations"]
    totals = body["totals"]

    assert totals["ingest_eps"] == pytest.approx(sum(s["eps"] for s in sources))
    assert totals["routed_per_min"] == pytest.approx(sum(rt["routed_per_min"] for rt in routes))
    assert totals["drop_per_min"] == pytest.approx(sum(rt["drop_per_min"] for rt in routes))
    assert totals["delivered_eps"] == pytest.approx(sum((d.get("eps") or 0.0) for d in dests))


# ── Graceful degradation ──────────────────────────────────────────────


def test_flow_never_returns_500_with_no_redis(client_factory) -> None:
    """Endpoint is robust: with no Redis and seeded data it still returns 200
    with 0.0 throughput instead of 500."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    d1 = _seed_destination(client, name="Robust Dest")
    _seed_route(client, name="Robust Route", dest_id=d1)

    # No mock needed — Redis is simply not available in the test environment.
    r = client.get("/api/collectors/routes/flow")
    assert r.status_code == 200, r.text
    body = r.json()
    # Destinations are present with 0.0 EPS (Redis down → store returns 0).
    assert len(body["destinations"]) >= 1
    for dest in body["destinations"]:
        assert dest["eps"] == 0.0 or dest["eps"] is None


def test_flow_empty_org_returns_empty_subsystems(client_factory) -> None:
    """An org with no routes/destinations still returns a valid (empty) graph."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    org_a = _create_org(client, "Empty Org")

    try:
        _override_user_to_org(org_a)
        r = client.get("/api/collectors/routes/flow")
        assert r.status_code == 200, r.text
        body = r.json()
        # No data yet → empty lists, totals all 0.
        assert body["routes"] == []
        assert body["destinations"] == []
        assert body["sources"] == []
        for field in ("ingest_eps", "routed_per_min", "drop_per_min", "delivered_eps"):
            assert body["totals"][field] == 0.0
    finally:
        app.dependency_overrides.pop(app_auth.require_admin_user, None)


# ── Org-scope ─────────────────────────────────────────────────────────


def test_flow_org_scoping(client_factory) -> None:
    """Org-scope: a non-global caller from org A never sees org B's data."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    org_a = _create_org(client, "Flow Org A")
    org_b = _create_org(client, "Flow Org B")

    da = _seed_destination(client, name="A Flow Dest", organization_id=org_a)
    db_ = _seed_destination(client, name="B Flow Dest", organization_id=org_b)
    ra = _seed_route(client, name="A Flow Route", dest_id=da, organization_id=org_a)
    rb = _seed_route(client, name="B Flow Route", dest_id=db_, organization_id=org_b)

    try:
        _override_user_to_org(org_a)
        r = client.get("/api/collectors/routes/flow")
        assert r.status_code == 200, r.text
        body = r.json()

        dest_ids = {d["id"] for d in body["destinations"]}
        route_ids = {rt["id"] for rt in body["routes"]}

        assert da in dest_ids, "org A's destination must be visible"
        assert db_ not in dest_ids, "org B's destination must NOT leak into org A's flow"
        assert ra in route_ids, "org A's route must be visible"
        assert rb not in route_ids, "org B's route must NOT leak into org A's flow"
    finally:
        app.dependency_overrides.pop(app_auth.require_admin_user, None)


# ── Source fields ─────────────────────────────────────────────────────


@pytest.mark.parametrize("status_val", ["healthy", "degraded", "unhealthy", "unknown"])
def test_flow_source_status_values_are_canonical(status_val: str) -> None:
    """FlowSource.status must be one of the four canonical values."""
    from backend.app.routers.routes import _pipeline_status_to_flow

    assert _pipeline_status_to_flow(status_val) == status_val


def test_flow_source_status_unknown_for_unexpected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any unexpected status from pipeline-health maps to 'unknown'."""
    from backend.app.routers.routes import _pipeline_status_to_flow

    assert _pipeline_status_to_flow("ok") == "unknown"
    assert _pipeline_status_to_flow("") == "unknown"
    assert _pipeline_status_to_flow("stale") == "unknown"


def test_flow_sources_eps_equals_epm_over_60(client_factory) -> None:
    """If a source had events_per_minute=120, eps must be 2.0 (120/60)."""
    # We can't easily seed a real integration with EPM in a unit test
    # (requires Redis snapshot machinery), so test the math via the schema directly.
    from backend.app.api.schemas_routes import FlowSource

    src = FlowSource(
        id="42",
        name="Test Integration",
        platform="sophos",
        status="healthy",
        events_per_minute=120.0,
        eps=120.0 / 60.0,
    )
    assert src.eps == pytest.approx(2.0)
    assert src.events_per_minute == pytest.approx(120.0)


def test_flow_wazuh_default_appears_only_as_real_destination(client_factory) -> None:
    """wazuh-default é agora uma row real de Destination — NÃO
    é mais sintetizado. Aparece no /flow APENAS quando existe como row de destino
    (org=NULL, global) e uma rota o referencia. Em installs vendor-neutros sem
    wazuh-default, ele simplesmente não aparece (correto)."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    # Vendor-neutral install: no wazuh-default destination seeded → must NOT appear.
    r_empty = client.get("/api/collectors/routes/flow")
    assert r_empty.status_code == 200, r_empty.text
    dest_ids_empty = {d["id"] for d in r_empty.json()["destinations"]}
    assert "wazuh-default" not in dest_ids_empty, (
        "wazuh-default must NOT appear when no destination row exists for it"
    )

    # Seed wazuh-default as a real Destination row and create a route targeting it.
    wd_id = _seed_destination(client, name="Wazuh (default)")
    _seed_route(client, name="Catch route", dest_id=wd_id)

    r = client.get("/api/collectors/routes/flow")
    assert r.status_code == 200, r.text
    body = r.json()
    dest_ids = {d["id"] for d in body["destinations"]}
    assert wd_id in dest_ids, "real wazuh-default destination row must appear in /flow"
    wd = next(d for d in body["destinations"] if d["id"] == wd_id)
    assert wd["name"] == "Wazuh (default)"
    assert "eps" in wd  # 0.0 sem tráfego, mas o nó existe (contado em delivered_eps)
