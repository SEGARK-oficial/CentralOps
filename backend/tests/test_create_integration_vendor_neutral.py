"""Criação de integração vendor-neutra (carve-out morto).

Trava que ``POST /api/integrations`` é plugin-driven: qualquer plataforma do
registry é criável (não só sophos/wazuh), a validação vem do catálogo (não de um
Literal hardcoded), e as credenciais são atribuídas/ cifradas genericamente a
partir das ``auth_fields`` declaradas pelo vendor.

Foco em ninjaone/defender (sem provider rico ⇒ criação hermética, sem rede). Era
impossível criar ninjaone/defender pela API (Literal sophos/wazuh +
branch ``if data.platform != 'sophos'``); agora é.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db.database import Base, get_session
from backend.app.main import app


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
    # Isola os side-effects fire-and-forget do on-create (1ª coleta + RedBeat):
    # sem Redis/broker no test eles dão timeout lento (são capturados, mas
    # atrasam). A corretude deles é coberta em test_integration_collector_hook.
    from backend.app.routers import integrations as _ri

    with TestClient(app) as client, patch.object(
        _ri, "_trigger_initial_collection", lambda *a, **k: None
    ), patch.object(_ri, "_register_in_beat", lambda *a, **k: None):
        yield client
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


def _ninjaone_payload(org_id: int, **over: Any) -> dict[str, Any]:
    base = {
        "organization_id": org_id,
        "name": "Ninja Test",
        "platform": "ninjaone",
        "client_id": "ninja-cid",
        "client_secret": "ninja-secret",
        "base_url": "https://app.ninjarmm.com",
    }
    base.update(over)
    return base


def _defender_payload(org_id: int, **over: Any) -> dict[str, Any]:
    base = {
        "organization_id": org_id,
        "name": "Defender Test",
        "platform": "microsoft_defender",
        "tenant_id": "azure-tenant",
        "client_id": "def-cid",
        "client_secret": "def-secret",
    }
    base.update(over)
    return base


class TestVendorNeutralCreate:
    def test_ninjaone_is_createable(self, db_client):
        """ninjaone era IMPOSSÍVEL de criar (Literal sophos/wazuh). Agora 200."""
        _bootstrap_admin(db_client)
        org_id = _create_org(db_client)
        resp = db_client.post("/api/integrations/", json=_ninjaone_payload(org_id))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["platform"] == "ninjaone"
        # client_secret NUNCA volta no payload (não há campo de leitura p/ ele)
        assert "client_secret" not in body or body.get("client_secret") is None
        # campos genéricos do capability model ecoam (admin vê client_id/base_url)
        assert body["client_id"] == "ninja-cid"
        assert body["base_url"] == "https://app.ninjarmm.com"

    def test_defender_is_createable(self, db_client):
        _bootstrap_admin(db_client)
        org_id = _create_org(db_client)
        resp = db_client.post("/api/integrations/", json=_defender_payload(org_id))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["platform"] == "microsoft_defender"
        assert body["tenant_id"] == "azure-tenant"

    def test_unknown_platform_is_rejected_by_registry(self, db_client):
        """Validação vem do registry (catálogo), não de Literal — desconhecido = 400."""
        _bootstrap_admin(db_client)
        org_id = _create_org(db_client)
        resp = db_client.post(
            "/api/integrations/",
            json={"organization_id": org_id, "name": "x", "platform": "totally-fake-vendor"},
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["code"] == "integration.platform_unsupported"
        assert resp.json()["error"]["details"]["platform"] == "totally-fake-vendor"

    def test_partner_kind_rejected_on_non_discover_platform(self, db_client):
        """kind=partner exige capability discover:children — ninjaone não tem."""
        _bootstrap_admin(db_client)
        org_id = _create_org(db_client)
        resp = db_client.post(
            "/api/integrations/", json=_ninjaone_payload(org_id, kind="partner")
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["code"] == "integration.kind_requires_discovery"

    def test_missing_required_credential_is_rejected(self, db_client):
        """Validação de obrigatório vem das auth_fields do vendor (genérica)."""
        _bootstrap_admin(db_client)
        org_id = _create_org(db_client)
        payload = _ninjaone_payload(org_id)
        payload.pop("client_secret")
        resp = db_client.post("/api/integrations/", json=payload)
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["code"] == "integration.missing_required_fields"
        assert "Client Secret" in resp.json()["error"]["details"]["fields"]

    def test_secret_is_encrypted_at_rest(self, db_client):
        """O client_secret é cifrado em repouso — no store integration_credentials
        (ninjaone é vendor-neutro), nunca em claro nem na coluna."""
        from backend.app.db import models
        from backend.app.db.database import get_session as _gs

        _bootstrap_admin(db_client)
        org_id = _create_org(db_client)
        resp = db_client.post("/api/integrations/", json=_ninjaone_payload(org_id))
        assert resp.status_code == 200, resp.text
        integ_id = resp.json()["id"]
        gen = app.dependency_overrides[_gs]()
        db = next(gen)
        try:
            row = db.query(models.Integration).filter_by(id=integ_id).one()
            # vendor-neutro NÃO usa a coluna batizada — secret vai pro store
            assert row.client_secret is None
            assert row.base_url == "https://app.ninjarmm.com"
            cred = (
                db.query(models.IntegrationCredential)
                .filter_by(integration_id=integ_id, logical_name="client_secret")
                .one()
            )
            assert cred.secret_ref not in (None, "", "ninja-secret")  # cifrado
        finally:
            db.close()
