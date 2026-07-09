"""Testa que o backfill não toca o cursor de polling normal (RF2.4).

O cursor de coleta incremental vive em ``CollectionState`` (tabela) e
no Redis hot-path. O backfill deve usar somente ``BackfillJob.current_cursor``
e nunca escrever em ``CollectionState``.
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
def session_factory():
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


def _seed_collection_state(
    session,
    *,
    integration_id: int,
    stream: str,
    cursor_value: dict,
) -> models.CollectionState:
    """Cria um CollectionState com cursor pré-definido."""
    state = models.CollectionState(
        integration_id=integration_id,
        stream=stream,
        cursor=json.dumps(cursor_value),
        last_success_at=datetime.utcnow() - timedelta(hours=1),
    )
    session.add(state)
    session.commit()
    session.refresh(state)
    return state


def test_backfill_does_not_touch_polling_cursor(session_factory) -> None:
    """Execução de backfill não altera CollectionState do polling normal."""
    engine, TestingSessionLocal = session_factory

    # Cria org + integration.
    with TestingSessionLocal() as db:
        org = models.Organization(
            name=f"Isolation Test Org {uuid4().hex[:6]}",
            slug=f"iso-test-{uuid4().hex[:6]}",
        )
        db.add(org)
        db.flush()
        integration = models.Integration(
            organization_id=org.id,
            name="Isolation Test Integration",
            platform="sophos",
            is_active=True,
        )
        db.add(integration)
        db.flush()
        db.commit()
        db.refresh(integration)
        integration_id = integration.id

    # Cursor de polling pré-existente — marcador que não deve ser alterado.
    original_cursor = {"page": 42, "token": "polling-cursor-marker"}
    with TestingSessionLocal() as db:
        _seed_collection_state(
            db,
            integration_id=integration_id,
            stream="alerts",
            cursor_value=original_cursor,
        )

    # Cria BackfillJob.
    with TestingSessionLocal() as db:
        now = datetime.utcnow()
        job = models.BackfillJob(
            integration_id=integration_id,
            streams=json.dumps(["alerts"]),
            from_ts=now - timedelta(days=7),
            to_ts=now - timedelta(days=1),
            status="pending",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id

    pipeline_mock = AsyncMock(
        return_value={
            "cursor": {"backfill_page": 1},
            "events_collected": 3,
            "events_dispatched": 3,
        }
    )

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

    # Cursor de polling deve estar intocado.
    with TestingSessionLocal() as db:
        state = (
            db.query(models.CollectionState)
            .filter(
                models.CollectionState.integration_id == integration_id,
                models.CollectionState.stream == "alerts",
            )
            .first()
        )
        assert state is not None, "CollectionState não deve ser removido"
        saved_cursor = json.loads(state.cursor)
        assert saved_cursor == original_cursor, (
            f"Cursor de polling foi alterado! Esperado {original_cursor}, "
            f"encontrado {saved_cursor}"
        )

    # Cursor de backfill deve estar no job (verificado via HMAC — HIGH 2 — F5-S5).
    with TestingSessionLocal() as db:
        fresh_job = db.get(models.BackfillJob, job_id)
        assert fresh_job is not None
        from backend.app.collectors.backfill_tasks import _verify_cursor
        import os
        secret = os.environ.get("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
        backfill_cursor = _verify_cursor(fresh_job.current_cursor or "{}", secret)
        assert backfill_cursor == {"backfill_page": 1}, (
            f"Cursor de backfill incorreto: {backfill_cursor}"
        )
