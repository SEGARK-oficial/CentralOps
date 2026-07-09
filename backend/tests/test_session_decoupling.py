"""Tests for the session-lifetime decoupling refactor.

Verifies three guarantees introduced in the refactor:
1. The read session is closed (expunge_all + close) before the provider HTTP call.
2. _collect_integration_health uses at most 3 queries for N integrations (no N+1).
3. SophosProvider._full_reauth opens its own SessionLocal during token refresh.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event as _sa_event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import database as db_module
from backend.app.db import models
from backend.app.db.database import Base, get_session
from backend.app.db.repository import IntegrationHealthRepository
from backend.app.main import app


# ── Shared fixtures ───────────────────────────────────────────────────────────


@pytest.fixture()
def mem_client():
    """SQLite in-memory + StaticPool; overrides get_session AND db_module.SessionLocal."""
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
    client = TestClient(app)
    yield client, TestingSession
    client.close()
    app.dependency_overrides.clear()
    db_module.SessionLocal = original_session_local
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def wal_session():
    """File-based SQLite with WAL mode; yields (TestingSession, db) for direct repo tests."""
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @_sa_event.listens_for(engine, "connect")
    def _wal(conn, _):
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    db = TestingSession()
    yield TestingSession, db
    db.close()
    Base.metadata.drop_all(bind=engine)
    engine.dispose()
    try:
        os.unlink(db_path)
    except OSError:
        pass


def _bootstrap_admin(client: TestClient) -> None:
    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPass1!", "display_name": "Admin"},
    )
    assert r.status_code == 200, r.text
    r2 = client.post("/api/auth/login", json={"username": "admin", "password": "AdminPass1!"})
    assert r2.status_code == 200, r2.text


def _create_wazuh_integration(client: TestClient, org_id: int) -> int:
    r = client.post("/api/integrations/", json={
        "organization_id": org_id,
        "name": f"Wazuh {org_id}",
        "platform": "wazuh",
        # Indexer é obrigatório (fonte); Manager é opcional add-on.
        "indexer_url": "https://wazuh.example.com:9200",
        "indexer_username": "admin",
        "indexer_password": "secretpass",
        "manager_url": "https://wazuh.example.com:55000",
        "manager_api_username": "admin",
        "manager_api_password": "secretpass",
    })
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _create_org(client: TestClient, name: str) -> int:
    r = client.post("/api/organizations/", json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()["id"]


# ── Test 1: session released before HTTP call ─────────────────────────────────


class TestSessionReleasedBeforeHttpCall:
    """The DB session must be closed (via expunge_all + close) BEFORE the provider
    makes any HTTP call. This prevents pool exhaustion under concurrent load.
    """

    def test_session_released_before_http_call(self, mem_client):
        """db.close() is called before provider.health_check() returns."""
        client, TestingSession = mem_client
        _bootstrap_admin(client)
        org_id = _create_org(client, "Org Session Test")
        int_id = _create_wazuh_integration(client, org_id)

        mock_result = MagicMock()
        mock_result.status = "healthy"
        mock_result.details = {"manager": {"status": "healthy"}}

        # Track Session.close calls with a spy
        close_calls: list[float] = []
        health_check_start: list[float] = []

        import time

        def spy_health_check(self, *args, **kwargs):
            health_check_start.append(time.monotonic())
            return mock_result

        # Patch Session.close to record call time
        original_session_close = TestingSession().__class__.close

        def spy_close(session_self):
            close_calls.append(time.monotonic())
            return original_session_close(session_self)

        with (
            patch("backend.app.providers.wazuh.provider.WazuhProvider.health_check", spy_health_check),
            patch("backend.app.providers.wazuh.provider.WazuhProvider.get_health_metrics", return_value=[]),
            patch("backend.app.routers.integrations.database.SessionLocal"),
            patch.object(TestingSession().__class__, "close", spy_close),
        ):
            r = client.get(f"/api/integrations/{int_id}/health")

        assert r.status_code == 200, r.text
        assert health_check_start, "health_check was not called"
        assert close_calls, "Session.close was not called"
        # The session close MUST happen before health_check is entered
        first_close = min(close_calls)
        first_health = min(health_check_start)
        assert first_close <= first_health, (
            f"Session was closed at {first_close:.4f} but health_check started at "
            f"{first_health:.4f} — session was NOT released before HTTP call"
        )

    def test_health_endpoint_returns_200_with_session_decoupled(self, mem_client):
        """Basic smoke test: /health returns 200 after the session decoupling."""
        client, _ = mem_client
        _bootstrap_admin(client)
        org_id = _create_org(client, "Org Smoke")
        int_id = _create_wazuh_integration(client, org_id)

        mock_result = MagicMock()
        mock_result.status = "healthy"
        mock_result.details = {"manager": {"status": "healthy"}}

        with (
            patch("backend.app.providers.wazuh.provider.WazuhProvider.health_check", return_value=mock_result),
            patch("backend.app.providers.wazuh.provider.WazuhProvider.get_health_metrics", return_value=[]),
            patch("backend.app.routers.integrations.database.SessionLocal"),
        ):
            r = client.get(f"/api/integrations/{int_id}/health")

        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("platform") == "wazuh"


# ── Test 2: no N+1 queries in _collect_integration_health ────────────────────


class TestCollectIntegrationHealthNoNPlus1:
    """_collect_integration_health must use at most 3 queries for N integrations.

    Specifically:
    - 1 query for integrations (with selectinload for organization)
    - 1 query for get_latest_bulk (MAX(id) GROUP BY integration_id)
    - 1 query for get_latest_before_bulk (same with checked_at filter)

    The old implementation issued 2 queries per integration (N+1 × 2 = 2N+1).
    """

    def test_collect_integration_health_no_n_plus_1(self, wal_session):
        """With 5 integrations, confirm at most 3 queries in _collect_integration_health."""
        TestingSession, db = wal_session

        # Seed data
        org = models.Organization(name="BulkOrg", slug="bulk-org")
        db.add(org)
        db.commit()
        db.refresh(org)

        integrations = []
        for i in range(5):
            integ = models.Integration(
                organization_id=org.id,
                name=f"Wazuh {i}",
                platform="wazuh",
            )
            db.add(integ)
        db.commit()

        all_int_ids = [
            row[0]
            for row in db.query(models.Integration.id)
            .filter(models.Integration.organization_id == org.id)
            .all()
        ]

        now = datetime.utcnow()
        for int_id in all_int_ids:
            # Add older check first (lower id) then newer check (higher id = latest)
            db.add(models.IntegrationHealthCheck(
                integration_id=int_id,
                status="degraded",
                checked_at=now - timedelta(hours=1),
                details="{}",
            ))
            db.add(models.IntegrationHealthCheck(
                integration_id=int_id,
                status="healthy",
                checked_at=now,
                details="{}",
            ))
        db.commit()

        # Load integrations AFTER final commit so expire_on_commit doesn't trigger re-loads
        integrations = db.query(models.Integration).filter(
            models.Integration.organization_id == org.id
        ).all()

        # Count queries
        query_count = [0]
        query_stmts: list[str] = []

        @_sa_event.listens_for(db.bind, "before_cursor_execute")
        def count_queries(conn, cursor, statement, parameters, context, executemany):
            query_count[0] += 1
            query_stmts.append(str(statement)[:120])

        health_repo = IntegrationHealthRepository(db)
        comparison_anchor = now - timedelta(days=7)

        from backend.app.routers.dashboard import _collect_integration_health
        result = _collect_integration_health(
            integrations,
            health_repo=health_repo,
            comparison_anchor=comparison_anchor,
        )

        # bulk methods issue exactly 2 queries; the integration list was already loaded
        assert query_count[0] <= 3, (
            f"Expected at most 3 queries, got {query_count[0]}. "
            f"Queries:\n" + "\n".join(f"  {i+1}: {q}" for i, q in enumerate(query_stmts))
        )
        assert result["healthy_count"] == 5
        assert result["degraded_count"] == 0


# ── Test 3: SophosProvider._full_reauth opens its own session ─────────────────


class TestSophosTokenRefreshOpensOwnSession:
    """SophosProvider._full_reauth must open a new SessionLocal() session
    independently of any caller-provided session, so token writes happen
    even when the original session has already been closed.
    """

    def test_sophos_token_refresh_opens_own_session(self):
        """_full_reauth opens its own SessionLocal() to persist tokens (store).

        Integração detached ⇒ ``_persist_tokens`` abre uma
        ``SessionLocal`` própria e grava no store via ``write_secret`` (não mais
        via colunas/IntegrationRepository), preservando o desacoplamento.
        """
        from backend.app.db.models import Integration as IntegrationModel
        from backend.app.providers.sophos.provider import SophosProvider
        from backend.app.services import integration_secrets

        integ = MagicMock(spec=IntegrationModel)
        integ.id = 42
        integ.client_id = "test-client"
        integ.tenant_id = "tenant-123"
        integ.region = None
        integ.credentials = []  # store (mirror in-memory escreve aqui)

        provider = SophosProvider(integ)

        mock_row = MagicMock()
        mock_row.credentials = []  # row re-fetchada na sessão própria
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.get.return_value = mock_row
        mock_session_local = MagicMock(return_value=mock_session)

        mock_auth = MagicMock()
        mock_auth.authenticate.return_value = {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
        }
        mock_auth.discover_region_and_tenant.return_value = ("us-west-2", "tenant-xyz")

        with (
            patch.object(provider, "_get_auth_service", return_value=mock_auth),
            patch("backend.app.providers.sophos.provider._db_module.SessionLocal", mock_session_local),
        ):
            headers = provider._full_reauth()

        # SessionLocal must have been called (new session opened)
        mock_session_local.assert_called_once()
        # Tokens persistidos no store da row re-fetchada na sessão própria
        assert integration_secrets.read_secret(mock_row, "access_token") == "new-access"
        assert integration_secrets.read_secret(mock_row, "refresh_token") == "new-refresh"
        assert mock_row.region == "us-west-2"
        assert mock_row.tenant_id == "tenant-xyz"
        # Headers must contain the new access token
        assert headers.get("Authorization") == "Bearer new-access"

    def test_sophos_token_refresh_updates_integration_in_memory(self):
        """After _full_reauth, integration attributes are updated in-memory."""
        from backend.app.db.models import Integration as IntegrationModel
        from backend.app.providers.sophos.provider import SophosProvider
        from backend.app.services import integration_secrets

        integ = MagicMock(spec=IntegrationModel)
        integ.id = 99
        integ.client_id = "cid"
        integ.tenant_id = "t99"
        integ.region = None
        integ.credentials = []

        provider = SophosProvider(integ)

        mock_row = MagicMock()
        mock_row.credentials = []
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.get.return_value = mock_row

        mock_auth = MagicMock()
        mock_auth.authenticate.return_value = {"access_token": "at", "refresh_token": "rt"}
        mock_auth.discover_region_and_tenant.return_value = ("eu-west-1", "t-new")

        with (
            patch.object(provider, "_get_auth_service", return_value=mock_auth),
            patch("backend.app.providers.sophos.provider._db_module.SessionLocal", return_value=mock_session),
        ):
            provider._full_reauth()

        # Colunas não-secret espelhadas in-memory; tokens no store da própria integração.
        assert provider.integration.region == "eu-west-1"
        assert provider.integration.tenant_id == "t-new"
        assert integration_secrets.read_secret(provider.integration, "access_token") == "at"
        assert integration_secrets.read_secret(provider.integration, "refresh_token") == "rt"
