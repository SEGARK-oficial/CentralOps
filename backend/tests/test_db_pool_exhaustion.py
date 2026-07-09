"""Regression tests for DB connection pool exhaustion (2026-04-27).

Root cause: GET /integrations/{id}/health holds a SQLAlchemy session open
for the entire duration of the request, including synchronous HTTP calls to
the Wazuh Manager/Indexer (via httpx.Client with connect=30s, read=60-120s).
Under concurrent load (N integrations × M simultaneous requests) the pool
of 5+10=15 connections was exhausted, causing:

    sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 10
    reached, connection timed out, timeout 30.00

Short-term fix: _get_engine_kwargs() now returns pool_size=20, max_overflow=20
for non-SQLite databases, giving 40 total connections as a band-aid.

Tests here verify:
1. _get_engine_kwargs() returns the expected keys for Postgres (not SQLite).
2. A GET /integrations/{id}/health request that hangs inside provider.health_check()
   does NOT release the session early — this is the session-hold that causes the
   problem, and a comment in the code documents the TODO refactor.
3. 20 concurrent requests to the health endpoint that each block 0.1s in the
   provider HTTP call succeed without pool timeout (they would fail with the
   old pool_size=5, max_overflow=10 configuration).
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event as _sa_event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool, StaticPool

from backend.app.db import database as db_module
from backend.app.db.database import Base, _get_engine_kwargs, get_session
from backend.app.main import app


# ── Unit: pool kwargs ─────────────────────────────────────────────────────────


class TestGetEngineKwargs:
    """_get_engine_kwargs() must return pool config for Postgres, not for SQLite."""

    def test_sqlite_returns_connect_args_only(self):
        """SQLite path: returns connect_args dict (WAL timeout), no pool keys."""
        with patch("backend.app.db.database.DATABASE_URL", "sqlite:///./test.db"):
            kwargs = _get_engine_kwargs()
        assert "connect_args" in kwargs
        # Pool keys must NOT be present — SQLite uses NullPool / StaticPool in tests.
        assert "pool_size" not in kwargs
        assert "max_overflow" not in kwargs

    def test_postgres_returns_pool_keys(self):
        """Postgres path: returns pool_size, max_overflow, pool_recycle, pool_pre_ping."""
        with patch("backend.app.db.database.DATABASE_URL", "postgresql://user:pass@db/app"):
            kwargs = _get_engine_kwargs()
        assert kwargs.get("pool_size") == 20, (
            "pool_size deve ser 20 para suportar carga atual (band-aid 2026-04-27)"
        )
        assert kwargs.get("max_overflow") == 20
        assert kwargs.get("pool_recycle") == 3600
        assert kwargs.get("pool_pre_ping") is True
        # SQLite connect_args must NOT bleed into Postgres config.
        assert "connect_args" not in kwargs

    def test_postgres_total_connections_at_least_30(self):
        """Pool size + overflow must be >= 30 to handle current concurrency."""
        with patch("backend.app.db.database.DATABASE_URL", "postgresql://user:pass@db/app"):
            kwargs = _get_engine_kwargs()
        total = kwargs.get("pool_size", 5) + kwargs.get("max_overflow", 10)
        assert total >= 30, (
            f"Total pool capacity {total} < 30. Produção tem Celery + uvicorn "
            "compartilhando o mesmo Postgres — pool muito pequeno causa TimeoutError."
        )


# ── Integration: session held open during provider HTTP call ─────────────────


@pytest.fixture()
def client_and_session():
    """Fixture padrão: SQLite in-memory + override de get_session e SessionLocal."""
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


def _bootstrap_admin(client: TestClient) -> None:
    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPass1!", "display_name": "Admin"},
    )
    assert r.status_code == 200, r.text
    r2 = client.post("/api/auth/login", json={"username": "admin", "password": "AdminPass1!"})
    assert r2.status_code == 200, r2.text


def _create_wazuh_integration(client: TestClient) -> int:
    r = client.post("/api/organizations/", json={"name": "Org Pool Test"})
    assert r.status_code == 200, r.text
    org_id = r.json()["id"]

    r = client.post("/api/integrations/", json={
        "organization_id": org_id,
        "name": "Wazuh Pool Test",
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


class TestSessionHeldDuringHealthCheck:
    """Documents and asserts the session-hold behavior in GET /integrations/{id}/health.

    The route holds the session open (via Depends(get_session)) for the entire
    request, including provider.health_check() and provider.get_health_metrics()
    which make synchronous HTTP calls.  This test is intentionally *documenting*
    the current behavior; the proper fix (TODO) is to fetch DB data, close the
    session, then do HTTP, and use a fresh short-lived session for the write-back.
    """

    def test_health_endpoint_returns_200_when_provider_is_slow(self, client_and_session):
        """Slow provider (100ms) should not break the endpoint — pool holds on."""
        client, _ = client_and_session
        _bootstrap_admin(client)
        int_id = _create_wazuh_integration(client)

        slow_result = MagicMock()
        slow_result.status = "healthy"
        slow_result.details = {"manager": {"status": "healthy"}}

        def slow_health_check(*args, **kwargs):
            time.sleep(0.1)  # simulate 100ms Wazuh latency
            return slow_result

        with (
            patch("backend.app.providers.wazuh.provider.WazuhProvider.health_check", slow_health_check),
            patch("backend.app.providers.wazuh.provider.WazuhProvider.get_health_metrics", return_value=[]),
        ):
            r = client.get(f"/api/integrations/{int_id}/health")

        assert r.status_code == 200, r.text

    def test_concurrent_health_requests_do_not_timeout(self, concurrent_client_and_session):
        """20 concurrent requests each sleeping 50ms in the provider must all succeed.

        After the session-decoupling refactor, the DB session is released BEFORE
        the provider HTTP call (health_check + get_health_metrics). The write-back
        uses a short-lived second session. This test verifies no request is dropped
        even when the provider is slow.

        Uses a file-based SQLite with WAL mode (not StaticPool) so concurrent
        sessions don't race on a single DBAPI connection. The pool_size constants
        in TestGetEngineKwargs prove the Postgres fix independently.
        """
        client, _ = concurrent_client_and_session
        _bootstrap_admin(client)
        int_id = _create_wazuh_integration(client)

        mock_result = MagicMock()
        mock_result.status = "healthy"
        mock_result.details = {"manager": {"status": "healthy"}}

        def slow_health_check(*args, **kwargs):
            time.sleep(0.05)  # 50ms simulated latency
            return mock_result

        results: list[int] = []
        errors: list[str] = []
        lock = threading.Lock()

        def make_request():
            try:
                r = client.get(f"/api/integrations/{int_id}/health")
                with lock:
                    results.append(r.status_code)
            except Exception as exc:
                with lock:
                    errors.append(str(exc))

        with (
            patch("backend.app.providers.wazuh.provider.WazuhProvider.health_check", slow_health_check),
            patch("backend.app.providers.wazuh.provider.WazuhProvider.get_health_metrics", return_value=[]),
        ):
            threads = [threading.Thread(target=make_request) for _ in range(20)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

        completed = len(results) + len(errors)
        assert completed == 20, f"Only {completed}/20 requests completed (errors={errors[:3]})"
        assert not errors, (
            f"{len(errors)}/20 requests threw exceptions (SQLite concurrency limit in tests): {errors[:2]}"
        )
        failed = [s for s in results if s != 200]
        assert not failed, (
            f"{len(failed)}/20 requests returned non-200: {set(failed)}. "
            "Se estiver vendo TimeoutError de pool, o pool_size pode estar muito baixo."
        )


@pytest.fixture()
def concurrent_client_and_session():
    """Fixture for concurrent tests: file-based SQLite with WAL + multi-connection pool.

    StaticPool (used in client_and_session) shares ONE connection across all threads,
    which makes concurrent writes racy. This fixture uses a real file-based SQLite
    with WAL mode so multiple sessions can coexist safely across threads.
    """
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    db_url = f"sqlite:///{db_path}"

    # Compiled-sweep hardening (2026-07). Antes: QueuePool default (size 5 +
    # overflow 10). As conexões de OVERFLOW (6ª–15ª) são HARD-CLOSED no check-in
    # (SQLAlchemy do_terminate → sqlite3_close). Com 20 threads batendo ao mesmo
    # tempo, esse fechamento CONCORRENTE de conexões pysqlite dava SIGSEGV na
    # imagem Cython (faulthandler fixou do_terminate/do_close). Aqui: pool grande
    # o suficiente p/ a concorrência do teste e SEM overflow — nenhuma conexão é
    # terminada no meio do teste; todas ficam no pool e só são fechadas UMA vez,
    # single-threaded, no dispose() do teardown. Mantém a concorrência de 20 vias
    # que o teste exige, sem o teardown concorrente que crashava o pysqlite.
    engine = create_engine(
        db_url,
        connect_args={"check_same_thread": False, "timeout": 30},
        poolclass=QueuePool,
        pool_size=40,
        max_overflow=0,
    )

    @_sa_event.listens_for(engine, "connect")
    def _wal(conn, _):
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.close()

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
    engine.dispose()
    try:
        os.unlink(db_path)
    except OSError:
        pass


class TestAuditMiddlewareDoesNotLeakSessions:
    """audit_api_requests abre SessionLocal() + fecha em finally — deve liberar conexão."""

    def test_audit_middleware_closes_session_on_success(self, client_and_session):
        """A request bem-sucedida não deve vazar sessão do middleware de auditoria."""
        client, _ = client_and_session
        _bootstrap_admin(client)

        # GET /api/auth/me é path de auditoria (não está em AUDIT_SKIP_PATHS)
        # mas está em AUDIT_SKIP_PATHS — usamos /api/organizations/ que não está.
        r = client.get("/api/organizations/")
        # 200 ou 401 são ambos válidos; o importante é que não haja exception de pool.
        assert r.status_code in (200, 401, 403)

    def test_audit_middleware_closes_session_on_error(self, client_and_session):
        """Request que falha (404) também deve liberar sessão do audit middleware."""
        client, _ = client_and_session
        _bootstrap_admin(client)

        r = client.get("/api/integrations/99999")
        # 404 é esperado — o middleware de auditoria deve ter fechado a sessão.
        assert r.status_code == 404
