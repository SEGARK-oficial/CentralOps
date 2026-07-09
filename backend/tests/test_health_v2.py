"""Testes para HealthSchema v2 e versionamento via header Accept.

Cobertura:
- HealthMetric / HealthResponse: validação Pydantic dos schemas.
- GET /integrations/{id}/health (sem header): retorna v2.
- GET /integrations/{id}/health (Accept: v1): retorna v1 + header X-API-Deprecation.
- GET /integrations/{id}/health (v2): metrics=[] quando provider não implementa;
  last_collection_at populado a partir de CollectionState.
- WazuhProvider.get_health_metrics(): smoke test com mock do manager/indexer.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import database as db_module
from backend.app.db import models
from backend.app.db.database import Base, get_session
from backend.app.main import app
from backend.app.schemas.health import HealthMetric, HealthResponse


# ── Schema unit tests ─────────────────────────────────────────────────────────


class TestHealthMetricSchema:
    def test_minimal_valid(self):
        m = HealthMetric(id="foo", label="Foo", value="ok")
        assert m.id == "foo"
        assert m.severity == "unknown"

    def test_all_fields(self):
        m = HealthMetric(
            id="manager_status",
            label="Manager",
            value="healthy",
            unit=None,
            severity="ok",
            icon_id="server",
            hint="All good",
            group="manager",
        )
        assert m.severity == "ok"
        assert m.group == "manager"

    def test_bool_value_accepted(self):
        m = HealthMetric(id="x", label="X", value=True)
        assert m.value is True

    def test_invalid_severity_rejected(self):
        with pytest.raises(Exception):
            HealthMetric(id="x", label="X", value="v", severity="bad_value")


class TestHealthResponseSchema:
    def test_schema_version_fixed(self):
        r = HealthResponse(platform="wazuh")
        assert r.schema_version == 2

    def test_metrics_default_empty(self):
        r = HealthResponse(platform="sophos")
        assert r.metrics == []

    def test_with_metrics(self):
        m = HealthMetric(id="a", label="A", value=1, severity="ok")
        r = HealthResponse(platform="wazuh", metrics=[m])
        assert len(r.metrics) == 1

    def test_timestamps_optional(self):
        r = HealthResponse(platform="wazuh")
        assert r.last_collection_at is None
        assert r.last_success_at is None


# ── Router integration tests ──────────────────────────────────────────────────


@pytest.fixture()
def client_factory():
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

    original_session_local = db_module.SessionLocal
    db_module.SessionLocal = TestingSession
    app.dependency_overrides[get_session] = override_get_session
    clients: list[TestClient] = []

    def factory() -> tuple[TestClient, Any]:
        client = TestClient(app)
        clients.append(client)
        return client, TestingSession

    yield factory

    for c in clients:
        c.close()
    app.dependency_overrides.clear()
    db_module.SessionLocal = original_session_local
    Base.metadata.drop_all(bind=engine)


def _bootstrap_admin(client: TestClient) -> None:
    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPass1!", "display_name": "Admin"},
    )
    assert r.status_code == 200, r.text
    r2 = client.post("/api/auth/login", json={"username": "admin", "password": "AdminPass1!"})
    assert r2.status_code == 200, r2.text


def _create_org_and_wazuh_integration(client: TestClient) -> int:
    r = client.post("/api/organizations/", json={"name": "Org Test"})
    assert r.status_code == 200, r.text
    org_id = r.json()["id"]

    r = client.post("/api/integrations/", json={
        "organization_id": org_id,
        "name": "Wazuh Test",
        "platform": "wazuh",
        "indexer_url": "https://indexer.example.com:9200",
        "indexer_username": "indexer-admin",
        "indexer_password": "indexerpass",
        "manager_url": "https://wazuh.example.com:55000",
        "manager_api_username": "admin",
        "manager_api_password": "secretpass",
    })
    assert r.status_code == 200, r.text
    return r.json()["id"]


class TestHealthEndpointV2:
    def test_v2_is_default(self, client_factory):
        client, Session = client_factory()
        _bootstrap_admin(client)

        mock_result = MagicMock()
        mock_result.status = "healthy"
        mock_result.details = {"manager": {"status": "healthy"}}

        mock_metrics = [
            HealthMetric(id="manager_status", label="Manager", value="healthy", severity="ok"),
        ]

        with (
            patch("backend.app.providers.wazuh.provider.WazuhProvider.health_check", return_value=mock_result),
            patch("backend.app.providers.wazuh.provider.WazuhProvider.get_health_metrics", return_value=mock_metrics),
        ):
            int_id = _create_org_and_wazuh_integration(client)
            r = client.get(f"/api/integrations/{int_id}/health")

        assert r.status_code == 200, r.text
        data = r.json()
        assert data["schema_version"] == 2
        assert data["platform"] == "wazuh"
        assert isinstance(data["metrics"], list)
        assert "X-API-Deprecation" not in r.headers

    def test_v1_via_accept_header(self, client_factory):
        client, Session = client_factory()
        _bootstrap_admin(client)

        mock_result = MagicMock()
        mock_result.status = "healthy"
        mock_result.details = {"manager": {"status": "healthy"}}

        with patch("backend.app.providers.wazuh.provider.WazuhProvider.health_check", return_value=mock_result):
            int_id = _create_org_and_wazuh_integration(client)
            r = client.get(
                f"/api/integrations/{int_id}/health",
                headers={"Accept": "application/vnd.centralops.v1+json"},
            )

        assert r.status_code == 200, r.text
        data = r.json()
        # v1 shape: tem integration_id, status — não tem schema_version=2
        assert "integration_id" in data
        assert data.get("schema_version") != 2
        assert "X-API-Deprecation" in r.headers
        assert "v1" in r.headers["X-API-Deprecation"].lower() or "v1" in r.headers["X-API-Deprecation"]

    def test_v2_metrics_empty_when_provider_raises(self, client_factory):
        """Provider com get_health_metrics levantando exceção → metrics=[] mas response válido."""
        client, Session = client_factory()
        _bootstrap_admin(client)

        mock_result = MagicMock()
        mock_result.status = "error"
        mock_result.details = {}

        with (
            patch("backend.app.providers.wazuh.provider.WazuhProvider.health_check", return_value=mock_result),
            patch("backend.app.providers.wazuh.provider.WazuhProvider.get_health_metrics", side_effect=RuntimeError("boom")),
        ):
            int_id = _create_org_and_wazuh_integration(client)
            r = client.get(f"/api/integrations/{int_id}/health")

        assert r.status_code == 200, r.text
        data = r.json()
        assert data["schema_version"] == 2
        assert data["metrics"] == []

    def test_v2_last_collection_at_from_collection_state(self, client_factory):
        """last_collection_at é populado a partir de CollectionState mesmo com metrics=[]."""
        client, Session = client_factory()
        _bootstrap_admin(client)

        mock_result = MagicMock()
        mock_result.status = "healthy"
        mock_result.details = {}

        with (
            patch("backend.app.providers.wazuh.provider.WazuhProvider.health_check", return_value=mock_result),
            patch("backend.app.providers.wazuh.provider.WazuhProvider.get_health_metrics", return_value=[]),
        ):
            int_id = _create_org_and_wazuh_integration(client)

            # Inserir CollectionState diretamente no banco
            db = Session()
            try:
                ts = datetime(2026, 4, 27, 12, 0, 0)
                state = models.CollectionState(
                    integration_id=int_id,
                    stream="alerts",
                    last_attempt_at=ts,
                    last_success_at=ts,
                )
                db.add(state)
                db.commit()
            finally:
                db.close()

            r = client.get(f"/api/integrations/{int_id}/health")

        assert r.status_code == 200, r.text
        data = r.json()
        assert data["schema_version"] == 2
        assert data["last_collection_at"] is not None
        assert data["metrics"] == []

    def test_v2_requires_auth(self, client_factory):
        client, _ = client_factory()
        r = client.get("/api/integrations/99/health")
        assert r.status_code in (401, 403)


# ── WazuhProvider.get_health_metrics smoke test ───────────────────────────────


class _FakeCred:
    """Linha de credencial fake p/ integration_secrets.read_secret/has_secret.

    Cifra o plaintext com o encrypt REAL (env de teste tem APP_MASTER_KEY),
    para que read_secret(...) -> decrypt(secret_ref) devolva o valor original."""

    def __init__(self, logical_name: str, plaintext: str):
        from backend.app.core.crypto import encrypt as _real_encrypt

        self.logical_name = logical_name
        self.secret_ref = _real_encrypt(plaintext)
        self.revoked_at = None


class TestWazuhProviderGetHealthMetrics:
    def _make_provider(self):
        from backend.app.providers.wazuh.provider import WazuhProvider

        integration = MagicMock()
        integration.id = 1
        integration.platform = "wazuh"
        integration.manager_url = "https://wazuh.example.com:55000"
        # Creds vivem no store vendor-neutro, não em colunas.
        # WazuhProvider lê via integration_secrets.read_secret(self.integration, "<logical>").
        integration.credentials = [
            _FakeCred("manager_api_username", "admin"),
            _FakeCred("manager_api_password", "secretpass"),
        ]
        integration.indexer_url = None
        integration.verify_ssl = True
        integration.organization = MagicMock()
        integration.organization.name = "TestOrg"

        provider = WazuhProvider.__new__(WazuhProvider)
        provider.integration = integration
        provider.db = None
        provider._manager = None
        provider._indexer = None
        return provider

    def test_returns_list(self):
        provider = self._make_provider()

        mock_manager = MagicMock()
        mock_manager.get_manager_status.return_value = {"data": {"affected_items": []}}
        mock_manager.get_agents_summary.return_value = {
            "data": {"connection": {"active": 5, "disconnected": 1}}
        }
        mock_manager.get_cluster_status.return_value = {"data": {"enabled": "no"}}

        with (
            patch.object(provider, "_has_manager_config", return_value=True),
            patch.object(provider, "_has_indexer_config", return_value=False),
            patch.object(provider, "_get_manager", return_value=mock_manager),
        ):
            metrics = provider.get_health_metrics()

        assert isinstance(metrics, list)
        assert len(metrics) > 0
        ids = [m.id for m in metrics]
        assert "manager_status" in ids
        assert "agents_active" in ids
        assert "agents_disconnected" in ids

    def test_manager_error_returns_critical(self):
        provider = self._make_provider()

        with (
            patch.object(provider, "_has_manager_config", return_value=True),
            patch.object(provider, "_has_indexer_config", return_value=False),
            patch.object(provider, "_get_manager", side_effect=RuntimeError("timeout")),
        ):
            metrics = provider.get_health_metrics()

        manager_metric = next((m for m in metrics if m.id == "manager_status"), None)
        assert manager_metric is not None
        assert manager_metric.severity == "critical"

    def test_not_configured_returns_unknown(self):
        provider = self._make_provider()

        with patch.object(provider, "_has_manager_config", return_value=False):
            metrics = provider.get_health_metrics()

        manager_metric = next((m for m in metrics if m.id == "manager_status"), None)
        assert manager_metric is not None
        assert manager_metric.severity == "unknown"
