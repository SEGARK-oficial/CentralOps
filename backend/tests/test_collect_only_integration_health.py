"""Regressão: integração só-coleta (sem ``provider_factory``) na aba Visão geral.

Bug (presente desde a 1.0.0, visto na 2.17.0 com ``azure``): ``/health``,
``/overview`` e ``/test-connection`` chamavam ``get_provider`` sem checar se a
plataforma tem provider rico. Só sophos/wazuh/crowdstrike/defender/lake têm;
todas as outras (azure, okta, gcp_pubsub, …) estouravam ``ValueError: No
provider registered for platform`` → 500 ao abrir a integração na UI.

Agora as duas leituras respondem com a saúde derivada do estado de coleta (a
mesma régua da aba Pipeline) e o teste de conexão pós-save responde 422.

SQLite em ARQUIVO (não ``:memory:`` + StaticPool): o middleware de auditoria
abre a própria Session, e sob StaticPool as duas dividiriam a mesma conexão —
passa no venv e reprova na imagem compilada.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.collectors.registry import get_platform
from backend.app.db import database as db_module
from backend.app.db import models
from backend.app.db.database import Base, get_session
from backend.app.main import app

COLLECT_ONLY_PLATFORM = "azure"


@pytest.fixture()
def env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'collect_only.db'}",
        connect_args={"check_same_thread": False},
    )
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_session():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    original_session_local = db_module.SessionLocal
    db_module.SessionLocal = TestingSession
    app.dependency_overrides[get_session] = override_get_session
    client = TestClient(app)
    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPass1!", "display_name": "Admin"},
    )
    assert r.status_code == 200, r.text
    r = client.post("/api/auth/login", json={"username": "admin", "password": "AdminPass1!"})
    assert r.status_code == 200, r.text

    yield client, TestingSession

    client.close()
    app.dependency_overrides.clear()
    db_module.SessionLocal = original_session_local
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def _make_integration(Session: Any, platform: str = COLLECT_ONLY_PLATFORM) -> int:
    db = Session()
    try:
        org = models.Organization(name=f"Org {platform}", slug=f"org-{platform}")
        db.add(org)
        db.flush()
        integration = models.Integration(
            organization_id=org.id, name=f"{platform} test", platform=platform, is_active=True,
        )
        db.add(integration)
        db.commit()
        return integration.id
    finally:
        db.close()


def _add_state(Session: Any, integration_id: int, **fields: Any) -> None:
    db = Session()
    try:
        db.add(models.CollectionState(integration_id=integration_id, stream="activity", **fields))
        db.commit()
    finally:
        db.close()


def test_precondition_platform_is_collect_only():
    """Sem isto os demais testes passariam por vacuidade se o azure ganhasse
    provider rico (ou saísse do registry)."""
    reg = get_platform(COLLECT_ONLY_PLATFORM)
    assert reg is not None, "azure precisa estar registrado"
    assert reg.provider_factory is None
    assert get_platform("wazuh").provider_factory is not None


def test_health_v2_returns_collection_timestamps_instead_of_500(env):
    client, Session = env
    int_id = _make_integration(Session)
    ts = datetime(2026, 11, 6, 18, 0, 0)
    _add_state(Session, int_id, last_attempt_at=ts, last_success_at=ts)

    r = client.get(f"/api/integrations/{int_id}/health")

    assert r.status_code == 200, r.text
    data = r.json()
    assert data["schema_version"] == 2
    assert data["platform"] == COLLECT_ONLY_PLATFORM
    assert data["metrics"] == []
    assert data["last_collection_at"] is not None
    assert data["last_success_at"] is not None


def test_health_v1_derives_status_from_collection_state(env):
    client, Session = env
    int_id = _make_integration(Session)
    now = datetime.utcnow()
    _add_state(
        Session, int_id,
        last_attempt_at=now, last_success_at=now,
        consecutive_failures=3, last_error="AuthorizationFailed",
    )

    r = client.get(
        f"/api/integrations/{int_id}/health",
        headers={"Accept": "application/vnd.centralops.v1+json"},
    )

    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "unhealthy"  # >= 3 falhas seguidas (régua do pipeline)
    assert data["details"]["source"] == "collection_state"
    assert data["details"]["last_error"] == "AuthorizationFailed"
    assert "X-API-Deprecation" in r.headers


def test_overview_without_collection_is_unknown(env):
    client, Session = env
    int_id = _make_integration(Session)

    r = client.get(f"/api/integrations/{int_id}/overview")

    assert r.status_code == 200, r.text
    data = r.json()
    assert data["integration"]["id"] == int_id
    assert data["health"]["status"] == "unknown"
    assert data["health"]["details"]["source"] == "collection_state"
    assert data["licensed_products"] is None


def test_overview_healthy_when_collecting(env):
    client, Session = env
    int_id = _make_integration(Session)
    now = datetime.utcnow()
    _add_state(Session, int_id, last_attempt_at=now, last_success_at=now)

    r = client.get(f"/api/integrations/{int_id}/overview")

    assert r.status_code == 200, r.text
    assert r.json()["health"]["status"] == "healthy"


def test_reads_do_not_persist_a_fake_health_check(env):
    """Nada foi verificado no fornecedor: não pode gravar auth_status nem
    histórico de health check como se uma sonda tivesse rodado."""
    client, Session = env
    int_id = _make_integration(Session)

    assert client.get(f"/api/integrations/{int_id}/health").status_code == 200
    assert client.get(f"/api/integrations/{int_id}/overview").status_code == 200

    db = Session()
    try:
        checks = db.query(models.IntegrationHealthCheck).filter_by(integration_id=int_id).count()
        integration = db.get(models.Integration, int_id)
        assert checks == 0
        assert integration.last_checked_at is None
    finally:
        db.close()


def test_test_connection_is_422_not_500(env):
    client, Session = env
    int_id = _make_integration(Session)

    r = client.post(f"/api/integrations/{int_id}/test-connection")

    assert r.status_code == 422, r.text
    assert "integration.test_connection_unsupported" in r.text
