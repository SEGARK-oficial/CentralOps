"""W1.6 — janela deslizante em voo: ``min_count`` em ``window_seconds`` para
regras inflight, com estado no Redis escrito só no flush.

O teste-guarda do plano: ``test_adr0015_inflight_window_count_is_sliding_not_tumbling``
— a contagem é sobre "os últimos W segundos", não sobre fatias fixas do relógio.
Relógio injetado, Redis falso: nada aqui depende de TTL.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import fakeredis
import pytest

from backend.app.collectors.inflight import runtime as runtime_mod
from backend.app.collectors.inflight import window as win
from backend.app.collectors.inflight.matcher import CompiledInflightRule
from backend.app.collectors.inflight.runtime import InflightAccumulator, compile_rule, flush_inflight
from backend.app.collectors import observability_store as obs
from backend.app.collectors import otel_metrics
from backend.app.core.config import settings


def _rule(rid=1, *, min_count=1, window=0):
    return CompiledInflightRule(rule_id=rid, name=f"r{rid}", severity_id=4, suppression_window_seconds=3600,
                                group_by_path=("u",), clauses=(), min_count=min_count, window_seconds=window)


@pytest.fixture
def redis():
    return fakeredis.FakeRedis(decode_responses=True)


# ── compile: caps e desligamento ───────────────────────────────────────────

def _row(**over):
    base = dict(id=7, name="r", where_json='[{"field":"normalized.a","op":"eq","value":"x"}]',
                group_by_field="normalized.u", min_count=5, window_seconds=300)
    base.update(over)
    return SimpleNamespace(**base)


def test_compile_carrega_janela_so_com_as_duas_pontas():
    c, _ = compile_rule(_row())
    assert (c.min_count, c.window_seconds) == (5, 300)
    c, _ = compile_rule(_row(min_count=1, window_seconds=300))
    assert (c.min_count, c.window_seconds) == (1, 0)  # sem janela: comportamento clássico
    c, _ = compile_rule(_row(min_count=5, window_seconds=0))
    assert (c.min_count, c.window_seconds) == (1, 0)
    c, _ = compile_rule(_row(min_count=None, window_seconds=None))
    assert (c.min_count, c.window_seconds) == (1, 0)


def test_compile_recusa_janela_e_contagem_acima_do_teto():
    assert compile_rule(_row(window_seconds=settings.INFLIGHT_MAX_WINDOW_SECONDS + 1))[1] == "window_over_cap"
    assert compile_rule(_row(min_count=settings.INFLIGHT_MAX_WINDOW_COUNT + 1))[1] == "window_count_over_cap"
    assert compile_rule(_row(window_seconds=settings.INFLIGHT_MAX_WINDOW_SECONDS))[1] is None


def test_razoes_novas_estao_declaradas():
    assert {"window_below", "window_unavailable"} <= set(runtime_mod.ERROR_REASONS)


# ── acumulador: conta hits por chave ───────────────────────────────────────

def test_acumulador_conta_hits_da_mesma_chave_no_ciclo():
    acc = InflightAccumulator()
    r = _rule(min_count=3, window=60)
    for _ in range(4):
        acc.add(r, {"u": "alice"}, organization_id=1)
    acc.add(r, {"u": "bob"}, organization_id=1)
    hits = {k.split(":")[-1]: v["hits"] for k, v in acc.pending.items()}
    assert hits == {"alice": 4, "bob": 1}
    assert acc.matches[1] == 5


# ── módulo window: deslizante, determinístico ──────────────────────────────

def test_adr0015_inflight_window_count_is_sliding_not_tumbling(redis):
    """W=10 s (fatias de 1 s). 3 matches em t=0, 2 em t=6 ⇒ 5 em t=6 (dispara);
    em t=11 a fatia de t=0 saiu da janela ⇒ 2 (não dispara). Tumbling teria
    zerado em t=10 e contado 2 — ou, pior, contado 5 até t=19."""
    items = {"k": (3, 5, 10)}
    assert win.apply_windows(redis, items, now=1000.0) == {"k": 3}
    assert win.apply_windows(redis, {"k": (2, 5, 10)}, now=1006.0) == {"k": 5}
    assert win.apply_windows(redis, {"k": (0, 5, 10)}, now=1011.0) == {"k": 2}
    assert win.apply_windows(redis, {"k": (0, 5, 10)}, now=1017.0) == {"k": 0}
    # Os buckets ganharam TTL (lixo recolhido sozinho).
    assert all(redis.ttl(k) > 0 for k in redis.keys("inflight:win:*"))


def test_bucket_ids_cobrem_a_janela_inteira():
    ids = win.bucket_ids(300, now=10_000)  # fatias de 30 s
    assert len(ids) == 11 and ids[-1] == 10_000 // 30 and ids[0] == ids[-1] - 10
    assert win.bucket_seconds(5) == 1 and win.bucket_seconds(3600) == 360
    assert win.windowed(_rule(min_count=2, window=10)) and not win.windowed(_rule(min_count=2)) and not win.windowed(_rule(window=10))


# ── flush: gating, evidência, fail-closed ──────────────────────────────────

@pytest.fixture
def espia(monkeypatch, redis):
    visto = {"pending": None}

    def _flush(pending, _org):
        visto["pending"] = {k: dict(v) for k, v in pending.items()}
        return ()
    monkeypatch.setattr(runtime_mod, "_flush_sync", _flush)
    monkeypatch.setattr(otel_metrics, "record", lambda *a, **k: None)
    monkeypatch.setattr(obs, "record_counter", lambda *a, **k: True)
    monkeypatch.setattr(obs, "_redis", lambda: redis)
    return visto


def test_flush_so_grava_quando_a_janela_alcanca_o_minimo(espia, redis, monkeypatch):
    r = _rule(min_count=3, window=60)
    monkeypatch.setattr(win.time, "time", lambda: 5000.0)
    acc = InflightAccumulator()
    acc.add(r, {"u": "alice"}, organization_id=1); acc.add(r, {"u": "alice"}, organization_id=1)  # 2 < 3
    acc.add(r, {"u": "bob"}, organization_id=1)                                                   # 1 < 3
    asyncio.run(flush_inflight(acc, organization_id=1))
    # Tudo retido ⇒ ``pending`` vazio ⇒ ``_flush_sync`` nem é chamado.
    assert not espia["pending"]
    assert acc.errors["window_below"] == {1: 2}

    # Ciclo seguinte, 20 s depois: alice chega a 3 ⇒ grava com evidência de janela.
    monkeypatch.setattr(win.time, "time", lambda: 5020.0)
    acc = InflightAccumulator()
    acc.add(r, {"u": "alice"}, organization_id=1)
    asyncio.run(flush_inflight(acc, organization_id=1))
    (key, item), = espia["pending"].items()
    assert key.endswith(":alice")
    assert item["source"]["window"] == {"count": 3, "seconds": 60, "min_count": 3}


def test_regra_sem_janela_nao_toca_o_redis(espia, redis):
    acc = InflightAccumulator()
    acc.add(_rule(), {"u": "alice"}, organization_id=1)
    asyncio.run(flush_inflight(acc, organization_id=1))
    assert len(espia["pending"]) == 1 and "window" not in espia["pending"]["inflight:1:1:alice"]["source"]
    assert redis.keys("inflight:win:*") == []


def test_redis_fora_e_fail_closed_e_contado(espia, monkeypatch):
    class _Down:
        def pipeline(self):
            raise ConnectionError("redis down")
    monkeypatch.setattr(obs, "_redis", lambda: _Down())
    acc = InflightAccumulator()
    acc.add(_rule(min_count=2, window=60), {"u": "alice"}, organization_id=1)
    acc.add(_rule(rid=2), {"u": "alice"}, organization_id=1)  # sem janela: segue
    asyncio.run(flush_inflight(acc, organization_id=1))
    assert list(espia["pending"]) == ["inflight:1:2:alice"]
    assert acc.errors["window_unavailable"] == {1: 1}
