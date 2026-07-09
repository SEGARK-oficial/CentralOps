"""Testes do endpoint GET /api/mappings/{definition_id}/discover-fields.

Cobre:
- Mapping existente com drift fields → retorna ordenado por occurrences DESC.
- Mapping existente sem drift → retorna fields: [].
- Mapping inexistente → 404.
- Sample values: deduplicados e truncados em 5 por field_path.
- Sem autenticação → 401.
- Cache-Control header presente.
- Isolamento multi-tenant: non-admin só vê vendor da própria organização.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import models
from backend.app.db.database import Base, get_session
from backend.app.main import app


# ── Fixture ───────────────────────────────────────────────────────────


@pytest.fixture()
def client_factory() -> Generator[Any, None, None]:
    """SQLite em memória + override get_session."""
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

    yield factory, TestingSession

    for c in clients:
        c.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


# ── Helpers ───────────────────────────────────────────────────────────


def _bootstrap_admin(client: TestClient) -> None:
    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPassword123!", "display_name": "Admin"},
    )
    assert r.status_code == 200, r.text


def _create_mapping(session, vendor: str, event_type: str) -> models.MappingDefinition:
    defn = models.MappingDefinition(
        vendor=vendor,
        event_type=event_type,
        ocsf_class_uid=2004,
        description="test",
    )
    session.add(defn)
    session.commit()
    session.refresh(defn)
    return defn


def _create_unknown_field(
    session,
    vendor: str,
    event_type: str,
    field_path: str,
    occurrence_count: int = 1,
    sample_value: str | None = None,
    first_seen: datetime | None = None,
) -> models.UnknownField:
    uf = models.UnknownField(
        vendor=vendor,
        event_type=event_type,
        field_path=field_path,
        occurrence_count=occurrence_count,
        sample_value=sample_value,
        first_seen=first_seen or datetime(2026, 4, 20, 10, 0, 0),
        last_seen=datetime(2026, 4, 27, 0, 0, 0),
        status="new",
    )
    session.add(uf)
    session.commit()
    session.refresh(uf)
    return uf


# ── Testes ────────────────────────────────────────────────────────────


def test_discover_fields_requires_auth(client_factory: Any) -> None:
    """Sem cookie de sessão deve retornar 401."""
    factory, TestingSession = client_factory
    with TestingSession() as db:
        defn = _create_mapping(db, "sophos", "sophos.alert")
        mapping_id = defn.id

    client = TestClient(app)  # sem login
    r = client.get(f"/api/mappings/{mapping_id}/discover-fields")
    assert r.status_code == 401


def test_discover_fields_not_found(client_factory: Any) -> None:
    """Mapping inexistente deve retornar 404."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.get("/api/mappings/00000000-0000-0000-0000-000000000000/discover-fields")
    assert r.status_code == 404


def test_discover_fields_empty_when_no_drift(client_factory: Any) -> None:
    """Mapping sem drift → fields: []."""
    factory, TestingSession = client_factory
    client = factory()
    _bootstrap_admin(client)

    with TestingSession() as db:
        defn = _create_mapping(db, "sophos", "sophos.alert")
        mapping_id = defn.id

    r = client.get(f"/api/mappings/{mapping_id}/discover-fields")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["fields"] == []


def test_discover_fields_returns_ordered_by_occurrences(client_factory: Any) -> None:
    """5 campos → retornados em ordem decrescente de occurrences."""
    factory, TestingSession = client_factory
    client = factory()
    _bootstrap_admin(client)

    with TestingSession() as db:
        defn = _create_mapping(db, "sophos", "sophos.alert")
        mapping_id = defn.id

        field_data = [
            ("data.severity", 1234, "critical"),
            ("event.action", 890, "denied"),
            ("data.id", 500, "abc123"),
            ("host.name", 200, "server01"),
            ("data.minor", 10, None),
        ]
        for path, count, sample in field_data:
            _create_unknown_field(db, "sophos", "sophos.alert", path, count, sample)

    r = client.get(f"/api/mappings/{mapping_id}/discover-fields")
    assert r.status_code == 200, r.text
    data = r.json()
    fields = data["fields"]

    assert len(fields) == 5
    # Ordem: occurrence_count decrescente
    occurrences = [f["occurrences"] for f in fields]
    assert occurrences == sorted(occurrences, reverse=True)
    assert fields[0]["path"] == "data.severity"
    assert fields[0]["occurrences"] == 1234
    assert fields[1]["path"] == "event.action"


def test_discover_fields_sample_values_present_when_exists(client_factory: Any) -> None:
    """Campo com sample_value preenchido → sample_values: [value]."""
    factory, TestingSession = client_factory
    client = factory()
    _bootstrap_admin(client)

    with TestingSession() as db:
        defn = _create_mapping(db, "sophos", "sophos.alert")
        mapping_id = defn.id
        # UnknownField: unique(vendor, event_type, field_path) — uma row por campo.
        # O drift detector guarda o primeiro sample_value observado.
        _create_unknown_field(
            db, "sophos", "sophos.alert", "data.severity",
            occurrence_count=500, sample_value="critical",
        )

    r = client.get(f"/api/mappings/{mapping_id}/discover-fields")
    assert r.status_code == 200, r.text
    fields = r.json()["fields"]

    assert len(fields) == 1
    field = fields[0]
    assert field["path"] == "data.severity"
    assert field["occurrences"] == 500
    assert field["sample_values"] == ["critical"]


def test_discover_fields_sample_values_empty_when_null(client_factory: Any) -> None:
    """Campo sem sample_value → sample_values: []."""
    factory, TestingSession = client_factory
    client = factory()
    _bootstrap_admin(client)

    with TestingSession() as db:
        defn = _create_mapping(db, "sophos", "sophos.alert")
        mapping_id = defn.id
        _create_unknown_field(
            db, "sophos", "sophos.alert", "data.severity",
            occurrence_count=100, sample_value=None,
        )

    r = client.get(f"/api/mappings/{mapping_id}/discover-fields")
    assert r.status_code == 200, r.text
    fields = r.json()["fields"]
    assert len(fields) == 1
    assert fields[0]["sample_values"] == []


def test_discover_fields_first_seen_at_correct(client_factory: Any) -> None:
    """first_seen_at deve refletir o first_seen da row."""
    factory, TestingSession = client_factory
    client = factory()
    _bootstrap_admin(client)

    with TestingSession() as db:
        defn = _create_mapping(db, "sophos", "sophos.alert")
        mapping_id = defn.id
        _create_unknown_field(
            db, "sophos", "sophos.alert", "event.action",
            occurrence_count=300, sample_value="denied",
            first_seen=datetime(2026, 4, 20, 10, 0, 0),
        )

    r = client.get(f"/api/mappings/{mapping_id}/discover-fields")
    assert r.status_code == 200, r.text
    fields = r.json()["fields"]
    assert len(fields) == 1
    assert "2026-04-20" in fields[0]["first_seen_at"]


def test_discover_fields_cache_control_header(client_factory: Any) -> None:
    """Resposta deve incluir Cache-Control: private, max-age=60."""
    factory, TestingSession = client_factory
    client = factory()
    _bootstrap_admin(client)

    with TestingSession() as db:
        defn = _create_mapping(db, "sophos", "sophos.alert")
        mapping_id = defn.id

    r = client.get(f"/api/mappings/{mapping_id}/discover-fields")
    assert r.status_code == 200, r.text
    cc = r.headers.get("cache-control", "")
    assert "private" in cc
    assert "max-age=60" in cc


def test_discover_fields_limit_100(client_factory: Any) -> None:
    """Deve retornar no máximo 100 campos, mesmo se houver mais no banco."""
    factory, TestingSession = client_factory
    client = factory()
    _bootstrap_admin(client)

    with TestingSession() as db:
        defn = _create_mapping(db, "sophos", "sophos.alert")
        mapping_id = defn.id

        for i in range(150):
            uf = models.UnknownField(
                vendor="sophos",
                event_type="sophos.alert",
                field_path=f"field.path.{i:04d}",
                occurrence_count=i + 1,
                sample_value=None,
                first_seen=datetime(2026, 4, 20, 10, 0, 0),
                last_seen=datetime(2026, 4, 27, 0, 0, 0),
                status="new",
            )
            db.add(uf)
        db.commit()

    r = client.get(f"/api/mappings/{mapping_id}/discover-fields")
    assert r.status_code == 200, r.text
    assert len(r.json()["fields"]) == 100


def test_discover_fields_ignores_other_vendor(client_factory: Any) -> None:
    """Drift de outro vendor/event_type não deve aparecer no resultado."""
    factory, TestingSession = client_factory
    client = factory()
    _bootstrap_admin(client)

    with TestingSession() as db:
        defn = _create_mapping(db, "sophos", "sophos.alert")
        mapping_id = defn.id

        # Mesmo field_path, vendor diferente
        _create_unknown_field(db, "wazuh", "wazuh.alert", "data.severity", 9999, "critical")
        # Mesmo vendor, event_type diferente
        _create_unknown_field(db, "sophos", "sophos.detection", "data.severity", 8888, "high")

    r = client.get(f"/api/mappings/{mapping_id}/discover-fields")
    assert r.status_code == 200, r.text
    assert r.json()["fields"] == []


@pytest.mark.parametrize("role,expected_status", [
    ("viewer", 200),
    ("operator", 200),
    ("engineer", 200),
    ("admin", 200),
])
def test_discover_fields_rbac(
    client_factory: Any,
    role: str,
    expected_status: int,
) -> None:
    """Todos os papéis com mapping.read devem receber 200.

    Non-admin precisam ter uma Integration com platform=sophos na sua org.
    """
    factory, TestingSession = client_factory

    admin_client = factory()
    _bootstrap_admin(admin_client)

    with TestingSession() as db:
        defn = _create_mapping(db, "sophos", "sophos.alert")
        mapping_id = defn.id

    org_id: int | None = None
    if role != "admin":
        with TestingSession() as db:
            org = models.Organization(
                name=f"Discover Test Org {role}",
                slug=f"discover-test-{role}",
                is_active=True,
            )
            db.add(org)
            db.flush()
            integration = models.Integration(
                organization_id=org.id,
                name="Sophos Integration",
                platform="sophos",
            )
            db.add(integration)
            db.commit()
            db.refresh(org)
            org_id = org.id

    r = admin_client.post(
        "/api/auth/users",
        json={
            "username": f"discover_user_{role}",
            "password": "Password123!X",
            "display_name": role.title(),
            "role": role,
            "organization_id": org_id,
        },
    )
    assert r.status_code == 200, r.text

    user_client = factory()
    login_r = user_client.post(
        "/api/auth/login",
        json={"username": f"discover_user_{role}", "password": "Password123!X"},
    )
    assert login_r.status_code == 200, login_r.text

    r = user_client.get(f"/api/mappings/{mapping_id}/discover-fields")
    assert r.status_code == expected_status, (
        f"role={role} esperava {expected_status}, got {r.status_code}: {r.text}"
    )


def test_discover_fields_non_admin_blocked_by_vendor(client_factory: Any) -> None:
    """Non-admin sem Integration com platform=sophos deve receber 404."""
    factory, TestingSession = client_factory

    admin_client = factory()
    _bootstrap_admin(admin_client)

    with TestingSession() as db:
        defn = _create_mapping(db, "sophos", "sophos.alert")
        mapping_id = defn.id

    # Cria org sem nenhuma integration
    with TestingSession() as db:
        org = models.Organization(
            name="Org Sem Integracao",
            slug="org-sem-integracao",
            is_active=True,
        )
        db.add(org)
        db.commit()
        db.refresh(org)
        org_id = org.id

    r = admin_client.post(
        "/api/auth/users",
        json={
            "username": "blocked_viewer",
            "password": "Password123!X",
            "display_name": "Blocked",
            "role": "viewer",
            "organization_id": org_id,
        },
    )
    assert r.status_code == 200, r.text

    blocked_client = factory()
    login_r = blocked_client.post(
        "/api/auth/login",
        json={"username": "blocked_viewer", "password": "Password123!X"},
    )
    assert login_r.status_code == 200, login_r.text

    r = blocked_client.get(f"/api/mappings/{mapping_id}/discover-fields")
    # 404 em vez de 403 para evitar enumeração de tenants
    assert r.status_code == 404


def test_discover_fields_isolated_per_org_same_vendor(client_factory: Any) -> None:
    """Regressão do leak: dois tenants ingerem o MESMO vendor.
    O viewer da org B NÃO pode ver field_path/sample_value do drift da org A."""
    factory, TestingSession = client_factory

    admin_client = factory()
    _bootstrap_admin(admin_client)

    with TestingSession() as db:
        defn = _create_mapping(db, "sophos", "sophos.alert")
        mapping_id = defn.id

        org_a = models.Organization(name="Org A", slug="org-a", is_active=True)
        org_b = models.Organization(name="Org B", slug="org-b", is_active=True)
        db.add_all([org_a, org_b])
        db.commit()
        db.refresh(org_a)
        db.refresh(org_b)
        org_a_id, org_b_id = org_a.id, org_b.id
        # AMBAS as orgs têm integração sophos — o vetor de vazamento.
        for oid in (org_a_id, org_b_id):
            db.add(models.Integration(
                organization_id=oid, name="i", platform="sophos",
                is_active=True, kind="tenant", auth_status="unknown",
            ))
        # Drift da org A (segredo) e da org B (próprio).
        db.add(models.UnknownField(
            vendor="sophos", event_type="sophos.alert", field_path="alert.A_SECRET",
            organization_id=org_a_id, sample_value="10.0.0.1", occurrence_count=1,
            first_seen=datetime(2026, 4, 20), last_seen=datetime(2026, 4, 27), status="new",
        ))
        db.add(models.UnknownField(
            vendor="sophos", event_type="sophos.alert", field_path="alert.B_OWN",
            organization_id=org_b_id, sample_value="b-val", occurrence_count=1,
            first_seen=datetime(2026, 4, 20), last_seen=datetime(2026, 4, 27), status="new",
        ))
        db.commit()

    r = admin_client.post("/api/auth/users", json={
        "username": "viewer_b", "password": "Password123!X",
        "display_name": "B", "role": "viewer", "organization_id": org_b_id,
    })
    assert r.status_code == 200, r.text
    b_client = factory()
    assert b_client.post(
        "/api/auth/login", json={"username": "viewer_b", "password": "Password123!X"}
    ).status_code == 200

    r = b_client.get(f"/api/mappings/{mapping_id}/discover-fields")
    assert r.status_code == 200, r.text
    paths = {f["path"] for f in r.json()["fields"]}
    assert "alert.A_SECRET" not in paths, "viewer da org B NÃO pode ver drift da org A"
    assert paths == {"alert.B_OWN"}, "B vê apenas o próprio drift (controle positivo)"
