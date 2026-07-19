"""Equivalência entre o fast-path de dot-path e o jmespath (ADR-0015, Fase 2).

O fast-path troca o interpretador de AST do jmespath por um walk de dicts em
~80% das expressões de mapping. Isso só é aceitável se a semântica for
IDÊNTICA — e a divergência, se houver, é SILENCIOSA: o valor vira ``None``, cai
no default do mapping ou manda o evento para quarentena. Nunca levanta erro,
nunca aparece em log. Um mapping em produção passaria a produzir campos vazios
sem que ninguém soubesse.

Por isso a cobertura tem duas camadas: casos de borda nomeados (D1..D14, para
que a falha aponte o caso) e um teste por PROPRIEDADE sobre milhares de objetos
gerados, que é mais forte que qualquer lista escolhida a dedo.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import itertools
import random

import jmespath
import pytest

from backend.app.collectors.normalize.dotpath import (
    DotPathResolver,
    compile_source,
    is_fast_path,
)


def _both(expr: str, obj):
    """(jmespath, fast-path) para a mesma expressão e objeto."""
    return jmespath.compile(expr).search(obj), DotPathResolver(expr).search(obj)


# ── Casos de borda nomeados ──────────────────────────────────────────────────

_EDGE = [
    ("D1  chave raiz ausente", "a.b", {}),
    ("D2  intermediário None", "a.b", {"a": None}),
    ("D3  intermediário é LISTA de dicts", "a.b", {"a": [{"b": 1}]}),
    ("D4  intermediário é lista vazia", "a.b", {"a": []}),
    ("D5  intermediário é string", "a.b", {"a": "str"}),
    ("D6  intermediário é int", "a.b", {"a": 1}),
    ("D7  intermediário é bool", "a.b", {"a": True}),
    ("D8  folha None em 3 níveis", "a.b.c", {"a": {"b": None}}),
    ("D9  folha ausente em 3 níveis", "a.b.c", {"a": {"b": {}}}),
    ("D10 valor final é null explícito", "a.b", {"a": {"b": None}}),
    ("D11 valor final é string vazia", "a.b", {"a": {"b": ""}}),
    ("D12 valor final é zero", "a.b", {"a": {"b": 0}}),
    ("D13 valor final é False", "a.b", {"a": {"b": False}}),
    ("D14 valor final é lista", "a.b", {"a": {"b": [1, 2]}}),
    ("D15 raiz não é dict", "a.b", "nao-e-dict"),
    ("D16 raiz é None", "a.b", None),
    ("D17 raiz é lista", "a.b", [{"a": {"b": 1}}]),
    ("D18 um nível, presente", "a", {"a": 7}),
    ("D19 um nível, ausente", "a", {}),
    ("D20 valor final é dict", "a.b", {"a": {"b": {"c": 1}}}),
    ("D21 chave com underscore", "_msg.user", {"_msg": {"user": "alice"}}),
    ("D22 dígitos no nome", "a1.b2", {"a1": {"b2": "x"}}),
]


@pytest.mark.parametrize("label,expr,obj", _EDGE, ids=[e[0].split()[0] for e in _EDGE])
def test_edge_case_matches_jmespath(label, expr, obj):
    esperado, obtido = _both(expr, obj)
    assert obtido == esperado, (
        f"{label}: fast-path devolveu {obtido!r}, jmespath devolveu {esperado!r} "
        f"para {expr!r} sobre {obj!r}"
    )


# ── Propriedade: concordam sobre objetos gerados ─────────────────────────────

def _random_value(rng, depth=0):
    """Valor arbitrário, incluindo os tipos que quebram walks ingênuos."""
    choices = [
        lambda: None, lambda: 0, lambda: 1, lambda: True, lambda: False,
        lambda: "", lambda: "texto", lambda: [], lambda: [{"b": 1}], lambda: [1, 2],
        lambda: {}, lambda: 3.14,
    ]
    if depth < 3:
        choices.append(lambda: {k: _random_value(rng, depth + 1)
                                for k in rng.sample(["a", "b", "c", "x"], k=2)})
    return rng.choice(choices)()


def test_property_fastpath_agrees_with_jmespath_on_generated_objects():
    """5.000 combinações de objeto × path. Mais forte que casos escolhidos:
    cobre encadeamentos que ninguém pensaria em listar."""
    rng = random.Random(20260718)
    exprs = [".".join(p) for n in (1, 2, 3)
             for p in itertools.product(["a", "b", "c", "x"], repeat=n)]
    divergences = []
    for _ in range(5000):
        obj = _random_value(rng)
        expr = rng.choice(exprs)
        esperado, obtido = _both(expr, obj)
        if obtido != esperado:
            divergences.append((expr, obj, esperado, obtido))
            if len(divergences) >= 5:
                break
    assert not divergences, (
        "fast-path divergiu do jmespath:\n"
        + "\n".join(f"  {e!r} sobre {o!r}: jmespath={j!r} fast={f!r}"
                    for e, o, j, f in divergences)
    )


# ── Elegibilidade ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("expr", [
    "a", "a.b", "a.b.c", "_msg.user", "raw.detail_x9.value_2",
])
def test_simple_dot_paths_are_eligible(expr):
    assert is_fast_path(expr)
    assert isinstance(compile_source(expr), DotPathResolver)


@pytest.mark.parametrize("expr", [
    "a[0]", "a[?b=='x']", "a.*.b", "length(a)", "a || b", "a.b[]",
    "@", "a.\"b c\"", "a[*].b", "sort(a)",
])
def test_real_jmespath_expressions_fall_back(expr):
    """Tudo que não é dot-path trivial continua no interpretador."""
    assert not is_fast_path(expr)
    assert not isinstance(compile_source(expr), DotPathResolver)


def test_unicode_names_are_not_eligible():
    """O motivo de o regex ser ASCII explícito e nunca ``\\w``.

    ``\\w`` é unicode-aware em Python: ``aça.b`` CASARIA e viraria fast-path
    funcional — enquanto ``jmespath.compile`` rejeita com "Unknown token". O
    guard de sintaxe de mapping deixaria de existir para esses nomes.
    """
    for expr in ("aça.b", "naïve.x", "日本.a"):
        assert not is_fast_path(expr), f"{expr!r} não deveria ser elegível"
        with pytest.raises(Exception):
            jmespath.compile(expr)


def test_compile_source_still_validates_syntax():
    """``jmespath.compile`` é chamado SEMPRE, mesmo quando o fast-path vence.

    É o único validador de sintaxe de mapping em compile-time; pulá-lo
    transformaria erros que hoje reprovam a criação da versão de mapping em
    fast-paths silenciosamente funcionais.
    """
    with pytest.raises(Exception):
        compile_source("a[?b==")


def test_kill_switch_disables_the_fast_path(monkeypatch):
    """Reversão sem redeploy de código, caso o fast-path divirja em produção."""
    from backend.app.collectors.normalize import dotpath

    monkeypatch.setattr(dotpath, "_FASTPATH_ENABLED", False)
    assert not dotpath.is_fast_path("a.b")
    assert not isinstance(dotpath.compile_source("a.b"), DotPathResolver)


def test_resolver_duck_types_parsed_result():
    """A assinatura precisa bater com ``ParsedResult.search`` — os call-sites
    chamam ``.search(value)`` sem saber qual dos dois recebeu."""
    r = DotPathResolver("a.b")
    assert r.search({"a": {"b": 1}}) == 1
    assert r.search({"a": {"b": 1}}, None) == 1, "options precisa ser aceito"
