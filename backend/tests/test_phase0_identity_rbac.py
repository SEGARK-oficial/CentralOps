"""Entra — base de identidade federada + RBAC de escopo global.

Cobertura:
  * ``core.tenant.has_global_scope`` e os helpers de scoping derivados.
  * Separação de design: escopo global NÃO concede privilégio de admin.
  * Campos novos do AppUser (email / auth_provider / external_subject /
    is_global) aceitos e expostos pelo CRUD de usuário e pela sessão.
  * Unicidade (auth_provider, external_subject) e e-mail.
  * RBAC global end-to-end: operator global vê todas as orgs; operator
    escopado vê apenas a sua.
  * Migração lightweight: upgrade de schema legado (SQLite) e idempotência.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

from datetime import datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core import tenant
from backend.app.db import database as db_module
from backend.app.db import models
from backend.app.db.database import Base, get_session, _run_lightweight_migrations
from backend.app.main import app


# ── Unit: has_global_scope + helpers de scoping ───────────────────────


def _user(role: str = "viewer", *, is_global: bool = False, organization_id=None) -> models.AppUser:
    """AppUser transiente (não persistido) só para exercitar a lógica pura."""
    return models.AppUser(
        username="u",
        role=role,
        is_global=is_global,
        organization_id=organization_id,
        is_active=True,
    )


def test_has_global_scope_none_is_false():
    assert tenant.has_global_scope(None) is False


def test_admin_is_always_global_even_without_flag():
    assert tenant.has_global_scope(_user(role="admin")) is True
    assert tenant.has_global_scope(_user(role="admin", is_global=False)) is True


@pytest.mark.parametrize("role", ["viewer", "operator", "engineer"])
def test_non_admin_with_flag_is_global(role):
    assert tenant.has_global_scope(_user(role=role, is_global=True)) is True


def test_scoped_user_is_not_global():
    assert tenant.has_global_scope(_user(role="operator", organization_id=5)) is False


def test_scoped_organization_ids_matrix():
    assert tenant.scoped_organization_ids(_user(role="admin")) is None
    assert tenant.scoped_organization_ids(_user(role="operator", is_global=True)) is None
    assert tenant.scoped_organization_ids(_user(role="operator", organization_id=7)) == [7]
    assert tenant.scoped_organization_ids(_user(role="viewer", organization_id=None)) == []


def test_can_access_organization():
    glob = _user(role="operator", is_global=True)
    scoped = _user(role="operator", organization_id=7)
    assert tenant.can_access_organization(glob, 999) is True
    assert tenant.can_access_organization(scoped, 7) is True
    assert tenant.can_access_organization(scoped, 8) is False
    assert tenant.can_access_organization(_user(role="viewer", organization_id=None), 1) is False


def test_global_scope_does_not_grant_admin():
    """Garantia de design: as duas dimensões são independentes — escopo global
    de leitura nunca implica capability administrativa (is_admin)."""
    glob_operator = _user(role="operator", is_global=True)
    assert tenant.has_global_scope(glob_operator) is True
    assert tenant.is_admin(glob_operator) is False


def test_filter_organizations_for_user():
    class _Org:
        def __init__(self, oid):
            self.id = oid

    orgs = [_Org(1), _Org(2), _Org(3)]

    def get_id(o):
        return o.id

    glob = tenant.filter_organizations_for_user(_user(role="viewer", is_global=True), orgs, get_org_id=get_id)
    assert {o.id for o in glob} == {1, 2, 3}
    scoped = tenant.filter_organizations_for_user(_user(role="viewer", organization_id=2), orgs, get_org_id=get_id)
    assert {o.id for o in scoped} == {2}


# ── API: CRUD de usuário + serialização ───────────────────────────────


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

    yield factory, TestingSessionLocal, engine

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


def _login(client: TestClient, username: str, password: str = "Passw0rd123!") -> None:
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text


def test_create_user_with_email_and_global(client_factory):
    factory, _, _ = client_factory
    c = factory()
    _bootstrap_admin(c)
    r = c.post(
        "/api/auth/users",
        json={
            "username": "analyst",
            "password": "AnalystPass123!",
            "email": "Analyst@SOC.com",
            "role": "operator",
            "is_global": True,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["email"] == "analyst@soc.com"  # normalizado p/ lowercase
    assert body["is_global"] is True
    assert body["auth_provider"] == "local"
    assert body["role"] == "operator"


def test_org_creation_enforces_tier_org_limit(client_factory, monkeypatch):
    """Starter single-tenant: com o teto do tier (via licença) em 1, criar uma
    2ª org é bloqueado com 403 + mensagem clara."""
    from backend.app.core import edition

    factory, _, _ = client_factory
    c = factory()
    _bootstrap_admin(c)
    monkeypatch.setattr(edition, "max_organizations", lambda: 1)

    first = c.post("/api/organizations/", json={"name": "Tenant A"})  # 1ª (dentro do teto)
    assert first.status_code == 200, first.text  # a 1ª org DEVE passar (teto=1)
    second = c.post("/api/organizations/", json={"name": "Tenant B"})  # excede
    assert second.status_code == 403, second.text
    assert "Limite de organizações" in second.json()["detail"]


def test_org_creation_tier_limit_counts_active_only(client_factory, monkeypatch):
    """O teto conta apenas orgs ATIVAS. Uma org soft-deletada
    (is_active=False) NÃO consome a vaga — senão um cliente Starter (teto=1) ficaria
    travado p/ sempre depois de desativar a 1ª org."""
    from backend.app.core import edition

    factory, _, _ = client_factory
    c = factory()
    _bootstrap_admin(c)
    monkeypatch.setattr(edition, "max_organizations", lambda: 1)

    first = c.post("/api/organizations/", json={"name": "Tenant A"})
    assert first.status_code == 200, first.text
    org_id = first.json()["id"]

    # 2ª org barrada enquanto a 1ª está ativa.
    assert c.post("/api/organizations/", json={"name": "Tenant B"}).status_code == 403

    # soft-delete (is_active=False) via bulk/deactivate libera a vaga.
    deact = c.post("/api/organizations/bulk/deactivate", json={"ids": [org_id]})
    assert deact.status_code == 200, deact.text

    second = c.post("/api/organizations/", json={"name": "Tenant B"})
    assert second.status_code == 200, second.text


def test_org_creation_unlimited_without_license(client_factory):
    """Fail-closed-to-Community: sem licença (claim ausente), max_organizations é None →
    SEM teto (o core AGPL é irrestrito; a trava é feature do tier pago)."""
    factory, _, _ = client_factory
    c = factory()
    _bootstrap_admin(c)
    for name in ("A", "B", "C"):
        r = c.post("/api/organizations/", json={"name": name})
        assert r.status_code == 200, r.text


def test_create_user_defaults(client_factory):
    factory, _, _ = client_factory
    c = factory()
    _bootstrap_admin(c)
    r = c.post(
        "/api/auth/users",
        json={"username": "u2", "password": "Passw0rd123!", "role": "viewer"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["email"] is None
    assert body["is_global"] is False
    assert body["auth_provider"] == "local"


def test_create_user_duplicate_email_conflicts(client_factory):
    factory, _, _ = client_factory
    c = factory()
    _bootstrap_admin(c)
    c.post(
        "/api/auth/users",
        json={"username": "a", "password": "Passw0rd123!", "email": "dup@x.com", "role": "viewer"},
    )
    r = c.post(
        "/api/auth/users",
        json={"username": "b", "password": "Passw0rd123!", "email": "DUP@x.com", "role": "viewer"},
    )
    assert r.status_code == 409, r.text


def test_create_user_invalid_email_rejected(client_factory):
    factory, _, _ = client_factory
    c = factory()
    _bootstrap_admin(c)
    r = c.post(
        "/api/auth/users",
        json={"username": "a", "password": "Passw0rd123!", "email": "notanemail", "role": "viewer"},
    )
    assert r.status_code == 422, r.text


def test_update_user_toggle_global_and_email(client_factory):
    factory, _, _ = client_factory
    c = factory()
    _bootstrap_admin(c)
    created = c.post(
        "/api/auth/users",
        json={"username": "a", "password": "Passw0rd123!", "role": "operator"},
    ).json()
    uid = created["id"]

    r = c.put(f"/api/auth/users/{uid}", json={"is_global": True, "email": "a@x.com"})
    assert r.status_code == 200, r.text
    assert r.json()["is_global"] is True
    assert r.json()["email"] == "a@x.com"

    r = c.put(f"/api/auth/users/{uid}", json={"is_global": False})
    assert r.status_code == 200, r.text
    assert r.json()["is_global"] is False
    assert r.json()["email"] == "a@x.com"  # não foi tocado


def test_update_user_duplicate_email_conflicts(client_factory):
    factory, _, _ = client_factory
    c = factory()
    _bootstrap_admin(c)
    c.post(
        "/api/auth/users",
        json={"username": "a", "password": "Passw0rd123!", "email": "a@x.com", "role": "viewer"},
    )
    other = c.post(
        "/api/auth/users",
        json={"username": "b", "password": "Passw0rd123!", "role": "viewer"},
    ).json()
    r = c.put(f"/api/auth/users/{other['id']}", json={"email": "A@X.com"})
    assert r.status_code == 409, r.text


def test_me_endpoint_exposes_identity_fields(client_factory):
    factory, _, _ = client_factory
    admin_c = factory()
    _bootstrap_admin(admin_c)
    admin_c.post(
        "/api/auth/users",
        json={
            "username": "glob",
            "password": "Passw0rd123!",
            "email": "glob@soc.com",
            "role": "operator",
            "is_global": True,
        },
    )
    user_c = factory()
    _login(user_c, "glob")
    r = user_c.get("/api/auth/me")
    assert r.status_code == 200, r.text
    me = r.json()
    assert me["is_global"] is True
    assert me["auth_provider"] == "local"
    assert me["email"] == "glob@soc.com"


# ── RBAC global end-to-end ────────────────────────────────────────────


def test_global_operator_sees_all_orgs_scoped_sees_one(client_factory):
    factory, Session, _ = client_factory
    admin_c = factory()
    _bootstrap_admin(admin_c)

    with Session() as s:
        oa = models.Organization(name="Org A", slug="org-a")
        ob = models.Organization(name="Org B", slug="org-b")
        s.add_all([oa, ob])
        s.commit()
        a_id = oa.id

    admin_c.post(
        "/api/auth/users",
        json={"username": "scoped", "password": "Passw0rd123!", "role": "operator", "organization_id": a_id},
    )
    admin_c.post(
        "/api/auth/users",
        json={"username": "global", "password": "Passw0rd123!", "role": "operator", "is_global": True},
    )

    scoped_c = factory()
    _login(scoped_c, "scoped")
    r = scoped_c.get("/api/organizations")
    assert r.status_code == 200, r.text
    assert {o["name"] for o in r.json()} == {"Org A"}

    global_c = factory()
    _login(global_c, "global")
    r = global_c.get("/api/organizations")
    assert r.status_code == 200, r.text
    assert {"Org A", "Org B"} <= {o["name"] for o in r.json()}


# ── Unicidade de identidade ───────────────────────────────────────────


def test_unique_external_subject_per_provider(client_factory):
    _, Session, _ = client_factory
    with Session() as s:
        s.add(models.AppUser(username="e1", auth_provider="entra", external_subject="oid-1", role="viewer", is_active=True))
        s.commit()
    with Session() as s:
        s.add(models.AppUser(username="e2", auth_provider="entra", external_subject="oid-1", role="viewer", is_active=True))
        with pytest.raises(IntegrityError):
            s.commit()


def test_multiple_local_users_with_null_subject_ok(client_factory):
    _, Session, _ = client_factory
    with Session() as s:
        s.add(models.AppUser(username="l1", auth_provider="local", password_hash="x", role="viewer", is_active=True))
        s.add(models.AppUser(username="l2", auth_provider="local", password_hash="y", role="viewer", is_active=True))
        s.commit()  # múltiplos external_subject NULL não conflitam
        assert s.query(models.AppUser).filter_by(auth_provider="local").count() == 2


# ── Migração lightweight ──────────────────────────────────────────────


def test_create_all_has_identity_columns(client_factory):
    _, _, engine = client_factory
    cols = {c["name"] for c in inspect(engine).get_columns("app_users")}
    assert {"email", "auth_provider", "external_subject", "is_global"} <= cols


def test_migration_upgrades_legacy_schema_sqlite(tmp_path, monkeypatch):
    """Schema legado (sem as colunas novas) → migração adiciona colunas,
    preenche defaults e preserva contas existentes. Idempotente em re-run."""
    engine = create_engine(f"sqlite:///{tmp_path}/legacy.db")
    now = datetime.utcnow()
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE app_users ("
                " id INTEGER PRIMARY KEY, uuid VARCHAR, username VARCHAR NOT NULL,"
                " display_name VARCHAR, password_hash VARCHAR NOT NULL,"
                " organization_id INTEGER, role VARCHAR NOT NULL DEFAULT 'viewer',"
                " is_active BOOLEAN NOT NULL DEFAULT 1, last_login_at TIMESTAMP,"
                " created_at TIMESTAMP, updated_at TIMESTAMP)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO app_users "
                "(uuid, username, password_hash, role, is_active, created_at, updated_at) "
                "VALUES ('u-legacy', 'legacy', 'pbkdf2_sha256$x', 'admin', 1, :n, :n)"
            ),
            {"n": now},
        )

    monkeypatch.setattr(db_module, "engine", engine)
    _run_lightweight_migrations()

    cols = {c["name"] for c in inspect(engine).get_columns("app_users")}
    assert {"email", "auth_provider", "external_subject", "is_global"} <= cols

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT auth_provider, is_global FROM app_users WHERE username = 'legacy'")
        ).fetchone()
    assert row.auth_provider == "local"  # DEFAULT preencheu a linha existente
    assert row.is_global in (0, False)

    # Segunda execução não deve falhar (ADD COLUMN guardado por checagem).
    _run_lightweight_migrations()
    cols2 = {c["name"] for c in inspect(engine).get_columns("app_users")}
    assert {"email", "auth_provider", "external_subject", "is_global"} <= cols2
