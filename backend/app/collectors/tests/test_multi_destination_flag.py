"""Roteamento das filas de dispatch.

Multi-destino é GA: o roteamento é o ÚNICO caminho de despacho.
A lane dedicada do Wazuh foi DELETADA: a task ``dispatch_to_wazuh`` e a
fila ``dispatch.wazuh`` não existem mais. ``wazuh-default`` deixou de ter
special-case no fan-out — agora é um ``Destination`` normal (kind syslog_rfc3164)
que flui pela MESMA via uniforme que todo destino: ``produce_delivery`` no tópico
``deliver`` do Kafka. A lane Celery ``dispatch_to_destination`` (e seus shards
``dispatch.destination.N``) saiu na ADR-0018 Onda 5; só ``dispatch.dlq`` fica.
"""

from __future__ import annotations

import os
from unittest.mock import patch

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.collectors import queues
from backend.app.collectors import tasks  # noqa: F401  — registra as tasks no celery_app
from backend.app.collectors.celery_app import celery_app
from backend.app.db import repository
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


def _ev(vendor: str = "sophos", org: int = 1) -> dict:
    return {"_centralops": {"vendor": vendor, "organization_id": org}}


def _seed_route(SessionLocal, **kw):
    with SessionLocal() as s:
        repository.RouteRepository(s).add(**kw)


def test_dispatch_to_destination_task_removed() -> None:
    """A lane Celery de entrega saiu (ADR-0018 Onda 5): a entrega é do papel
    dispatcher do Kafka. Só o reenvio da DLQ pedido pela UI segue como task."""
    assert "collectors.dispatch_to_destination" not in celery_app.tasks
    assert "collectors.dispatch_to_dlq" not in celery_app.tasks
    assert "collectors.drain_destination_dlq" in celery_app.tasks
    assert "collectors.dispatch_to_destination" not in celery_app.conf.task_routes
    for removed in (
        "Q_DISPATCH_DESTINATION",
        "T_DISPATCH_DESTINATION",
        "T_DISPATCH_DLQ",
        "dispatch_dest_shard_queue",
        "all_dispatch_dest_queues",
    ):
        assert not hasattr(queues, removed), removed


def test_only_dlq_dispatch_queue_declared() -> None:
    declared = {q.name for q in celery_app.conf.task_queues}
    # Única fila de dispatch que sobra: o reenvio da DLQ (drain_destination_dlq).
    assert queues.Q_DLQ == "dispatch.dlq"
    assert "dispatch.dlq" in declared
    # Nem a lane genérica, nem os shards, nem a antiga lane dedicada do Wazuh.
    assert not {n for n in declared if n.startswith("dispatch.destination")}
    assert "dispatch.wazuh" not in declared


def test_enqueue_dispatch_routes_wazuh_default_via_uniform_lane(static_db) -> None:
    """``wazuh-default`` NÃO tem lane dedicada.

    Uma rota catch-all apontando para ``wazuh-default`` despacha pela MESMA via
    uniforme de qualquer destino — ``produce_delivery`` no tópico ``deliver`` do
    data-plane Kafka (único data-plane desde a Onda 5)."""
    SessionLocal, _ = static_db
    _seed_route(
        SessionLocal,
        name="rest",
        condition={},
        destination_ids=["wazuh-default"],
        is_final=True,
        priority=100,
        organization_id=None,
    )

    from backend.app.collectors import pipeline

    batch = [_ev()]
    with patch("backend.app.collectors.dataplane.produce_delivery") as mock_produce:
        pipeline._enqueue_dispatch(batch)

    # wazuh-default flui pela via uniforme produce_delivery(dest, lote, tp).
    mock_produce.assert_called_once()
    dest_id, sent = mock_produce.call_args.args[:2]
    assert dest_id == "wazuh-default"
    assert sent == batch
