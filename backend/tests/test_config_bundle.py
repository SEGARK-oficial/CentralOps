"""Tests for /api/collectors/config/export and /api/collectors/config/import
(config-as-code / GitOps bundle).

Conventions mirror test_destinations_router.py + test_routes_router.py:
  - Single StaticPool engine per test (StaticPool isolates sessions).
  - client_factory yields (factory_fn, TestingSessionLocal).
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

from backend.app.db.database import Base, get_session
from backend.app.main import app


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def client_factory():
    """Shared StaticPool engine so all clients within a test share the same DB."""
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


def _create_org(client: TestClient, name: str) -> dict[str, Any]:
    r = client.post(
        "/api/organizations",
        json={"name": name, "slug": name.lower().replace(" ", "-")},
    )
    assert r.status_code in (200, 201), r.text
    return r.json()


def _create_org_admin(
    client: TestClient,
    *,
    username: str,
    password: str,
    org_id: int,
) -> dict[str, Any]:
    r = client.post(
        "/api/auth/users",
        json={
            "username": username,
            "password": password,
            "display_name": username.title(),
            "role": "admin",
            "organization_id": org_id,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _login(client: TestClient, username: str, password: str) -> None:
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text


def _seed_destination(client: TestClient, name: str = "Syslog A") -> dict[str, Any]:
    # creating a destination auto-creates a broadcast route
    # ``{} → [dest]``. These config-bundle tests export/import EXPLICITLY defined
    # config and assert exact route counts/ordering, so ``auto_route: false`` keeps
    # the auto-route out of the bundle (its behaviour is covered elsewhere).
    r = client.post(
        "/api/collectors/destinations",
        json={
            "name": name,
            "kind": "syslog_rfc3164",
            "config": {"host": "h", "port": 514},
            "auto_route": False,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _seed_route(
    client: TestClient,
    dest_id: str,
    name: str = "Route A",
    priority: int = 10,
) -> dict[str, Any]:
    r = client.post(
        "/api/collectors/routes",
        json={
            "name": name,
            "condition": {"severity_id": {"gte": 3}},
            "destination_ids": [dest_id],
            "priority": priority,
            "is_final": True,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


# ── Gating tests ───────────────────────────────────────────────────────


def test_export_401_without_auth(client_factory) -> None:
    """Unauthenticated request → 401 (or 403)."""
    factory, _ = client_factory
    client = TestClient(app, raise_server_exceptions=False)

    r = client.get("/api/collectors/config/export")
    assert r.status_code in (401, 403)


# ── Export tests ───────────────────────────────────────────────────────


def test_export_empty_org(client_factory) -> None:
    """Export with no destinations/routes returns a valid empty bundle."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.get("/api/collectors/config/export")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == "1.0"
    assert "exported_at" in body
    assert body["destinations"] == []
    assert body["routes"] == []


def test_export_does_not_leak_secret_ref(client_factory) -> None:
    """The export MUST NOT include secret_ref in any destination."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    # Create a destination with a secret (hec_token).
    r = client.post(
        "/api/collectors/destinations",
        json={
            "name": "Splunk Secure",
            "kind": "splunk_hec",
            "config": {"url": "https://splunk.example.com:8088"},
            "hec_token": "super-secret-token-12345",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["has_secret"] is True
    # Verify secret_ref is not in the response already (destination endpoint).
    assert "secret_ref" not in r.json()

    export_r = client.get("/api/collectors/config/export")

    assert export_r.status_code == 200, export_r.text
    body = export_r.json()
    assert len(body["destinations"]) == 1
    dest = body["destinations"][0]
    # has_secret signals credential exists without leaking it.
    assert dest["has_secret"] is True
    # secret_ref must not appear anywhere in the response.
    assert "secret_ref" not in dest
    raw_text = export_r.text
    assert "super-secret-token" not in raw_text
    assert "secret_ref" not in raw_text


def test_export_includes_destinations_and_routes(client_factory) -> None:
    """Export bundle contains the seeded destination + route."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    dest = _seed_destination(client, "My Dest")
    _seed_route(client, dest["id"], "My Route", priority=20)

    r = client.get("/api/collectors/config/export")

    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["destinations"]) == 1
    assert body["destinations"][0]["name"] == "My Dest"
    assert len(body["routes"]) == 1
    assert body["routes"][0]["name"] == "My Route"
    assert body["routes"][0]["priority"] == 20


# ── Import dry_run tests ───────────────────────────────────────────────


def _make_bundle(
    *,
    dest_name: str = "Bundle Dest",
    route_name: str = "Bundle Route",
    org_id: int | None = None,
) -> dict[str, Any]:
    """Helper: minimal valid bundle with one destination and one route."""
    return {
        "version": "1.0",
        "exported_at": "2026-06-17T00:00:00Z",
        "organization_id": org_id,
        "destinations": [
            {
                "id": "fake-dest-id",
                "name": dest_name,
                "kind": "syslog_rfc3164",
                "enabled": True,
                "config": {"host": "syslog.local", "port": 514},
                "delivery": {},
                "config_version": "v1",
                "organization_id": org_id,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "has_secret": False,
            }
        ],
        "routes": [
            {
                "id": "fake-route-id",
                "name": route_name,
                "priority": 50,
                "condition": {"severity_id": {"gte": 3}},
                "action": "route",
                "destination_ids": ["fake-dest-id"],
                "is_final": True,
                "canary_percent": 100,
                "transform_ref": None,
                "pii_redaction": None,
                "enabled": True,
                "organization_id": org_id,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "unreachable": False,
            }
        ],
    }


def test_import_dry_run_shows_diff_without_persisting(client_factory) -> None:
    """dry_run=true computes the diff (all 'created') but does NOT write to DB."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    bundle = _make_bundle()

    r = client.post(
        "/api/collectors/config/import",
        json={"bundle": bundle, "dry_run": True},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True
    assert body["destinations"][0]["status"] == "created"
    assert body["routes"][0]["status"] == "created"

    # Verify nothing was actually persisted.
    export_r = client.get("/api/collectors/config/export")
    assert export_r.json()["destinations"] == []
    assert export_r.json()["routes"] == []


def test_import_dry_run_invalid_route_condition_422(client_factory) -> None:
    """A bundle with an invalid route condition fails validation (422) in dry_run."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    bundle = _make_bundle()
    bundle["routes"][0]["condition"] = {"invalid_operator": {"bad": True}}

    r = client.post(
        "/api/collectors/config/import",
        json={"bundle": bundle, "dry_run": True},
    )

    assert r.status_code == 422, r.text


def test_import_dry_run_invalid_action_drop_with_dest_422(client_factory) -> None:
    """action=drop + destination_ids present → 422 (routing engine invariant)."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    bundle = _make_bundle()
    bundle["routes"][0]["action"] = "drop"
    # destination_ids still populated — violates the routing invariant.

    r = client.post(
        "/api/collectors/config/import",
        json={"bundle": bundle, "dry_run": True},
    )

    assert r.status_code == 422, r.text


@pytest.mark.parametrize(
    "bad_key",
    [
        "src_ip",  # campo do log, não label de roteamento → assinatura degenerada
        "vendor,user",  # uma boa e uma ruim: o bundle inteiro cai
        "event_id",  # único por evento → a supressão nunca dispararia
    ],
)
def test_import_rejects_invalid_suppress_key_422(client_factory, bad_key) -> None:
    """suppress_key fora da allowlist de roteamento derruba o import.

    Sem esta checagem o bundle ressuscitava uma assinatura que agrupa tráfego
    demais e descarta em silêncio (a supressão roda ANTES do roteamento)."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    bundle = _make_bundle(route_name="Rota Suspeita")
    bundle["routes"][0]["suppress_key"] = bad_key
    bundle["routes"][0]["suppress_allow"] = 10

    r = client.post(
        "/api/collectors/config/import",
        json={"bundle": bundle, "dry_run": True},
    )

    assert r.status_code == 422, r.text
    body = r.json()
    assert body["error"]["code"] == "config_bundle.invalid_route_suppress_key"
    # Num bundle com dezenas de rotas, um erro anônimo é inútil.
    assert "Rota Suspeita" in body["detail"]
    assert body["error"]["details"]["route_name"] == "Rota Suspeita"


def test_import_apply_rejects_invalid_suppress_key_without_writing(client_factory) -> None:
    """dry_run=false com chave inválida: 422 ANTES de tocar no banco."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    bundle = _make_bundle()
    bundle["routes"][0]["suppress_key"] = "dst_ip"

    r = client.post(
        "/api/collectors/config/import",
        json={"bundle": bundle, "dry_run": False},
    )
    assert r.status_code == 422, r.text

    export = client.get("/api/collectors/config/export").json()
    assert export["routes"] == []
    assert export["destinations"] == []


def test_import_applies_valid_suppress_key(client_factory) -> None:
    """Caminho feliz: chave válida atravessa o import e volta no export."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    bundle = _make_bundle()
    bundle["routes"][0]["protect_detection"] = False
    bundle["routes"][0]["suppress_key"] = "vendor,severity_id"
    bundle["routes"][0]["suppress_allow"] = 5
    bundle["routes"][0]["suppress_window_s"] = 60

    r = client.post(
        "/api/collectors/config/import",
        json={"bundle": bundle, "dry_run": False},
    )
    assert r.status_code == 200, r.text

    exported = client.get("/api/collectors/config/export").json()["routes"][0]
    assert exported["suppress_key"] == "vendor,severity_id"
    assert exported["suppress_allow"] == 5


@pytest.mark.parametrize("value", [None, ""])
def test_import_allows_route_without_suppression(client_factory, value) -> None:
    """None/vazia = supressão desligada — legítimo, não pode virar erro."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    bundle = _make_bundle()
    bundle["routes"][0]["suppress_key"] = value

    r = client.post(
        "/api/collectors/config/import",
        json={"bundle": bundle, "dry_run": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["routes"][0]["status"] == "created"


def test_import_allows_route_with_suppress_key_absent(client_factory) -> None:
    """Bundle antigo, sem o campo: o import não pode exigir o que não existe."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    bundle = _make_bundle()
    bundle["routes"][0].pop("suppress_key", None)
    assert "suppress_key" not in bundle["routes"][0]

    r = client.post(
        "/api/collectors/config/import",
        json={"bundle": bundle, "dry_run": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["routes"][0]["status"] == "created"


# ── Import apply tests ─────────────────────────────────────────────────


def test_import_apply_creates_destination_and_route(client_factory) -> None:
    """apply mode creates destination + route; both visible via export."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    bundle = _make_bundle(dest_name="ApplyDest", route_name="ApplyRoute")

    r = client.post(
        "/api/collectors/config/import",
        json={"bundle": bundle, "dry_run": False},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is False
    assert body["destinations"][0]["status"] == "created"
    assert body["routes"][0]["status"] == "created"

    # Confirm round-trip: they are now visible in the DB.
    export_r = client.get("/api/collectors/config/export")
    export_body = export_r.json()
    assert any(d["name"] == "ApplyDest" for d in export_body["destinations"])
    assert any(r["name"] == "ApplyRoute" for r in export_body["routes"])


def test_import_apply_updates_existing_destination(client_factory) -> None:
    """apply on an existing destination with changed config → 'updated'."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    # Seed a destination first via the destinations endpoint.
    dest = _seed_destination(client, "UpdateMe")
    dest_id = dest["id"]

    bundle = _make_bundle(dest_name="UpdateMe", route_name="Update Route")
    # Change the port to force a diff.
    bundle["destinations"][0]["config"]["port"] = 9999

    r = client.post(
        "/api/collectors/config/import",
        json={"bundle": bundle, "dry_run": False},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["destinations"][0]["status"] == "updated"
    assert body["destinations"][0]["id"] == dest_id

    # Confirm the config was actually written.
    get_r = client.get(f"/api/collectors/destinations/{dest_id}")
    assert get_r.status_code == 200
    assert get_r.json()["config"]["port"] == 9999


# ── Round-trip idempotence ─────────────────────────────────────────────


def test_round_trip_export_import_apply_is_idempotent(client_factory) -> None:
    """export → import apply → export again → second import = all unchanged."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    # Seed state.
    dest = _seed_destination(client, "RoundTripDest")
    _seed_route(client, dest["id"], "RoundTripRoute", priority=30)

    # 1st export.
    export1 = client.get("/api/collectors/config/export").json()

    # 1st apply — should be all 'unchanged' (data already matches).
    apply1 = client.post(
        "/api/collectors/config/import",
        json={"bundle": export1, "dry_run": False},
    ).json()

    assert apply1["destinations"][0]["status"] == "unchanged"
    assert apply1["routes"][0]["status"] == "unchanged"

    # 2nd export — still identical.
    export2 = client.get("/api/collectors/config/export").json()

    # 2nd apply — also all 'unchanged'.
    apply2 = client.post(
        "/api/collectors/config/import",
        json={"bundle": export2, "dry_run": False},
    ).json()

    assert apply2["destinations"][0]["status"] == "unchanged"
    assert apply2["routes"][0]["status"] == "unchanged"


def test_round_trip_creates_then_second_import_unchanged(client_factory) -> None:
    """Import apply (create) followed by second apply (same bundle) = unchanged."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    bundle = _make_bundle(dest_name="IdemDest", route_name="IdemRoute")

    # First apply — creates both.
    first = client.post(
        "/api/collectors/config/import",
        json={"bundle": bundle, "dry_run": False},
    ).json()

    assert first["destinations"][0]["status"] == "created"
    assert first["routes"][0]["status"] == "created"

    # Second apply — same bundle → both unchanged.
    second = client.post(
        "/api/collectors/config/import",
        json={"bundle": bundle, "dry_run": False},
    ).json()

    assert second["destinations"][0]["status"] == "unchanged"
    assert second["routes"][0]["status"] == "unchanged"


# ── Org-scope tests ───────────────────────────────────────────────────


def test_import_bundle_org_id_drives_row_creation_org(client_factory) -> None:
    """Global admin importing a bundle with a specific organization_id creates
    rows for that org, not NULL / the global default."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    org_a = _create_org(client, "Org Alpha")

    # Build a bundle explicitly scoped to org_a.
    bundle = _make_bundle(dest_name="OrgADest", route_name="OrgARoute", org_id=org_a["id"])

    r = client.post(
        "/api/collectors/config/import",
        json={"bundle": bundle, "dry_run": False},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["destinations"][0]["status"] == "created"
    assert body["routes"][0]["status"] == "created"

    # Re-export: the created rows should appear (global admin sees all).
    export_r = client.get("/api/collectors/config/export")

    export_body = export_r.json()
    dest_names = [d["name"] for d in export_body["destinations"]]
    route_names = [r["name"] for r in export_body["routes"]]
    assert "OrgADest" in dest_names
    assert "OrgARoute" in route_names


def test_export_global_admin_sees_all_orgs(client_factory) -> None:
    """A global admin export contains destinations from multiple orgs."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    org_a = _create_org(client, "Org Alpha")
    org_b = _create_org(client, "Org Beta")

    # Create a destination explicitly for each org.
    r_a = client.post(
        "/api/collectors/destinations",
        json={
            "name": "Org-A Dest",
            "kind": "syslog_rfc3164",
            "config": {"host": "ha", "port": 514},
            "organization_id": org_a["id"],
        },
    )
    assert r_a.status_code == 201, r_a.text

    r_b = client.post(
        "/api/collectors/destinations",
        json={
            "name": "Org-B Dest",
            "kind": "syslog_rfc3164",
            "config": {"host": "hb", "port": 514},
            "organization_id": org_b["id"],
        },
    )
    assert r_b.status_code == 201, r_b.text

    export_r = client.get("/api/collectors/config/export")

    assert export_r.status_code == 200, export_r.text
    body = export_r.json()
    dest_names = {d["name"] for d in body["destinations"]}
    # Global admin sees both orgs.
    assert "Org-A Dest" in dest_names
    assert "Org-B Dest" in dest_names


# ── Parametrized edge cases ────────────────────────────────────────────


@pytest.mark.parametrize(
    "route_override,expected_status",
    [
        # Valid: empty condition = match-all.
        ({"condition": {}}, 200),
        # Valid: known operator.
        ({"condition": {"severity_id": {"gte": 1}}}, 200),
        # Invalid: unknown top-level key.
        ({"condition": {"bad_key": "value"}}, 422),
        # Invalid: drop + destination_ids.
        ({"action": "drop", "destination_ids": ["x"]}, 422),
        # Invalid: route + empty destination_ids.
        ({"action": "route", "destination_ids": []}, 422),
    ],
)
def test_import_dry_run_route_validation(
    client_factory,
    route_override: dict,
    expected_status: int,
) -> None:
    """Parametrized: validate route conditions/actions in dry_run mode."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    bundle = _make_bundle()
    bundle["routes"][0].update(route_override)

    r = client.post(
        "/api/collectors/config/import",
        json={"bundle": bundle, "dry_run": True},
    )

    assert r.status_code == expected_status, r.text


# ── Export secret_ref never appears ───────────────────────────────────


def test_export_secret_ref_never_in_response_text(client_factory) -> None:
    """The literal string 'secret_ref' must not appear anywhere in export JSON."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    _seed_destination(client, "NormalDest")

    r = client.get("/api/collectors/config/export")

    assert r.status_code == 200, r.text
    assert "secret_ref" not in r.text
