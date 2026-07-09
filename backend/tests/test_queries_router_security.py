"""Testes de segurança para o router /api/queries — F4-S4.

Verifica:
- POST / PUT / DELETE requerem MAPPING_WRITE (engineer+).
- GET requer MAPPING_READ (todos os papéis autenticados na matriz).
- client_ids no payload são validados contra tenant scope do usuário.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import models
from backend.app.db.database import Base, get_session
from backend.app.main import app


# ── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture()
def client_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
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


# ── Helpers ───────────────────────────────────────────────────────────

def _bootstrap_admin(client: TestClient) -> dict[str, Any]:
    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPassword123!"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _create_user_with_role(
    admin_client: TestClient,
    *,
    username: str,
    role: str,
    organization_id: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "username": username,
        "password": "TestPassword123!",
        "role": role,
    }
    if organization_id is not None:
        payload["organization_id"] = organization_id
    r = admin_client.post("/api/auth/users", json=payload)
    assert r.status_code == 200, f"Falha ao criar {username}: {r.text}"
    return r.json()


def _login_as(client: TestClient, *, username: str, password: str = "TestPassword123!") -> None:
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, f"Falha ao logar como {username}: {r.text}"


def _seed_org_and_integration(session) -> tuple[int, int]:
    org = models.Organization(
        name=f"Queries Test Org {uuid4().hex[:6]}",
        slug=f"queries-test-{uuid4().hex[:6]}",
        is_active=True,
    )
    session.add(org)
    session.flush()
    integration = models.Integration(
        organization_id=org.id,
        name="Queries Test Integration",
        platform="sophos",
    )
    session.add(integration)
    session.flush()
    session.commit()
    session.refresh(integration)
    return org.id, integration.id


_NEW_QUERY_PAYLOAD: dict[str, Any] = {
    "title": "Test Query",
    "description": "desc",
    "statement": "SELECT * FROM xdr_data LIMIT 10",
    "table": "xdr_data",
}


# ── Testes: POST /queries requer MAPPING_WRITE ────────────────────────

@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_create_query_requires_mapping_write(client_factory, role: str) -> None:
    """Viewer e operator não têm MAPPING_WRITE → 403."""
    factory, Session = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)

    with Session() as db:
        org_id, _ = _seed_org_and_integration(db)

    username = f"test_{role}_{uuid4().hex[:6]}"
    _create_user_with_role(admin_client, username=username, role=role, organization_id=org_id)

    user_client = factory()
    _login_as(user_client, username=username)

    r = user_client.post("/api/queries/", json=_NEW_QUERY_PAYLOAD)
    assert r.status_code == 403, f"role={role} deveria ser 403, got {r.status_code}: {r.text}"


def test_engineer_can_create_query(client_factory) -> None:
    """Engineer tem MAPPING_WRITE → pode criar query."""
    factory, Session = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)

    with Session() as db:
        org_id, _ = _seed_org_and_integration(db)

    username = f"eng_{uuid4().hex[:6]}"
    _create_user_with_role(admin_client, username=username, role="engineer", organization_id=org_id)

    eng_client = factory()
    _login_as(eng_client, username=username)

    r = eng_client.post("/api/queries/", json=_NEW_QUERY_PAYLOAD)
    assert r.status_code == 200, f"Engineer deve criar query: {r.text}"


def test_create_query_validates_client_ids_tenant_scope(client_factory) -> None:
    """Non-admin tentando usar client_id de outra org → 403."""
    factory, Session = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)

    with Session() as db:
        org_a_id, int_a_id = _seed_org_and_integration(db)
        org_b_id, _int_b_id = _seed_org_and_integration(db)

    # Engineer pertence à Org A mas tenta referenciar integração da Org B
    username = f"eng_{uuid4().hex[:6]}"
    _create_user_with_role(admin_client, username=username, role="engineer", organization_id=org_a_id)

    eng_client = factory()
    _login_as(eng_client, username=username)

    payload = {**_NEW_QUERY_PAYLOAD, "client_ids": [_int_b_id]}
    r = eng_client.post("/api/queries/", json=payload)
    assert r.status_code == 403, f"Engineer de Org A não pode usar integração de Org B: {r.status_code}"


def test_create_query_with_own_org_client_id_passes_tenant_scope(client_factory) -> None:
    """Engineer pode referenciar integração da própria org."""
    factory, Session = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)

    with Session() as db:
        org_id, int_id = _seed_org_and_integration(db)

    username = f"eng_{uuid4().hex[:6]}"
    _create_user_with_role(admin_client, username=username, role="engineer", organization_id=org_id)

    eng_client = factory()
    _login_as(eng_client, username=username)

    payload = {**_NEW_QUERY_PAYLOAD, "client_ids": [int_id]}
    r = eng_client.post("/api/queries/", json=payload)
    assert r.status_code == 200, f"Engineer pode usar integração da própria org: {r.text}"


# ── Testes: GET /queries requer MAPPING_READ ──────────────────────────

def test_get_queries_requires_authentication(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    r = client.get("/api/queries/")
    assert r.status_code == 401


@pytest.mark.parametrize("role", ["viewer", "operator", "engineer", "admin"])
def test_get_queries_accessible_with_mapping_read(client_factory, role: str) -> None:
    """Todos os papéis com MAPPING_READ podem listar queries."""
    factory, Session = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)

    if role == "admin":
        r = admin_client.get("/api/queries/")
        assert r.status_code == 200, f"Admin deve ter acesso: {r.text}"
        return

    with Session() as db:
        org_id, _ = _seed_org_and_integration(db)

    username = f"reader_{role}_{uuid4().hex[:6]}"
    _create_user_with_role(admin_client, username=username, role=role, organization_id=org_id)
    user_client = factory()
    _login_as(user_client, username=username)

    r = user_client.get("/api/queries/")
    assert r.status_code == 200, f"role={role} deve acessar GET /queries: {r.text}"


# ── Testes: DELETE /queries requer MAPPING_WRITE ──────────────────────

@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_delete_query_requires_mapping_write(client_factory, role: str) -> None:
    """Viewer e operator não têm MAPPING_WRITE → 403."""
    factory, Session = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)

    # Admin cria uma query primeiro
    r = admin_client.post("/api/queries/", json=_NEW_QUERY_PAYLOAD)
    assert r.status_code == 200, r.text
    query_id = r.json()["id"]

    with Session() as db:
        org_id, _ = _seed_org_and_integration(db)

    username = f"del_{role}_{uuid4().hex[:6]}"
    _create_user_with_role(admin_client, username=username, role=role, organization_id=org_id)

    user_client = factory()
    _login_as(user_client, username=username)

    r = user_client.delete(f"/api/queries/{query_id}")
    assert r.status_code == 403, f"role={role} deveria ser 403 ao deletar, got {r.status_code}"
