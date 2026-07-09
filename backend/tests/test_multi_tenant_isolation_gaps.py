"""Regressão dos gaps de isolamento multi-tenant fechados na auditoria.

Cada teste reproduz um vazamento cross-tenant que um usuário ESCOPADO
(role != admin, is_global=False, organization_id=X) conseguia explorar antes
do fix, e prova que agora está fechado:

- queries (PredefinedQuery): list/get/update/delete escopados por org.
- scheduled_queries (ScheduledQuery): list escopado + DELETE cross-tenant
  (era CRÍTICO) bloqueado.
- carimbo de org na criação (escopado herda a própria org).

O admin/is_global continua com escopo global por design — não é o alvo aqui.
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


def _bootstrap_admin(client: TestClient) -> None:
    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPassword123!"},
    )
    assert r.status_code == 200, r.text


def _create_scoped_user(
    admin_client: TestClient, *, username: str, role: str, organization_id: int
) -> None:
    r = admin_client.post(
        "/api/auth/users",
        json={
            "username": username,
            "password": "TestPassword123!",
            "role": role,
            "organization_id": organization_id,
        },
    )
    assert r.status_code == 200, f"Falha ao criar {username}: {r.text}"


def _login(client: TestClient, username: str) -> None:
    r = client.post(
        "/api/auth/login", json={"username": username, "password": "TestPassword123!"}
    )
    assert r.status_code == 200, r.text


def _seed_org(session, name: str) -> int:
    org = models.Organization(
        name=f"{name} {uuid4().hex[:6]}", slug=f"{name}-{uuid4().hex[:6]}", is_active=True
    )
    session.add(org)
    session.commit()
    session.refresh(org)
    return org.id


def _seed_query(session, *, org_id: int | None, title: str | None = None) -> int:
    q = models.PredefinedQuery(
        title=title or f"Q {uuid4().hex[:8]}",
        statement="SELECT * FROM xdr_data LIMIT 1",
        table="xdr_data",
        organization_id=org_id,
    )
    session.add(q)
    session.commit()
    session.refresh(q)
    return q.id


def _seed_schedule(session, *, org_id: int | None, query_id: int) -> int:
    sched = models.ScheduledQuery(
        query_id=query_id,
        organization_id=org_id,
        client_ids="1",
        interval_minutes=30,
        interval_value=30,
        interval_unit="minutes",
        days_back=1,
        lookback_value=1,
        lookback_unit="days",
        next_run=datetime.utcnow() + timedelta(minutes=30),
    )
    session.add(sched)
    session.commit()
    session.refresh(sched)
    return sched.id


# ── queries: leitura escopada ─────────────────────────────────────────────────

def test_list_queries_scoped_to_own_org(client_factory) -> None:
    """Engineer da Org B só vê queries da própria org (não as da Org A nem NULL)."""
    factory, Session = client_factory
    admin = factory()
    _bootstrap_admin(admin)
    with Session() as db:
        org_a = _seed_org(db, "qa")
        org_b = _seed_org(db, "qb")
        _seed_query(db, org_id=org_a, title="ORG-A-QUERY")
        _seed_query(db, org_id=None, title="GLOBAL-QUERY")
        qb_id = _seed_query(db, org_id=org_b, title="ORG-B-QUERY")

    _create_scoped_user(admin, username="eng_b", role="engineer", organization_id=org_b)
    eng = factory()
    _login(eng, "eng_b")

    r = eng.get("/api/queries/")
    assert r.status_code == 200, r.text
    ids = {q["id"] for q in r.json()}
    assert ids == {qb_id}, f"engineer de B deveria ver só a query de B, viu {ids}"

    # admin (global) vê todas
    ra = admin.get("/api/queries/")
    assert len(ra.json()) == 3


def test_get_query_cross_tenant_returns_404(client_factory) -> None:
    factory, Session = client_factory
    admin = factory()
    _bootstrap_admin(admin)
    with Session() as db:
        org_a = _seed_org(db, "qa")
        org_b = _seed_org(db, "qb")
        qa_id = _seed_query(db, org_id=org_a)
    _create_scoped_user(admin, username="eng_b", role="engineer", organization_id=org_b)
    eng = factory()
    _login(eng, "eng_b")
    r = eng.get(f"/api/queries/{qa_id}")
    assert r.status_code == 404, f"get cross-tenant deve ser 404, got {r.status_code}"


def test_update_and_delete_query_cross_tenant_blocked(client_factory) -> None:
    factory, Session = client_factory
    admin = factory()
    _bootstrap_admin(admin)
    with Session() as db:
        org_a = _seed_org(db, "qa")
        org_b = _seed_org(db, "qb")
        qa_id = _seed_query(db, org_id=org_a)
    _create_scoped_user(admin, username="eng_b", role="engineer", organization_id=org_b)
    eng = factory()
    _login(eng, "eng_b")

    ru = eng.put(f"/api/queries/{qa_id}", json={"title": "HACKED"})
    assert ru.status_code == 404, f"update cross-tenant deve ser 404, got {ru.status_code}"
    rd = eng.delete(f"/api/queries/{qa_id}")
    assert rd.status_code == 404, f"delete cross-tenant deve ser 404, got {rd.status_code}"

    with Session() as db:
        q = db.query(models.PredefinedQuery).filter_by(id=qa_id).first()
        assert q is not None and q.title != "HACKED", "recurso de outra org foi alterado/removido"


def test_create_query_stamps_creator_org(client_factory) -> None:
    """Query criada por engineer escopado nasce carimbada com a org dele."""
    factory, Session = client_factory
    admin = factory()
    _bootstrap_admin(admin)
    with Session() as db:
        org_b = _seed_org(db, "qb")
    _create_scoped_user(admin, username="eng_b", role="engineer", organization_id=org_b)
    eng = factory()
    _login(eng, "eng_b")
    r = eng.post(
        "/api/queries/",
        json={"title": f"mine-{uuid4().hex[:6]}", "statement": "SELECT 1", "table": "xdr_data"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["organization_id"] == org_b


# ── scheduled_queries: list escopado + DELETE cross-tenant (era CRÍTICO) ───────

def test_list_schedules_scoped_to_own_org(client_factory) -> None:
    factory, Session = client_factory
    admin = factory()
    _bootstrap_admin(admin)
    with Session() as db:
        org_a = _seed_org(db, "sa")
        org_b = _seed_org(db, "sb")
        qa = _seed_query(db, org_id=org_a)
        qb = _seed_query(db, org_id=org_b)
        _seed_schedule(db, org_id=org_a, query_id=qa)
        sb_id = _seed_schedule(db, org_id=org_b, query_id=qb)
    _create_scoped_user(admin, username="eng_b", role="engineer", organization_id=org_b)
    eng = factory()
    _login(eng, "eng_b")
    r = eng.get("/api/schedules/")
    assert r.status_code == 200, r.text
    ids = {s["id"] for s in r.json()}
    assert ids == {sb_id}, f"engineer de B deveria ver só o schedule de B, viu {ids}"


def test_delete_schedule_cross_tenant_blocked_critical(client_factory) -> None:
    """CRÍTICO: engineer escopado da Org B NÃO pode deletar agendamento da Org A."""
    factory, Session = client_factory
    admin = factory()
    _bootstrap_admin(admin)
    with Session() as db:
        org_a = _seed_org(db, "sa")
        org_b = _seed_org(db, "sb")
        qa = _seed_query(db, org_id=org_a)
        sa_id = _seed_schedule(db, org_id=org_a, query_id=qa)
    _create_scoped_user(admin, username="eng_b", role="engineer", organization_id=org_b)
    eng = factory()
    _login(eng, "eng_b")

    r = eng.delete(f"/api/schedules/{sa_id}")
    assert r.status_code == 404, f"delete cross-tenant deve ser 404, got {r.status_code}: {r.text}"
    with Session() as db:
        assert (
            db.query(models.ScheduledQuery).filter_by(id=sa_id).first() is not None
        ), "agendamento de outra org foi deletado (vazamento crítico)"
