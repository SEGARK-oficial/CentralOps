"""Tests pros scopes Fase 2 do PAT.

Cobre:
- Criação de PAT pessoal com scopes (válidos / inválidos / vazio).
- ``effective_scopes`` faz INTERSEÇÃO(role, token_scopes) corretamente.
- Token com scope que **não está** na role do owner → 403 no request.
- Token sem scopes (legacy Fase 1, NULL/[]) → full inherit (mantém comportamento).
- ``parse_scopes`` / ``serialize_scopes`` round-trip estável.
"""

from __future__ import annotations

from typing import Any, Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core.auth import (
    Permission,
    UserRole,
    effective_scopes,
)
from backend.app.db import models
from backend.app.db.database import Base, get_session
from backend.app.main import app
from backend.app.services.api_tokens import (
    parse_scopes,
    serialize_scopes,
    validate_scopes,
)


# ── Helpers de unit (sem TestClient) ─────────────────────────────────────


def test_parse_scopes_handles_none_and_empty():
    assert parse_scopes(None) == []
    assert parse_scopes("") == []
    assert parse_scopes("[]") == []


def test_parse_scopes_handles_invalid_json_returns_empty():
    assert parse_scopes("not-json") == []
    assert parse_scopes('{"not": "an array"}') == []


def test_serialize_scopes_round_trip():
    scopes = ["mapping.read", "integration.read"]
    encoded = serialize_scopes(scopes)
    decoded = parse_scopes(encoded)
    assert sorted(decoded) == sorted(scopes)


def test_serialize_scopes_empty_returns_none():
    """Lista vazia codifica como NULL (semantica full inherit)."""
    assert serialize_scopes([]) is None
    assert serialize_scopes(None) is None


def test_serialize_scopes_dedupes_and_sorts():
    encoded = serialize_scopes(["mapping.read", "mapping.read", "audit.read"])
    decoded = parse_scopes(encoded)
    assert decoded == ["audit.read", "mapping.read"]


def test_validate_scopes_rejects_invalid():
    with pytest.raises(ValueError, match="invalid scope"):
        validate_scopes(["foo.bar", "mapping.read"])


def test_validate_scopes_accepts_valid_subset():
    result = validate_scopes(["mapping.read", "integration.read"])
    assert result == ["integration.read", "mapping.read"]


# ── effective_scopes() — INTERSEÇÃO(role, token_scopes) ─────────────────


def test_effective_scopes_no_token_returns_role_perms():
    """token_scopes=None → todas perms da role."""
    perms = effective_scopes(UserRole.VIEWER, None)
    assert Permission.MAPPING_READ in perms
    assert Permission.MAPPING_WRITE not in perms  # viewer não tem


def test_effective_scopes_empty_list_treated_as_full_inherit():
    """token_scopes=[] também deve ser full inherit (não deny-all)."""
    perms_none = effective_scopes(UserRole.OPERATOR, None)
    perms_empty = effective_scopes(UserRole.OPERATOR, [])
    assert perms_none == perms_empty


def test_effective_scopes_intersection_with_role():
    """Token com scope fora da role → não passa."""
    # Engineer tem mapping.write; mas se o token só pediu mapping.read,
    # mapping.write fica fora.
    perms = effective_scopes(UserRole.ENGINEER, [Permission.MAPPING_READ])
    assert Permission.MAPPING_READ in perms
    assert Permission.MAPPING_WRITE not in perms


def test_effective_scopes_token_cannot_escalate_beyond_role():
    """Mesmo se o token pedir USER_MANAGE, viewer não tem → token não tem."""
    perms = effective_scopes(UserRole.VIEWER, [Permission.USER_MANAGE])
    assert Permission.USER_MANAGE not in perms
    # E também não deve "ganhar" outras perms — interseção pode dar empty.
    assert perms == frozenset()


def test_effective_scopes_legacy_role_user_treated_as_viewer():
    """Role 'user' (legado) deve ser equivalente a 'viewer'."""
    perms_user = effective_scopes("user", None)
    perms_viewer = effective_scopes(UserRole.VIEWER, None)
    assert perms_user == perms_viewer


# ── E2E via TestClient: tokens com scope restrito ───────────────────────


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

    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPassword123!"},
    )
    assert r.status_code == 200, r.text

    yield client, TestingSession

    client.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


def test_create_pat_with_scopes_persists(setup):
    client, _ = setup
    r = client.post(
        "/api/v1/tokens",
        json={
            "name": "scoped",
            "is_eternal": True,
            "scopes": ["mapping.read", "integration.read"],
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert sorted(body["api_token"]["scopes"]) == ["integration.read", "mapping.read"]


def test_create_pat_with_invalid_scope_returns_400(setup):
    client, _ = setup
    r = client.post(
        "/api/v1/tokens",
        json={"name": "bad", "is_eternal": True, "scopes": ["foo.bar"]},
    )
    assert r.status_code == 400
    assert "invalid scope" in r.json()["detail"]


def test_create_pat_rejects_service_account_id_on_personal_endpoint(setup):
    """POST /api/v1/tokens não aceita service_account_id (mesmo que SA existir)."""
    client, _ = setup
    r = client.post(
        "/api/v1/service-accounts",
        json={"name": "x", "role": "viewer"},
    )
    sa_id = r.json()["id"]

    r2 = client.post(
        "/api/v1/tokens",
        json={
            "name": "wrong-place",
            "is_eternal": True,
            "service_account_id": sa_id,
        },
    )
    assert r2.status_code == 400
    assert "Use POST /api/v1/service-accounts" in r2.json()["detail"]


def test_pat_with_restrictive_scope_blocks_other_endpoints(setup):
    """PAT criado só com mapping.read → 403 ao tentar org.manage.

    O admin tem todas perms; mesmo assim, o token restringe.
    """
    client, _ = setup
    r = client.post(
        "/api/v1/tokens",
        json={
            "name": "readonly",
            "is_eternal": True,
            "scopes": ["mapping.read"],
        },
    )
    assert r.status_code == 201
    raw_token = r.json()["token"]

    # Logout pra forçar Bearer-only.
    client.post("/api/auth/logout")

    # /api/auth/users exige USER_MANAGE → token não tem → 403.
    r2 = client.get(
        "/api/auth/users",
        headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert r2.status_code == 403


def test_pat_without_scopes_inherits_all_role_perms(setup):
    """Backwards compat: token Fase 1 (sem scopes) ainda funciona em tudo."""
    client, _ = setup
    r = client.post(
        "/api/v1/tokens",
        json={"name": "legacy", "is_eternal": True},  # sem scopes
    )
    raw_token = r.json()["token"]
    client.post("/api/auth/logout")

    # Admin deveria conseguir listar users.
    r2 = client.get(
        "/api/auth/users",
        headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert r2.status_code == 200


def test_token_scopes_are_intersection_with_role_not_union(setup):
    """Mesmo passando scopes que excedem role do owner, token fica limitado."""
    client, TestingSession = setup
    # Cria viewer.
    r = client.post(
        "/api/auth/users",
        json={"username": "lim", "password": "ViewerPwd123!"},
    )
    # Set role = viewer.
    with TestingSession() as db:
        u = db.query(models.AppUser).filter(models.AppUser.username == "lim").first()
        u.role = "viewer"
        db.commit()

    client.post("/api/auth/logout")
    client.post("/api/auth/login", json={"username": "lim", "password": "ViewerPwd123!"})

    # Tenta criar token pedindo USER_MANAGE — backend valida scopes
    # contra Permission enum (ok), mas em runtime `effective_scopes`
    # vai filtrar fora porque viewer não tem.
    r2 = client.post(
        "/api/v1/tokens",
        json={
            "name": "tries-to-escalate",
            "is_eternal": True,
            "scopes": ["user.manage"],
        },
    )
    # Aceita criar (scope é válido contra Permission enum), mas runtime bloqueia.
    assert r2.status_code == 201
    raw_token = r2.json()["token"]
    client.post("/api/auth/logout")

    # Tentativa de USAR USER_MANAGE → 403.
    r3 = client.get(
        "/api/auth/users",
        headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert r3.status_code == 403
