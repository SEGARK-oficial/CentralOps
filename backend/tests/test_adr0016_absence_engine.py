"""ADR-0016 — ausência de evento em voo: compilação, presença no flush, emissão.

A metade do motor que roda no CICLO. O contrato que estes testes fixam:

* uma regra ``absence`` compila com a marca e o prazo próprio (teto de 7 d, não
  os 3600 s da janela deslizante), e é recusada com a razão certa quando falta
  chave, filtro ou prazo;
* no flush, as chaves de presença NUNCA chegam à escrita de Detection (contadas
  por CHAMADA, não por sentinela — ``except Exception`` engoliria sentinela), e
  o batimento do observador sai MESMO com zero matches;
* o teto de chaves vigiadas descarta só as NOVAS; Redis fora conta
  ``absence_unavailable`` e o flush das outras regras segue (R3);
* o 2004 emitido diz que é ausência (``types``, ``unmapped.absence``) e passa no
  gate estrutural que valida os produtores internos.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import fakeredis
import pytest

from backend.app.collectors import observability_store as obs
from backend.app.collectors import otel_metrics
from backend.app.collectors.inflight import absence as absence_mod
from backend.app.collectors.inflight import runtime as runtime_mod
from backend.app.collectors.inflight.matcher import CompiledInflightRule
from backend.app.collectors.inflight.runtime import (
    ERROR_REASONS,
    REJECT_REASONS,
    DetectionEmit,
    InflightAccumulator,
    compile_row,
    compile_rule,
    flush_inflight,
)
from backend.app.core.config import settings

_WHERE = json.dumps([
    {"field": "_centralops.event_type", "op": "eq", "value": "sophos.detection"},
    {"field": "normalized.metadata.event_code", "op": "eq", "value": "XDR-veeam-restorepointcreated"},
])


def _row(rid: int = 1, *, window: int = 93600, forget=None, group_by="_centralops.customer_name",
         where=_WHERE, **extra) -> SimpleNamespace:
    return SimpleNamespace(
        id=rid, name=f"abs-{rid}", severity_id=4, suppression_window_seconds=21600,
        rule_type="absence", eval_mode="inflight", where_json=where, group_by_field=group_by,
        window_seconds=window, absence_forget_seconds=forget, min_count=1, legs_json=None,
        emit_event=True, max_dedup_keys=None, **extra,
    )


def _event(customer: str, code: str = "XDR-veeam-restorepointcreated") -> dict:
    return {
        "_centralops": {"event_type": "sophos.detection", "stream": "sophos.detection",
                        "customer_name": customer, "event_id": f"ev-{customer}", "vendor": "sophos"},
        "normalized": {"metadata": {"event_code": code}, "class_uid": 2004, "time": 1},
        "raw": {},
    }


@pytest.fixture()
def redis():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture()
def quiet(monkeypatch, redis):
    """Redis falso + espião da escrita de Detection: devolve a lista de chaves
    que chegaram a ``_flush_sync``. Contagem, não sentinela."""
    chamadas: list[list[str]] = []

    def _flush(pending, _org):
        chamadas.append(list(pending))
        return ()

    monkeypatch.setattr(runtime_mod, "_flush_sync", _flush)
    monkeypatch.setattr(otel_metrics, "record", lambda *a, **k: None)
    monkeypatch.setattr(obs, "record_counter", lambda *a, **k: True)
    monkeypatch.setattr(obs, "_redis", lambda: redis)
    return chamadas


# ── compilação ─────────────────────────────────────────────────────────


def test_absence_compiles_with_its_own_window_cap_and_default_forget():
    rule, reason = compile_rule(_row(window=93600))
    assert reason is None and rule is not None
    assert rule.absence is True
    assert rule.window_seconds == 93600
    # 3 × prazo quando a coluna está vazia.
    assert rule.forget_seconds == 3 * 93600
    assert rule.min_count == 1
    assert rule.group_by_path == ("_centralops", "customer_name")
    # compile_row devolve UMA regra (não é sequência).
    rules, reason = compile_row(_row(window=93600))
    assert reason is None and len(rules) == 1 and rules[0].absence

    # O PAR: o mesmo prazo numa regra de LIMIAR estoura o teto da janela
    # deslizante. São dois tetos, e é isso que este teste prova.
    thr = SimpleNamespace(id=2, name="thr", severity_id=4, suppression_window_seconds=3600,
                          rule_type="threshold", where_json=_WHERE, group_by_field="raw.u",
                          min_count=5, window_seconds=93600, emit_event=False, max_dedup_keys=None)
    assert compile_rule(thr) == (None, "window_over_cap")


@pytest.mark.parametrize(
    "kw, reason",
    [
        ({"group_by": None}, "bad_absence"),
        ({"group_by": "customer"}, "group_by_root"),
        ({"where": "[]"}, "empty_where"),
        ({"window": 10}, "bad_absence"),
        ({"window": 7 * 24 * 3600 + 1}, "absence_window_over_cap"),
        ({"forget": 60}, "bad_absence"),  # esquecer antes do prazo: contradição
        ({"forget": 31 * 24 * 3600}, "bad_absence"),
    ],
)
def test_absence_rejections_carry_a_closed_reason(kw, reason):
    assert compile_rule(_row(**kw)) == (None, reason)
    assert reason in REJECT_REASONS


def test_absence_reasons_are_in_the_closed_enums():
    for r in ("bad_absence", "absence_window_over_cap"):
        assert r in REJECT_REASONS
    for r in ("absence_unavailable", "absence_key_cap", "absence_unobservable", "absence_source_lagging"):
        assert r in ERROR_REASONS


# ── presença no flush ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_flush_writes_presence_and_never_a_detection(quiet, redis):
    rule, _ = compile_rule(_row(1))
    acc = InflightAccumulator()
    acc.note_rules([rule])
    acc.add(rule, _event("acme"), organization_id=7)
    acc.add(rule, _event("beta"), organization_id=7)
    acc.add(rule, _event("acme"), organization_id=7)  # repetida: mesmo token

    await flush_inflight(acc, organization_id=7)

    # NENHUMA chamada de escrita de Detection: presença não é alerta.
    assert quiet == []
    seen = redis.hgetall(absence_mod.seen_key(7, 1))
    assert set(seen) == {"acme", "beta"}
    assert all(v.isdigit() for v in seen.values())
    meta = redis.hgetall(absence_mod.meta_key(7, 1))
    assert "last_cycle" in meta
    # TTL = esquecimento + folga, nunca sem TTL (regra apagada some sozinha).
    assert redis.ttl(absence_mod.seen_key(7, 1)) > rule.forget_seconds
    assert acc.matches[1] == 3


@pytest.mark.asyncio
async def test_heartbeat_is_written_even_with_zero_matches(quiet, redis):
    """O batimento é o que separa 'a fonte calou' de 'ninguém estava olhando'.
    Sem esta linha, um ciclo sem match deixaria o tique cego."""
    rule, _ = compile_rule(_row(1))
    acc = InflightAccumulator()
    acc.note_rules([rule])

    await flush_inflight(acc, organization_id=7)

    assert quiet == []
    assert "last_cycle" in redis.hgetall(absence_mod.meta_key(7, 1))
    assert redis.hgetall(absence_mod.seen_key(7, 1)) == {}


@pytest.mark.asyncio
async def test_key_cap_drops_only_new_keys_and_counts(quiet, redis, monkeypatch):
    rule, _ = compile_rule(_row(1))
    monkeypatch.setattr(settings, "ABSENCE_MAX_KEYS_PER_RULE", 1)
    # "beta" já vigiada de um ciclo anterior.
    redis.hset(absence_mod.seen_key(7, 1), mapping={"beta": 1})
    acc = InflightAccumulator()
    acc.note_rules([rule])
    acc.add(rule, _event("beta"), organization_id=7)   # existente: renova
    acc.add(rule, _event("gamma"), organization_id=7)  # nova: não cabe

    await flush_inflight(acc, organization_id=7)

    seen = redis.hgetall(absence_mod.seen_key(7, 1))
    assert set(seen) == {"beta"}
    assert seen["beta"] != "1"  # renovada
    assert acc.errors["absence_key_cap"] == {1: 1}


@pytest.mark.asyncio
async def test_redis_down_counts_unavailable_and_threshold_rules_still_flush(monkeypatch):
    class _Broken:
        def pipeline(self):
            raise ConnectionError("redis fora")

    chamadas: list[list[str]] = []
    monkeypatch.setattr(runtime_mod, "_flush_sync", lambda p, _o: chamadas.append(list(p)) or ())
    monkeypatch.setattr(otel_metrics, "record", lambda *a, **k: None)
    monkeypatch.setattr(obs, "record_counter", lambda *a, **k: True)
    monkeypatch.setattr(obs, "_redis", lambda: _Broken())

    abs_rule, _ = compile_rule(_row(1))
    thr = CompiledInflightRule(rule_id=2, name="thr", severity_id=4, suppression_window_seconds=3600,
                               group_by_path=("_centralops", "customer_name"), clauses=())
    acc = InflightAccumulator()
    acc.note_rules([abs_rule, thr])
    acc.add(abs_rule, _event("acme"), organization_id=7)
    acc.add(thr, _event("acme"), organization_id=7)

    await flush_inflight(acc, organization_id=7)

    assert acc.errors["absence_unavailable"] == {1: 1}
    # A regra de limiar chegou à escrita; a de ausência não (nem com Redis fora).
    assert chamadas == [["inflight:7:2:acme"]]


# ── emissão ────────────────────────────────────────────────────────────


def _absence_emit() -> DetectionEmit:
    return DetectionEmit(
        dedup_key="absence:7:1:acme", detection_id=42, rule_id=1, rule_name="abs-1",
        severity_id=4, integration_id=None, emit_event=True,
        source={
            "group_field": "_centralops.customer_name", "group_value": "acme",
            "stream": "sophos.detection",
            "absence": {"last_seen": 1000, "silent_for_seconds": 97200,
                        "expected_within_seconds": 93600, "forget_after_seconds": 280800},
        },
    )


def test_detection_event_says_it_is_an_absence():
    env = runtime_mod._build_detection_event(_absence_emit(), 7, now_ms=1_700_000_000_000)
    n = env["normalized"]
    assert n["class_uid"] == 2004 and n["activity_id"] == 1
    assert n["finding_info"]["types"] == ["inflight", "absence"]
    assert n["finding_info"]["uid"] == "absence:7:1:acme"
    assert "Ausência" in n["message"] and "97200 s" in n["message"] and "93600 s" in n["message"]
    assert n["unmapped"]["absence"]["silent_for_seconds"] == 97200
    assert n["unmapped"]["group_value"] == "acme"
    # Nenhum valor de evento além da chave vigiada viaja.
    assert "raw" not in json.dumps(n["unmapped"]).lower() or n["unmapped"].get("raw") is None


def test_threshold_event_is_untouched_by_the_absence_branch():
    emit = DetectionEmit(dedup_key="inflight:7:2:x", detection_id=1, rule_id=2, rule_name="thr",
                         severity_id=4, integration_id=None, source={"group_value": "x"})
    n = runtime_mod._build_detection_event(emit, 7, now_ms=1)["normalized"]
    assert n["finding_info"]["types"] == ["inflight"]
    assert "absence" not in n["unmapped"]


def test_absence_event_passes_the_internal_producer_gate(monkeypatch):
    """O gate estrutural da ADR-0012 valida o que a própria CentralOps emite e
    DESCARTA o que não é OCSF válido — um 2004 de ausência malformado seria
    uma Detection gravada que nunca chega ao SIEM, em silêncio."""
    from backend.app.collectors import pipeline as pipeline_mod

    monkeypatch.setattr(settings, "OCSF_VALIDATE_INTERNAL_PRODUCERS", True)
    env = runtime_mod._build_detection_event(_absence_emit(), 7, now_ms=1_700_000_000_000)
    kept = pipeline_mod._gate_internal_producers([env])
    assert len(kept) == 1
    assert kept[0]["normalized"]["finding_info"]["types"] == ["inflight", "absence"]
