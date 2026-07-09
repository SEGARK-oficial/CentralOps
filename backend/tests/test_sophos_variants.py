"""Sophos split em 3 variantes-card.

Trava que ``sophos_partner``/``sophos_organization`` são cards distintos na
galeria que mapeiam para ``Integration.platform="sophos"`` + ``kind`` no create
(via ``base_platform``) — collectors/providers downstream continuam vendo
"sophos", zero ripple. O card base "sophos" segue funcionando (back-compat).
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import models
from backend.app.db.database import Base, get_session
from backend.app.main import app
from backend.app.providers.base import HealthResult
from backend.app.routers import integrations as ri


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


def test_catalog_exposes_three_sophos_cards(db_client):
    client, _ = db_client
    _bootstrap_admin(client)
    slugs = {p["platform"] for p in client.get("/api/providers/platforms").json()}
    assert {"sophos", "sophos_partner", "sophos_organization"} <= slugs


@pytest.mark.parametrize("variant_platform,expected_kind", [
    ("sophos_partner", "partner"),
    ("sophos_organization", "organization"),
])
def test_variant_card_maps_to_base_platform_and_kind(db_client, variant_platform, expected_kind):
    client, Session = db_client
    _bootstrap_admin(client)
    org_id = _create_org(client)

    fake_provider = MagicMock()
    with patch.object(ri, "get_provider", return_value=fake_provider):
        resp = client.post("/api/integrations/", json={
            "organization_id": org_id,
            "name": f"{variant_platform} acct",
            "platform": variant_platform,
            "client_id": "cid",
            "client_secret": "sec",
        })
    assert resp.status_code == 200, resp.text

    db = Session()
    try:
        row = db.query(models.Integration).filter_by(id=resp.json()["id"]).one()
        # persistido na plataforma-BASE + kind derivado da variante
        assert row.platform == "sophos"
        assert row.kind == expected_kind
        # client_secret no store integration_credentials (não em coluna)
        from backend.app.services import integration_secrets as _iss
        assert _iss.read_secret(row, "client_secret") == "sec"
        assert row.client_secret is None
    finally:
        db.close()
    # variante MSSP dispara o hook de descoberta (provider.on_created)
    fake_provider.on_created.assert_called_once()


def test_base_sophos_card_still_creates_tenant(db_client):
    """Back-compat: o card base "sophos" cria um tenant (sem variante)."""
    client, Session = db_client
    _bootstrap_admin(client)
    org_id = _create_org(client)

    fake = MagicMock()
    fake.test_connection.return_value = HealthResult(status="healthy", details={})
    with patch.object(ri, "get_provider", return_value=fake):
        resp = client.post("/api/integrations/", json={
            "organization_id": org_id,
            "name": "tenant acct",
            "platform": "sophos",
            "client_id": "cid",
            "client_secret": "sec",
            "region": "eu01",
        })
    assert resp.status_code == 200, resp.text
    db = Session()
    try:
        row = db.query(models.Integration).filter_by(id=resp.json()["id"]).one()
        assert row.platform == "sophos"
        assert row.kind == "tenant"
    finally:
        db.close()


def test_partner_kind_via_base_card_still_supported(db_client):
    """O caminho legado (platform=sophos + kind=partner) segue válido — a base
    "sophos" mantém a capability discover:children (aditivo, não remove)."""
    client, Session = db_client
    _bootstrap_admin(client)
    org_id = _create_org(client)

    fake = MagicMock()
    with patch.object(ri, "get_provider", return_value=fake):
        resp = client.post("/api/integrations/", json={
            "organization_id": org_id,
            "name": "legacy partner",
            "platform": "sophos",
            "kind": "partner",
            "client_id": "cid",
            "client_secret": "sec",
        })
    assert resp.status_code == 200, resp.text
    db = Session()
    try:
        row = db.query(models.Integration).filter_by(id=resp.json()["id"]).one()
        assert row.platform == "sophos"
        assert row.kind == "partner"
    finally:
        db.close()
