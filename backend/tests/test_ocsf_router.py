"""/api/ocsf router: per-org enforcement policy + compliance report.

Uses the in-memory SQLite + TestClient harness (mirrors test_pipeline_health_router).
Imports use ``backend.app.*`` (compiled .so dual-root gotcha).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.collectors import quarantine
from backend.app.db import models
from backend.app.db.database import Base, get_session
from backend.app.main import app


@pytest.fixture()
def client_factory() -> Generator[Any, None, None]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_session():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_session] = override_get_session
    clients: list[TestClient] = []
    try:
        yield (lambda: clients.append(c := TestClient(app)) or c), TestingSession
    finally:
        for c in clients:
            c.close()
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)


def _bootstrap_admin(client: TestClient) -> None:
    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPassword123!", "display_name": "Admin"},
    )
    assert r.status_code in (200, 201), r.text


def _seed_org(Session, name: str = "Acme") -> int:
    with Session() as db:
        org = models.Organization(name=name, slug=name.lower())
        db.add(org)
        db.commit()
        db.refresh(org)
        return org.id


# ── policies ──────────────────────────────────────────────────────────────────

def test_list_policies_shows_global_default_for_orgs_without_row(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)
    org_id = _seed_org(Session)

    r = client.get("/api/ocsf/policies")
    assert r.status_code == 200, r.text
    rows = {p["organization_id"]: p for p in r.json()}
    assert org_id in rows
    assert rows[org_id]["enforcement_mode"] == "tag_and_pass"  # global default
    assert rows[org_id]["is_default"] is True


def test_set_and_read_policy(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)
    org_id = _seed_org(Session)

    r = client.put(f"/api/ocsf/policies/{org_id}", json={"enforcement_mode": "quarantine"})
    assert r.status_code == 200, r.text
    assert r.json()["enforcement_mode"] == "quarantine"
    assert r.json()["is_default"] is False

    rows = {p["organization_id"]: p for p in client.get("/api/ocsf/policies").json()}
    assert rows[org_id]["enforcement_mode"] == "quarantine"
    assert rows[org_id]["is_default"] is False

    # update again (upsert path)
    r2 = client.put(f"/api/ocsf/policies/{org_id}", json={"enforcement_mode": "fail_closed"})
    assert r2.status_code == 200 and r2.json()["enforcement_mode"] == "fail_closed"


def test_set_policy_invalid_mode_422(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)
    org_id = _seed_org(Session)
    r = client.put(f"/api/ocsf/policies/{org_id}", json={"enforcement_mode": "nope"})
    assert r.status_code == 422, r.text


def test_set_policy_unknown_org_404(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    r = client.put("/api/ocsf/policies/99999", json={"enforcement_mode": "quarantine"})
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "org.not_found"


def test_policies_require_admin(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    # no bootstrap / no auth → not allowed
    assert client.get("/api/ocsf/policies").status_code in (401, 403)


# ── compliance ────────────────────────────────────────────────────────────────

def test_compliance_report_counts_validate_quarantine(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)
    org_id = _seed_org(Session)
    with Session() as db:
        integ = models.Integration(
            organization_id=org_id, name="fgt", platform="fortinet_fortigate", kind="tenant"
        )
        db.add(integ)
        db.commit()
        db.refresh(integ)
        iid = integ.id
        # 2 validate-quarantine events in the window + 1 other-kind (must NOT count)
        now = datetime.utcnow()
        for _ in range(2):
            db.add(models.QuarantineEvent(
                organization_id=org_id, integration_id=iid, vendor="fortinet_fortigate",
                event_type="fortinet_fortigate.traffic", raw_payload="{}",
                error_kind=quarantine.ERROR_KIND_VALIDATE, created_at=now,
                expires_at=now + timedelta(days=7),
            ))
        db.add(models.QuarantineEvent(
            organization_id=org_id, integration_id=iid, vendor="fortinet_fortigate",
            event_type="fortinet_fortigate.traffic", raw_payload="{}",
            error_kind=quarantine.ERROR_KIND_MISSING_CUSTOMER_ID, created_at=now,
            expires_at=now + timedelta(days=7),
        ))
        db.commit()

    r = client.get("/api/ocsf/compliance")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["validation_enabled"] is False
    assert body["global_default"] == "tag_and_pass"
    assert body["ocsf_version"] == "1.8.0"
    item = next(i for i in body["items"] if i["integration_id"] == iid)
    assert item["invalid_quarantined_24h"] == 2  # only ERROR_KIND_VALIDATE counted
    assert item["enforcement_mode"] == "tag_and_pass"
