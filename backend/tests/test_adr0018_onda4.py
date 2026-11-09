"""ADR-0018 Onda 4 — store-and-forward por destino.

4a. **Destino fora espera no Kafka.** Lote de destino com o disjuntor aberto (ou
com transitório esgotado) ESTACIONA no tópico ``deliver.parked`` em vez de virar
linha na DLQ do Postgres (``test_destination_outage_writes_no_postgres_rows``, em
``test_dataplane_consumer.py``). O drenador pausa só a partição do destino que
segue fora e entrega o estacionado quando ele volta
(``test_parked_batches_drain_on_recovery``).
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import asyncio
import threading
import types
from typing import Dict, List

from backend.app.collectors import circuit_breaker
from backend.app.collectors import delivery as _delivery
from backend.app.collectors import pipeline
from backend.app.collectors.dataplane import kafka_transport as kt
from backend.app.collectors.dataplane import parked
from backend.app.core.config import settings


# ── Dublê do consumer do estacionamento ──────────────────────────────────────


class _ParkedConsumer:
    """Partições com fila, posição, pausa e commit — o que o drenador usa."""

    def __init__(self, records: Dict[int, List[bytes]], topic: str) -> None:
        from aiokafka.structs import TopicPartition

        self.topic = topic
        self.tps = {p: TopicPartition(topic, p) for p in records}
        self.records = records
        self.pos = {p: 0 for p in records}
        self.paused: set = set()
        self.commits: List[tuple] = []
        self.seeks: List[tuple] = []
        self.pauses: List[int] = []

    async def start(self):
        pass

    async def stop(self):
        pass

    def assignment(self):
        return set(self.tps.values())

    def pause(self, *tps):
        for tp in tps:
            self.paused.add(tp.partition)
            self.pauses.append(tp.partition)

    def resume(self, *tps):
        for tp in tps:
            self.paused.discard(tp.partition)

    def seek(self, tp, offset):
        self.seeks.append((tp.partition, offset))
        self.pos[tp.partition] = offset

    async def commit(self, offsets):
        for tp, off in offsets.items():
            self.commits.append((tp.partition, off))

    async def end_offsets(self, tps):
        return {tp: len(self.records[tp.partition]) for tp in tps}

    async def position(self, tp):
        return self.pos[tp.partition]

    async def getone(self):
        while True:
            for p, recs in self.records.items():
                if p in self.paused or self.pos[p] >= len(recs):
                    continue
                off = self.pos[p]
                self.pos[p] += 1
                return types.SimpleNamespace(
                    topic=self.topic, partition=p, offset=off, value=recs[off]
                )
            await asyncio.sleep(0.002)


def _value(dest: str, n: int) -> bytes:
    return kt.encode_delivery(dest, [{"_centralops": {"event_id": f"{dest}-{n}"}}])


def _install(monkeypatch, consumer, dispatch, dlq: list) -> threading.Event:
    import aiokafka

    stop = threading.Event()
    monkeypatch.setattr(aiokafka, "AIOKafkaConsumer", lambda *a, **k: consumer)

    async def _noop():
        return None

    monkeypatch.setattr(parked, "ensure_parked_topic", _noop)
    monkeypatch.setattr(pipeline, "dispatch_batch_to_destination", dispatch)
    monkeypatch.setattr(settings, "KAFKA_LAG_REFRESH_SECONDS", 3600)

    class _Redis:
        async def aclose(self):
            pass

    monkeypatch.setattr("backend.app.collectors.celery_app.get_worker_redis", lambda: _Redis())

    def _dlq(*a, **k):
        dlq.append((a, k))
        return True

    monkeypatch.setattr(_delivery, "persist_batch_dlq", _dlq)
    monkeypatch.setattr(_delivery, "persist_rejected_to_dlq", _dlq)
    return stop


async def _drain_until(stop: threading.Event, done, timeout: float = 5.0, **kw) -> None:
    task = asyncio.create_task(parked.run_parked_consumer(stop_event=stop, **kw))
    deadline = asyncio.get_running_loop().time() + timeout
    while not done():
        if asyncio.get_running_loop().time() > deadline:
            stop.set()
            await task
            raise AssertionError("o drenador não terminou no prazo")
        await asyncio.sleep(0.005)
    stop.set()
    await task


# ── 4a. Drenador ─────────────────────────────────────────────────────────────


def test_parked_batches_drain_on_recovery(monkeypatch):
    """Destino A fora: a partição dele PAUSA (volta o offset) e as outras seguem;
    quando A volta, o estacionado sai em ordem e é commitado. Nada vai à DLQ."""
    topic = parked.parked_topic()
    consumer = _ParkedConsumer(
        {0: [_value("dest-a", 0), _value("dest-a", 1)], 1: [_value("dest-b", 0)]}, topic
    )
    delivered: List[str] = []
    a_down = {"left": 3}  # A recusa as 3 primeiras tentativas (fora do ar)

    async def _dispatch(dest, batch):
        if dest == "dest-a" and a_down["left"] > 0:
            a_down["left"] -= 1
            raise circuit_breaker.BreakerOpen(dest)
        delivered.append(batch[0]["_centralops"]["event_id"])

    dlq: list = []
    stop = _install(monkeypatch, consumer, _dispatch, dlq)

    asyncio.run(
        _drain_until(stop, lambda: len(delivered) == 3, hold_seconds=0.02)
    )

    # B não esperou por A (outra partição), e A saiu em ordem depois de voltar.
    assert delivered.index("dest-b-0") < delivered.index("dest-a-0")
    assert [d for d in delivered if d.startswith("dest-a")] == ["dest-a-0", "dest-a-1"]
    # Cada recusa voltou o offset de A e pausou SÓ a partição dele.
    assert set(consumer.pauses) == {0} and len(consumer.pauses) == 3
    assert all(seek == (0, 0) for seek in consumer.seeks)
    # Commits: B, depois A inteiro; nenhuma linha na DLQ.
    assert (1, 1) in consumer.commits and (0, 2) in consumer.commits
    assert dlq == []


def test_parked_transient_error_stays_parked_never_dlq(monkeypatch):
    """Transitório no drenador não esgota para a DLQ: o lote segue no Kafka,
    com espera crescente (até o HOLD), até entregar."""
    topic = parked.parked_topic()
    consumer = _ParkedConsumer({0: [_value("dest-a", 0)]}, topic)
    calls = {"n": 0}

    async def _dispatch(dest, batch):
        calls["n"] += 1
        if calls["n"] <= 4:
            raise ConnectionRefusedError("destino recusando")

    dlq: list = []
    stop = _install(monkeypatch, consumer, _dispatch, dlq)
    asyncio.run(_drain_until(stop, lambda: consumer.commits, hold_seconds=0.01))

    assert calls["n"] == 5 and consumer.commits == [(0, 1)]
    assert dlq == []


def test_parked_invalid_record_is_dropped_and_committed(monkeypatch):
    """Registro que não veio do dispatcher (não decodifica) é poison: descarta e
    commita, sem travar a partição."""
    topic = parked.parked_topic()
    consumer = _ParkedConsumer({0: [b"not-json", _value("dest-a", 0)]}, topic)
    delivered: list = []

    async def _dispatch(dest, batch):
        delivered.append(dest)

    stop = _install(monkeypatch, consumer, _dispatch, [])
    asyncio.run(_drain_until(stop, lambda: len(consumer.commits) == 2))
    assert consumer.commits == [(0, 1), (0, 2)] and delivered == ["dest-a"]


def test_park_writes_the_record_to_the_destination_partition(monkeypatch):
    """O registro vai ao estacionamento como veio, com ``acks`` (produce que
    espera), na partição do destino — sempre a mesma para o mesmo destino."""
    sent: list = []

    def _produce(dest, value, key, topic, spread=None):
        sent.append((dest, value, key, topic, spread))

    monkeypatch.setattr(kt._producer_thread, "_produce_value", _produce, raising=False)
    started: list = []
    monkeypatch.setattr(
        kt._producer_thread, "_ensure_started", lambda: started.append(1), raising=False
    )
    value = _value("dest-a", 0)
    assert asyncio.run(parked.park(value, "dest-a", "breaker_open")) is True
    assert asyncio.run(parked.park(value, "dest-a", "breaker_open")) is True
    (d, v, k, t, spread), again = sent
    assert (d, v, k, t) == ("dest-a", value, b"dest-a", parked.parked_topic())
    assert spread == (parked.destination_base("dest-a"), 0) == again[4]
    assert started, "estacionar precisa subir o producer (o dispatcher não produz entrega)"

    def _down(*a, **k):
        raise ConnectionError("broker fora")

    monkeypatch.setattr(kt._producer_thread, "_produce_value", _down, raising=False)
    assert asyncio.run(parked.park(value, "dest-a", "breaker_open")) is False


def test_dispatcher_process_runs_both_consumers(monkeypatch):
    """O papel ``dispatcher`` roda o consumer de entrega E o drenador; sem o
    drenador, o estacionado nunca sairia."""
    from backend.app.collectors.dataplane import dispatcher

    ran: list = []

    async def _deliver(stop_event=None):
        ran.append("deliver")

    async def _parked(stop_event=None):
        ran.append("parked")

    monkeypatch.setattr(dispatcher, "run_dispatch_consumer", _deliver)
    monkeypatch.setattr(dispatcher, "run_parked_consumer", _parked)
    asyncio.run(dispatcher._run_consumers(threading.Event()))
    assert sorted(ran) == ["deliver", "parked"]


# ── 4b. Destino lento isolado ────────────────────────────────────────────────


def test_slow_destination_isolated(monkeypatch):
    """Um destino lento não segura os outros. Antes, o dispatcher entregava um
    registro por vez: um lote parado num destino lento travava todo destino que
    dividisse a partição. Agora a entrega é concorrente, e o lote que espera mais
    que ``KAFKA_DISPATCH_SLOW_PARK_SECONDS`` pela vez no seu destino vai ao
    estacionamento — o atraso que um destino lento impõe aos outros tem teto."""
    import time as _time

    from backend.app.collectors.tests.test_dataplane_consumer import (
        _FakeConsumer,
        _install as _install_consumer,
        _msg,
        _real_sleep,
    )

    release = asyncio.Event()
    fast: List[int] = []
    slow: List[int] = []

    async def _dispatch(dest, batch):
        if dest == "slow":
            await release.wait()           # destino travado até o teste soltar
            slow.append(batch[0]["e"])
        else:
            fast.append(batch[0]["e"])

    # 20 registros na MESMA partição, intercalados: slow, fast, slow, fast…
    msgs = [_msg(i, "slow" if i % 2 == 0 else "fast", [{"e": i}]) for i in range(20)]
    fake = _FakeConsumer(msgs)
    parked_: list = []
    _install_consumer(monkeypatch, fake, _dispatch, park_capture=parked_)
    monkeypatch.setattr(settings, "KAFKA_DISPATCH_INFLIGHT_PER_DEST", 2)
    monkeypatch.setattr(settings, "KAFKA_DISPATCH_SLOW_PARK_SECONDS", 0.05)

    async def _run():
        task = asyncio.create_task(kt.run_dispatch_consumer())
        t0 = _time.monotonic()
        while (len(fast) < 10 or len(parked_) < 8) and _time.monotonic() - t0 < 5:
            await _real_sleep(0.01)
        # Todo o rápido saiu com o lento travado, sem esperar por ele.
        assert sorted(fast) == list(range(1, 20, 2)), fast
        assert slow == []
        # Do lento, 2 seguem em voo (o teto por destino) e o resto estacionou
        # depois de esperar a vez por mais que o limite.
        assert [p["reason"] for p in parked_] == ["slow"] * 8
        # O commit não passa do primeiro registro do lento ainda em voo (offset 0).
        assert all(list(c.values())[0] <= 0 for c in fake.commits)
        release.set()
        await task

    asyncio.run(_run())
    assert sorted(slow) == [0, 2]
    # Liberado o lento, a partição commita até o fim.
    assert list(fake.commits[-1].values())[0] == 20


def test_offset_tracker_never_commits_past_an_open_record():
    from aiokafka.structs import TopicPartition

    tp = TopicPartition("t", 0)
    t = kt.OffsetTracker()
    for off in (10, 11, 12):
        t.start(tp, off)
    t.done(tp, 11)
    t.done(tp, 12)
    assert t.commit_point(tp) == 10        # 10 segue aberto
    assert t.commit_point(tp) is None      # nada novo
    t.done(tp, 10)
    assert t.commit_point(tp) == 13
    # Registro devolvido ao tópico (seek): o commit não passa dele.
    t.start(tp, 13)
    t.forget(tp, 13)
    assert t.commit_point(tp) is None
    t.start(tp, 13)
    t.done(tp, 13)
    assert t.commit_point(tp) == 14
    t.reset(tp)
    assert t.commit_point(tp) is None


def test_dispatch_revoke_waits_in_flight_and_commits_before_handing_over():
    """Rebalance com lotes em voo: a revogação espera eles terminarem e commita
    ainda como dono (sem isto, o novo dono relia lotes já entregues)."""
    from aiokafka.structs import TopicPartition

    tp = TopicPartition("t", 0)

    async def _run():
        tracker = kt.OffsetTracker()
        inflight: set = set()
        committed: list = []

        async def _advance(p):
            nxt = tracker.commit_point(p)
            if nxt is not None:
                committed.append(nxt)

        drain = kt._dispatch_revoke_drain(inflight, tracker, _advance)
        tracker.start(tp, 0)

        async def _batch():
            await asyncio.sleep(0.01)
            tracker.done(tp, 0)

        task = asyncio.ensure_future(_batch())
        inflight.add(task)
        await drain.on_partitions_revoked({tp})
        assert drain.revoking is True
        assert task.done() and committed == [1]
        await drain.on_partitions_assigned({tp})
        assert drain.revoking is False

    asyncio.run(_run())


def test_inflight_cap_bounds_memory(monkeypatch):
    """O teto de registros em voo vale com o destino travado: o consumer para de
    buscar em vez de acumular lotes na memória."""
    from backend.app.collectors.tests.test_dataplane_consumer import (
        _FakeConsumer,
        _install as _install_consumer,
        _msg,
        _real_sleep,
    )

    release = asyncio.Event()
    started: List[int] = []

    async def _dispatch(dest, batch):
        started.append(batch[0]["e"])
        await release.wait()

    fake = _FakeConsumer([_msg(i, f"d{i}", [{"e": i}]) for i in range(10)])
    _install_consumer(monkeypatch, fake, _dispatch)
    monkeypatch.setattr(settings, "KAFKA_DISPATCH_MAX_INFLIGHT", 3)

    async def _run():
        task = asyncio.create_task(kt.run_dispatch_consumer())
        for _ in range(50):
            await _real_sleep(0.005)
        assert len(started) == 3 and len(fake._messages) == 7
        release.set()
        await task

    asyncio.run(_run())
    assert sorted(started) == list(range(10))
