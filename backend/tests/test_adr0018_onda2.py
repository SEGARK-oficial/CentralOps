"""ADR-0018, Onda 2 — ingestão push no log.

* ``POST /api/ingest`` só responde 200 DEPOIS do ack do tópico raw; broker fora
  é 503 com ``Retry-After`` — nunca um 200 de evento que não está gravado.
* O receptor syslog grava no mesmo tópico (modo kafka).
* O ``processor`` consome o raw, roda o ciclo e commita o offset só depois; falha
  re-tenta e, esgotada, vai à DLQ ``__raw__``; ciclo que não leu o lote (integração
  inativa) também vai à DLQ — nunca um commit mudo.
* O coletor push lê o lote do Kafka em vez da lista Redis, e o ciclo do processor
  usa um conjunto de claims pendentes escopado pela partição.
"""

from __future__ import annotations

import asyncio
import os
import types

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest

from backend.app.collectors.dataplane import processor as proc
from backend.app.collectors.dataplane import raw as raw_mod
from backend.app.core.config import settings


def _run(coro):
    return asyncio.run(coro)


# ── Codec e enquadramento do raw ─────────────────────────────────────────────


def test_raw_codec_roundtrip_and_framing(monkeypatch):
    events = [{"i": i, "pad": "x" * 200} for i in range(50)]
    doc = raw_mod.decode_raw(raw_mod.encode_raw(7, "traffic", events, received_at=1.0))
    assert (doc["integration_id"], doc["stream"], len(doc["events"])) == (7, "traffic", 50)

    frames = list(raw_mod._raw_frames(7, "traffic", events, 2000, 1.0))
    assert len(frames) > 1
    assert all(len(v) <= 2000 for _, v in frames)
    assert [e["i"] for sub, _ in frames for e in sub] == list(range(50))


@pytest.mark.parametrize("bad", [b"nao-json", b"[]", b'{"integration_id":"x","stream":"s","events":[]}'])
def test_raw_decode_rejects_malformed(bad):
    with pytest.raises(ValueError):
        raw_mod.decode_raw(bad)


def test_raw_oversized_event_rejected_before_any_write(monkeypatch):
    from backend.app.collectors.dataplane import kafka_transport as kt

    written: list = []
    monkeypatch.setattr(raw_mod, "_ensure_raw_topic", lambda: None)
    monkeypatch.setattr(kt._producer_thread, "_produce_value", lambda *a, **k: written.append(a))
    monkeypatch.setattr(settings, "KAFKA_MAX_REQUEST_BYTES", 4096)

    with pytest.raises(raw_mod.OversizedRawEventError):
        raw_mod.produce_raw(1, "s", [{"ok": 1}, {"big": "y" * 10_000}])
    assert written == []  # nada do lote foi gravado

    assert raw_mod.produce_raw(1, "s", [{"ok": 1}, {"ok": 2}]) == 2
    assert len(written) == 1 and written[0][3] == raw_mod.raw_topic()


# Endpoint (200 só depois do ack; 503 com broker fora): ver test_ingest_endpoint.py,
# que tem o fixture do app.


# ── Syslog no modo kafka ─────────────────────────────────────────────────────


def test_syslog_flush_writes_to_raw_in_kafka_mode(monkeypatch):
    from backend.app.collectors import ingest_buffer
    from backend.app.collectors.dataplane import raw as raw_module
    from backend.app.syslog import server as srv

    monkeypatch.setattr(settings, "EVENT_DATAPLANE", "kafka")
    produced: list = []

    async def _produce(iid, stream, events):
        produced.append((iid, stream, len(events)))
        return len(events)

    monkeypatch.setattr(raw_module, "produce_raw_async", _produce)

    rec = srv.Receiver.__new__(srv.Receiver)
    rec.redis = object()  # qualquer uso do Redis quebraria
    rec._queues = {(3, "traffic", "fortinet_fortigate"): [{"m": 1}, {"m": 2}]}
    rec.batch_max = 500
    rec.stats = {}

    total = _run(rec.flush())
    assert total == 2 and produced == [(3, "traffic", 2)]
    assert not rec._queues
    assert ingest_buffer.push_goes_to_kafka()


# ── Processor → estágio de processamento (sem ciclo de coleta desde a Onda 3) ──


def test_processor_group_goes_straight_to_the_processing_stage(monkeypatch):
    """O lote do raw vai direto ao ``StreamProcessor`` (``process_raw_events``),
    com as claims escopadas pela partição e os ids gravados pelo coletor."""
    from backend.app.collectors import pipeline

    seen: dict = {}

    async def _stage(iid, stream, events, *, ids=None, scope="", dedupe=None):
        seen.update(iid=iid, stream=stream, n=len(events), ids=ids, scope=scope)
        seen.setdefault("windows", []).append(dedupe)
        return True

    def _no_cycle(*a, **k):  # pragma: no cover — qualquer chamada falha o teste
        raise AssertionError("o processor voltou a rodar o ciclo de coleta")

    monkeypatch.setattr(pipeline, "process_raw_events", _stage)
    monkeypatch.setattr(pipeline, "_run_collection_once", _no_cycle)
    consumed = _run(proc.process_group(9, "security", [{"x": 1}], partition=3, ids=["a"]))
    _run(proc.process_group(9, "security", [{"x": 2}], partition=3, ids=["b"]))
    _run(proc.process_group(9, "security", [{"x": 3}], partition=4, ids=["c"]))

    assert consumed is True
    windows = seen.pop("windows")
    assert seen == {"iid": 9, "stream": "security", "n": 1, "ids": ["c"], "scope": "raw-p4"}
    # Onda 3c: uma janela de dedupe por partição, a MESMA entre lotes dela.
    from backend.app.collectors.state.dedupe_window import DedupeWindow

    assert all(isinstance(w, DedupeWindow) for w in windows)
    assert windows[0] is windows[1] and windows[0] is not windows[2]


def test_push_collector_no_longer_reads_a_kafka_batch():
    """O atalho ``RAW_BATCH`` (Onda 2) sumiu com o ciclo no processor: o coletor
    push só drena a lista Redis do modo sem broker."""
    from backend.app.collectors.vendors import push_ingest

    assert not hasattr(push_ingest, "RAW_BATCH")
    assert not hasattr(push_ingest, "RawBatch")


# ── Consumer do processor: commit só depois, DLQ quando preciso ──────────────


class _TP:
    def __init__(self, partition):
        self.topic, self.partition = "centralops.raw", partition

    def __hash__(self):
        return hash((self.topic, self.partition))

    def __eq__(self, other):
        return (self.topic, self.partition) == (other.topic, other.partition)


class _FakeConsumer:
    def __init__(self, batches, stop):
        self._batches = list(batches)
        self._all = list(batches)
        self._stop = stop
        self.revoked: set = set()
        self.commits: list = []
        self.seeks: list = []

    async def start(self):
        pass

    async def stop(self):
        pass

    async def getmany(self, timeout_ms=0, max_records=None):
        if not self._batches:
            self._stop.set()
            return {}
        return self._batches.pop(0)

    async def commit(self, offsets):
        self.commits.append({tp.partition: off for tp, off in offsets.items()})

    def seek(self, tp, offset):
        self.seeks.append((tp.partition, offset))

    def assignment(self):
        tps = {tp for batch in self._all for tp in batch}
        return tps - self.revoked


def _rec(offset, iid=1, stream="traffic", events=None):
    value = raw_mod.encode_raw(iid, stream, events or [{"o": offset}], received_at=0.0)
    return types.SimpleNamespace(offset=offset, value=value)


def _install(monkeypatch, batches, *, cycle, dlq_ok=True, dlq=None):
    import threading

    import aiokafka

    stop = threading.Event()
    fake = _FakeConsumer(batches, stop)
    monkeypatch.setattr(aiokafka, "AIOKafkaConsumer", lambda *a, **k: fake)
    monkeypatch.setattr(raw_mod, "_ensure_raw_topic", lambda: None)
    monkeypatch.setattr(proc, "process_group", cycle)
    monkeypatch.setattr(proc, "_touch", lambda path: None)
    monkeypatch.setattr(settings, "KAFKA_LAG_REFRESH_SECONDS", 3600)

    # Backoff sem esperar, mas CEDENDO o loop: um sleep que não cede deixa a
    # tarefa de lag (que também dorme) num laço ocupado e o teste trava.
    _real_sleep = asyncio.sleep

    async def _fast_sleep(*_a, **_k):
        await _real_sleep(0)

    monkeypatch.setattr(proc.asyncio, "sleep", _fast_sleep)

    from backend.app.collectors import delivery

    def _dlq(batch, *, destination_id, error_kind, organization_id=None, error_detail=None):
        if dlq is not None:
            dlq.append((destination_id, error_kind, len(batch)))
        return dlq_ok

    monkeypatch.setattr(delivery, "persist_batch_dlq", _dlq)
    return fake, stop


def test_processor_commits_after_cycle_groups_consecutive(monkeypatch):
    calls: list = []

    async def _cycle(iid, stream, events, partition, ids=None):
        calls.append((iid, stream, len(events)))
        return True

    tp = _TP(0)
    fake, stop = _install(
        monkeypatch,
        [{tp: [_rec(10, 1, "a"), _rec(11, 1, "a"), _rec(12, 2, "b")]}],
        cycle=_cycle,
    )
    _run(proc.run_raw_processor(stop_event=stop))

    assert calls == [(1, "a", 2), (2, "b", 1)]          # consecutivos agrupados
    assert fake.commits == [{0: 12}, {0: 13}]
    assert fake.seeks == []


def test_processor_failure_seeks_and_does_not_commit(monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("RoutingUnavailable")

    tp = _TP(1)
    fake, stop = _install(monkeypatch, [{tp: [_rec(5), _rec(6, 2, "z")]}], cycle=_boom)
    _run(proc.run_raw_processor(stop_event=stop))

    assert fake.commits == []
    assert fake.seeks == [(1, 5)]                        # o resto da partição espera


def test_processor_exhausted_goes_to_dlq_then_commits(monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("falha persistente")

    monkeypatch.setattr(settings, "KAFKA_MAX_DELIVERY_ATTEMPTS", 1)
    tp = _TP(0)
    dlq: list = []
    fake, stop = _install(
        monkeypatch, [{tp: [_rec(3)]}, {tp: [_rec(3)]}], cycle=_boom, dlq=dlq
    )
    _run(proc.run_raw_processor(stop_event=stop))

    assert dlq == [("__raw__", "processing_failed", 1)]
    assert fake.commits == [{0: 4}]


def test_processor_unconsumed_batch_goes_to_dlq_not_silent_commit(monkeypatch):
    async def _inactive(*a, **k):
        return False  # ciclo voltou cedo: integração inativa

    tp = _TP(0)
    dlq: list = []
    fake, stop = _install(monkeypatch, [{tp: [_rec(8)]}], cycle=_inactive, dlq=dlq)
    _run(proc.run_raw_processor(stop_event=stop))

    assert dlq == [("__raw__", "integration_unavailable", 1)]
    assert fake.commits == [{0: 9}]


def test_processor_dlq_failure_holds_offset(monkeypatch):
    async def _inactive(*a, **k):
        return False

    tp = _TP(0)
    fake, stop = _install(monkeypatch, [{tp: [_rec(8)]}], cycle=_inactive, dlq_ok=False)
    _run(proc.run_raw_processor(stop_event=stop))

    assert fake.commits == []
    assert fake.seeks == [(0, 8)]


def test_processor_skips_malformed_record(monkeypatch):
    async def _cycle(*a, **k):
        return True

    tp = _TP(0)
    bad = types.SimpleNamespace(offset=1, value=b"lixo")
    fake, stop = _install(monkeypatch, [{tp: [bad, _rec(2)]}], cycle=_cycle)
    _run(proc.run_raw_processor(stop_event=stop))

    assert fake.commits == [{0: 2}, {0: 3}]



def test_processor_survives_rebalance_mid_cycle(monkeypatch):
    """Visto no loadtest: ao subir réplicas, a partição é revogada durante um ciclo
    longo. O processor não commita nem faz seek nela (o novo dono relê) e segue."""
    tp = _TP(5)
    fake_holder: dict = {}

    async def _cycle(iid, stream, events, partition, ids=None):
        fake_holder["fake"].revoked.add(tp)
        return True

    fake, stop = _install(monkeypatch, [{tp: [_rec(1), _rec(2, 2, "b")]}], cycle=_cycle)
    fake_holder["fake"] = fake
    _run(proc.run_raw_processor(stop_event=stop))   # não levanta

    assert fake.commits == [] and fake.seeks == []
