"""Fast-path de dot-path simples para resolução de ``source`` (ADR-0015, Fase 2).

O normalize é o maior custo de CPU por evento do pipeline: medido, 657-717 µs em
``sophos.detection``, com 64% do tempo cumulativo dentro de ``jmespath/visitor.py``
e ~201 buscas por evento. A esmagadora maioria dessas expressões não usa nada de
jmespath — são dot-paths triviais como ``raw.user.name``, para as quais o
interpretador de AST é puro overhead.

Medido em ``apply_compiled`` sobre as 36 fixtures reais de
``tests/benchmarks/fixtures/``: 5.362,4 → 3.408,7 µs somados = **1,57x**.
``sophos.detection`` medium: 773,4 → 437,7 µs/evento (1,77x).
Cobertura nos 17 mappings default: 435/542 expressões (80,3%) elegíveis.

NÃO confundir com os números do resolvedor ISOLADO (18,6x para campo simples):
a resolução é ~43% do custo de ``apply_compiled``, e é o número end-to-end que
importa. E não há ganho em "compilar o jmespath uma vez" — o engine já faz isso,
e o parser do jmespath tem cache LRU próprio.

O resolvedor faz duck-typing de ``jmespath.parser.ParsedResult``: expõe
``search(value, options=None)`` e nada mais. Verificado que nenhum consumidor do
runtime toca internals do ParsedResult.
"""

from __future__ import annotations

import os
import re
from typing import Any, Protocol

import jmespath

# ASCII EXPLÍCITO, NUNCA ``\w``. ``\w`` em Python é UNICODE-AWARE: medido que
# ``re.match(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*$", "aça.b")`` CASA, mas
# ``jmespath.compile("aça.b")`` levanta "Unknown token ç". Com ``\w``, um mapping
# hoje REJEITADO em compile-time viraria fast-path silenciosamente funcional —
# ou seja, o guard de sintaxe deixaria de existir para um subconjunto de nomes.
_DOT_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")

# Lido UMA VEZ no import. NUNCA consultado dentro de ``search()``: a decisão é de
# COMPILE-TIME, e um ``if`` por resolução devolveria parte do ganho.
# Kill-switch de ambiente para o caso de o fast-path divergir em produção.
_FASTPATH_ENABLED = os.environ.get("NORMALIZE_DOTPATH_FASTPATH", "1") != "0"


class SourceResolver(Protocol):
    """Contrato mínimo que ``ParsedResult`` e ``DotPathResolver`` compartilham."""

    def search(self, value: Any, options: Any = None) -> Any: ...


class DotPathResolver:
    """Resolve ``a.b.c`` em dicts aninhados, com a semântica EXATA do jmespath.

    O corpo de :meth:`search` reproduz ``jmespath/visitor.py`` (``visit_field``
    encadeado por ``visit_subexpression``). A equivalência é travada por
    ``test_adr0015_dotpath_equivalence.py``, que compara os dois resolvedores
    sobre casos de borda nomeados E sobre milhares de objetos gerados.

    NÃO "otimizar" trocando ``.get(k)`` por ``[k]``, adicionando
    ``isinstance(v, dict)`` ou alargando o ``except`` para ``TypeError``. Cada uma
    dessas mudanças faz a maioria dos casos de borda voltar a divergir — e a
    divergência é SILENCIOSA: o valor vira ``None``, cai no default do mapping ou
    manda o evento para quarentena, nunca levanta erro.
    """

    __slots__ = ("expression", "_first", "_rest")

    def __init__(self, expression: str) -> None:
        tokens = expression.split(".")
        self.expression = expression
        # O primeiro token sai do laço: evita um teste de ``is None`` a mais no
        # caso de 1 nível, que é 203 das 435 expressões elegíveis.
        self._first = tokens[0]
        self._rest = tuple(tokens[1:])

    def search(self, value: Any, options: Any = None) -> Any:
        # ``.get`` com ``except AttributeError`` e não ``isinstance(v, dict)``:
        # aceita qualquer mapeamento (o jmespath também aceita) e custa zero no
        # caminho feliz, porque a exceção só é montada quando de fato falha.
        try:
            value = value.get(self._first)
        except AttributeError:
            return None
        for key in self._rest:
            if value is None:
                return None
            try:
                value = value.get(key)
            except AttributeError:
                return None
        return value

    def __repr__(self) -> str:  # pragma: no cover — diagnóstico
        return f"DotPathResolver({self.expression!r})"


def is_fast_path(expr: str) -> bool:
    """``True`` se ``expr`` é elegível ao resolvedor rápido. Para telemetria."""
    return bool(_FASTPATH_ENABLED and _DOT_PATH.match(expr))


def compile_source(expr: str) -> SourceResolver:
    """Compila ``expr`` escolhendo o resolvedor em COMPILE-TIME.

    ``jmespath.compile`` é chamado SEMPRE e ANTES da decisão, mesmo quando o
    fast-path vai ser usado: ele é o único validador de sintaxe de mapping em
    compile-time. Pular a chamada transformaria expressões que hoje levantam
    ``MappingDefinitionError`` — e reprovam a criação da versão de mapping — em
    fast-paths silenciosamente funcionais.
    """
    parsed = jmespath.compile(expr)
    if _FASTPATH_ENABLED and _DOT_PATH.match(expr):
        return DotPathResolver(expr)
    return parsed
