"""Testes paramétricos de RBAC — 4 papéis × 5+ endpoints sensíveis.

Valida que a matriz ROLE_PERMISSIONS é aplicada corretamente nos endpoints.
"""

from __future__ import annotations

import json
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
from backend.app.core.auth import ROLE_PERMISSIONS, UserRole, Permission


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


def _create_user_with_role(admin_client: TestClient, *, username: str, role: str) -> dict[str, Any]:
    r = admin_client.post(
        "/api/auth/users",
        json={"username": username, "password": "TestPassword123!", "role": role},
    )
    assert r.status_code == 200, f"Falha ao criar user {username} role={role}: {r.text}"
    return r.json()


def _login_as(client: TestClient, *, username: str, password: str = "TestPassword123!") -> None:
    r = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert r.status_code == 200, f"Falha ao logar como {username}: {r.text}"


def _seed_mapping_definition(session, *, vendor: str = "sophos", event_type: str = "sophos.alert") -> str:
    defn = models.MappingDefinition(
        vendor=vendor,
        event_type=event_type,
        ocsf_class_uid=2004,
    )
    session.add(defn)
    session.commit()
    session.refresh(defn)
    return defn.id


def _seed_org_and_integration(session, *, platform: str = "sophos") -> tuple[int, int]:
    """Cria uma organização e uma integração, retorna (org_id, integration_id)."""
    org = models.Organization(
        name=f"RBAC Test Org {uuid4().hex[:6]}",
        slug=f"rbac-test-{uuid4().hex[:6]}",
        is_active=True,
    )
    session.add(org)
    session.flush()
    integration = models.Integration(
        organization_id=org.id,
        name="RBAC Test Integration",
        platform=platform,
    )
    session.add(integration)
    session.flush()
    session.commit()
    session.refresh(integration)
    return org.id, integration.id


def _seed_quarantine_event(session, *, integration_id: int | None = None) -> str:
    ev = models.QuarantineEvent(
        vendor="sophos",
        event_type="sophos.alert",
        raw_payload=json.dumps({"id": "x"}),
        error_kind="map",
        integration_id=integration_id,
        expires_at=datetime.utcnow() + timedelta(days=7),
    )
    session.add(ev)
    session.commit()
    session.refresh(ev)
    return ev.id


def _seed_unknown_field(
    session, *, vendor: str = "sophos", organization_id: int | None = None
) -> str:
    uf = models.UnknownField(
        vendor=vendor,
        event_type="sophos.alert",
        field_path=f"test.field.{uuid4().hex[:8]}",
        organization_id=organization_id,
        occurrence_count=1,
        first_seen=datetime.utcnow(),
        last_seen=datetime.utcnow(),
        status="new",
    )
    session.add(uf)
    session.commit()
    session.refresh(uf)
    return uf.id


# ── Testes unitários da matriz ROLE_PERMISSIONS ───────────────────────


def test_matriz_viewer_nao_tem_write():
    assert Permission.MAPPING_WRITE not in ROLE_PERMISSIONS[UserRole.VIEWER]
    assert Permission.QUARANTINE_DISCARD not in ROLE_PERMISSIONS[UserRole.VIEWER]
    assert Permission.USER_MANAGE not in ROLE_PERMISSIONS[UserRole.VIEWER]


def test_matriz_operator_pode_ignorar_drift_e_descartar_quarentena():
    perms = ROLE_PERMISSIONS[UserRole.OPERATOR]
    assert Permission.QUARANTINE_DISCARD in perms
    assert Permission.DRIFT_IGNORE in perms
    assert Permission.MAPPING_WRITE not in perms


def test_matriz_engineer_pode_escrever_mapping_e_deletar_drift():
    perms = ROLE_PERMISSIONS[UserRole.ENGINEER]
    assert Permission.MAPPING_WRITE in perms
    assert Permission.MAPPING_ROLLBACK in perms
    assert Permission.DRIFT_DELETE in perms
    assert Permission.USER_MANAGE not in perms


def test_matriz_admin_tem_todas_permissoes():
    admin_perms = ROLE_PERMISSIONS[UserRole.ADMIN]
    for perm in Permission:
        assert perm in admin_perms, f"Admin deveria ter {perm}"


def test_matriz_query_rbac_por_capability():
    """Capability como autorização: QUERY_RUN/QUERY_SAVE.

    viewer não roda/salva query; operator (responder SOC) roda query;
    engineer roda E salva. ACTION_BLOCK removido: response actions descontinuadas."""
    viewer = ROLE_PERMISSIONS[UserRole.VIEWER]
    operator = ROLE_PERMISSIONS[UserRole.OPERATOR]
    engineer = ROLE_PERMISSIONS[UserRole.ENGINEER]

    assert Permission.QUERY_RUN not in viewer
    assert Permission.QUERY_SAVE not in viewer

    assert Permission.QUERY_RUN in operator
    assert Permission.QUERY_SAVE not in operator

    assert Permission.QUERY_RUN in engineer
    assert Permission.QUERY_SAVE in engineer


def test_get_user_permissions_role_legado():
    from backend.app.core.auth import get_user_permissions
    perms_viewer = get_user_permissions("viewer")
    perms_user_legado = get_user_permissions("user")
    assert perms_viewer == perms_user_legado


# ── Testes parametrizados: papel × endpoint → status esperado ─────────


# (role, endpoint_fn, expected_status)
# endpoint_fn recebe (client, def_id, quarantine_id, drift_id) e retorna Response
_RBAC_CASES = [
    # mapping.write: POST /api/mappings/{id}/versions
    ("viewer",   "create_mapping_version", 403),
    ("operator", "create_mapping_version", 403),
    ("engineer", "create_mapping_version", 201),
    ("admin",    "create_mapping_version", 201),

    # mapping.rollback: POST /api/mappings/{id}/rollback
    ("viewer",   "rollback_mapping", 403),
    ("operator", "rollback_mapping", 403),
    ("engineer", "rollback_mapping", 400),  # 400 = "Already on this version" (lógica ok, auth ok)
    ("admin",    "rollback_mapping", 400),

    # quarantine.discard
    ("viewer",   "quarantine_discard", 403),
    ("operator", "quarantine_discard", 204),
    ("engineer", "quarantine_discard", 204),
    ("admin",    "quarantine_discard", 204),

    # drift.ignore
    ("viewer",   "drift_ignore", 403),
    ("operator", "drift_ignore", 200),
    ("engineer", "drift_ignore", 200),
    ("admin",    "drift_ignore", 200),

    # user.manage (GET /api/auth/users)
    ("viewer",   "list_users", 403),
    ("operator", "list_users", 403),
    ("engineer", "list_users", 403),
    ("admin",    "list_users", 200),
]


@pytest.mark.parametrize("role,action,expected_status", _RBAC_CASES)
def test_rbac_role_permission_enforcement(
    client_factory,
    role: str,
    action: str,
    expected_status: int,
) -> None:
    factory, Session = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)

    with Session() as db:
        def_id = _seed_mapping_definition(db)
        # Cria uma versão para poder testar rollback "já na versão" → 400
        v = models.MappingVersion(
            definition_id=def_id,
            version_number=1,
            rules='{"preprocess":[],"rules":[]}',
            commit_message="seed",
            dsl_version=2,
        )
        db.add(v)
        db.flush()
        defn = db.get(models.MappingDefinition, def_id)
        defn.current_version_id = v.id
        db.commit()
        db.refresh(v)
        version_id = v.id

        _seed_quarantine_event(db)
        _seed_unknown_field(db)

    # Cria org + integration para non-admin (necessário para tenant isolation)
    org_id: int | None = None
    integration_id_for_test: int | None = None
    if role != "admin" and action in ("quarantine_discard", "drift_ignore"):
        with Session() as db_setup:
            org_id, integration_id_for_test = _seed_org_and_integration(
                db_setup, platform="sophos"
            )

    # Cria o usuário com o papel certo (exceto admin que já foi bootstrapped)
    username = f"testuser_{role}_{action.replace('_', '')}"
    if role != "admin":
        r_create = admin_client.post(
            "/api/auth/users",
            json={
                "username": username,
                "password": "TestPassword123!",
                "role": role,
                "organization_id": org_id,
            },
        )
        assert r_create.status_code == 200, f"Falha ao criar user {username} role={role}: {r_create.text}"

    # Login como o usuário alvo
    target_client = factory()
    if role == "admin":
        # admin foi bootstrapped com senha diferente — já está logado via bootstrap
        target_client = admin_client
    else:
        _login_as(target_client, username=username)

    # Executa a ação
    if action == "create_mapping_version":
        r = target_client.post(
            f"/api/mappings/{def_id}/versions",
            json={
                "rules": {"preprocess": [], "rules": [{"target": "x", "source": "y"}]},
                "commit_message": "test",
            },
        )
    elif action == "rollback_mapping":
        # Tenta fazer rollback para a versão atual (gerará 400 se autenticado)
        r = target_client.post(
            f"/api/mappings/{def_id}/rollback",
            json={"version_id": version_id, "commit_message": "rb"},
        )
    elif action == "quarantine_discard":
        # Cria evento fresco vinculado à integração da org do usuário
        with Session() as db2:
            q2 = _seed_quarantine_event(db2, integration_id=integration_id_for_test)
        r = target_client.post(f"/api/quarantine/{q2}/discard")
    elif action == "drift_ignore":
        # Campo fresco escopado à org do usuário (acesso é por
        # organization_id, não mais por vendor). Admin (global) ignora qualquer.
        with Session() as db2:
            d2 = _seed_unknown_field(db2, vendor="sophos", organization_id=org_id)
        r = target_client.post(f"/api/drift/{d2}/ignore")
    elif action == "list_users":
        r = target_client.get("/api/auth/users")
    else:
        pytest.fail(f"Ação desconhecida: {action}")

    assert r.status_code == expected_status, (
        f"role={role} action={action}: esperava {expected_status}, got {r.status_code} — {r.text[:300]}"
    )
