"""Testes unitários do worker Celery de backfill (RF2.4).

Cobertura:
- Transição de status: pending → running → completed.
- Cancelamento detectado em meio à execução.
- Persistência de cursor e progresso após cada stream.
- Erro durante coleta → status="failed", last_error preenchido.

O worker é testado com chamada direta via ``collect_backfill_job.run(job_id=...)``,
sem broker Celery real.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import models
from backend.app.db.database import Base


@pytest.fixture()
def db_session():
    """Banco SQLite in-memory isolado para cada teste."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(
        autocommit=False, autoflush=False, bind=engine
    )
    Base.metadata.create_all(bind=engine)

    # Patch do SessionLocal que o worker usa diretamente.
    with patch(
        "backend.app.collectors.backfill_tasks.database.SessionLocal",
        return_value=TestingSessionLocal(),
    ):
        yield TestingSessionLocal

    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def session_factory():
    """Retorna factory de sessões para criação/verificação de dados nos testes."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(
        autocommit=False, autoflush=False, bind=engine
    )
    Base.metadata.create_all(bind=engine)
    yield engine, TestingSessionLocal
    Base.metadata.drop_all(bind=engine)


def _make_job(
    session,
    *,
    status: str = "pending",
    streams: list[str] | None = None,
    days_back: int = 7,
    integration_id: int | None = None,
) -> models.BackfillJob:
    """Cria BackfillJob diretamente no banco."""
    now = datetime.utcnow()

    # Cria org + integration se não fornecida.
    if integration_id is None:
        org = models.Organization(
            name=f"Worker Test Org {uuid4().hex[:6]}",
            slug=f"worker-test-{uuid4().hex[:6]}",
        )
        session.add(org)
        session.flush()
        integration = models.Integration(
            organization_id=org.id,
            name="Worker Test Integration",
            platform="sophos",
            is_active=True,
        )
        session.add(integration)
        session.flush()
        integration_id = integration.id

    job = models.BackfillJob(
        integration_id=integration_id,
        streams=json.dumps(streams or ["alerts"]),
        from_ts=now - timedelta(days=days_back),
        to_ts=now - timedelta(days=1),
        status=status,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


# ── Helpers ───────────────────────────────────────────────────────────

_MOCK_PIPELINE_RESULT = {
    "cursor": {"page": 2},
    "events_collected": 10,
    "events_dispatched": 10,
}


def _make_pipeline_mock(result: dict | None = None) -> AsyncMock:
    """Mock de run_backfill_collection_once que retorna resultado padrão."""
    mock = AsyncMock(return_value=result or _MOCK_PIPELINE_RESULT)
    return mock


# ── Testes ────────────────────────────────────────────────────────────


def test_collect_backfill_job_marks_running_then_completed(
    session_factory,
) -> None:
    """Worker deve transitar pending → running → completed."""
    engine, TestingSessionLocal = session_factory

    with TestingSessionLocal() as db:
        job = _make_job(db, status="pending", streams=["alerts"])
        job_id = job.id

    pipeline_mock = _make_pipeline_mock()

    with patch(
        "backend.app.collectors.backfill_tasks.database.SessionLocal",
        TestingSessionLocal,
    ), patch(
        "backend.app.collectors.backfill_tasks.run_backfill_collection_once",
        new=pipeline_mock,
    ):
        from backend.app.collectors.backfill_tasks import collect_backfill_job

        result = collect_backfill_job.run(job_id=job_id)

    assert result["status"] == "completed"
    assert result["events_collected"] == 10
    assert result["events_dispatched"] == 10

    with TestingSessionLocal() as db:
        fresh = db.get(models.BackfillJob, job_id)
        assert fresh is not None
        assert fresh.status == "completed"
        assert fresh.started_at is not None
        assert fresh.finished_at is not None
        assert fresh.progress_pct == 100
        assert fresh.events_collected == 10
        assert fresh.events_dispatched == 10


def test_collect_backfill_job_handles_cancellation_mid_run(
    session_factory,
) -> None:
    """Worker deve sair limpo quando status muda para 'cancelled' entre streams."""
    engine, TestingSessionLocal = session_factory

    with TestingSessionLocal() as db:
        job = _make_job(db, status="pending", streams=["alerts", "incidents"])
        job_id = job.id

    call_count = 0

    async def _pipeline_that_cancels_after_first(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Simula cancelamento externo entre o 1º e 2º stream.
            with TestingSessionLocal() as db:
                fresh = db.get(models.BackfillJob, job_id)
                fresh.status = "cancelled"
                fresh.cancelled_at = datetime.utcnow()
                db.commit()
        return _MOCK_PIPELINE_RESULT

    with patch(
        "backend.app.collectors.backfill_tasks.database.SessionLocal",
        TestingSessionLocal,
    ), patch(
        "backend.app.collectors.backfill_tasks.run_backfill_collection_once",
        side_effect=_pipeline_that_cancels_after_first,
    ):
        from backend.app.collectors.backfill_tasks import collect_backfill_job

        result = collect_backfill_job.run(job_id=job_id)

    # Retornou "cancelled" porque detectou no início do 2º stream.
    assert result["status"] == "cancelled"
    # Pipeline só foi chamado 1 vez (1º stream) — 2º foi abortado.
    assert call_count == 1

    with TestingSessionLocal() as db:
        fresh = db.get(models.BackfillJob, job_id)
        assert fresh is not None
        assert fresh.status == "cancelled"


def test_collect_backfill_job_persists_cursor_progress(
    session_factory,
) -> None:
    """Cursor e contadores devem ser salvos no banco após cada stream."""
    engine, TestingSessionLocal = session_factory

    with TestingSessionLocal() as db:
        job = _make_job(db, status="pending", streams=["alerts", "incidents"])
        job_id = job.id

    cursors_returned = [
        {"page": 1, "token": "abc"},
        {"page": 2, "token": "def"},
    ]
    call_index = [0]

    async def _pipeline_with_cursor(*args, **kwargs):
        idx = call_index[0]
        call_index[0] += 1
        return {
            "cursor": cursors_returned[idx],
            "events_collected": 5,
            "events_dispatched": 5,
        }

    with patch(
        "backend.app.collectors.backfill_tasks.database.SessionLocal",
        TestingSessionLocal,
    ), patch(
        "backend.app.collectors.backfill_tasks.run_backfill_collection_once",
        side_effect=_pipeline_with_cursor,
    ):
        from backend.app.collectors.backfill_tasks import collect_backfill_job

        result = collect_backfill_job.run(job_id=job_id)

    assert result["status"] == "completed"

    with TestingSessionLocal() as db:
        fresh = db.get(models.BackfillJob, job_id)
        assert fresh is not None
        # Cursor final deve ser o do último stream, verificado via HMAC (HIGH 2 — F5-S5).
        from backend.app.collectors.backfill_tasks import _verify_cursor
        import os
        secret = os.environ.get("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
        cursor_saved = _verify_cursor(fresh.current_cursor, secret)
        assert cursor_saved == cursors_returned[-1]
        # Contadores acumulados.
        assert fresh.events_collected == 10  # 5 + 5
        assert fresh.events_dispatched == 10


def test_collect_backfill_job_handles_errors_marks_failed(
    session_factory,
) -> None:
    """Erro durante coleta deve marcar job como 'failed' com last_error."""
    engine, TestingSessionLocal = session_factory

    with TestingSessionLocal() as db:
        job = _make_job(db, status="pending", streams=["alerts"])
        job_id = job.id

    async def _pipeline_raises(*args, **kwargs):
        raise RuntimeError("Erro simulado no vendor")

    with patch(
        "backend.app.collectors.backfill_tasks.database.SessionLocal",
        TestingSessionLocal,
    ), patch(
        "backend.app.collectors.backfill_tasks.run_backfill_collection_once",
        side_effect=_pipeline_raises,
    ):
        from backend.app.collectors.backfill_tasks import collect_backfill_job

        with pytest.raises(RuntimeError, match="Erro simulado no vendor"):
            collect_backfill_job.run(job_id=job_id)

    with TestingSessionLocal() as db:
        fresh = db.get(models.BackfillJob, job_id)
        assert fresh is not None
        assert fresh.status == "failed"
        assert fresh.finished_at is not None
        assert "Erro simulado" in (fresh.last_error or "")


def test_collect_backfill_job_skips_if_not_pending_or_running(
    session_factory,
) -> None:
    """Job com status 'completed' deve ser ignorado sem erro."""
    engine, TestingSessionLocal = session_factory

    with TestingSessionLocal() as db:
        job = _make_job(db, status="completed", streams=["alerts"])
        job_id = job.id

    pipeline_mock = _make_pipeline_mock()

    with patch(
        "backend.app.collectors.backfill_tasks.database.SessionLocal",
        TestingSessionLocal,
    ), patch(
        "backend.app.collectors.backfill_tasks.run_backfill_collection_once",
        new=pipeline_mock,
    ):
        from backend.app.collectors.backfill_tasks import collect_backfill_job

        result = collect_backfill_job.run(job_id=job_id)

    assert result.get("skipped") is True
    # Pipeline não deve ter sido chamado.
    pipeline_mock.assert_not_called()
