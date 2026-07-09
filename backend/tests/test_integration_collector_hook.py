"""Testes do hook on-create/on-delete de coleta (Fase 1.3).

Cobre:
- _trigger_initial_collection chama apply_async com args corretos por stream.
- Fila collect.priority usa task collect_vendor_logs_priority.
- Fila collect.bulk usa task collect_vendor_logs_bulk.
- Falha em apply_async não propaga (fire-and-forget).
- _register_in_beat e _deregister_from_beat não propagam exceções.
- POST /integrations retorna 200 mesmo se os hooks falharem.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

from backend.app.db.database import Base, get_session
from backend.app.main import app


# ── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture()
def db_client():
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

    with TestClient(app) as client:
        yield client

    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


def _bootstrap_admin(client: TestClient) -> None:
    resp = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPassword123!", "display_name": "Admin"},
    )
    assert resp.status_code == 200, resp.text


def _create_org(client: TestClient) -> int:
    resp = client.post("/api/organizations/", json={"name": "Test Org"})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _wazuh_payload(org_id: int, name: str = "Wazuh Test") -> dict[str, Any]:
    return {
        "organization_id": org_id,
        "name": name,
        "platform": "wazuh",
        "indexer_url": "https://indexer.example.com:9200",
        "indexer_username": "indexer-user",
        "indexer_password": "indexer-pass",
        "manager_url": "manager.example.com:55000",
        "manager_api_username": "user",
        "manager_api_password": "pass",
        "verify_ssl": False,
    }


def _make_fake_reg(stream: str, queue: str = "collect.bulk") -> MagicMock:
    reg = MagicMock()
    reg.stream = stream
    reg.queue = queue
    return reg


# ── Tests: _trigger_initial_collection ───────────────────────────────

class TestTriggerInitialCollection:

    def _call(self, streams, mock_bulk, mock_priority):
        """Chama _trigger_initial_collection com mocks injetados."""
        from backend.app.routers import integrations as router_mod

        # Patches no namespace do módulo (onde os nomes são resolvidos).
        with (
            patch.object(router_mod, "_iter_for_platform_in_trigger", create=True, side_effect=None),
        ):
            # Como iter_for_platform é importado lazy dentro da função,
            # patchamos o caminho absoluto do módulo de origem.
            with (
                patch(
                    "backend.app.collectors.registry.iter_for_platform",
                    return_value=iter(streams),
                ),
                patch(
                    "backend.app.collectors.tasks.collect_vendor_logs_bulk",
                    mock_bulk,
                ),
                patch(
                    "backend.app.collectors.tasks.collect_vendor_logs_priority",
                    mock_priority,
                ),
            ):
                router_mod._trigger_initial_collection(42, "wazuh")

    def test_bulk_queue_uses_bulk_task(self):
        reg = _make_fake_reg("alerts", queue="collect.bulk")
        mock_bulk = MagicMock()
        mock_priority = MagicMock()

        # A função importa lazy de collectors — patchamos direto os módulos.
        with (
            patch("backend.app.collectors.registry.iter_for_platform", return_value=iter([reg])),
            patch("backend.app.collectors.tasks.collect_vendor_logs_bulk", mock_bulk),
            patch("backend.app.collectors.tasks.collect_vendor_logs_priority", mock_priority),
        ):
            from backend.app.routers.integrations import _trigger_initial_collection
            _trigger_initial_collection(42, "wazuh")

        mock_bulk.apply_async.assert_called_once_with(
            args=[42, "alerts"],
            countdown=5,
            queue="collect.bulk",
        )
        mock_priority.apply_async.assert_not_called()

    def test_priority_queue_uses_priority_task(self):
        reg = _make_fake_reg("incidents", queue="collect.priority")
        mock_bulk = MagicMock()
        mock_priority = MagicMock()

        with (
            patch("backend.app.collectors.registry.iter_for_platform", return_value=iter([reg])),
            patch("backend.app.collectors.tasks.collect_vendor_logs_bulk", mock_bulk),
            patch("backend.app.collectors.tasks.collect_vendor_logs_priority", mock_priority),
        ):
            from backend.app.routers.integrations import _trigger_initial_collection
            _trigger_initial_collection(7, "sophos")

        mock_priority.apply_async.assert_called_once_with(
            args=[7, "incidents"],
            countdown=5,
            queue="collect.priority",
        )
        mock_bulk.apply_async.assert_not_called()

    def test_apply_async_failure_does_not_propagate(self):
        """ConnectionError em apply_async é silenciado."""
        reg = _make_fake_reg("alerts", queue="collect.bulk")
        mock_bulk = MagicMock()
        mock_bulk.apply_async.side_effect = ConnectionError("Redis down")
        mock_priority = MagicMock()

        with (
            patch("backend.app.collectors.registry.iter_for_platform", return_value=iter([reg])),
            patch("backend.app.collectors.tasks.collect_vendor_logs_bulk", mock_bulk),
            patch("backend.app.collectors.tasks.collect_vendor_logs_priority", mock_priority),
        ):
            from backend.app.routers.integrations import _trigger_initial_collection
            _trigger_initial_collection(99, "wazuh")  # não deve levantar

    def test_multiple_streams_enqueue_each(self):
        """Dois streams = dois apply_async."""
        reg_a = _make_fake_reg("alerts", queue="collect.bulk")
        reg_b = _make_fake_reg("detections", queue="collect.bulk")
        mock_bulk = MagicMock()
        mock_priority = MagicMock()

        with (
            patch("backend.app.collectors.registry.iter_for_platform", return_value=iter([reg_a, reg_b])),
            patch("backend.app.collectors.tasks.collect_vendor_logs_bulk", mock_bulk),
            patch("backend.app.collectors.tasks.collect_vendor_logs_priority", mock_priority),
        ):
            from backend.app.routers.integrations import _trigger_initial_collection
            _trigger_initial_collection(5, "wazuh")

        assert mock_bulk.apply_async.call_count == 2


# ── Tests: _register_in_beat / _deregister_from_beat ─────────────────

class TestBeatHooks:

    def test_register_in_beat_does_not_raise_on_failure(self):
        with patch(
            "backend.app.collectors.scheduler.register_integration_in_beat",
            side_effect=Exception("RedBeat unavailable"),
        ):
            from backend.app.routers.integrations import _register_in_beat
            _register_in_beat(1)  # não deve levantar

    def test_deregister_from_beat_does_not_raise_on_failure(self):
        with patch(
            "backend.app.collectors.scheduler.deregister_integration_from_beat",
            side_effect=Exception("RedBeat unavailable"),
        ):
            from backend.app.routers.integrations import _deregister_from_beat
            _deregister_from_beat(1)  # não deve levantar


# ── Tests: POST retorna 200 mesmo com falhas nos hooks ────────────────

class TestPostIntegrationWithHooks:

    def test_returns_200_even_if_trigger_raises(self, db_client):
        """_trigger_initial_collection que levanta não deve causar 500."""
        _bootstrap_admin(db_client)
        org_id = _create_org(db_client)

        with (
            patch(
                "backend.app.routers.integrations._trigger_initial_collection",
                side_effect=ConnectionError("Redis down"),
            ),
            patch("backend.app.routers.integrations._register_in_beat"),
        ):
            resp = db_client.post("/api/integrations/", json=_wazuh_payload(org_id))

        # _trigger_initial_collection já é silenciosa internamente,
        # mas mesmo se o wrapper falhar, o except externo protege.
        assert resp.status_code == 200
        assert resp.json()["platform"] == "wazuh"

    def test_returns_200_even_if_register_in_beat_raises(self, db_client):
        """_register_in_beat que levanta não deve causar 500."""
        _bootstrap_admin(db_client)
        org_id = _create_org(db_client)

        with (
            patch("backend.app.routers.integrations._trigger_initial_collection"),
            patch(
                "backend.app.routers.integrations._register_in_beat",
                side_effect=RuntimeError("unexpected"),
            ),
        ):
            resp = db_client.post("/api/integrations/", json=_wazuh_payload(org_id))

        assert resp.status_code == 200
