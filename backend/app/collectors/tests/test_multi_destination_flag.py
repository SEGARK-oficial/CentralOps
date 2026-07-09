"""Roteamento das filas de dispatch.

Multi-destino é GA: o roteamento é o ÚNICO caminho de despacho.
A lane dedicada do Wazuh foi DELETADA: a task ``dispatch_to_wazuh`` e a
fila ``dispatch.wazuh`` não existem mais. ``wazuh-default`` deixou de ter
special-case no fan-out — agora é um ``Destination`` normal (kind syslog_rfc3164)
que flui pela MESMA via uniforme que todo destino, isto é,
``dispatch_to_destination`` (modo celery/default) ou ``produce_delivery`` (kafka).
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


def test_dispatch_to_destination_task_registered() -> None:
    assert "collectors.dispatch_to_destination" in celery_app.tasks


def test_dispatch_to_destination_routed_to_dedicated_queue() -> None:
    route = celery_app.conf.task_routes.get("collectors.dispatch_to_destination")
    assert route == {"queue": "dispatch.destination"}
    # Constante canônica bate com o roteamento.
    assert queues.Q_DISPATCH_DESTINATION == "dispatch.destination"
    assert queues.T_DISPATCH_DESTINATION == "collectors.dispatch_to_destination"


def test_dispatch_destination_queue_declared() -> None:
    declared = {q.name for q in celery_app.conf.task_queues}
    assert "dispatch.destination" in declared
    # A fila dedicada do Wazuh foi DELETADA — não deve mais
    # ser declarada. wazuh-default entrega pela lane genérica dispatch.destination.
    assert "dispatch.wazuh" not in declared


def test_enqueue_dispatch_routes_wazuh_default_via_uniform_lane(static_db) -> None:
    """``wazuh-default`` NÃO tem mais lane dedicada.

    Uma rota catch-all apontando para ``wazuh-default`` agora despacha pela MESMA
    via uniforme de qualquer destino — ``dispatch_to_destination.apply_async`` na sua
    shard queue ``dispatch.destination.*``. A lane dedicada do Wazuh
    (``dispatch_to_wazuh`` / ``dispatch.wazuh``) foi DELETADA.
    (EVENT_DATAPLANE default = celery no env de teste.)"""
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
    with patch(
        "backend.app.collectors.tasks.dispatch_to_destination.apply_async"
    ) as mock_dest:
        pipeline._enqueue_dispatch(batch)

    # wazuh-default agora flui pela via uniforme dispatch_to_destination.
    mock_dest.assert_called_once()
    kwargs = mock_dest.call_args.kwargs["kwargs"]
    assert kwargs["destination_id"] == "wazuh-default"
    assert kwargs["batch"] == batch
    assert mock_dest.call_args.kwargs["queue"].startswith("dispatch.destination.")
