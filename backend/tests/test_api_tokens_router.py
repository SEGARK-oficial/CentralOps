"""Testes de integração do router /api/v1/tokens.

Cobre:
- POST cria token, retorna raw uma única vez, audita token.created.
- GET lista tokens do user (filtro include_revoked).
- DELETE revoga (soft) e audita token.revoked.
- 404 quando user tenta revogar token de outro user.
- Bearer + cookie: Bearer ganha quando ambos presentes.
- Bearer inválido = 401 imediato (não cai pra cookie).
- Cookie continua funcionando quando não há header Authorization.
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

    # Reset ratelimiter para evitar contaminacao entre testes
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


def test_create_token_returns_raw_only_once_and_audits(setup):
    client, TestingSession = setup
    r = client.post(
        "/api/v1/tokens",
        json={"name": "ci-bot", "expires_at": None},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    raw = body["token"]
    assert raw.startswith("copsk_")
    assert body["api_token"]["name"] == "ci-bot"
    assert body["api_token"]["token_prefix"] == raw[:12]
    assert body["api_token"]["revoked_at"] is None

    # Auditoria em audit_logs
    with TestingSession() as db:
        logs = db.query(models.AuditLog).filter(models.AuditLog.action == "token.created").all()
        assert len(logs) == 1
        assert "token_id" in (logs[0].detail or "")


def test_create_token_rejects_duplicate_name(setup):
    client, _ = setup
    r1 = client.post("/api/v1/tokens", json={"name": "dup", "expires_at": None})
    assert r1.status_code == 201
    r2 = client.post("/api/v1/tokens", json={"name": "dup", "expires_at": None})
    assert r2.status_code == 400
    assert "already in use" in r2.json()["detail"]


def test_list_tokens_omits_revoked_by_default(setup):
    client, _ = setup
    r1 = client.post("/api/v1/tokens", json={"name": "a", "expires_at": None})
    r2 = client.post("/api/v1/tokens", json={"name": "b", "expires_at": None})
    tid_b = r2.json()["api_token"]["id"]

    rev = client.delete(f"/api/v1/tokens/{tid_b}")
    assert rev.status_code == 204

    listing = client.get("/api/v1/tokens")
    assert listing.status_code == 200
    names = {t["name"] for t in listing.json()}
    assert names == {"a"}

    listing_all = client.get("/api/v1/tokens?include_revoked=true")
    names_all = {t["name"] for t in listing_all.json()}
    assert names_all == {"a", "b"}


def test_revoke_token_writes_audit(setup):
    client, TestingSession = setup
    r = client.post("/api/v1/tokens", json={"name": "x", "expires_at": None})
    tid = r.json()["api_token"]["id"]
    client.delete(f"/api/v1/tokens/{tid}")
    with TestingSession() as db:
        logs = db.query(models.AuditLog).filter(models.AuditLog.action == "token.revoked").all()
        assert len(logs) == 1


def test_revoke_token_returns_404_when_not_owner(setup):
    client, TestingSession = setup
    # Cria token do admin
    r = client.post("/api/v1/tokens", json={"name": "ot", "expires_at": None})
    tid = r.json()["api_token"]["id"]

    # Cria segundo user e loga como ele
    create_r = client.post(
        "/api/auth/users",
        json={
            "username": "victim",
            "password": "VictimPass123!",
            "role": "viewer",
        },
    )
    assert create_r.status_code == 200, create_r.text
    # Loga como victim
    other_client = TestClient(app)
    login = other_client.post(
        "/api/auth/login",
        json={"username": "victim", "password": "VictimPass123!"},
    )
    assert login.status_code == 200, login.text

    rev = other_client.delete(f"/api/v1/tokens/{tid}")
    assert rev.status_code == 404

    # Token continua ativo
    other_client.close()
    listing = client.get("/api/v1/tokens")
    assert any(t["id"] == tid and t["revoked_at"] is None for t in listing.json())


def test_bearer_token_authenticates_request(setup):
    client, _ = setup
    # Cria PAT via cookie session
    r = client.post("/api/v1/tokens", json={"name": "bearer", "expires_at": None})
    raw = r.json()["token"]

    # Limpa cookies para garantir que vamos via Bearer
    bearer_client = TestClient(app)
    me = bearer_client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert me.status_code == 200, me.text
    assert me.json()["username"] == "admin"
    bearer_client.close()


def test_bearer_invalid_returns_401(setup):
    client, _ = setup
    bearer_client = TestClient(app)
    r = bearer_client.get(
        "/api/auth/me",
        headers={"Authorization": "Bearer copsk_invalidtoken_aB3xK7zY9MmRTpFqZqVm"},
    )
    assert r.status_code == 401
    assert "API token" in r.json()["detail"]
    bearer_client.close()


def test_bearer_revoked_returns_401(setup):
    client, _ = setup
    r = client.post("/api/v1/tokens", json={"name": "rv", "expires_at": None})
    raw = r.json()["token"]
    tid = r.json()["api_token"]["id"]
    client.delete(f"/api/v1/tokens/{tid}")

    bearer_client = TestClient(app)
    me = bearer_client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert me.status_code == 401
    bearer_client.close()


def test_bearer_priority_over_cookie(setup):
    """Bearer + cookie: Bearer ganha. Cookie do admin + Bearer do víctim."""
    client, TestingSession = setup
    # Cria victim
    create_r = client.post(
        "/api/auth/users",
        json={"username": "v2", "password": "Password123!X", "role": "viewer"},
    )
    assert create_r.status_code == 200
    # Loga como victim e cria PAT
    victim = TestClient(app)
    victim.post("/api/auth/login", json={"username": "v2", "password": "Password123!X"})
    r = victim.post("/api/v1/tokens", json={"name": "vt", "expires_at": None})
    raw = r.json()["token"]
    victim.close()

    # Cliente com cookie do admin + Bearer do v2 → Bearer ganha
    me = client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert me.status_code == 200
    assert me.json()["username"] == "v2"


def test_pat_use_count_increments_on_use(setup):
    client, TestingSession = setup
    r = client.post("/api/v1/tokens", json={"name": "uc", "expires_at": None})
    raw = r.json()["token"]
    tid = r.json()["api_token"]["id"]

    bearer_client = TestClient(app)
    bearer_client.get("/api/auth/me", headers={"Authorization": f"Bearer {raw}"})
    bearer_client.get("/api/auth/me", headers={"Authorization": f"Bearer {raw}"})
    bearer_client.close()

    with TestingSession() as db:
        token = db.query(models.ApiToken).filter(models.ApiToken.id == tid).first()
        assert token.use_count >= 2
        assert token.last_used_at is not None
