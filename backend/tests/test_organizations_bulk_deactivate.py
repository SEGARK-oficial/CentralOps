"""Testes de PR #5 — bulk deactivate em organizations.

Cobre:
- Happy path: lote misto (algumas ativas, uma já inactive, uma auto_managed,
  uma inexistente) é processado idempotentemente.
- Bloqueio de auto_managed: vai pra ``errors`` com motivo claro.
- Idempotência: já-inactive não conta em ``deactivated`` mas conta em ``processed``.
- Permissão: usuário não-admin recebe 403.
- Cap de 500 IDs respeitado pelo schema.
- Audit logs criados apenas para transições efetivas.

Filtros novos do GET /api/organizations/ também cobertos:
- name (substring case-insensitive em name e slug)
- status (active/inactive/all)
- auto_managed (true/false/all)
- external_provider
- page/size + headers X-Total-Count/X-Page/X-Size
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import database, models
from backend.app.db.database import Base, get_session
from backend.app.main import app


# ── Fixtures ──────────────────────────────────────────────────────────


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


# ── Helpers ───────────────────────────────────────────────────────────


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


def _create_user(
    client: TestClient,
    *,
    username: str,
    role: str,
    organization_id: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "username": username,
        "password": "Senha1234!",
        "display_name": username.title(),
        "role": role,
    }
    if organization_id is not None:
        payload["organization_id"] = organization_id
    r = client.post("/api/auth/users", json=payload)
    assert r.status_code in (200, 201), r.text
    return r.json()


def _login(client: TestClient, username: str, password: str) -> None:
    r = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert r.status_code == 200, f"login falhou: {r.text}"


def _create_org(
    client: TestClient,
    name: str,
    *,
    is_active: bool = True,
) -> dict[str, Any]:
    r = client.post("/api/organizations/", json={"name": name})
    assert r.status_code == 200, r.text
    org = r.json()
    if not is_active:
        # Deactivate via DELETE endpoint? No — DELETE remove. Force via DB.
        r = client.put(
            f"/api/organizations/{org['id']}",
            json={"is_active": False},
        )
        assert r.status_code == 200, r.text
        org = r.json()
    return org


def _seed_auto_managed(
    Session,
    *,
    name: str,
    external_id: str | None = None,
) -> int:
    """Insere uma org auto_managed direto no banco (não há endpoint público)."""
    with Session() as db:
        org = models.Organization(
            name=name,
            slug=name.lower().replace(" ", "-"),
            description="auto",
            is_active=True,
            auto_managed=True,
            external_provider="sophos",
            external_id=external_id or uuid4().hex,
        )
        db.add(org)
        db.commit()
        db.refresh(org)
        return org.id


# ── Bulk deactivate — happy paths e edge cases ────────────────────────


def test_bulk_deactivate_mixed_batch(client_factory) -> None:
    """Lote misto: ativas → desativam, inactive → idempotente, auto_managed →
    erro, inexistente → erro."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)
    _login(client, "admin", "AdminPass123!")

    org_a = _create_org(client, "Org Ativa A")
    org_b = _create_org(client, "Org Ativa B")
    org_inactive = _create_org(client, "Org Inativa", is_active=False)
    auto_id = _seed_auto_managed(Session, name="Sophos Auto Org")

    target_ids = [
        org_a["id"],
        org_b["id"],
        org_inactive["id"],
        auto_id,
        9_999_999,  # inexistente
    ]

    r = client.post(
        "/api/organizations/bulk/deactivate",
        json={"ids": target_ids},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["processed"] == 5
    assert body["deactivated"] == 2  # apenas org_a e org_b
    error_ids = {e["id"] for e in body["errors"]}
    assert auto_id in error_ids
    assert 9_999_999 in error_ids
    auto_err = next(e for e in body["errors"] if e["id"] == auto_id)
    assert "auto_managed" in auto_err["reason"].lower()

    # Confirma side effects no banco.
    with Session() as db:
        assert db.get(models.Organization, org_a["id"]).is_active is False
        assert db.get(models.Organization, org_b["id"]).is_active is False
        # auto_managed continua active=True — não foi tocada.
        assert db.get(models.Organization, auto_id).is_active is True


def test_bulk_deactivate_idempotent_when_already_inactive(client_factory) -> None:
    """Re-rodar o bulk em orgs já desativadas: 0 transições, 0 errors."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)
    _login(client, "admin", "AdminPass123!")

    org = _create_org(client, "Org Repeat")

    first = client.post(
        "/api/organizations/bulk/deactivate",
        json={"ids": [org["id"]]},
    )
    assert first.status_code == 200
    assert first.json()["deactivated"] == 1

    second = client.post(
        "/api/organizations/bulk/deactivate",
        json={"ids": [org["id"]]},
    )
    assert second.status_code == 200
    body = second.json()
    assert body["processed"] == 1
    assert body["deactivated"] == 0
    assert body["errors"] == []


def test_bulk_deactivate_dedupes_repeated_ids(client_factory) -> None:
    """IDs duplicados no payload contam só 1 vez em ``processed``."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    _login(client, "admin", "AdminPass123!")

    org = _create_org(client, "Org Dup")

    r = client.post(
        "/api/organizations/bulk/deactivate",
        json={"ids": [org["id"], org["id"], org["id"]]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["processed"] == 1
    assert body["deactivated"] == 1


def test_bulk_deactivate_audit_log_only_for_transitions(client_factory) -> None:
    """Apenas transições efetivas geram AuditLog; auto_managed/inactive não."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)
    _login(client, "admin", "AdminPass123!")

    org_a = _create_org(client, "Audit A")
    org_inactive = _create_org(client, "Audit Inactive", is_active=False)
    auto_id = _seed_auto_managed(Session, name="Audit Auto")

    r = client.post(
        "/api/organizations/bulk/deactivate",
        json={"ids": [org_a["id"], org_inactive["id"], auto_id]},
    )
    assert r.status_code == 200

    with Session() as db:
        logs = (
            db.query(models.AuditLog)
            .filter(models.AuditLog.action == "bulk_deactivate_organization")
            .all()
        )
        # Só uma transição efetiva (org_a).
        assert len(logs) == 1
        assert str(org_a["id"]) in logs[0].detail


def test_bulk_deactivate_requires_admin(client_factory) -> None:
    """Usuário não-admin recebe 403."""
    factory, _ = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)
    _login(admin_client, "admin", "AdminPass123!")

    org = _create_org(admin_client, "Org Permissao")

    _create_user(admin_client, username="viewer", role="viewer")

    viewer_client = factory()
    _login(viewer_client, "viewer", "Senha1234!")

    r = viewer_client.post(
        "/api/organizations/bulk/deactivate",
        json={"ids": [org["id"]]},
    )
    assert r.status_code == 403, r.text


def test_bulk_deactivate_empty_payload_rejected(client_factory) -> None:
    """ids vazio → 422 (Pydantic min_length=1)."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    _login(client, "admin", "AdminPass123!")

    r = client.post(
        "/api/organizations/bulk/deactivate",
        json={"ids": []},
    )
    assert r.status_code == 422


def test_bulk_deactivate_over_cap_rejected(client_factory) -> None:
    """501 IDs → 422 (Pydantic max_length=500)."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    _login(client, "admin", "AdminPass123!")

    r = client.post(
        "/api/organizations/bulk/deactivate",
        json={"ids": list(range(1, 502))},
    )
    assert r.status_code == 422


# ── GET /organizations/ filtros novos ─────────────────────────────────


def test_list_filter_by_name_substring(client_factory) -> None:
    """Filtro `name` faz substring case-insensitive em name e slug."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    _login(client, "admin", "AdminPass123!")

    _create_org(client, "Acme Corp")
    _create_org(client, "Globex")
    _create_org(client, "Initech")

    r = client.get("/api/organizations/?name=acme")
    assert r.status_code == 200
    names = [o["name"] for o in r.json()]
    assert names == ["Acme Corp"]

    # Search também casa em slug.
    r = client.get("/api/organizations/?name=glo")
    assert r.status_code == 200
    assert {o["name"] for o in r.json()} == {"Globex"}


def test_list_status_filter(client_factory) -> None:
    """status=active|inactive|all controla visibilidade."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    _login(client, "admin", "AdminPass123!")

    _create_org(client, "Org Live")
    _create_org(client, "Org Dead", is_active=False)

    active_only = client.get("/api/organizations/?status=active").json()
    assert {o["name"] for o in active_only} == {"Org Live"}

    inactive_only = client.get("/api/organizations/?status=inactive").json()
    assert {o["name"] for o in inactive_only} == {"Org Dead"}

    all_ = client.get("/api/organizations/?status=all").json()
    assert {o["name"] for o in all_} == {"Org Live", "Org Dead"}


def test_list_auto_managed_filter(client_factory) -> None:
    """auto_managed=true|false|all filtra por origem."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)
    _login(client, "admin", "AdminPass123!")

    _create_org(client, "Manual One")
    _seed_auto_managed(Session, name="Auto One")

    only_manual = client.get("/api/organizations/?auto_managed=false").json()
    assert {o["name"] for o in only_manual} == {"Manual One"}

    only_auto = client.get("/api/organizations/?auto_managed=true").json()
    assert {o["name"] for o in only_auto} == {"Auto One"}

    all_ = client.get("/api/organizations/?auto_managed=all").json()
    assert {o["name"] for o in all_} == {"Manual One", "Auto One"}


def test_list_invalid_auto_managed_value_returns_422(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    _login(client, "admin", "AdminPass123!")

    r = client.get("/api/organizations/?auto_managed=maybe")
    assert r.status_code == 422


def test_list_external_provider_filter(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)
    _login(client, "admin", "AdminPass123!")

    _create_org(client, "Manual NoProvider")
    _seed_auto_managed(Session, name="Sophos Provider Org")

    sophos = client.get("/api/organizations/?external_provider=sophos").json()
    assert {o["name"] for o in sophos} == {"Sophos Provider Org"}


def test_list_pagination_headers(client_factory) -> None:
    """page/size paginam e expõem total via header."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    _login(client, "admin", "AdminPass123!")

    for i in range(7):
        _create_org(client, f"Org {i:02d}")

    r = client.get("/api/organizations/?page=1&size=3")
    assert r.status_code == 200
    assert len(r.json()) == 3
    assert r.headers["X-Total-Count"] == "7"
    assert r.headers["X-Page"] == "1"
    assert r.headers["X-Size"] == "3"

    r = client.get("/api/organizations/?page=3&size=3")
    assert r.status_code == 200
    assert len(r.json()) == 1  # 7 = 3+3+1
    assert r.headers["X-Total-Count"] == "7"


def test_list_legacy_call_still_works(client_factory) -> None:
    """Chamadas antigas sem parâmetros mantém comportamento — só ativas."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    _login(client, "admin", "AdminPass123!")

    _create_org(client, "Compat Active")
    _create_org(client, "Compat Inactive", is_active=False)

    r = client.get("/api/organizations/")
    assert r.status_code == 200
    names = {o["name"] for o in r.json()}
    assert names == {"Compat Active"}
