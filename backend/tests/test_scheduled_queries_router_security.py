"""Testes de segurança para o router /api/schedules — F4-S4.

Verifica:
- POST (create) requer MAPPING_WRITE (engineer+ — antes era admin-only).
- DELETE requer MAPPING_WRITE.
- GET list / GET /{id}/history requerem MAPPING_READ.
- Engineer agora pode criar schedule (antes não podia).
- client_ids são validados contra tenant scope.
"""

from __future__ import annotations

from datetime import datetime, timedelta
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


def _seed_org_and_integration(
    session,
    *,
    with_credentials: bool = True,
) -> tuple[int, int]:
    """Cria uma org e integração Sophos. Se with_credentials, adiciona token/region/tenant_id."""
    org = models.Organization(
        name=f"Sched Test Org {uuid4().hex[:6]}",
        slug=f"sched-{uuid4().hex[:6]}",
        is_active=True,
    )
    session.add(org)
    session.flush()
    integration = models.Integration(
        organization_id=org.id,
        name="Sched Sophos Integration",
        platform="sophos",
        region="us01" if with_credentials else None,
        tenant_id="tenant-abc" if with_credentials else None,
        access_token="tok-xyz" if with_credentials else None,
    )
    session.add(integration)
    session.flush()
    session.commit()
    session.refresh(integration)
    return org.id, integration.id


def _seed_predefined_query(session) -> int:
    q = models.PredefinedQuery(
        title="Sched Security Test Query",
        statement="SELECT * FROM xdr_data LIMIT 1",
        table="xdr_data",
    )
    session.add(q)
    session.commit()
    session.refresh(q)
    return q.id


def _schedule_payload(query_id: int, client_ids: list[int]) -> dict[str, Any]:
    return {
        "query_id": query_id,
        "client_ids": client_ids,
        "interval_value": 30,
        "interval_unit": "minutes",
        "lookback_value": 1,
        "lookback_unit": "days",
        "notify_on_results": False,
    }


# ── Testes: POST /schedules requer MAPPING_WRITE ──────────────────────

@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_create_schedule_requires_mapping_write(client_factory, role: str) -> None:
    """Viewer e operator não têm MAPPING_WRITE → 403."""
    factory, Session = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)

    with Session() as db:
        org_id, int_id = _seed_org_and_integration(db)
        query_id = _seed_predefined_query(db)

    username = f"test_{role}_{uuid4().hex[:6]}"
    _create_user_with_role(admin_client, username=username, role=role, organization_id=org_id)

    user_client = factory()
    _login_as(user_client, username=username)

    r = user_client.post("/api/schedules/", json=_schedule_payload(query_id, [int_id]))
    assert r.status_code == 403, f"role={role} deveria ser 403, got {r.status_code}: {r.text}"


def test_engineer_can_create_schedule(client_factory) -> None:
    """Engineer tem MAPPING_WRITE — antes era admin-only. F4-S4 expande acesso a engineer."""
    factory, Session = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)

    with Session() as db:
        org_id, int_id = _seed_org_and_integration(db)
        query_id = _seed_predefined_query(db)

    username = f"eng_{uuid4().hex[:6]}"
    _create_user_with_role(admin_client, username=username, role="engineer", organization_id=org_id)

    eng_client = factory()
    _login_as(eng_client, username=username)

    # Engineer pode criar — pode falhar com 400/404 por falta de exec real, mas não 403.
    r = eng_client.post("/api/schedules/", json=_schedule_payload(query_id, [int_id]))
    assert r.status_code not in (401, 403), (
        f"Engineer deve ter MAPPING_WRITE: got {r.status_code}: {r.text}"
    )


def test_create_schedule_validates_client_ids_tenant_scope(client_factory) -> None:
    """Engineer de Org A não pode criar schedule com integração de Org B → 403."""
    factory, Session = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)

    with Session() as db:
        _org_a_id, _int_a = _seed_org_and_integration(db)
        org_b_id, int_b = _seed_org_and_integration(db)
        query_id = _seed_predefined_query(db)

    # Engineer pertence à Org B, mas tenta criar schedule com integração da Org A
    username = f"eng_{uuid4().hex[:6]}"
    _create_user_with_role(admin_client, username=username, role="engineer", organization_id=org_b_id)

    eng_client = factory()
    _login_as(eng_client, username=username)

    # Tenta usar integração de outra org (int_a pertence a org_a)
    r = eng_client.post("/api/schedules/", json=_schedule_payload(query_id, [_int_a]))
    assert r.status_code == 403, f"Engineer de Org B não pode usar Org A: {r.status_code}: {r.text}"


def test_create_schedule_with_own_org_integration_passes(client_factory) -> None:
    """Engineer pode criar schedule usando integração da própria org."""
    factory, Session = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)

    with Session() as db:
        org_id, int_id = _seed_org_and_integration(db)
        query_id = _seed_predefined_query(db)

    username = f"eng_{uuid4().hex[:6]}"
    _create_user_with_role(admin_client, username=username, role="engineer", organization_id=org_id)

    eng_client = factory()
    _login_as(eng_client, username=username)

    r = eng_client.post("/api/schedules/", json=_schedule_payload(query_id, [int_id]))
    # Não deve ser 401/403 (autenticação/tenant ok); pode ser outros erros de negócio
    assert r.status_code not in (401, 403), (
        f"Engineer com integração própria não deve ser bloqueado: {r.status_code}: {r.text}"
    )


# ── Testes: GET /schedules requer MAPPING_READ ───────────────────────

def test_list_schedules_requires_authentication(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    r = client.get("/api/schedules/")
    assert r.status_code == 401


@pytest.mark.parametrize("role", ["viewer", "operator", "engineer", "admin"])
def test_list_schedules_accessible_with_mapping_read(client_factory, role: str) -> None:
    """Todos os papéis com MAPPING_READ podem listar schedules."""
    factory, Session = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)

    if role == "admin":
        r = admin_client.get("/api/schedules/")
        assert r.status_code == 200, f"Admin deve acessar /schedules: {r.text}"
        return

    with Session() as db:
        org_id, _ = _seed_org_and_integration(db)

    username = f"list_{role}_{uuid4().hex[:6]}"
    _create_user_with_role(admin_client, username=username, role=role, organization_id=org_id)
    user_client = factory()
    _login_as(user_client, username=username)

    r = user_client.get("/api/schedules/")
    assert r.status_code == 200, f"role={role} deve acessar GET /schedules: {r.text}"


# ── Testes: DELETE /schedules requer MAPPING_WRITE ───────────────────

@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_delete_schedule_requires_mapping_write(client_factory, role: str) -> None:
    """Viewer e operator não têm MAPPING_WRITE → 403."""
    factory, Session = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)

    # Admin cria um schedule e uma query
    with Session() as db:
        org_id, int_id = _seed_org_and_integration(db)
        q_id = _seed_predefined_query(db)
        sched = models.ScheduledQuery(
            query_id=q_id,
            client_ids=str(int_id),
            interval_minutes=30,
            interval_value=30,
            interval_unit="minutes",
            days_back=1,
            lookback_value=1,
            lookback_unit="days",
            next_run=datetime.utcnow() + timedelta(minutes=30),
        )
        db.add(sched)
        db.commit()
        db.refresh(sched)
        sched_id = sched.id

    username = f"del_{role}_{uuid4().hex[:6]}"
    _create_user_with_role(admin_client, username=username, role=role, organization_id=org_id)

    user_client = factory()
    _login_as(user_client, username=username)

    r = user_client.delete(f"/api/schedules/{sched_id}")
    assert r.status_code == 403, f"role={role} deveria ser 403 ao deletar schedule, got {r.status_code}"
