"""Teste de INTEGRAÇÃO contra um broker Kafka/Redpanda real.

Round-trip de verdade: ``produce_delivery`` (producer singleton em loop-thread) →
broker → ``run_dispatch_consumer`` → dispatch. Valida o caminho que os mocks não
cobrem (serialização no broker, particionamento por key, commit do consumer).

PULA automaticamente se não houver broker em ``KAFKA_BOOTSTRAP_SERVERS`` (default
localhost:9092). Para rodar local::

    docker run -d --name redpanda-test -p 9092:9092 redpandadata/redpanda:v24.2.7 \
        redpanda start --smp 1 --overprovisioned --node-id 0 --check=false \
        --kafka-addr PLAINTEXT://0.0.0.0:9092 --advertise-kafka-addr PLAINTEXT://localhost:9092
    KAFKA_BOOTSTRAP_SERVERS=localhost:9092 pytest backend/app/collectors/tests/test_dataplane_integration.py
"""

from __future__ import annotations

import asyncio
import os
import socket
import uuid

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest

from backend.app.collectors import pipeline
from backend.app.collectors.dataplane import kafka_transport as kt
from backend.app.core.config import settings


def _broker_reachable() -> bool:
    bootstrap = settings.KAFKA_BOOTSTRAP_SERVERS
    host, _, port = bootstrap.partition(":")
    try:
        with socket.create_connection((host, int(port or "9092")), timeout=1.0):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _broker_reachable(),
    reason="sem broker Kafka/Redpanda em KAFKA_BOOTSTRAP_SERVERS — teste de integração pulado",
)


async def test_produce_consume_roundtrip(monkeypatch):
    # Tópico/group únicos por execução: lê do zero, sem estado de runs anteriores.
    run_id = uuid.uuid4().hex[:8]
    monkeypatch.setattr(settings, "KAFKA_TOPIC_PREFIX", f"it-{run_id}")
    monkeypatch.setattr(settings, "KAFKA_DELIVER_TOPIC", "deliver")
    monkeypatch.setattr(settings, "KAFKA_CONSUMER_GROUP", f"it-group-{run_id}")
    # Producer singleton fresco (aponta para o broker/topic do teste).
    monkeypatch.setattr(kt, "_producer_thread", kt._ProducerThread())

    received: list = []

    async def _record(dest, batch):
        received.append((dest, batch))

    monkeypatch.setattr(pipeline, "dispatch_batch_to_destination", _record)

    # Produz 2 sub-lotes para 2 destinos distintos (key=dest → partições estáveis).
    kt.produce_delivery("dest-A", [{"event_id": "a1"}, {"event_id": "a2"}], {"traceparent": "tp"})
    kt.produce_delivery("dest-B", [{"event_id": "b1"}], None)

    consumer_task = asyncio.create_task(kt.run_dispatch_consumer())
    try:
        for _ in range(100):  # ~10s máx
            if len(received) >= 2:
                break
            await asyncio.sleep(0.1)
    finally:
        consumer_task.cancel()
        try:
            await consumer_task
        except asyncio.CancelledError:
            pass
        kt.shutdown_producer()

    by_dest = {dest: batch for dest, batch in received}
    assert by_dest.get("dest-A") == [{"event_id": "a1"}, {"event_id": "a2"}]
    assert by_dest.get("dest-B") == [{"event_id": "b1"}]
