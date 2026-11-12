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
import time
import types
from typing import Dict, List

import pytest

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
    # Cada recusa voltou o offset de A (só a partição dele); B nunca foi segurada.
    assert consumer.seeks == [(0, 0)] * 3
    # Commits: B, depois A inteiro; nenhuma linha na DLQ.
    assert (1, 1) in consumer.commits and (0, 2) in consumer.commits
    assert dlq == []


def test_slow_parked_destination_does_not_hold_the_others(monkeypatch):
    """Drenador concorrente por partição: enquanto o lote de um destino lento (ou
    com entrega em ordem e muito atraso) está em voo, as outras partições
    continuam saindo. Antes era um laço só e B esperava A terminar."""
    topic = parked.parked_topic()
    consumer = _ParkedConsumer(
        {0: [_value("dest-a", 0), _value("dest-a", 1)], 1: [_value("dest-b", i) for i in range(3)]},
        topic,
    )
    delivered: List[str] = []
    a_inflight = {"n": 0, "max": 0}

    async def _dispatch(dest, batch):
        if dest == "dest-a":
            a_inflight["n"] += 1
            a_inflight["max"] = max(a_inflight["max"], a_inflight["n"])
            await asyncio.sleep(0.2)
            a_inflight["n"] -= 1
        delivered.append(batch[0]["_centralops"]["event_id"])

    stop = _install(monkeypatch, consumer, _dispatch, [])
    asyncio.run(_drain_until(stop, lambda: len(delivered) == 5))

    # B inteiro saiu enquanto o 1º lote de A ainda estava em voo...
    assert delivered[:3] == ["dest-b-0", "dest-b-1", "dest-b-2"]
    # ...e A continuou em ordem, um lote por vez na partição dele.
    assert delivered[3:] == ["dest-a-0", "dest-a-1"] and a_inflight["max"] == 1
    assert (0, 2) in consumer.commits and (1, 3) in consumer.commits


def test_parked_ledger_counts_what_waits_per_destination(monkeypatch):
    """A tela do destino lê quantos lotes/eventos esperam e desde quando. O
    contador sobe ao estacionar e desce ao drenar; zerou, some."""
    import fakeredis

    from backend.app.collectors import observability_store as obs

    fake = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(obs, "_redis", lambda: fake)
    assert parked.parked_state("d1") == {"batches": 0, "events": 0, "since": None}
    parked.note_parked("d1", 3)
    parked.note_parked("d1", 2)
    state = parked.parked_state("d1")
    assert state["batches"] == 2 and state["events"] == 5 and state["since"] > 0
    assert fake.pttl(parked._ledger_key("d1")) > 0, "expira com a retenção do tópico"
    parked.note_drained("d1", 3)
    assert parked.parked_state("d1")["batches"] == 1
    parked.note_drained("d1", 2)
    assert parked.parked_state("d1")["batches"] == 0
    assert not fake.exists(parked._ledger_key("d1"))
    # Redis fora: a tela mostra zero, nunca quebra.
    monkeypatch.setattr(obs, "_redis", lambda: (_ for _ in ()).throw(ConnectionError("x")))
    assert parked.parked_state("d1")["batches"] == 0
    parked.note_parked("d1", 1)  # não levanta


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


# ── 4c. Destino grande, disjuntor e DLQ só de poison em lote ─────────────────


def test_deliver_sub_shards_spread_one_destination(monkeypatch):
    """Com a chave ``destination_id`` pura, um destino tinha UMA partição e um
    dispatcher só. Os lotes de um destino agora se espalham por N partições
    consecutivas a partir da base dele; com 1 shard, volta ao hash da chave."""
    monkeypatch.setattr(settings, "KAFKA_DELIVER_SHARDS_PER_DEST", 4)
    spreads = {kt.deliver_spread("dest-big") for _ in range(400)}
    assert {base for base, _ in spreads} == {kt.zlib.crc32(b"dest-big")}
    assert {shard for _, shard in spreads} == {0, 1, 2, 3}

    calls: list = []
    monkeypatch.setattr(kt._producer_thread, "_ensure_started", lambda: None, raising=False)
    monkeypatch.setattr(
        kt._producer_thread, "_produce_value",
        lambda dest, value, key, topic=None, **kw: calls.append(kw.get("spread")),
        raising=False,
    )
    kt._producer_thread.produce("dest-big", [{"_centralops": {"event_id": "e1"}}])
    assert calls and calls[0][0] == kt.zlib.crc32(b"dest-big")

    monkeypatch.setattr(settings, "KAFKA_DELIVER_SHARDS_PER_DEST", 1)
    assert kt.deliver_spread("dest-big") is None
    calls.clear()
    kt._producer_thread.produce("dest-big", [{"_centralops": {"event_id": "e1"}}])
    assert calls == [None]


def test_breaker_probe_outlives_one_send_attempt():
    """Defaults timeout_ms=30000 e cooldown_s=30: a sonda expirava junto com o
    envio dela, e num destino em blackhole outro processo pegava uma sonda nova
    antes de a falha reabrir o disjuntor — que nunca falhava rápido."""
    assert circuit_breaker.probe_ttl_for(30, 30.0) > 30
    assert circuit_breaker.probe_ttl_for(30, 29.2) >= 30 + 1
    assert circuit_breaker.probe_ttl_for(120, 10.0) == 120   # cooldown maior vale
    assert circuit_breaker.probe_ttl_for(30, None) == 30


def test_check_for_config_uses_the_send_timeout(monkeypatch):
    seen: dict = {}

    async def _check(redis, destination_id, *, kind, probe_ttl_s):
        seen["ttl"] = probe_ttl_s

    monkeypatch.setattr(circuit_breaker, "check", _check)
    cfg = types.SimpleNamespace(destination_id="d", kind="syslog_rfc3164", delivery={})
    asyncio.run(circuit_breaker.check_for_config(None, cfg, send_timeout_s=30.0))
    assert seen["ttl"] > 30


@pytest.fixture()
def dlq_db(monkeypatch):
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from backend.app.db import database, models

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    models.DestinationDeadLetter.__table__.create(engine)
    inserts: list = []

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("INSERT"):
            # Linhas na instrução = parâmetros ÷ colunas (8 na DLQ).
            n = len(params) if isinstance(params, (tuple, list)) else len(params or {})
            inserts.append(n // 8)

    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))
    return types.SimpleNamespace(engine=engine, inserts=inserts, models=models)


def _env(event_id: str, org: int = 7) -> dict:
    return {"_centralops": {"event_id": event_id, "organization_id": org}, "x": 1}


def test_poison_dlq_is_written_in_batch(dlq_db):
    """DLQ só de poison, gravada EM LOTE: uma instrução por bloco, não um
    SAVEPOINT + flush por evento. Evento já na DLQ tem o motivo ATUALIZADO (o
    dreno e o operador dependem do motivo mais recente), sem linha duplicada."""
    from sqlalchemy.orm import Session

    batch = [_env(f"e{i}") for i in range(120)] + [_env("e0")]  # e0 repetido
    assert _delivery.persist_batch_dlq(batch, destination_id="d", error_kind="size") is True
    # UMA instrução, com uma linha por evento DISTINTO: no Postgres a mesma chave
    # duas vezes num ``ON CONFLICT DO UPDATE`` derruba a instrução inteira.
    assert dlq_db.inserts == [120]

    assert _delivery.persist_batch_dlq(
        [_env("e5"), _env("new")], destination_id="d", error_kind="schema"
    ) is True
    with Session(dlq_db.engine) as s:
        rows = s.query(dlq_db.models.DestinationDeadLetter).all()
        by_id = {r.event_id: r for r in rows}
    assert len(rows) == 121
    assert by_id["e5"].error_kind == "schema" and by_id["e1"].error_kind == "size"
    assert by_id["new"].organization_id is None and by_id["e1"].payload


def test_rejected_events_dlq_is_written_in_batch(dlq_db):
    from backend.app.collectors.output.base import RejectedEvent
    from sqlalchemy.orm import Session

    cfg = types.SimpleNamespace(destination_id="d", organization_id=None)
    batch = [_env(f"r{i}", org=9) for i in range(30)]
    rejected = [
        RejectedEvent(event_id=f"r{i}", reason="schema", error_kind="schema")
        for i in range(30)
    ]
    assert _delivery.persist_rejected_to_dlq(cfg, rejected, batch) is True
    assert dlq_db.inserts == [30]
    with Session(dlq_db.engine) as s:
        rows = s.query(dlq_db.models.DestinationDeadLetter).all()
    assert len(rows) == 30 and {r.organization_id for r in rows} == {9}



def test_slow_destination_is_parked_at_fetch_while_marked(monkeypatch):
    """Achado do gate de carga: os lotes do destino lento esperavam a vez OCUPANDO
    vagas do teto global, e o saudável quase não entrava (31 s → 159 s). Depois
    que um lote estoura a espera, o destino fica marcado lento e os lotes dele
    estacionam já na busca, sem ocupar vaga."""
    import time as _time

    from backend.app.collectors.tests.test_dataplane_consumer import (
        _FakeConsumer,
        _install as _install_consumer,
        _msg,
        _real_sleep,
    )

    release = asyncio.Event()
    fast: List[int] = []

    async def _dispatch(dest, batch):
        if dest == "slow":
            await release.wait()
        else:
            fast.append(batch[0]["e"])

    class _Spaced(_FakeConsumer):
        """Registros chegando espaçados: o teste dura mais que uma janela de
        espera, e uma marca que expirasse nela deixaria o lento voltar à fila."""

        async def getone(self, *partitions):
            await _real_sleep(0.004)
            return await super().getone(*partitions)

    # 60 lentos e 60 rápidos intercalados; o lento nunca responde.
    msgs = [_msg(i, "slow" if i % 2 == 0 else "fast", [{"e": i}]) for i in range(120)]
    fake = _Spaced(msgs)
    parked_: list = []
    _install_consumer(monkeypatch, fake, _dispatch, park_capture=parked_)
    from backend.app.collectors import metrics as _metrics

    outcomes: list = []

    class _Consumed:
        def labels(self, outcome):
            outcomes.append(outcome)
            return types.SimpleNamespace(inc=lambda *a: None)

    monkeypatch.setattr(_metrics, "DATAPLANE_CONSUMED", _Consumed())
    monkeypatch.setattr(settings, "KAFKA_DISPATCH_INFLIGHT_PER_DEST", 1)
    monkeypatch.setattr(settings, "KAFKA_DISPATCH_MAX_INFLIGHT", 4)
    monkeypatch.setattr(settings, "KAFKA_DISPATCH_SLOW_PARK_SECONDS", 0.2)
    # A marca dura o ciclo do estacionamento. Aqui ela precisa cobrir o teste
    # inteiro, e o teste não tem duração fixa: com a CPU disputada (xdist na
    # imagem de CI) 120 registros levam bem mais que os ~0,5 s de uma máquina
    # ociosa, e uma marca de 5 s expirava no meio, devolvendo o lento à fila.
    # Uma regressão para marcar só pela janela de espera (0,2 s) continua
    # aparecendo na contagem de esperas abaixo.
    monkeypatch.setattr(settings, "KAFKA_PARKED_HOLD_SECONDS", 600.0)

    async def _run():
        task = asyncio.create_task(kt.run_dispatch_consumer())
        # Guarda contra travamento, não medida de vazão: falha só se o saudável
        # ficar 30 s sem entregar nada. Um prazo total media a máquina, não o
        # código: o relógio começava antes de o consumer subir (imports
        # preguiçosos) e, com a CPU disputada, 20 s não bastavam.
        t0 = last_progress = _time.monotonic()
        seen = 0
        while len(fast) < 60 and _time.monotonic() - last_progress < 30:
            await _real_sleep(0.005)
            if len(fast) != seen:
                seen, last_progress = len(fast), _time.monotonic()
        from collections import Counter as _C
        _diag = (len(fast), round(_time.monotonic() - t0, 2), dict(_C(outcomes)), len(parked_))
        assert len(fast) == 60, _diag
        # Uma janela de espera (0,2 s) para marcar o lento; depois tudo flui.
        # Só os lotes da PRIMEIRA janela esperaram a vez (o teto de 4 vagas menos
        # o travado em voo); a marca durou o teste inteiro. Sem a marca, cada
        # janela deixava mais lotes do lento esperando a vez, ocupando vagas.
        assert 1 <= outcomes.count("parked_slow_wait") <= 3, _diag
        # Do lento: 1 em voo (travado), o resto estacionou (na espera ou na busca).
        assert len(parked_) == 59 and {p["reason"] for p in parked_} == {"slow"}
        release.set()
        await task

    asyncio.run(_run())


# ── 4e. Entrega em ordem por destino ─────────────────────────────────────────


from backend.app.collectors.dataplane import ordered as _ordered  # noqa: E402


class _OrderRedis:
    """O que a entrega em ordem usa do Redis, com o compare-and-delete do Lua."""

    def __init__(self, log: list | None = None) -> None:
        self.kv: Dict[str, str] = {}
        self.ttl: Dict[str, int] = {}
        self.log = log if log is not None else []

    async def exists(self, key):
        return int(key in self.kv)

    async def incr(self, key):
        self.kv[key] = str(int(self.kv.get(key, "0")) + 1)
        return int(self.kv[key])

    async def set(self, key, value, ex=None):
        self.kv[key] = str(value)
        self.ttl[key] = ex

    async def eval(self, script, numkeys, key, arg):
        assert "get" in script and "del" in script
        self.log.append(("release", arg))
        if self.kv.get(key) == arg:
            del self.kv[key]
            return 1
        return 0

    async def aclose(self):
        pass


_LAST = "dataplane:ordered:dest-ord:last"


def _ord_msg(offset, dest, n):
    return types.SimpleNamespace(
        topic="centralops.deliver", partition=0, offset=offset,
        value=kt.encode_delivery(dest, [{"e": n}], ordered=True),
    )


def test_ordered_flag_lives_in_the_delivery_policy():
    from backend.app.collectors.output.delivery_config import DeliveryConfig, parse_delivery

    assert DeliveryConfig().ordered is False
    assert parse_delivery("syslog_rfc3164", {"ordered": True}).ordered is True


def test_ordered_destination_uses_one_partition_and_marks_the_envelope(monkeypatch):
    """Destino ordenado: toda produção na partição-base dele, envelope marcado.
    Os outros seguem espalhados e com o envelope de antes (sem a chave)."""
    monkeypatch.setattr(settings, "KAFKA_DELIVER_SHARDS_PER_DEST", 4)
    monkeypatch.setattr(_ordered, "is_ordered", lambda dest: dest == "dest-ord")
    sent: list = []
    monkeypatch.setattr(kt._producer_thread, "_ensure_started", lambda: None, raising=False)
    monkeypatch.setattr(
        kt._producer_thread, "_produce_value",
        lambda dest, value, key, topic=None, **kw: sent.append((dest, value, kw.get("spread"))),
        raising=False,
    )
    for _ in range(60):
        kt._producer_thread.produce("dest-ord", [{"e": 1}])
        kt._producer_thread.produce("dest-free", [{"e": 1}])
    ord_ = [(v, s) for d, v, s in sent if d == "dest-ord"]
    free = [(v, s) for d, v, s in sent if d == "dest-free"]
    assert {s for _, s in ord_} == {(kt.zlib.crc32(b"dest-ord"), 0)}
    assert all(kt.decode_delivery(v)["ordered"] is True for v, _ in ord_)
    assert len({s for _, s in free}) > 1
    assert all("ordered" not in kt.decode_delivery(v) for v, _ in free)


def test_is_ordered_caches_and_survives_the_database(monkeypatch):
    _ordered.reset_cache()
    reads: list = []
    answer = {"v": True}

    def _load(dest):
        reads.append(dest)
        if answer["v"] is None:
            raise RuntimeError("banco fora")
        return answer["v"]

    monkeypatch.setattr(_ordered, "_load", _load)
    clock = {"t": 100.0}
    monkeypatch.setattr(_ordered.time, "monotonic", lambda: clock["t"])
    assert _ordered.is_ordered("d") is True and _ordered.is_ordered("d") is True
    assert reads == ["d"]                       # uma leitura por janela
    clock["t"] += _ordered.CONFIG_TTL_S + 1
    answer["v"] = None                          # banco fora: mantém a última leitura
    assert _ordered.is_ordered("d") is True and len(reads) == 2
    _ordered.reset_cache()
    assert _ordered.is_ordered("never-read") is False


def test_is_ordered_reads_the_destination_row(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from backend.app.db import database, models

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    models.Destination.__table__.create(engine)
    maker = sessionmaker(bind=engine)
    with maker() as s:
        s.add(models.Destination(id="o", name="o", kind="syslog_rfc3164", delivery='{"ordered": true}'))
        s.add(models.Destination(id="f", name="f", kind="syslog_rfc3164", delivery="{}"))
        s.commit()
    monkeypatch.setattr(database, "SessionLocal", maker)
    assert _ordered._load("o") is True
    assert _ordered._load("f") is False and _ordered._load("missing") is False


def test_reserve_marks_before_parking_and_release_is_compare_and_delete():
    redis = _OrderRedis()

    async def _run():
        assert await _ordered.reserve(redis, "dest-ord") == 1
        assert await _ordered.reserve(redis, "dest-ord") == 2
        assert await _ordered.has_backlog(redis, "dest-ord")
        # O lote 1 sai do estacionamento: o 2 ainda está lá, a marca fica.
        assert await _ordered.release(redis, "dest-ord", 1) is False
        assert await _ordered.has_backlog(redis, "dest-ord")
        assert await _ordered.release(redis, "dest-ord", 2) is True
        assert not await _ordered.has_backlog(redis, "dest-ord")

    asyncio.run(_run())
    assert redis.ttl[_LAST] == settings.KAFKA_PARKED_RETENTION_MS // 1000
    stamped = _ordered.stamp(kt.encode_delivery("dest-ord", [{"e": 1}], ordered=True), 7)
    assert _ordered.sequence_of(kt.decode_delivery(stamped)) == 7


def _ordered_consumer_setup(monkeypatch, msgs, dispatch, *, slow_after=5.0):
    from backend.app.collectors.tests.test_dataplane_consumer import (
        _FakeConsumer,
        _install as _install_consumer,
    )

    fake = _FakeConsumer(msgs)
    parked_: list = []
    _install_consumer(monkeypatch, fake, dispatch, park_capture=parked_)
    redis = _OrderRedis()
    monkeypatch.setattr("backend.app.collectors.celery_app.get_worker_redis", lambda: redis)
    monkeypatch.setattr(settings, "KAFKA_DISPATCH_SLOW_PARK_SECONDS", slow_after)
    return fake, parked_, redis


def test_ordered_destination_is_delivered_in_arrival_order(monkeypatch):
    """O lote 0 do destino ordenado trava; os seguintes dele esperam (em ordem) e
    os de outro destino na mesma partição saem sem esperar."""
    from backend.app.collectors.tests.test_dataplane_consumer import _msg, _real_sleep

    release = asyncio.Event()
    ord_: List[int] = []
    free: List[int] = []

    async def _dispatch(dest, batch):
        n = batch[0]["e"]
        if dest == "dest-ord":
            if n == 0:
                await release.wait()
            ord_.append(n)
        else:
            free.append(n)

    msgs = []
    for i in range(12):
        msgs.append(_ord_msg(i, "dest-ord", i) if i % 2 == 0 else _msg(i, "dest-free", [{"e": i}]))
    fake, parked_, _redis = _ordered_consumer_setup(monkeypatch, msgs, _dispatch)

    async def _run():
        task = asyncio.create_task(kt.run_dispatch_consumer())
        for _ in range(400):
            if len(free) == 6:
                break
            await _real_sleep(0.005)
        assert sorted(free) == [1, 3, 5, 7, 9, 11] and ord_ == []
        release.set()
        await task

    asyncio.run(_run())
    assert ord_ == [0, 2, 4, 6, 8, 10]
    assert parked_ == []
    assert list(fake.commits[-1].values())[0] == 12


def test_parked_ordered_destination_parks_everything_behind_it(monkeypatch):
    """Disjuntor aberto no 1º lote: ele estaciona com o nº 1, e os seguintes
    estacionam atrás dele (sem tentar o destino) enquanto houver estacionado."""
    calls: list = []

    async def _dispatch(dest, batch):
        calls.append(batch[0]["e"])
        raise circuit_breaker.BreakerOpen(dest)

    msgs = [_ord_msg(i, "dest-ord", i) for i in range(4)]
    fake, parked_, redis = _ordered_consumer_setup(monkeypatch, msgs, _dispatch)
    asyncio.run(kt.run_dispatch_consumer())

    assert calls == [0]
    assert [p["reason"] for p in parked_] == ["breaker_open"] + ["ordered_backlog"] * 3
    seqs = [kt.decode_delivery(p["value"])["ord_seq"] for p in parked_]
    events = [kt.decode_delivery(p["value"])["batch"][0]["e"] for p in parked_]
    assert seqs == [1, 2, 3, 4] and events == [0, 1, 2, 3]
    assert redis.kv[_LAST] == "4"
    assert list(fake.commits[-1].values())[0] == 4


def test_ordered_slow_delivery_is_cut_and_parked(monkeypatch):
    """Entrega direta acima de ``slow_after`` é cortada e o lote estaciona; o
    seguinte acha a marca e estaciona atrás. Nada segura a fila do destino."""

    async def _dispatch(dest, batch):
        await asyncio.Event().wait()          # nunca responde

    msgs = [_ord_msg(i, "dest-ord", i) for i in range(3)]
    fake, parked_, redis = _ordered_consumer_setup(monkeypatch, msgs, _dispatch, slow_after=0.05)
    asyncio.run(kt.run_dispatch_consumer())
    assert [p["reason"] for p in parked_] == ["slow", "ordered_backlog", "ordered_backlog"]
    assert list(fake.commits[-1].values())[0] == 3


def test_ordered_batch_that_waited_too_long_parks_without_trying(monkeypatch):
    """Cada entrega leva 0,3 s com teto de 0,5 s: o 2º lote esperou 0,3 s e vai
    direto; o 3º esperou 0,6 s e estaciona sem tentar — a fila do destino não
    cresce sem fim atrás de um destino mais lento que a entrada.

    Relógio VIRTUAL: com sleep real de 0,3 s contra o teto de 0,5 s, a folga do
    2º lote era 0,2 s, e um runner carregado (xdist, gate da imagem .so) a
    estourava — o 2º lote estacionava e o teste falhava sem bug nenhum. Aqui o
    tempo só anda dentro da entrega, depois que os três lotes já marcaram o
    início deles."""
    from backend.app.collectors.tests.test_dataplane_consumer import _real_sleep

    calls: list = []
    clock = {"now": 0.0}
    holder: dict = {}

    class _VirtualTime:
        def perf_counter(self) -> float:
            return clock["now"]

        def __getattr__(self, name):
            return getattr(time, name)

    async def _dispatch(dest, batch):
        calls.append(batch[0]["e"])
        # Só avança o relógio quando o consumidor já leu os três lotes e as
        # tasks deles rodaram o 1º passo (``started``): a medição fica exata.
        while holder["fake"]._messages:
            await _real_sleep(0.001)
        await _real_sleep(0.01)
        clock["now"] += 0.3

    msgs = [_ord_msg(i, "dest-ord", i) for i in range(3)]
    fake, parked_, redis = _ordered_consumer_setup(monkeypatch, msgs, _dispatch, slow_after=0.5)
    holder["fake"] = fake
    monkeypatch.setattr(kt, "time", _VirtualTime())
    asyncio.run(kt.run_dispatch_consumer())
    assert calls == [0, 1]
    assert [p["reason"] for p in parked_] == ["slow"]
    assert kt.decode_delivery(parked_[0]["value"])["batch"] == [{"e": 2}]


def test_drainer_releases_the_ordered_mark_only_on_the_last_parked(monkeypatch):
    """O drenador apaga a marca ao entregar o lote com o número dela, e ANTES de
    commitar: uma queda entre os dois re-entrega o lote, nunca deixa marca órfã."""
    topic = parked.parked_topic()
    base = kt.encode_delivery("dest-ord", [{"e": 0}], ordered=True)
    log: list = []

    class _Logged(_ParkedConsumer):
        async def commit(self, offsets):
            log.append(("commit", list(offsets.values())[0]))
            await super().commit(offsets)

    consumer = _Logged({0: [_ordered.stamp(base, 1), _ordered.stamp(base, 2)]}, topic)
    delivered: list = []

    async def _dispatch(dest, batch):
        delivered.append(dest)

    stop = _install(monkeypatch, consumer, _dispatch, [])
    redis = _OrderRedis(log)
    redis.kv[_LAST] = "2"
    monkeypatch.setattr("backend.app.collectors.celery_app.get_worker_redis", lambda: redis)
    asyncio.run(_drain_until(stop, lambda: len(consumer.commits) == 2))

    assert log == [("release", "1"), ("commit", 1), ("release", "2"), ("commit", 2)]
    assert _LAST not in redis.kv and delivered == ["dest-ord", "dest-ord"]
