"""per-destination shadow mode (dispatch path).

A destination with delivery.shadow=true formats + measures real routed traffic
but NEVER delivers: no send_batch, no breaker, no DLQ, no Redis. Safe cutover
rehearsal. (The on-demand /shadow preview endpoint is covered in the router
test suite.)
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import models
from backend.app.db.database import Base


@pytest.fixture()
def static_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    import backend.app.db.database as db_module

    original = db_module.SessionLocal
    db_module.SessionLocal = TestingSessionLocal
    yield TestingSessionLocal, engine
    db_module.SessionLocal = original
    Base.metadata.drop_all(bind=engine)


def _env(eid="e1"):
    return {"_centralops": {"event_id": eid, "organization_id": None}, "normalized": {}, "raw": {}}


@pytest.mark.asyncio
async def test_shadow_formats_but_never_delivers(static_db) -> None:
    SessionLocal, _ = static_db
    dest_id = "d-shadow"
    with SessionLocal() as s:
        s.add(
            models.Destination(
                id=dest_id,
                name="shadow",
                kind="splunk_hec",
                enabled=True,
                config='{"url": "https://x:8088"}',
                secret_ref=None,
                delivery='{"shadow": true}',
                config_version="v1",
                organization_id=None,
            )
        )
        s.commit()

    target = MagicMock()
    target.format = MagicMock(return_value={"event": {}})
    target.send_batch = AsyncMock()
    target.close = AsyncMock()

    with (
        patch("backend.app.core.secrets.get_default_backend", return_value=None),
        patch(
            "backend.app.collectors.output.destination_cache.get_destination",
            new_callable=AsyncMock,
            return_value=target,
        ),
        # If shadow leaks into the delivery path this would be needed; assert it ISN'T.
        patch("backend.app.collectors.celery_app.get_worker_redis") as mock_redis,
    ):
        from backend.app.collectors.pipeline import dispatch_batch_to_destination

        batch = [_env("a"), _env("b"), _env("c")]
        result = await dispatch_batch_to_destination(dest_id, batch)

    assert result is None
    # Formatted every event, delivered NONE.
    assert target.format.call_count == 3
    target.send_batch.assert_not_awaited()
    # Shadow short-circuits BEFORE touching Redis/breaker.
    mock_redis.assert_not_called()

    # No DLQ rows from a shadow run.
    with SessionLocal() as s:
        n = (
            s.query(models.DestinationDeadLetter)
            .filter(models.DestinationDeadLetter.destination_id == dest_id)
            .count()
        )
    assert n == 0


@pytest.mark.asyncio
async def test_non_shadow_still_delivers(static_db) -> None:
    """Control: without shadow, send_batch IS called (shadow is opt-in)."""
    SessionLocal, _ = static_db
    dest_id = "d-live"
    with SessionLocal() as s:
        s.add(
            models.Destination(
                id=dest_id,
                name="live",
                kind="splunk_hec",
                enabled=True,
                config='{"url": "https://x:8088"}',
                secret_ref=None,
                delivery="{}",  # no shadow
                config_version="v1",
                organization_id=None,
            )
        )
        s.commit()

    import fakeredis.aioredis as fakeredis_aio

    from backend.app.collectors.output.base import DeliveryResult

    target = MagicMock()
    target.send_batch = AsyncMock(return_value=DeliveryResult(accepted=1))
    target.close = AsyncMock()

    with (
        patch("backend.app.core.secrets.get_default_backend", return_value=None),
        patch(
            "backend.app.collectors.output.destination_cache.get_destination",
            new_callable=AsyncMock,
            return_value=target,
        ),
        patch(
            "backend.app.collectors.celery_app.get_worker_redis",
            return_value=fakeredis_aio.FakeRedis(decode_responses=True),
        ),
    ):
        from backend.app.collectors.pipeline import dispatch_batch_to_destination

        await dispatch_batch_to_destination(dest_id, [_env("a")])

    target.send_batch.assert_awaited_once()
