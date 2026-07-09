"""Store de credencial vendor-neutro.

Trava que TODOS os vendors guardam o segredo na tabela ``integration_credentials``
(não na coluna batizada), com lifecycle (rotate/revoke) e leitura tolerante a
``detached`` (lazy="selectin"). Sophos/Wazuh migraram — também travado aqui.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.api import schemas
from backend.app.collectors.registry import get_platform
from backend.app.db import models
from backend.app.db.database import Base, get_session
from backend.app.main import app
from backend.app.routers import integrations as ri
from backend.app.services import integration_secrets as iss


@pytest.fixture()
def db_client():
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
    with TestClient(app) as client, patch.object(
        ri, "_trigger_initial_collection", lambda *a, **k: None
    ), patch.object(ri, "_register_in_beat", lambda *a, **k: None):
        yield client, TestingSession
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


def _bootstrap_admin(client: TestClient) -> None:
    resp = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPassword123!", "display_name": "Admin"},
    )
    assert resp.status_code == 200, resp.text


def _create_org(client: TestClient) -> int:
    resp = client.post("/api/organizations/", json={"name": "Test Org"})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _ninja(org_id: int, **over: Any) -> dict[str, Any]:
    base = {
        "organization_id": org_id,
        "name": "Ninja",
        "platform": "ninjaone",
        "client_id": "cid",
        "client_secret": "topsecret",
        "base_url": "https://app.ninjarmm.com",
    }
    base.update(over)
    return base


# ── HTTP: create / update via store ───────────────────────────────────

def test_create_stores_secret_in_table_not_column(db_client):
    client, Session = db_client
    _bootstrap_admin(client)
    org_id = _create_org(client)
    resp = client.post("/api/integrations/", json=_ninja(org_id))
    assert resp.status_code == 200, resp.text
    integ_id = resp.json()["id"]

    db = Session()
    try:
        integ = db.query(models.Integration).filter_by(id=integ_id).one()
        # coluna batizada NÃO é usada por vendor genérico
        assert integ.client_secret is None
        # store tem o segredo cifrado (não em claro)
        creds = db.query(models.IntegrationCredential).filter_by(integration_id=integ_id).all()
        assert len(creds) == 1
        assert creds[0].logical_name == "client_secret"
        assert creds[0].secret_ref != "topsecret"
        assert creds[0].secret_version == 1
        assert iss.read_secret(integ, "client_secret") == "topsecret"
    finally:
        db.close()


def test_update_rotates_secret_in_store(db_client):
    client, Session = db_client
    _bootstrap_admin(client)
    org_id = _create_org(client)
    integ_id = client.post("/api/integrations/", json=_ninja(org_id)).json()["id"]

    resp = client.put(
        f"/api/integrations/{integ_id}", json={"client_secret": "rotated-secret"}
    )
    assert resp.status_code == 200, resp.text

    db = Session()
    try:
        cred = (
            db.query(models.IntegrationCredential)
            .filter_by(integration_id=integ_id, logical_name="client_secret")
            .one()
        )
        assert cred.secret_version == 2  # rotacionado
        assert cred.rotated_at is not None
        integ = db.query(models.Integration).filter_by(id=integ_id).one()
        assert iss.read_secret(integ, "client_secret") == "rotated-secret"
    finally:
        db.close()


# ── Unit: store mechanics ─────────────────────────────────────────────

def test_sophos_secret_goes_to_store(db_client):
    """Sophos/Wazuh migraram — client_secret no store, coluna NÃO usada."""
    _client, Session = db_client
    db = Session()
    try:
        org = models.Organization(name="o", slug="o")
        db.add(org)
        db.commit()
        data = schemas.IntegrationCreate(
            organization_id=org.id, name="s", platform="sophos",
            client_id="cid", client_secret="sophossecret",
        )
        integ = models.Integration(organization_id=org.id, name="s", platform="sophos", kind="tenant")
        ri._assign_credentials(integ, data, get_platform("sophos"), "tenant")
        db.add(integ)
        db.commit()
        db.refresh(integ)
        # secret no store integration_credentials (vendor-neutro); coluna legada vazia
        assert iss.read_secret(integ, "client_secret") == "sophossecret"
        assert db.query(models.IntegrationCredential).filter_by(integration_id=integ.id).count() == 1
        assert integ.client_secret is None
    finally:
        db.close()


def test_store_revoke_hides_secret(db_client):
    _client, Session = db_client
    db = Session()
    try:
        org = models.Organization(name="o", slug="o")
        db.add(org)
        db.commit()
        integ = models.Integration(organization_id=org.id, name="n", platform="ninjaone", kind="tenant")
        iss.write_secret(integ, "client_secret", "s3cr3t")
        db.add(integ)
        db.commit()
        assert iss.read_secret(integ, "client_secret") == "s3cr3t"
        assert iss.revoke_secret(integ, "client_secret") is True
        db.commit()
        # revogado some da leitura, mas a linha persiste (auditoria)
        assert iss.read_secret(integ, "client_secret") is None
        assert db.query(models.IntegrationCredential).filter_by(integration_id=integ.id).count() == 1
        # detached: selectin já materializou — leitura ainda funciona
        db.expunge(integ)
        assert iss.read_secret(integ, "client_secret") is None
    finally:
        db.close()


def test_exotic_secret_without_column_is_stored(db_client):
    """Cred exótica (sem coluna dedicada) ainda é guardada no store."""
    _client, Session = db_client
    db = Session()
    try:
        org = models.Organization(name="o", slug="o")
        db.add(org)
        db.commit()
        integ = models.Integration(organization_id=org.id, name="aws", platform="aws", kind="tenant")
        iss.write_secret(integ, "aws_secret_access_key", "AKIA-secret-value")
        db.add(integ)
        db.commit()
        assert iss.read_secret(integ, "aws_secret_access_key") == "AKIA-secret-value"
    finally:
        db.close()
