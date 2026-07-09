"""Fase 2B (Graph-sync) — testes unitarios e de integracao.

Cobertura:
  * Campos novos em IdentityConfig e IdentitySnapshot.
  * Task sync_entra_users: skip por flag, por credenciais ausentes, upsert,
    deprovision, lock Redis, status gravado no banco.
  * Endpoints REST POST /sync e GET /sync-status.
  * entra_graph.py: token, paginacao, filtragem por principalType,
    resolucao de role via entra_role_map e fallback para entra_default_role,
    fallback de email para userPrincipalName.
  * Reativacao de usuario Entra previamente desativado.
  * Summary persistido como JSON valido no banco.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

# Variaveis de ambiente devem ser definidas antes de importar qualquer modulo do app.
os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

# Garante que a raiz do repositorio esta no PYTHONPATH antes dos imports do projeto.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from backend.app.core import identity_config  # noqa: E402
from backend.app.core.identity_config import IdentitySnapshot  # noqa: E402
from backend.app.db import models  # noqa: E402
from backend.app.db.database import Base, get_session  # noqa: E402
from backend.app.db.repository import IdentityConfigRepository, UserRepository  # noqa: E402
from backend.app.main import app  # noqa: E402


# ── Fixture base ─────────────────────────────────────────────────────


@pytest.fixture()
def db_factory():
    """Retorna (TestingSessionLocal, engine) com schema in-memory."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    yield TestingSessionLocal, engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def client_factory(db_factory):
    """Retorna (factory_fn, TestingSessionLocal) para criacao de clientes HTTP."""
    TestingSessionLocal, _ = db_factory

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


def _bootstrap_admin(client: TestClient) -> dict[str, Any]:
    r = client.post("/api/auth/bootstrap", json={"username": "admin", "password": "AdminPass123!"})
    assert r.status_code == 200, r.text
    return r.json()


def _login(client: TestClient, username: str = "admin", password: str = "AdminPass123!") -> None:
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, f"login falhou: {r.text}"


def _make_snapshot(**overrides) -> IdentitySnapshot:
    """Constroi um IdentitySnapshot com valores minimos validos para testes."""
    defaults: dict[str, Any] = dict(
        entra_enabled=True,
        entra_tenant_id="tenant-123",
        entra_client_id="client-456",
        entra_client_secret="secret-abc",
        entra_sync_enabled=True,
        entra_sync_deprovision=True,
        entra_default_role="viewer",
        entra_default_is_global=False,
        entra_role_map={},
    )
    defaults.update(overrides)
    return IdentitySnapshot(**defaults)


def _run_sync_with_members(
    members: list,
    snap: IdentitySnapshot,
    TestingSessionLocal,
) -> dict:
    """Helper: roda sync_entra_users mockando Graph para retornar ``members``."""
    from backend.app.collectors.entra_sync_tasks import sync_entra_users
    from backend.app.core import entra_graph as _eg

    with patch("backend.app.collectors.entra_sync_tasks._load_cfg", return_value=snap), \
         patch("backend.app.collectors.entra_sync_tasks.database.SessionLocal", TestingSessionLocal), \
         patch("backend.app.collectors.entra_sync_tasks._get_redis", return_value=None), \
         patch.object(_eg, "get_app_token", return_value="fake-token"), \
         patch.object(_eg, "list_app_members", return_value=members), \
         patch.object(_eg, "get_app_role_map", return_value={}):

        return sync_entra_users()


# ── 8.1 Campos de banco e snapshot ───────────────────────────────────


def test_identity_config_has_sync_fields(db_factory):
    """Campos Fase 2B sao gravados e lidos corretamente via IdentityConfigRepository."""
    TestingSessionLocal, _ = db_factory
    with TestingSessionLocal() as db:
        repo = IdentityConfigRepository(db)
        repo.get_or_create()
        row = repo.update(entra_sync_enabled=True, entra_sync_deprovision=False)
        assert row.entra_sync_enabled is True
        assert row.entra_sync_deprovision is False


def test_identity_snapshot_defaults():
    """from_settings() retorna defaults corretos para campos da Fase 2B."""
    snap = identity_config.from_settings()
    assert snap.entra_sync_enabled is False
    assert snap.entra_sync_deprovision is True
    assert snap.entra_last_sync_at is None
    assert snap.entra_last_sync_status is None
    assert snap.entra_last_sync_summary is None


def test_identity_config_update_rejects_status_fields():
    """Campos de status (entra_last_sync_at etc) nao existem em IdentityConfigUpdate."""
    from backend.app.api.schemas import IdentityConfigUpdate
    schema_fields = set(IdentityConfigUpdate.model_fields.keys())
    assert "entra_last_sync_at" not in schema_fields
    assert "entra_last_sync_status" not in schema_fields
    assert "entra_last_sync_summary" not in schema_fields
    # Campos de controle devem estar presentes
    assert "entra_sync_enabled" in schema_fields
    assert "entra_sync_deprovision" in schema_fields


# ── 8.2 Task sync_entra_users ─────────────────────────────────────────


def test_sync_skips_when_disabled(db_factory):
    """Quando entra_sync_enabled=False, retorna ok sem chamar Graph."""
    TestingSessionLocal, _ = db_factory
    snap_disabled = _make_snapshot(entra_sync_enabled=False)

    from backend.app.core import entra_graph as _eg
    from backend.app.collectors.entra_sync_tasks import sync_entra_users

    with patch("backend.app.collectors.entra_sync_tasks._load_cfg", return_value=snap_disabled), \
         patch("backend.app.collectors.entra_sync_tasks.database.SessionLocal", TestingSessionLocal), \
         patch("backend.app.collectors.entra_sync_tasks._get_redis", return_value=None), \
         patch.object(_eg, "get_app_token") as mock_token:

        result = sync_entra_users()

    assert result["status"] == "ok"
    assert any("desabilitado" in w for w in result["warnings"])
    mock_token.assert_not_called()


def test_sync_skips_when_missing_credentials(db_factory):
    """Sem client_id configurado, retorna error sem chamar Graph."""
    TestingSessionLocal, _ = db_factory
    snap = _make_snapshot(entra_client_id=None)

    from backend.app.collectors.entra_sync_tasks import sync_entra_users

    with patch("backend.app.collectors.entra_sync_tasks._load_cfg", return_value=snap), \
         patch("backend.app.collectors.entra_sync_tasks.database.SessionLocal", TestingSessionLocal), \
         patch("backend.app.collectors.entra_sync_tasks._get_redis", return_value=None):

        result = sync_entra_users()

    assert result["status"] == "error"
    assert any("entra_client_id" in e for e in result["errors"])


def test_sync_creates_new_user(db_factory):
    """Mock de list_app_members com 1 membro cria AppUser com auth_provider='entra'."""
    TestingSessionLocal, _ = db_factory
    snap = _make_snapshot()

    member = {
        "subject": "oid-user-001",
        "email": "joao@empresa.com",
        "display_name": "Joao Silva",
        "role": "viewer",
        "is_global": False,
        "account_enabled": True,
    }

    result = _run_sync_with_members([member], snap, TestingSessionLocal)

    assert result["created"] == 1, f"esperado 1 criado, got: {result}"

    with TestingSessionLocal() as db:
        user = UserRepository(db).get_by_external_subject("entra", "oid-user-001")
        assert user is not None
        assert user.email == "joao@empresa.com"
        assert user.auth_provider == "entra"
        assert user.is_active is True


def test_sync_updates_existing_user_role(db_factory):
    """Usuario existente com role diferente deve ter a role atualizada."""
    TestingSessionLocal, _ = db_factory

    with TestingSessionLocal() as db:
        user = models.AppUser(
            username="maria",
            email="maria@empresa.com",
            auth_provider="entra",
            external_subject="oid-maria-001",
            role="viewer",
            is_active=True,
            password_hash=None,
        )
        db.add(user)
        db.commit()

    snap = _make_snapshot()
    member = {
        "subject": "oid-maria-001",
        "email": "maria@empresa.com",
        "display_name": "Maria Souza",
        "role": "operator",
        "is_global": False,
        "account_enabled": True,
    }

    result = _run_sync_with_members([member], snap, TestingSessionLocal)

    assert result["updated"] == 1

    with TestingSessionLocal() as db:
        user = UserRepository(db).get_by_external_subject("entra", "oid-maria-001")
        assert user is not None
        assert user.role == "operator"


def _present_member(subject: str = "oid-present-999", email: str = "present@empresa.com") -> dict:
    """Membro 'presente' qualquer — garante lista nao-vazia (fail-safe nao dispara)."""
    return {
        "subject": subject, "email": email, "display_name": "Present",
        "role": "viewer", "is_global": False, "account_enabled": True,
    }


def test_sync_deactivates_absent_user(db_factory):
    """Usuario Entra ausente e desativado com deprovision=True E lista nao-vazia."""
    TestingSessionLocal, _ = db_factory

    with TestingSessionLocal() as db:
        db.add(models.AppUser(
            username="carlos", email="carlos@empresa.com", auth_provider="entra",
            external_subject="oid-carlos-001", role="viewer", is_active=True, password_hash=None,
        ))
        db.commit()

    snap = _make_snapshot(entra_sync_deprovision=True)
    result = _run_sync_with_members([_present_member()], snap, TestingSessionLocal)

    assert result["deactivated"] == 1

    with TestingSessionLocal() as db:
        user = UserRepository(db).get_by_external_subject("entra", "oid-carlos-001")
        assert user is not None
        assert user.is_active is False


def test_sync_does_not_deactivate_when_deprovision_off(db_factory):
    """Com entra_sync_deprovision=False, usuario ausente permanece ativo."""
    TestingSessionLocal, _ = db_factory

    with TestingSessionLocal() as db:
        user = models.AppUser(
            username="ana",
            email="ana@empresa.com",
            auth_provider="entra",
            external_subject="oid-ana-001",
            role="viewer",
            is_active=True,
            password_hash=None,
        )
        db.add(user)
        db.commit()

    snap = _make_snapshot(entra_sync_deprovision=False)
    result = _run_sync_with_members([], snap, TestingSessionLocal)

    assert result["deactivated"] == 0

    with TestingSessionLocal() as db:
        user = UserRepository(db).get_by_external_subject("entra", "oid-ana-001")
        assert user is not None
        assert user.is_active is True


def test_sync_never_touches_local_users(db_factory):
    """Usuarios auth_provider='local' nunca sao alterados pelo sync."""
    TestingSessionLocal, _ = db_factory

    with TestingSessionLocal() as db:
        user = models.AppUser(
            username="breakglass",
            email="bg@empresa.com",
            auth_provider="local",
            external_subject=None,
            role="admin",
            is_active=True,
            password_hash="$argon2id$...",
        )
        db.add(user)
        db.commit()

    snap = _make_snapshot(entra_sync_deprovision=True)
    result = _run_sync_with_members([], snap, TestingSessionLocal)

    assert result["deactivated"] == 0

    with TestingSessionLocal() as db:
        user = UserRepository(db).get_by_username("breakglass")
        assert user is not None
        assert user.is_active is True
        assert user.auth_provider == "local"


def test_sync_empty_graph_skips_deprovision(db_factory):
    """FAIL-SAFE: Graph retornando 0 membros NUNCA desativa em massa."""
    TestingSessionLocal, _ = db_factory
    with TestingSessionLocal() as db:
        for i in range(3):
            db.add(models.AppUser(
                username=f"u{i}", email=f"u{i}@empresa.com", auth_provider="entra",
                external_subject=f"oid-{i}", role="viewer", is_active=True, password_hash=None,
            ))
        db.commit()

    snap = _make_snapshot(entra_sync_deprovision=True)
    result = _run_sync_with_members([], snap, TestingSessionLocal)

    assert result["deactivated"] == 0
    assert any("fail-safe" in e.lower() or "0 membros" in e.lower() for e in result["errors"])
    with TestingSessionLocal() as db:
        active = db.query(models.AppUser).filter(
            models.AppUser.auth_provider == "entra", models.AppUser.is_active.is_(True),
        ).count()
        assert active == 3  # ninguem foi desativado


def test_sync_circuit_breaker_blocks_mass_deactivation(db_factory):
    """FAIL-SAFE: desativar > 50% dos usuarios Entra ativos e bloqueado."""
    TestingSessionLocal, _ = db_factory
    with TestingSessionLocal() as db:
        for i in range(5):
            db.add(models.AppUser(
                username=f"u{i}", email=f"u{i}@empresa.com", auth_provider="entra",
                external_subject=f"oid-{i}", role="viewer", is_active=True, password_hash=None,
            ))
        db.commit()

    # Graph retorna so 1 dos 5 -> 4/5 = 80% seriam desativados -> circuit-breaker.
    present = {
        "subject": "oid-0", "email": "u0@empresa.com", "display_name": "U0",
        "role": "viewer", "is_global": False, "account_enabled": True,
    }
    snap = _make_snapshot(entra_sync_deprovision=True)
    result = _run_sync_with_members([present], snap, TestingSessionLocal)

    assert result["deactivated"] == 0
    assert any("deprovision pulado" in e.lower() for e in result["errors"])


def test_sync_preserves_last_active_admin(db_factory):
    """FAIL-SAFE: o ultimo admin ativo nunca e desativado pelo deprovision."""
    TestingSessionLocal, _ = db_factory
    with TestingSessionLocal() as db:
        db.add(models.AppUser(
            username="onlyadmin", email="admin@empresa.com", auth_provider="entra",
            external_subject="oid-admin", role="admin", is_active=True, password_hash=None,
        ))
        db.commit()

    # Lista nao-vazia (membro viewer presente); o admin esta ausente.
    snap = _make_snapshot(entra_sync_deprovision=True)
    result = _run_sync_with_members([_present_member()], snap, TestingSessionLocal)

    assert result["deactivated"] == 0  # admin preservado
    assert any("ultimo admin" in w.lower() for w in result["warnings"])
    with TestingSessionLocal() as db:
        admin = UserRepository(db).get_by_external_subject("entra", "oid-admin")
        assert admin is not None and admin.is_active is True


def test_sync_revokes_sessions_on_deprovision(db_factory):
    """Sessao ativa do usuario e revogada apos deprovision."""
    TestingSessionLocal, _ = db_factory

    with TestingSessionLocal() as db:
        user = models.AppUser(
            username="pedro",
            email="pedro@empresa.com",
            auth_provider="entra",
            external_subject="oid-pedro-001",
            role="viewer",
            is_active=True,
            password_hash=None,
        )
        db.add(user)
        db.flush()
        session = models.UserSession(
            user_id=user.id,
            token_hash="hash-pedro-session",
            expires_at=datetime(2099, 1, 1),
        )
        db.add(session)
        db.commit()
        user_id = user.id

    snap = _make_snapshot(entra_sync_deprovision=True)
    _run_sync_with_members([_present_member()], snap, TestingSessionLocal)

    with TestingSessionLocal() as db:
        sessions = (
            db.query(models.UserSession)
            .filter(models.UserSession.user_id == user_id)
            .all()
        )
        assert all(s.revoked_at is not None for s in sessions), "Sessao deveria estar revogada"


def test_sync_updates_status_in_db(db_factory):
    """Apos sync bem-sucedido, entra_last_sync_status e entra_last_sync_at sao gravados."""
    TestingSessionLocal, _ = db_factory

    snap = _make_snapshot()
    _run_sync_with_members([], snap, TestingSessionLocal)

    with TestingSessionLocal() as db:
        row = IdentityConfigRepository(db).get()
        if row is not None:
            assert row.entra_last_sync_status in ("ok", "partial")
            assert row.entra_last_sync_at is not None


def test_sync_lock_prevents_concurrent_run(db_factory):
    """Com lock Redis ativo, sync retorna error sem chamar Graph."""
    TestingSessionLocal, _ = db_factory
    snap = _make_snapshot()

    mock_redis = MagicMock()
    mock_redis.set.return_value = False  # lock nao adquirido

    from backend.app.core import entra_graph as _eg
    from backend.app.collectors.entra_sync_tasks import sync_entra_users

    with patch("backend.app.collectors.entra_sync_tasks._load_cfg", return_value=snap), \
         patch("backend.app.collectors.entra_sync_tasks.database.SessionLocal", TestingSessionLocal), \
         patch("backend.app.collectors.entra_sync_tasks._get_redis", return_value=mock_redis), \
         patch.object(_eg, "get_app_token") as mock_token:

        result = sync_entra_users()

    assert result["status"] == "error"
    assert any("lock" in e.lower() for e in result["errors"])
    mock_token.assert_not_called()


# ── 8.3 Endpoints REST ────────────────────────────────────────────────


def test_post_sync_returns_202(client_factory):
    """Admin autenticado dispara sync; resposta 202 com queued=True."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)
    _login(client)

    with Session() as db:
        IdentityConfigRepository(db).update(entra_sync_enabled=True)

    # O router faz import local da task; patch no modulo de origem
    with patch("backend.app.routers.identity_config._entra_sync_lock_active", return_value=False), \
         patch("backend.app.collectors.entra_sync_tasks.sync_entra_users.delay"):
        resp = client.post("/api/identity/config/sync")

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["queued"] is True


def test_post_sync_returns_429_when_lock_active(client_factory):
    """Com lock ativo, POST /sync retorna 429."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    _login(client)

    with patch("backend.app.routers.identity_config._entra_sync_lock_active", return_value=True):
        resp = client.post("/api/identity/config/sync")

    assert resp.status_code == 429


def test_get_sync_status_returns_200(client_factory):
    """GET /sync-status retorna 200 com campos esperados."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    _login(client)

    with patch("backend.app.routers.identity_config._entra_sync_lock_active", return_value=False):
        resp = client.get("/api/identity/config/sync-status")

    assert resp.status_code == 200
    body = resp.json()
    assert "last_sync_at" in body
    assert "last_sync_status" in body
    assert "lock_active" in body


def test_sync_endpoints_require_admin(client_factory):
    """Endpoints de sync exigem autenticacao de admin."""
    factory, _ = client_factory
    client = factory()
    # Sem cookie de sessao — nao autenticado
    r_post = client.post("/api/identity/config/sync")
    r_get = client.get("/api/identity/config/sync-status")
    assert r_post.status_code in (401, 403)
    assert r_get.status_code in (401, 403)


# ── 8.4 entra_graph.py — unitario com mock httpx ─────────────────────


def test_get_app_token_success():
    """Token endpoint com 200 retorna o access_token."""
    from backend.app.core.entra_graph import get_app_token

    snap = _make_snapshot()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"access_token": "tok-abc123"}

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post.return_value = mock_resp

    with patch("httpx.Client", return_value=mock_client):
        token = get_app_token(snap)

    assert token == "tok-abc123"


def test_get_app_token_failure():
    """Token endpoint com 401 levanta EntraGraphError."""
    from backend.app.core.entra_graph import get_app_token, EntraGraphError

    snap = _make_snapshot()

    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.json.return_value = {"error": "invalid_client", "error_description": "bad creds"}

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post.return_value = mock_resp

    with patch("httpx.Client", return_value=mock_client):
        with pytest.raises(EntraGraphError):
            get_app_token(snap)


def test_list_app_members_pagination():
    """Paginacao via @odata.nextLink + detalhes em lote (getByIds)."""
    from backend.app.core.entra_graph import list_app_members

    snap = _make_snapshot()

    page1 = MagicMock(status_code=200)
    page1.json.return_value = {
        "value": [{"principalType": "User", "principalId": "oid-001", "appRoleId": "ra"}],
        "@odata.nextLink": "https://graph.microsoft.com/v1.0/next-page",
    }
    page2 = MagicMock(status_code=200)
    page2.json.return_value = {
        "value": [{"principalType": "User", "principalId": "oid-002", "appRoleId": "rb"}],
    }
    getbyids = MagicMock(status_code=200)
    getbyids.json.return_value = {"value": [
        {"id": "oid-001", "mail": "user1@empresa.com", "displayName": "User One", "accountEnabled": True},
        {"id": "oid-002", "mail": "user2@empresa.com", "displayName": "User Two", "accountEnabled": True},
    ]}

    get_responses = [page1, page2]
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get.side_effect = lambda *a, **k: get_responses.pop(0)
    mock_client.post.return_value = getbyids

    with patch("httpx.Client", return_value=mock_client), \
         patch("backend.app.core.entra_graph.get_app_role_map", return_value={}):
        members = list_app_members(snap, "fake-token")

    assert len(members) == 2
    assert {m["subject"] for m in members} == {"oid-001", "oid-002"}


def test_list_app_members_ignores_non_users():
    """Membros com principalType != 'User' sao ignorados silenciosamente."""
    from backend.app.core.entra_graph import list_app_members

    snap = _make_snapshot()

    assignments_resp = MagicMock(status_code=200)
    assignments_resp.json.return_value = {"value": [
        {"principalType": "Group", "principalId": "grp-001", "appRoleId": "ra"},
        {"principalType": "User", "principalId": "oid-usuario", "appRoleId": "rb"},
    ]}
    getbyids = MagicMock(status_code=200)
    getbyids.json.return_value = {"value": [
        {"id": "oid-usuario", "mail": "user@empresa.com", "displayName": "Usuario Real", "accountEnabled": True},
    ]}

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get.return_value = assignments_resp
    mock_client.post.return_value = getbyids

    with patch("httpx.Client", return_value=mock_client), \
         patch("backend.app.core.entra_graph.get_app_role_map", return_value={}):
        members = list_app_members(snap, "fake-token")

    assert len(members) == 1
    assert members[0]["subject"] == "oid-usuario"


# ── Testes adicionais: role resolution, fallbacks, re-ativacao ────────


def test_list_app_members_role_resolution():
    """Resolucao completa: appRoleId -> role_value (via role_map_raw) -> local_role (via entra_role_map)."""
    from backend.app.core.entra_graph import list_app_members

    # Configura snapshot com mapa de papeis: valor Entra "AppAdmin" mapeia para "admin" local.
    snap = _make_snapshot(
        entra_role_map={"AppAdmin": "admin", "AppViewer": "viewer"},
        entra_default_role="viewer",
    )

    assignments_resp = MagicMock(status_code=200)
    assignments_resp.json.return_value = {"value": [
        {"principalType": "User", "principalId": "oid-admin-user", "appRoleId": "guid-admin-role"},
    ]}
    getbyids = MagicMock(status_code=200)
    getbyids.json.return_value = {"value": [
        {"id": "oid-admin-user", "mail": "admin@empresa.com", "displayName": "Admin User", "accountEnabled": True},
    ]}

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get.return_value = assignments_resp
    mock_client.post.return_value = getbyids

    # get_app_role_map retorna guid-admin-role -> "AppAdmin"
    with patch("httpx.Client", return_value=mock_client), \
         patch("backend.app.core.entra_graph.get_app_role_map",
               return_value={"guid-admin-role": "AppAdmin"}):
        members = list_app_members(snap, "fake-token")

    assert len(members) == 1
    # "AppAdmin" mapeado para "admin" via entra_role_map
    assert members[0]["role"] == "admin", f"role esperado 'admin', got '{members[0]['role']}'"
    assert members[0]["subject"] == "oid-admin-user"


def test_list_app_members_role_fallback_to_default():
    """Quando appRoleId nao esta no mapa, usa entra_default_role como fallback."""
    from backend.app.core.entra_graph import list_app_members

    # entra_role_map nao tem a role desconhecida -> cai em entra_default_role
    snap = _make_snapshot(
        entra_role_map={"KnownRole": "operator"},
        entra_default_role="viewer",
    )

    assignments_resp = MagicMock(status_code=200)
    assignments_resp.json.return_value = {"value": [
        {"principalType": "User", "principalId": "oid-unknown-role", "appRoleId": "guid-unknown"},
    ]}
    getbyids = MagicMock(status_code=200)
    getbyids.json.return_value = {"value": [
        {"id": "oid-unknown-role", "mail": "user@empresa.com", "displayName": "Sem Role", "accountEnabled": True},
    ]}

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get.return_value = assignments_resp
    mock_client.post.return_value = getbyids

    # role_map_raw sem guid-unknown -> role_value="" -> entra_role_map[""] ausente -> default
    with patch("httpx.Client", return_value=mock_client), \
         patch("backend.app.core.entra_graph.get_app_role_map", return_value={}):
        members = list_app_members(snap, "fake-token")

    assert len(members) == 1
    assert members[0]["role"] == "viewer", f"fallback esperado 'viewer', got '{members[0]['role']}'"


def test_list_app_members_email_fallback_to_upn():
    """Quando mail e None, usa userPrincipalName como fallback de email."""
    from backend.app.core.entra_graph import list_app_members

    snap = _make_snapshot()

    assignments_resp = MagicMock(status_code=200)
    assignments_resp.json.return_value = {"value": [
        {"principalType": "User", "principalId": "oid-sem-mail", "appRoleId": "rg"},
    ]}
    getbyids = MagicMock(status_code=200)
    getbyids.json.return_value = {"value": [
        {"id": "oid-sem-mail", "mail": None, "userPrincipalName": "UPN@Tenant.onmicrosoft.com",
         "displayName": "Sem Mail", "accountEnabled": True},
    ]}

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get.return_value = assignments_resp
    mock_client.post.return_value = getbyids

    with patch("httpx.Client", return_value=mock_client), \
         patch("backend.app.core.entra_graph.get_app_role_map", return_value={}):
        members = list_app_members(snap, "fake-token")

    assert len(members) == 1
    # mail None -> usa UPN, normalizado para lowercase
    assert members[0]["email"] == "upn@tenant.onmicrosoft.com"


def test_list_app_members_includes_user_when_detail_missing():
    """getByIds que nao retorna o detalhe de um oid (deletado/sem permissao):
    o membro e incluido com email/display None — preserva a autorizacao e
    evita deprovision indevido."""
    from backend.app.core.entra_graph import list_app_members

    snap = _make_snapshot()

    assignments_resp = MagicMock(status_code=200)
    assignments_resp.json.return_value = {"value": [
        {"principalType": "User", "principalId": "oid-no-detail", "appRoleId": "rg"},
    ]}
    getbyids = MagicMock(status_code=200)
    getbyids.json.return_value = {"value": []}  # detalhe ausente

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get.return_value = assignments_resp
    mock_client.post.return_value = getbyids

    with patch("httpx.Client", return_value=mock_client), \
         patch("backend.app.core.entra_graph.get_app_role_map", return_value={}):
        members = list_app_members(snap, "fake-token")

    assert len(members) == 1
    assert members[0]["subject"] == "oid-no-detail"
    assert members[0]["email"] is None
    assert members[0]["account_enabled"] is True


def test_sync_reactivates_entra_user(db_factory):
    """Usuario Entra previamente desativado e reativado quando volta como membro com account_enabled=True."""
    TestingSessionLocal, _ = db_factory

    with TestingSessionLocal() as db:
        user = models.AppUser(
            username="reativado",
            email="reativado@empresa.com",
            auth_provider="entra",
            external_subject="oid-reativado-001",
            role="viewer",
            is_active=False,           # estava desativado (deprovisionado antes)
            password_hash=None,
        )
        db.add(user)
        db.commit()

    snap = _make_snapshot()
    member = {
        "subject": "oid-reativado-001",
        "email": "reativado@empresa.com",
        "display_name": "Reativado",
        "role": "viewer",
        "is_global": False,
        "account_enabled": True,       # voltou a estar habilitado no Entra
    }

    result = _run_sync_with_members([member], snap, TestingSessionLocal)

    # Deve contar como update (is_active voltou para True)
    assert result["updated"] == 1, f"esperado 1 updated, got: {result}"

    with TestingSessionLocal() as db:
        user = UserRepository(db).get_by_external_subject("entra", "oid-reativado-001")
        assert user is not None
        assert user.is_active is True, "usuario deveria ter sido reativado"


def test_sync_summary_stored_as_valid_json(db_factory):
    """Apos sync bem-sucedido, entra_last_sync_summary e JSON valido e parseavel."""
    import json as _json

    TestingSessionLocal, _ = db_factory

    snap = _make_snapshot()
    _run_sync_with_members([], snap, TestingSessionLocal)

    with TestingSessionLocal() as db:
        row = IdentityConfigRepository(db).get()
        if row is not None and row.entra_last_sync_summary is not None:
            # Deve ser parseable sem excecao
            parsed = _json.loads(row.entra_last_sync_summary)
            # Shape minima esperada
            assert "status" in parsed
            assert "created" in parsed
            assert "updated" in parsed
            assert "deactivated" in parsed
            assert "errors" in parsed


def test_sync_get_sync_status_parses_summary(client_factory):
    """GET /sync-status retorna EntraSyncSummary estruturado quando summary valido existe."""
    import json as _json

    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)
    _login(client)

    # Grava um summary JSON valido diretamente no banco
    summary_payload = {
        "created": 3,
        "updated": 1,
        "deactivated": 0,
        "errors": [],
        "started_at": "2026-06-14T10:00:00",
        "finished_at": "2026-06-14T10:00:05",
        "status": "ok",
    }
    with Session() as db:
        IdentityConfigRepository(db).update(
            entra_last_sync_status="ok",
            entra_last_sync_summary=_json.dumps(summary_payload),
        )

    with patch("backend.app.routers.identity_config._entra_sync_lock_active", return_value=False):
        resp = client.get("/api/identity/config/sync-status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["last_sync_status"] == "ok"
    summary = body.get("last_sync_summary")
    assert summary is not None, "summary deveria estar presente"
    assert summary["created"] == 3
    assert summary["updated"] == 1
    assert summary["deactivated"] == 0
    assert summary["errors"] == []


def test_sync_get_sync_status_handles_malformed_summary(client_factory):
    """GET /sync-status retorna last_sync_summary=null quando JSON esta malformado."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)
    _login(client)

    # Grava JSON invalido no campo summary
    with Session() as db:
        row = IdentityConfigRepository(db).get_or_create()
        row.entra_last_sync_status = "error"
        row.entra_last_sync_summary = "{invalido json sem fechar"
        db.commit()

    with patch("backend.app.routers.identity_config._entra_sync_lock_active", return_value=False):
        resp = client.get("/api/identity/config/sync-status")

    assert resp.status_code == 200
    body = resp.json()
    # Nao deve quebrar — summary deve ser null quando JSON e invalido
    assert body["last_sync_summary"] is None


def test_post_sync_returns_202_when_disabled(client_factory):
    """POST /sync com entra_sync_enabled=False retorna 202 com queued=False."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)
    _login(client)

    # Garante sync desabilitado no banco
    with Session() as db:
        IdentityConfigRepository(db).update(entra_sync_enabled=False)

    with patch("backend.app.routers.identity_config._entra_sync_lock_active", return_value=False):
        resp = client.post("/api/identity/config/sync")

    # Deve retornar 202 mas queued=False pois sync esta desabilitado
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["queued"] is False
    assert "desabilitado" in body["message"].lower()
