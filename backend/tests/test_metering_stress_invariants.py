"""Stress test do revisor — invariantes de contagem do InVolumeAccumulator.

Verifica adversarialmente (independente dos testes do implementador):
  1. NUNCA perde nem dobra: N adds + flushes por threshold intercalados +
     exceção no meio + flush final => total EXATO (events e bytes), inclusive
     quando o flush final roda depois de flushes automáticos parciais.
  2. O(N/500): número de chamadas record_counter == 4 * n_flushes, com
     n_flushes == ceil(N/500) para 1 par (org, integ) — NÃO O(4N).
  3. Ciclo vazio: zero writes.
  4. Exceção injetada ENTRE acumular e flush (estilo soft-timeout): finally
     flusha o parcial UMA vez; repetir flush não duplica.
"""
from __future__ import annotations

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import math

import pytest

from backend.app.collectors import metrics
from backend.app.collectors import observability_store as obs
from backend.app.collectors.output._fastjson import dumps_bytes
from backend.app.collectors.reduction import metering
from backend.app.core.config import settings


class _Rec:
    def __init__(self):
        self.calls = []

    def __call__(self, kind, oid, metric, value=1.0, *, now=None):
        self.calls.append((kind, oid, metric, float(value)))


class _Spy:
    def __init__(self):
        self.incs = []
        self._l = {}

    def labels(self, **kw):
        self._l = kw
        return self

    def inc(self, a=1):
        self.incs.append((dict(self._l), float(a)))


@pytest.fixture()
def spies(monkeypatch):
    rec, ev, by = _Rec(), _Spy(), _Spy()
    monkeypatch.setattr(obs, "record_counter", rec)
    monkeypatch.setattr(metrics, "EVENTS_IN", ev)
    monkeypatch.setattr(metrics, "BYTES_IN", by)
    monkeypatch.setattr(settings, "COST_METERING_ENABLED", True)
    return rec, ev, by


_RAW = {"e": "x", "p": "y" * 40}
_B = float(len(dumps_bytes(_RAW)))


def _sum_events(rec, kind="source", oid="42"):
    return sum(v for k, o, m, v in rec.calls if k == kind and o == oid and m == "events_in")


def _sum_bytes(rec, kind="source", oid="42"):
    return sum(v for k, o, m, v in rec.calls if k == kind and o == oid and m == "bytes_in")


def test_stress_10k_exact_totals_and_flush_count(spies):
    rec, ev, by = spies
    N = 10_000
    acc = metering.InVolumeAccumulator(flush_seconds=1e9)  # só threshold de 500
    for _ in range(N):
        acc.add(7, 42, _RAW)
    acc.flush()  # flush final do finally (aqui: buffer já vazio — 10000 % 500 == 0)

    assert _sum_events(rec) == float(N)
    assert _sum_bytes(rec) == N * _B
    assert _sum_events(rec, "org", "7") == float(N)
    assert sum(a for _, a in ev.incs) == float(N)
    assert sum(a for _, a in by.incs) == N * _B
    # O(N/500): 4 record_counter por flush, ceil(10000/500)=20 flushes => 80, não 40000.
    n_flushes = math.ceil(N / 500)
    assert len(rec.calls) == 4 * n_flushes, len(rec.calls)
    assert len(ev.incs) == n_flushes


def test_stress_exception_midloop_partial_once_no_double(spies):
    rec, ev, _ = spies
    N, STOP = 1_234, 1_101  # 2 flushes automáticos (500, 1000) + parcial de 101
    acc = metering.InVolumeAccumulator(flush_seconds=1e9)

    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom):
        try:
            for i in range(N):
                acc.add(7, 42, _RAW)
                if i + 1 == STOP:
                    raise Boom()
        finally:
            acc.flush()

    assert _sum_events(rec) == float(STOP)  # nem perdeu nem dobrou
    assert _sum_bytes(rec) == STOP * _B
    assert len(rec.calls) == 4 * 3  # 2 automáticos + 1 final
    # flush repetido (double-finally hipotético) não duplica:
    acc.flush()
    assert _sum_events(rec) == float(STOP)


def test_stress_empty_cycle_zero_writes(spies):
    rec, ev, by = spies
    acc = metering.InVolumeAccumulator()
    acc.flush()
    acc.flush()
    assert rec.calls == [] and ev.incs == [] and by.incs == []


def test_stress_multi_pair_totals(spies):
    """Fan de pares (multi-integração no mesmo ciclo): cada par soma certo."""
    rec, _, _ = spies
    acc = metering.InVolumeAccumulator(flush_events=7, flush_seconds=1e9)
    for i in range(100):
        acc.add(7 if i % 2 else 8, 42 if i % 3 else 43, _RAW)
    acc.flush()
    assert _sum_events(rec, "org", "7") + _sum_events(rec, "org", "8") == 100.0
    assert _sum_events(rec, "source", "42") + _sum_events(rec, "source", "43") == 100.0
