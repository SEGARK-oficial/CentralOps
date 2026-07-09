"""Testes de retenção configurável por organização (RNF7.2).

Commit 1 — F5-S1.
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


# ── Fixture base ──────────────────────────────────────────────────────


@pytest.fixture()
def env():
    """SQLite in-memory + TestClient isolado."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_session():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_session] = override_session
    client = TestClient(app)

    yield client, Session

    client.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


# ── Helpers ───────────────────────────────────────────────────────────


def _bootstrap(client: TestClient) -> None:
    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPass123!"},
    )
    assert r.status_code == 200, r.text


def _login(client: TestClient, username: str, password: str = "TestPass123!") -> None:
    r = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert r.status_code == 200, r.text


def _create_user(
    admin_client: TestClient,
    *,
    username: str,
    role: str,
    org_id: int | None = None,
) -> None:
    payload: dict[str, Any] = {
        "username": username,
        "password": "TestPass123!",
        "role": role,
    }
    if org_id is not None:
        payload["organization_id"] = org_id
    r = admin_client.post("/api/auth/users", json=payload)
    assert r.status_code == 200, r.text


def _seed_org(session_factory, *, name: str | None = None) -> int:
    slug = f"org-{uuid4().hex[:8]}"
    with session_factory() as db:
        org = models.Organization(
            name=name or f"Org {slug}",
            slug=slug,
            is_active=True,
        )
        db.add(org)
        db.commit()
        db.refresh(org)
        return org.id


# ── Testes ────────────────────────────────────────────────────────────


def test_retention_default_values_returned_when_no_config_set(env) -> None:
    """GET /organizations/{id}/retention retorna defaults se não houver config."""
    client, Session = env
    _bootstrap(client)
    org_id = _seed_org(Session)

    r = client.get(f"/api/organizations/{org_id}/retention")
    assert r.status_code == 200, r.text
    data = r.json()

    assert data["organization_id"] == org_id
    assert data["quarantine_retention_days"] == 7
    assert data["drift_retention_days"] == 90
    assert data["history_retention_days"] == 30
    assert data["search_result_retention_days"] == 7
    assert data["audit_log_retention_days"] == 365


def test_retention_get_requires_org_access(env) -> None:
    """Usuário de outra org não pode ler config de retenção de outra organização."""
    client, Session = env
    _bootstrap(client)

    org_a = _seed_org(Session, name="Org A Retention")
    org_b = _seed_org(Session, name="Org B Retention")

    _create_user(client, username="viewer_b", role="viewer", org_id=org_b)

    viewer_client = TestClient(app)
    _login(viewer_client, "viewer_b")

    # Tenta acessar org_a — deve ser negado.
    r = viewer_client.get(f"/api/organizations/{org_a}/retention")
    assert r.status_code == 403, r.text

    viewer_client.close()


def test_retention_update_requires_org_manage_permission(env) -> None:
    """engineer e operator não têm ORG_MANAGE → 403 no PUT."""
    client, Session = env
    _bootstrap(client)
    org_id = _seed_org(Session)

    for role in ("engineer", "operator"):
        username = f"{role}_retention_{uuid4().hex[:4]}"
        _create_user(client, username=username, role=role, org_id=org_id)

        target = TestClient(app)
        _login(target, username)

        r = target.put(
            f"/api/organizations/{org_id}/retention",
            json={"quarantine_retention_days": 14},
        )
        assert r.status_code == 403, (
            f"role={role} deveria receber 403 mas got {r.status_code}: {r.text}"
        )
        target.close()


def test_retention_update_validates_min_max_days(env) -> None:
    """Valores fora do range [1, 3650] devem retornar 422."""
    client, Session = env
    _bootstrap(client)
    org_id = _seed_org(Session)

    # Valor zero.
    r = client.put(
        f"/api/organizations/{org_id}/retention",
        json={"quarantine_retention_days": 0},
    )
    assert r.status_code == 422, r.text

    # Valor acima do máximo (3650 = 10 anos).
    r = client.put(
        f"/api/organizations/{org_id}/retention",
        json={"history_retention_days": 4000},
    )
    assert r.status_code == 422, r.text


def test_retention_update_writes_audit_log(env) -> None:
    """PUT /retention deve registrar AuditLog com action='update_retention_config'."""
    client, Session = env
    _bootstrap(client)
    org_id = _seed_org(Session)

    r = client.put(
        f"/api/organizations/{org_id}/retention",
        json={
            "quarantine_retention_days": 14,
            "drift_retention_days": 60,
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["quarantine_retention_days"] == 14
    assert data["drift_retention_days"] == 60

    # Verifica audit log.
    with Session() as db:
        log = (
            db.query(models.AuditLog)
            .filter(models.AuditLog.action == "update_retention_config")
            .first()
        )
    assert log is not None, "AuditLog de update_retention_config não encontrado"
    assert str(org_id) in (log.detail or "")


def test_retention_update_creates_and_updates(env) -> None:
    """Segundo PUT deve atualizar, não criar novo registro."""
    client, Session = env
    _bootstrap(client)
    org_id = _seed_org(Session)

    # Primeira chamada — cria.
    r1 = client.put(
        f"/api/organizations/{org_id}/retention",
        json={"quarantine_retention_days": 10},
    )
    assert r1.status_code == 200, r1.text

    # Segunda chamada — atualiza.
    r2 = client.put(
        f"/api/organizations/{org_id}/retention",
        json={"quarantine_retention_days": 21},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["quarantine_retention_days"] == 21

    # Deve haver exatamente 1 registro no banco.
    with Session() as db:
        count = (
            db.query(models.OrganizationRetentionConfig)
            .filter(models.OrganizationRetentionConfig.organization_id == org_id)
            .count()
        )
    assert count == 1, f"Esperado 1 config, encontrado {count}"
