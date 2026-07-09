"""Testes do audit log de mudança de papel (role_change) em
PUT /api/auth/users/{user_id}.

Cobre:
- Mudança de role grava entrada na tabela audit_logs com action="role_change".
- Mudança para o mesmo role NÃO grava audit.
- Viewer/operator não têm user.manage → 403 ao tentar alterar.
- Detalhe JSON contém campos esperados.
"""

from __future__ import annotations

import json
from typing import Any, Generator

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
def client_factory() -> Generator[Any, None, None]:
    """Engine SQLite em memória + override de get_session."""
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


def _bootstrap_admin(client: TestClient) -> dict[str, Any]:
    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPassword123!", "display_name": "Admin"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _create_user(
    client: TestClient,
    *,
    username: str,
    password: str = "Password123!X",
    role: str = "viewer",
) -> dict[str, Any]:
    r = client.post(
        "/api/auth/users",
        json={
            "username": username,
            "password": password,
            "display_name": username.title(),
            "role": role,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _login(client: TestClient, *, username: str, password: str = "Password123!X") -> None:
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text


# ── Testes ────────────────────────────────────────────────────────────


def test_role_change_writes_audit_log(client_factory: Any) -> None:
    """Admin muda role de viewer → engineer; deve gravar audit_logs com action='role_change'."""
    factory, Session = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)

    target = _create_user(admin_client, username="target_user", role="viewer")
    target_uuid = target["id"]  # UUID público

    r = admin_client.put(
        f"/api/auth/users/{target_uuid}",
        json={"role": "engineer"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "engineer"

    # Verifica entrada no audit_logs
    with Session() as db:
        logs = (
            db.query(models.AuditLog)
            .filter(models.AuditLog.action == "role_change")
            .all()
        )

    assert len(logs) == 1, f"Esperava 1 entrada de role_change, encontrou {len(logs)}"
    log = logs[0]
    assert log.username == "admin"
    assert log.method == "PUT"
    assert log.status_code == 200

    detail = json.loads(log.detail)
    assert detail["previous_role"] == "viewer"
    assert detail["new_role"] == "engineer"
    assert detail["target_username"] == "target_user"


def test_role_change_same_role_no_audit(client_factory: Any) -> None:
    """PUT com role idêntica ao atual NÃO deve gravar entrada de role_change."""
    factory, Session = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)

    target = _create_user(admin_client, username="same_role_user", role="operator")
    target_uuid = target["id"]

    # Envia o mesmo role que o usuário já tem.
    r = admin_client.put(
        f"/api/auth/users/{target_uuid}",
        json={"role": "operator"},
    )
    assert r.status_code == 200, r.text

    with Session() as db:
        logs = (
            db.query(models.AuditLog)
            .filter(models.AuditLog.action == "role_change")
            .all()
        )

    assert len(logs) == 0, (
        f"Não esperava nenhum role_change audit, mas encontrou {len(logs)}"
    )


def test_role_change_no_role_field_no_audit(client_factory: Any) -> None:
    """PUT sem campo 'role' (apenas display_name) NÃO deve gravar role_change."""
    factory, Session = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)

    target = _create_user(admin_client, username="no_role_field_user", role="viewer")
    target_uuid = target["id"]

    r = admin_client.put(
        f"/api/auth/users/{target_uuid}",
        json={"display_name": "Novo Nome"},
    )
    assert r.status_code == 200, r.text

    with Session() as db:
        logs = (
            db.query(models.AuditLog)
            .filter(models.AuditLog.action == "role_change")
            .all()
        )

    assert len(logs) == 0


@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_role_change_unauthorized_returns_403(client_factory: Any, role: str) -> None:
    """Viewer e operator não têm user.manage → PUT deve retornar 403."""
    factory, Session = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)

    # Cria o ator sem privilégio suficiente e o alvo.
    _create_user(admin_client, username=f"actor_{role}", role=role)
    target = _create_user(admin_client, username=f"target_{role}", role="viewer")
    target_uuid = target["id"]

    actor_client = factory()
    _login(actor_client, username=f"actor_{role}")

    r = actor_client.put(
        f"/api/auth/users/{target_uuid}",
        json={"role": "engineer"},
    )
    assert r.status_code == 403, f"role={role} deveria receber 403, got {r.status_code}"


def test_role_change_engineer_cannot_manage_users(client_factory: Any) -> None:
    """Engineer não tem user.manage → PUT deve retornar 403."""
    factory, Session = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)

    _create_user(admin_client, username="eng_actor", role="engineer")
    target = _create_user(admin_client, username="eng_target", role="viewer")
    target_uuid = target["id"]

    eng_client = factory()
    _login(eng_client, username="eng_actor")

    r = eng_client.put(
        f"/api/auth/users/{target_uuid}",
        json={"role": "operator"},
    )
    assert r.status_code == 403


def test_role_change_detail_json_structure(client_factory: Any) -> None:
    """Verifica todos os campos esperados no JSON de detalhe do audit."""
    factory, Session = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)

    target = _create_user(admin_client, username="detail_check_user", role="operator")
    target_uuid = target["id"]

    r = admin_client.put(
        f"/api/auth/users/{target_uuid}",
        json={"role": "admin"},
    )
    assert r.status_code == 200, r.text

    with Session() as db:
        log = (
            db.query(models.AuditLog)
            .filter(models.AuditLog.action == "role_change")
            .first()
        )

    assert log is not None
    detail = json.loads(log.detail)

    # Campos obrigatórios conforme contrato
    required_fields = {"target_user_id", "target_username", "previous_role", "new_role"}
    assert required_fields.issubset(detail.keys()), (
        f"Campos faltando: {required_fields - detail.keys()}"
    )
    assert detail["previous_role"] == "operator"
    assert detail["new_role"] == "admin"
    assert detail["target_username"] == "detail_check_user"
