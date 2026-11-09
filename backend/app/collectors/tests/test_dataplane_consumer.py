"""Consumer dispatcher (FAKE, sem broker).

Valida a lógica de commit at-least-once + a resiliência de ``run_dispatch_consumer``:
- sucesso                       → commita o offset;
- breaker OPEN                  → lote estacionado no Kafka + commita;
- transitório < N               → ``seek`` de volta + NÃO commita (reprocessa);
- transitório esgotado (> N)    → lote à DLQ (exhausted) + commita;
- payload não-JSON              → descarta + commita (não trava a partição);
- envelope sem batch/dest válido→ descarta + commita;
- erro não classificado         → seek (transitório), nunca commit sem DLQ;
- lag                           → highwater − position p/ TODAS as partições.
O round-trip contra Redpanda real fica no teste de integração.
"""

from __future__ import annotations

import asyncio
import os
import types

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest

from backend.app.collectors import pipeline
from backend.app.collectors import delivery as _delivery
from backend.app.collectors.dataplane import dispatcher, kafka_transport as kt
from backend.app.collectors.delivery import TransientDeliveryError

_real_sleep = asyncio.sleep


class _FakeTP:
    """TopicPartition mínimo, hashable (usado como chave de dict de lag)."""

    def __init__(self, topic: str, partition: int):
        self.topic = topic
        self.partition = partition

    def __hash__(self):
        return hash((self.topic, self.partition))

    def __eq__(self, other):
        return (self.topic, self.partition) == (other.topic, other.partition)


class _FakeConsumer:
    def __init__(self, messages, *, highwater=None, position=None):
        self._messages = list(messages)
        self.commits: list = []          # offsets commitados (dicts {tp: off})
        self.seeks: list = []
        self.started = self.stopped = False
        # p/ o teste de lag: assignment()/end_offsets()/position()
        self._highwater = highwater
        self._position = position
        self.revoked: set = set()

    def subscribe(self, topics, listener=None):
        self.topics, self.listener = list(topics), listener

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    async def getone(self, *partitions):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)

    async def commit(self, offsets=None):
        self.commits.append(offsets)

    def seek(self, tp, offset):
        self.seeks.append((tp, offset))

    # ── superfície usada pelo emissor de lag ──
    def assignment(self):
        if self._highwater is None:
            # Sem lag configurado: todas as partições atribuídas (o consumer só
            # commita/faz seek em partição que é dele).
            from aiokafka.structs import TopicPartition

            return {TopicPartition("centralops.deliver", p) for p in range(8)} - self.revoked
        return {_FakeTP("centralops.deliver", 0)}

    async def end_offsets(self, partitions):
        return {tp: self._highwater for tp in partitions}

    async def position(self, tp):
        return self._position


def _msg(offset, dest, batch):
    return types.SimpleNamespace(
        topic="centralops.deliver", partition=0, offset=offset,
        value=kt.encode_delivery(dest, batch),
    )


def _install(
    monkeypatch, fake, dispatch_impl, *, dlq_capture=None, dlq_ok=True,
    park_capture=None, park_ok=True,
):
    import aiokafka

    monkeypatch.setattr(aiokafka, "AIOKafkaConsumer", lambda *a, **k: fake)
    monkeypatch.setattr(pipeline, "dispatch_batch_to_destination", dispatch_impl)

    # Sem broker: o ensure-topic é no-op (senão tentaria AIOKafkaAdminClient real).
    async def _noop_topic():
        return None

    monkeypatch.setattr(kt, "_ensure_deliver_topic", _noop_topic)

    # DLQ capturável (senão tentaria escrever no DB).
    def _fake_dlq(batch, *, destination_id, error_kind, organization_id=None):
        if dlq_capture is not None:
            dlq_capture.append(
                {"batch": batch, "destination_id": destination_id, "error_kind": error_kind}
            )
        return dlq_ok

    monkeypatch.setattr(_delivery, "persist_batch_dlq", _fake_dlq)

    # Estacionamento por destino (ADR-0018 Onda 4a): capturável, sem broker.
    from backend.app.collectors.dataplane import parked as _parked

    async def _fake_park(value, destination_id, reason):
        if park_capture is not None:
            park_capture.append(
                {"destination_id": destination_id, "reason": reason, "value": value}
            )
        return park_ok

    monkeypatch.setattr(_parked, "park", _fake_park)

    # Acelera os backoffs MAS cede o controle ao loop (senão o _emit_lag em
    # background vira um loop sem yield e starva o teste).
    async def _fast_sleep(_delay=0, *a, **k):
        await _real_sleep(0)

    monkeypatch.setattr("asyncio.sleep", _fast_sleep)
    # Lag refresh alto → a task de background parka e não interfere no teste.
    monkeypatch.setattr(kt.settings, "KAFKA_LAG_REFRESH_SECONDS", 3600)
    monkeypatch.setattr(kt.settings, "KAFKA_MAX_DELIVERY_ATTEMPTS", 10)


async def test_success_commits(monkeypatch):
    calls = []

    async def _dispatch(dest, batch):
        calls.append((dest, batch))

    fake = _FakeConsumer([_msg(0, "dest-1", [{"e": 1}])])
    _install(monkeypatch, fake, _dispatch)

    await kt.run_dispatch_consumer()

    assert calls == [("dest-1", [{"e": 1}])]
    assert len(fake.commits) == 1
    ((_tp, _off),) = fake.commits[0].items()
    assert _off == 1                                 # commit explícito {tp: offset+1}
    assert fake.seeks == []
    assert fake.stopped is True


async def test_transient_is_retried_in_place_not_by_seek(monkeypatch):
    """Onda 4b: o transitório é re-tentado DENTRO da tarefa do registro — voltar o
    offset (seek) prendia a partição inteira, e com entrega concorrente rebobinaria
    registros de outros destinos já em voo. Só commita depois de entregar."""
    calls: list = []

    async def _dispatch(dest, batch):
        calls.append(dest)
        if len(calls) == 1:
            raise TransientDeliveryError("broker do destino instável")

    fake = _FakeConsumer([_msg(7, "dest-2", [{"e": 1}])])
    _install(monkeypatch, fake, _dispatch)

    await kt.run_dispatch_consumer()

    assert len(calls) == 2 and fake.seeks == []
    assert [list(c.values())[0] for c in fake.commits] == [8]


async def test_transient_exhausted_is_parked_and_commits(monkeypatch):
    """Transitório que NUNCA cessa: após N tentativas, ESTACIONA e commita. A
    partição nunca ficou presa. Transitório não é poison: espera no Kafka, não na
    DLQ do Postgres (ADR-0018 Onda 4a)."""
    dlq: list = []
    parked: list = []
    calls: list = []

    async def _dispatch(dest, batch):
        calls.append(dest)
        raise TransientDeliveryError("destino transitório-permanente")

    msg = _msg(5, "dest-x", [{"e": 1}])
    fake = _FakeConsumer([msg])
    _install(monkeypatch, fake, _dispatch, dlq_capture=dlq, park_capture=parked)
    monkeypatch.setattr(kt.settings, "KAFKA_MAX_DELIVERY_ATTEMPTS", 3)

    await kt.run_dispatch_consumer()

    assert len(calls) == 4                           # 1 + 3 re-tentativas
    assert dlq == [] and fake.seeks == []
    assert [p["reason"] for p in parked] == ["exhausted"]
    assert parked[0]["value"] == msg.value           # o registro, byte a byte
    assert [list(c.values())[0] for c in fake.commits] == [6]


async def test_destination_outage_writes_no_postgres_rows(monkeypatch):
    """ADR-0018 Onda 4: destino fora (breaker OPEN) não grava linha no Postgres —
    o lote nunca tentado ESTACIONA no Kafka e só então o offset é commitado."""
    from backend.app.collectors import circuit_breaker

    dlq: list = []
    parked: list = []

    async def _dispatch(dest, batch):
        raise circuit_breaker.BreakerOpen("dest-y")

    msgs = [_msg(9 + i, "dest-y", [{"e": i}]) for i in range(5)]
    fake = _FakeConsumer(msgs)
    _install(monkeypatch, fake, _dispatch, dlq_capture=dlq, park_capture=parked)

    def _no_db(*a, **k):
        raise AssertionError("destino fora tocou o Postgres")

    monkeypatch.setattr(_delivery, "persist_rejected_to_dlq", _no_db)

    await kt.run_dispatch_consumer()

    assert dlq == []
    assert [p["reason"] for p in parked] == ["breaker_open"] * 5
    assert [p["value"] for p in parked] == [m.value for m in msgs]
    assert len(fake.commits) == 5                   # commitou após estacionar
    assert fake.seeks == []


async def test_unclassified_error_never_commits_without_a_durable_outcome(monkeypatch):
    """ADR-0018 Onda 0: erro não classificado NÃO commita sem entrega ou destino
    durável. Com o estacionamento recusando e o processo parando, o registro é
    abandonado SEM commit (o próximo dono relê)."""
    import threading

    stop = threading.Event()
    calls: list = []

    async def _dispatch(dest, batch):
        calls.append(dest)
        raise ValueError("erro não classificado")

    fake = _FakeConsumer([_msg(3, "dest-3", [{"e": 1}])])
    parked: list = []
    _install(monkeypatch, fake, _dispatch, park_capture=parked, park_ok=False)
    monkeypatch.setattr(kt.settings, "KAFKA_MAX_DELIVERY_ATTEMPTS", 2)

    from backend.app.collectors.dataplane import parked as _parked

    async def _park_refused(value, destination_id, reason):
        parked.append(reason)
        stop.set()                                   # processo encerrando
        return False

    monkeypatch.setattr(_parked, "park", _park_refused)
    await kt.run_dispatch_consumer(stop_event=stop)

    assert len(calls) == 3 and parked == ["exhausted"]
    assert fake.commits == [] and fake.seeks == []


async def test_invalid_payload_skipped(monkeypatch):
    async def _dispatch(dest, batch):  # pragma: no cover — não deve rodar
        raise AssertionError("dispatch não deveria rodar p/ payload inválido")

    bad = types.SimpleNamespace(topic="centralops.deliver", partition=0, offset=1, value=b"not-json")
    fake = _FakeConsumer([bad])
    _install(monkeypatch, fake, _dispatch)

    await kt.run_dispatch_consumer()

    assert len(fake.commits) == 1                   # descarta + commita


async def test_malformed_envelope_skipped(monkeypatch):
    """JSON VÁLIDO mas sem 'batch'/'destination_id' = poison message:
    descarta+commita em vez de KeyError caindo no except-genérico (que mentiria
    'DLQ no dispatch')."""
    async def _dispatch(dest, batch):  # pragma: no cover — não deve rodar
        raise AssertionError("dispatch não deveria rodar p/ envelope malformado")

    import json as _json

    bad = types.SimpleNamespace(
        topic="centralops.deliver", partition=0, offset=2,
        value=_json.dumps({"v": 1, "destination_id": "d1"}).encode(),  # SEM 'batch'
    )
    fake = _FakeConsumer([bad])
    _install(monkeypatch, fake, _dispatch)

    await kt.run_dispatch_consumer()

    assert len(fake.commits) == 1                   # descarta + commita (não trava)


async def test_emit_consumer_lag_uses_end_offsets(monkeypatch):
    """Lag = highwater − position p/ as partições atribuídas, via
    end_offsets (idle-safe). Cobre a matemática que o background emite."""
    from unittest.mock import MagicMock

    fake = _FakeConsumer([], highwater=10, position=3)
    metrics = types.SimpleNamespace(DATAPLANE_CONSUMER_LAG=MagicMock())

    await kt._emit_consumer_lag(fake, metrics)

    metrics.DATAPLANE_CONSUMER_LAG.labels.assert_called_once_with(partition="0")
    metrics.DATAPLANE_CONSUMER_LAG.labels.return_value.set.assert_called_once_with(7)  # 10-3


def test_dispatcher_main_importable():
    # O entrypoint do role importa e expõe main() (smoke).
    assert callable(dispatcher.main)



async def test_revoked_partition_is_not_committed_or_seeked(monkeypatch):
    """Rebalance durante o dispatch: a partição deixou de ser deste consumer. Antes,
    ``commit``/``seek`` nela levantavam IllegalStateError e derrubavam o processo."""
    from aiokafka.structs import TopicPartition

    fake = _FakeConsumer([_msg(4, "dest-r", [{"e": 1}]), _msg(5, "dest-r", [{"e": 2}])])

    async def _dispatch(dest, batch):
        fake.revoked.add(TopicPartition("centralops.deliver", 0))  # perdeu no meio
        if batch[0]["e"] == 2:
            raise TransientDeliveryError(dest)

    _install(monkeypatch, fake, _dispatch)
    await kt.run_dispatch_consumer()      # não levanta

    assert fake.commits == [] and fake.seeks == []


async def test_one_redis_client_for_the_whole_consumer(monkeypatch):
    """ADR-0018 Onda 3f: o despacho criava um cliente Redis POR LOTE (padrão das
    tasks Celery, cujo loop morre a cada task) — conexão nova e
    ``importlib.metadata.version`` do redis-py a cada lote, ~1/4 do tempo do
    dispatcher no gate de carga. O consumer vive o processo: um cliente só,
    publicado para o despacho e fechado no fim."""
    clients: list = []

    class _Redis:
        closed = False

        async def aclose(self):
            self.closed = True

    def _new():
        clients.append(_Redis())
        return clients[-1]

    monkeypatch.setattr("backend.app.collectors.celery_app.get_worker_redis", _new)
    seen: list = []

    async def _dispatch(dest, batch):
        seen.append(pipeline.DISPATCH_REDIS.get())

    fake = _FakeConsumer([_msg(0, "d", [{"e": 1}]), _msg(1, "d", [{"e": 2}])])
    _install(monkeypatch, fake, _dispatch)

    await kt.run_dispatch_consumer()

    assert len(clients) == 1 and seen == [clients[0], clients[0]]
    assert clients[0].closed is True
    assert pipeline.DISPATCH_REDIS.get() is None
