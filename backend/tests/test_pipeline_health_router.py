"""Testes do endpoint GET /api/integrations/{id}/pipeline-health.

Cobre:
- Status healthy / unhealthy / degraded / unknown com base em CollectionState.
- Backlog: watermark_lag_seconds / backlog_detected e a regra que exige os DOIS
  sinais (teto por ciclo + watermark atrasado no MESMO stream) para escalar.
- drift_count_24h filtrando por mappings da integration.
- quarantine_count_24h filtrando por integration_id.
- mapped_field_ratio aproximado.
- Cache Redis: primeira chamada computa, segunda lê do cache (cached_at igual).
- Auth: 401 sem sessão.
- Multi-tenancy: non-admin não acessa integration de outra org (403).
- Bulk endpoint: retorna apenas integrations acessíveis ao user.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import models
from backend.app.db.database import Base, get_session
from backend.app.main import app

try:
    import fakeredis.aioredis  # noqa: F401
    _FAKEREDIS_AVAILABLE = True
except ImportError:
    _FAKEREDIS_AVAILABLE = False


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def client_factory() -> Generator[Any, None, None]:
    """Engine SQLite em memória + override de get_session."""
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


# ── Helpers de seed ───────────────────────────────────────────────────


def _bootstrap_admin(client: TestClient) -> None:
    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPassword123!", "display_name": "Admin"},
    )
    assert r.status_code == 200, r.text


def _create_org(client: TestClient, name: str) -> int:
    r = client.post("/api/organizations/", json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _seed_integration(db: Session, org_id: int, name: str = "test-int", platform: str = "sophos") -> int:
    """Cria integration diretamente no banco para evitar validações do router HTTP."""
    integration = models.Integration(
        organization_id=org_id,
        name=name,
        platform=platform,
        is_active=True,
        auth_status="unknown",
    )
    db.add(integration)
    db.commit()
    db.refresh(integration)
    return integration.id


def _seed_org(db: Session, name: str) -> int:
    """Cria organização diretamente no banco."""
    from uuid import uuid4
    slug = name.lower().replace(" ", "-") + "-" + uuid4().hex[:6]
    org = models.Organization(name=name, slug=slug)
    db.add(org)
    db.commit()
    db.refresh(org)
    return org.id


def _create_user_with_org(
    client: TestClient,
    *,
    username: str,
    role: str = "viewer",
    org_id: int | None = None,
) -> None:
    r = client.post(
        "/api/auth/users",
        json={
            "username": username,
            "password": "TestPassword123!",
            "display_name": username,
            "role": role,
            "organization_id": org_id,
        },
    )
    assert r.status_code == 200, r.text


def _login(client: TestClient, username: str, password: str = "TestPassword123!") -> None:
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text


def _seed_collection_state(
    db: Session,
    integration_id: int,
    *,
    stream: str = "alerts",
    last_success_at: datetime | None = None,
    last_attempt_at: datetime | None = None,
    last_error: str | None = None,
    consecutive_failures: int = 0,
    events_collected_total: int = 100,
    watermark_at: datetime | None = None,
    last_run_capped: bool = False,
) -> models.CollectionState:
    cs = models.CollectionState(
        integration_id=integration_id,
        stream=stream,
        last_success_at=last_success_at,
        last_attempt_at=last_attempt_at or datetime.utcnow(),
        last_error=last_error,
        consecutive_failures=consecutive_failures,
        events_collected_total=events_collected_total,
        watermark_at=watermark_at,
        last_run_capped=last_run_capped,
    )
    db.add(cs)
    db.commit()
    db.refresh(cs)
    return cs


def _seed_mapping_definition(
    db: Session,
    vendor: str = "sophos",
    event_type: str = "sophos.alert",
) -> models.MappingDefinition:
    # Cria versão e aponta como current
    md = models.MappingDefinition(
        vendor=vendor,
        event_type=event_type,
        ocsf_class_uid=2004,
    )
    db.add(md)
    db.flush()

    rules = [
        {"target": "class_uid", "const": 2004},
        {"target": "severity", "source": "severity"},
        {"target": "actor.user.name", "source": "actor"},
    ]
    mv = models.MappingVersion(
        definition_id=md.id,
        version_number=1,
        rules=json.dumps(rules),
        commit_message="seed",
    )
    db.add(mv)
    db.flush()

    md.current_version_id = mv.id
    db.commit()
    db.refresh(md)
    return md


def _seed_mapping_definition_v2(
    db: Session,
    vendor: str = "entra_id",
    event_type: str = "entra_id.signin",
) -> models.MappingDefinition:
    """Seed com DSL v2 (dict ``{'preprocess', 'rules'}``) — o formato do editor
    (``dsl_version=2``). Antes do fix, versões v2 somavam 0
    known-paths (``isinstance(rules, list)`` falhava p/ o dict) e
    ``mapped_field_ratio`` virava ``None`` permanentemente."""
    md = models.MappingDefinition(
        vendor=vendor,
        event_type=event_type,
        ocsf_class_uid=3002,
    )
    db.add(md)
    db.flush()

    payload = {
        "preprocess": [{"op": "rename", "source": "raw.src", "target": "_src"}],
        "rules": [
            {"target": "class_uid", "const": 3002},
            {"target": "severity_id", "source": "sev"},
            {"target": "actor.user.name", "source": "user"},
        ],
    }
    mv = models.MappingVersion(
        definition_id=md.id,
        version_number=1,
        rules=json.dumps(payload),
        commit_message="seed v2",
    )
    db.add(mv)
    db.flush()

    md.current_version_id = mv.id
    db.commit()
    db.refresh(md)
    return md


def _seed_unknown_field(
    db: Session,
    vendor: str = "sophos",
    event_type: str = "sophos.alert",
    *,
    last_seen: datetime | None = None,
    status: str = "new",
    field_suffix: str = "x",
    organization_id: int | None = None,
) -> models.UnknownField:
    uf = models.UnknownField(
        vendor=vendor,
        event_type=event_type,
        field_path=f"unknown.field.{field_suffix}",
        organization_id=organization_id,
        last_seen=last_seen or datetime.utcnow(),
        status=status,
    )
    db.add(uf)
    db.commit()
    db.refresh(uf)
    return uf


def _seed_quarantine_event(
    db: Session,
    integration_id: int,
    *,
    created_at: datetime | None = None,
) -> models.QuarantineEvent:
    ev = models.QuarantineEvent(
        integration_id=integration_id,
        vendor="sophos",
        event_type="sophos.alert",
        raw_payload='{"id":"x"}',
        error_kind="map",
        created_at=created_at or datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(days=7),
    )
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return ev


def _make_fake_redis():
    """Cria FakeRedis e retorna o patcher + instância."""
    if not _FAKEREDIS_AVAILABLE:
        pytest.skip("fakeredis[lua] não instalado")

    import fakeredis.aioredis as fakeredis_aio

    fake_redis = fakeredis_aio.FakeRedis(decode_responses=True)
    mock_from_url = MagicMock(return_value=fake_redis)
    patcher = patch(
        "backend.app.routers.pipeline_health.redis_async.from_url",
        mock_from_url,
    )
    return patcher, fake_redis


# ── Testes ────────────────────────────────────────────────────────────


def test_pipeline_health_requires_auth(client_factory: Any) -> None:
    """Sem cookie de sessão, endpoint deve retornar 401."""
    _, _ = client_factory
    client = TestClient(app)  # sem sessão
    r = client.get("/api/integrations/1/pipeline-health")
    assert r.status_code == 401


def test_pipeline_health_returns_404_for_nonexistent_integration(
    client_factory: Any,
) -> None:
    """Integration inexistente deve retornar 404."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    patcher, _ = _make_fake_redis()
    with patcher:
        r = client.get("/api/integrations/999/pipeline-health")
    assert r.status_code == 404


def test_pipeline_health_returns_unknown_when_never_collected(
    client_factory: Any,
) -> None:
    """Status deve ser 'unknown' quando nunca houve coleta (last_success_at IS NULL)."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org_id = _seed_org(db, "Org A")
        iid = _seed_integration(db, org_id)
        _seed_collection_state(db, iid, last_success_at=None)

    patcher, _ = _make_fake_redis()
    with patcher:
        r = client.get(f"/api/integrations/{iid}/pipeline-health")

    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "unknown"
    assert data["lag_seconds"] is None
    assert data["last_success_at"] is None


def test_pipeline_health_returns_healthy_for_recent_success(
    client_factory: Any,
) -> None:
    """Coleta recente sem erros → status 'healthy'."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    now = datetime.utcnow()
    with Session() as db:
        org_id = _seed_org(db, "Org B")
        iid = _seed_integration(db, org_id)
        _seed_collection_state(
            db,
            iid,
            last_success_at=now - timedelta(seconds=30),
            last_error=None,
            consecutive_failures=0,
        )

    patcher, _ = _make_fake_redis()
    with patcher:
        r = client.get(f"/api/integrations/{iid}/pipeline-health")

    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "healthy"
    assert data["lag_seconds"] is not None
    assert data["lag_seconds"] <= 60  # dentro do esperado


def test_pipeline_health_returns_unhealthy_for_lag_over_5min(
    client_factory: Any,
) -> None:
    """Lag > 300s → status 'unhealthy'."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org_id = _seed_org(db, "Org C")
        iid = _seed_integration(db, org_id)
        _seed_collection_state(
            db,
            iid,
            last_success_at=datetime.utcnow() - timedelta(seconds=400),
            consecutive_failures=0,
            last_error=None,
        )

    patcher, _ = _make_fake_redis()
    with patcher:
        r = client.get(f"/api/integrations/{iid}/pipeline-health")

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "unhealthy"
    assert r.json()["lag_seconds"] > 300


def test_pipeline_health_returns_unhealthy_for_consecutive_failures(
    client_factory: Any,
) -> None:
    """consecutive_failures >= 3 → 'unhealthy', mesmo com lag curto."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org_id = _seed_org(db, "Org D")
        iid = _seed_integration(db, org_id)
        _seed_collection_state(
            db,
            iid,
            last_success_at=datetime.utcnow() - timedelta(seconds=10),
            consecutive_failures=3,
        )

    patcher, _ = _make_fake_redis()
    with patcher:
        r = client.get(f"/api/integrations/{iid}/pipeline-health")

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "unhealthy"


def test_pipeline_health_returns_degraded_for_recent_error(
    client_factory: Any,
) -> None:
    """Lag <= 300s com last_error presente → 'degraded'."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org_id = _seed_org(db, "Org E")
        iid = _seed_integration(db, org_id)
        _seed_collection_state(
            db,
            iid,
            last_success_at=datetime.utcnow() - timedelta(seconds=60),
            last_attempt_at=datetime.utcnow() - timedelta(seconds=5),
            last_error="connection timeout",
            consecutive_failures=1,
        )

    patcher, _ = _make_fake_redis()
    with patcher:
        r = client.get(f"/api/integrations/{iid}/pipeline-health")

    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "degraded"
    assert data["last_error"] == "connection timeout"


def test_pipeline_health_last_error_truncated_at_500_chars(
    client_factory: Any,
) -> None:
    """last_error deve ser truncado em 500 chars."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    long_error = "x" * 600

    with Session() as db:
        org_id = _seed_org(db, "Org F")
        iid = _seed_integration(db, org_id)
        _seed_collection_state(
            db,
            iid,
            last_success_at=datetime.utcnow() - timedelta(seconds=10),
            last_attempt_at=datetime.utcnow(),
            last_error=long_error,
            consecutive_failures=1,
        )

    patcher, _ = _make_fake_redis()
    with patcher:
        r = client.get(f"/api/integrations/{iid}/pipeline-health")

    assert r.status_code == 200, r.text
    assert len(r.json()["last_error"]) == 500


def test_pipeline_health_quarantine_count_24h_filters_by_integration_id(
    client_factory: Any,
) -> None:
    """quarantine_count_24h só conta eventos da integration correta."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org_id = _seed_org(db, "Org G")
        iid1 = _seed_integration(db, org_id, name="int-1")
        iid2 = _seed_integration(db, org_id, name="int-2")
        _seed_collection_state(db, iid1, last_success_at=datetime.utcnow() - timedelta(seconds=10))
        _seed_quarantine_event(db, iid1)
        _seed_quarantine_event(db, iid1)
        _seed_quarantine_event(db, iid2)  # não deve contar para iid1

    patcher, _ = _make_fake_redis()
    with patcher:
        r = client.get(f"/api/integrations/{iid1}/pipeline-health")

    assert r.status_code == 200, r.text
    assert r.json()["quarantine_count_24h"] == 2


def test_pipeline_health_quarantine_count_24h_ignores_old_events(
    client_factory: Any,
) -> None:
    """quarantine_count_24h só conta eventos criados nas últimas 24h."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org_id = _seed_org(db, "Org H")
        iid = _seed_integration(db, org_id)
        _seed_collection_state(db, iid, last_success_at=datetime.utcnow() - timedelta(seconds=10))
        _seed_quarantine_event(db, iid)  # recente
        _seed_quarantine_event(
            db, iid, created_at=datetime.utcnow() - timedelta(hours=25)
        )  # antigo — não deve contar

    patcher, _ = _make_fake_redis()
    with patcher:
        r = client.get(f"/api/integrations/{iid}/pipeline-health")

    assert r.status_code == 200, r.text
    assert r.json()["quarantine_count_24h"] == 1


def test_pipeline_health_drift_count_24h_filters_by_integration_mappings(
    client_factory: Any,
) -> None:
    """drift_count_24h considera apenas UnknownFields dos mappings da integration."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org_id = _seed_org(db, "Org I")
        iid = _seed_integration(db, org_id, platform="sophos")
        _seed_collection_state(db, iid, last_success_at=datetime.utcnow() - timedelta(seconds=10))
        _seed_mapping_definition(db, vendor="sophos", event_type="sophos.alert")
        # Campos da sophos → devem contar
        _seed_unknown_field(db, organization_id=org_id, vendor="sophos", event_type="sophos.alert", field_suffix="a1")
        _seed_unknown_field(db, organization_id=org_id, vendor="sophos", event_type="sophos.alert", field_suffix="a2")
        # Campo de outro vendor → não deve contar
        _seed_unknown_field(
            db, organization_id=org_id, vendor="microsoft_defender", event_type="defender.alert", field_suffix="b1"
        )

    patcher, _ = _make_fake_redis()
    with patcher:
        r = client.get(f"/api/integrations/{iid}/pipeline-health")

    assert r.status_code == 200, r.text
    assert r.json()["drift_count_24h"] == 2


def test_pipeline_health_drift_count_24h_ignores_old_fields(
    client_factory: Any,
) -> None:
    """drift_count_24h ignora UnknownFields com last_seen > 24h atrás."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org_id = _seed_org(db, "Org J")
        iid = _seed_integration(db, org_id, platform="sophos")
        _seed_collection_state(db, iid, last_success_at=datetime.utcnow() - timedelta(seconds=10))
        _seed_mapping_definition(db, vendor="sophos", event_type="sophos.alert")
        # Recente — conta
        _seed_unknown_field(db, organization_id=org_id, vendor="sophos", event_type="sophos.alert", field_suffix="c1")
        # Antigo — não conta
        _seed_unknown_field(
            db,
            organization_id=org_id, vendor="sophos",
            event_type="sophos.alert",
            last_seen=datetime.utcnow() - timedelta(hours=25),
            field_suffix="c2",
        )

    patcher, _ = _make_fake_redis()
    with patcher:
        r = client.get(f"/api/integrations/{iid}/pipeline-health")

    assert r.status_code == 200, r.text
    assert r.json()["drift_count_24h"] == 1


def test_pipeline_health_drift_ignores_non_new_status(
    client_factory: Any,
) -> None:
    """drift_count_24h só conta UnknownFields com status='new'."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org_id = _seed_org(db, "Org K")
        iid = _seed_integration(db, org_id, platform="sophos")
        _seed_collection_state(db, iid, last_success_at=datetime.utcnow() - timedelta(seconds=10))
        _seed_mapping_definition(db, vendor="sophos", event_type="sophos.alert")
        _seed_unknown_field(db, organization_id=org_id, vendor="sophos", event_type="sophos.alert", status="new", field_suffix="d1")
        _seed_unknown_field(db, organization_id=org_id, vendor="sophos", event_type="sophos.alert", status="ignored", field_suffix="d2")
        _seed_unknown_field(db, organization_id=org_id, vendor="sophos", event_type="sophos.alert", status="mapped", field_suffix="d3")

    patcher, _ = _make_fake_redis()
    with patcher:
        r = client.get(f"/api/integrations/{iid}/pipeline-health")

    assert r.status_code == 200, r.text
    assert r.json()["drift_count_24h"] == 1


def test_pipeline_health_drift_count_isolated_per_org_same_vendor(
    client_factory: Any,
) -> None:
    """drift_count_24h conta APENAS o drift da org da integração.
    Duas orgs no MESMO vendor não se contaminam (antes o count somava drift de
    todos os tenants do vendor — side-channel cross-tenant)."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org_a = _seed_org(db, "Org A drift")
        org_b = _seed_org(db, "Org B drift")
        iid_a = _seed_integration(db, org_a, platform="sophos")
        _seed_integration(db, org_b, platform="sophos")  # mesmo vendor
        _seed_collection_state(db, iid_a, last_success_at=datetime.utcnow() - timedelta(seconds=10))
        _seed_mapping_definition(db, vendor="sophos", event_type="sophos.alert")
        # org A: 1 campo de drift.
        _seed_unknown_field(db, organization_id=org_a, vendor="sophos", event_type="sophos.alert", field_suffix="a1")
        # org B: 2 campos do MESMO vendor/event_type — NÃO podem entrar no count de A.
        _seed_unknown_field(db, organization_id=org_b, vendor="sophos", event_type="sophos.alert", field_suffix="b1")
        _seed_unknown_field(db, organization_id=org_b, vendor="sophos", event_type="sophos.alert", field_suffix="b2")

    patcher, _ = _make_fake_redis()
    with patcher:
        r = client.get(f"/api/integrations/{iid_a}/pipeline-health")

    assert r.status_code == 200, r.text
    # 1 (só org A), não 3 — o drift de B não contamina o count de A.
    assert r.json()["drift_count_24h"] == 1


def test_pipeline_health_mapped_field_ratio_approximation(
    client_factory: Any,
) -> None:
    """mapped_field_ratio deve ser entre 0.0 e 1.0 quando há mappings configurados."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org_id = _seed_org(db, "Org L")
        iid = _seed_integration(db, org_id, platform="sophos")
        _seed_collection_state(db, iid, last_success_at=datetime.utcnow() - timedelta(seconds=10))
        # Mapping com 3 regras; 1 campo novo desconhecido
        _seed_mapping_definition(db, vendor="sophos", event_type="sophos.alert")
        _seed_unknown_field(db, organization_id=org_id, vendor="sophos", event_type="sophos.alert", field_suffix="e1")

    patcher, _ = _make_fake_redis()
    with patcher:
        r = client.get(f"/api/integrations/{iid}/pipeline-health")

    assert r.status_code == 200, r.text
    data = r.json()
    assert data["mapped_field_ratio"] is not None
    ratio = data["mapped_field_ratio"]
    assert 0.0 <= ratio <= 1.0
    # Com 3 regras e 1 campo drift: 3/(3+1) = 0.75.
    # Era `1 - 1/3 ≈ 0.667`, que não é proporção — passa de 1 assim que há mais
    # paths novos do que regras, e o clamp prendia o indicador em 0.
    assert abs(ratio - 0.75) < 0.01


def test_pipeline_health_mapped_field_ratio_none_without_mappings(
    client_factory: Any,
) -> None:
    """mapped_field_ratio deve ser None quando não há mappings configurados para o vendor."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org_id = _seed_org(db, "Org M")
        # platform "myvendor" — sem MappingDefinition criada
        iid = _seed_integration(db, org_id, platform="myvendor")
        _seed_collection_state(db, iid, last_success_at=datetime.utcnow() - timedelta(seconds=10))

    patcher, _ = _make_fake_redis()
    with patcher:
        r = client.get(f"/api/integrations/{iid}/pipeline-health")

    assert r.status_code == 200, r.text
    assert r.json()["mapped_field_ratio"] is None


def test_pipeline_health_mapped_field_ratio_counts_v2_dict_rules(
    client_factory: Any,
) -> None:
    """DSL v2 (dict com 'rules') deve contar known-paths e produzir um ratio.

    Regressão: antes, ``total_known_paths`` só somava quando
    ``isinstance(rules, list)`` — toda versão v2 (dict, o default do editor) somava
    0 e ``mapped_field_ratio`` virava ``None`` para praticamente todos os mappings.
    """
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org_id = _seed_org(db, "Org V2")
        iid = _seed_integration(db, org_id, platform="entra_id")
        _seed_collection_state(
            db, iid, last_success_at=datetime.utcnow() - timedelta(seconds=10)
        )
        # Mapping v2 (dict) com 3 regras; 1 campo novo desconhecido.
        _seed_mapping_definition_v2(db, vendor="entra_id", event_type="entra_id.signin")
        _seed_unknown_field(
            db,
            organization_id=org_id,
            vendor="entra_id",
            event_type="entra_id.signin",
            field_suffix="e1",
        )

    patcher, _ = _make_fake_redis()
    with patcher:
        r = client.get(f"/api/integrations/{iid}/pipeline-health")

    assert r.status_code == 200, r.text
    ratio = r.json()["mapped_field_ratio"]
    assert ratio is not None, (
        "v2 (dict) mapping deve produzir ratio != None (regressão)"
    )
    assert 0.0 <= ratio <= 1.0
    # 3 regras (v2 payload['rules']) e 1 drift → 3/(3+1) = 0.75
    assert abs(ratio - 0.75) < 0.01


def test_pipeline_health_caches_for_60s(client_factory: Any) -> None:
    """Segunda chamada dentro de 60s deve retornar mesmo cached_at (lido do cache)."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org_id = _seed_org(db, "Org N")
        iid = _seed_integration(db, org_id)
        _seed_collection_state(db, iid, last_success_at=datetime.utcnow() - timedelta(seconds=10))

    patcher, _ = _make_fake_redis()
    with patcher:
        r1 = client.get(f"/api/integrations/{iid}/pipeline-health")
        assert r1.status_code == 200, r1.text
        cached_at_1 = r1.json()["cached_at"]

        r2 = client.get(f"/api/integrations/{iid}/pipeline-health")
        assert r2.status_code == 200, r2.text
        cached_at_2 = r2.json()["cached_at"]

    # Ambas as chamadas devem retornar o mesmo cached_at (do cache)
    assert cached_at_1 == cached_at_2, (
        f"Esperava mesmo cached_at nas duas chamadas; got {cached_at_1!r} vs {cached_at_2!r}"
    )


def test_pipeline_health_non_admin_cannot_access_other_org_integration(
    client_factory: Any,
) -> None:
    """Non-admin não deve acessar integration de outra org → 403."""
    factory, Session = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)

    with Session() as db:
        org_a = _seed_org(db, "Org Alfa")
        org_b = _seed_org(db, "Org Beta")
        iid_a = _seed_integration(db, org_a, name="int-alfa")

    # Cria user vinculado à org B via API (precisa de org_b id)
    _create_user_with_org(
        admin_client,
        username="user_beta",
        role="viewer",
        org_id=org_b,
    )

    user_client = factory()
    _login(user_client, "user_beta")

    patcher, _ = _make_fake_redis()
    with patcher:
        r = user_client.get(f"/api/integrations/{iid_a}/pipeline-health")

    assert r.status_code == 403, r.text


def test_pipeline_health_non_admin_can_access_own_org_integration(
    client_factory: Any,
) -> None:
    """Non-admin pode acessar integration da própria org."""
    factory, Session = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)

    with Session() as db:
        org_id = _seed_org(db, "Org Própria")
        iid = _seed_integration(db, org_id)
        _seed_collection_state(db, iid, last_success_at=datetime.utcnow() - timedelta(seconds=10))

    _create_user_with_org(
        admin_client,
        username="user_propria",
        role="viewer",
        org_id=org_id,
    )

    user_client = factory()
    _login(user_client, "user_propria")

    patcher, _ = _make_fake_redis()
    with patcher:
        r = user_client.get(f"/api/integrations/{iid}/pipeline-health")

    assert r.status_code == 200, r.text


def test_pipeline_health_response_schema_complete(client_factory: Any) -> None:
    """Response deve conter todos os campos do schema IntegrationPipelineHealth."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org_id = _seed_org(db, "Org Schema")
        iid = _seed_integration(db, org_id)
        _seed_collection_state(db, iid, last_success_at=datetime.utcnow() - timedelta(seconds=10))

    patcher, _ = _make_fake_redis()
    with patcher:
        r = client.get(f"/api/integrations/{iid}/pipeline-health")

    assert r.status_code == 200, r.text
    data = r.json()
    expected_fields = {
        "integration_id",
        "status",
        "events_per_minute",
        "lag_seconds",
        "watermark_lag_seconds",
        "backlog_detected",
        "last_error",
        "last_success_at",
        "mapped_field_ratio",
        "drift_count_24h",
        "quarantine_count_24h",
        "cached_at",
    }
    assert expected_fields == set(data.keys()), (
        f"Campos inesperados/faltando: {set(data.keys()) ^ expected_fields}"
    )
    assert data["integration_id"] == iid


def test_pipeline_health_no_collection_state(client_factory: Any) -> None:
    """Integration sem nenhum CollectionState → status unknown, contadores 0."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org_id = _seed_org(db, "Org Empty")
        iid = _seed_integration(db, org_id)

    patcher, _ = _make_fake_redis()
    with patcher:
        r = client.get(f"/api/integrations/{iid}/pipeline-health")

    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "unknown"
    assert data["drift_count_24h"] == 0
    assert data["quarantine_count_24h"] == 0


# ── Bulk endpoint ─────────────────────────────────────────────────────


def test_pipeline_health_bulk_returns_user_accessible_integrations(
    client_factory: Any,
) -> None:
    """Bulk: admin vê todas as integrations; non-admin só da própria org."""
    factory, Session = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)

    with Session() as db:
        org_a = _seed_org(db, "BulkOrg A")
        org_b = _seed_org(db, "BulkOrg B")
        iid_a1 = _seed_integration(db, org_a, name="ba1")
        _seed_integration(db, org_a, name="ba2")
        _seed_integration(db, org_b, name="bb1")

    # Cria user vinculado à org_a via API
    _create_user_with_org(admin_client, username="user_bulk_a", role="viewer", org_id=org_a)
    user_client = factory()
    _login(user_client, "user_bulk_a")

    patcher, _ = _make_fake_redis()
    with patcher:
        # Admin vê 3 integrations (pode haver mais do banco de outras fixtures)
        r_admin = admin_client.get("/api/integrations/pipeline-health")
        assert r_admin.status_code == 200, r_admin.text
        admin_data = r_admin.json()
        assert admin_data["total"] >= 3

        # User vê apenas 2 (da org_a)
        r_user = user_client.get("/api/integrations/pipeline-health")
        assert r_user.status_code == 200, r_user.text
        user_data = r_user.json()
        assert user_data["total"] == 2

    # Verifica que bulk response tem os campos esperados
    assert "items" in user_data
    assert "total" in user_data
    assert "cached_at" in user_data
    # Verifica que todos os items são da org correta
    ids_retornados = {item["integration_id"] for item in user_data["items"]}
    assert iid_a1 in ids_retornados


def test_pipeline_health_bulk_requires_auth(client_factory: Any) -> None:
    """Bulk sem autenticação → 401."""
    _, _ = client_factory
    client = TestClient(app)
    r = client.get("/api/integrations/pipeline-health")
    assert r.status_code == 401


# ── Teste de determinação de status (unitário puro) ───────────────────


@pytest.mark.parametrize(
    "last_success_at,lag_seconds,consecutive_failures_max,last_error,expected",
    [
        (None, None, 0, None, "unknown"),
        (datetime.utcnow(), 400, 0, None, "unhealthy"),
        (datetime.utcnow(), 100, 3, None, "unhealthy"),
        (datetime.utcnow(), 100, 1, "timeout", "degraded"),
        (datetime.utcnow(), 100, 0, None, "healthy"),
        (datetime.utcnow(), 300, 0, None, "healthy"),   # exatamente 300 → não ultrapassa
        (datetime.utcnow(), 301, 0, None, "unhealthy"),  # 301 ultrapassa
    ],
)
def test_determine_status_parametrized(
    last_success_at: datetime | None,
    lag_seconds: int | None,
    consecutive_failures_max: int,
    last_error: str | None,
    expected: str,
) -> None:
    """Testa a função pura _determine_status diretamente."""
    from backend.app.routers.pipeline_health import _determine_status

    result = _determine_status(
        last_success_at=last_success_at,
        lag_seconds=lag_seconds,
        consecutive_failures_max=consecutive_failures_max,
        last_error=last_error,
    )
    assert result == expected, f"esperava {expected!r}, got {result!r}"


# ── Backlog: watermark atrasado + teto por ciclo ──────────────────────


@pytest.mark.parametrize(
    "backlog_detected,backlog_lag_seconds,expected,motivo",
    [
        # A ARMADILHA, nos dois sentidos: nenhum dos dois sinais escala sozinho.
        (False, 54_000, "healthy", "watermark 15h atrás SEM teto = stream sem eventos"),
        (True, 120, "healthy", "teto atingido mas em dia = pico absorvido"),
        # Os dois juntos: é backlog. Forma do incidente jul/2026 (coletor Wazuh).
        (True, 54_000, "degraded", "teto + 15h atrás = backlog real"),
        # Limiar: 1800s exatos não ultrapassam; 1801 ultrapassa.
        (True, 1800, "healthy", "exatamente no limiar não escala"),
        (True, 1801, "degraded", "um segundo acima escala"),
        # Cursor não temporal: sem watermark não há prova de atraso.
        (True, None, "healthy", "sem watermark não dá para provar atraso"),
        (False, None, "healthy", "baseline — nenhum sinal"),
    ],
)
def test_determine_status_backlog_requires_both_signals(
    backlog_detected: bool,
    backlog_lag_seconds: int | None,
    expected: str,
    motivo: str,
) -> None:
    """Backlog só escala com teto atingido E watermark atrasado — nunca com um só."""
    from backend.app.routers.pipeline_health import _determine_status

    result = _determine_status(
        last_success_at=datetime.utcnow(),
        lag_seconds=10,  # coletando normalmente: o velho lag_seconds diz "healthy"
        consecutive_failures_max=0,
        last_error=None,
        backlog_detected=backlog_detected,
        backlog_lag_seconds=backlog_lag_seconds,
    )
    assert result == expected, f"{motivo}: esperava {expected!r}, got {result!r}"


def test_determine_status_backlog_does_not_mask_unhealthy() -> None:
    """Parado E atrasado continua 'unhealthy' — backlog não rebaixa a regra 2.

    Backlog é 'coleta, mas atrasada'; se o coletor está falhando, o problema
    urgente é a falha, e o operador reage a ela primeiro.
    """
    from backend.app.routers.pipeline_health import _determine_status

    assert (
        _determine_status(
            last_success_at=datetime.utcnow(),
            lag_seconds=10,
            consecutive_failures_max=3,
            last_error="boom",
            backlog_detected=True,
            backlog_lag_seconds=54_000,
        )
        == "unhealthy"
    )


def test_determine_status_default_args_preserve_legacy_behavior() -> None:
    """Chamada sem os argumentos de backlog dá exatamente o status de antes."""
    from backend.app.routers.pipeline_health import _determine_status

    now = datetime.utcnow()
    assert _determine_status(now, 100, 0, None) == "healthy"
    assert _determine_status(now, 100, 0, "timeout") == "degraded"
    assert _determine_status(now, 400, 0, None) == "unhealthy"
    assert _determine_status(None, None, 0, None) == "unknown"


def test_pipeline_health_watermark_lag_is_null_without_watermark(
    client_factory: Any,
) -> None:
    """Cursor não temporal → watermark_lag_seconds null, NUNCA 0.

    0 afirmaria 'em dia'; a resposta correta é 'não medível'.
    """
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org_id = _seed_org(db, "Org WM Null")
        iid = _seed_integration(db, org_id)
        _seed_collection_state(
            db, iid, last_success_at=datetime.utcnow() - timedelta(seconds=10)
        )

    patcher, _ = _make_fake_redis()
    with patcher:
        r = client.get(f"/api/integrations/{iid}/pipeline-health")

    assert r.status_code == 200, r.text
    data = r.json()
    assert data["watermark_lag_seconds"] is None
    assert data["backlog_detected"] is False
    assert data["status"] == "healthy"


def test_pipeline_health_watermark_lag_reports_worst_stream(
    client_factory: Any,
) -> None:
    """N streams agregam pelo PIOR: o stream em dia não dilui o atrasado."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    now = datetime.utcnow()
    with Session() as db:
        org_id = _seed_org(db, "Org WM Worst")
        iid = _seed_integration(db, org_id)
        _seed_collection_state(
            db, iid, stream="alerts",
            last_success_at=now - timedelta(seconds=10),
            watermark_at=now - timedelta(seconds=30),
        )
        _seed_collection_state(
            db, iid, stream="detections",
            last_success_at=now - timedelta(seconds=10),
            watermark_at=now - timedelta(hours=3),
        )

    patcher, _ = _make_fake_redis()
    with patcher:
        r = client.get(f"/api/integrations/{iid}/pipeline-health")

    assert r.status_code == 200, r.text
    lag = r.json()["watermark_lag_seconds"]
    assert lag is not None and lag >= 3 * 3600 - 60, (
        f"esperava o atraso do pior stream (~10800s), got {lag!r}"
    )


def test_pipeline_health_backlog_escalates_to_degraded(
    client_factory: Any,
) -> None:
    """A forma do incidente: coleta a cada ciclo, mas processando ontem.

    ``last_success_at`` recente ⇒ ``lag_seconds`` ~0 ⇒ as regras antigas diziam
    'healthy'. Com teto atingido + watermark 15h atrás, o status vira 'degraded'.
    """
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    now = datetime.utcnow()
    with Session() as db:
        org_id = _seed_org(db, "Org Backlog")
        iid = _seed_integration(db, org_id, platform="wazuh")
        _seed_collection_state(
            db, iid, stream="detections",
            last_success_at=now - timedelta(seconds=10),
            watermark_at=now - timedelta(hours=15),
            last_run_capped=True,
        )

    patcher, _ = _make_fake_redis()
    with patcher:
        r = client.get(f"/api/integrations/{iid}/pipeline-health")

    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "degraded", data
    assert data["backlog_detected"] is True
    assert data["lag_seconds"] <= 60, "o lag antigo continua 'saudável' — esse era o bug"
    assert data["watermark_lag_seconds"] >= 15 * 3600 - 60


def test_pipeline_health_stale_watermark_without_cap_stays_healthy(
    client_factory: Any,
) -> None:
    """Watermark parado há 15h SEM teto = stream sem eventos. Não escala."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    now = datetime.utcnow()
    with Session() as db:
        org_id = _seed_org(db, "Org Quiet")
        iid = _seed_integration(db, org_id)
        _seed_collection_state(
            db, iid, stream="detections",
            last_success_at=now - timedelta(seconds=10),
            watermark_at=now - timedelta(hours=15),
            last_run_capped=False,
        )

    patcher, _ = _make_fake_redis()
    with patcher:
        r = client.get(f"/api/integrations/{iid}/pipeline-health")

    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "healthy", data
    assert data["backlog_detected"] is False
    # O atraso continua VISÍVEL — só não é motivo de escalada por si só.
    assert data["watermark_lag_seconds"] >= 15 * 3600 - 60


def test_pipeline_health_backlog_within_threshold_stays_healthy(
    client_factory: Any,
) -> None:
    """Teto atingido mas watermark de 5 min = pico sendo absorvido. Não escala."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    now = datetime.utcnow()
    with Session() as db:
        org_id = _seed_org(db, "Org Burst")
        iid = _seed_integration(db, org_id)
        _seed_collection_state(
            db, iid, stream="detections",
            last_success_at=now - timedelta(seconds=10),
            watermark_at=now - timedelta(minutes=5),
            last_run_capped=True,
        )

    patcher, _ = _make_fake_redis()
    with patcher:
        r = client.get(f"/api/integrations/{iid}/pipeline-health")

    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "healthy", data
    assert data["backlog_detected"] is True  # o fato é reportado…
    # …mas sozinho não escala: o teto absorvendo um pico é o teto funcionando.


def test_pipeline_health_cap_and_lag_on_different_streams_does_not_escalate(
    client_factory: Any,
) -> None:
    """O par (teto, atraso) tem de vir do MESMO stream.

    Stream A bateu o teto mas está em dia; stream B está 15h atrás porque não tem
    evento. Um ``any(teto) AND max(atraso)`` global inventaria backlog aqui.
    """
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    now = datetime.utcnow()
    with Session() as db:
        org_id = _seed_org(db, "Org Cross Stream")
        iid = _seed_integration(db, org_id)
        _seed_collection_state(
            db, iid, stream="alerts",
            last_success_at=now - timedelta(seconds=10),
            watermark_at=now - timedelta(seconds=30),
            last_run_capped=True,   # capado, mas em dia
        )
        _seed_collection_state(
            db, iid, stream="detections",
            last_success_at=now - timedelta(seconds=10),
            watermark_at=now - timedelta(hours=15),
            last_run_capped=False,  # atrasado, mas sem teto (silencioso)
        )

    patcher, _ = _make_fake_redis()
    with patcher:
        r = client.get(f"/api/integrations/{iid}/pipeline-health")

    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "healthy", (
        "teto num stream + atraso em OUTRO não é backlog"
    )
    # Os dois fatos continuam reportados, cada um pelo pior stream.
    assert data["backlog_detected"] is True
    assert data["watermark_lag_seconds"] >= 15 * 3600 - 60
