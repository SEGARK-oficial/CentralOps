"""Hardening — isolamento do ADMIN-DE-ORG (admin escopado).

Persona: ``role=admin`` + ``organization_id`` + ``is_global=False`` (MSSP/
self-service). A auditoria de multi-tenancy achou uma classe consistente de
gaps: listagens sem filtro de escopo, mutação de recursos GLOBAIS (org NULL),
reassign de org sem re-check, singletons de plataforma (SSO/SMTP/collector-
config/licença) e escalação via Service Account de plataforma. Este arquivo
trava cada um — e a sanidade de que o admin GLOBAL segue com poder pleno.

Imports usam ``backend.app.*`` (gotcha .so dual-root).
"""

from __future__ import annotations

from typing import Any, Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db.database import Base, get_session
from backend.app.main import app


@pytest.fixture()
def env() -> Generator[Any, None, None]:
    """Harness completo: admin global + 2 orgs + admin ESCOPADO da org A.

    Devolve um dict com: ``global_admin`` (client logado), ``scoped`` (client
    logado como admin-de-org da org A), ``org_a``/``org_b`` (ids) e ``factory``.
    """
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

    ga = factory()
    r = ga.post(
        "/api/auth/bootstrap",
        json={"username": "root", "password": "AdminPassword123!", "display_name": "Root"},
    )
    assert r.status_code in (200, 201), r.text

    def create_org(name: str) -> int:
        r = ga.post(
            "/api/organizations",
            json={"name": name, "slug": name.lower().replace(" ", "-")},
        )
        assert r.status_code in (200, 201), r.text
        return r.json()["id"]

    org_a = create_org("Org A")
    org_b = create_org("Org B")

    # Admin ESCOPADO da org A (is_global default False).
    r = ga.post(
        "/api/auth/users",
        json={
            "username": "orgadmin",
            "password": "OrgAdminPassword123!",
            "role": "admin",
            "organization_id": org_a,
        },
    )
    assert r.status_code in (200, 201), r.text

    scoped = factory()
    r = scoped.post(
        "/api/auth/login",
        json={"username": "orgadmin", "password": "OrgAdminPassword123!"},
    )
    assert r.status_code == 200, r.text

    yield {
        "global_admin": ga,
        "scoped": scoped,
        "org_a": org_a,
        "org_b": org_b,
        "factory": factory,
        "Session": TestingSession,
    }

    for c in clients:
        c.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


def _mk_user(client: TestClient, username: str, org_id: int | None, role: str = "viewer") -> str:
    body: dict[str, Any] = {
        "username": username,
        "password": "SomePassword123!",
        "role": role,
    }
    if org_id is not None:
        body["organization_id"] = org_id
    r = client.post("/api/auth/users", json=body)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _mk_destination(client: TestClient, name: str, org_id: int | None = None) -> str:
    body: dict[str, Any] = {
        "name": name,
        "kind": "syslog_rfc3164",
        "config": {"host": "h", "port": 514},
        "auto_route": False,
    }
    if org_id is not None:
        body["organization_id"] = org_id
    r = client.post("/api/collectors/destinations", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _mk_route(client: TestClient, name: str, dest_id: str, org_id: int | None = None) -> str:
    body: dict[str, Any] = {"name": name, "condition": {}, "destination_ids": [dest_id]}
    if org_id is not None:
        body["organization_id"] = org_id
    r = client.post("/api/collectors/routes", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ── Usuários ──────────────────────────────────────────────────────────────────


def test_scoped_admin_lists_only_own_org_users(env) -> None:
    ga, sc = env["global_admin"], env["scoped"]
    _mk_user(ga, "alice-a", env["org_a"])
    _mk_user(ga, "bob-b", env["org_b"])

    r = sc.get("/api/auth/users")
    assert r.status_code == 200, r.text
    names = {u["username"] for u in r.json()}
    assert "alice-a" in names and "orgadmin" in names
    assert "bob-b" not in names, "usuário de outra org vazou"
    assert "root" not in names, "admin de plataforma (org NULL) vazou"

    # Admin global segue vendo todos.
    all_names = {u["username"] for u in ga.get("/api/auth/users").json()}
    assert {"alice-a", "bob-b", "orgadmin", "root"} <= all_names


def test_scoped_admin_cannot_delete_outside_own_org(env) -> None:
    ga, sc = env["global_admin"], env["scoped"]
    uid_b = _mk_user(ga, "victim-b", env["org_b"])
    uid_a = _mk_user(ga, "victim-a", env["org_a"])

    assert sc.delete(f"/api/auth/users/{uid_b}").status_code == 403
    # admin de plataforma (org NULL) está acima do teto do escopado
    root_id = next(u["id"] for u in ga.get("/api/auth/users").json() if u["username"] == "root")
    assert sc.delete(f"/api/auth/users/{root_id}").status_code == 403
    # a própria org continua deletável
    assert sc.delete(f"/api/auth/users/{uid_a}").status_code in (200, 204)


def test_scoped_admin_cannot_create_cross_org_or_global_user(env) -> None:
    sc = env["scoped"]
    r = sc.post(
        "/api/auth/users",
        json={
            "username": "smuggle",
            "password": "SomePassword123!",
            "role": "viewer",
            "organization_id": env["org_b"],
        },
    )
    assert r.status_code == 403, r.text
    r = sc.post(
        "/api/auth/users",
        json={
            "username": "smuggle2",
            "password": "SomePassword123!",
            "role": "viewer",
            "organization_id": env["org_a"],
            "is_global": True,
        },
    )
    assert r.status_code == 403, r.text


# ── Destinos ──────────────────────────────────────────────────────────────────


def test_scoped_admin_cannot_mutate_global_destination(env) -> None:
    ga, sc = env["global_admin"], env["scoped"]
    gdest = _mk_destination(ga, "shared-global")  # org NULL

    # visível (roteável) …
    assert sc.get(f"/api/collectors/destinations/{gdest}").status_code == 200
    # … mas imutável para o escopado
    assert sc.put(f"/api/collectors/destinations/{gdest}", json={"name": "hijack"}).status_code == 403
    assert sc.delete(f"/api/collectors/destinations/{gdest}").status_code == 403
    # admin global segue podendo
    assert ga.put(f"/api/collectors/destinations/{gdest}", json={"name": "renamed-ok"}).status_code == 200


def test_scoped_admin_cannot_reassign_destination_org(env) -> None:
    ga, sc = env["global_admin"], env["scoped"]
    adest = _mk_destination(ga, "dest-a", env["org_a"])

    # editar a própria org: ok
    assert sc.put(f"/api/collectors/destinations/{adest}", json={"name": "dest-a2"}).status_code == 200
    # mover para outra org: 403
    r = sc.put(f"/api/collectors/destinations/{adest}", json={"organization_id": env["org_b"]})
    assert r.status_code == 403, r.text


# ── Rotas ─────────────────────────────────────────────────────────────────────


def test_scoped_admin_cannot_mutate_global_route(env) -> None:
    ga, sc = env["global_admin"], env["scoped"]
    gdest = _mk_destination(ga, "gdest-r")
    groute = _mk_route(ga, "global-route", gdest)  # org NULL

    assert sc.put(f"/api/collectors/routes/{groute}", json={"name": "hijack"}).status_code == 403
    assert sc.delete(f"/api/collectors/routes/{groute}").status_code == 403
    assert ga.put(f"/api/collectors/routes/{groute}", json={"name": "still-mine"}).status_code == 200


def test_scoped_admin_cannot_reassign_route_org(env) -> None:
    ga, sc = env["global_admin"], env["scoped"]
    adest = _mk_destination(ga, "dest-ra", env["org_a"])
    aroute = _mk_route(ga, "route-a", adest, env["org_a"])

    assert sc.put(f"/api/collectors/routes/{aroute}", json={"name": "route-a2"}).status_code == 200
    r = sc.put(f"/api/collectors/routes/{aroute}", json={"organization_id": env["org_b"]})
    assert r.status_code == 403, r.text


# ── Singletons de plataforma ─────────────────────────────────────────────────


def test_scoped_admin_cannot_touch_identity_sso_config(env) -> None:
    ga, sc = env["global_admin"], env["scoped"]
    assert sc.get("/api/identity/config").status_code == 403
    assert sc.put("/api/identity/config", json={"entra_enabled": False}).status_code == 403
    assert ga.get("/api/identity/config").status_code == 200


def test_scoped_admin_cannot_update_platform_collector_config(env) -> None:
    ga, sc = env["global_admin"], env["scoped"]
    assert sc.put("/api/collectors/config", json={"dedupe_ttl_days": 5}).status_code == 403
    assert ga.get("/api/collectors/config").status_code == 200


def test_scoped_admin_cannot_manage_platform_license(env) -> None:
    sc = env["scoped"]
    # token com >=16 chars passa o schema (min_length) → chega ao guard de escopo,
    # que deve 403 ANTES da verificação criptográfica.
    r = sc.post("/api/licenses/activate", json={"token": "x" * 32})
    assert r.status_code == 403, r.text
    assert sc.delete("/api/licenses").status_code == 403


def test_scoped_admin_cannot_update_smtp_config(env) -> None:
    ga, sc = env["global_admin"], env["scoped"]
    assert sc.put("/api/emails/config", json={"smtp_host": "evil.example"}).status_code == 403
    assert ga.put("/api/emails/config", json={"smtp_host": "smtp.ok"}).status_code == 200


# ── Notificações de e-mail ────────────────────────────────────────────────────


def test_email_recipients_are_org_scoped(env) -> None:
    ga, sc = env["global_admin"], env["scoped"]
    ga.post("/api/emails/", json={"email": "a@a.com", "organization_id": env["org_a"]})
    ga.post("/api/emails/", json={"email": "b@b.com", "organization_id": env["org_b"]})
    ga.post("/api/emails/", json={"email": "platform@x.com"})  # org NULL (global)

    rows = sc.get("/api/emails/").json()
    emails = {r["email"] for r in rows}
    assert emails == {"a@a.com"}, f"escopado deveria ver só a própria org: {emails}"

    # criar em outra org: 403; deletar destinatário de outra org: 403
    assert sc.post(
        "/api/emails/", json={"email": "x@b.com", "organization_id": env["org_b"]}
    ).status_code == 403
    b_id = next(r["id"] for r in ga.get("/api/emails/").json() if r["email"] == "b@b.com")
    assert sc.delete(f"/api/emails/{b_id}").status_code == 403


# ── Ring de auditoria + captura ao vivo (dados de eventos) ────────────────────


def test_scoped_admin_cannot_read_or_clear_another_orgs_audit_ring(env) -> None:
    sc = env["scoped"]
    assert sc.get(f"/api/collectors/config/audit/recent?org_id={env['org_b']}").status_code == 403
    assert sc.delete(f"/api/collectors/config/audit/recent?org_id={env['org_b']}").status_code == 403


def test_scoped_admin_cannot_capture_another_orgs_traffic(env) -> None:
    sc = env["scoped"]
    r = sc.post(
        f"/api/collectors/config/capture-sessions?org_id={env['org_b']}",
        json={"duration_seconds": 60},
    )
    assert r.status_code == 403, r.text


# ── Service accounts (anti-escalação) ────────────────────────────────────────


def test_scoped_admin_cannot_create_platform_service_account(env) -> None:
    sc = env["scoped"]
    # SA sem org + role admin herdaria escopo GLOBAL (shim) → escalação; 403.
    r = sc.post("/api/v1/service-accounts", json={"name": "esc-sa", "role": "admin"})
    assert r.status_code == 403, r.text
    # SA em outra org: 403
    r = sc.post(
        "/api/v1/service-accounts",
        json={"name": "esc-sa-b", "role": "operator", "organization_id": env["org_b"]},
    )
    assert r.status_code == 403, r.text
    # SA na própria org: ok
    r = sc.post(
        "/api/v1/service-accounts",
        json={"name": "ok-sa", "role": "operator", "organization_id": env["org_a"]},
    )
    assert r.status_code == 201, r.text


def test_scoped_admin_sa_list_and_platform_sa_hidden(env) -> None:
    ga, sc = env["global_admin"], env["scoped"]
    r = ga.post("/api/v1/service-accounts", json={"name": "platform-sa", "role": "admin"})
    assert r.status_code == 201, r.text
    platform_sa_id = r.json()["id"]
    r = ga.post(
        "/api/v1/service-accounts",
        json={"name": "a-sa", "role": "operator", "organization_id": env["org_a"]},
    )
    assert r.status_code == 201, r.text

    names = {sa["name"] for sa in sc.get("/api/v1/service-accounts").json()}
    assert "a-sa" in names
    assert "platform-sa" not in names, "SA de plataforma vazou p/ admin escopado"

    # operar o SA de plataforma: 403 (get/update/delete/token)
    assert sc.get(f"/api/v1/service-accounts/{platform_sa_id}").status_code == 403
    assert sc.delete(f"/api/v1/service-accounts/{platform_sa_id}").status_code == 403
    assert (
        sc.post(f"/api/v1/service-accounts/{platform_sa_id}/tokens", json={"name": "t"}).status_code
        == 403
    )


def test_global_admin_unaffected_sanity(env) -> None:
    """O hardening não pode regredir o operador de plataforma."""
    ga = env["global_admin"]
    gdest = _mk_destination(ga, "sanity-dest")
    groute = _mk_route(ga, "sanity-route", gdest)
    assert ga.put(f"/api/collectors/routes/{groute}", json={"name": "sanity-2"}).status_code == 200
    assert ga.delete(f"/api/collectors/routes/{groute}").status_code == 204
    assert ga.delete(f"/api/collectors/destinations/{gdest}").status_code == 204
    assert ga.get("/api/emails/config").status_code == 200
    assert ga.get("/api/collectors/config").status_code == 200
