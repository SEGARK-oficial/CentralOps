"""Testes do router REST do Collector (``/api/collectors/*``).

Cobertura:

- ``GET /api/collectors/vendors`` exige auth e devolve os built-ins do registry.
- ``GET /api/collectors/state`` aplica scope multi-tenant (admin vs user).
- ``GET /api/collectors/summary`` agrega KPIs corretos.
- ``POST /api/collectors/state/{id}/{stream}/trigger`` valida integração e autorização.
- ``DELETE /api/collectors/state/{id}/{stream}/cursor`` é admin-only.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import models
from backend.app.db.database import Base, get_session
from backend.app.main import app


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
        client = TestClient(app)
        clients.append(client)
        return client

    yield factory, TestingSessionLocal

    for client in clients:
        client.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


def _bootstrap_admin(client: TestClient) -> dict[str, Any]:
    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPassword123!", "display_name": "Admin"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _login(client: TestClient, username: str, password: str) -> None:
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text


def _create_org(client: TestClient, name: str) -> dict[str, Any]:
    r = client.post("/api/organizations/", json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()


def _create_user(
    client: TestClient, *, username: str, password: str, organization_id: int
) -> dict[str, Any]:
    r = client.post(
        "/api/auth/users",
        json={
            "username": username,
            "password": password,
            "display_name": username.title(),
            "role": "user",
            "organization_id": organization_id,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _seed_integration(session, *, organization_id: int, name: str, platform: str) -> int:
    integ = models.Integration(
        organization_id=organization_id,
        name=name,
        platform=platform,
        is_active=True,
        client_id="seed-cid",
    )
    session.add(integ)
    session.commit()
    session.refresh(integ)
    return integ.id


def _seed_collection_state(
    session,
    *,
    integration_id: int,
    stream: str,
    events: int = 10,
    failures: int = 0,
    last_success_minutes_ago: int | None = 2,
    last_error: str | None = None,
) -> None:
    now = datetime.utcnow()
    row = models.CollectionState(
        integration_id=integration_id,
        stream=stream,
        cursor='{"from_ts":"2026-04-23T00:00:00Z"}',
        last_success_at=(
            now - timedelta(minutes=last_success_minutes_ago)
            if last_success_minutes_ago is not None
            else None
        ),
        last_attempt_at=now,
        last_error=last_error,
        consecutive_failures=failures,
        events_collected_total=events,
    )
    session.add(row)
    session.commit()


# ── Testes ────────────────────────────────────────────────────────────


def test_vendors_requires_auth(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    assert client.get("/api/collectors/vendors").status_code == 401


def test_vendors_lists_builtins_when_authenticated(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.get("/api/collectors/vendors")
    assert r.status_code == 200, r.text
    platforms = {v["platform"] for v in r.json()}
    # Built-ins registrados via ``vendors/__init__.py``.
    assert {"sophos", "microsoft_defender", "ninjaone"}.issubset(platforms)


def test_platforms_streams_requires_auth(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    assert client.get("/api/collectors/platforms-streams").status_code == 401


def test_platforms_streams_returns_aggregated_map(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.get("/api/collectors/platforms-streams")
    assert r.status_code == 200, r.text
    data = r.json()
    assert "platforms" in data
    platforms = data["platforms"]
    # Sophos tem 3 streams: alerts, cases, detections.
    assert "sophos" in platforms
    assert set(platforms["sophos"]) >= {"alerts", "cases", "detections"}
    # Streams ordenados alfabeticamente para UX consistente.
    assert platforms["sophos"] == sorted(platforms["sophos"])
    # Defender tem alerts + incidents.
    assert "microsoft_defender" in platforms
    assert set(platforms["microsoft_defender"]) >= {"alerts", "incidents"}
    # NinjaOne tem activities.
    assert "ninjaone" in platforms
    assert "activities" in platforms["ninjaone"]


def test_state_is_scoped_by_tenant(client_factory) -> None:
    factory, SessionLocal = client_factory

    admin_client = factory()
    _bootstrap_admin(admin_client)
    org_a = _create_org(admin_client, "Org A")
    org_b = _create_org(admin_client, "Org B")
    _create_user(
        admin_client,
        username="user_a",
        password="UserPass123!",
        organization_id=org_a["id"],
    )

    with SessionLocal() as s:
        integ_a = _seed_integration(
            s, organization_id=org_a["id"], name="A-sophos", platform="sophos"
        )
        integ_b = _seed_integration(
            s, organization_id=org_b["id"], name="B-sophos", platform="sophos"
        )
        _seed_collection_state(s, integration_id=integ_a, stream="alerts")
        _seed_collection_state(s, integration_id=integ_b, stream="alerts")

    # Admin vê ambas
    r = admin_client.get("/api/collectors/state")
    assert r.status_code == 200, r.text
    assert len(r.json()) == 2

    # User-A vê apenas a da própria organização
    user_client = factory()
    _login(user_client, "user_a", "UserPass123!")
    r = user_client.get("/api/collectors/state")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == 1
    assert body[0]["organization_id"] == org_a["id"]


def test_state_hides_inactive_integration_by_default(client_factory) -> None:
    """Regressão: Integration ``is_active=false`` não deve aparecer em
    ``/api/collectors/state``. Antes do fix, o ``last_success_at`` ficava
    congelado e poluía o KPI ``stale_minutes_max`` com integrações zumbis.
    """
    factory, SessionLocal = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)
    org = _create_org(admin_client, "Org A")

    with SessionLocal() as s:
        active = _seed_integration(s, organization_id=org["id"], name="active", platform="sophos")
        inactive = _seed_integration(s, organization_id=org["id"], name="inactive", platform="sophos")
        _seed_collection_state(s, integration_id=active, stream="alerts")
        _seed_collection_state(s, integration_id=inactive, stream="alerts", last_success_minutes_ago=999)
        # Marca a 2ª como desativada
        s.query(models.Integration).filter(models.Integration.id == inactive).update(
            {"is_active": False}
        )
        s.commit()

    r = admin_client.get("/api/collectors/state")
    assert r.status_code == 200, r.text
    rows = r.json()
    integration_ids = {row["integration_id"] for row in rows}
    assert active in integration_ids
    assert inactive not in integration_ids, "Integration inativa não deveria aparecer"

    # ?include_inactive=true ressuscita pra debug
    r2 = admin_client.get("/api/collectors/state?include_inactive=true")
    rows2 = r2.json()
    assert {row["integration_id"] for row in rows2} == {active, inactive}


def test_state_hides_org_inactive_cascade(client_factory) -> None:
    """Cascade do Partner soft-delete (Org ``is_active=false``) também
    esconde os ``CollectionState`` dos children — caso contrário o ``Lag
    máximo`` fica preso em integrações abandonadas via cascade.
    """
    factory, SessionLocal = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)
    org_a = _create_org(admin_client, "Active Org")
    org_b = _create_org(admin_client, "Cascaded Org")

    with SessionLocal() as s:
        ia = _seed_integration(s, organization_id=org_a["id"], name="ia", platform="sophos")
        ib = _seed_integration(s, organization_id=org_b["id"], name="ib", platform="sophos")
        _seed_collection_state(s, integration_id=ia, stream="alerts")
        _seed_collection_state(s, integration_id=ib, stream="alerts", last_success_minutes_ago=600)
        # Cascade — só desativa Org, mantém Integration is_active=True (típico
        # do Partner soft-delete que zera org mas children podem ficar live).
        s.query(models.Organization).filter(models.Organization.id == org_b["id"]).update(
            {"is_active": False}
        )
        s.commit()

    r = admin_client.get("/api/collectors/state")
    rows = r.json()
    integration_ids = {row["integration_id"] for row in rows}
    assert ia in integration_ids
    assert ib not in integration_ids, "Children de Org desativada não devem aparecer"


def test_summary_excludes_inactive_from_lag_kpi(client_factory) -> None:
    """``stale_minutes_max`` deve ignorar Integrations/Orgs desativadas.

    Antes do fix: 1 integração ativa OK + 1 desativada com last_success há 12h
    → ``stale_minutes_max=720`` (kpi alarmado por algo zumbi).
    Depois do fix: ``stale_minutes_max ≈ 2`` (só conta a ativa).
    """
    factory, SessionLocal = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)
    org = _create_org(admin_client, "Lag Org")

    with SessionLocal() as s:
        active = _seed_integration(s, organization_id=org["id"], name="active", platform="sophos")
        zombie = _seed_integration(s, organization_id=org["id"], name="zombie", platform="sophos")
        _seed_collection_state(s, integration_id=active, stream="alerts", last_success_minutes_ago=2)
        _seed_collection_state(s, integration_id=zombie, stream="alerts", last_success_minutes_ago=720)
        s.query(models.Integration).filter(models.Integration.id == zombie).update(
            {"is_active": False}
        )
        s.commit()

    r = admin_client.get("/api/collectors/summary")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["integrations_tracked"] == 1, "Só a integração ativa deve contar"
    # Lag máximo deve ser ~2 min (da ativa), não 720 (da zombie).
    assert data["stale_minutes_max"] is not None
    assert data["stale_minutes_max"] < 60, (
        f"stale_minutes_max deveria refletir só a ativa (~2 min), "
        f"got {data['stale_minutes_max']}"
    )


def test_summary_counts_errors_and_events(client_factory) -> None:
    factory, SessionLocal = client_factory

    admin_client = factory()
    _bootstrap_admin(admin_client)
    org = _create_org(admin_client, "Summary Org")

    with SessionLocal() as s:
        a = _seed_integration(
            s, organization_id=org["id"], name="sx", platform="sophos"
        )
        b = _seed_integration(
            s, organization_id=org["id"], name="dx", platform="microsoft_defender"
        )
        _seed_collection_state(s, integration_id=a, stream="alerts", events=1000)
        _seed_collection_state(
            s,
            integration_id=b,
            stream="incidents",
            events=50,
            failures=3,
            last_error="auth refused",
        )

    r = admin_client.get("/api/collectors/summary")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["integrations_tracked"] == 2
    assert data["events_collected_total"] == 1050
    assert data["integrations_with_errors"] == 1
    assert data["vendors_registered"] >= 3  # builtins
    platforms = {b["platform"] for b in data["per_platform"]}
    assert platforms == {"sophos", "microsoft_defender"}


def test_trigger_404_for_unknown_integration(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.post("/api/collectors/state/9999/alerts/trigger")
    assert r.status_code == 404


def test_reset_cursor_admin_only(client_factory) -> None:
    factory, SessionLocal = client_factory

    admin_client = factory()
    _bootstrap_admin(admin_client)
    org = _create_org(admin_client, "Reset Org")
    _create_user(
        admin_client,
        username="basic_user",
        password="BasicPass123!",
        organization_id=org["id"],
    )

    with SessionLocal() as s:
        integ_id = _seed_integration(
            s, organization_id=org["id"], name="x", platform="sophos"
        )
        _seed_collection_state(s, integration_id=integ_id, stream="alerts")

    # Admin: 204
    r = admin_client.delete(
        f"/api/collectors/state/{integ_id}/alerts/cursor"
    )
    assert r.status_code == 204

    # Re-insere e tenta como user comum → 403
    with SessionLocal() as s:
        _seed_collection_state(s, integration_id=integ_id, stream="alerts")

    user_client = factory()
    _login(user_client, "basic_user", "BasicPass123!")
    r = user_client.delete(f"/api/collectors/state/{integ_id}/alerts/cursor")
    assert r.status_code == 403
