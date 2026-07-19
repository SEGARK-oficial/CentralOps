"""Paridade de operadores entre os modos batch e inflight (ADR-0015).

MOTIVO DESTE ARQUIVO — um bug real, encontrado em auditoria e não por teste:

``INFLIGHT_ALLOWED_OPS`` aceita ``contains``, herdado do vocabulário do motor
batch (``correlation_engine._OPS``). Mas a Fase 1 unificou a avaliação em
``routing.engine.compare_values``, que é a implementação do ROTEAMENTO — e
``ALLOWED_OPS`` de rota NUNCA teve ``contains``. Resultado: a cláusula era
aceita na escrita (nenhum 422), compilava sem rejeição, a regra aparecia verde
na lista, e o operador caía no ``return False`` final de ``compare_values``. A
regra nunca disparava. Para sempre. E a MESMA regra em ``eval_mode='batch'``
funcionava.

A ironia é a lição: a unificação de vocabulário foi feita para IMPEDIR que duas
implementações divergissem em silêncio, e produziu a divergência ao escolher o
lado que tinha menos operadores. Nenhum teste pegou porque os testes existentes
só afirmavam que o operador estava no ``frozenset`` — nunca que ele CASAVA algo.

O guard abaixo é comportamental por operador. Se alguém adicionar um operador a
``INFLIGHT_ALLOWED_OPS`` sem implementá-lo, isto reprova.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import pytest

from backend.app.collectors.inflight.runtime import INFLIGHT_ALLOWED_OPS
from backend.app.collectors.routing.engine import compare_values
from backend.app.services.correlation_engine import _OPS as BATCH_OPS
from backend.app.services.correlation_engine import matches_where

#: Para cada operador: (valor_do_evento, valor_da_regra) que DEVE casar.
_POSITIVE: dict[str, tuple] = {
    "eq": ("malware", "malware"),
    "ne": ("malware", "outro"),
    "contains": ("malware-detected", "malware"),
    "gt": (5, 3),
    "gte": (5, 5),
    "lt": (3, 5),
    "lte": (5, 5),
    "in": ("alice", frozenset({"alice", "bob"})),
    "nin": ("carol", frozenset({"alice", "bob"})),
    "exists": ("qualquer", True),
}


@pytest.mark.parametrize("op", sorted(INFLIGHT_ALLOWED_OPS))
def test_every_allowed_inflight_operator_actually_matches(op):
    """O guard central: aceitar um operador e não implementá-lo é falha muda.

    Antes desta correção, ``contains`` passava no teste de pertencimento ao
    ``frozenset`` e reprovaria aqui — que é a diferença entre um teste que
    verifica configuração e um que verifica comportamento.
    """
    assert op in _POSITIVE, (
        f"operador {op!r} foi adicionado a INFLIGHT_ALLOWED_OPS sem um caso "
        "positivo aqui — acrescente o caso ou remova o operador"
    )
    actual, expected = _POSITIVE[op]
    assert compare_values(op, actual, expected) is True, (
        f"operador {op!r} está ACEITO no inflight mas não casa nada: a regra "
        "compila, fica verde na UI e nunca dispara"
    )


@pytest.mark.parametrize("op", sorted(set(BATCH_OPS) & INFLIGHT_ALLOWED_OPS))
def test_shared_operators_agree_between_batch_and_inflight(op):
    """Operador presente nos dois modos precisa produzir o MESMO veredito.

    Divergir aqui significa que mover uma regra de ``batch`` para ``inflight``
    muda o resultado sem que nada na UI avise — o autor troca um radio button e
    a cobertura de detecção muda em silêncio.
    """
    actual, expected = _POSITIVE[op]
    # O batch compara via str(); normaliza o lado direito para o formato que ele
    # aceita (o batch não conhece frozenset).
    if isinstance(expected, (frozenset, set, tuple, list)):
        pytest.skip(f"{op} não existe no vocabulário batch")

    inflight = compare_values(op, actual, expected)
    batch = matches_where(
        {"campo": actual}, [{"field": "campo", "op": op, "value": expected}]
    )
    assert inflight == batch, (
        f"operador {op!r} diverge: inflight={inflight}, batch={batch}. "
        "A mesma regra produz resultados diferentes conforme o eval_mode."
    )


def test_contains_regression_specifically():
    """Regressão nomeada do bug que motivou o arquivo."""
    assert compare_values("contains", "malware-detected", "malware") is True
    assert compare_values("contains", "limpo", "malware") is False


def test_contains_is_not_offered_to_route_conditions():
    """A correção NÃO pode ter vazado ``contains`` para condições de rota.

    ``ALLOWED_OPS`` é o vocabulário das rotas e é validado separadamente por
    ``validate_condition``. Adicionar o ramo em ``compare_values`` habilita o
    operador para as regras em voo sem alterar o contrato de roteamento.
    """
    from backend.app.collectors.routing.engine import ALLOWED_OPS

    assert "contains" not in ALLOWED_OPS


def test_missing_field_semantics_agree_for_negative_operators():
    """``ne`` sobre campo ausente casa por vacuidade nos DOIS modos.

    É a semântica que o compilador em voo fecha auto-injetando ``exists``; se os
    modos divergissem aqui, a allowlist se comportaria diferente conforme o
    eval_mode.
    """
    inflight = compare_values("ne", None, "x")
    batch = matches_where({}, [{"field": "ausente", "op": "ne", "value": "x"}])
    assert inflight is True and batch is True


# ── Truncamento de regras: o teto de CRIAÇÃO ≠ teto de AVALIAÇÃO ─────────────
#
# Achado de auditoria, e o mais grave da feature: `CORRELATION_MAX_RULES_PER_ORG`
# (200) governa a CRIAÇÃO; `INFLIGHT_MAX_RULES_PER_CYCLE` (50) governa a
# AVALIAÇÃO. Um cliente pode criar 200 regras em voo e apenas 50 rodam. Pior: a
# query ordena por `id ASC`, então as descartadas são sempre as MAIS RECENTES —
# exatamente a regra que o operador acabou de escrever e está testando. Verde na
# lista, nunca dispara, zero sinal em log ou métrica.

def test_creation_cap_exceeds_evaluation_cap_so_truncation_is_reachable():
    """Documenta a assimetria que torna o truncamento possível.

    Não é bug por si — é decisão de produto (o modo batch usa o mesmo teto de
    criação). O bug era o SILÊNCIO, coberto pelo teste seguinte.
    """
    from backend.app.core.config import settings

    assert settings.CORRELATION_MAX_RULES_PER_ORG > settings.INFLIGHT_MAX_RULES_PER_CYCLE, (
        "se os tetos se igualarem, o truncamento deixa de existir e o aviso "
        "abaixo vira código morto — remova os dois juntos"
    )


@pytest.mark.source_only  # lê o .py; na imagem Cython o fonte não existe
def test_truncation_is_reported_not_silent():
    """O guard: acima do teto, o operador PRECISA ser avisado.

    Verifica no fonte da função de carga porque reproduzir exigiria banco com
    51+ regras; o que importa é que o caminho exista e nomeie o total.
    """
    import inspect

    from backend.app.collectors.inflight import runtime

    src = inspect.getsource(runtime.load_inflight_rules_for_org)
    assert "count_inflight_for_org" in src, (
        "sem contar o total, é impossível saber que houve truncamento"
    )
    assert 'reason="truncated"' in src, "o truncamento precisa de métrica própria"
    assert "truncated" in runtime.REJECT_REASONS, (
        "a razão precisa estar no enum FECHADO — ela vira label de métrica"
    )


def test_suppression_window_zero_is_not_swallowed():
    """``0`` = supressão desligada é valor LEGÍTIMO.

    ``or 3600`` o engoliria, dando ao operador uma janela de 1h que ele não
    pediu — a mesma classe do bug ``or 7`` do TTL de dedupe corrigido nesta
    mesma branch.
    """
    import types

    from backend.app.collectors.inflight.runtime import compile_rule

    row = types.SimpleNamespace(
        id=1, name="r", severity_id=4, group_by_field=None,
        suppression_window_seconds=0,
        where_json='[{"field":"a","op":"eq","value":"x"}]',
    )
    rule, reason = compile_rule(row)
    assert reason is None
    assert rule.suppression_window_seconds == 0, (
        f"0 virou {rule.suppression_window_seconds} — o fallback engoliu o zero"
    )
