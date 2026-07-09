"""Self-service account settings — GET/PATCH /auth/me, password change, sessions.

A logged-in user manages their OWN profile and password without touching any
control-plane field (role/org/is_global/is_active/auth_provider). Covers the
mass-assignment defense, the current-password re-auth on sensitive changes, the
SSO (federated) refusals, and the "keep current session, revoke the rest"
semantics on password change / sign-out-others.

Imports use ``backend.app.*`` (compiled .so dual-root gotcha).
"""
from __future__ import annotations

import json
from typing import Any, Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core import auth as app_auth
from backend.app.core.config import settings
from backend.app.db import models
from backend.app.db.database import Base, get_session
from backend.app.main import app


# ── Fixtures ──────────────────────────────────────────────────────────


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

    def factory() -> TestClient:
        c = TestClient(app)
        clients.append(c)
        return c

    yield factory, TestingSession

    for c in clients:
        c.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


# ── Helpers ───────────────────────────────────────────────────────────

ADMIN_PW = "AdminPassword123!"
USER_PW = "Password123!X"


def _bootstrap_admin(client: TestClient) -> dict[str, Any]:
    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": ADMIN_PW, "display_name": "Admin"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _create_user(
    client: TestClient,
    *,
    username: str,
    password: str = USER_PW,
    role: str = "viewer",
    email: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "username": username,
        "password": password,
        "display_name": username.title(),
        "role": role,
    }
    if email is not None:
        body["email"] = email
    r = client.post("/api/auth/users", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _authed_client(factory: Any, *, username: str, password: str = USER_PW) -> TestClient:
    c = factory()
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return c


def _mint_sso_client(
    factory: Any,
    Session: Any,
    *,
    username: str = "sso.user",
    email: str = "sso@corp.example",
) -> TestClient:
    """Cria uma conta federada (Entra, sem senha local) e devolve um client
    autenticado com um cookie de sessão real — não há endpoint de login SSO nos
    testes, então mintamos a sessão diretamente."""
    with Session() as db:
        user = models.AppUser(
            username=username,
            email=email,
            display_name="SSO User",
            auth_provider="entra",
            external_subject="entra-oid-123",
            password_hash=None,
            role="viewer",
            is_active=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        token, _session = app_auth.create_user_session(user, db, user_agent="pytest")
    c = factory()
    c.cookies.set(settings.SESSION_COOKIE_NAME, token)
    return c


# ── GET /auth/me/profile ──────────────────────────────────────────────


def test_profile_returns_rich_view_without_secrets(client_factory: Any) -> None:
    factory, _ = client_factory
    admin = factory()
    _bootstrap_admin(admin)
    _create_user(admin, username="alice", role="operator", email="alice@corp.example")

    user = _authed_client(factory, username="alice")
    r = user.get("/api/auth/me/profile")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["username"] == "alice"
    assert body["role"] == "operator"
    assert body["auth_provider"] == "local"
    assert body["email"] == "alice@corp.example"
    assert "created_at" in body and body["created_at"]
    assert "last_login_at" in body  # populated by login
    # No secret ever leaks through the profile contract.
    assert "password_hash" not in body
    assert "external_subject" not in body


def test_profile_requires_auth(client_factory: Any) -> None:
    factory, _ = client_factory
    anon = factory()
    assert anon.get("/api/auth/me/profile").status_code == 401


# ── PATCH /auth/me — safe fields ──────────────────────────────────────


def test_patch_display_name_and_locale(client_factory: Any) -> None:
    factory, Session = client_factory
    admin = factory()
    _bootstrap_admin(admin)
    _create_user(admin, username="bob")

    user = _authed_client(factory, username="bob")
    r = user.patch("/api/auth/me", json={"display_name": "Bobby", "locale": "en"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["display_name"] == "Bobby"
    assert body["locale"] == "en"

    with Session() as db:
        logs = db.query(models.AuditLog).filter(
            models.AuditLog.action == "profile_self_update"
        ).all()
    assert len(logs) == 1
    assert set(json.loads(logs[0].detail)["fields"]) == {"display_name", "locale"}


def test_patch_ignores_privilege_fields_massassignment(client_factory: Any) -> None:
    """Sending role/is_global/organization_id/is_active must NOT change them —
    the self schema has no such fields (Pydantic drops the extras)."""
    factory, _ = client_factory
    admin = factory()
    _bootstrap_admin(admin)
    _create_user(admin, username="carol", role="viewer")

    user = _authed_client(factory, username="carol")
    r = user.patch(
        "/api/auth/me",
        json={
            "display_name": "Carol",
            "role": "admin",
            "is_global": True,
            "is_active": False,
            "organization_id": 999,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["display_name"] == "Carol"  # only allowed field applied
    assert body["role"] == "viewer"
    assert body["is_global"] is False
    assert body["is_active"] is True
    assert body["organization_id"] is None


def test_patch_locale_rejects_invalid_value(client_factory: Any) -> None:
    factory, _ = client_factory
    admin = factory()
    _bootstrap_admin(admin)
    _create_user(admin, username="dora")
    user = _authed_client(factory, username="dora")
    r = user.patch("/api/auth/me", json={"locale": "de"})
    assert r.status_code == 422  # schema validation


# ── PATCH /auth/me — email (sensitive) ────────────────────────────────


def test_email_change_requires_current_password(client_factory: Any) -> None:
    factory, _ = client_factory
    admin = factory()
    _bootstrap_admin(admin)
    _create_user(admin, username="erin", email="erin@corp.example")
    user = _authed_client(factory, username="erin")

    # missing current_password
    r = user.patch("/api/auth/me", json={"email": "erin.new@corp.example"})
    assert r.status_code == 401, r.text

    # wrong current_password
    r = user.patch(
        "/api/auth/me",
        json={"email": "erin.new@corp.example", "current_password": "wrong-one"},
    )
    assert r.status_code == 401, r.text

    # correct current_password
    r = user.patch(
        "/api/auth/me",
        json={"email": "erin.new@corp.example", "current_password": USER_PW},
    )
    assert r.status_code == 200, r.text
    assert r.json()["email"] == "erin.new@corp.example"


def test_email_change_conflict_is_rejected(client_factory: Any) -> None:
    factory, _ = client_factory
    admin = factory()
    _bootstrap_admin(admin)
    _create_user(admin, username="frank", email="frank@corp.example")
    _create_user(admin, username="grace", email="grace@corp.example")
    user = _authed_client(factory, username="frank")
    r = user.patch(
        "/api/auth/me",
        json={"email": "grace@corp.example", "current_password": USER_PW},
    )
    assert r.status_code == 409, r.text


def test_sso_user_cannot_change_email(client_factory: Any) -> None:
    factory, Session = client_factory
    admin = factory()
    _bootstrap_admin(admin)
    sso = _mint_sso_client(factory, Session)
    r = sso.patch(
        "/api/auth/me",
        json={"email": "hijack@corp.example", "current_password": "irrelevant"},
    )
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "auth.email_managed_by_idp"


# ── POST /auth/me/password ────────────────────────────────────────────


def test_password_change_happy_path(client_factory: Any) -> None:
    factory, _ = client_factory
    admin = factory()
    _bootstrap_admin(admin)
    _create_user(admin, username="ivan")
    user = _authed_client(factory, username="ivan")

    new_pw = "BrandNewPass456!"
    r = user.post(
        "/api/auth/me/password",
        json={"current_password": USER_PW, "new_password": new_pw},
    )
    assert r.status_code == 200, r.text
    assert r.json()["detail"] == "password_changed"

    # old password no longer logs in; the new one does
    fresh = factory()
    assert fresh.post(
        "/api/auth/login", json={"username": "ivan", "password": USER_PW}
    ).status_code == 401
    assert fresh.post(
        "/api/auth/login", json={"username": "ivan", "password": new_pw}
    ).status_code == 200


def test_password_change_wrong_current(client_factory: Any) -> None:
    factory, _ = client_factory
    admin = factory()
    _bootstrap_admin(admin)
    _create_user(admin, username="judy")
    user = _authed_client(factory, username="judy")
    r = user.post(
        "/api/auth/me/password",
        json={"current_password": "nope-nope", "new_password": "BrandNewPass456!"},
    )
    assert r.status_code == 401, r.text


def test_password_change_weak_new(client_factory: Any) -> None:
    factory, _ = client_factory
    admin = factory()
    _bootstrap_admin(admin)
    _create_user(admin, username="karl")
    user = _authed_client(factory, username="karl")
    r = user.post(
        "/api/auth/me/password",
        json={"current_password": USER_PW, "new_password": "short"},
    )
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "auth.weak_password"


def test_password_change_rejects_reuse(client_factory: Any) -> None:
    factory, _ = client_factory
    admin = factory()
    _bootstrap_admin(admin)
    _create_user(admin, username="lena")
    user = _authed_client(factory, username="lena")
    r = user.post(
        "/api/auth/me/password",
        json={"current_password": USER_PW, "new_password": USER_PW},
    )
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "auth.password_reuse"


def test_sso_user_cannot_change_password(client_factory: Any) -> None:
    factory, Session = client_factory
    admin = factory()
    _bootstrap_admin(admin)
    sso = _mint_sso_client(factory, Session)
    r = sso.post(
        "/api/auth/me/password",
        json={"current_password": "x", "new_password": "BrandNewPass456!"},
    )
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "auth.password_managed_by_idp"


def test_password_change_keeps_current_revokes_others(client_factory: Any) -> None:
    factory, _ = client_factory
    admin = factory()
    _bootstrap_admin(admin)
    _create_user(admin, username="mike")

    session_a = _authed_client(factory, username="mike")
    session_b = _authed_client(factory, username="mike")
    # both sessions live
    assert session_a.get("/api/auth/me/profile").status_code == 200
    assert session_b.get("/api/auth/me/profile").status_code == 200

    r = session_a.post(
        "/api/auth/me/password",
        json={"current_password": USER_PW, "new_password": "BrandNewPass456!"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["revoked_other_sessions"] == 1

    # current session survives; the other is revoked
    assert session_a.get("/api/auth/me/profile").status_code == 200
    assert session_b.get("/api/auth/me/profile").status_code == 401


# ── POST /auth/me/sessions/revoke-others ──────────────────────────────


def test_revoke_other_sessions(client_factory: Any) -> None:
    factory, _ = client_factory
    admin = factory()
    _bootstrap_admin(admin)
    _create_user(admin, username="nina")

    session_a = _authed_client(factory, username="nina")
    session_b = _authed_client(factory, username="nina")
    session_c = _authed_client(factory, username="nina")

    r = session_a.post("/api/auth/me/sessions/revoke-others")
    assert r.status_code == 200, r.text
    assert r.json()["revoked"] == 2

    assert session_a.get("/api/auth/me/profile").status_code == 200
    assert session_b.get("/api/auth/me/profile").status_code == 401
    assert session_c.get("/api/auth/me/profile").status_code == 401


def test_revoke_other_sessions_requires_auth(client_factory: Any) -> None:
    factory, _ = client_factory
    anon = factory()
    assert anon.post("/api/auth/me/sessions/revoke-others").status_code == 401
