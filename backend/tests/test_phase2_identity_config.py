"""Fase 2A (Entra) — config de identidade/SSO no banco, operada pela UI.

Cobertura:
  * ``identity_config.load``: fallback ``.env`` quando vazio; leitura do banco
    com decifragem do secret.
  * Router ``/api/identity/config`` GET/PUT: admin-only, secret cifrado no
    banco e mascarado na resposta, preservado quando omitido, role_map validado.
  * Config pela UI controla o SSO: habilitar via PUT reflete em /auth/status,
    sem tocar no .env.
  * Hardening: desativar um usuário revoga seus PATs (fecha o gap de offboarding).
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core import identity_config
from backend.app.db import models
from backend.app.db.database import Base, get_session
from backend.app.db.repository import IdentityConfigRepository
from backend.app.main import app


@pytest.fixture()
def client_factory():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
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


def _bootstrap_admin(client: TestClient) -> dict[str, Any]:
    r = client.post("/api/auth/bootstrap", json={"username": "admin", "password": "AdminPassword123!"})
    assert r.status_code == 200, r.text
    return r.json()


# ── identity_config.load ──────────────────────────────────────────────


def test_load_falls_back_to_settings_when_empty(client_factory):
    _, Session = client_factory
    with Session() as s:
        snap = identity_config.load(s)
    assert snap.is_persisted is False  # veio do .env (sem linha no banco)


def test_repository_serializes_json_fields(client_factory):
    _, Session = client_factory
    with Session() as s:
        repo = IdentityConfigRepository(s)
        repo.update(entra_role_map={"AppAdmin": "admin"}, entra_allowed_email_domains=["x.com"])
    with Session() as s:
        snap = identity_config.load(s)
        assert snap.is_persisted is True
        assert snap.entra_role_map == {"AppAdmin": "admin"}
        assert snap.entra_allowed_email_domains == ["x.com"]


# ── Router: GET/PUT, crypto, mascaramento ─────────────────────────────


def test_get_config_requires_admin(client_factory):
    factory, _ = client_factory
    c = factory()
    _bootstrap_admin(c)
    # cria viewer e loga como ele
    c.post("/api/auth/users", json={"username": "v", "password": "Passw0rd123!", "role": "viewer"})
    viewer = factory()
    viewer.post("/api/auth/login", json={"username": "v", "password": "Passw0rd123!"})
    r = viewer.get("/api/identity/config")
    assert r.status_code == 403, r.text


def test_put_encrypts_secret_and_masks_response(client_factory):
    factory, Session = client_factory
    c = factory()
    _bootstrap_admin(c)
    r = c.put("/api/identity/config", json={
        "entra_tenant_id": "tid", "entra_client_id": "cid",
        "entra_client_secret": "my-plain-secret",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert "entra_client_secret" not in body          # nunca devolve o valor
    assert body["entra_client_secret_configured"] is True
    # no banco: cifrado (não é o plaintext)
    with Session() as s:
        row = s.query(models.IdentityConfig).filter_by(id=1).first()
        assert row.entra_client_secret not in (None, "", "my-plain-secret")
    # load() decifra de volta para uso interno
    with Session() as s:
        assert identity_config.load(s).entra_client_secret == "my-plain-secret"


def test_put_preserves_secret_when_omitted(client_factory):
    factory, Session = client_factory
    c = factory()
    _bootstrap_admin(c)
    c.put("/api/identity/config", json={"entra_client_secret": "keep-me", "entra_client_id": "cid"})
    # update posterior sem secret não deve apagá-lo
    c.put("/api/identity/config", json={"entra_button_label": "Login MS"})
    with Session() as s:
        assert identity_config.load(s).entra_client_secret == "keep-me"


def test_put_rejects_invalid_role_map(client_factory):
    factory, _ = client_factory
    c = factory()
    _bootstrap_admin(c)
    r = c.put("/api/identity/config", json={"entra_role_map": {"X": "superadmin"}})
    assert r.status_code == 422, r.text


def test_put_rejects_unsafe_identifier(client_factory):
    """client_id/tenant_id com aspas (OData injection) sao rejeitados (422)."""
    factory, _ = client_factory
    c = factory()
    _bootstrap_admin(c)
    assert c.put("/api/identity/config", json={"entra_client_id": "abc'or'1"}).status_code == 422
    assert c.put("/api/identity/config", json={"entra_tenant_id": "x') or 1=1--"}).status_code == 422
    # GUID e dominio validos passam:
    ok = c.put("/api/identity/config", json={
        "entra_client_id": "00000000-0000-0000-0000-000000000000",
        "entra_tenant_id": "contoso.onmicrosoft.com",
    })
    assert ok.status_code == 200, ok.text


def test_put_rejects_non_microsoft_authority(client_factory):
    """authority fora da allowlist Microsoft (anti-SSRF) e rejeitado (422)."""
    factory, _ = client_factory
    c = factory()
    _bootstrap_admin(c)
    assert c.put("/api/identity/config", json={"entra_authority": "http://169.254.169.254"}).status_code == 422
    assert c.put("/api/identity/config", json={"entra_authority": "https://evil.example.com"}).status_code == 422
    ok = c.put("/api/identity/config", json={"entra_authority": "https://login.microsoftonline.us"})
    assert ok.status_code == 200, ok.text


def test_config_via_ui_controls_sso(client_factory):
    """O coração da Fase 2A: habilitar SSO pela UI (banco) reflete no /status,
    sem nenhuma env var."""
    factory, _ = client_factory
    c = factory()
    _bootstrap_admin(c)

    assert c.get("/api/auth/status").json()["sso_enabled"] is False

    r = c.put("/api/identity/config", json={
        "entra_enabled": True,
        "entra_tenant_id": "tid",
        "entra_client_id": "cid",
        "entra_client_secret": "secret",
        "entra_redirect_uri": "https://app.test/api/auth/sso/callback",
        "entra_button_label": "Entrar com a Microsoft",
    })
    assert r.status_code == 200, r.text

    status = c.get("/api/auth/status").json()
    assert status["sso_enabled"] is True
    assert status["sso_button_label"] == "Entrar com a Microsoft"


# ── Hardening: revogar PATs no offboarding ────────────────────────────


def test_deactivating_user_revokes_pats(client_factory):
    factory, Session = client_factory
    c = factory()
    _bootstrap_admin(c)
    created = c.post("/api/auth/users", json={"username": "u", "password": "Passw0rd123!", "role": "operator"}).json()
    uid = created["id"]

    # cria um PAT pessoal ativo para o usuário (direto no banco)
    with Session() as s:
        user = s.query(models.AppUser).filter_by(uuid=uid).first()
        s.add(models.ApiToken(
            user_id=user.id, name="ci-token",
            token_prefix="copsk_test1234", token_hash="$argon2id$fake",
            is_eternal=True, use_count=0,
        ))
        s.commit()
        token_id = s.query(models.ApiToken).filter_by(user_id=user.id).first().id

    # desativa o usuário via API
    r = c.put(f"/api/auth/users/{uid}", json={"is_active": False})
    assert r.status_code == 200, r.text

    with Session() as s:
        tok = s.query(models.ApiToken).filter_by(id=token_id).first()
        assert tok.revoked_at is not None, "PAT deveria ter sido revogado no offboarding"
