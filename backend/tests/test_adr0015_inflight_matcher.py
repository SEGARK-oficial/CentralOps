"""Classificação em voo — matcher, compilação e acumulador (ADR-0015, Fase 1).

Cobre as três coisas que, se quebrarem, tornam a feature pior que inexistente:
pureza do caminho por-evento (R1), operadores que não falham abertos, e tetos que
nunca descartam evento.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import ast
import inspect
import types
from pathlib import Path

import pytest

from backend.app.collectors.inflight import matcher as matcher_mod
from backend.app.collectors.inflight import runtime as runtime_mod
from backend.app.collectors.inflight.matcher import (
    CompiledClause,
    CompiledInflightRule,
    evaluate_inflight,
)
from backend.app.collectors.inflight.runtime import (
    INFLIGHT_ALLOWED_OPS,
    REJECT_REASONS,
    InflightAccumulator,
    compile_rule,
    validate_where_json,
)
from backend.app.core.config import settings


# ── R1 mecanizado: o caminho por-evento não pode tocar o mundo ───────────────
#
# Este é o guard mais importante do arquivo. Um ``import redis`` acrescentado a
# matcher.py daqui a seis meses seria invisível numa revisão de PR e custaria um
# round-trip por evento no gargalo do pipeline. A convenção vira mecânica.

_FORBIDDEN_IMPORTS = (
    "redis", "httpx", "requests", "aiohttp", "sqlalchemy", "celery",
    "backend.app.db", "..db", "...db",
)


def test_matcher_module_imports_nothing_that_touches_the_world():
    src = Path(matcher_mod.__file__).read_text()
    tree = ast.parse(src)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append("." * (node.level or 0) + (node.module or ""))
    offenders = [
        m for m in imported
        if any(m == f or m.startswith(f + ".") for f in _FORBIDDEN_IMPORTS)
    ]
    assert not offenders, (
        f"matcher.py importa {offenders} — R1 proíbe I/O no caminho por-evento. "
        "Se a lógica precisa do mundo, ela pertence a runtime.py."
    )


def test_matcher_has_no_async_and_no_logger():
    """``async`` implicaria await; logger implicaria I/O e formatação por evento."""
    src = Path(matcher_mod.__file__).read_text()
    tree = ast.parse(src)
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]
    assert "getLogger" not in src


# ── Avaliação ────────────────────────────────────────────────────────────────

def _rule(*clauses, rule_id=1, group_by=None):
    return CompiledInflightRule(
        rule_id=rule_id, name=f"r{rule_id}", severity_id=4,
        suppression_window_seconds=3600, group_by_path=group_by,
        clauses=tuple(clauses),
    )


def test_all_clauses_must_match():
    env = {"a": "x", "b": "y"}
    r = _rule(
        CompiledClause(("a",), "eq", "x"),
        CompiledClause(("b",), "eq", "NOPE"),
    )
    assert evaluate_inflight(env, [r]) == ()


def test_match_returns_the_rule():
    env = {"a": "x"}
    r = _rule(CompiledClause(("a",), "eq", "x"))
    assert evaluate_inflight(env, [r]) == (r,)


def test_nested_path_resolution():
    env = {"raw": {"user": {"name": "svc-backup"}}}
    r = _rule(CompiledClause(("raw", "user", "name"), "eq", "svc-backup"))
    assert evaluate_inflight(env, [r]) == (r,)


def test_path_crossing_a_list_resolves_to_none_not_crash():
    """``_resolve`` não navega listas — limitação declarada, não deve levantar."""
    env = {"raw": {"args": ["a", "b"]}}
    r = _rule(CompiledClause(("raw", "args", "0"), "eq", "a"))
    assert evaluate_inflight(env, [r]) == ()


def test_numeric_coercion_closes_the_string_severity_false_negative():
    """O modo de falha silenciosa mais provável da fase.

    Um vendor que serializa severidade como ``"5"`` faria ``'5' >= 3`` levantar
    TypeError dentro de ``compare_values``, que devolve False — a regra nunca
    casaria e o contador ficaria em zero, indistinguível de "não bateu".
    """
    env = {"raw": {"severity": "5"}}
    sem_coercao = _rule(CompiledClause(("raw", "severity"), "gte", 3.0, numeric=False))
    com_coercao = _rule(CompiledClause(("raw", "severity"), "gte", 3.0, numeric=True))
    assert evaluate_inflight(env, [sem_coercao]) == ()
    assert evaluate_inflight(env, [com_coercao]) != ()


def test_empty_rules_returns_empty_tuple():
    assert evaluate_inflight({"a": 1}, []) == ()


# ── Compilação ───────────────────────────────────────────────────────────────

def _row(**over):
    base = dict(
        id=1, name="regra", severity_id=4, suppression_window_seconds=3600,
        group_by_field=None, where_json='[{"field":"a","op":"eq","value":"x"}]',
    )
    base.update(over)
    return types.SimpleNamespace(**base)


@pytest.mark.parametrize(
    "where,reason",
    [
        ("nao-e-json", "bad_json"),
        ('{"nao":"lista"}', "bad_json"),
        ("[]", "empty_where"),
        ('[{"field":"a","op":"regex","value":"x"}]', "unknown_op"),
        ('[{"field":"a","op":"in","value":"a,b"}]', "bad_json"),
        ('[{"field":"a","op":"gte","value":"abc"}]', "bad_json"),
    ],
)
def test_compile_rejects_with_closed_reason(where, reason):
    rule, got = compile_rule(_row(where_json=where))
    assert rule is None
    assert got == reason
    assert got in REJECT_REASONS, "razão precisa estar no enum fechado (vira label)"


def test_csv_string_is_rejected_for_in_not_silently_split():
    """``"a,b"`` virando lista de 3 caracteres seria falso-negativo mudo."""
    rule, reason = compile_rule(_row(where_json='[{"field":"a","op":"in","value":"a,b"}]'))
    assert rule is None and reason == "bad_json"


def test_in_compiles_to_frozenset_for_o1_lookup():
    rule, _ = compile_rule(_row(where_json='[{"field":"a","op":"in","value":["x","y"]}]'))
    assert isinstance(rule.clauses[0].value, frozenset)


def test_numeric_op_marks_clause_numeric():
    rule, _ = compile_rule(_row(where_json='[{"field":"a","op":"gte","value":3}]'))
    assert rule.clauses[0].numeric is True
    assert rule.clauses[0].value == 3.0


def test_clause_cap_is_enforced():
    many = ",".join(f'{{"field":"f{i}","op":"eq","value":1}}' for i in range(99))
    rule, reason = compile_rule(_row(where_json=f"[{many}]"))
    assert rule is None and reason == "over_cap"


def test_allowed_ops_superset_of_batch_plus_three():
    assert {"in", "nin", "exists"} <= INFLIGHT_ALLOWED_OPS
    assert {"eq", "ne", "contains", "gt", "lt", "gte", "lte"} <= INFLIGHT_ALLOWED_OPS


# ── O fail-open de allowlist, fechado por auto-injeção ───────────────────────

def test_nin_auto_injects_exists_closing_the_allowlist_fail_open():
    """Sem isso, um evento SEM o campo passa pela allowlist que deveria excluí-lo.

    ``nin`` casa por vacuidade em campo ausente (contrato de ``compare_values``).
    Numa regra "logon E usuário NÃO em [svc-backup]", um evento cujo ``raw.user``
    sumiu — path atravessa lista, ou o raw foi trimado — satisfaria a allowlist e
    dispararia exatamente sobre o que o operador quis calar.
    """
    rule, _ = compile_rule(
        _row(where_json='[{"field":"raw.user","op":"nin","value":["svc-backup"]}]')
    )
    ops = {(c.path, c.op) for c in rule.clauses}
    assert (("raw", "user"), "exists") in ops, "cláusula exists não foi auto-injetada"

    # Comportamento observável: evento sem o campo NÃO casa mais.
    assert evaluate_inflight({"raw": {}}, [rule]) == ()
    # E o caso legítimo continua casando.
    assert evaluate_inflight({"raw": {"user": "alice"}}, [rule]) == (rule,)


def test_ne_also_gets_the_guard():
    rule, _ = compile_rule(_row(where_json='[{"field":"a","op":"ne","value":"x"}]'))
    assert (("a",), "exists") in {(c.path, c.op) for c in rule.clauses}


def test_explicit_exists_is_not_duplicated():
    rule, _ = compile_rule(_row(where_json=(
        '[{"field":"a","op":"exists","value":true},'
        '{"field":"a","op":"nin","value":["x"]}]'
    )))
    exists_clauses = [c for c in rule.clauses if c.op == "exists"]
    assert len(exists_clauses) == 1


# ── Acumulador: tetos que nunca descartam evento ─────────────────────────────

def test_group_by_none_collapses_to_one_detection():
    acc = InflightAccumulator()
    r = _rule(CompiledClause(("a",), "eq", "x"))
    for _ in range(1000):
        acc.add(r, {"a": "x"}, organization_id=1)
    assert len(acc.pending) == 1, "regra sem group_by deve gerar UMA Detection"
    assert acc.matches[1] == 1000, "os matches seguem contados com fidelidade"


def test_unresolved_group_by_is_an_error_not_a_generic_detection():
    """Agrupar não-resolvidos numa Detection genérica esconderia 'regra apontando
    para campo errado' dentro de um alerta que parece legítimo."""
    acc = InflightAccumulator()
    r = _rule(CompiledClause(("a",), "eq", "x"), group_by=("ausente",))
    acc.add(r, {"a": "x"}, organization_id=1)
    assert acc.pending == {}
    assert acc.errors["group_by_unresolved"] == 1


def test_key_cap_counts_overflow_and_never_loses_the_match_count():
    cap = int(settings.INFLIGHT_MAX_DEDUP_KEYS_PER_RULE_PER_CYCLE)
    acc = InflightAccumulator()
    r = _rule(CompiledClause(("a",), "eq", "x"), group_by=("u",))
    for i in range(cap + 25):
        acc.add(r, {"a": "x", "u": f"user{i}"}, organization_id=1)
    assert len(acc.pending) == cap
    assert acc.overflow[1] == 25
    assert acc.matches[1] == cap + 25, "match count nunca perde fidelidade no teto"


def test_group_value_is_truncated_into_the_dedup_key():
    acc = InflightAccumulator()
    r = _rule(CompiledClause(("a",), "eq", "x"), group_by=("u",))
    acc.add(r, {"a": "x", "u": "z" * 5000}, organization_id=1)
    key = next(iter(acc.pending))
    assert len(key) < 5000
    assert len(key.split(":")[-1]) == int(settings.INFLIGHT_MAX_GROUP_VALUE_LEN)


def test_dedup_key_is_org_scoped():
    """Chave sem org permitiria colisão entre tenants na tabela de Detection."""
    acc = InflightAccumulator()
    r = _rule(CompiledClause(("a",), "eq", "x"))
    acc.add(r, {"a": "x"}, organization_id=1)
    acc.add(r, {"a": "x"}, organization_id=2)
    assert len(acc.pending) == 2
    assert all(k.startswith("inflight:") for k in acc.pending)


# ── Tetos: invariantes executáveis (R8) ──────────────────────────────────────

def test_budget_invariant_is_enforced_at_boot():
    """O que protege o hot path é o PRODUTO, não os tetos isolados."""
    from pydantic import ValidationError

    from backend.app.core.config import Settings

    with pytest.raises((ValidationError, ValueError)):
        Settings(INFLIGHT_MAX_RULES_PER_CYCLE=200, INFLIGHT_MAX_WHERE_CLAUSES=10)


def test_current_defaults_respect_the_budget():
    budget = (
        int(settings.INFLIGHT_MAX_RULES_PER_CYCLE)
        * int(settings.INFLIGHT_MAX_WHERE_CLAUSES)
    )
    assert 0 < budget <= 500


@pytest.mark.parametrize(
    "name",
    [
        "INFLIGHT_MAX_WHERE_CLAUSES",
        "INFLIGHT_MAX_DEDUP_KEYS_PER_RULE_PER_CYCLE",
        "INFLIGHT_MAX_GROUP_VALUE_LEN",
    ],
)
def test_caps_are_positive(name):
    """Zero em qualquer um destes mataria a feature em silêncio. Só
    INFLIGHT_MAX_RULES_PER_CYCLE admite 0, e é kill-switch documentado."""
    assert int(getattr(settings, name)) > 0


def test_validate_where_json_is_public_for_reuse_by_the_write_path():
    """O CRUD do EE deve rejeitar com 422 na escrita em vez de deixar a regra
    entrar no banco e falhar silenciosamente na compilação."""
    assert callable(validate_where_json)
    clauses, reason = validate_where_json('[{"field":"a","op":"eq","value":1}]')
    assert reason is None and len(clauses) == 1


# ── Custo por evento (R1): provado por benchmark, não por métrica em produção ─
#
# A spec cortou deliberadamente o histograma `inflight_eval_seconds` e os dois
# `perf_counter_ns` por evento: alimentar o histograma exigiria, POR EVENTO,
# exatamente o trabalho que R1 existe para impedir. O custo se prova aqui.
#
# Números medidos (Python 3.12.13, Apple Silicon), pior caso — toda cláusula
# avaliada porque todas casam menos a última:
#     50 regras x 10 cláusulas (500 comparações) = 121,8 µs/evento -> ~8,2k EPS
#     10 regras x  5 cláusulas ( 50 comparações) =  12,2 µs/evento -> ~82k EPS
# O gargalo pré-existente do pipeline (`await claim`, 1 round-trip Redis por
# evento) limita a ~2-4k EPS/task. No ORÇAMENTO MÁXIMO o matcher mantém ~2-4x de
# folga sobre ele — apertado o bastante para justificar o teto de 500 no
# validador de boot, e folgado o bastante para não ser o dominante.

def test_worst_case_cost_per_event_stays_within_budget():
    """Reprova se o matcher ficar >5x mais lento que o medido.

    Limiar deliberadamente frouxo (600 µs contra 122 µs medidos): o objetivo é
    pegar uma REGRESSÃO ESTRUTURAL — alguém trocar o frozenset por lista, tirar
    o short-circuit, ou reintroduzir um split de path por evento — sem virar
    teste instável em CI compartilhada.
    """
    import time

    env = {
        "_centralops": {"organization_id": 1, "vendor": "sophos"},
        "raw": {f"f{j}": f"v{j}" for j in range(12)},
    }
    n_rules = int(settings.INFLIGHT_MAX_RULES_PER_CYCLE)
    n_clauses = int(settings.INFLIGHT_MAX_WHERE_CLAUSES)

    def worst(i):
        cl = [CompiledClause(("raw", f"f{j}"), "eq", f"v{j}") for j in range(n_clauses - 1)]
        cl.append(CompiledClause(("raw", "f0"), "eq", "NUNCA"))
        return _rule(*cl, rule_id=i)

    rules = [worst(i) for i in range(n_rules)]
    N = 2000
    t0 = time.perf_counter()
    for _ in range(N):
        evaluate_inflight(env, rules)
    us = (time.perf_counter() - t0) / N * 1e6

    assert us < 600, (
        f"{us:.1f} µs/evento no orçamento máximo ({n_rules}x{n_clauses}). "
        "Acima disso o matcher passa a competir com a INGESTÃO, que é o produto. "
        "Procure: lista no lugar de frozenset, perda do short-circuit, ou split "
        "de dotted-path movido de volta para o caminho por-evento."
    )


def test_empty_ruleset_is_effectively_free():
    """R2: sem regra ativa o custo tem de ser indistinguível de zero."""
    import time

    env = {"raw": {"a": 1}}
    N = 50000
    t0 = time.perf_counter()
    for _ in range(N):
        evaluate_inflight(env, ())
    us = (time.perf_counter() - t0) / N * 1e6
    assert us < 5, f"{us:.2f} µs/evento com ZERO regras — deveria ser ~0"
