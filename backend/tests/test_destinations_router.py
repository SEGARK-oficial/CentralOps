"""Tests for /api/collectors/destinations.

Mirrors test_collector_config_router.py conventions:
  - Single StaticPool engine shared across all fixtures in a test so the
    repo sees seeded rows (critical: avoids the StaticPool gotcha).
  - client_factory yields (factory_fn, SessionLocal) so callers can seed
    rows directly via the session.
  - POST /{id}/test must NOT populate destination_cache._cache.
"""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

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


def _login(client: TestClient, username: str, password: str) -> None:
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text


def _create_org(client: TestClient, name: str) -> dict[str, Any]:
    r = client.post(
        "/api/organizations",
        json={"name": name, "slug": name.lower().replace(" ", "-")},
    )
    assert r.status_code in (200, 201), r.text
    return r.json()


def _create_admin_user(
    client: TestClient,
    *,
    username: str,
    password: str,
    organization_id: int | None = None,
) -> dict[str, Any]:
    r = client.post(
        "/api/auth/users",
        json={
            "username": username,
            "password": password,
            "display_name": username.title(),
            "role": "admin",
            "organization_id": organization_id,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


# ── Helper: valid splunk_hec payload ─────────────────────────────────


def _splunk_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "Test Splunk",
        "kind": "splunk_hec",
        "config": {"url": "https://splunk.example.com:8088"},
    }
    base.update(overrides)
    return base


def _syslog_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "Test Syslog",
        "kind": "syslog_rfc3164",
        "config": {"host": "syslog.local", "port": 514},
    }
    base.update(overrides)
    return base


# ── /destination-types ────────────────────────────────────────────────


def test_destination_types_returns_all_kinds(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.get("/api/collectors/destinations/destination-types")
    assert r.status_code == 200, r.text
    kinds = {item["kind"] for item in r.json()}
    # The 4 built-in kinds registered in destinations/registry.py
    assert "syslog_rfc3164" in kinds
    assert "syslog_rfc5424" in kinds
    assert "jsonl" in kinds
    assert "splunk_hec" in kinds


def test_destination_types_includes_config_schema(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.get("/api/collectors/destinations/destination-types")
    assert r.status_code == 200
    splunk = next(item for item in r.json() if item["kind"] == "splunk_hec")
    assert "config_schema" in splunk
    assert isinstance(splunk["config_schema"], dict)
    assert splunk["required_secrets"] == ["hec_token"]


def test_destination_types_requires_admin(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    r = client.get("/api/collectors/destinations/destination-types")
    assert r.status_code == 401


# ── POST (create) ─────────────────────────────────────────────────────


def test_create_destination_happy_path(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.post("/api/collectors/destinations", json=_syslog_payload())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "Test Syslog"
    assert body["kind"] == "syslog_rfc3164"
    assert body["enabled"] is True
    assert "id" in body
    assert body["config"]["host"] == "syslog.local"
    assert "secret_ref" not in body
    assert "hec_token" not in body
    assert body["has_secret"] is False


def test_create_destination_invalid_kind_422(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.post(
        "/api/collectors/destinations",
        json={"name": "Bad Kind", "kind": "nonexistent_kind", "config": {}},
    )
    assert r.status_code == 422, r.text


def test_create_destination_invalid_config_422(client_factory) -> None:
    """Config that fails the kind's config_schema must return 422."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    # splunk_hec requires 'url' field
    r = client.post(
        "/api/collectors/destinations",
        json={
            "name": "Bad Config",
            "kind": "splunk_hec",
            "config": {"not_a_url_field": "oops"},
        },
    )
    assert r.status_code == 422, r.text


def test_create_duplicate_name_409(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    client.post("/api/collectors/destinations", json=_syslog_payload(name="UniqueOne"))
    r = client.post("/api/collectors/destinations", json=_syslog_payload(name="UniqueOne"))
    assert r.status_code == 409, r.text


def test_create_destination_requires_admin(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    r = client.post("/api/collectors/destinations", json=_syslog_payload())
    assert r.status_code == 401


# ── Secret round-trip ─────────────────────────────────────────────────


def test_create_splunk_with_hec_token_secret_round_trip(client_factory, tmp_path) -> None:
    """POST with hec_token:
    - secret_ref stored encrypted (not plaintext)
    - DestinationRead.has_secret is True
    - token absent from response
    - POST audit does not contain the token or secret_ref
    """
    import logging as _logging

    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)

    captured_logs: list[str] = []

    class LogCapture(_logging.Handler):
        def emit(self, record):
            captured_logs.append(self.format(record))

    handler = LogCapture()
    _logging.getLogger("backend.app.routers.destinations").addHandler(handler)

    try:
        r = client.post(
            "/api/collectors/destinations",
            json=_splunk_payload(hec_token="super-secret-hec-token-12345"),
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["has_secret"] is True
        assert "hec_token" not in body
        assert "secret_ref" not in body

        # Verify in DB: secret_ref is encrypted (not plaintext)
        dest_id = body["id"]
        with SessionLocal() as db:
            row = db.query(models.Destination).filter_by(id=dest_id).first()
            assert row is not None
            assert row.secret_ref is not None
            assert row.secret_ref != "super-secret-hec-token-12345"
            # The encrypted value must be decryptable.
            from backend.app.core.secrets import get_default_backend
            plaintext = get_default_backend().decrypt(row.secret_ref)
            assert plaintext == "super-secret-hec-token-12345"

        # Audit log must not contain the token or secret_ref
        for log_line in captured_logs:
            assert "super-secret-hec-token-12345" not in log_line
            # secret_ref is never [REDACTED]-printed as a value
            assert "REDACTED" in log_line or "secret_ref" not in log_line.lower()
    finally:
        _logging.getLogger("backend.app.routers.destinations").removeHandler(handler)


# ── GET (list) ────────────────────────────────────────────────────────


def test_list_destinations_happy_path(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    client.post("/api/collectors/destinations", json=_syslog_payload(name="Dest A"))
    client.post("/api/collectors/destinations", json=_syslog_payload(name="Dest B"))

    r = client.get("/api/collectors/destinations")
    assert r.status_code == 200, r.text
    names = [item["name"] for item in r.json()]
    assert "Dest A" in names
    assert "Dest B" in names


def test_list_destinations_requires_admin(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    r = client.get("/api/collectors/destinations")
    assert r.status_code == 401


# ── GET /{id} ─────────────────────────────────────────────────────────


def test_get_destination_happy_path(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    created = client.post(
        "/api/collectors/destinations", json=_syslog_payload()
    ).json()
    dest_id = created["id"]

    r = client.get(f"/api/collectors/destinations/{dest_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == dest_id
    assert "secret_ref" not in body
    assert "hec_token" not in body


def test_get_destination_not_found_404(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.get("/api/collectors/destinations/does-not-exist")
    assert r.status_code == 404, r.text


# ── PUT /{id} ─────────────────────────────────────────────────────────


def test_update_destination_happy_path(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    created = client.post(
        "/api/collectors/destinations", json=_syslog_payload()
    ).json()
    dest_id = created["id"]

    r = client.put(
        f"/api/collectors/destinations/{dest_id}",
        json={"name": "Updated Name", "enabled": False},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "Updated Name"
    assert body["enabled"] is False


def test_update_config_bumps_config_version(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    created = client.post(
        "/api/collectors/destinations", json=_syslog_payload()
    ).json()
    dest_id = created["id"]
    original_version = created["config_version"]

    r = client.put(
        f"/api/collectors/destinations/{dest_id}",
        json={"config": {"host": "new-host.local", "port": 514}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["config_version"] != original_version


def test_update_hec_token_reencrypts(client_factory) -> None:
    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)

    created = client.post(
        "/api/collectors/destinations",
        json=_splunk_payload(hec_token="original-token"),
    ).json()
    dest_id = created["id"]

    # Get the original secret_ref
    with SessionLocal() as db:
        row = db.query(models.Destination).filter_by(id=dest_id).first()
        original_ref = row.secret_ref

    client.put(
        f"/api/collectors/destinations/{dest_id}",
        json={"hec_token": "new-token-99"},
    )

    with SessionLocal() as db:
        row = db.query(models.Destination).filter_by(id=dest_id).first()
        # The ref must have changed (re-encrypted)
        assert row.secret_ref != original_ref
        from backend.app.core.secrets import get_default_backend
        assert get_default_backend().decrypt(row.secret_ref) == "new-token-99"


def test_update_not_found_404(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.put(
        "/api/collectors/destinations/nonexistent-id",
        json={"name": "Whatever"},
    )
    assert r.status_code == 404, r.text


# ── DELETE /{id} ──────────────────────────────────────────────────────


def test_delete_destination_happy_path(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    created = client.post(
        "/api/collectors/destinations", json=_syslog_payload()
    ).json()
    dest_id = created["id"]

    r = client.delete(f"/api/collectors/destinations/{dest_id}")
    assert r.status_code == 204, r.text

    r2 = client.get(f"/api/collectors/destinations/{dest_id}")
    assert r2.status_code == 404


def test_delete_not_found_404(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.delete("/api/collectors/destinations/nonexistent-id")
    assert r.status_code == 404, r.text


# ── Cross-tenant isolation ───────────────────────────────────────


def test_org_id_filter_on_list(client_factory) -> None:
    """list ?org_id filters only that org's destinations for global admin."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    org_a = _create_org(client, "Org Alpha")
    org_b = _create_org(client, "Org Beta")

    # Create a destination for each org
    client.post(
        "/api/collectors/destinations",
        json=_syslog_payload(name="Alpha Dest", organization_id=org_a["id"]),
    )
    client.post(
        "/api/collectors/destinations",
        json=_syslog_payload(name="Beta Dest", organization_id=org_b["id"]),
    )

    # Filter by org_a — should only see Alpha Dest (and global NULL ones)
    r = client.get(f"/api/collectors/destinations?org_id={org_a['id']}&include_disabled=true")
    assert r.status_code == 200, r.text
    names = [item["name"] for item in r.json()]
    assert "Alpha Dest" in names
    assert "Beta Dest" not in names

    # Filter by org_b
    r2 = client.get(f"/api/collectors/destinations?org_id={org_b['id']}&include_disabled=true")
    assert r2.status_code == 200, r2.text
    names2 = [item["name"] for item in r2.json()]
    assert "Beta Dest" in names2
    assert "Alpha Dest" not in names2


def test_cross_tenant_isolation(client_factory) -> None:
    """404 for cross-tenant access in _assert_visible.

    Since all admin users are globally-scoped (tenant.has_global_scope returns
    True for any role=admin user), true cross-tenant 404 enforcement applies to
    non-admin roles — which cannot reach admin-only endpoints. What we test here
    is that DestinationRepository.list applies the filter correctly for
    a global admin using explicit ?org_id, and that a destination created for
    org B is NOT visible in a list filtered to org A.
    """
    factory, _ = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)

    # Create orgs
    org_a = _create_org(admin_client, "Org Alpha S1")
    org_b = _create_org(admin_client, "Org Beta S1")

    # Admin creates a destination exclusively for org_b
    dest_b_body = admin_client.post(
        "/api/collectors/destinations",
        json=_syslog_payload(name="OrgB-Only Syslog", organization_id=org_b["id"]),
    ).json()
    assert "id" in dest_b_body, dest_b_body
    dest_b_id = dest_b_body["id"]

    # When listing filtered to org_a, org_b's destination must not appear
    r = admin_client.get(f"/api/collectors/destinations?org_id={org_a['id']}&include_disabled=true")
    assert r.status_code == 200, r.text
    ids = [item["id"] for item in r.json()]
    assert dest_b_id not in ids, (
        "S1 violated: org B destination appeared in org A's filtered list"
    )


# ── POST /{id}/test ───────────────────────────────────────────────────


def test_test_endpoint_maps_test_result(client_factory) -> None:
    """POST /test returns DestinationTestResponse with ok/detail/latency_ms."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    created = client.post(
        "/api/collectors/destinations",
        json=_splunk_payload(hec_token="dummy-token"),
    ).json()
    dest_id = created["id"]

    from backend.app.collectors.output.base import TestResult

    mock_result = TestResult(ok=True, detail="probe ok", latency_ms=42.0)

    with patch(
        "backend.app.collectors.output.destinations.splunk_hec.SplunkHecClient.test",
        new_callable=AsyncMock,
        return_value=mock_result,
    ), patch(
        "backend.app.collectors.output.destinations.splunk_hec.SplunkHecClient.close",
        new_callable=AsyncMock,
    ):
        r = client.post(f"/api/collectors/destinations/{dest_id}/test")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["detail"] == "probe ok"
    assert body["latency_ms"] == 42.0


def test_test_endpoint_does_not_pollute_destination_cache(client_factory) -> None:
    """POST /test must NOT add entries to destination_cache._cache."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    created = client.post(
        "/api/collectors/destinations",
        json=_splunk_payload(hec_token="dummy-token"),
    ).json()
    dest_id = created["id"]

    from backend.app.collectors.output import destination_cache
    # Ensure cache starts empty
    destination_cache._cache.clear()

    from backend.app.collectors.output.base import TestResult

    mock_result = TestResult(ok=True, detail="ok", latency_ms=10.0)

    with patch(
        "backend.app.collectors.output.destinations.splunk_hec.SplunkHecClient.test",
        new_callable=AsyncMock,
        return_value=mock_result,
    ), patch(
        "backend.app.collectors.output.destinations.splunk_hec.SplunkHecClient.close",
        new_callable=AsyncMock,
    ):
        r = client.post(f"/api/collectors/destinations/{dest_id}/test")

    assert r.status_code == 200, r.text
    # Production singleton cache must remain empty
    assert destination_cache._cache == {}, (
        f"LM-1 violated: destination_cache._cache is not empty: {destination_cache._cache}"
    )


def test_test_endpoint_cross_tenant_isolation_via_list(client_factory) -> None:
    """/test is admin-only, all admins are globally-scoped.

    We verify the same property as cross_tenant_isolation: that a destination
    scoped to org B does not appear in an org-A–filtered list (and thus would
    not be surfaced to an org-A operator via any UI that calls list first).

    Direct /test of a cross-org ID by an admin returns 200 because admins
    have global scope — this is documented behavior, not a bug.
    """
    factory, _ = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)

    org_a = _create_org(admin_client, "S1Test-Alpha")
    org_b = _create_org(admin_client, "S1Test-Beta")

    dest_b = admin_client.post(
        "/api/collectors/destinations",
        json=_syslog_payload(name="S1Test-OrgB", organization_id=org_b["id"]),
    ).json()
    dest_b_id = dest_b["id"]

    # org_b's destination must not appear in org_a list
    r = admin_client.get(
        f"/api/collectors/destinations?org_id={org_a['id']}&include_disabled=true"
    )
    ids = [item["id"] for item in r.json()]
    assert dest_b_id not in ids


def test_test_endpoint_not_found_404(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.post("/api/collectors/destinations/nonexistent/test")
    assert r.status_code == 404, r.text


# ── Audit does not contain token ──────────────────────────────────────


def test_create_audit_does_not_contain_token(client_factory) -> None:
    """The structured log emitted on POST must not include hec_token."""
    import logging as _logging

    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    captured: list[str] = []

    class _Cap(_logging.Handler):
        def emit(self, record):
            captured.append(self.format(record))

    handler = _Cap()
    _logging.getLogger("backend.app.routers.destinations").addHandler(handler)

    try:
        r = client.post(
            "/api/collectors/destinations",
            json=_splunk_payload(hec_token="top-secret-hec-token"),
        )
        assert r.status_code == 201, r.text
    finally:
        _logging.getLogger("backend.app.routers.destinations").removeHandler(handler)

    for line in captured:
        assert "top-secret-hec-token" not in line, (
            f"hec_token leaked into audit log: {line}"
        )


# ── Update: invalid config for kind returns 422 ───────────────────────


def test_update_invalid_config_for_kind_422(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    created = client.post(
        "/api/collectors/destinations",
        json=_splunk_payload(),
    ).json()
    dest_id = created["id"]

    # splunk_hec requires 'url' in config
    r = client.put(
        f"/api/collectors/destinations/{dest_id}",
        json={"config": {"no_url_here": "oops"}},
    )
    assert r.status_code == 422, r.text


# ── POST /{id}/shadow — formats without delivering ─────────────────


def test_shadow_endpoint_formats_without_delivery(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    dest_id = client.post(
        "/api/collectors/destinations", json=_syslog_payload()
    ).json()["id"]

    # No sample → synthetic envelope; never contacts the syslog server.
    r = client.post(f"/api/collectors/destinations/{dest_id}/shadow", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["count"] == 1
    assert body["formatted_preview"]  # non-empty wire preview
    # RFC3164 framing in the preview (PRI + JSON in MSG).
    assert body["formatted_preview"].startswith("<")


def test_shadow_endpoint_with_custom_sample(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    dest_id = client.post(
        "/api/collectors/destinations", json=_splunk_payload()
    ).json()["id"]

    sample = {
        "_centralops": {"event_id": "evt-xyz", "organization_id": None},
        "normalized": {"message": "hello shadow"},
        "raw": {},
    }
    r = client.post(
        f"/api/collectors/destinations/{dest_id}/shadow", json={"sample": sample}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert "evt-xyz" in body["formatted_preview"]


def test_shadow_endpoint_unknown_destination_404(client_factory) -> None:
    """Shadow of a non-existent destination → 404 (anti-enumeration, shares the
    _assert_visible guard with /get and /test)."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.post(
        "/api/collectors/destinations/does-not-exist/shadow", json={}
    )
    assert r.status_code == 404, r.text


def test_shadow_endpoint_requires_admin(client_factory) -> None:
    """Shadow is admin-only (no auth → 401/403)."""
    factory, _ = client_factory
    client = factory()
    r = client.post("/api/collectors/destinations/whatever/shadow", json={})
    assert r.status_code in (401, 403), r.text


# ── GET /{id}/health ───────────────────────────────────────────────────


def test_health_endpoint_healthy(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    dest_id = client.post(
        "/api/collectors/destinations", json=_splunk_payload()
    ).json()["id"]

    # Force a verifiable closed breaker → genuine "healthy" derivation.
    async def _closed(_id: str) -> str:
        return "closed"

    with patch("backend.app.routers.destinations._read_breaker_state", _closed):
        r = client.get(f"/api/collectors/destinations/{dest_id}/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["destination_id"] == dest_id
    assert body["enabled"] is True
    assert body["dlq_total"] == 0
    assert body["dlq_24h"] == 0
    assert body["breaker_state"] == "closed"
    assert body["status"] == "healthy"


def test_health_endpoint_unknown_when_breaker_unverifiable(client_factory) -> None:
    """Redis down (breaker unknown) + no DLQ → status 'unknown', not a green
    'healthy' badge for an unverifiable state."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    dest_id = client.post(
        "/api/collectors/destinations", json=_splunk_payload()
    ).json()["id"]

    # No live Redis in tests → _read_breaker_state returns "unknown".
    r = client.get(f"/api/collectors/destinations/{dest_id}/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["breaker_state"] == "unknown"
    assert body["status"] == "unknown"


def test_health_endpoint_degraded_with_dlq(client_factory) -> None:
    from backend.app.db import models as _m

    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)

    dest_id = client.post(
        "/api/collectors/destinations", json=_splunk_payload()
    ).json()["id"]

    # Seed a recent DLQ row → degraded.
    with SessionLocal() as s:
        s.add(
            _m.DestinationDeadLetter(
                destination_id=dest_id,
                event_id="evt-dlq-1",
                organization_id=None,
                error_kind="schema_rejected",
            )
        )
        s.commit()

    r = client.get(f"/api/collectors/destinations/{dest_id}/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dlq_total"] == 1
    assert body["dlq_24h"] == 1
    assert body["status"] == "degraded"
    assert body["last_dlq_at"] is not None


def test_health_endpoint_disabled(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    dest_id = client.post(
        "/api/collectors/destinations", json=_splunk_payload(enabled=False)
    ).json()["id"]

    r = client.get(f"/api/collectors/destinations/{dest_id}/health")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "disabled"


def test_health_endpoint_unknown_404(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    r = client.get("/api/collectors/destinations/nope/health")
    assert r.status_code == 404, r.text


# ── GET /{id}/dlq (drill-in) ────────────────────────────────────


def test_dlq_drill_in(client_factory) -> None:
    from backend.app.db import models as _m

    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest_id = client.post("/api/collectors/destinations", json=_splunk_payload()).json()["id"]

    with SessionLocal() as s:
        s.add(_m.DestinationDeadLetter(
            destination_id=dest_id, event_id="e1", organization_id=None,
            error_kind="schema_rejected", error_detail="bad", payload='{"_centralops":{"event_id":"e1"}}'))
        s.add(_m.DestinationDeadLetter(
            destination_id=dest_id, event_id="e2", organization_id=None,
            error_kind="schema_rejected", error_detail="bad2", payload='{"x":1}'))
        s.add(_m.DestinationDeadLetter(
            destination_id=dest_id, event_id="e3", organization_id=None,
            error_kind="payload_too_large", error_detail="big", payload=None))
        s.commit()

    r = client.get(f"/api/collectors/destinations/{dest_id}/dlq")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 3
    assert body["by_error_kind"] == {"schema_rejected": 2, "payload_too_large": 1}
    assert len(body["entries"]) == 3
    # payload drill-in is parsed to an object
    e1 = next(e for e in body["entries"] if e["event_id"] == "e1")
    assert e1["payload"]["_centralops"]["event_id"] == "e1"
    assert e1["error_kind"] == "schema_rejected"


def test_dlq_drill_in_redacts_secrets_in_payload(client_factory) -> None:
    """o drill-in mascara segredos por nome no payload retornado —
    o dado armazenado fica íntegro (forense), mas a API não expõe credenciais."""
    from backend.app.db import models as _m

    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest_id = client.post("/api/collectors/destinations", json=_splunk_payload()).json()["id"]

    with SessionLocal() as s:
        s.add(_m.DestinationDeadLetter(
            destination_id=dest_id, event_id="e1", organization_id=None,
            error_kind="exhausted", error_detail="x",
            payload='{"token":"supersecret","raw":{"password":"p@ss"},"msg":"keep"}'))
        s.commit()

    body = client.get(f"/api/collectors/destinations/{dest_id}/dlq").json()
    e1 = next(e for e in body["entries"] if e["event_id"] == "e1")
    assert e1["payload"]["token"] == "[REDACTED]"          # segredo top-level
    assert e1["payload"]["raw"]["password"] == "[REDACTED]"  # redação recursiva
    assert e1["payload"]["msg"] == "keep"                   # não-sensível preservado


def test_dlq_drill_in_unknown_404(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    assert client.get("/api/collectors/destinations/nope/dlq").status_code == 404


def test_tap_endpoint_live_data(client_factory) -> None:
    """Live data-tap: recent envelopes flowing to a destination (redacted)."""
    import fakeredis

    from backend.app.collectors import observability_store as obs

    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest_id = client.post("/api/collectors/destinations", json=_splunk_payload()).json()["id"]

    fake = fakeredis.FakeStrictRedis(decode_responses=True)
    with patch.object(obs, "_redis", return_value=fake):
        obs.record_tap(dest_id, [
            {"_centralops": {"event_id": "e1", "organization_id": None}, "normalized": {"msg": "hi"}},
            {"_centralops": {"event_id": "e2", "organization_id": None}, "normalized": {}},
        ])
        r = client.get(f"/api/collectors/destinations/{dest_id}/tap")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["destination_id"] == dest_id
    assert len(body["entries"]) == 2
    # newest first
    assert body["entries"][0]["_centralops"]["event_id"] == "e2"


def test_tap_endpoint_unknown_404(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    assert client.get("/api/collectors/destinations/nope/tap").status_code == 404


def test_dlq_repo_org_scope_filters_cross_tenant(client_factory) -> None:
    """a non-global caller only sees their org's (+ NULL) DLQ rows on
    a (global) destination. Tested at the repo level — no org-scoped admin role
    exists yet, so the endpoint can't reach this path."""
    from backend.app.db import models as _m, repository

    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest_id = client.post("/api/collectors/destinations", json=_splunk_payload()).json()["id"]
    with SessionLocal() as s:
        for eid, org in (("a", 1), ("b", 2), ("c", None)):
            s.add(_m.DestinationDeadLetter(destination_id=dest_id, event_id=eid, organization_id=org, error_kind="x"))
        s.commit()
        repo = repository.DestinationRepository(s)
        # non-global org 1 → org1 + NULL (a, c), NOT org2 (b)
        assert {r.event_id for r in repo.list_dlq(dest_id, org_id=1, global_scope=False)} == {"a", "c"}
        assert repo.dlq_stats(dest_id, org_id=1, global_scope=False)["dlq_total"] == 2
        # global → all 3 (default behavior preserved)
        assert len(repo.list_dlq(dest_id, global_scope=True)) == 3


# ── GET /{id}/metrics ─────────────────────────────────────────


def test_metrics_endpoint_native_store_self_sufficient(client_factory) -> None:
    """Native store (Redis rollups) — available=True without any Prometheus;
    DB DLQ summary always returned alongside the time-series."""
    import fakeredis

    from backend.app.collectors import observability_store as obs
    from backend.app.db import models as _m

    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest_id = client.post("/api/collectors/destinations", json=_splunk_payload()).json()["id"]
    with SessionLocal() as s:
        s.add(_m.DestinationDeadLetter(destination_id=dest_id, event_id="e1", organization_id=None, error_kind="schema_rejected"))
        s.commit()

    fake = fakeredis.FakeStrictRedis(decode_responses=True)
    # Point the store (writes) + the endpoint (reads) at the same fake redis.
    with patch.object(obs, "_redis", return_value=fake):
        obs.record_counter("dest", dest_id, "sent", 12)
        obs.set_gauge("dest", dest_id, "queue_depth", 3)
        resp = client.get(f"/api/collectors/destinations/{dest_id}/metrics")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["available"] is True
    assert body["dlq_total"] == 1
    assert body["by_error_kind"] == {"schema_rejected": 1}
    # native series present (events this minute)
    assert body["series"]["sent"] and body["series"]["sent"][0][1] == 12
    assert body["gauges"]["queue_depth"] == "3"


# ── POST /{id}/dlq/reprocess ───────────────────────


def _seed_dlq_row(
    SessionLocal,
    *,
    destination_id: str,
    event_id: str = "ev-1",
    error_kind: str = "exhausted",
    payload: dict | None = None,
) -> str:
    """Insert a DestinationDeadLetter row and return its id."""
    from backend.app.db import models as _m

    row = _m.DestinationDeadLetter(
        destination_id=destination_id,
        event_id=event_id,
        organization_id=None,
        error_kind=error_kind,
        error_detail="test failure",
        payload=json.dumps(payload or {"_centralops": {"event_id": event_id}}),
    )
    with SessionLocal() as s:
        s.add(row)
        s.commit()
        return str(row.id)


def _count_dlq(SessionLocal, destination_id: str) -> int:
    """Count DLQ rows for a destination (direct DB read)."""
    from backend.app.db import models as _m

    with SessionLocal() as s:
        return (
            s.query(_m.DestinationDeadLetter)
            .filter(_m.DestinationDeadLetter.destination_id == destination_id)
            .count()
        )


def test_dlq_reprocess_requires_admin(client_factory) -> None:
    """Unauthenticated callers must receive 401/403."""
    factory, _ = client_factory
    client = factory()
    r = client.post("/api/collectors/destinations/whatever/dlq/reprocess", json={})
    assert r.status_code in (401, 403), r.text


def test_dlq_reprocess_unknown_destination_404(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.post(
        "/api/collectors/destinations/nonexistent/dlq/reprocess", json={}
    )
    assert r.status_code == 404, r.text


def test_dlq_reprocess_empty_dlq_returns_queued_zero(client_factory) -> None:
    """When DLQ is empty, response is immediate (no task spawned) with queued=0."""
    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest_id = client.post("/api/collectors/destinations", json=_syslog_payload()).json()["id"]

    r = client.post(f"/api/collectors/destinations/{dest_id}/dlq/reprocess", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["destination_id"] == dest_id
    assert body["queued"] == 0
    assert body["task_id"] == ""


def test_dlq_reprocess_enqueues_task_for_all_entries(client_factory) -> None:
    """With DLQ entries, the endpoint enqueues a task and returns queued count."""
    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest_id = client.post("/api/collectors/destinations", json=_syslog_payload()).json()["id"]

    _seed_dlq_row(SessionLocal, destination_id=dest_id, event_id="e-1")
    _seed_dlq_row(SessionLocal, destination_id=dest_id, event_id="e-2")

    mock_task_result = MagicMock()
    mock_task_result.id = "fake-celery-task-id"

    with patch(
        "backend.app.collectors.tasks.drain_destination_dlq.apply_async",
        return_value=mock_task_result,
    ) as mock_apply:
        r = client.post(f"/api/collectors/destinations/{dest_id}/dlq/reprocess", json={})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["queued"] == 2
    assert body["task_id"] == "fake-celery-task-id"
    # Verify the task was called with the right destination_id
    call_kwargs = mock_apply.call_args.kwargs["kwargs"]
    assert call_kwargs["destination_id"] == dest_id
    assert set(call_kwargs["event_ids"]) == {"e-1", "e-2"}


def test_dlq_reprocess_specific_event_ids(client_factory) -> None:
    """Passing explicit event_ids limits the scope of the drain."""
    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest_id = client.post("/api/collectors/destinations", json=_syslog_payload()).json()["id"]

    _seed_dlq_row(SessionLocal, destination_id=dest_id, event_id="ev-target")
    _seed_dlq_row(SessionLocal, destination_id=dest_id, event_id="ev-skip")

    mock_task_result = MagicMock()
    mock_task_result.id = "t-xyz"

    with patch(
        "backend.app.collectors.tasks.drain_destination_dlq.apply_async",
        return_value=mock_task_result,
    ) as mock_apply:
        r = client.post(
            f"/api/collectors/destinations/{dest_id}/dlq/reprocess",
            json={"event_ids": ["ev-target"]},
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["queued"] == 1  # only the targeted event
    call_kwargs = mock_apply.call_args.kwargs["kwargs"]
    assert call_kwargs["event_ids"] == ["ev-target"]


# ── drain_destination_dlq task (synchronous unit tests) ──────────────


def test_drain_dlq_task_removes_on_success(client_factory) -> None:
    """Successful re-delivery → DLQ row is hard-deleted."""
    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest_id = client.post("/api/collectors/destinations", json=_syslog_payload()).json()["id"]
    _seed_dlq_row(SessionLocal, destination_id=dest_id, event_id="ev-drain")

    from backend.app.collectors.tasks import drain_destination_dlq
    from backend.app.db import database as _db

    with (
        patch.object(_db, "SessionLocal", SessionLocal),
        patch(
            "backend.app.collectors.tasks.run_coro_blocking",
            side_effect=lambda coro, **kw: None,
        ),
    ):
        result = drain_destination_dlq(
            destination_id=dest_id,
            event_ids=["ev-drain"],
            org_id=None,
            global_scope=True,
        )

    assert result["delivered"] == 1
    assert result["failed"] == 0
    # Row must be gone from the DB.
    assert _count_dlq(SessionLocal, dest_id) == 0


def test_drain_dlq_task_keeps_row_on_failure(client_factory) -> None:
    """Failed re-delivery → DLQ row stays with updated error_detail."""
    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest_id = client.post("/api/collectors/destinations", json=_syslog_payload()).json()["id"]
    _seed_dlq_row(SessionLocal, destination_id=dest_id, event_id="ev-fail")

    from backend.app.collectors.tasks import drain_destination_dlq
    from backend.app.db import database as _db

    with (
        patch.object(_db, "SessionLocal", SessionLocal),
        patch(
            "backend.app.collectors.tasks.run_coro_blocking",
            side_effect=ConnectionError("timeout"),
        ),
    ):
        result = drain_destination_dlq(
            destination_id=dest_id,
            event_ids=["ev-fail"],
            org_id=None,
            global_scope=True,
        )

    assert result["delivered"] == 0
    assert result["failed"] == 1
    # Row must still exist.
    assert _count_dlq(SessionLocal, dest_id) == 1

    from backend.app.db import models as _m

    with SessionLocal() as s:
        row = (
            s.query(_m.DestinationDeadLetter)
            .filter(_m.DestinationDeadLetter.destination_id == dest_id)
            .first()
        )
        assert row is not None
        assert row.error_kind == "reprocess_failed"


def test_drain_dlq_task_handles_no_payload(client_factory) -> None:
    """Row with no payload is treated as delivered (nothing to redeliver) and removed."""
    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest_id = client.post("/api/collectors/destinations", json=_syslog_payload()).json()["id"]

    from backend.app.db import models as _m

    with SessionLocal() as s:
        row = _m.DestinationDeadLetter(
            destination_id=dest_id,
            event_id="ev-nopayload",
            organization_id=None,
            error_kind="exhausted",
            payload=None,
        )
        s.add(row)
        s.commit()

    from backend.app.collectors.tasks import drain_destination_dlq
    from backend.app.db import database as _db

    with patch.object(_db, "SessionLocal", SessionLocal):
        result = drain_destination_dlq(
            destination_id=dest_id,
            event_ids=["ev-nopayload"],
            org_id=None,
            global_scope=True,
        )
    assert result["delivered"] == 1
    assert result["failed"] == 0
    assert _count_dlq(SessionLocal, dest_id) == 0


@pytest.mark.parametrize(
    "event_ids,expected_queued",
    [
        (None, 3),   # all entries
        (["e-a", "e-b"], 2),  # subset
        (["e-a"], 1),  # single
        ([], 3),  # empty list = all
    ],
)
def test_dlq_reprocess_event_id_filter(
    client_factory, event_ids, expected_queued
) -> None:
    """Parametrize: filtering by event_ids returns correct queued count."""
    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest_id = client.post("/api/collectors/destinations", json=_syslog_payload()).json()["id"]

    for eid in ["e-a", "e-b", "e-c"]:
        _seed_dlq_row(SessionLocal, destination_id=dest_id, event_id=eid)

    mock_task_result = MagicMock()
    mock_task_result.id = "t-1"

    with patch(
        "backend.app.collectors.tasks.drain_destination_dlq.apply_async",
        return_value=mock_task_result,
    ):
        body_payload: dict = {}
        if event_ids is not None:
            body_payload["event_ids"] = event_ids
        r = client.post(
            f"/api/collectors/destinations/{dest_id}/dlq/reprocess",
            json=body_payload,
        )

    assert r.status_code == 200, r.text
    assert r.json()["queued"] == expected_queued


# ── credential lifecycle (rotate / revoke) ────────────────────────


def test_rotate_credential_bumps_version_and_writes_audit(client_factory) -> None:
    """POST /credential/rotate: secret_version increments, secret_ref re-encrypted,
    audit record 'rotate' created."""
    from backend.app.db import models as _m, repository

    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)

    dest = client.post(
        "/api/collectors/destinations",
        json=_splunk_payload(hec_token="original-token"),
    ).json()
    dest_id = dest["id"]

    r = client.post(
        f"/api/collectors/destinations/{dest_id}/credential/rotate",
        json={"new_secret": "rotated-token-v2"},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["destination_id"] == dest_id
    assert body["secret_version"] == 2  # bumped from default 1
    assert body["has_secret"] is True
    assert body["secret_rotated_at"] is not None

    # Verify in DB: secret_ref re-encrypted + version bumped
    with SessionLocal() as db:
        row = db.query(_m.Destination).filter_by(id=dest_id).first()
        assert row is not None
        assert row.secret_version == 2
        assert row.secret_ref is not None
        assert row.secret_ref != "rotated-token-v2"  # stored encrypted, not plaintext
        from backend.app.core.secrets import get_default_backend
        assert get_default_backend().decrypt(row.secret_ref) == "rotated-token-v2"
        assert row.secret_rotated_at is not None
        assert row.secret_revoked_at is None  # re-key clears revoke

    # Verify audit trail: 'rotate' entry exists
    with SessionLocal() as db:
        repo = repository.DestinationRepository(db)
        total, entries = repo.list_credential_access_log(dest_id)
        assert total >= 1
        actions = [e.action for e in entries]
        assert "rotate" in actions
        rotate_entry = next(e for e in entries if e.action == "rotate")
        assert rotate_entry.actor == "admin"
        assert rotate_entry.detail is not None
        assert "2" in rotate_entry.detail  # version in detail JSON


def test_rotate_credential_unknown_destination_404(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.post(
        "/api/collectors/destinations/nonexistent/credential/rotate",
        json={"new_secret": "some-secret"},
    )
    assert r.status_code == 404, r.text


def test_rotate_credential_requires_admin(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    r = client.post(
        "/api/collectors/destinations/whatever/credential/rotate",
        json={"new_secret": "x"},
    )
    assert r.status_code in (401, 403), r.text


@pytest.mark.parametrize(
    "expires_at,expect_in_response",
    [
        (None, None),
        ("2030-01-01T00:00:00", "2030-01-01"),
    ],
)
def test_rotate_credential_with_expires_at(
    client_factory, expires_at: str | None, expect_in_response: str | None
) -> None:
    """rotate with expires_at sets the field; without it, field is absent."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest_id = client.post("/api/collectors/destinations", json=_syslog_payload()).json()["id"]

    payload: dict = {"new_secret": "tok"}
    if expires_at is not None:
        payload["expires_at"] = expires_at

    r = client.post(
        f"/api/collectors/destinations/{dest_id}/credential/rotate",
        json=payload,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    if expect_in_response:
        assert body["secret_expires_at"] is not None
        assert expect_in_response in body["secret_expires_at"]
    else:
        assert body["secret_expires_at"] is None


def test_revoke_credential_clears_secret_and_disables(client_factory) -> None:
    """POST /credential/revoke: secret_ref cleared, enabled=False, audit 'revoke'."""
    from backend.app.db import models as _m, repository

    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)

    dest = client.post(
        "/api/collectors/destinations",
        json=_splunk_payload(hec_token="secret-to-revoke"),
    ).json()
    dest_id = dest["id"]
    assert dest["enabled"] is True

    r = client.post(f"/api/collectors/destinations/{dest_id}/credential/revoke")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["destination_id"] == dest_id
    assert body["enabled"] is False
    assert body["has_secret"] is False
    assert body["secret_revoked_at"] is not None

    # Verify DB state
    with SessionLocal() as db:
        row = db.query(_m.Destination).filter_by(id=dest_id).first()
        assert row is not None
        assert row.secret_ref is None
        assert row.enabled is False
        assert row.secret_revoked_at is not None

    # Verify audit trail: 'revoke' entry exists
    with SessionLocal() as db:
        repo = repository.DestinationRepository(db)
        total, entries = repo.list_credential_access_log(dest_id)
        assert total >= 1
        assert any(e.action == "revoke" and e.actor == "admin" for e in entries)


def test_revoke_credential_unknown_destination_404(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.post("/api/collectors/destinations/nonexistent/credential/revoke")
    assert r.status_code == 404, r.text


# ── credential access audit (/test writes 'test' + decrypt) ─────


def test_test_endpoint_writes_credential_access_audit(client_factory) -> None:
    """POST /test with a destination that has a secret_ref writes a 'test'
    (decrypt) entry to credential_access_log."""
    from backend.app.db import repository
    from backend.app.collectors.output.base import TestResult

    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)

    dest = client.post(
        "/api/collectors/destinations",
        json=_splunk_payload(hec_token="audit-me-token"),
    ).json()
    dest_id = dest["id"]

    mock_result = TestResult(ok=True, detail="probe ok", latency_ms=5.0)

    with patch(
        "backend.app.collectors.output.destinations.splunk_hec.SplunkHecClient.test",
        new_callable=AsyncMock,
        return_value=mock_result,
    ), patch(
        "backend.app.collectors.output.destinations.splunk_hec.SplunkHecClient.close",
        new_callable=AsyncMock,
    ):
        r = client.post(f"/api/collectors/destinations/{dest_id}/test")

    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    # Verify audit entry in DB
    with SessionLocal() as db:
        repo = repository.DestinationRepository(db)
        total, entries = repo.list_credential_access_log(dest_id)
        assert total >= 1
        assert any(e.action == "test" and e.actor == "admin" for e in entries)


def test_test_endpoint_no_audit_when_no_secret(client_factory) -> None:
    """POST /test on a destination with NO secret_ref does NOT write an audit entry
    (there is no credential to audit)."""
    from backend.app.db import repository
    from backend.app.collectors.output.base import TestResult, Destination

    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)

    # syslog has no hec_token / secret
    dest = client.post(
        "/api/collectors/destinations", json=_syslog_payload()
    ).json()
    dest_id = dest["id"]

    mock_result = TestResult(ok=True, detail="ok", latency_ms=1.0)

    # Mock at the registry level: intercept build() to return a stub Destination
    # (syslog_rfc3164 uses a functional style — no client class to patch directly).
    mock_dest = MagicMock(spec=Destination)
    mock_dest.test = AsyncMock(return_value=mock_result)
    mock_dest.close = AsyncMock()

    with patch(
        "backend.app.routers.destinations._registry.build",
        return_value=mock_dest,
    ):
        r = client.post(f"/api/collectors/destinations/{dest_id}/test")

    assert r.status_code == 200, r.text

    with SessionLocal() as db:
        repo = repository.DestinationRepository(db)
        total, _ = repo.list_credential_access_log(dest_id)
        assert total == 0  # no secret → no audit entry


# ── GET /{id}/credential/audit ────────────────────────────────────


def test_credential_audit_returns_trail(client_factory) -> None:
    """GET /credential/audit returns the combined trail of rotate + revoke events."""
    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)

    dest = client.post(
        "/api/collectors/destinations",
        json=_splunk_payload(hec_token="initial-tok"),
    ).json()
    dest_id = dest["id"]

    # Rotate once
    client.post(
        f"/api/collectors/destinations/{dest_id}/credential/rotate",
        json={"new_secret": "rotated-tok"},
    )
    # Revoke
    client.post(f"/api/collectors/destinations/{dest_id}/credential/revoke")
    # Read audit
    r = client.get(f"/api/collectors/destinations/{dest_id}/credential/audit")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["destination_id"] == dest_id
    assert body["total"] >= 2  # rotate + revoke
    actions = [e["action"] for e in body["entries"]]
    assert "rotate" in actions
    assert "revoke" in actions
    # newest first
    assert body["entries"][0]["created_at"] >= body["entries"][-1]["created_at"]


def test_credential_audit_unknown_destination_404(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.get("/api/collectors/destinations/nonexistent/credential/audit")
    assert r.status_code == 404, r.text


def test_credential_audit_requires_admin(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    r = client.get("/api/collectors/destinations/whatever/credential/audit")
    assert r.status_code in (401, 403), r.text


def test_credential_audit_empty_for_new_destination(client_factory) -> None:
    """A freshly-created destination with no credential operations has empty audit."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest_id = client.post("/api/collectors/destinations", json=_syslog_payload()).json()["id"]

    r = client.get(f"/api/collectors/destinations/{dest_id}/credential/audit")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 0
    assert body["entries"] == []


def test_rotate_twice_increments_version_correctly(client_factory) -> None:
    """Two successive rotations produce version 3 (original=1, first=2, second=3)."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest_id = client.post(
        "/api/collectors/destinations",
        json=_splunk_payload(hec_token="v1"),
    ).json()["id"]

    r1 = client.post(
        f"/api/collectors/destinations/{dest_id}/credential/rotate",
        json={"new_secret": "v2"},
    )
    r2 = client.post(
        f"/api/collectors/destinations/{dest_id}/credential/rotate",
        json={"new_secret": "v3"},
    )

    assert r1.json()["secret_version"] == 2
    assert r2.json()["secret_version"] == 3


def test_revoke_then_rotate_re_enables_is_false_after_revoke(client_factory) -> None:
    """After revoke the destination is disabled; rotate re-keys but does NOT
    automatically re-enable (operator must call PUT /enabled=True separately)."""
    from backend.app.db import models as _m

    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest_id = client.post(
        "/api/collectors/destinations",
        json=_splunk_payload(hec_token="initial"),
    ).json()["id"]

    client.post(f"/api/collectors/destinations/{dest_id}/credential/revoke")
    client.post(
        f"/api/collectors/destinations/{dest_id}/credential/rotate",
        json={"new_secret": "re-keyed"},
    )

    with SessionLocal() as db:
        row = db.query(_m.Destination).filter_by(id=dest_id).first()
        # Re-key clears revoked_at but does NOT flip enabled back to True.
        assert row.secret_revoked_at is None  # cleared by rotate
        assert row.enabled is False  # still disabled — must be re-enabled via PUT


# ── GET /{id}/lineage ─────────────────────────


def test_destination_lineage_flag_off_returns_empty(client_factory) -> None:
    """LINEAGE_ENABLED=False → 200 with empty entries (endpoint always routable)."""
    import fakeredis
    from backend.app.collectors.output import lineage as lin
    from backend.app.core.config import settings

    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest_id = client.post("/api/collectors/destinations", json=_syslog_payload()).json()["id"]

    with (
        patch.object(settings, "LINEAGE_ENABLED", False),
        patch.object(lin, "_redis", return_value=fakeredis.FakeStrictRedis(decode_responses=True)),
    ):
        r = client.get(f"/api/collectors/destinations/{dest_id}/lineage?event_id=e1")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["destination_id"] == dest_id
    assert body["event_id"] == "e1"
    assert body["entries"] == []


def test_destination_lineage_returns_delivery_entry(client_factory) -> None:
    """With LINEAGE_ENABLED=True, recorded delivery entry is returned."""
    import fakeredis
    from backend.app.collectors.output import lineage as lin
    from backend.app.core.config import settings

    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    # Create a destination and get its org (global admin has no org → use a row org)
    client.post("/api/collectors/destinations", json=_syslog_payload())

    r = fakeredis.FakeStrictRedis(decode_responses=True)

    # Seed lineage for org_id=None: global admin with no org falls through →
    # We need to set an org on the destination. Since we're a global admin (no
    # per-org restriction), we instead write the lineage at the module level and
    # query directly via the module (unit layer). For the router endpoint we need
    # the dest's org. Let's patch settings to set LINEAGE_ENABLED=True and
    # manually populate the fake redis for org_id resolved from the destination.
    # A global admin on a destination with org_id=None → effective_org=None → empty.
    # So we use a destination with an explicit org.

    # Create org and org-scoped admin
    org = _create_org(client, "Lineage Test Org")
    org_id = org["id"]
    dest2 = client.post(
        "/api/collectors/destinations",
        json=_syslog_payload(name="Org Syslog", organization_id=org_id),
    ).json()
    dest2_id = dest2["id"]

    with patch.object(lin, "_redis", return_value=r):
        lin.record_delivery.__wrapped__ = None  # ensure not already monkeypatched
        # Bypass the _is_enabled gate by calling the inner logic directly.
        key = lin._lineage_key(org_id, "event-xyz")
        import json as _json
        r.lpush(key, _json.dumps({
            "destination_id": dest2_id,
            "kind": "syslog_rfc3164",
            "status": "delivered",
            "ts": 1_718_000_000.0,
        }))

        with patch.object(settings, "LINEAGE_ENABLED", True):
            resp = client.get(
                f"/api/collectors/destinations/{dest2_id}/lineage?event_id=event-xyz"
            )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["destination_id"] == dest2_id
    assert body["event_id"] == "event-xyz"
    assert len(body["entries"]) == 1
    assert body["entries"][0]["destination_id"] == dest2_id
    assert body["entries"][0]["status"] == "delivered"
    assert "retention_note" in body


def test_destination_lineage_requires_admin(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    r = client.get("/api/collectors/destinations/whatever/lineage?event_id=e1")
    assert r.status_code in (401, 403), r.text


def test_destination_lineage_unknown_destination_404(client_factory) -> None:
    from backend.app.core.config import settings

    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    with patch.object(settings, "LINEAGE_ENABLED", True):
        r = client.get("/api/collectors/destinations/nonexistent/lineage?event_id=e1")
    assert r.status_code == 404, r.text


def test_destination_lineage_missing_event_id_422(client_factory) -> None:
    """Missing required event_id query param → 422 from Pydantic."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest_id = client.post("/api/collectors/destinations", json=_syslog_payload()).json()["id"]
    r = client.get(f"/api/collectors/destinations/{dest_id}/lineage")
    assert r.status_code == 422, r.text


# ── GET /collectors/lineage/{event_id} (admin, org-scoped) ──────────


def test_event_lineage_flag_off_returns_empty(client_factory) -> None:
    """LINEAGE_ENABLED=False → 200 with empty entries."""
    import fakeredis
    from backend.app.collectors.output import lineage as lin
    from backend.app.core.config import settings

    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    # Need an org-scoped user because global admin without org_id gets 400
    org = _create_org(client, "Lineage Org2")
    org_id = org["id"]
    _create_admin_user(client, username="lin-admin", password="AdminPass123!", organization_id=org_id)
    _login(client, "lin-admin", "AdminPass123!")

    fake = fakeredis.FakeStrictRedis(decode_responses=True)
    with (
        patch.object(settings, "LINEAGE_ENABLED", False),
        patch.object(lin, "_redis", return_value=fake),
    ):
        r = client.get("/api/collectors/lineage/event-abc")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["event_id"] == "event-abc"
    assert body["entries"] == []


def test_event_lineage_global_admin_requires_org_id(client_factory) -> None:
    """Global admin without ?org_id → 400 (no cross-tenant query without scope)."""
    from backend.app.core.config import settings

    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    with patch.object(settings, "LINEAGE_ENABLED", True):
        r = client.get("/api/collectors/lineage/some-event")
    assert r.status_code == 400, r.text


def test_event_lineage_requires_admin(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    r = client.get("/api/collectors/lineage/some-event")
    assert r.status_code in (401, 403), r.text


def test_event_lineage_returns_all_destinations_for_event(client_factory) -> None:
    """Org-scoped admin can retrieve lineage for an event across destinations."""
    import fakeredis
    import json as _json
    from backend.app.collectors.output import lineage as lin
    from backend.app.core.config import settings

    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    org = _create_org(client, "Lineage Org Multi")
    org_id = org["id"]
    _create_admin_user(
        client, username="lin-multi", password="AdminPass123!", organization_id=org_id
    )
    _login(client, "lin-multi", "AdminPass123!")

    fake = fakeredis.FakeStrictRedis(decode_responses=True)
    key = lin._lineage_key(org_id, "multi-event")

    with patch.object(lin, "_redis", return_value=fake):
        # Seed two destinations for the same event
        fake.lpush(
            key,
            _json.dumps({"destination_id": "d1", "kind": "jsonl", "status": "delivered", "ts": 1.0}),
            _json.dumps({"destination_id": "d2", "kind": "otlp", "status": "delivered", "ts": 2.0}),
        )

        with patch.object(settings, "LINEAGE_ENABLED", True):
            r = client.get("/api/collectors/lineage/multi-event")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["event_id"] == "multi-event"
    assert body["organization_id"] == org_id
    dest_ids = {e["destination_id"] for e in body["entries"]}
    assert dest_ids == {"d1", "d2"}
    assert "retention_note" in body


def _fake_redis_for_lineage():
    """Alias for clarity in tests."""
    import fakeredis
    return fakeredis.FakeStrictRedis(decode_responses=True)


# ── editar wazuh-default persiste na própria Destination.config ──
# (a lane dual + a projeção p/ CollectorConfig.wazuh_* foram removidas)


def _seed_wazuh_default(SessionLocal, *, kind="syslog_rfc3164", config=None) -> None:
    """Seed the wazuh-default destination row + the singleton CollectorConfig,
    mirroring the lightweight migration seed."""
    from backend.app.collectors.output.destinations.registry import (
        compute_config_version,
    )

    cfg = (
        config
        if config is not None
        else {"host": "old.host", "port": 514, "use_tls": False, "ca_bundle": None}
    )
    with SessionLocal() as s:
        s.add(
            models.CollectorConfig(
                id=1,
                wazuh_syslog_host="old.host",
                wazuh_syslog_port=514,
                wazuh_dispatch_mode="syslog",
                wazuh_syslog_format="rfc3164",
            )
        )
        s.add(
            models.Destination(
                id="wazuh-default",
                name="Wazuh (default)",
                kind=kind,
                enabled=True,
                config=json.dumps(cfg),
                delivery="{}",
                config_version=compute_config_version(cfg, {}),
                organization_id=None,
            )
        )
        s.commit()


def test_update_wazuh_default_persists_to_destination_config(client_factory) -> None:
    """editar wazuh-default em /destinations persiste na PRÓPRIA
    ``Destination.config`` (fonte de verdade da entrega — dispatch_batch_to_destination
    lê dela). A projeção p/ CollectorConfig.wazuh_* foi removida (lane dual deletada)."""
    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)
    _seed_wazuh_default(SessionLocal)

    r = client.put(
        "/api/collectors/destinations/wazuh-default",
        json={
            "config": {
                "host": "new.siem.local",
                "port": 6514,
                "use_tls": True,
                "ca_bundle": "/etc/ca.pem",
            }
        },
    )
    assert r.status_code == 200, r.text

    with SessionLocal() as s:
        dest = s.query(models.Destination).filter_by(id="wazuh-default").first()
        cfg = json.loads(dest.config)
        assert cfg["host"] == "new.siem.local"
        assert int(cfg["port"]) == 6514
        assert cfg["use_tls"] is True
        assert cfg["ca_bundle"] == "/etc/ca.pem"
        # Sem projeção: CollectorConfig.wazuh_* NÃO é mais tocado pela edição.
        cc = s.query(models.CollectorConfig).filter_by(id=1).first()
        assert cc.wazuh_syslog_host == "old.host"


def test_update_wazuh_default_jsonl_persists_to_destination_config(client_factory) -> None:
    """jsonl-kind wazuh-default: a edição persiste no Destination.config; sem projeção."""
    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)
    _seed_wazuh_default(SessionLocal, kind="jsonl", config={"jsonl_dir": "/old/dir"})

    r = client.put(
        "/api/collectors/destinations/wazuh-default",
        json={"config": {"jsonl_dir": "/var/log/new"}},
    )
    assert r.status_code == 200, r.text

    with SessionLocal() as s:
        dest = s.query(models.Destination).filter_by(id="wazuh-default").first()
        cfg = json.loads(dest.config)
        assert cfg["jsonl_dir"] == "/var/log/new"
        # Sem projeção: collector_jsonl_dir do CollectorConfig inalterado pela edição.
        cc = s.query(models.CollectorConfig).filter_by(id=1).first()
        assert cc.collector_jsonl_dir != "/var/log/new"


def test_update_non_wazuh_destination_does_not_touch_collector_config(
    client_factory,
) -> None:
    """A regular destination update must NOT write through to CollectorConfig."""
    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)
    _seed_wazuh_default(SessionLocal)
    created = client.post(
        "/api/collectors/destinations", json=_syslog_payload(name="Other")
    ).json()

    client.put(
        f"/api/collectors/destinations/{created['id']}",
        json={"config": {"host": "changed.host", "port": 1234}},
    )

    with SessionLocal() as s:
        cc = s.query(models.CollectorConfig).filter_by(id=1).first()
        # Unchanged — only wazuh-default writes through.
        assert cc.wazuh_syslog_host == "old.host"


# ── auto-rota broadcast na criação do destino ──────


def test_create_destination_auto_creates_broadcast_route(client_factory) -> None:
    """Criar um destino gera automaticamente uma rota broadcast ``{} → [dest]``
    (clone+continue), de forma visível/editável em /routes — o destino recebe
    todos os eventos por default (modelo Cribl 'tudo é rota')."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    created = client.post(
        "/api/collectors/destinations", json=_syslog_payload(name="Auto Dest")
    ).json()

    routes = client.get("/api/collectors/routes").json()
    matching = [r for r in routes if created["id"] in r["destination_ids"]]
    assert len(matching) == 1
    r = matching[0]
    assert r["condition"] == {}  # casa tudo (broadcast)
    assert r["is_final"] is False  # clone+continue → segue ao catch-all wazuh
    assert r["action"] == "route"


def test_create_destination_auto_route_false_skips(client_factory) -> None:
    """``auto_route: false`` no payload → nenhuma rota auto-criada (roteamento
    explícito puro: o destino só recebe quando o operador cria uma rota)."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    payload = _syslog_payload(name="No Auto Route")
    payload["auto_route"] = False
    created = client.post("/api/collectors/destinations", json=payload).json()

    routes = client.get("/api/collectors/routes").json()
    matching = [r for r in routes if created["id"] in r.get("destination_ids", [])]
    assert matching == []
