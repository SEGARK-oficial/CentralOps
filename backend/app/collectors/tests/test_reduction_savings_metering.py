"""Regressão — bytes_saved (Evitado/Redução) para as alavancas SAMPLING e SUPPRESSION.

Gap corrigido: as alavancas de redução ``sample`` (routing/engine.py) e ``suppress``
(pipeline.py) DESCARTAVAM volume real mas NUNCA chamavam ``record_saving`` — então
``obs:org:{id}:bytes_saved`` ficava 0 e os cards "Evitado"/"Redução" (GET /collectors/
cost-summary) mostravam 0 mesmo com a alavanca ligada e eventos sendo jogados fora.
(Só ``trim`` e ``aggregate`` creditavam savings.)

Convenção de bytes: o volume evitado é medido com o MESMO serializador da entrega
(``dumps_bytes``), no envelope — mesma unidade de ``bytes_out`` (Entregue), para a razão
Redução = evitado/(entregue+evitado) ser coerente.
"""

from __future__ import annotations

import os
import types
from unittest.mock import MagicMock

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import fakeredis

from backend.app.collectors import (
    dataplane,
    observability_store as obs,
    pipeline,
    routing,
    tasks,
    tracing,
)
from backend.app.collectors.output._fastjson import dumps_bytes
from backend.app.collectors.reduction import metering
from backend.app.collectors.routing.engine import CompiledRoute, SamplingConfig, route_batch
from backend.app.core.config import settings

_W = 180  # janela de leitura (min) = janela da /cost-summary


def _fake_redis(monkeypatch):
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(obs, "_redis", lambda: r)
    return r


def _flags(monkeypatch, *, metering_on: bool, sample=False, suppress=False):
    monkeypatch.setattr(settings, "COST_METERING_ENABLED", metering_on)
    monkeypatch.setattr(settings, "REDUCTION_SAMPLE_ENABLED", sample)
    monkeypatch.setattr(settings, "REDUCTION_SUPPRESS_ENABLED", suppress)


# ── metering.record_sample_saving ────────────────────────────────────────────
def test_sample_saving_written_when_both_flags_on(monkeypatch):
    _flags(monkeypatch, metering_on=True, sample=True)
    _fake_redis(monkeypatch)
    metering.record_sample_saving(7, 2048.0)
    assert obs.read_window_total("org", "7", "bytes_saved", minutes=_W) == 2048.0


def test_sample_saving_noop_when_lever_off(monkeypatch):
    _flags(monkeypatch, metering_on=True, sample=False)  # metering on, sample OFF
    _fake_redis(monkeypatch)
    metering.record_sample_saving(7, 2048.0)
    assert obs.read_window_total("org", "7", "bytes_saved", minutes=_W) == 0.0


def test_sample_saving_noop_when_metering_off(monkeypatch):
    _flags(monkeypatch, metering_on=False, sample=True)  # master OFF
    _fake_redis(monkeypatch)
    metering.record_sample_saving(7, 2048.0)
    assert obs.read_window_total("org", "7", "bytes_saved", minutes=_W) == 0.0


def test_sample_saving_fail_closed_on_missing_org(monkeypatch):
    _flags(monkeypatch, metering_on=True, sample=True)
    _fake_redis(monkeypatch)
    metering.record_sample_saving(None, 2048.0)  # org ausente → nada gravado
    assert obs.read_window_total("org", "None", "bytes_saved", minutes=_W) == 0.0


# ── metering.record_suppress_saving ──────────────────────────────────────────
def test_suppress_saving_measures_envelope_bytes(monkeypatch):
    _flags(monkeypatch, metering_on=True, suppress=True)
    _fake_redis(monkeypatch)
    env = {"_centralops": {"organization_id": 9, "event_id": "e1"}, "normalized": {"a": 1}, "raw": {"b": 2}}
    metering.record_suppress_saving(9, env)
    expected = float(len(dumps_bytes(env)))  # MESMO serializador da entrega
    assert obs.read_window_total("org", "9", "bytes_saved", minutes=_W) == expected


def test_suppress_saving_noop_when_lever_off(monkeypatch):
    _flags(monkeypatch, metering_on=True, suppress=False)  # suppress OFF
    _fake_redis(monkeypatch)
    metering.record_suppress_saving(9, {"_centralops": {"organization_id": 9}})
    assert obs.read_window_total("org", "9", "bytes_saved", minutes=_W) == 0.0


# ── engine: acúmulo de sampled_bytes_per_org ─────────────────────────────────
def _sampling_route():
    # sample_percent=0 → 0% passa → TODOS os eventos casados são amostrados p/ fora
    # (determinístico); protect_detection=False para a rota poder ser amostrada.
    return CompiledRoute(
        id="r1", name="r1", priority=100, condition={}, action="route",
        destination_ids=("d1",), is_final=True, enabled=True,
        sample_percent=0, protect_detection=False,
    )


def test_engine_accumulates_sampled_bytes_per_org():
    batch = [{"_centralops": {"organization_id": 7, "event_id": f"e-{i}"}} for i in range(5)]
    res = route_batch(batch, [_sampling_route()], sampling=SamplingConfig(enabled=True))
    assert res.sampled == 5
    assert "d1" not in res.sub_batches  # nada entregue (tudo amostrado)
    expected = float(sum(len(dumps_bytes(e)) for e in batch))
    assert res.sampled_bytes_per_org == {7: expected}


def test_engine_no_sampled_bytes_when_sampling_off():
    batch = [{"_centralops": {"organization_id": 7, "event_id": f"e-{i}"}} for i in range(5)]
    res = route_batch(batch, [_sampling_route()], sampling=None)  # sampling desligado
    assert res.sampled == 0
    assert res.sampled_bytes_per_org == {}
    assert len(res.sub_batches["d1"]) == 5  # tudo entregue


def test_engine_sampling_never_raises_on_unserializable_envelope():
    """INVARIANTE: metering NUNCA derruba o roteamento. Um envelope não-serializável
    (ref. circular) amostrado-para-fora NÃO pode levantar em route_batch — a medição de
    bytes é best-effort (0), o lote inteiro continua roteável (sem poison-pill)."""
    circular: dict = {}
    circular["self"] = circular  # ref. circular → dumps_bytes levanta
    env = {"_centralops": {"organization_id": 7, "event_id": "e1"}, "raw": circular}
    # NÃO deve levantar; retorna BatchRouting normalmente.
    res = route_batch([env], [_sampling_route()], sampling=SamplingConfig(enabled=True))
    assert res.sampled == 1
    # bytes daquele evento não creditados (serialização falhou → 0), mas SEM crash.
    assert res.sampled_bytes_per_org.get(7, 0.0) == 0.0


# ── pipeline._enqueue_routed: consome sampled_bytes_per_org ───────────────────
def _result(**over):
    base = dict(
        routed=0, dropped=0, fallback=0, residency_blocked=0, loop_blocked=0,
        unrouted=0, unrouted_events=[], per_route={}, sampled_per_route={},
        sampled_bytes_per_org={}, sub_batches={},
    )
    base.update(over)
    return types.SimpleNamespace(**base)


def test_enqueue_routed_writes_sample_bytes_saved(monkeypatch):
    _flags(monkeypatch, metering_on=True, sample=True)
    r = _fake_redis(monkeypatch)
    monkeypatch.setattr(pipeline, "_load_destination_residency", lambda ids: {})
    monkeypatch.setattr(pipeline, "_load_wazuh_loop_destination_ids", lambda ids: frozenset())
    monkeypatch.setattr(pipeline, "_load_fallback_destination_id", lambda org: None)
    monkeypatch.setattr(routing, "route_batch", lambda *a, **k: _result(
        sampled_per_route={"r1": 3}, sampled_bytes_per_org={7: 3000.0}))
    monkeypatch.setattr(tracing, "carrier", lambda: {})
    monkeypatch.setattr(settings, "EVENT_DATAPLANE", "celery")
    monkeypatch.setattr(tasks, "dispatch_to_destination", MagicMock())
    monkeypatch.setattr(dataplane, "produce_delivery", lambda *a, **k: None)

    pipeline._enqueue_routed([{"e": 1}], [types.SimpleNamespace(id="r1", action="route", destination_ids=["d1"])])

    assert obs.read_window_total("org", "7", "bytes_saved", minutes=_W) == 3000.0
