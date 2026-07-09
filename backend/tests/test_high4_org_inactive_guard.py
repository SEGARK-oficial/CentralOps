"""Testes para HIGH 4 — race em right-to-delete: org.is_active=False guard.

Cenários cobertos:
- Criar Integration em organização inactive → HTTP 409.
- request_data_deletion marca org.is_active=False na mesma transação (antes do dispatch).
- Org ativa permite criação de Integration normalmente.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import database, models, repository
from backend.app.db.database import Base, get_session
from backend.app.main import app


# ── Fixture de banco isolado ──────────────────────────────────────────


@pytest.fixture()
def db_session():
    """SQLite in-memory com SessionLocal redirecionado para os testes."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    original = database.SessionLocal
    database.SessionLocal = Session  # type: ignore[assignment]

    yield Session

    database.SessionLocal = original  # type: ignore[assignment]
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def client_factory():
    """TestClient com banco SQLite in-memory isolado."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(
        autocommit=False, autoflush=False, bind=engine
    )
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


# ── Helpers ────────────────────────────────────────────────────────────


def _bootstrap_admin(client: TestClient) -> dict[str, Any]:
    r = client.post(
        "/api/auth/bootstrap",
        json={
            "username": "admin",
            "password": "AdminPass123!",
            "display_name": "Admin",
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _login(client: TestClient, username: str, password: str) -> None:
    r = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert r.status_code == 200, f"login falhou: {r.text}"


# ── Testes unitários do repository ────────────────────────────────────


def test_integration_add_rejects_inactive_org(db_session) -> None:
    """IntegrationRepository.add deve levantar HTTP 409 se org.is_active=False."""
    with db_session() as db:
        org = models.Organization(
            name=f"Org Inactive {uuid4().hex[:6]}",
            slug=f"org-{uuid4().hex[:8]}",
            is_active=False,  # organização já inativa
        )
        db.add(org)
        db.commit()
        org_id = org.id

    with db_session() as db:
        repo = repository.IntegrationRepository(db)
        integration = models.Integration(
            organization_id=org_id,
            name="Nova Integration",
            platform="sophos",
        )
        with pytest.raises(HTTPException) as exc_info:
            repo.add(integration)

        assert exc_info.value.status_code == 409
        assert "inactive" in exc_info.value.detail.lower()


def test_integration_add_allows_active_org(db_session) -> None:
    """IntegrationRepository.add deve funcionar normalmente em org ativa."""
    with db_session() as db:
        org = models.Organization(
            name=f"Org Ativa {uuid4().hex[:6]}",
            slug=f"org-ativa-{uuid4().hex[:8]}",
            is_active=True,
        )
        db.add(org)
        db.commit()
        org_id = org.id

    with db_session() as db:
        repo = repository.IntegrationRepository(db)
        integration = models.Integration(
            organization_id=org_id,
            name="Integration Nova",
            platform="sophos",
        )
        result = repo.add(integration)
        assert result.id is not None


# ── Teste de endpoint request_data_deletion ───────────────────────────


def test_request_data_deletion_marks_org_inactive(client_factory) -> None:
    """request_data_deletion deve marcar org.is_active=False antes do dispatch Celery."""
    factory, Session = client_factory
    client = factory()

    _bootstrap_admin(client)
    _login(client, "admin", "AdminPass123!")

    # Cria organização via API.
    r = client.post(
        "/api/organizations/",
        json={"name": "Org Para Deletar", "slug": "org-para-deletar"},
    )
    assert r.status_code == 200, r.text
    org_id = r.json()["id"]
    org_slug = r.json()["slug"]

    # Mock Celery para não precisar de broker.
    with patch(
        "backend.app.collectors.retention_tasks.execute_data_deletion"
    ) as mock_task:
        mock_result = type("Result", (), {"id": "celery-task-id"})()
        mock_task.apply_async.return_value = mock_result

        r = client.request(
            "DELETE",
            f"/api/organizations/{org_id}/data",
            json={
                "confirmation_text": f"DELETAR {org_slug}",
                "reason": "Teste LGPD",
            },
        )

    assert r.status_code == 202, r.text

    # Verifica que a org foi marcada inactive no banco.
    with Session() as db:
        org = db.get(models.Organization, org_id)
        assert org is not None
        assert org.is_active is False, "Org deveria estar inactive após request_data_deletion"
