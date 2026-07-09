"""Tests for the service-to-service /api/internal/* router consumed by IASOC."""

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

from backend.app.db.database import Base, get_session
from backend.app.main import app


@pytest.fixture
def client(monkeypatch):
    from backend.app.core import config as _cfg

    monkeypatch.setattr(_cfg.settings, "CENTRALOPS_INTERNAL_API_KEY", "test-secret-key")

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    # Seed: one Org + one Sophos child.
    with TestingSession() as db:
        db.execute(text(
            "INSERT INTO organizations(name, slug, is_active, created_at, updated_at, "
            "auto_managed, external_provider, external_id, iris_customer_id) "
            "VALUES ('ACME', 'acme', 1, datetime('now'), datetime('now'), 1, "
            "'sophos', 't-uuid', 42)"
        ))
        db.execute(text(
            "INSERT INTO integrations(organization_id, name, platform, is_active, "
            "kind, external_id, region, tenant_id, auth_status, created_at, "
            "updated_at, auto_managed) "
            "VALUES (1, 'ACME-sophos', 'sophos', 1, 'tenant', 't-uuid', 'eu03', "
            "'t-uuid', 'healthy', datetime('now'), datetime('now'), 1)"
        ))
        # a resolução por IRIS customer id vai pela tabela de mapping
        # (kind='iris'), não mais pela coluna deprecada organizations.iris_customer_id.
        db.execute(text(
            "INSERT INTO destination_customer_mappings(organization_id, "
            "destination_kind, external_customer_id, created_at, updated_at) "
            "VALUES (1, 'iris', '42', datetime('now'), datetime('now'))"
        ))
        db.commit()

    def override_get_session():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_session] = override_get_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


def test_internal_requires_api_key(client):
    r = client.get("/api/internal/tenants/by-iris-customer/42")
    assert r.status_code == 401


def test_internal_rejects_wrong_key(client):
    r = client.get(
        "/api/internal/tenants/by-iris-customer/42",
        headers={"X-Internal-Api-Key": "nope"},
    )
    assert r.status_code == 401


def test_internal_resolves_by_iris_customer(client):
    r = client.get(
        "/api/internal/tenants/by-iris-customer/42",
        headers={"X-Internal-Api-Key": "test-secret-key"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["organization_id"] == 1
    assert data["organization_slug"] == "acme"
    assert data["iris_customer_id"] == 42
    assert data["sophos"]["tenant_external_id"] == "t-uuid"
    assert data["sophos"]["region"] == "eu03"
    assert data["mcps_enabled"] == ["sophos"]


def test_internal_resolves_by_sophos_external_id(client):
    r = client.get(
        "/api/internal/tenants/by-sophos-tenant/t-uuid",
        headers={"X-Internal-Api-Key": "test-secret-key"},
    )
    assert r.status_code == 200
    assert r.json()["organization_id"] == 1


def test_internal_resolves_by_org_id(client):
    r = client.get(
        "/api/internal/tenants/1",
        headers={"X-Internal-Api-Key": "test-secret-key"},
    )
    assert r.status_code == 200
    assert r.json()["sophos"]["region"] == "eu03"


def test_internal_404_when_iris_unmapped(client):
    r = client.get(
        "/api/internal/tenants/by-iris-customer/9999",
        headers={"X-Internal-Api-Key": "test-secret-key"},
    )
    assert r.status_code == 404


def test_internal_503_when_key_unconfigured(client, monkeypatch):
    from backend.app.core import config as _cfg

    monkeypatch.setattr(_cfg.settings, "CENTRALOPS_INTERNAL_API_KEY", None)
    r = client.get(
        "/api/internal/tenants/by-iris-customer/42",
        headers={"X-Internal-Api-Key": "any"},
    )
    assert r.status_code == 503
