"""Testes para MAJOR 1+2 — quarantine reprocess hotfixes (F5-S5).

MAJOR 1: assert result.envelope is not None substituído por HTTPException 500.
MAJOR 2: enqueue-before-mark: enqueue PRIMEIRO, marca reprocessed_at SOMENTE
         se enqueue retornar sem exceção.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import models
from backend.app.db.database import Base, get_session
from backend.app.main import app


# ── Fixture de banco ──────────────────────────────────────────────────


@pytest.fixture()
def client_factory():
    """TestClient + SQLite in-memory."""
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

    def factory(raise_server_exceptions: bool = True) -> TestClient:
        c = TestClient(app, raise_server_exceptions=raise_server_exceptions)
        clients.append(c)
        return c

    yield factory, TestingSessionLocal

    for c in clients:
        c.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


# ── Helpers ────────────────────────────────────────────────────────────


def _bootstrap_and_login(client: TestClient) -> None:
    r = client.post(
        "/api/auth/bootstrap",
        json={
            "username": "admin",
            "password": "AdminPass123!",
            "display_name": "Admin",
        },
    )
    assert r.status_code == 200, r.text
    r = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "AdminPass123!"},
    )
    assert r.status_code == 200, r.text


def _seed_quarantine_event(
    session_factory,
    *,
    vendor: str = "sophos",
    event_type: str = "sophos.alert",
    expires_days: int = 7,
) -> str:
    """Cria QuarantineEvent e retorna o ID."""
    with session_factory() as db:
        org = models.Organization(
            name=f"Org {uuid4().hex[:6]}",
            slug=f"org-{uuid4().hex[:8]}",
            is_active=True,
        )
        db.add(org)
        db.flush()

        intg = models.Integration(
            organization_id=org.id,
            name="Test Integration",
            platform=vendor,
        )
        db.add(intg)
        db.flush()

        ev = models.QuarantineEvent(
            integration_id=intg.id,
            vendor=vendor,
            event_type=event_type,
            raw_payload=json.dumps({"id": str(uuid4()), "event": "test"}),
            error_kind="map",
            created_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(days=expires_days),
            reprocessed_at=None,
        )
        db.add(ev)
        db.commit()
        return ev.id


# ── MAJOR 1: envelope=None → HTTPException 500 ────────────────────────


def test_reprocess_raises_500_when_envelope_is_none(client_factory) -> None:
    """Quando attempt_reprocess retorna success=True mas envelope=None,
    deve levantar HTTP 500 (não usar assert que desaparece com -O).
    """
    factory, Session = client_factory
    client = factory()
    _bootstrap_and_login(client)

    event_id = _seed_quarantine_event(Session)

    # Mock attempt_reprocess retornando success=True mas envelope=None.
    mock_result = MagicMock()
    mock_result.success = True
    mock_result.envelope = None  # bug simulado
    mock_result.error_kind = None
    mock_result.error_detail = None
    mock_result.mapping_version_id = "v1"

    with patch(
        "backend.app.collectors.normalize.reprocess.attempt_reprocess",
        return_value=mock_result,
    ):
        r = client.post(f"/api/quarantine/{event_id}/reprocess")

    assert r.status_code == 500, (
        f"HTTP 500 esperado para envelope=None, obtido {r.status_code}: {r.text}"
    )
    assert "Envelope nulo" in r.json()["detail"]


# ── MAJOR 2: enqueue falha → reprocessed_at permanece null ──────────


def test_reprocess_reprocessed_at_null_when_enqueue_fails(client_factory) -> None:
    """Se _enqueue_reprocess_dispatch lançar exceção, reprocessed_at deve
    permanecer null (o evento fica na quarentena para nova tentativa).
    """
    factory, Session = client_factory
    # raise_server_exceptions=False permite inspecionar resposta HTTP 500
    # sem o TestClient propagar a exceção do servidor.
    client = factory(raise_server_exceptions=False)
    _bootstrap_and_login(client)

    event_id = _seed_quarantine_event(Session)

    # Mock attempt_reprocess retornando sucesso com envelope real.
    mock_result = MagicMock()
    mock_result.success = True
    mock_result.envelope = {"_centralops": {"event_id": "evt-123", "vendor": "sophos"}}
    mock_result.error_kind = None
    mock_result.error_detail = None
    mock_result.mapping_version_id = "v1"

    # _enqueue_reprocess_dispatch lança exceção (broker offline).
    with patch(
        "backend.app.collectors.normalize.reprocess.attempt_reprocess",
        return_value=mock_result,
    ):
        with patch(
            "backend.app.routers.quarantine._enqueue_reprocess_dispatch",
            side_effect=RuntimeError("Broker offline"),
        ):
            r = client.post(f"/api/quarantine/{event_id}/reprocess")

    # Endpoint deve retornar erro (não 200).
    assert r.status_code != 200, (
        "Endpoint não deveria retornar 200 quando enqueue falha"
    )

    # reprocessed_at deve permanecer null no banco.
    with Session() as db:
        ev = db.get(models.QuarantineEvent, event_id)
        assert ev is not None
        assert ev.reprocessed_at is None, (
            f"reprocessed_at deveria ser None quando enqueue falha, "
            f"mas está: {ev.reprocessed_at}"
        )


def test_reprocess_marks_reprocessed_at_only_after_successful_enqueue(
    client_factory,
) -> None:
    """Quando enqueue é bem-sucedido, reprocessed_at deve ser preenchido."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_and_login(client)

    event_id = _seed_quarantine_event(Session)

    mock_result = MagicMock()
    mock_result.success = True
    mock_result.envelope = {"_centralops": {"event_id": "evt-456", "vendor": "sophos"}}
    mock_result.error_kind = None
    mock_result.error_detail = None
    mock_result.mapping_version_id = "v2"

    with patch(
        "backend.app.collectors.normalize.reprocess.attempt_reprocess",
        return_value=mock_result,
    ):
        with patch(
            "backend.app.routers.quarantine._enqueue_reprocess_dispatch"
        ):
            r = client.post(f"/api/quarantine/{event_id}/reprocess")

    assert r.status_code == 200, r.text

    with Session() as db:
        ev = db.get(models.QuarantineEvent, event_id)
        assert ev is not None
        assert ev.reprocessed_at is not None, (
            "reprocessed_at deve ser preenchido após enqueue bem-sucedido"
        )
