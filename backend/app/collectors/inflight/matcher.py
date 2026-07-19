"""Avaliador single-event — PURO (ADR-0015, Fase 1, restrição R1).

O ÚNICO código desta feature que roda por evento. Não faz I/O, não guarda
estado, não loga, não emite métrica, não é ``async``. Um guard estrutural em CI
(``test_adr0015_inflight_matcher_purity.py``) reprova qualquer import proibido
neste módulo — a restrição vira mecânica em vez de convenção, porque um
``import redis`` acrescentado aqui daqui a seis meses seria invisível numa
revisão de PR e custaria um round-trip por evento no gargalo do pipeline.

Não há ``try/except`` interno, e isso é deliberado: ``compare_values`` já captura
``TypeError`` e trata ``actual is None`` explicitamente, ``_resolve`` só navega
dicts e nunca levanta, e os valores do lado direito já foram materializados em
escalares/frozensets/floats na compilação. O conjunto de exceções possíveis é
quase vazio, e a rede de segurança de R3 (fail-open na entrega) é o ``try`` do
hot path, não daqui. O preço é que uma exceção aborta a avaliação daquele evento
— aceito, porque o evento segue no batch e o detector é observador, nunca
porteiro.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..routing.engine import compare_values


@dataclass(frozen=True, slots=True)
class CompiledClause:
    """Um predicado pronto para avaliar.

    ``path`` já vem TOKENIZADO (``"raw.user.name"`` → ``("raw","user","name")``)
    para não pagar um ``str.split`` por evento por cláusula. ``value`` já vem na
    forma final: ``frozenset``/``tuple`` para ``in``/``nin``, ``bool`` para
    ``exists``, ``float`` quando ``numeric``.

    ``numeric=True`` faz o avaliador coagir o lado ESQUERDO com ``float()`` antes
    de comparar. Sem isso, um vendor que serializa severidade como ``"5"`` faria
    ``'5' > 3`` levantar ``TypeError`` dentro de ``compare_values``, que devolve
    False — e a regra nunca casaria, com o contador em zero, indistinguível de
    "o valor simplesmente não bateu". É o modo de falha silenciosa mais provável
    de toda a fase.
    """

    path: tuple[str, ...]
    op: str
    value: Any
    numeric: bool = False


@dataclass(frozen=True, slots=True)
class CompiledInflightRule:
    """Uma regra pronta para avaliar. Imutável e sem referência ao ORM."""

    rule_id: int
    name: str
    severity_id: int
    suppression_window_seconds: int
    #: Seletor da chave de dedup da ``Detection``. ``None`` ⇒ token ``"*"``, o
    #: que colapsa a regra inteira em UMA Detection por ciclo.
    group_by_path: tuple[str, ...] | None
    clauses: tuple[CompiledClause, ...]


@dataclass(frozen=True, slots=True)
class CompiledRuleSet:
    """As regras do ciclo + a decisão de cachear resolução de path.

    ``share_paths`` é calculado UMA VEZ na compilação: True quando algum path
    aparece em mais de uma cláusula do conjunto. É o caso REAL — regras de SOC
    quase sempre começam discriminando por ``_centralops.vendor`` ou
    ``event_type``, então dezenas de regras resolvem o mesmo path por evento.

    A guarda é OBRIGATÓRIA e não uma micro-otimização: medido, o cache
    INCONDICIONAL **piora** 1,21x quando todo path é único (o dict local custa
    mais que os ``dict.get`` que ele evita). Ligado só quando há compartilhamento,
    mede 2,23x de ganho no orçamento máximo.
    """

    rules: tuple[CompiledInflightRule, ...]
    share_paths: bool


def _resolve(envelope: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    """Navega ``path`` no envelope. ``None`` se ausente ou se cruzar não-dict.

    Mesma limitação de ``services/correlation_engine.extract_path``: NÃO navega
    listas. Um path que atravessa array resolve para ``None``, e o efeito disso
    num operador negativo (``nin``) seria fail-open — fechado no compilador por
    auto-injeção de ``exists``.
    """
    cur: Any = envelope
    for part in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
        if cur is None:
            return None
    return cur


def _num(value: Any) -> float | None:
    """``float(value)`` ou ``None``. Porte local de ``_coerce_number``."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def evaluate_inflight(
    envelope: Mapping[str, Any],
    rules: Sequence[CompiledInflightRule],
) -> tuple[CompiledInflightRule, ...]:
    """Devolve as regras cujas cláusulas TODAS casam o envelope (AND implícito).

    Caso comum — nenhum match — devolve ``()``, que é singleton em CPython, logo
    zero alocação no caminho quente.
    """
    matched: list[CompiledInflightRule] = []
    for rule in rules:
        for clause in rule.clauses:
            actual = _resolve(envelope, clause.path)
            if clause.numeric and actual is not None:
                actual = _num(actual)
            if not compare_values(clause.op, actual, clause.value):
                break
        else:
            matched.append(rule)
    return tuple(matched)


def evaluate_ruleset(
    envelope: Mapping[str, Any], ruleset: CompiledRuleSet
) -> tuple[CompiledInflightRule, ...]:
    """Como :func:`evaluate_inflight`, mas cacheia resolução de path quando vale.

    Regras de SOC quase sempre discriminam pelo mesmo campo na primeira cláusula
    (``_centralops.vendor``, ``event_type``), então N regras resolvem o MESMO
    path por evento. Um dict local elimina essa repetição: medido, 2,23x no
    orçamento máximo com paths compartilhados.

    O cache é por EVENTO e morre com a chamada — nada de estado entre eventos,
    que violaria R5. E é condicional: com paths todos distintos o dict custa mais
    do que economiza (1,21x de PIORA medida), por isso ``share_paths`` decide na
    compilação, fora do caminho quente.

    O cache guarda o valor CRU, antes da coerção numérica: a mesma chave pode ser
    usada por uma cláusula ``numeric`` e outra não, e cachear o valor já coagido
    faria a segunda ver um float onde deveria ver a string original.
    """
    if not ruleset.share_paths:
        return evaluate_inflight(envelope, ruleset.rules)

    cache: dict[tuple[str, ...], Any] = {}
    matched: list[CompiledInflightRule] = []
    for rule in ruleset.rules:
        for clause in rule.clauses:
            path = clause.path
            if path in cache:
                actual = cache[path]
            else:
                actual = _resolve(envelope, path)
                cache[path] = actual
            if clause.numeric and actual is not None:
                actual = _num(actual)
            if not compare_values(clause.op, actual, clause.value):
                break
        else:
            matched.append(rule)
    return tuple(matched)
