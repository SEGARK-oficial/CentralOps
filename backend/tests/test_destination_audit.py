"""Tests for the append-only destination CRUD audit trail.

Mirrors test_destinations_router.py conventions:
  - Single StaticPool engine shared across fixtures so the repo sees the same
    DB state (critical: avoids the StaticPool gotcha).
  - client_factory yields (factory_fn, SessionLocal) so callers can read the
    audit table directly via the session.

Coverage:
  - create → exactly one 'create' audit row
  - update → a 'update' audit row appended
  - delete → a 'delete' audit row appended (snapshot captured before removal)
  - secret NEVER appears in any audit snapshot (hec_token round-trip)
  - trail is newest-first
  - GET /{id}/audit requires admin and respects org-scope (cross-tenant empty)
"""

from __future__ import annotations

import json
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

from backend.app.db import models
from backend.app.db.database import Base, get_session
from backend.app.main import app


# ── Fixture ───────────────────────────────────────────────────────────


@pytest.fixture()
def client_factory():
    """Shared StaticPool engine so all test clients see the same DB state."""
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


# ── Auth helpers ──────────────────────────────────────────────────────


def _bootstrap_admin(client: TestClient) -> dict[str, Any]:
    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPassword123!", "display_name": "Admin"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _create_org(client: TestClient, name: str) -> dict[str, Any]:
    r = client.post(
        "/api/organizations",
        json={"name": name, "slug": name.lower().replace(" ", "-")},
    )
    assert r.status_code in (200, 201), r.text
    return r.json()


# ── Payload helpers ───────────────────────────────────────────────────


def _syslog_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "Audit Syslog",
        "kind": "syslog_rfc3164",
        "config": {"host": "syslog.local", "port": 514},
    }
    base.update(overrides)
    return base


def _splunk_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "Audit Splunk",
        "kind": "splunk_hec",
        "config": {"url": "https://splunk.example.com:8088"},
    }
    base.update(overrides)
    return base


def _audit_rows(SessionLocal, destination_id: str) -> list[models.DestinationAuditLog]:
    db = SessionLocal()
    try:
        return (
            db.query(models.DestinationAuditLog)
            .filter(models.DestinationAuditLog.destination_id == destination_id)
            .order_by(models.DestinationAuditLog.created_at.asc())
            .all()
        )
    finally:
        db.close()


# ── create → 'create' audit row ───────────────────────────────────────


def test_create_writes_single_create_audit_row(client_factory) -> None:
    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)

    created = client.post("/api/collectors/destinations", json=_syslog_payload()).json()
    dest_id = created["id"]

    rows = _audit_rows(SessionLocal, dest_id)
    assert len(rows) == 1
    assert rows[0].action == "create"
    assert rows[0].actor == "admin"
    snap = json.loads(rows[0].snapshot)
    assert snap["name"] == "Audit Syslog"
    assert snap["kind"] == "syslog_rfc3164"
    assert snap["has_secret"] is False


# ── update → 'update' audit row ───────────────────────────────────────


def test_update_appends_update_audit_row(client_factory) -> None:
    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)

    created = client.post("/api/collectors/destinations", json=_syslog_payload()).json()
    dest_id = created["id"]

    r = client.put(
        f"/api/collectors/destinations/{dest_id}",
        json={"name": "Renamed Syslog"},
    )
    assert r.status_code == 200, r.text

    rows = _audit_rows(SessionLocal, dest_id)
    actions = [row.action for row in rows]
    assert actions == ["create", "update"]
    update_snap = json.loads(rows[1].snapshot)
    assert update_snap["name"] == "Renamed Syslog"


# ── delete → 'delete' audit row ───────────────────────────────────────


def test_delete_appends_delete_audit_row(client_factory) -> None:
    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)

    created = client.post("/api/collectors/destinations", json=_syslog_payload()).json()
    dest_id = created["id"]

    r = client.delete(f"/api/collectors/destinations/{dest_id}")
    assert r.status_code == 204, r.text

    # The destination row is gone, but the audit trail survives (forensics).
    rows = _audit_rows(SessionLocal, dest_id)
    actions = [row.action for row in rows]
    assert actions == ["create", "delete"]
    delete_snap = json.loads(rows[1].snapshot)
    assert delete_snap["name"] == "Audit Syslog"


# ── secret NEVER in any snapshot ──────────────────────────────────────


def test_secret_never_appears_in_audit_snapshot(client_factory) -> None:
    """A destination created/updated with a hec_token must never leak the token
    (nor the encrypted secret_ref ciphertext) into any audit snapshot."""
    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)

    secret = "super-secret-hec-token-DO-NOT-LEAK"

    created = client.post(
        "/api/collectors/destinations",
        json=_splunk_payload(hec_token=secret),
    ).json()
    dest_id = created["id"]

    # Rotate the token via update → another audit row.
    r = client.put(
        f"/api/collectors/destinations/{dest_id}",
        json={"hec_token": "another-secret-token-LEAK-CHECK"},
    )
    assert r.status_code == 200, r.text

    # Delete → third audit row.
    assert client.delete(f"/api/collectors/destinations/{dest_id}").status_code == 204

    rows = _audit_rows(SessionLocal, dest_id)
    assert [row.action for row in rows] == ["create", "update", "delete"]
    for row in rows:
        blob = row.snapshot
        assert secret not in blob, f"plaintext token leaked in {row.action} snapshot"
        assert "another-secret-token-LEAK-CHECK" not in blob
        # has_secret reflects credential presence without exposing it.
        snap = json.loads(blob)
        assert "secret_ref" not in json.dumps(snap.get("config", {}))
        if row.action != "delete":
            assert snap["has_secret"] is True


# ── trail newest-first via repository ─────────────────────────────────


def test_audit_trail_is_newest_first(client_factory) -> None:
    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)

    created = client.post("/api/collectors/destinations", json=_syslog_payload()).json()
    dest_id = created["id"]
    client.put(f"/api/collectors/destinations/{dest_id}", json={"enabled": False})

    from backend.app.db import repository

    db = SessionLocal()
    try:
        repo = repository.DestinationRepository(db)
        trail = repo.audit_trail(dest_id)
        assert [t.action for t in trail] == ["update", "create"]
    finally:
        db.close()


# ── GET /{id}/audit endpoint ──────────────────────────────────────────


def test_audit_endpoint_returns_trail(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    created = client.post("/api/collectors/destinations", json=_syslog_payload()).json()
    dest_id = created["id"]
    client.put(f"/api/collectors/destinations/{dest_id}", json={"name": "Updated"})

    r = client.get(f"/api/collectors/destinations/{dest_id}/audit")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["destination_id"] == dest_id
    assert body["total"] == 2
    # Newest first.
    assert [e["action"] for e in body["entries"]] == ["update", "create"]
    # Snapshots are scrubbed structures (dicts), never secret strings.
    for entry in body["entries"]:
        assert isinstance(entry["snapshot"], dict)
        assert "has_secret" in entry["snapshot"]
        assert entry["actor"] == "admin"


def test_audit_endpoint_requires_admin(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    created = client.post("/api/collectors/destinations", json=_syslog_payload()).json()
    dest_id = created["id"]

    # Anonymous (no session) → 401.
    anon = factory()
    r = anon.get(f"/api/collectors/destinations/{dest_id}/audit")
    assert r.status_code == 401, r.text


def test_audit_endpoint_unknown_destination_404(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.get("/api/collectors/destinations/does-not-exist/audit")
    assert r.status_code == 404, r.text


def test_audit_endpoint_respects_org_scope(client_factory) -> None:
    """The /audit endpoint enforces org-scoping via ``_assert_visible``.

    All admin users are globally-scoped (``tenant.has_global_scope`` is True
    for any role=admin), so the cross-tenant 404 path can only be exercised by
    a NON-global user. We assert the enforcement point directly: a
    tenant-scoped (non-global) user hitting a destination owned by a different
    org gets a 404 from ``_assert_visible`` — exactly what the endpoint calls
    before reading the audit trail.
    """
    import types

    from backend.app.core.errors import ApiError
    from backend.app.routers.destinations import _assert_visible

    factory, SessionLocal = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)

    org_a = _create_org(admin_client, "AuditOrg Alpha")
    org_b = _create_org(admin_client, "AuditOrg Beta")

    dest_b = admin_client.post(
        "/api/collectors/destinations",
        json=_syslog_payload(name="OrgB Audit Dest", organization_id=org_b["id"]),
    ).json()
    dest_b_id = dest_b["id"]

    db = SessionLocal()
    try:
        from backend.app.db import repository

        row = repository.DestinationRepository(db).get(dest_b_id)
        assert row is not None

        # Non-global user scoped to org A → cross-tenant row is invisible (404).
        org_a_user = types.SimpleNamespace(
            role="operator", is_global=False, organization_id=org_a["id"]
        )
        with pytest.raises(ApiError) as exc:
            _assert_visible(row, org_a_user)  # type: ignore[arg-type]
        assert exc.value.status_code == 404
        assert exc.value.code == "destination.not_found"

        # The owning org (org B) DOES see it.
        org_b_user = types.SimpleNamespace(
            role="operator", is_global=False, organization_id=org_b["id"]
        )
        assert _assert_visible(row, org_b_user) is row  # type: ignore[arg-type]
    finally:
        db.close()
