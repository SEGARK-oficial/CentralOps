"""Regressão — séries per-rota que alimentam a UI /flow.

As séries ``route`` e ``drop`` (lidas por ``routed_per_min``/``drop_per_min`` em
``GET /collectors/routes/flow``) DEVEM ser gravadas para TODA rota casada — não só
quando a amostragem de redução (``sampled_per_route``) está ativa.

Bug (pré-fix): o ``record_counter("route", id, action, count)`` do split route|drop
estava DENTRO do loop de ``sampled_per_route`` em ``_enqueue_routed``. Com amostragem
OFF (default de produção), a série ``route`` nunca era escrita → a /flow mostrava
``0/min`` em TODA rota e a aresta rota→destino ficava idle/pontilhada, mesmo com o
destino recebendo dados (o destino tem seu PRÓPRIO counter ``sent``, por isso aparecia
volume no destino e 0 na rota). Além disso, o ``action``/``count`` usados vinham
"vazados" do loop anterior (valor errado) e havia risco de ``NameError``.
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
from backend.app.core.config import settings


def _result(per_route, sub_batches, *, sampled_per_route=None):
    """RoutingResult duck-typed (só os campos que ``_enqueue_routed`` lê)."""
    return types.SimpleNamespace(
        routed=sum(per_route.values()),
        dropped=0,
        fallback=0,
        residency_blocked=0,
        loop_blocked=0,
        unrouted=0,
        unrouted_events=[],
        per_route=dict(per_route),
        sampled_per_route=dict(sampled_per_route or {}),
        sub_batches=sub_batches,
    )


def _patch(monkeypatch, result):
    """Isola ``_enqueue_routed``: sem DB, sem Kafka, com Redis fake para a store."""
    monkeypatch.setattr(pipeline, "_load_destination_residency", lambda ids: {})
    monkeypatch.setattr(pipeline, "_load_wazuh_loop_destination_ids", lambda ids: frozenset())
    monkeypatch.setattr(pipeline, "_load_fallback_destination_id", lambda org: None)
    monkeypatch.setattr(routing, "route_batch", lambda *a, **k: result)
    monkeypatch.setattr(tracing, "carrier", lambda: {})
    monkeypatch.setattr(settings, "EVENT_DATAPLANE", "celery")
    monkeypatch.setattr(tasks, "dispatch_to_destination", MagicMock())

    def _no_kafka(*_a, **_k):
        raise AssertionError("caminho celery não deveria produzir ao Kafka")

    monkeypatch.setattr(dataplane, "produce_delivery", _no_kafka)
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(obs, "_redis", lambda: r)
    return r


def test_route_action_writes_routed_series_without_sampling(monkeypatch):
    """Rota ``action=route``, amostragem OFF: a série ``route`` recebe a contagem
    casada → ``routed_per_min`` > 0 na /flow (CERNE do fix)."""
    route = types.SimpleNamespace(id="7", action="route", destination_ids=["dest-1"])
    _patch(monkeypatch, _result({"7": 5}, {"dest-1": [{"e": i} for i in range(5)]}))

    pipeline._enqueue_routed([{"e": 1}], [route])

    assert obs.read_window_total("route", "7", "matched", minutes=60) == 5.0
    # O que estava quebrado: a série ``route`` é escrita mesmo SEM amostragem.
    assert obs.read_window_total("route", "7", "route", minutes=60) == 5.0
    assert obs.read_window_total("route", "7", "drop", minutes=60) == 0.0


def test_drop_action_writes_drop_series(monkeypatch):
    """Rota ``action=drop``: a série ``drop`` recebe a contagem casada
    (``drop_per_min`` > 0) e ``route`` fica zerada."""
    route = types.SimpleNamespace(id="9", action="drop", destination_ids=[])
    _patch(monkeypatch, _result({"9": 4}, {}))

    pipeline._enqueue_routed([{"e": 1}], [route])

    assert obs.read_window_total("route", "9", "drop", minutes=60) == 4.0
    assert obs.read_window_total("route", "9", "route", minutes=60) == 0.0


def test_sampling_still_records_events_dropped(monkeypatch):
    """Ao mover o split route|drop p/ FORA do loop de sampling, NÃO regredimos a
    série ``events_dropped`` (redução) — ela segue sendo gravada, e ``route``
    também é escrita (a rota entregou o que não foi amostrado)."""
    route = types.SimpleNamespace(id="3", action="route", destination_ids=["dest-1"])
    _patch(monkeypatch, _result({"3": 10}, {"dest-1": [{"e": 1}]}, sampled_per_route={"3": 4}))

    pipeline._enqueue_routed([{"e": 1}], [route])

    assert obs.read_window_total("route", "3", "route", minutes=60) == 10.0
    assert obs.read_window_total("route", "3", "events_dropped", minutes=60) == 4.0


def test_multiple_routes_each_get_own_action_bucket(monkeypatch):
    """Duas rotas de ações distintas no MESMO batch: cada uma cai no seu bucket
    (route vs drop), sem o ``action`` vazar entre elas (bug original usava o
    ``action`` da última iteração do loop anterior)."""
    r_route = types.SimpleNamespace(id="a", action="route", destination_ids=["dest-1"])
    r_drop = types.SimpleNamespace(id="b", action="drop", destination_ids=[])
    _patch(monkeypatch, _result({"a": 6, "b": 2}, {"dest-1": [{"e": 1}]}))

    pipeline._enqueue_routed([{"e": 1}], [r_route, r_drop])

    assert obs.read_window_total("route", "a", "route", minutes=60) == 6.0
    assert obs.read_window_total("route", "a", "drop", minutes=60) == 0.0
    assert obs.read_window_total("route", "b", "drop", minutes=60) == 2.0
    assert obs.read_window_total("route", "b", "route", minutes=60) == 0.0
