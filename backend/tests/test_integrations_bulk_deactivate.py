"""Testes do POST /api/integrations/bulk/deactivate (PR #4).

Cobre:
  * Happy path: 3 ids ativos → processed=3, deactivated=3, errors=[].
  * Idempotência: id já-inativo → erro 'already_inactive', não 500.
  * Id inexistente → erro 'not_found'.
  * Cross-tenant não-admin: ids fora do escopo da org viram 'not_found'
    (sem revelar existência).
  * Partner blocking: body com Partner integration → 422 antes de mutar.
  * Validação: body vazio → 422; > 500 ids → 422.
  * Permissão: viewer recebe 403.
  * Audit log: uma entrada AuditLog(action='bulk_deactivate_integration')
    por sucesso.
  * Filtros novos no GET /api/integrations/ (name/kind/status/region/
    data_geography/page/size).

Roda com SQLite in-memory + override de get_session, mesmo padrão dos
demais testes da suíte.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import database as _db_module
from backend.app.db import models  # noqa: F401  — registra tabelas
from backend.app.db.database import Base, get_session
from backend.app.main import app


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture()
def client_factory(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(_db_module, "SessionLocal", TestingSession)

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


# ── Helpers ──────────────────────────────────────────────────────────────


def _bootstrap_admin(client: TestClient) -> dict[str, Any]:
    r = client.post(
        "/api/auth/bootstrap",
        json={
            "username": "admin",
            "password": "AdminPassword123!",
            "display_name": "Admin",
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _create_user(
    admin_client: TestClient,
    *,
    username: str,
    role: str,
    organization_id: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "username": username,
        "password": "TestPassword123!",
        "display_name": username.title(),
        "role": role,
    }
    if organization_id is not None:
        payload["organization_id"] = organization_id
    r = admin_client.post("/api/auth/users", json=payload)
    assert r.status_code == 200, f"Falha ao criar user {username}: {r.text}"
    return r.json()


def _login_as(
    client: TestClient,
    *,
    username: str,
    password: str = "TestPassword123!",
) -> None:
    r = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert r.status_code == 200, f"Falha ao logar como {username}: {r.text}"


def _seed_organization(Session, *, name: str, slug: str | None = None) -> int:
    with Session() as db:
        db.execute(
            text(
                "INSERT INTO organizations(name, slug, is_active, created_at, "
                "updated_at, auto_managed) "
                "VALUES (:name, :slug, 1, datetime('now'), datetime('now'), 0)"
            ),
            {"name": name, "slug": slug or name.lower().replace(" ", "-")},
        )
        db.commit()
        org_id = db.execute(
            text("SELECT id FROM organizations WHERE name=:n"), {"n": name}
        ).fetchone().id
    return org_id


def _seed_integration(
    Session,
    *,
    org_id: int,
    name: str,
    platform: str = "sophos",
    kind: str = "tenant",
    is_active: bool = True,
    region: str | None = None,
    data_geography: str | None = None,
) -> int:
    with Session() as db:
        db.execute(
            text(
                "INSERT INTO integrations(organization_id, name, platform, "
                "is_active, kind, auth_status, created_at, updated_at, "
                "auto_managed, region, data_geography) "
                "VALUES (:org, :name, :plat, :act, :kind, 'unknown', "
                "datetime('now'), datetime('now'), 0, :region, :geo)"
            ),
            {
                "org": org_id,
                "name": name,
                "plat": platform,
                "act": 1 if is_active else 0,
                "kind": kind,
                "region": region,
                "geo": data_geography,
            },
        )
        db.commit()
        int_id = db.execute(
            text("SELECT id FROM integrations WHERE name=:n"), {"n": name}
        ).fetchone().id
    return int_id


def _count_audit_logs(Session, *, action: str) -> int:
    with Session() as db:
        return db.execute(
            text("SELECT COUNT(*) FROM audit_logs WHERE action=:a"),
            {"a": action},
        ).scalar() or 0


# Patch _deregister_from_beat to no-op for these tests — we don't have a
# Redis available, the beat helper já é fire-and-forget mas evitamos logs.


@pytest.fixture(autouse=True)
def _patch_beat_helpers():
    with patch("backend.app.routers.integrations._deregister_from_beat"):
        yield


# ── Testes: bulk/deactivate ──────────────────────────────────────────────


def test_bulk_deactivate_happy_path(client_factory) -> None:
    """3 ids ativos → processed=3, deactivated=3, audit logs gravados."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    org_id = _seed_organization(Session, name="HappyOrg")
    ids = [
        _seed_integration(Session, org_id=org_id, name=f"Int-{i}")
        for i in range(3)
    ]

    audit_before = _count_audit_logs(Session, action="bulk_deactivate_integration")

    r = client.post("/api/integrations/bulk/deactivate", json={"ids": ids})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["processed"] == 3
    assert body["deactivated"] == 3
    assert body["errors"] == []

    # DB state: todas inativas.
    with Session() as db:
        rows = db.execute(
            text("SELECT id, is_active FROM integrations WHERE id IN (:a, :b, :c)"),
            {"a": ids[0], "b": ids[1], "c": ids[2]},
        ).fetchall()
    assert all(row.is_active == 0 for row in rows), "Todas devem estar inativas"

    audit_after = _count_audit_logs(Session, action="bulk_deactivate_integration")
    assert audit_after - audit_before == 3, (
        f"Esperava 3 audit logs novos, got {audit_after - audit_before}"
    )


def test_bulk_deactivate_idempotent_already_inactive(client_factory) -> None:
    """ID já-inativo → erro 'already_inactive', NÃO 500. Outros ids ok."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    org_id = _seed_organization(Session, name="IdempOrg")
    active_id = _seed_integration(Session, org_id=org_id, name="Active-One")
    inactive_id = _seed_integration(
        Session, org_id=org_id, name="Already-Off", is_active=False
    )

    r = client.post(
        "/api/integrations/bulk/deactivate",
        json={"ids": [active_id, inactive_id]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["processed"] == 2
    assert body["deactivated"] == 1, "Apenas a ativa deve ter sido deactivated"
    errors = {e["id"]: e["reason"] for e in body["errors"]}
    assert errors == {inactive_id: "already_inactive"}


def test_bulk_deactivate_unknown_id_returns_not_found(client_factory) -> None:
    """ID inexistente → erro 'not_found' no items, não 500."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    org_id = _seed_organization(Session, name="UnknownOrg")
    valid_id = _seed_integration(Session, org_id=org_id, name="Real")

    r = client.post(
        "/api/integrations/bulk/deactivate",
        json={"ids": [valid_id, 9999]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deactivated"] == 1
    errors = {e["id"]: e["reason"] for e in body["errors"]}
    assert errors == {9999: "not_found"}


def test_bulk_deactivate_partner_blocking_returns_422(client_factory) -> None:
    """Partner integration no body → 422 ANTES de mutar nada (decisão #2)."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    org_id = _seed_organization(Session, name="PartnerOrg")
    tenant_id = _seed_integration(Session, org_id=org_id, name="Tenant-A")
    partner_id = _seed_integration(
        Session, org_id=org_id, name="Partner-X", kind="partner"
    )

    r = client.post(
        "/api/integrations/bulk/deactivate",
        json={"ids": [tenant_id, partner_id]},
    )
    assert r.status_code == 422, r.text
    error = r.json()["error"]
    assert error["code"] == "integration.partner_bulk_unsupported"
    # Mensagem expecta o blocker_id no payload.
    assert partner_id in error["details"]["partner_ids"]

    # Garantia crítica: nenhuma integração foi desativada (atomicidade pre-flight).
    with Session() as db:
        rows = db.execute(
            text("SELECT id, is_active FROM integrations WHERE id IN (:a, :b)"),
            {"a": tenant_id, "b": partner_id},
        ).fetchall()
    assert all(row.is_active == 1 for row in rows), (
        "Nenhuma integração deveria ter sido desativada quando há Partner blocker"
    )


def test_bulk_deactivate_organization_kind_also_blocked(client_factory) -> None:
    """kind='organization' (Sophos auto-managed) também é bloqueado."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    org_id = _seed_organization(Session, name="OrgKindOrg")
    org_kind_id = _seed_integration(
        Session, org_id=org_id, name="OrgKind", kind="organization"
    )

    r = client.post(
        "/api/integrations/bulk/deactivate",
        json={"ids": [org_kind_id]},
    )
    assert r.status_code == 422, r.text
    error = r.json()["error"]
    assert error["code"] == "integration.partner_bulk_unsupported"
    assert org_kind_id in error["details"]["partner_ids"]


def test_bulk_deactivate_empty_body_returns_422(client_factory) -> None:
    """body vazio → 422 (pydantic validation)."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.post("/api/integrations/bulk/deactivate", json={"ids": []})
    assert r.status_code == 422, r.text


def test_bulk_deactivate_over_limit_returns_422(client_factory) -> None:
    """501 ids → 422 (limite é 500)."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    oversized = list(range(1, 502))
    r = client.post("/api/integrations/bulk/deactivate", json={"ids": oversized})
    assert r.status_code == 422, r.text


def test_bulk_deactivate_dedupes_input(client_factory) -> None:
    """[id1, id2, id1] → processed=2 (após dedup), audit log único por id."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    org_id = _seed_organization(Session, name="DedupOrg")
    a_id = _seed_integration(Session, org_id=org_id, name="A")
    b_id = _seed_integration(Session, org_id=org_id, name="B")

    audit_before = _count_audit_logs(Session, action="bulk_deactivate_integration")
    r = client.post(
        "/api/integrations/bulk/deactivate",
        json={"ids": [a_id, b_id, a_id]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["processed"] == 2, "Deve dedupar para 2 ids"
    assert body["deactivated"] == 2

    audit_after = _count_audit_logs(Session, action="bulk_deactivate_integration")
    assert audit_after - audit_before == 2


def test_bulk_deactivate_requires_integration_write_permission(
    client_factory,
) -> None:
    """Viewer não pode chamar bulk → 403."""
    factory, Session = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)

    org_id = _seed_organization(Session, name="PermOrg")
    int_id = _seed_integration(Session, org_id=org_id, name="ToDeact")

    _create_user(
        admin_client,
        username="viewer_user",
        role="viewer",
        organization_id=org_id,
    )
    user_client = factory()
    _login_as(user_client, username="viewer_user")

    r = user_client.post(
        "/api/integrations/bulk/deactivate", json={"ids": [int_id]}
    )
    assert r.status_code == 403, r.text


def test_bulk_deactivate_partial_progress_when_mix_of_outcomes(
    client_factory,
) -> None:
    """Mistura de [ativo válido, já-inativo, inexistente] retorna estado coerente.

    Garante que erros parciais NÃO interrompem o loop e que o response
    contabiliza cada outcome separadamente. Esse é o caso real mais
    comum em produção (admin desativando lote heterogêneo).
    """
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    org_id = _seed_organization(Session, name="MixedOrg")
    active_id = _seed_integration(Session, org_id=org_id, name="Active")
    inactive_id = _seed_integration(
        Session, org_id=org_id, name="Inactive", is_active=False
    )

    r = client.post(
        "/api/integrations/bulk/deactivate",
        json={"ids": [active_id, inactive_id, 9999]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["processed"] == 3
    assert body["deactivated"] == 1
    errors = {e["id"]: e["reason"] for e in body["errors"]}
    assert errors == {
        inactive_id: "already_inactive",
        9999: "not_found",
    }


# ── Testes: filtros novos no GET / ───────────────────────────────────────


def test_list_integrations_filter_by_name(client_factory) -> None:
    """name=substring case-insensitive filtra por nome."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    org_id = _seed_organization(Session, name="FilterOrg")
    _seed_integration(Session, org_id=org_id, name="Production-Sophos")
    _seed_integration(Session, org_id=org_id, name="Staging-Wazuh", platform="wazuh")
    _seed_integration(Session, org_id=org_id, name="Dev-Misc")

    r = client.get("/api/integrations/?name=PROD")
    assert r.status_code == 200, r.text
    names = [i["name"] for i in r.json()]
    assert names == ["Production-Sophos"], names


def test_list_integrations_filter_by_kind(client_factory) -> None:
    """kind=partner retorna só partners."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    org_id = _seed_organization(Session, name="KindOrg")
    _seed_integration(Session, org_id=org_id, name="Tn-1", kind="tenant")
    _seed_integration(Session, org_id=org_id, name="Pt-1", kind="partner")
    _seed_integration(Session, org_id=org_id, name="Pt-2", kind="partner")

    r = client.get("/api/integrations/?kind=partner")
    assert r.status_code == 200, r.text
    items = r.json()
    assert len(items) == 2
    assert all(i["kind"] == "partner" for i in items)

    r_all = client.get("/api/integrations/?kind=all")
    assert r_all.status_code == 200
    assert len(r_all.json()) == 3


def test_list_integrations_filter_by_status(client_factory) -> None:
    """status=inactive retorna só inativas (admin only)."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    org_id = _seed_organization(Session, name="StatusOrg")
    _seed_integration(Session, org_id=org_id, name="Active-1")
    _seed_integration(
        Session, org_id=org_id, name="Inactive-1", is_active=False
    )

    r = client.get("/api/integrations/?status=inactive")
    assert r.status_code == 200, r.text
    items = r.json()
    assert len(items) == 1
    assert items[0]["name"] == "Inactive-1"
    assert items[0]["is_active"] is False

    r_all = client.get("/api/integrations/?status=all")
    assert r_all.status_code == 200
    assert len(r_all.json()) == 2


def test_list_integrations_filter_by_region(client_factory) -> None:
    """region=substring filtra por região."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    org_id = _seed_organization(Session, name="RegionOrg")
    _seed_integration(Session, org_id=org_id, name="EU-Int", region="eu03")
    _seed_integration(Session, org_id=org_id, name="US-Int", region="us02")

    r = client.get("/api/integrations/?region=eu")
    assert r.status_code == 200, r.text
    items = r.json()
    assert len(items) == 1
    assert items[0]["name"] == "EU-Int"


def test_list_integrations_pagination(client_factory) -> None:
    """page+size respeita o limite e ordena por nome."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    org_id = _seed_organization(Session, name="PageOrg")
    for i in range(5):
        _seed_integration(Session, org_id=org_id, name=f"Int-{i:02d}")

    r1 = client.get("/api/integrations/?page=1&size=2")
    assert r1.status_code == 200, r1.text
    items1 = r1.json()
    assert len(items1) == 2
    assert [i["name"] for i in items1] == ["Int-00", "Int-01"]

    r2 = client.get("/api/integrations/?page=2&size=2")
    items2 = r2.json()
    assert [i["name"] for i in items2] == ["Int-02", "Int-03"]

    r3 = client.get("/api/integrations/?page=3&size=2")
    items3 = r3.json()
    assert [i["name"] for i in items3] == ["Int-04"]


def test_list_integrations_combined_filters(client_factory) -> None:
    """name + kind + status combinam corretamente."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    org_id = _seed_organization(Session, name="ComboOrg")
    _seed_integration(Session, org_id=org_id, name="Sophos-Prod", kind="partner")
    _seed_integration(
        Session, org_id=org_id, name="Sophos-Staging", kind="tenant"
    )
    _seed_integration(
        Session, org_id=org_id, name="Sophos-Old", kind="tenant", is_active=False
    )

    r = client.get(
        "/api/integrations/?name=sophos&kind=tenant&status=active"
    )
    assert r.status_code == 200, r.text
    items = r.json()
    assert len(items) == 1
    assert items[0]["name"] == "Sophos-Staging"


def test_list_integrations_default_excludes_inactive(client_factory) -> None:
    """Sem status param, comportamento legado: só ativas (compat)."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    org_id = _seed_organization(Session, name="DefaultOrg")
    _seed_integration(Session, org_id=org_id, name="On")
    _seed_integration(Session, org_id=org_id, name="Off", is_active=False)

    r = client.get("/api/integrations/")
    assert r.status_code == 200, r.text
    names = [i["name"] for i in r.json()]
    assert names == ["On"], "Default deve excluir inativas (compat com PRs anteriores)"
