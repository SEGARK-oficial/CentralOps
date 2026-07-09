"""Isolamento cross-tenant do e-mail de scheduled query.

Antes: o resultado de uma scheduled query do tenant A (nome da integração +
contagem) era enviado a TODOS os e-mails globais. Agora os destinatários são
escopados à org da integração — tenant B NÃO recebe o que é do tenant A.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import models, repository
from backend.app.db.database import Base, get_session
from backend.app.main import app


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = Session()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def _org(db, name: str) -> models.Organization:
    org = models.Organization(name=name, slug=f"{name}-{uuid4().hex[:6]}", is_active=True)
    db.add(org)
    db.flush()
    return org


# ── Repository: list_for_org é estrito (fecha o leak) ────────────────────


def test_list_for_org_strict_isolation(db_session) -> None:
    org_a = _org(db_session, "tenant-a")
    org_b = _org(db_session, "tenant-b")
    repo = repository.EmailRepository(db_session)
    repo.add(models.NotificationEmail(email="a@x.com", organization_id=org_a.id))
    repo.add(models.NotificationEmail(email="b@x.com", organization_id=org_b.id))
    repo.add(models.NotificationEmail(email="sys@x.com", organization_id=None))

    a_recipients = {e.email for e in repo.list_for_org(org_a.id)}
    b_recipients = {e.email for e in repo.list_for_org(org_b.id)}

    assert a_recipients == {"a@x.com"}
    assert b_recipients == {"b@x.com"}
    # E-mail de sistema (org NULL) NÃO entra em nenhum escopo de tenant.
    assert "sys@x.com" not in a_recipients
    assert "sys@x.com" not in b_recipients
    # O leak antigo: b@x.com jamais aparece no escopo do tenant A.
    assert "b@x.com" not in a_recipients


def test_list_for_org_none_returns_only_system(db_session) -> None:
    org_a = _org(db_session, "tenant-a")
    repo = repository.EmailRepository(db_session)
    repo.add(models.NotificationEmail(email="a@x.com", organization_id=org_a.id))
    repo.add(models.NotificationEmail(email="sys@x.com", organization_id=None))
    assert {e.email for e in repo.list_for_org(None)} == {"sys@x.com"}


# ── Router: create_email carimba a org do admin ──────────────────────────


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


def test_create_email_stamps_explicit_org(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    r = client.post("/api/auth/bootstrap", json={"username": "admin", "password": "AdminPassword123!"})
    assert r.status_code == 200, r.text

    with Session() as db:
        org = _org(db, "tenant-a")
        db.commit()
        org_id = org.id

    r = client.post("/api/emails/", json={"email": "soc@tenant-a.com", "organization_id": org_id})
    assert r.status_code == 200, r.text
    assert r.json()["organization_id"] == org_id

    # Persistiu escopado.
    with Session() as db:
        rows = repository.EmailRepository(db).list_for_org(org_id)
        assert [e.email for e in rows] == ["soc@tenant-a.com"]
