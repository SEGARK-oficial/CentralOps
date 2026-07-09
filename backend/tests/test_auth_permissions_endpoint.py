"""Testes dos endpoints /api/auth/me (campo permissions) e /api/auth/permissions."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db.database import Base, get_session
from backend.app.main import app
from backend.app.core.auth import ROLE_PERMISSIONS, UserRole, Permission, get_user_permissions


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


def _bootstrap_admin(client: TestClient) -> dict[str, Any]:
    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPassword123!"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _create_user(admin_client: TestClient, *, username: str, role: str) -> dict[str, Any]:
    r = admin_client.post(
        "/api/auth/users",
        json={"username": username, "password": "TestPassword123!", "role": role},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _login_as(client: TestClient, *, username: str, password: str = "TestPassword123!") -> None:
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text


# ── Testes de /api/auth/me ────────────────────────────────────────────


def test_me_retorna_permissions_para_admin(client_factory):
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.get("/api/auth/me")
    assert r.status_code == 200
    data = r.json()

    assert "permissions" in data
    perms = data["permissions"]
    assert isinstance(perms, list)
    assert len(perms) > 0

    # Admin tem todas as permissões
    expected = sorted(get_user_permissions("admin"))
    assert sorted(perms) == expected
    assert "user.manage" in perms
    assert "secret.read" in perms
    assert "org.manage" in perms


def test_me_retorna_permissions_para_viewer(client_factory):
    factory, _ = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)
    _create_user(admin_client, username="viewer1", role="viewer")

    client = factory()
    _login_as(client, username="viewer1")

    r = client.get("/api/auth/me")
    assert r.status_code == 200
    data = r.json()

    perms = data["permissions"]
    expected = sorted(get_user_permissions("viewer"))
    assert sorted(perms) == expected

    # viewer não tem permissões de escrita
    assert "mapping.write" not in perms
    assert "user.manage" not in perms
    # viewer tem permissões de leitura
    assert "mapping.read" in perms
    assert "audit.read" in perms


@pytest.mark.parametrize("role", ["viewer", "operator", "engineer", "admin"])
def test_me_permissions_match_role_matrix(client_factory, role: str):
    factory, _ = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)

    if role != "admin":
        _create_user(admin_client, username=f"user_{role}", role=role)
        client = factory()
        _login_as(client, username=f"user_{role}")
    else:
        client = admin_client

    r = client.get("/api/auth/me")
    assert r.status_code == 200
    data = r.json()

    expected = sorted(get_user_permissions(role))
    actual = sorted(data["permissions"])
    assert actual == expected, f"role={role}: esperava {expected}, got {actual}"


def test_me_sem_autenticacao_retorna_401(client_factory):
    factory, _ = client_factory
    client = factory()

    r = client.get("/api/auth/me")
    assert r.status_code == 401


# ── Testes de /api/auth/permissions ──────────────────────────────────


def test_permissions_matriz_retornada_para_autenticado(client_factory):
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.get("/api/auth/permissions")
    assert r.status_code == 200
    data = r.json()

    # Deve conter os 4 papéis
    for role in ["viewer", "operator", "engineer", "admin"]:
        assert role in data, f"Papel '{role}' ausente na resposta"

    # Cada papel é uma lista de strings
    for role, perms in data.items():
        assert isinstance(perms, list)
        assert all(isinstance(p, str) for p in perms)


def test_permissions_matriz_admin_tem_todas(client_factory):
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.get("/api/auth/permissions")
    assert r.status_code == 200
    data = r.json()

    all_permission_values = {p.value for p in Permission}
    admin_perms = set(data["admin"])
    assert admin_perms == all_permission_values


def test_permissions_matriz_heranca_crescente(client_factory):
    """Verifica que as permissões crescem: viewer ⊂ operator ⊂ engineer ⊂ admin."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.get("/api/auth/permissions")
    assert r.status_code == 200
    data = r.json()

    viewer = set(data["viewer"])
    operator = set(data["operator"])
    engineer = set(data["engineer"])
    admin = set(data["admin"])

    assert viewer.issubset(operator), "viewer deve ser subconjunto de operator"
    assert viewer.issubset(engineer), "viewer deve ser subconjunto de engineer"
    assert operator.issubset(admin), "operator deve ser subconjunto de admin"
    assert engineer.issubset(admin), "engineer deve ser subconjunto de admin"


def test_permissions_endpoint_sem_autenticacao_retorna_401(client_factory):
    factory, _ = client_factory
    client = factory()

    r = client.get("/api/auth/permissions")
    assert r.status_code == 401


# ── Testes de auditoria em role_change ────────────────────────────────


def test_role_change_grava_audit_log(client_factory):
    factory, Session = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)
    _create_user(admin_client, username="target_user", role="viewer")

    # Obtém o uuid do usuário criado
    r = admin_client.get("/api/auth/users")
    assert r.status_code == 200
    users = r.json()
    target = next(u for u in users if u["username"] == "target_user")
    target_id = target["id"]

    # Promove para operator
    r = admin_client.put(
        f"/api/auth/users/{target_id}",
        json={"role": "operator"},
    )
    assert r.status_code == 200
    assert r.json()["role"] == "operator"

    # Verifica que há entrada de audit com action=role_change
    from backend.app.db.models import AuditLog
    import json as _json

    with Session() as db:
        logs = db.query(AuditLog).filter(AuditLog.action == "role_change").all()

    assert len(logs) >= 1
    log = logs[0]
    detail = _json.loads(log.detail)
    assert detail["target_username"] == "target_user"
    assert detail["previous_role"] == "viewer"
    assert detail["new_role"] == "operator"


def test_role_change_sem_mudanca_nao_grava_audit(client_factory):
    """Atualizar outros campos sem mudar role não deve gravar role_change."""
    factory, Session = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)
    _create_user(admin_client, username="stable_user", role="engineer")

    r = admin_client.get("/api/auth/users")
    assert r.status_code == 200
    target = next(u for u in r.json() if u["username"] == "stable_user")
    target_id = target["id"]

    # Atualiza display_name sem mudar role
    r = admin_client.put(
        f"/api/auth/users/{target_id}",
        json={"display_name": "Stable User Display"},
    )
    assert r.status_code == 200

    from backend.app.db.models import AuditLog

    with Session() as db:
        count = db.query(AuditLog).filter(AuditLog.action == "role_change").count()

    assert count == 0


# ── Testes de migração user → viewer ─────────────────────────────────


def test_migration_user_legado_vira_viewer(client_factory):
    """Usuário com role='user' deve virar 'viewer' após execução da migration SQL."""
    factory, Session = client_factory

    from backend.app.db.models import AppUser
    from backend.app.core.security import hash_password
    from sqlalchemy import text
    from uuid import uuid4 as _uuid4

    with Session() as db:
        legacy = AppUser(
            uuid=str(_uuid4()),
            username="legacy_user",
            password_hash=hash_password("TestPassword123!"),
            role="user",  # papel legado
        )
        db.add(legacy)
        db.commit()
        db.refresh(legacy)
        legacy_id = legacy.id

    # Simula a migration SQL diretamente na sessão de teste
    with Session() as db:
        db.execute(
            text(
                "UPDATE app_users SET role = 'viewer' "
                "WHERE role NOT IN ('viewer', 'operator', 'engineer', 'admin')"
            )
        )
        db.commit()

    with Session() as db:
        refreshed = db.get(AppUser, legacy_id)
        assert refreshed is not None
        assert refreshed.role == "viewer", f"Esperava 'viewer', got '{refreshed.role}'"


def test_migration_roles_validos_nao_sao_alterados(client_factory):
    """Papéis já no conjunto válido não são tocados pela migração."""
    factory, Session = client_factory

    from backend.app.db.models import AppUser
    from backend.app.core.security import hash_password
    from sqlalchemy import text
    from uuid import uuid4 as _uuid4

    created_ids: dict[str, int] = {}
    with Session() as db:
        for role in ["viewer", "operator", "engineer", "admin"]:
            u = AppUser(
                uuid=str(_uuid4()),
                username=f"stable_{role}",
                password_hash=hash_password("TestPassword123!"),
                role=role,
            )
            db.add(u)
        db.commit()

        for role in ["viewer", "operator", "engineer", "admin"]:
            u = db.query(AppUser).filter(AppUser.username == f"stable_{role}").first()
            created_ids[role] = u.id

    # Simula a migration SQL
    with Session() as db:
        db.execute(
            text(
                "UPDATE app_users SET role = 'viewer' "
                "WHERE role NOT IN ('viewer', 'operator', 'engineer', 'admin')"
            )
        )
        db.commit()

    with Session() as db:
        for role, uid in created_ids.items():
            u = db.get(AppUser, uid)
            assert u.role == role, f"role '{role}' foi alterado para '{u.role}'"
