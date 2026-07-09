"""Testes do router bulk de quarantine (PR #3 — frente bulk ops).

Cobertura:

- POST /bulk/discard
  - happy path: lista válida → discard + audit por id.
  - idempotente: ID inexistente vai pra errors, sem 5xx.
  - dedupe: IDs repetidos no body são processados uma vez.
  - validation: lista vazia → 422; >500 IDs → 422.
  - multi-tenant: non-admin não vê IDs de outra org (vão pra errors).
- POST /bulk/reprocess
  - happy path: enfileira N tasks Celery (apply_async mocked) → 202.
  - early-reject: expirado, já reprocessado, não-encontrado.
  - audit log único da operação bulk.
- GET /bulk/ids
  - retorna IDs casados pelos filtros, ordenados por created_at desc.
  - aplica cap default = 2000 (e respeita ``capped`` flag).
  - filtro status pending/reprocessed/all funcional.
  - filtro integration_name (substring case-insensitive).
- GET / (filtros novos)
  - integration_name + status filtram corretamente.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import patch
from uuid import uuid4

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
def client_factory():
    """SQLite in-memory + dependency override pra get_session."""
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
            "password": "AdminPassword123!",
            "display_name": "Admin",
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _login(client: TestClient, username: str, password: str) -> None:
    r = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert r.status_code == 200, f"login falhou: {r.text}"


def _create_org(session, *, name: str = "org-a") -> int:
    org = models.Organization(name=name, slug=name)
    session.add(org)
    session.commit()
    session.refresh(org)
    return org.id


def _create_integration(
    session,
    *,
    org_id: int,
    name: str = "sophos-integration",
    platform: str = "sophos",
) -> int:
    integration = models.Integration(
        organization_id=org_id, name=name, platform=platform
    )
    session.add(integration)
    session.commit()
    session.refresh(integration)
    return integration.id


def _seed_event(
    session,
    *,
    integration_id: int | None = None,
    vendor: str = "sophos",
    event_type: str | None = "sophos.alert",
    error_kind: str = "map",
    expires_delta: timedelta = timedelta(days=7),
    reprocessed_at: datetime | None = None,
    raw: dict | None = None,
) -> str:
    now = datetime.utcnow()
    ev = models.QuarantineEvent(
        integration_id=integration_id,
        vendor=vendor,
        event_type=event_type,
        raw_payload=json.dumps(raw or {"id": "alert-x"}),
        error_kind=error_kind,
        error_detail="seed",
        expires_at=now + expires_delta,
        reprocessed_at=reprocessed_at,
    )
    session.add(ev)
    session.commit()
    session.refresh(ev)
    return ev.id


def _create_user(
    client: TestClient,
    *,
    username: str,
    role: str = "operator",
    password: str = "Password123!",
) -> dict[str, Any]:
    r = client.post(
        "/api/auth/users",
        json={
            "username": username,
            "password": password,
            "display_name": username,
            "role": role,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


# ── POST /bulk/discard ────────────────────────────────────────────────


def test_bulk_discard_happy_path(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        ids = [_seed_event(db) for _ in range(3)]

    r = client.post("/api/quarantine/bulk/discard", json={"ids": ids})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["processed"] == 3
    assert body["discarded"] == 3
    assert body["errors"] == []

    # Audit gravado para cada id (3 entries).
    with Session() as db:
        audits = (
            db.query(models.MappingAuditLog)
            .filter_by(action="discard_quarantine")
            .all()
        )
        assert len(audits) == 3
        for a in audits:
            assert json.loads(a.detail).get("bulk") is True


def test_bulk_discard_idempotent_with_unknown_ids(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        eid = _seed_event(db)

    fake_id = str(uuid4())
    r = client.post(
        "/api/quarantine/bulk/discard",
        json={"ids": [eid, fake_id]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["processed"] == 2
    assert body["discarded"] == 1
    assert len(body["errors"]) == 1
    assert body["errors"][0]["id"] == fake_id
    assert body["errors"][0]["reason"] == "not_found"


def test_bulk_discard_dedupes_repeated_ids(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        eid = _seed_event(db)

    r = client.post(
        "/api/quarantine/bulk/discard",
        json={"ids": [eid, eid, eid]},
    )
    assert r.status_code == 200
    body = r.json()
    # Dedupe → processed conta IDs únicos.
    assert body["processed"] == 1
    assert body["discarded"] == 1
    assert body["errors"] == []


def test_bulk_discard_rejects_empty_list(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.post("/api/quarantine/bulk/discard", json={"ids": []})
    assert r.status_code == 422


def test_bulk_discard_rejects_oversized_batch(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.post(
        "/api/quarantine/bulk/discard",
        json={"ids": [str(uuid4()) for _ in range(501)]},
    )
    assert r.status_code == 422


def test_bulk_discard_multitenant_filters_other_orgs(client_factory) -> None:
    factory, Session = client_factory
    admin = factory()
    _bootstrap_admin(admin)

    with Session() as db:
        org_a = _create_org(db, name="org-a")
        org_b = _create_org(db, name="org-b")
        int_a = _create_integration(db, org_id=org_a, name="int-a")
        int_b = _create_integration(db, org_id=org_b, name="int-b")
        eid_a = _seed_event(db, integration_id=int_a)
        eid_b = _seed_event(db, integration_id=int_b)

    _create_user(admin, username="op-b", role="operator")
    with Session() as db:
        ub = db.query(models.AppUser).filter_by(username="op-b").first()
        assert ub is not None
        ub.organization_id = org_b
        db.commit()

    op = factory()
    _login(op, "op-b", "Password123!")

    r = op.post(
        "/api/quarantine/bulk/discard",
        json={"ids": [eid_a, eid_b]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["processed"] == 2
    assert body["discarded"] == 1  # só o do org-b
    assert len(body["errors"]) == 1
    assert body["errors"][0]["id"] == eid_a
    assert body["errors"][0]["reason"] == "not_found"


def test_bulk_discard_requires_permission(client_factory) -> None:
    """Viewer não tem QUARANTINE_DISCARD → 403."""
    factory, Session = client_factory
    admin = factory()
    _bootstrap_admin(admin)

    _create_user(admin, username="viewer-x", role="viewer")
    viewer = factory()
    _login(viewer, "viewer-x", "Password123!")

    r = viewer.post(
        "/api/quarantine/bulk/discard",
        json={"ids": [str(uuid4())]},
    )
    assert r.status_code == 403


# ── POST /bulk/reprocess ──────────────────────────────────────────────


def test_bulk_reprocess_enqueues_eligible_events(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org = _create_org(db, name="org-x")
        intg = _create_integration(db, org_id=org)
        ids_ok = [_seed_event(db, integration_id=intg) for _ in range(3)]

    with patch(
        "backend.app.collectors.tasks.reprocess_quarantine_event.apply_async"
    ) as mocked:
        r = client.post(
            "/api/quarantine/bulk/reprocess", json={"ids": ids_ok}
        )

    assert r.status_code == 202, r.text
    body = r.json()
    assert body["accepted"] == 3
    assert body["expired"] == 0
    assert body["already_reprocessed"] == 0
    assert body["errors"] == []
    assert mocked.call_count == 3
    # Cada call deve ter event_id e queue=maintenance
    for call in mocked.call_args_list:
        kwargs = call.kwargs
        assert kwargs["queue"] == "maintenance"
        assert "event_id" in kwargs["kwargs"]


def test_bulk_reprocess_buckets_expired_and_already(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org = _create_org(db, name="org-y")
        intg = _create_integration(db, org_id=org)
        ok_id = _seed_event(db, integration_id=intg)
        expired_id = _seed_event(
            db, integration_id=intg, expires_delta=timedelta(days=-1)
        )
        already_id = _seed_event(
            db,
            integration_id=intg,
            reprocessed_at=datetime.utcnow() - timedelta(hours=1),
        )

    with patch(
        "backend.app.collectors.tasks.reprocess_quarantine_event.apply_async"
    ) as mocked:
        r = client.post(
            "/api/quarantine/bulk/reprocess",
            json={"ids": [ok_id, expired_id, already_id]},
        )

    assert r.status_code == 202
    body = r.json()
    assert body["accepted"] == 1
    assert body["expired"] == 1
    assert body["already_reprocessed"] == 1
    assert body["errors"] == []
    assert mocked.call_count == 1


def test_bulk_reprocess_unknown_ids_go_to_errors(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    fake = str(uuid4())
    with patch(
        "backend.app.collectors.tasks.reprocess_quarantine_event.apply_async"
    ) as mocked:
        r = client.post("/api/quarantine/bulk/reprocess", json={"ids": [fake]})

    assert r.status_code == 202
    body = r.json()
    assert body["accepted"] == 0
    assert len(body["errors"]) == 1
    assert body["errors"][0]["id"] == fake
    assert mocked.call_count == 0


def test_bulk_reprocess_audits_operation_once(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org = _create_org(db, name="org-z")
        intg = _create_integration(db, org_id=org)
        ids = [_seed_event(db, integration_id=intg) for _ in range(2)]

    with patch(
        "backend.app.collectors.tasks.reprocess_quarantine_event.apply_async"
    ):
        r = client.post("/api/quarantine/bulk/reprocess", json={"ids": ids})
    assert r.status_code == 202

    with Session() as db:
        audits = (
            db.query(models.MappingAuditLog)
            .filter_by(action="bulk_reprocess_quarantine")
            .all()
        )
        # Apenas UMA entry, não uma por id (decisão consciente).
        assert len(audits) == 1
        detail = json.loads(audits[0].detail)
        assert detail["requested"] == 2
        assert detail["accepted"] == 2


def test_bulk_reprocess_requires_permission(client_factory) -> None:
    factory, _ = client_factory
    admin = factory()
    _bootstrap_admin(admin)

    _create_user(admin, username="viewer-y", role="viewer")
    viewer = factory()
    _login(viewer, "viewer-y", "Password123!")

    r = viewer.post(
        "/api/quarantine/bulk/reprocess", json={"ids": [str(uuid4())]}
    )
    assert r.status_code == 403


# ── GET /bulk/ids ─────────────────────────────────────────────────────


def test_bulk_ids_returns_filtered_pending_ids(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org = _create_org(db, name="org-q")
        intg = _create_integration(db, org_id=org)
        # 2 pending + 1 reprocessed
        p1 = _seed_event(db, integration_id=intg)
        p2 = _seed_event(db, integration_id=intg)
        _seed_event(
            db,
            integration_id=intg,
            reprocessed_at=datetime.utcnow(),
        )

    r = client.get("/api/quarantine/bulk/ids")
    assert r.status_code == 200, r.text
    body = r.json()
    # default status=pending → só os 2
    assert body["total"] == 2
    assert set(body["ids"]) == {p1, p2}
    assert body["capped"] is False


def test_bulk_ids_status_all_includes_reprocessed(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org = _create_org(db, name="org-a")
        intg = _create_integration(db, org_id=org)
        _seed_event(db, integration_id=intg)
        _seed_event(
            db,
            integration_id=intg,
            reprocessed_at=datetime.utcnow(),
        )

    r = client.get("/api/quarantine/bulk/ids?status=all")
    assert r.status_code == 200
    assert r.json()["total"] == 2


def test_bulk_ids_filters_by_integration_name_substring(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org = _create_org(db, name="org-1")
        sophos = _create_integration(
            db, org_id=org, name="Sophos-Prod-EU", platform="sophos"
        )
        ninja = _create_integration(
            db, org_id=org, name="NinjaOne-LAB", platform="ninjaone"
        )
        _seed_event(db, integration_id=sophos)
        _seed_event(db, integration_id=sophos)
        _seed_event(db, integration_id=ninja)

    # Substring case-insensitive
    r = client.get("/api/quarantine/bulk/ids?integration_name=sophos")
    assert r.status_code == 200
    assert r.json()["total"] == 2

    r = client.get("/api/quarantine/bulk/ids?integration_name=NINJA")
    assert r.status_code == 200
    assert r.json()["total"] == 1


def test_bulk_ids_respects_max_cap(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org = _create_org(db, name="org-cap")
        intg = _create_integration(db, org_id=org)
        for _ in range(5):
            _seed_event(db, integration_id=intg)

    r = client.get("/api/quarantine/bulk/ids?max=3")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 5
    assert len(body["ids"]) == 3
    assert body["capped"] is True


def test_bulk_ids_rejects_max_above_2000(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.get("/api/quarantine/bulk/ids?max=2001")
    assert r.status_code == 422


# ── GET / com filtros novos ───────────────────────────────────────────


def test_list_filters_by_status_pending_default(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org = _create_org(db, name="org-listf")
        intg = _create_integration(db, org_id=org)
        _seed_event(db, integration_id=intg)
        _seed_event(
            db, integration_id=intg, reprocessed_at=datetime.utcnow()
        )

    # Sem status → default pending
    r = client.get("/api/quarantine/")
    assert r.status_code == 200
    assert r.json()["total"] == 1


def test_list_filter_integration_name(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org = _create_org(db, name="org-iname")
        sophos = _create_integration(
            db, org_id=org, name="Sophos-X", platform="sophos"
        )
        ninja = _create_integration(
            db, org_id=org, name="Ninja-Y", platform="ninjaone"
        )
        _seed_event(db, integration_id=sophos)
        _seed_event(db, integration_id=ninja)

    r = client.get("/api/quarantine/?integration_name=ninja")
    assert r.status_code == 200
    assert r.json()["total"] == 1


def test_list_status_invalid_value_returns_422(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.get("/api/quarantine/?status=foobar")
    assert r.status_code == 422
