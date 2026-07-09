"""Consumer dispatcher (FAKE, sem broker).

Valida a lógica de commit at-least-once + a resiliência de ``run_dispatch_consumer``:
- sucesso                       → commita o offset;
- breaker OPEN                  → lote à DLQ (breaker_open) + commita;
- transitório < N               → ``seek`` de volta + NÃO commita (reprocessa);
- transitório esgotado (> N)    → lote à DLQ (exhausted) + commita;
- payload não-JSON              → descarta + commita (não trava a partição);
- envelope sem batch/dest válido→ descarta + commita;
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
            return set()
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


def _install(monkeypatch, fake, dispatch_impl, *, dlq_capture=None):
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

    monkeypatch.setattr(_delivery, "persist_batch_dlq", _fake_dlq)

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


async def test_transient_seeks_back_no_commit(monkeypatch):
    async def _dispatch(dest, batch):
        raise TransientDeliveryError("broker do destino instável")

    fake = _FakeConsumer([_msg(7, "dest-2", [{"e": 1}])])
    _install(monkeypatch, fake, _dispatch)

    await kt.run_dispatch_consumer()

    assert fake.commits == []                       # NÃO commita → reprocessa
    assert fake.seeks and fake.seeks[0][1] == 7     # volta ao offset do registro


async def test_transient_exhausted_goes_to_dlq_and_commits(monkeypatch):
    """Transitório que NUNCA cessa: após N tentativas, vai à DLQ e commita
    p/ DESBLOQUEAR a partição (sem poison-trap infinito)."""
    dlq: list = []

    async def _dispatch(dest, batch):
        raise TransientDeliveryError("destino transitório-permanente")

    # 1 msg, mas re-entregue: como o fake só entrega 1x, simulamos N entregas do
    # MESMO offset enfileirando a mesma msg N+1 vezes (cada getone re-popa).
    same = [_msg(5, "dest-x", [{"e": 1}]) for _ in range(11)]
    fake = _FakeConsumer(same)
    _install(monkeypatch, fake, _dispatch, dlq_capture=dlq)
    monkeypatch.setattr(kt.settings, "KAFKA_MAX_DELIVERY_ATTEMPTS", 3)

    await kt.run_dispatch_consumer()

    assert dlq and dlq[-1]["error_kind"] == "exhausted"
    assert dlq[-1]["destination_id"] == "dest-x"
    # após esgotar, commitou o offset do registro (desbloqueia a partição)
    assert fake.commits and list(fake.commits[-1].values())[0] == 6  # offset 5 + 1


async def test_breaker_open_goes_to_dlq_and_commits(monkeypatch):
    """Breaker OPEN: lote NUNCA tentado → DLQ(breaker_open) ANTES de commitar
    (senão somem silenciosamente)."""
    from backend.app.collectors import circuit_breaker

    dlq: list = []

    async def _dispatch(dest, batch):
        raise circuit_breaker.BreakerOpen("dest-y")

    fake = _FakeConsumer([_msg(9, "dest-y", [{"e": 1}])])
    _install(monkeypatch, fake, _dispatch, dlq_capture=dlq)

    await kt.run_dispatch_consumer()

    assert dlq and dlq[0]["error_kind"] == "breaker_open"
    assert dlq[0]["destination_id"] == "dest-y"
    assert len(fake.commits) == 1                   # commitou após persistir na DLQ
    assert fake.seeks == []


async def test_nontransient_commits(monkeypatch):
    async def _dispatch(dest, batch):
        raise ValueError("erro não-transitório (rejeições já DLQadas no dispatch)")

    fake = _FakeConsumer([_msg(3, "dest-3", [{"e": 1}])])
    _install(monkeypatch, fake, _dispatch)

    await kt.run_dispatch_consumer()

    assert len(fake.commits) == 1                   # commita: não reprocessa permanente
    assert fake.seeks == []


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
