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

DIVERGÊNCIA CONHECIDA E ACEITA — ``in``, ``nin``, ``exists``:

O vocabulário em voo tem 10 operadores; o batch (``correlation_engine._OPS``)
tem 7. Os 3 excedentes não são "não suportados" no batch — são MORTOS:
``matches_where`` abre com ``if op not in _OPS: return False``, então a cláusula
reprova o evento antes de sequer olhar o valor. Consequência para o operador:
uma regra ``eval_mode='batch'`` que use qualquer um dos três nunca casa NADA,
para sempre, sem log, sem métrica e sem 422 — exatamente a classe de falha que
este arquivo existe para eliminar, e que ele PULAVA (a interseção do parametrize
de paridade os removia, e um ``pytest.skip`` cobria o resto). Este arquivo agora
DOCUMENTA a divergência em asserção, em vez de escondê-la em skip.

O que segura o buraco fechado hoje é a camada de escrita, e SÓ ela — verificado
em ``centralops_ee/routers/correlation_rules.py:48-51``, não presumido:
``WhereFilter.op`` é um ``Literal`` com os 7 operadores do batch e ``value`` é
``str``, então POST/PATCH com ``in``/``nin``/``exists`` leva 422 do Pydantic nos
DOIS eval_modes — inclusive no inflight, que os implementa de fato. O risco não
é o estado atual, é o próximo PR: ``_validate_where`` só chama ``compile_rule``
(que valida contra os 10 ops do inflight) quando ``eval_mode == 'inflight'``;
para batch ele checa apenas ``bad_json``. Alargar o ``Literal`` para o
vocabulário em voo — a mudança óbvia para liberar os três no inflight — libera
os três para o BATCH junto, e ali nada revalida: a regra entra com 200 OK,
aparece verde na lista e morre no ``return False``.
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

    Este é o POSITIVO que sustenta o teste de divergência abaixo. ``inflight ==
    batch`` sozinho passaria por vacuidade: dois motores quebrados concordam em
    ``False`` tão bem quanto dois motores certos concordam em ``True``. Por isso
    o veredito compartilhado é ancorado em ``True`` — os 7 operadores comuns
    CASAM nos dois motores, os 3 divergentes só casam num.
    """
    actual, expected = _POSITIVE[op]
    # O batch compara via ``str()`` e não conhece coleção. Se um operador de
    # coleção algum dia entrar na interseção, isto vira uma DECISÃO explícita
    # (como ``matches_where`` deve stringificar a lista?) e não um skip que
    # apaga a pergunta — foi o skip aqui que escondeu ``in``/``nin``/``exists``.
    assert not isinstance(expected, (frozenset, set, tuple, list)), (
        f"{op!r} entrou na interseção com valor de coleção — defina a semântica "
        "de str(target) no motor batch antes de liberar este par"
    )

    inflight = compare_values(op, actual, expected)
    batch = matches_where(
        {"campo": actual}, [{"field": "campo", "op": op, "value": expected}]
    )
    assert inflight == batch, (
        f"operador {op!r} diverge: inflight={inflight}, batch={batch}. "
        "A mesma regra produz resultados diferentes conforme o eval_mode."
    )
    assert batch is True, (
        f"o par positivo de {op!r} parou de casar nos DOIS motores: a igualdade "
        "acima passou por vacuidade (False == False), não por paridade"
    )


# ── Os 3 operadores que o batch NÃO tem: assertar, não pular ────────────────
#
# COMPORTAMENTAL DE PROPÓSITO, sem ``source_only``: nada aqui lê fonte, tudo
# chama os dois motores. O valor de rodar no gate ``.so`` é justamente este — na
# imagem Cython o fonte não existe, mas a divergência existe igual.

#: Por operador divergente: ``(valor como aparece no where_json, evento que a
#: semântica DEVERIA casar, evento que ela NÃO deveria casar)``.
#:
#: Os DOIS eventos existem por método: um ``False`` isolado no batch não prova
#: nada — toda comparação que não bate devolve False. Só a reprovação dos dois
#: lados do predicado distingue "cláusula MORTA" de "valor não bateu". O valor é
#: a forma JSON (lista, não ``frozenset``): é o que o autor da regra escreve e o
#: que sai do banco; o ``frozenset`` de ``_POSITIVE`` é produto do
#: ``compile_rule`` e só vale para o lado inflight.
_INFLIGHT_ONLY_CASES: dict[str, tuple] = {
    "in": (["alice", "bob"], "alice", "zoe"),
    "nin": (["alice", "bob"], "carol", "alice"),
    "exists": (True, "qualquer", None),  # None ⇒ campo AUSENTE no evento
}


def test_divergent_operator_set_is_exactly_the_documented_three():
    """Meta-guard: sem ele o parametrize abaixo pode secar e virar nada.

    Parametrize vazio no pytest não reprova — o teste simplesmente desaparece da
    coleta, que é a mesma vacuidade do skip que este bloco substituiu. Se alguém
    acrescentar os operadores a ``_OPS`` (fechando a divergência) ou inventar um
    11º operador em voo, isto cai e obriga uma decisão explícita.
    """
    divergent = set(INFLIGHT_ALLOWED_OPS) - set(BATCH_OPS)
    assert divergent == {"in", "nin", "exists"}, (
        f"o conjunto divergente virou {sorted(divergent)}: atualize "
        "_INFLIGHT_ONLY_CASES e o docstring do módulo, ou apague os dois se o "
        "vocabulário finalmente foi unificado"
    )
    assert set(_INFLIGHT_ONLY_CASES) == divergent, (
        "todo operador divergente precisa de caso aqui — sem o caso, o guard "
        "abaixo não o cobre e a divergência volta a ser invisível"
    )


@pytest.mark.parametrize("op", sorted(_INFLIGHT_ONLY_CASES))
def test_inflight_only_operators_are_dead_clauses_in_the_batch_engine(op):
    """A divergência vira ASSERÇÃO: no batch estes operadores nunca casam.

    Não é "o batch reprovou porque o valor não bateu" — é cláusula morta. O par
    que prova isso:

    * POSITIVO — no inflight o operador tem semântica real: casa o evento que
      deve casar e reprova o que não deve. Sem este lado, o ``False`` do batch
      seria compatível com "o operador não existe em lugar nenhum".
    * NEGATIVO — no batch os DOIS eventos são reprovados, inclusive aquele que a
      semântica do operador aprovaria. Só cláusula morta se comporta assim.

    Consequência para quem opera: regra batch com estes ops nunca dispara. Hoje
    a API barra a escrita (``WhereFilter.op`` é ``Literal`` dos 7 do batch), mas
    ``_validate_where`` só exige ``bad_json`` no modo batch — alargar aquele
    ``Literal`` abre o buraco com 200 OK. Ver o docstring do módulo.
    """
    json_value, matching, non_matching = _INFLIGHT_ONLY_CASES[op]
    inflight_value = _POSITIVE[op][1]

    assert compare_values(op, matching, inflight_value) is True, (
        f"{op!r} parou de casar no inflight — o caso deste teste ficou obsoleto"
    )
    assert compare_values(op, non_matching, inflight_value) is False, (
        f"{op!r} casou no inflight o evento que NÃO deveria casar: o operador "
        "virou catch-all e o positivo acima deixou de significar algo"
    )

    for actual in (matching, non_matching):
        # ``None`` modela campo AUSENTE, não campo com valor nulo: é assim que
        # ``exists`` é exercitado de verdade.
        item = {} if actual is None else {"campo": actual}
        assert (
            matches_where(item, [{"field": "campo", "op": op, "value": json_value}])
            is False
        ), (
            f"{op!r} passou a casar no batch com actual={actual!r}: a "
            "divergência FOI FECHADA — mova o operador para o teste de paridade "
            "e apague este caso, em vez de deixar os dois guards se contradizerem"
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
