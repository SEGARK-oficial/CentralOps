"""Testes para os endpoints bulk do Drift Explorer.

Cobre:
- POST /api/drift/bulk/ignore
- POST /api/drift/bulk/mark_mapped

Casos testados:
- Happy path ignore: 3 ids válidos → updated=3, failed=0, status no DB.
- Happy path mark_mapped: análogo.
- Id inexistente: 2 válidos + 1 inválido → updated=2, failed=1.
- Cross-tenant non-admin: user da org A tenta ids da org B → not_found.
- Permission denied: user sem DRIFT_IGNORE → 403.
- Batch vazio → 422.
- Batch > 500 → 422.
- Dedup: [id1, id2, id1] → 2 itens processados.
- Audit log: uma entrada por sucesso.
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


# ── Fixtures ─────────────────────────────────────────────────────────────────


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


# ── Helpers ───────────────────────────────────────────────────────────────────


def _bootstrap_admin(client: TestClient) -> dict[str, Any]:
    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPassword123!"},
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
        "role": role,
    }
    if organization_id is not None:
        payload["organization_id"] = organization_id
    r = admin_client.post("/api/auth/users", json=payload)
    assert r.status_code == 200, f"Falha ao criar user {username}: {r.text}"
    return r.json()


def _login_as(client: TestClient, *, username: str, password: str = "TestPassword123!") -> None:
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, f"Falha ao logar como {username}: {r.text}"


def _seed_org_and_integration(session, *, platform: str = "sophos") -> tuple[int, int]:
    """Cria organização + integração; retorna (org_id, integration_id)."""
    org = models.Organization(
        name=f"Bulk Drift Org {uuid4().hex[:6]}",
        slug=f"bulk-drift-{uuid4().hex[:6]}",
        is_active=True,
    )
    session.add(org)
    session.flush()
    integration = models.Integration(
        organization_id=org.id,
        name="Bulk Drift Integration",
        platform=platform,
    )
    session.add(integration)
    session.flush()
    session.commit()
    session.refresh(integration)
    return org.id, integration.id


def _seed_unknown_field(
    session,
    *,
    vendor: str = "sophos",
    event_type: str = "sophos.alert",
    path: str | None = None,
) -> str:
    """Persiste um UnknownField e retorna seu id."""
    now = datetime.utcnow()
    uf = models.UnknownField(
        vendor=vendor,
        event_type=event_type,
        field_path=path or f"extra.field.{uuid4().hex[:8]}",
        sample_value="example",
        sample_type="string",
        occurrence_count=1,
        first_seen=now - timedelta(days=1),
        last_seen=now,
        status="new",
    )
    session.add(uf)
    session.commit()
    session.refresh(uf)
    return uf.id


def _count_audit_logs(session, *, action: str) -> int:
    return (
        session.query(models.MappingAuditLog)
        .filter(models.MappingAuditLog.action == action)
        .count()
    )


# ── Testes: bulk/ignore ───────────────────────────────────────────────────────


def test_bulk_ignore_happy_path(client_factory) -> None:
    """3 ids válidos → updated=3, failed=0, status 'ignored' no DB, 3 audit logs."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        fids = [_seed_unknown_field(db, path=f"bulk.ignore.field{i}") for i in range(3)]

    r = client.post("/api/drift/bulk/ignore", json={"field_ids": fids})
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["updated"] == 3
    assert body["failed"] == 0
    assert len(body["items"]) == 3
    assert all(item["success"] is True for item in body["items"])

    with Session() as db:
        for fid in fids:
            uf = db.get(models.UnknownField, fid)
            assert uf is not None
            assert uf.status == "ignored", f"Campo {fid} deve estar 'ignored'"
        audit_count = _count_audit_logs(db, action="ignore_field")
        assert audit_count >= 3, f"Esperava >= 3 audit logs, got {audit_count}"


def test_bulk_mark_mapped_happy_path(client_factory) -> None:
    """3 ids válidos → updated=3, failed=0, status 'mapped' no DB, 3 audit logs."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        fids = [_seed_unknown_field(db, path=f"bulk.mapped.field{i}") for i in range(3)]

    r = client.post("/api/drift/bulk/mark_mapped", json={"field_ids": fids})
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["updated"] == 3
    assert body["failed"] == 0
    assert len(body["items"]) == 3
    assert all(item["success"] is True for item in body["items"])

    with Session() as db:
        for fid in fids:
            uf = db.get(models.UnknownField, fid)
            assert uf is not None
            assert uf.status == "mapped", f"Campo {fid} deve estar 'mapped'"
        audit_count = _count_audit_logs(db, action="mark_mapped")
        assert audit_count >= 3, f"Esperava >= 3 audit logs, got {audit_count}"


# ── Testes: id inexistente ────────────────────────────────────────────────────


def test_bulk_ignore_partial_not_found(client_factory) -> None:
    """2 ids válidos + 1 inexistente → updated=2, failed=1, not_found."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        valid1 = _seed_unknown_field(db, path="bulk.partial.field1")
        valid2 = _seed_unknown_field(db, path="bulk.partial.field2")

    missing_id = str(uuid4())
    r = client.post(
        "/api/drift/bulk/ignore",
        json={"field_ids": [valid1, missing_id, valid2]},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["updated"] == 2
    assert body["failed"] == 1

    items_by_id = {item["id"]: item for item in body["items"]}
    assert items_by_id[valid1]["success"] is True
    assert items_by_id[valid2]["success"] is True
    assert items_by_id[missing_id]["success"] is False
    assert items_by_id[missing_id]["error"] == "not_found"


# ── Testes: isolamento cross-tenant ──────────────────────────────────────────


def test_bulk_ignore_cross_tenant_returns_not_found(client_factory) -> None:
    """Non-admin da org A tentando ids da org B recebe not_found, nunca forbidden.

    Garante que não há enumeração de tenants: mesma resposta de id inexistente.
    """
    factory, Session = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)

    with Session() as db:
        # Org A — usuário operator terá acesso somente a platform "sophos"
        org_a_id, _ = _seed_org_and_integration(db, platform="sophos")
        # Org B — unknown fields com vendor "wazuh" (fora do acesso da org A)
        _, _ = _seed_org_and_integration(db, platform="wazuh")
        # Campos de vendor "wazuh" pertencentes conceptualmente à org B
        field_org_b_1 = _seed_unknown_field(db, vendor="wazuh", path="org_b.field1")
        field_org_b_2 = _seed_unknown_field(db, vendor="wazuh", path="org_b.field2")

    username = f"operator_a_{uuid4().hex[:6]}"
    _create_user(admin_client, username=username, role="operator", organization_id=org_a_id)

    user_client = factory()
    _login_as(user_client, username=username)

    r = user_client.post(
        "/api/drift/bulk/ignore",
        json={"field_ids": [field_org_b_1, field_org_b_2]},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # Todos falham com not_found — não revela que o tenant existe
    assert body["updated"] == 0
    assert body["failed"] == 2
    for item in body["items"]:
        assert item["success"] is False
        assert item["error"] == "not_found", (
            f"Esperava not_found, got '{item['error']}' — não deve revelar tenant info"
        )


# ── Testes: permissão global ──────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["viewer"])
def test_bulk_ignore_requires_drift_ignore_permission(client_factory, role: str) -> None:
    """Roles sem DRIFT_IGNORE devem receber 403."""
    factory, Session = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)

    with Session() as db:
        org_id, _ = _seed_org_and_integration(db, platform="sophos")
        fid = _seed_unknown_field(db, vendor="sophos")

    username = f"no_perm_{role}_{uuid4().hex[:6]}"
    _create_user(admin_client, username=username, role=role, organization_id=org_id)

    user_client = factory()
    _login_as(user_client, username=username)

    r = user_client.post("/api/drift/bulk/ignore", json={"field_ids": [fid]})
    assert r.status_code == 403, f"role={role} deve receber 403, got {r.status_code}: {r.text}"


@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_bulk_mark_mapped_requires_drift_mark_mapped_permission(client_factory, role: str) -> None:
    """Roles sem DRIFT_MARK_MAPPED (viewer, operator) devem receber 403."""
    factory, Session = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)

    with Session() as db:
        org_id, _ = _seed_org_and_integration(db, platform="sophos")
        fid = _seed_unknown_field(db, vendor="sophos")

    username = f"no_mapped_{role}_{uuid4().hex[:6]}"
    _create_user(admin_client, username=username, role=role, organization_id=org_id)

    user_client = factory()
    _login_as(user_client, username=username)

    r = user_client.post("/api/drift/bulk/mark_mapped", json={"field_ids": [fid]})
    assert r.status_code == 403, f"role={role} deve receber 403, got {r.status_code}: {r.text}"


# ── Testes: validação de entrada ──────────────────────────────────────────────


@pytest.mark.parametrize("endpoint", ["/api/drift/bulk/ignore", "/api/drift/bulk/mark_mapped"])
def test_bulk_empty_field_ids_returns_422(client_factory, endpoint: str) -> None:
    """field_ids vazio → 422."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.post(endpoint, json={"field_ids": []})
    assert r.status_code == 422, f"{endpoint}: esperava 422, got {r.status_code}: {r.text}"


@pytest.mark.parametrize("endpoint", ["/api/drift/bulk/ignore", "/api/drift/bulk/mark_mapped"])
def test_bulk_over_limit_returns_422(client_factory, endpoint: str) -> None:
    """field_ids com 501 itens → 422 (limite é 500)."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    oversized = [str(uuid4()) for _ in range(501)]
    r = client.post(endpoint, json={"field_ids": oversized})
    assert r.status_code == 422, f"{endpoint}: esperava 422, got {r.status_code}: {r.text}"
    # Erros são localizados (i18n Fase 4) → asserir no code estável, não no texto.
    assert r.json()["error"]["code"] == "drift.bulk.limit_exceeded"


# ── Testes: deduplicação ──────────────────────────────────────────────────────


def test_bulk_ignore_deduplicates_field_ids(client_factory) -> None:
    """[id1, id2, id1] → processa apenas id1 e id2 (2 itens únicos, não 3).

    Status é aplicado uma única vez — sem duplicate audit logs por id.
    """
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        fid1 = _seed_unknown_field(db, path="dedup.field1")
        fid2 = _seed_unknown_field(db, path="dedup.field2")

    # Envia id1 duas vezes
    r = client.post("/api/drift/bulk/ignore", json={"field_ids": [fid1, fid2, fid1]})
    assert r.status_code == 200, r.text
    body = r.json()

    # Deve retornar 2 itens (após dedup), não 3
    assert len(body["items"]) == 2, f"Esperava 2 itens dedupados, got {len(body['items'])}"
    assert body["updated"] == 2
    assert body["failed"] == 0

    # Os ids processados são exatamente fid1 e fid2
    result_ids = [item["id"] for item in body["items"]]
    assert result_ids == [fid1, fid2], f"Ordem preservada pós-dedup: {result_ids}"


# ── Testes: audit log por item ────────────────────────────────────────────────


def test_bulk_ignore_audit_log_one_entry_per_success(client_factory) -> None:
    """Audit log grava exatamente uma entrada por item com sucesso."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        fids = [_seed_unknown_field(db, path=f"audit.field{i}") for i in range(4)]

    with Session() as db:
        count_before = _count_audit_logs(db, action="ignore_field")

    r = client.post("/api/drift/bulk/ignore", json={"field_ids": fids})
    assert r.status_code == 200, r.text
    assert r.json()["updated"] == 4

    with Session() as db:
        count_after = _count_audit_logs(db, action="ignore_field")

    assert count_after - count_before == 4, (
        f"Esperava 4 novas entradas de audit, got {count_after - count_before}"
    )
