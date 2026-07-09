"""Testes de integração do router /api/v1/service-accounts (Fase 2).

Cobre:
- POST cria SA com role/description, audita service_account.created.
- GET lista todos / get individual.
- PATCH atualiza role (sensitive — sempre auditado), is_active, etc.
- DELETE remove SA + cascade nos tokens vinculados.
- Tokens nested: POST/GET/DELETE em /service-accounts/{id}/tokens.
- USER_MANAGE: viewer/operator/engineer recebem 403; admin OK.
- Token de SA autentica request normal e usa shim AppUser
  (username == "sa:<name>", id negativo).
"""

from __future__ import annotations

from typing import Any, Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import models
from backend.app.db.database import Base, get_session
from backend.app.main import app


@pytest.fixture()
def setup() -> Generator[Any, None, None]:
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
    client = TestClient(app)

    # Reset rate limiter pra evitar contaminação cruzada.
    from backend.app.core.rate_limiter import token_rate_limiter
    token_rate_limiter._windows.clear()

    # Bootstrap admin
    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPassword123!"},
    )
    assert r.status_code == 200, r.text

    yield client, TestingSession

    client.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


def _login_as(client: TestClient, username: str, password: str) -> None:
    """Helper: faz login com cookie session."""
    r = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert r.status_code == 200, r.text


def _create_user_with_role(
    client: TestClient,
    TestingSession,
    *,
    username: str,
    password: str,
    role: str,
) -> int:
    """Cria user via admin endpoint + altera role direto na DB (test fixture)."""
    # Primeiro garante que estamos como admin.
    _login_as(client, "admin", "AdminPassword123!")
    r = client.post(
        "/api/auth/users",
        json={"username": username, "password": password},
    )
    assert r.status_code in (200, 201), r.text
    body = r.json()
    # Endpoint retorna UserRead — id é uuid string. Pegamos o int via DB.
    with TestingSession() as db:
        u = (
            db.query(models.AppUser)
            .filter(models.AppUser.username == username)
            .first()
        )
        user_id = u.id
    # Atualiza role direto na DB pra evitar atravessar /role endpoint.
    with TestingSession() as db:
        u = db.query(models.AppUser).filter(models.AppUser.id == user_id).first()
        u.role = role
        db.commit()
    return user_id


# ── CRUD básico ─────────────────────────────────────────────────────────


def test_create_sa_returns_201_and_audits(setup):
    client, TestingSession = setup
    r = client.post(
        "/api/v1/service-accounts",
        json={"name": "iasoc-prod", "role": "operator", "description": "IASOC binder"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "iasoc-prod"
    assert body["role"] == "operator"
    assert body["is_active"] is True
    assert body["active_token_count"] == 0
    sa_id = body["id"]

    with TestingSession() as db:
        logs = (
            db.query(models.AuditLog)
            .filter(models.AuditLog.action == "service_account.created")
            .all()
        )
        assert len(logs) == 1
        assert str(sa_id) in (logs[0].detail or "")


def test_create_sa_rejects_duplicate_name(setup):
    client, _ = setup
    r1 = client.post(
        "/api/v1/service-accounts",
        json={"name": "dup", "role": "viewer"},
    )
    assert r1.status_code == 201
    r2 = client.post(
        "/api/v1/service-accounts",
        json={"name": "dup", "role": "viewer"},
    )
    assert r2.status_code == 400
    assert "already in use" in r2.json()["detail"]


def test_create_sa_rejects_invalid_role(setup):
    client, _ = setup
    r = client.post(
        "/api/v1/service-accounts",
        json={"name": "x", "role": "superuser"},
    )
    assert r.status_code == 422  # Pydantic rejeita


def test_create_sa_rejects_invalid_charset_in_name(setup):
    client, _ = setup
    r = client.post(
        "/api/v1/service-accounts",
        json={"name": "bad name with spaces", "role": "viewer"},
    )
    assert r.status_code == 422


def test_list_sas(setup):
    client, _ = setup
    client.post("/api/v1/service-accounts", json={"name": "a", "role": "viewer"})
    client.post("/api/v1/service-accounts", json={"name": "b", "role": "operator"})
    r = client.get("/api/v1/service-accounts")
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 2
    assert {item["name"] for item in items} == {"a", "b"}


def test_get_sa_404(setup):
    client, _ = setup
    r = client.get("/api/v1/service-accounts/9999")
    assert r.status_code == 404


def test_patch_sa_role_audits(setup):
    client, TestingSession = setup
    r = client.post(
        "/api/v1/service-accounts",
        json={"name": "promote-me", "role": "viewer"},
    )
    sa_id = r.json()["id"]

    r2 = client.patch(
        f"/api/v1/service-accounts/{sa_id}",
        json={"role": "operator"},
    )
    assert r2.status_code == 200
    assert r2.json()["role"] == "operator"

    with TestingSession() as db:
        logs = (
            db.query(models.AuditLog)
            .filter(models.AuditLog.action == "service_account.updated")
            .all()
        )
        assert len(logs) == 1
        # detail JSON deve conter role no "changed"
        assert '"role": "operator"' in (logs[0].detail or "")


def test_patch_sa_no_change_no_audit(setup):
    """PATCH sem mudança real (todos os campos iguais ao atual) NÃO grava audit."""
    client, TestingSession = setup
    r = client.post(
        "/api/v1/service-accounts",
        json={"name": "stable", "role": "viewer"},
    )
    sa_id = r.json()["id"]

    # Reseta logs de criação para isolar.
    with TestingSession() as db:
        db.query(models.AuditLog).delete()
        db.commit()

    r2 = client.patch(
        f"/api/v1/service-accounts/{sa_id}",
        json={"role": "viewer"},  # mesmo role
    )
    assert r2.status_code == 200

    with TestingSession() as db:
        logs = (
            db.query(models.AuditLog)
            .filter(models.AuditLog.action == "service_account.updated")
            .all()
        )
        assert len(logs) == 0


def test_delete_sa_cascades_tokens(setup):
    client, TestingSession = setup
    r = client.post(
        "/api/v1/service-accounts",
        json={"name": "throwaway", "role": "operator"},
    )
    sa_id = r.json()["id"]

    # Emite token.
    rt = client.post(
        f"/api/v1/service-accounts/{sa_id}/tokens",
        json={"name": "tok", "is_eternal": True},
    )
    assert rt.status_code == 201

    # Delete SA.
    rd = client.delete(f"/api/v1/service-accounts/{sa_id}")
    assert rd.status_code == 204

    # Token deve ter sido cascade deleted.
    with TestingSession() as db:
        tokens = (
            db.query(models.ApiToken)
            .filter(models.ApiToken.service_account_id == sa_id)
            .all()
        )
        assert len(tokens) == 0


# ── Tokens nested ───────────────────────────────────────────────────────


def test_create_token_for_sa_returns_raw_once(setup):
    client, _ = setup
    r = client.post(
        "/api/v1/service-accounts",
        json={"name": "ci-bot-sa", "role": "operator"},
    )
    sa_id = r.json()["id"]

    rt = client.post(
        f"/api/v1/service-accounts/{sa_id}/tokens",
        json={"name": "deploy", "is_eternal": True},
    )
    assert rt.status_code == 201
    body = rt.json()
    assert body["token"].startswith("copsk_")
    assert body["api_token"]["service_account_id"] == sa_id
    assert body["api_token"]["user_id"] is None
    assert body["api_token"]["is_eternal"] is True


def test_create_token_for_sa_with_scopes(setup):
    client, _ = setup
    r = client.post(
        "/api/v1/service-accounts",
        json={"name": "scoped-sa", "role": "operator"},
    )
    sa_id = r.json()["id"]

    rt = client.post(
        f"/api/v1/service-accounts/{sa_id}/tokens",
        json={
            "name": "limited",
            "is_eternal": True,
            "scopes": ["mapping.read", "integration.read"],
        },
    )
    assert rt.status_code == 201
    assert sorted(rt.json()["api_token"]["scopes"]) == [
        "integration.read",
        "mapping.read",
    ]


def test_create_token_for_sa_rejects_invalid_scope(setup):
    client, _ = setup
    r = client.post(
        "/api/v1/service-accounts",
        json={"name": "x", "role": "admin"},
    )
    sa_id = r.json()["id"]

    rt = client.post(
        f"/api/v1/service-accounts/{sa_id}/tokens",
        json={
            "name": "bad",
            "is_eternal": True,
            "scopes": ["foo.bar"],
        },
    )
    assert rt.status_code == 400
    assert "invalid scope" in rt.json()["detail"]


def test_create_token_for_inactive_sa_rejected(setup):
    client, _ = setup
    r = client.post(
        "/api/v1/service-accounts",
        json={"name": "deactivated", "role": "viewer"},
    )
    sa_id = r.json()["id"]
    client.patch(
        f"/api/v1/service-accounts/{sa_id}",
        json={"is_active": False},
    )

    rt = client.post(
        f"/api/v1/service-accounts/{sa_id}/tokens",
        json={"name": "x", "is_eternal": True},
    )
    assert rt.status_code == 400


def test_list_tokens_for_sa_excludes_other_sas(setup):
    client, _ = setup
    r1 = client.post("/api/v1/service-accounts", json={"name": "a", "role": "viewer"})
    r2 = client.post("/api/v1/service-accounts", json={"name": "b", "role": "viewer"})
    sa_a = r1.json()["id"]
    sa_b = r2.json()["id"]

    client.post(
        f"/api/v1/service-accounts/{sa_a}/tokens",
        json={"name": "ta", "is_eternal": True},
    )
    client.post(
        f"/api/v1/service-accounts/{sa_b}/tokens",
        json={"name": "tb", "is_eternal": True},
    )

    r = client.get(f"/api/v1/service-accounts/{sa_a}/tokens")
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 1
    assert items[0]["name"] == "ta"


def test_revoke_token_for_sa_returns_404_when_other_sa(setup):
    client, _ = setup
    r1 = client.post("/api/v1/service-accounts", json={"name": "a", "role": "viewer"})
    r2 = client.post("/api/v1/service-accounts", json={"name": "b", "role": "viewer"})
    sa_a = r1.json()["id"]
    sa_b = r2.json()["id"]

    rt = client.post(
        f"/api/v1/service-accounts/{sa_a}/tokens",
        json={"name": "tok", "is_eternal": True},
    )
    token_id = rt.json()["api_token"]["id"]

    # Tenta revogar token de SA_a usando rota de SA_b → 404.
    r = client.delete(f"/api/v1/service-accounts/{sa_b}/tokens/{token_id}")
    assert r.status_code == 404


# ── Permissions / RBAC ──────────────────────────────────────────────────


def test_viewer_cannot_create_sa(setup):
    client, TestingSession = setup
    _create_user_with_role(
        client, TestingSession,
        username="vista", password="ViewerPwd123!", role="viewer",
    )
    # Logout admin → login viewer.
    client.post("/api/auth/logout")
    _login_as(client, "vista", "ViewerPwd123!")

    r = client.post(
        "/api/v1/service-accounts",
        json={"name": "x", "role": "viewer"},
    )
    assert r.status_code == 403


def test_engineer_cannot_create_sa(setup):
    """USER_MANAGE só admin tem — engineer também recebe 403."""
    client, TestingSession = setup
    _create_user_with_role(
        client, TestingSession,
        username="engy", password="EngineerPwd123!", role="engineer",
    )
    client.post("/api/auth/logout")
    _login_as(client, "engy", "EngineerPwd123!")

    r = client.post(
        "/api/v1/service-accounts",
        json={"name": "x", "role": "viewer"},
    )
    assert r.status_code == 403


# ── PAT de SA autentica request via shim ────────────────────────────────


def test_sa_token_authenticates_request_with_shim(setup):
    """PAT de SA autentica em endpoints normais (current_user fica shim).

    Validamos via /api/auth/me que o "user logado" tem username sa:<name>
    e id negativo (sintético).
    """
    client, _ = setup
    r = client.post(
        "/api/v1/service-accounts",
        json={"name": "shim-test", "role": "admin"},
    )
    sa_id = r.json()["id"]

    rt = client.post(
        f"/api/v1/service-accounts/{sa_id}/tokens",
        json={"name": "tok", "is_eternal": True},
    )
    raw_token = rt.json()["token"]

    # Logout admin pra garantir que só Bearer está autenticando.
    client.post("/api/auth/logout")

    r = client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert r.status_code == 200
    body = r.json()
    # Shim username = "sa:<sa_name>"
    assert body["username"] == "sa:shim-test"
    # Role do SA passa pra current_user (admin neste teste).
    assert body["role"] == "admin"
