"""ADR-0015 X1 — sequência entre fontes em voo (evento A de Sophos + evento B
de Okta, mesmo usuário, mesma janela).

O ciclo de coleta é mono-fonte por construção: Sophos e Okta nunca estão no
mesmo laço. A W1.6 tirou o estado do laço (Redis, escrito só no flush); a
sequência usa o mesmo substrato com outra forma: um HASH por (regra, valor de
junção), um campo por perna, TTL = janela. A regra fecha quando todas as
pernas foram vistas.

O que torna isso "cross-source" de verdade é a chave de junção POR PERNA:
Okta/Entra escrevem ``normalized.user.name``, Sophos/CrowdStrike escrevem
``normalized.actor.user.name``. Cada perna aponta o seu caminho; o valor é o
que junta.

Restrições que continuam valendo: o matcher segue puro (uma perna é uma regra
comum para ele); I/O só no flush; fail-closed sem Redis; tetos com teste.
"""

from __future__ import annotations

import asyncio
import json
import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

from types import SimpleNamespace

import fakeredis
import pytest

from backend.app.collectors import observability_store as obs
from backend.app.collectors import otel_metrics
from backend.app.collectors.inflight import runtime as runtime_mod
from backend.app.collectors.inflight.matcher import evaluate_ruleset
from backend.app.collectors.inflight.runtime import (
    InflightAccumulator,
    compile_row,
    compile_rule,
    flush_inflight,
    validate_legs_json,
)
from backend.app.core.config import settings

_LEGS = [
    {
        "label": "okta_mfa_fail",
        "stream": "okta.system_log",
        "where": [{"field": "normalized.class_uid", "op": "eq", "value": 3002},
                  {"field": "normalized.status_id", "op": "eq", "value": 2}],
        "join_path": "normalized.user.name",
    },
    {
        "label": "sophos_process",
        "stream": "sophos.siem_event",
        "where": [{"field": "normalized.class_uid", "op": "eq", "value": 2004}],
        "join_path": "normalized.actor.user.name",
    },
]


def _row(rid: int = 1, *, legs=_LEGS, window: int = 900, **extra) -> SimpleNamespace:
    return SimpleNamespace(
        id=rid, name=f"seq-{rid}", severity_id=4, suppression_window_seconds=3600,
        rule_type="sequence", legs_json=json.dumps(legs), window_seconds=window,
        min_count=1, group_by_field=None, where_json=None, emit_event=False,
        max_dedup_keys=None, **extra,
    )


def _okta(user: str, *, status_id: int = 2, event_id: str = "okta-1") -> dict:
    return {
        "_centralops": {"stream": "okta.system_log", "event_id": event_id, "vendor": "okta",
                        "platform": "okta", "event_type": "okta.system_log"},
        "normalized": {"class_uid": 3002, "status_id": status_id, "time": 1_000,
                       "user": {"name": user}},
    }


def _sophos(user: str, *, event_id: str = "sophos-1") -> dict:
    return {
        "_centralops": {"stream": "sophos.siem_event", "event_id": event_id, "vendor": "sophos",
                        "platform": "sophos", "event_type": "sophos.siem_event"},
        "normalized": {"class_uid": 2004, "time": 2_000, "actor": {"user": {"name": user}}},
    }


@pytest.fixture()
def redis():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture()
def espia(monkeypatch, redis):
    """Captura o ``pending`` que chegaria à escrita e planta o Redis falso."""
    visto: dict = {"pending": None}

    def _flush(pending, _org):
        visto["pending"] = {k: dict(v) for k, v in pending.items()}
        return ()

    monkeypatch.setattr(runtime_mod, "_flush_sync", _flush)
    monkeypatch.setattr(otel_metrics, "record", lambda *a, **k: None)
    monkeypatch.setattr(obs, "record_counter", lambda *a, **k: True)
    monkeypatch.setattr(obs, "_redis", lambda: redis)
    return visto


# ── compilação ────────────────────────────────────────────────────────


def test_reasons_are_declared() -> None:
    assert {"bad_legs", "legs_over_cap", "join_root"} <= set(runtime_mod.REJECT_REASONS)
    assert {"sequence_below", "sequence_unavailable"} <= set(runtime_mod.ERROR_REASONS)


def test_sequence_expands_into_one_rule_per_leg() -> None:
    rules, reason = compile_row(_row())
    assert reason is None
    assert [r.leg_index for r in rules] == [0, 1]
    assert {r.rule_id for r in rules} == {1}
    assert all(r.legs_total == 2 and r.window_seconds == 900 for r in rules)
    assert rules[0].group_by_path == ("normalized", "user", "name")
    assert rules[1].group_by_path == ("normalized", "actor", "user", "name")
    assert rules[0].leg_label == "okta_mfa_fail"
    # A perna que nomeia a fonte ganha a cláusula de stream.
    assert any(c.path == ("_centralops", "stream") and c.value == "okta.system_log" for c in rules[0].clauses)


def test_compile_rule_returns_the_first_leg_for_validators() -> None:
    rule, reason = compile_rule(_row())
    assert reason is None and rule is not None and rule.leg_index == 0


@pytest.mark.parametrize(
    "legs, reason",
    [
        ("{nao json}", "bad_legs"),
        ([], "bad_legs"),
        ([_LEGS[0]], "bad_legs"),                                            # uma perna só
        ([{**_LEGS[0], "where": []}, _LEGS[1]], "bad_legs"),                 # perna sem where
        ([{**_LEGS[0], "join_path": ""}, _LEGS[1]], "bad_legs"),             # perna sem junção
        ([{**_LEGS[0], "join_path": "user.name"}, _LEGS[1]], "join_root"),   # raiz fora do envelope
        ([{**_LEGS[0], "where": [{"field": "a", "op": "regex", "value": "x"}]}, _LEGS[1]], "unknown_op"),
    ],
)
def test_bad_legs_are_rejected_with_the_right_reason(legs, reason) -> None:
    raw = legs if isinstance(legs, str) else json.dumps(legs)
    rules, got = compile_row(SimpleNamespace(**{**vars(_row()), "legs_json": raw}))
    assert rules == () and got == reason


def test_more_legs_than_the_cap_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(settings, "INFLIGHT_MAX_LEGS", 2)
    legs = [_LEGS[0], _LEGS[1], {**_LEGS[1], "label": "third"}]
    assert validate_legs_json(json.dumps(legs)) == ([], "legs_over_cap")


def test_window_is_mandatory_and_capped() -> None:
    assert compile_row(_row(window=0)) == ((), "bad_legs")
    over = int(settings.INFLIGHT_MAX_WINDOW_SECONDS) + 1
    assert compile_row(_row(window=over)) == ((), "window_over_cap")


def test_classic_rule_still_compiles_to_one() -> None:
    row = SimpleNamespace(
        id=9, name="r", severity_id=4, suppression_window_seconds=3600, rule_type="threshold",
        where_json='[{"field":"raw.a","op":"eq","value":"x"}]', group_by_field="raw.u",
        min_count=1, window_seconds=0,
    )
    rules, reason = compile_row(row)
    assert reason is None and len(rules) == 1 and rules[0].leg_index is None


# ── matcher: cada perna é uma regra comum ─────────────────────────────


def test_matcher_matches_the_right_leg_for_each_source() -> None:
    from backend.app.collectors.inflight.matcher import CompiledRuleSet

    rules, _ = compile_row(_row())
    ruleset = CompiledRuleSet(rules=rules, share_paths=True)
    assert [r.leg_index for r in evaluate_ruleset(_okta("alice"), ruleset)] == [0]
    assert [r.leg_index for r in evaluate_ruleset(_sophos("alice"), ruleset)] == [1]
    # Okta com login OK não é a perna de MFA falho.
    assert evaluate_ruleset(_okta("alice", status_id=1), ruleset) == ()


# ── acumulador: as pernas caem na mesma chave ─────────────────────────


def test_legs_share_the_join_key_and_keep_one_pointer_each() -> None:
    rules, _ = compile_row(_row())
    acc = InflightAccumulator()
    acc.add(rules[0], _okta("alice", event_id="o1"), organization_id=7)
    acc.add(rules[1], _sophos("alice", event_id="s1"), organization_id=7)
    acc.add(rules[1], _sophos("bob", event_id="s2"), organization_id=7)

    assert set(acc.pending) == {"inflight:7:1:seq:alice", "inflight:7:1:seq:bob"}
    alice = acc.pending["inflight:7:1:seq:alice"]
    assert set(alice["legs"]) == {0, 1}
    assert alice["legs"][0]["event_id"] == "o1"
    assert alice["legs"][1]["event_id"] == "s1"
    assert acc.matches[1] == 3


# ── flush: junção entre ciclos, fail-closed, evidência ────────────────


def test_sequence_closes_across_cycles_with_the_same_user(espia, redis) -> None:
    rules, _ = compile_row(_row())

    # Ciclo 1 — coleta do Okta: só a perna 0 de alice. Nada grava.
    acc = InflightAccumulator()
    acc.add(rules[0], _okta("alice", event_id="o1"), organization_id=7)
    asyncio.run(flush_inflight(acc, organization_id=7))
    assert not espia["pending"]
    assert acc.errors["sequence_below"] == {1: 1}
    assert redis.hgetall("inflight:seq:inflight:7:1:seq:alice").keys() == {"0"}
    assert redis.ttl("inflight:seq:inflight:7:1:seq:alice") > 0

    # Ciclo 2 — coleta do Sophos (outra integração, outro worker): perna 1 de
    # alice fecha a sequência; bob só tem a perna 1 e fica retido.
    acc = InflightAccumulator()
    acc.add(rules[1], _sophos("alice", event_id="s1"), organization_id=7)
    acc.add(rules[1], _sophos("bob", event_id="s2"), organization_id=7)
    asyncio.run(flush_inflight(acc, organization_id=7))

    assert list(espia["pending"]) == ["inflight:7:1:seq:alice"]
    item = espia["pending"]["inflight:7:1:seq:alice"]
    assert item["source"]["sequence"] == {"legs": 2, "window_seconds": 900}
    assert [leg["event_id"] for leg in item["source"]["legs"]] == ["o1", "s1"]
    assert [leg["stream"] for leg in item["source"]["legs"]] == ["okta.system_log", "sophos.siem_event"]
    assert acc.errors["sequence_below"] == {1: 1}  # bob
    # O estado fechado foi apagado: a próxima Detection exige pernas novas.
    assert not redis.exists("inflight:seq:inflight:7:1:seq:alice")
    assert redis.exists("inflight:seq:inflight:7:1:seq:bob")


def test_both_legs_in_the_same_cycle_close_at_once(espia) -> None:
    rules, _ = compile_row(_row())
    acc = InflightAccumulator()
    acc.add(rules[0], _okta("alice"), organization_id=7)
    acc.add(rules[1], _sophos("alice"), organization_id=7)
    asyncio.run(flush_inflight(acc, organization_id=7))
    assert list(espia["pending"]) == ["inflight:7:1:seq:alice"]
    assert "sequence_below" not in acc.errors


def test_expired_leg_does_not_count(espia, redis) -> None:
    rules, _ = compile_row(_row(window=60))
    acc = InflightAccumulator()
    acc.add(rules[0], _okta("alice"), organization_id=7)
    asyncio.run(flush_inflight(acc, organization_id=7))
    # A janela passou: o Redis recolheu a perna 0.
    redis.delete("inflight:seq:inflight:7:1:seq:alice")

    acc = InflightAccumulator()
    acc.add(rules[1], _sophos("alice"), organization_id=7)
    asyncio.run(flush_inflight(acc, organization_id=7))
    assert not espia["pending"]
    assert acc.errors["sequence_below"] == {1: 1}


def test_redis_down_is_fail_closed_and_counted(espia, monkeypatch) -> None:
    class _Down:
        def pipeline(self):
            raise ConnectionError("redis down")

    monkeypatch.setattr(obs, "_redis", lambda: _Down())
    rules, _ = compile_row(_row())
    acc = InflightAccumulator()
    acc.add(rules[0], _okta("alice"), organization_id=7)
    acc.add(rules[1], _sophos("alice"), organization_id=7)
    asyncio.run(flush_inflight(acc, organization_id=7))
    assert not espia["pending"]
    assert acc.errors["sequence_unavailable"] == {1: 1}


def test_classic_rules_never_touch_the_sequence_state(espia, redis) -> None:
    from backend.app.collectors.inflight.matcher import CompiledInflightRule

    classic = CompiledInflightRule(
        rule_id=5, name="r5", severity_id=4, suppression_window_seconds=3600,
        group_by_path=("normalized", "user", "name"), clauses=(),
    )
    acc = InflightAccumulator()
    acc.add(classic, _okta("alice"), organization_id=7)
    asyncio.run(flush_inflight(acc, organization_id=7))
    assert list(espia["pending"]) == ["inflight:7:5:alice"]
    assert redis.keys("inflight:seq:*") == []


# ── o evento 2004 da sequência ────────────────────────────────────────


def test_detection_event_carries_one_pointer_per_leg() -> None:
    emit = runtime_mod.DetectionEmit(
        dedup_key="inflight:7:1:seq:alice",
        detection_id=42,
        rule_id=1,
        rule_name="seq-1",
        severity_id=4,
        integration_id=9,
        source={
            "event_id": "s1", "vendor": "sophos", "platform": "sophos",
            "stream": "sophos.siem_event", "group_field": "normalized.actor.user.name",
            "group_value": "alice", "self_emitted": False,
            "sequence": {"legs": 2, "window_seconds": 900},
            "legs": [
                {"leg_index": 0, "event_id": "o1", "stream": "okta.system_log",
                 "event_type": "okta.system_log", "platform": "okta", "event_time": 1000,
                 "group_field": "normalized.user.name"},
                {"leg_index": 1, "event_id": "s1", "stream": "sophos.siem_event",
                 "event_type": "sophos.siem_event", "platform": "sophos", "event_time": 2000,
                 "group_field": "normalized.actor.user.name"},
            ],
        },
    )
    envelope = runtime_mod._build_detection_event(emit, organization_id=7, now_ms=3_000)
    n = envelope["normalized"]
    assert n["class_uid"] == 2004
    assert "Sequência em voo" in n["message"] and "2 perna(s)" in n["message"]
    assert n["unmapped"]["sequence"] == {"legs": 2, "window_seconds": 900}
    assert [leg["event_id"] for leg in n["unmapped"]["legs"]] == ["o1", "s1"]
    assert [leg["platform"] for leg in n["unmapped"]["legs"]] == ["okta", "sophos"]
    # Nenhum payload de cliente viaja: só ponteiros.
    assert "raw" not in json.dumps(n["unmapped"]["legs"])

    from backend.app.collectors.normalize.ocsf import get_registry, structural_gate

    verdict = structural_gate(n, get_registry("1.8.0"))
    assert verdict.valid, (verdict.reason, verdict.missing_required)
