"""Segurança do endpoint POST /api/integrations/{id}/alerts/search.

Hunt/query free-text AO VIVO na fonte do cliente é semanticamente igual ao
/api/search/* — custa $ e toca o tenant. Deve exigir QUERY_RUN (não basta
autenticação): viewer → 403; operator+ passa o RBAC (erro posterior é de
negócio/provider, não de auth).
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


def _bootstrap_admin(client: TestClient) -> dict[str, Any]:
    r = client.post("/api/auth/bootstrap", json={"username": "admin", "password": "AdminPassword123!"})
    assert r.status_code == 200, r.text
    return r.json()


def _create_user_with_role(admin_client: TestClient, *, username: str, role: str,
                           organization_id: int | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"username": username, "password": "TestPassword123!", "role": role}
    if organization_id is not None:
        payload["organization_id"] = organization_id
    r = admin_client.post("/api/auth/users", json=payload)
    assert r.status_code == 200, f"Falha ao criar {username}: {r.text}"
    return r.json()


def _login_as(client: TestClient, *, username: str, password: str = "TestPassword123!") -> None:
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, f"Falha ao logar como {username}: {r.text}"


def _seed_org_and_integration(session) -> tuple[int, int]:
    org = models.Organization(name=f"Org {uuid4().hex[:6]}", slug=f"org-{uuid4().hex[:6]}", is_active=True)
    session.add(org)
    session.flush()
    integration = models.Integration(
        organization_id=org.id,
        name="Sophos Integration",
        platform="sophos",
        region="us01",
        tenant_id="tenant-x",
        access_token="tok-x",
    )
    session.add(integration)
    session.flush()
    session.commit()
    session.refresh(integration)
    return org.id, integration.id


def test_alerts_search_requires_authentication(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    with Session() as db:
        _, int_id = _seed_org_and_integration(db)
    r = client.post(f"/api/integrations/{int_id}/alerts/search?query=foo")
    assert r.status_code == 401


@pytest.mark.parametrize(
    "role,allowed",
    [
        ("viewer", False),    # viewer só lê — NÃO roda hunt ao vivo
        ("operator", True),
        ("engineer", True),
        ("admin", True),
    ],
)
def test_alerts_search_requires_query_run(client_factory, role: str, allowed: bool) -> None:
    factory, Session = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)

    with Session() as db:
        org_id, int_id = _seed_org_and_integration(db)

    if role == "admin":
        r = admin_client.post(f"/api/integrations/{int_id}/alerts/search?query=foo")
        assert r.status_code not in (401, 403), f"Admin não deve ser bloqueado: {r.status_code}"
        return

    username = f"as_{role}_{uuid4().hex[:6]}"
    _create_user_with_role(admin_client, username=username, role=role, organization_id=org_id)
    user_client = factory()
    _login_as(user_client, username=username)

    r = user_client.post(f"/api/integrations/{int_id}/alerts/search?query=foo")
    if allowed:
        assert r.status_code not in (401, 403), (
            f"role={role} tem QUERY_RUN — não deve ser bloqueado: {r.status_code}: {r.text}"
        )
    else:
        assert r.status_code == 403, (
            f"role={role} sem QUERY_RUN deve ser 403: {r.status_code}: {r.text}"
        )


# ── re-verificação: o gêmeo GET /alerts?query= também exige QUERY_RUN ──


def test_list_alerts_freetext_query_requires_query_run(client_factory) -> None:
    """viewer com ?query= (hunt ao vivo) → 403; sem query (listagem) → read OK."""
    factory, Session = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)
    with Session() as db:
        org_id, int_id = _seed_org_and_integration(db)

    username = f"v_{uuid4().hex[:6]}"
    _create_user_with_role(admin_client, username=username, role="viewer", organization_id=org_id)
    viewer = factory()
    _login_as(viewer, username=username)

    # free-text query → 403 (hunt exige QUERY_RUN, que viewer não tem)
    r_hunt = viewer.get(f"/api/integrations/{int_id}/alerts?query=*evil*")
    assert r_hunt.status_code == 403, f"viewer com query deve ser 403: {r_hunt.status_code}: {r_hunt.text}"

    # sem free-text → listagem normal (read): NÃO deve ser 403 de auth
    r_list = viewer.get(f"/api/integrations/{int_id}/alerts")
    assert r_list.status_code != 403, f"viewer SEM query não deve ser 403: {r_list.status_code}: {r_list.text}"


def test_aggregate_alerts_freetext_query_requires_query_run(client_factory) -> None:
    """O endpoint agregado /alerts/aggregate?query= também exige QUERY_RUN."""
    factory, Session = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)
    with Session() as db:
        org_id, int_id = _seed_org_and_integration(db)

    username = f"v_{uuid4().hex[:6]}"
    _create_user_with_role(admin_client, username=username, role="viewer", organization_id=org_id)
    viewer = factory()
    _login_as(viewer, username=username)

    r = viewer.get(f"/api/integrations/alerts/aggregate?integration_ids={int_id}&query=*evil*")
    assert r.status_code == 403, f"viewer com query agregada deve ser 403: {r.status_code}: {r.text}"

    # sem free-text → agregação normal (read): NÃO deve ser 403 de auth
    r_list = viewer.get(f"/api/integrations/alerts/aggregate?integration_ids={int_id}")
    assert r_list.status_code != 403, f"viewer SEM query agregada não deve ser 403: {r_list.status_code}: {r_list.text}"
