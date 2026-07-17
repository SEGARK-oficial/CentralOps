"""Batching do metering IN (InVolumeAccumulator) — pré-condição do default ON.

O ``record_in`` por-evento fazia 4 pipelines Redis SÍNCRONOS por evento no event
loop de coleta (~0,8ms/evento) — o motivo legítimo do antigo default off. Com o
flip de ``COST_METERING_ENABLED`` para ON, o pipeline passa a acumular
(eventos, bytes) por ``(org, integração)`` e a gravar por AGREGADO. Cobre:

  * **soma correta** + equivalência ``N × record_in ≡ 1 × record_in_batch``
    (mesmos totais em OTel e no observability_store);
  * **flush por contagem** (>= flush_events) e **por tempo** (>= flush_seconds,
    clock injetado) — preserva a granularidade de minuto dos buckets;
  * **flush no finally** com exceção no meio do loop: o PARCIAL é gravado e o
    erro original NUNCA é mascarado (padrão do incidente ``_track_claims``);
  * **fail-open** com o store quebrado (add/flush jamais levantam no hot path);
  * **flag-off = no-op** (zero serialização) — a garantia de rollback continua;
  * o **pipeline** instancia o acumulador ANTES do try e faz flush no finally
    mesmo em falha precoce (antes do loop de coleta).
"""
from __future__ import annotations

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import asyncio

import pytest

from backend.app.collectors import metrics
from backend.app.collectors import observability_store as obs
from backend.app.collectors.output._fastjson import dumps_bytes
from backend.app.collectors.reduction import metering
from backend.app.core.config import settings


class _RecordingCounter:
    """Spy para o store: grava (kind, oid, metric, value)."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __call__(self, kind, oid, metric, value=1.0, *, now=None) -> None:
        self.calls.append((kind, oid, metric, float(value)))


class _SpyInstrument:
    """Spy para uma fachada OTel: grava labels + inc."""

    def __init__(self) -> None:
        self.incs: list[tuple] = []
        self._last: dict = {}

    def labels(self, **kw):
        self._last = kw
        return self

    def inc(self, amount: float = 1) -> None:
        self.incs.append((dict(self._last), float(amount)))


@pytest.fixture()
def spies(monkeypatch):
    rec = _RecordingCounter()
    ev_in, by_in = _SpyInstrument(), _SpyInstrument()
    monkeypatch.setattr(obs, "record_counter", rec)
    monkeypatch.setattr(metrics, "EVENTS_IN", ev_in)
    monkeypatch.setattr(metrics, "BYTES_IN", by_in)
    return rec, ev_in, by_in


class _Clock:
    """Relógio monotônico injetável."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t


_RAW = {"event": "x", "payload": "y" * 50}
_RAW_BYTES = float(len(dumps_bytes(_RAW)))


# ── soma correta + equivalência N record_in == 1 record_in_batch ─────────────────


def test_accumulator_sums_and_flush_writes_one_aggregate(monkeypatch, spies):
    rec, ev_in, by_in = spies
    monkeypatch.setattr(settings, "COST_METERING_ENABLED", True)
    acc = metering.InVolumeAccumulator(flush_events=100, flush_seconds=999.0, clock=_Clock())

    for _ in range(3):
        acc.add(7, 42, _RAW)
    # nada gravado antes do flush (é este o ponto do batching).
    assert rec.calls == [] and ev_in.incs == [] and by_in.incs == []

    acc.flush()
    # OTel: UM inc agregado por contador (não 3).
    assert ev_in.incs == [({"org_id": "7", "integration_id": "42"}, 3.0)]
    assert by_in.incs == [({"org_id": "7", "integration_id": "42"}, 3 * _RAW_BYTES)]
    # store: 4 escritas agregadas (source×2 + org×2), não 12.
    assert rec.calls == [
        ("source", "42", "events_in", 3.0),
        ("source", "42", "bytes_in", 3 * _RAW_BYTES),
        ("org", "7", "events_in", 3.0),
        ("org", "7", "bytes_in", 3 * _RAW_BYTES),
    ]

    # flush repetido sem pendências = no-op (buffer zerado).
    acc.flush()
    assert len(rec.calls) == 4 and len(ev_in.incs) == 1


def _totals(rec: _RecordingCounter) -> dict:
    out: dict = {}
    for kind, oid, metric, value in rec.calls:
        out[(kind, oid, metric)] = out.get((kind, oid, metric), 0.0) + value
    return out


def test_n_record_in_equivalent_to_one_record_in_batch(monkeypatch, spies):
    rec, ev_in, by_in = spies
    monkeypatch.setattr(settings, "COST_METERING_ENABLED", True)

    for _ in range(5):
        metering.record_in(7, 42, _RAW)
    per_event_totals = _totals(rec)
    per_event_otel = (
        sum(a for _, a in ev_in.incs),
        sum(a for _, a in by_in.incs),
    )

    rec.calls.clear()
    ev_in.incs.clear()
    by_in.incs.clear()
    metering.record_in_batch(7, 42, 5, 5 * _RAW_BYTES)

    assert _totals(rec) == per_event_totals
    assert (sum(a for _, a in ev_in.incs), sum(a for _, a in by_in.incs)) == per_event_otel


def test_record_in_batch_fail_closed_without_org_or_integration(monkeypatch, spies):
    """Espelha o contrato do record_in: sem org → sem série org nem OTel
    (anti cross-tenant); sem integração → sem série source."""
    rec, ev_in, by_in = spies
    monkeypatch.setattr(settings, "COST_METERING_ENABLED", True)

    metering.record_in_batch(None, 42, 2, 100.0)
    assert ev_in.incs == [] and by_in.incs == []
    assert not any(c[0] == "org" for c in rec.calls)
    assert ("source", "42", "events_in", 2.0) in rec.calls

    rec.calls.clear()
    metering.record_in_batch(7, None, 2, 100.0)
    assert ev_in.incs == [] and by_in.incs == []
    assert not any(c[0] == "source" for c in rec.calls)
    assert ("org", "7", "events_in", 2.0) in rec.calls


# ── thresholds de flush: contagem e tempo ─────────────────────────────────────────


def test_accumulator_flushes_on_event_count(monkeypatch, spies):
    rec, ev_in, _ = spies
    monkeypatch.setattr(settings, "COST_METERING_ENABLED", True)
    acc = metering.InVolumeAccumulator(flush_events=3, flush_seconds=999.0, clock=_Clock())

    acc.add(7, 42, _RAW)
    acc.add(7, 42, _RAW)
    assert rec.calls == []  # abaixo do threshold
    acc.add(7, 42, _RAW)  # 3º evento → flush automático
    assert ("source", "42", "events_in", 3.0) in rec.calls
    assert ev_in.incs == [({"org_id": "7", "integration_id": "42"}, 3.0)]

    # o buffer recomeça do zero após o flush.
    n = len(rec.calls)
    acc.add(7, 42, _RAW)
    assert len(rec.calls) == n


def test_accumulator_flushes_on_time_and_resets_window(monkeypatch, spies):
    rec, ev_in, _ = spies
    monkeypatch.setattr(settings, "COST_METERING_ENABLED", True)
    clock = _Clock(0.0)
    acc = metering.InVolumeAccumulator(flush_events=10_000, flush_seconds=15.0, clock=clock)

    acc.add(7, 42, _RAW)  # janela começa no 1º pendente (t=0)
    clock.t = 10.0
    acc.add(7, 42, _RAW)
    assert rec.calls == []  # 10s < 15s
    clock.t = 16.0
    acc.add(7, 42, _RAW)  # 16s >= 15s → flush leva os 3 (preserva minuto do bucket)
    assert ("source", "42", "events_in", 3.0) in rec.calls
    assert ev_in.incs == [({"org_id": "7", "integration_id": "42"}, 3.0)]

    # janela reinicia no próximo pendente (t=20), não no flush anterior.
    n = len(rec.calls)
    clock.t = 20.0
    acc.add(7, 42, _RAW)
    clock.t = 34.0
    acc.add(7, 42, _RAW)  # 14s < 15s → segue pendente
    assert len(rec.calls) == n
    clock.t = 35.5
    acc.add(7, 42, _RAW)  # 15.5s >= 15s → flush com 3
    assert ("source", "42", "events_in", 3.0) in rec.calls[n:]


# ── flush final no finally: parcial gravado, erro original preservado ─────────────


class _Boom(RuntimeError):
    """Erro original que o flush do finally NÃO pode mascarar."""


def test_final_flush_in_finally_keeps_partial_and_original_error(monkeypatch, spies):
    rec, ev_in, _ = spies
    monkeypatch.setattr(settings, "COST_METERING_ENABLED", True)
    acc = metering.InVolumeAccumulator(flush_events=10_000, flush_seconds=999.0, clock=_Clock())

    with pytest.raises(_Boom):
        try:
            for i in range(5):
                acc.add(7, 42, _RAW)
                if i == 1:
                    raise _Boom("colapso no meio do ciclo")
        finally:
            acc.flush()  # padrão do pipeline: flush best-effort no finally

    # o PARCIAL (2 eventos) foi gravado; a exceção original propagou intacta.
    assert ("source", "42", "events_in", 2.0) in rec.calls
    assert ev_in.incs == [({"org_id": "7", "integration_id": "42"}, 2.0)]


def test_accumulator_fail_open_when_store_and_otel_broken(monkeypatch):
    """Redis/OTel quebrados: add+flush jamais levantam (best-effort, hot path)."""
    monkeypatch.setattr(settings, "COST_METERING_ENABLED", True)

    def _raise(*a, **k):
        raise RuntimeError("redis down")

    class _BrokenInstrument:
        def labels(self, **kw):
            raise RuntimeError("otel down")

    monkeypatch.setattr(obs, "record_counter", _raise)
    monkeypatch.setattr(metrics, "EVENTS_IN", _BrokenInstrument())
    monkeypatch.setattr(metrics, "BYTES_IN", _BrokenInstrument())

    acc = metering.InVolumeAccumulator(flush_events=2, flush_seconds=999.0, clock=_Clock())
    for _ in range(5):
        acc.add(7, 42, _RAW)  # inclui flushes automáticos com o store quebrado
    acc.flush()  # não levanta


def test_accumulator_add_survives_event_bytes_failure(monkeypatch, spies):
    """Falha ao medir os bytes de UM evento não quebra a coleta nem envenena o
    buffer (o evento problemático é pulado; os demais seguem contando)."""
    rec, _, _ = spies
    monkeypatch.setattr(settings, "COST_METERING_ENABLED", True)
    marker = object()
    orig = metering._event_bytes

    def _boomy(raw):
        if raw is marker:
            raise RuntimeError("serialização quebrou")
        return orig(raw)

    monkeypatch.setattr(metering, "_event_bytes", _boomy)
    acc = metering.InVolumeAccumulator(flush_events=100, flush_seconds=999.0, clock=_Clock())

    acc.add(7, 42, marker)  # falha → engolida em debug, nada acumulado
    acc.add(7, 42, _RAW)  # o acumulador segue funcional
    acc.flush()
    assert ("source", "42", "events_in", 1.0) in rec.calls


# ── flag-off = no-op (garantia de rollback preservada) ────────────────────────────


def test_accumulator_add_is_noop_without_serialization_when_flag_off(monkeypatch, spies):
    rec, ev_in, by_in = spies
    monkeypatch.setattr(settings, "COST_METERING_ENABLED", False)
    called = {"n": 0}
    import backend.app.collectors.output._fastjson as fj

    orig = fj.dumps_bytes
    monkeypatch.setattr(
        fj, "dumps_bytes", lambda o: (called.__setitem__("n", called["n"] + 1) or orig(o))
    )

    acc = metering.InVolumeAccumulator(flush_events=1, flush_seconds=0.0, clock=_Clock())
    acc.add(7, 42, _RAW)
    acc.flush()
    assert called["n"] == 0  # nem serializa
    assert rec.calls == [] and ev_in.incs == [] and by_in.incs == []


def test_record_in_batch_is_noop_when_flag_off(monkeypatch, spies):
    rec, ev_in, by_in = spies
    monkeypatch.setattr(settings, "COST_METERING_ENABLED", False)
    metering.record_in_batch(7, 42, 10, 1000.0)
    assert rec.calls == [] and ev_in.incs == [] and by_in.incs == []


# ── wiring do pipeline: acumulador antes do try + flush no finally ────────────────


class _FakeRedis:
    async def aclose(self) -> None:
        return None


def test_pipeline_flushes_accumulator_in_finally_even_on_early_failure(monkeypatch):
    """O acumulador é instanciado ANTES do try (padrão _track_claims) e o finally
    SEMPRE faz o flush final — inclusive quando o ciclo falha antes do loop de
    coleta — sem mascarar o erro original."""
    from backend.app.collectors import pipeline

    class _SpyAcc:
        instances: list = []

        def __init__(self, **kw) -> None:
            self.flushes = 0
            _SpyAcc.instances.append(self)

        def add(self, *a, **k) -> None:  # pragma: no cover — não alcançado aqui
            pass

        def flush(self) -> None:
            self.flushes += 1

    monkeypatch.setattr(metering, "InVolumeAccumulator", _SpyAcc)
    monkeypatch.setattr(
        "backend.app.collectors.celery_app.get_worker_redis", lambda: _FakeRedis()
    )

    def _raise(*args, **kwargs):
        raise _Boom("db down")

    monkeypatch.setattr(pipeline.database, "SessionLocal", _raise)

    with pytest.raises(_Boom):
        asyncio.run(pipeline._run_collection_once(integration_id=999, stream="alerts"))

    assert len(_SpyAcc.instances) == 1
    assert _SpyAcc.instances[0].flushes == 1
