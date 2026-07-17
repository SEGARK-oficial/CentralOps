"""Engine de mapping — interpretador da DSL declarativa (RF3.3, RF3.4).

Recebe ``rules`` (lista de dicts no formato da DSL v1, ou dict no formato
da DSL v2) + ``raw`` (payload do vendor) e produz um dict com path-based
writes baseado em ``target``.

Convenção de uso pelo CentralOps: ``target`` sempre começa com
``normalized.`` para que ``envelope.build_envelope`` apenas faça merge
do bloco ``normalized`` produzido pelo engine. A engine, no entanto,
não força isso — quem usa pode também escrever em ``_centralops.*``
para overrides controlados.

DSL v1 (list-shaped, legado)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
A DSL aceita por regra:

- ``target`` (str, obrigatório): path dot-separated onde escrever.
- ``source`` (str, JMESPath, opcional): expressão para extrair valor
  do raw. Mutuamente exclusivo com ``const``.
- ``const`` (any, opcional): valor literal.
- ``default`` (any, opcional): fallback se ``source`` resolveu ``None``.
- ``pre_cast`` (str, opcional): nome do cast (mesmo registry de
  ``type_cast``) aplicado ANTES de ``value_map``. Permite normalizar
  o tipo do valor antes do lookup -- ex. ``to_str`` para converter
  ``int`` em ``str`` antes de ``value_map`` com chaves de string.
  ``pre_cast`` e ``value_map`` **não** são mutuamente exclusivos;
  combiná-los é justamente o caso de uso.
- ``value_map`` (dict, opcional): lookup pós-resolução.
- ``type_cast`` (str, opcional): nome do cast em :mod:`operators`.
- ``required`` (bool, opcional, default ``False``): se ``True`` e o
  valor final for ``None``, levanta :class:`MappingRequiredFieldError`
  -> pipeline manda para quarentena (RF2.6).

DSL v2 (dict-shaped)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
O payload passa a ser um dict com as chaves:

- ``preprocess`` (list[dict], opcional): lista de operadores de
  pré-processamento executados UMA vez antes do loop de regras.
  Cada item: ``{op, source, target, tolerant}``.
  Targets DEVEM começar com ``_``.
- ``rules`` (list[dict], obrigatório): mesma gramática de regras que v1.
  Targets NÃO PODEM começar com ``_`` (namespace reservado para preprocess).

Ordem de aplicação por regra::

    source/const -> default -> pre_cast -> value_map -> type_cast

Performance: expressões JMESPath são compiladas e cacheadas por
(mapping_version_id, dsl_version) via :func:`compile_rules`. O cache LRU
vive na instância do :class:`MappingEngine` -- uma por processo de worker.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, Literal, Mapping, Optional, Sequence, Union

import jmespath
from jmespath.parser import ParsedResult

from . import registry as _registry
from .array_builder import (
    CompiledArrayBuilderRule,
    apply_array_builder,
    compile_array_builder_rule,
)
from .exceptions import (  # noqa: F401 — re-export for backward compat
    MappingDefinitionError,
    MappingError,
    MappingRequiredFieldError,
)
from .operators import (
    OperatorError,
    apply_default,
    apply_type_cast,
    apply_value_map,
)
from .registry import OperatorSizeError
from .predicates import (
    CompiledPredicate,
    collect_predicate_source_strs as _collect_predicate_source_strs,
    compile_predicate as _compile_predicate,
    evaluate_predicate as _evaluate_predicate,
)
from .preprocess import (
    PREPROCESS_OPS,
    CompiledPreprocessOp,
    apply_preprocess_op,
)
from .payload_reduction import (
    CompiledReductionSpec,
    apply_raw_reduction,
    compile_raw_reduction,
)

logger = logging.getLogger(__name__)

# Sentinel para distinguir "valor ausente" de "valor literal None". A
# DSL pode legitimamente mapear para ``None`` (ex: campo opcional).
_MISSING = object()

# Fontes válidas para ``default_from`` (resiliência temporal).
# ``ingest_time``: quando o valor resolve None (após source + fallback_source),
# usa o epoch de ingestão passado ao engine. Rede de segurança GENÉRICA — a
# DECISÃO de usá-la é declarativa, por mapping (não vendor-específico no Core).
_DEFAULT_FROM_INGEST_TIME = "ingest_time"
_DEFAULT_FROM_SOURCES = frozenset({_DEFAULT_FROM_INGEST_TIME})


@dataclass(frozen=True)
class CompiledRule:
    """Forma pré-validada e pré-compilada de uma regra da DSL.

    Campos adicionais para ``fallback_source``:

    - ``fallback_compiled_sources``: tupla de JMESPath compilados, na ordem
      declarada em ``fallback_source``.  Vazia para regras sem fallback.
    - ``fallback_source_strs``: tupla paralela com as strings originais,
      usadas para drift detection (prefixadas com ``source:`` ao incluir em
      ``consumed_paths``).

    Regra de ``source_root`` para fallbacks:
        O ``source_root`` é determinado EXCLUSIVAMENTE pelo prefixo ``_`` do
        source primário.  Todos os fallbacks DEVEM usar a mesma raiz que o
        primary — isto é garantido em compile time (``_compile_single_rule``
        levanta ``MappingDefinitionError`` se houver inconsistência).  Não há
        troca de raiz no meio da cadeia de fallback.
    """

    target: str
    target_path: tuple[str, ...]
    source: Optional[ParsedResult]  # JMESPath expr compilada
    source_str: Optional[str]  # JMESPath original (para drift detection)
    source_root: Literal["raw", "extracted"]  # onde resolver o JMESPath (v2)
    const: Any  # _MISSING se não houver
    default: Any  # _MISSING se não houver
    value_map: Optional[Mapping[Any, Any]]
    pre_cast: Optional[str]  # cast aplicado ANTES de value_map
    type_cast: Optional[str]  # cast aplicado APOS value_map
    required: bool
    # fallback_source — lista ordenada de JMESPath tentados quando
    # o source primário resolve None.  Ambos padrão para tupla vazia →
    # compatibilidade retroativa total com regras v1 e v2 sem fallback.
    fallback_compiled_sources: tuple[ParsedResult, ...] = ()
    fallback_source_strs: tuple[str, ...] = ()
    # when — predicate que guarda a regra.  Se None, a regra é
    # aplicada incondicionalmente.  Se avaliado como False, a regra é
    # IGNORADA (o target NÃO é escrito — semanticamente diferente de
    # escrever None ou o default).
    # ``predicate_source_strs``: todas as expressões JMESPath na árvore do
    # predicate, usadas para drift detection (adicionadas a consumed_paths).
    when_predicate: Optional[CompiledPredicate] = None
    predicate_source_strs: tuple[str, ...] = ()
    # expected_always_default — flag diagnóstico.
    # Se True, a regra é intencionalmente um placeholder/always-default e
    # NÃO deve ser sinalizada no warning de 100%-default do dry-run.
    # Não altera comportamento em runtime — puramente metadata.
    expected_always_default: bool = False
    # Resiliência temporal: fonte de fallback de último recurso resolvida pelo
    # engine (não do raw). Hoje só ``"ingest_time"``. Aplicado APÓS source +
    # fallback_source + default literal, ANTES do check ``required`` — de modo
    # que um campo temporal required nunca quarentene só por falta de timestamp.
    # ``None`` (default) = sem fallback de engine; compat retroativa total.
    default_from: Optional[str] = None


@dataclass(frozen=True)
class CompiledRules:
    """Resultado compilado de uma versão de mapping (v1 ou v2).

    - ``preprocess_ops``: operadores de pré-processamento (vazio para v1).
    - ``rules``: regras compiladas.  Cada elemento pode
      ser :class:`CompiledRule` (scalar) ou :class:`CompiledArrayBuilderRule`
      (kind="array_builder").  v1 mappings sempre produzem apenas
      ``CompiledRule`` entries.
    """

    preprocess_ops: tuple[CompiledPreprocessOp, ...]
    rules: tuple[Union[CompiledRule, CompiledArrayBuilderRule], ...]
    # Fase de redução de payload (v2, opcional): specs declaradas por mapping
    # que o engine aplica ao raw APÓS as regras, produzindo ``reduced_raw`` no
    # ``ApplyResult`` para o dispatch caber no limite do destino (ex.: Wazuh
    # ~64 KiB). Vazia para v1 e v2 sem ``raw_reduction``.
    raw_reduction: tuple[CompiledReductionSpec, ...] = ()


# -- Compilação --------------------------------------------------------

def _validate_target(target: str) -> tuple[str, ...]:
    if not isinstance(target, str) or not target.strip():
        raise MappingDefinitionError("regra sem 'target' válido")
    parts = tuple(p for p in target.split(".") if p)
    if not parts:
        raise MappingDefinitionError(f"target inválido: {target!r}")
    return parts


def _compile_single_rule(
    idx: int,
    rule: Any,
    *,
    reject_underscore_target: bool = False,
    allow_fallback_source: bool = False,
    allow_when: bool = False,
    allow_expected_always_default: bool = False,
    allow_default_from: bool = False,
) -> CompiledRule:
    """Compila uma única regra DSL em CompiledRule.

    Args:
        idx: Índice da regra (para mensagens de erro).
        rule: Dict da regra DSL.
        reject_underscore_target: Se True (v2), rejeita targets com prefixo ``_``.
        allow_fallback_source: Se True (v2), aceita e compila o campo
            ``fallback_source``.  Quando False (v1), a presença de
            ``fallback_source`` levanta ``MappingDefinitionError``.
        allow_when: Se True (v2), aceita e compila o campo ``when``.
            Quando False (v1), a presença de ``when`` levanta
            ``MappingDefinitionError``.
        allow_expected_always_default: Se True (v2), aceita o campo
            ``expected_always_default``.  Quando False (v1), a presença do
            campo levanta ``MappingDefinitionError`` para evitar uso silencioso
            em mappings legados.
    """
    if not isinstance(rule, Mapping):
        raise MappingDefinitionError(f"regra #{idx} não é um objeto")

    target = rule.get("target")
    target_path = _validate_target(target)

    if reject_underscore_target and target.startswith("_"):
        raise MappingDefinitionError(
            f"regra {target!r}: target começando com '_' é reservado para "
            "o bloco 'preprocess'. Use um nome diferente ou mova para preprocess."
        )

    has_source = "source" in rule
    has_const = "const" in rule
    if has_source and has_const:
        raise MappingDefinitionError(
            f"regra {target!r}: 'source' e 'const' são mutuamente exclusivos"
        )
    if not has_source and not has_const:
        raise MappingDefinitionError(
            f"regra {target!r}: precisa de 'source' (JMESPath) ou 'const'"
        )

    source_expr: Optional[ParsedResult] = None
    source_str: Optional[str] = None
    source_root: Literal["raw", "extracted"] = "raw"
    if has_source:
        raw_source = rule["source"]
        if not isinstance(raw_source, str) or not raw_source.strip():
            raise MappingDefinitionError(
                f"regra {target!r}: 'source' deve ser string JMESPath não-vazia"
            )
        try:
            source_expr = jmespath.compile(raw_source)
            source_str = raw_source
        except Exception as exc:  # jmespath.exceptions.ParseError
            raise MappingDefinitionError(
                f"regra {target!r}: JMESPath inválido {raw_source!r}: {exc}"
            ) from exc
        # Compile-time decision: _ prefix -> consult extracted dict, else raw.
        source_root = "extracted" if raw_source.startswith("_") else "raw"

    type_cast = rule.get("type_cast")
    if type_cast is not None and not isinstance(type_cast, str):
        raise MappingDefinitionError(
            f"regra {target!r}: 'type_cast' deve ser string"
        )

    pre_cast = rule.get("pre_cast")
    if pre_cast is not None:
        if not isinstance(pre_cast, str):
            raise MappingDefinitionError(
                f"regra {target!r}: 'pre_cast' deve ser string"
            )
        if pre_cast not in _registry.TYPE_CASTS:
            raise MappingDefinitionError(
                f"regra {target!r}: 'pre_cast' desconhecido {pre_cast!r}. "
                f"Suportados: {sorted(_registry.TYPE_CASTS.keys())}"
            )

    value_map = rule.get("value_map")
    if value_map is not None and not isinstance(value_map, Mapping):
        raise MappingDefinitionError(
            f"regra {target!r}: 'value_map' deve ser dict"
        )

    # ── fallback_source ─────────────────────────────────────
    raw_fallbacks = rule.get("fallback_source")
    fallback_compiled: tuple[ParsedResult, ...] = ()
    fallback_strs: tuple[str, ...] = ()

    if raw_fallbacks is not None:
        if not allow_fallback_source:
            raise MappingDefinitionError(
                f"regra {target!r}: 'fallback_source' requires DSL v2 "
                "(use dict-shaped mapping)"
            )
        if not has_source:
            raise MappingDefinitionError(
                f"regra {target!r}: 'fallback_source' requires a primary source"
            )
        if not isinstance(raw_fallbacks, list):
            raise MappingDefinitionError(
                f"regra {target!r}: 'fallback_source' deve ser uma lista de strings"
            )
        compiled_list: list[ParsedResult] = []
        str_list: list[str] = []
        primary_is_extracted = source_root == "extracted"
        for fb_idx, fb_expr in enumerate(raw_fallbacks):
            if not isinstance(fb_expr, str):
                raise MappingDefinitionError(
                    f"regra {target!r}: 'fallback_source[{fb_idx}]' deve ser string"
                )
            fb_is_extracted = fb_expr.startswith("_")
            if fb_is_extracted != primary_is_extracted:
                raise MappingDefinitionError(
                    f"regra {target!r}: 'fallback_source[{fb_idx}]' {fb_expr!r} "
                    f"usa raiz diferente do source primário {source_str!r}. "
                    "Todos os fallbacks devem usar a mesma source_root que o "
                    "source primário (ambos raw ou ambos extracted)."
                )
            try:
                compiled_list.append(jmespath.compile(fb_expr))
                str_list.append(fb_expr)
            except Exception as exc:
                raise MappingDefinitionError(
                    f"regra {target!r}: 'fallback_source[{fb_idx}]' JMESPath "
                    f"inválido {fb_expr!r}: {exc}"
                ) from exc
        fallback_compiled = tuple(compiled_list)
        fallback_strs = tuple(str_list)

    # ── when predicate ──────────────────────────────────────
    raw_when = rule.get("when")
    compiled_when: Optional[CompiledPredicate] = None
    predicate_strs: tuple[str, ...] = ()

    if raw_when is not None:
        if not allow_when:
            raise MappingDefinitionError(
                f"regra {target!r}: 'when' requires DSL v2 "
                "(use dict-shaped mapping)"
            )
        compiled_when = _compile_predicate(raw_when)
        predicate_strs = _collect_predicate_source_strs(compiled_when)

    # ── expected_always_default ───────────────────────────
    raw_ead = rule.get("expected_always_default", False)
    # Presença explícita (mesmo que False) em v1 é rejeitada para evitar
    # uso silencioso do flag de supressão em mappings legados.
    if "expected_always_default" in rule and not allow_expected_always_default:
        raise MappingDefinitionError(
            f"regra {target!r}: 'expected_always_default' requires DSL v2 "
            "(use dict-shaped mapping)"
        )
    if not isinstance(raw_ead, bool):
        raise MappingDefinitionError(
            f"regra {target!r}: 'expected_always_default' deve ser bool, "
            f"recebeu {type(raw_ead).__name__!r}"
        )

    # ── default_from (resiliência temporal) ───────────────────────────
    raw_default_from = rule.get("default_from")
    default_from: Optional[str] = None
    if raw_default_from is not None:
        if not allow_default_from:
            raise MappingDefinitionError(
                f"regra {target!r}: 'default_from' requires DSL v2 "
                "(use dict-shaped mapping)"
            )
        if raw_default_from not in _DEFAULT_FROM_SOURCES:
            raise MappingDefinitionError(
                f"regra {target!r}: 'default_from' inválido {raw_default_from!r}. "
                f"Suportados: {sorted(_DEFAULT_FROM_SOURCES)}"
            )
        default_from = raw_default_from

    return CompiledRule(
        target=target,
        target_path=target_path,
        source=source_expr,
        source_str=source_str,
        source_root=source_root,
        const=rule.get("const", _MISSING),
        default=rule.get("default", _MISSING),
        value_map=value_map,
        pre_cast=pre_cast,
        type_cast=type_cast,
        required=bool(rule.get("required", False)),
        fallback_compiled_sources=fallback_compiled,
        fallback_source_strs=fallback_strs,
        when_predicate=compiled_when,
        predicate_source_strs=predicate_strs,
        expected_always_default=raw_ead,
        default_from=default_from,
    )


def _compile_preprocess_op(idx: int, item: Any) -> CompiledPreprocessOp:
    """Valida e compila um item do bloco 'preprocess'."""
    if not isinstance(item, Mapping):
        raise MappingDefinitionError(
            f"preprocess[{idx}]: item deve ser um objeto, recebeu "
            f"{type(item).__name__}"
        )

    op = item.get("op")
    if not isinstance(op, str) or not op.strip():
        raise MappingDefinitionError(
            f"preprocess[{idx}]: 'op' é obrigatório e deve ser string"
        )
    if op not in PREPROCESS_OPS:
        raise MappingDefinitionError(
            f"preprocess[{idx}]: op {op!r} desconhecido. "
            f"Suportados: {sorted(PREPROCESS_OPS.keys())}"
        )

    source_raw = item.get("source")
    if not isinstance(source_raw, str) or not source_raw.strip():
        raise MappingDefinitionError(
            f"preprocess[{idx}]: 'source' é obrigatório e deve ser JMESPath string"
        )
    try:
        compiled_source = jmespath.compile(source_raw)
    except Exception as exc:
        raise MappingDefinitionError(
            f"preprocess[{idx}]: JMESPath inválido {source_raw!r}: {exc}"
        ) from exc

    target = item.get("target")
    if not isinstance(target, str) or not target.strip():
        raise MappingDefinitionError(
            f"preprocess[{idx}]: 'target' é obrigatório e deve ser string"
        )
    if not target.startswith("_"):
        raise MappingDefinitionError(
            f"preprocess[{idx}]: 'target' {target!r} deve começar com '_' "
            "(namespace reservado para preprocess)"
        )

    tolerant = bool(item.get("tolerant", False))

    return CompiledPreprocessOp(
        op=op,
        compiled_source=compiled_source,
        source_str=source_raw,
        target=target,
        tolerant=tolerant,
    )


def _compile_v1(rules: Sequence[Any]) -> CompiledRules:
    """Compila DSL v1 (lista de regras). Lógica original, inalterada.

    ``fallback_source``, ``when``, e ``kind`` são explicitamente rejeitados
    em v1 — todos requerem v2.  O campo ``kind`` (incluindo
    ``kind: "array_builder"``) é uma funcionalidade exclusiva da DSL v2.
    """
    if not isinstance(rules, (list, tuple)):
        raise MappingDefinitionError("rules deve ser uma lista")

    compiled: list[CompiledRule] = []
    for idx, rule in enumerate(rules):
        # Reject `kind` in v1 — it requires DSL v2.
        if isinstance(rule, Mapping) and "kind" in rule:
            target = rule.get("target", f"#{idx}")
            raise MappingDefinitionError(
                f"regra {target!r}: 'kind' requires DSL v2 "
                "(use dict-shaped mapping with dsl_version=2)"
            )
        compiled.append(
            _compile_single_rule(
                idx,
                rule,
                reject_underscore_target=False,
                allow_fallback_source=False,
                allow_when=False,
            )
        )

    return CompiledRules(
        preprocess_ops=(),
        rules=tuple(compiled),
    )


def _compile_v2(payload: Mapping[str, Any]) -> CompiledRules:
    """Compila DSL v2 (dict com 'preprocess' opcional + 'rules' obrigatório)."""
    if not isinstance(payload, Mapping):
        raise MappingDefinitionError("DSL v2 espera um dict com 'rules' e 'preprocess'")

    preprocess_raw = payload.get("preprocess")
    preprocess_ops: list[CompiledPreprocessOp] = []
    if preprocess_raw is not None:
        if not isinstance(preprocess_raw, (list, tuple)):
            raise MappingDefinitionError("DSL v2: 'preprocess' deve ser uma lista")
        for idx, item in enumerate(preprocess_raw):
            preprocess_ops.append(_compile_preprocess_op(idx, item))

    rules_raw = payload.get("rules")
    if rules_raw is None:
        raise MappingDefinitionError("DSL v2: 'rules' é obrigatório")
    if not isinstance(rules_raw, (list, tuple)):
        raise MappingDefinitionError("DSL v2: 'rules' deve ser uma lista")
    if len(rules_raw) == 0:
        raise MappingDefinitionError("DSL v2: 'rules' não pode ser lista vazia")

    compiled_rules: list[Union[CompiledRule, CompiledArrayBuilderRule]] = []
    for idx, rule in enumerate(rules_raw):
        if not isinstance(rule, Mapping):
            raise MappingDefinitionError(f"DSL v2: rules[{idx}] deve ser um objeto")

        kind = rule.get("kind", "scalar")

        if kind == "scalar":
            compiled_rules.append(
                _compile_single_rule(
                    idx,
                    rule,
                    reject_underscore_target=True,
                    allow_fallback_source=True,
                    allow_when=True,
                    allow_expected_always_default=True,
                    allow_default_from=True,
                )
            )
        elif kind == "array_builder":
            compiled_rules.append(compile_array_builder_rule(rule))
        else:
            target = rule.get("target", f"#{idx}")
            raise MappingDefinitionError(
                f"regra {target!r}: 'kind' {kind!r} desconhecido. "
                "Valores válidos: 'scalar' (default), 'array_builder'."
            )

    raw_reduction = compile_raw_reduction(payload.get("raw_reduction"))

    return CompiledRules(
        preprocess_ops=tuple(preprocess_ops),
        rules=tuple(compiled_rules),
        raw_reduction=raw_reduction,
    )


def compile_rules(
    rules_or_dict: Union[Sequence[Any], Mapping[str, Any]],
    dsl_version: Optional[int] = None,
) -> CompiledRules:
    """Valida e compila a DSL de uma versão de mapping.

    Despacha para ``_compile_v1`` ou ``_compile_v2`` conforme o tipo de
    ``rules_or_dict`` e o valor de ``dsl_version``.

    Args:
        rules_or_dict:
            - ``list`` ou ``tuple``: contrato v1 (lista de regras).
            - ``dict``: contrato v2 (chaves ``preprocess`` + ``rules``).
        dsl_version: versão da DSL. ``None`` = auto-detect pelo shape
            (list → 1, dict → 2). Passando explicitamente ``1`` ou ``2``
            força a validação contra o shape esperado.

    Returns:
        :class:`CompiledRules` com ``preprocess_ops`` e ``rules``.

    Raises:
        :class:`MappingDefinitionError`: Se a forma for inválida.

    Nota sobre ``pre_cast`` e ``value_map``: os dois campos nao sao
    mutuamente exclusivos.  Combiná-los é o caso de uso intencional --
    ``pre_cast`` normaliza o tipo antes do lookup.
    """
    if dsl_version is None:
        if isinstance(rules_or_dict, (list, tuple)):
            dsl_version = 1
        elif isinstance(rules_or_dict, Mapping):
            dsl_version = 2
        else:
            raise MappingDefinitionError(
                f"rules_or_dict deve ser list ou dict, recebeu {type(rules_or_dict).__name__}"
            )

    if dsl_version not in (1, 2):
        raise MappingDefinitionError(
            f"dsl_version {dsl_version!r} não suportada. Valores válidos: 1, 2."
        )

    if isinstance(rules_or_dict, (list, tuple)):
        if dsl_version == 2:
            raise MappingDefinitionError(
                "DSL v2 espera um dict com 'rules' e 'preprocess', recebeu list/tuple. "
                "Use dsl_version=1 para listas simples."
            )
        return _compile_v1(rules_or_dict)

    if isinstance(rules_or_dict, Mapping):
        if dsl_version == 1:
            raise MappingDefinitionError(
                "DSL v1 espera rules como list, got dict. "
                "Use dsl_version=2 para o formato dict com 'preprocess'/'rules'."
            )
        return _compile_v2(rules_or_dict)

    raise MappingDefinitionError(
        f"rules_or_dict deve ser list ou dict, recebeu {type(rules_or_dict).__name__}"
    )


def detect_dsl_version(rules_or_dict: Any) -> int:
    """Heurística pública: list → 1, dict → 2.

    Útil para callers que precisam persistir ``dsl_version`` no banco
    sem ter que abrir o ``rules_or_dict``.
    """
    if isinstance(rules_or_dict, (list, tuple)):
        return 1
    if isinstance(rules_or_dict, Mapping):
        return 2
    raise MappingDefinitionError(
        f"detect_dsl_version: shape inesperado {type(rules_or_dict).__name__}"
    )


# -- Aplicação ---------------------------------------------------------

def _resolve_value(
    rule: CompiledRule,
    raw: Mapping[str, Any],
    extracted: Mapping[str, Any],
) -> Any:
    """Resolve o valor de uma regra consultando raw ou extracted.

    A decisão de raiz é tomada em tempo de compilação (``rule.source_root``):
    - ``"extracted"``: pesquisa em ``extracted`` (namespace ``_``).
    - ``"raw"``: pesquisa em ``raw`` (comportamento original v1).

    Cadeia de fallback:
        Se o source primário retorna ``None`` e a regra declara
        ``fallback_compiled_sources``, cada fallback é tentado na ordem
        declarada, usando a MESMA raiz do primary (``source_root``).  O
        primeiro resultado não-null é retornado.  Se toda a cadeia resulta
        em ``None``, retorna ``None`` e a lógica de ``default`` em
        ``apply_compiled`` trata o restante.

        Nota: todos os fallbacks compartilham ``source_root`` com o primary —
        isso é validado em compile time.  Não há troca de raiz mid-rule.
    """
    if rule.const is not _MISSING:
        return rule.const
    if rule.source is None:
        raise MappingError(f"regra {rule.target!r}: sem source nem const")

    root = extracted if rule.source_root == "extracted" else raw
    value = rule.source.search(root)

    if value is None and rule.fallback_compiled_sources:
        for fb in rule.fallback_compiled_sources:
            value = fb.search(root)
            if value is not None:
                break

    return value


def _set_path(target: Dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    cursor = target
    for key in path[:-1]:
        nxt = cursor.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cursor[key] = nxt
        cursor = nxt
    cursor[path[-1]] = value


@dataclass
class ApplyResult:
    """Resultado de aplicar um mapping a um raw event.

    Campos de diagnóstico:

    - ``default_hits``: tupla de ``rule.target`` cujo valor resolvido foi
      ``None`` ANTES de ``default`` ser aplicado. Somente regras scalar com
      ``default`` configurado são contadas — array_builder rules são excluídas.
      Usada pelo dry-run para calcular a taxa de default por regra e emitir
      o warning "100%-default" quando apropriado.
    """

    output: Dict[str, Any]
    consumed_paths: frozenset[str]  # paths JMESPath efetivamente lidos do raw
    default_hits: tuple[str, ...] = ()  # targets com value=None antes de default
    # Resiliência temporal: targets cujo valor veio do fallback de ingestão
    # (``default_from: "ingest_time"``). O pipeline marca esses como degradados
    # no envelope (proveniência) — o analista sabe que o timestamp é aproximado.
    ingest_fallback_targets: tuple[str, ...] = ()
    # Redução de payload: cópia reduzida do raw para o dispatch (quando o
    # mapping declara ``raw_reduction`` E alguma redução foi aplicada). ``None``
    # quando não há redução — o caller reusa o raw original.
    reduced_raw: Optional[Dict[str, Any]] = None


def apply_compiled(
    compiled: CompiledRules,
    raw: Mapping[str, Any],
    *,
    ingest_time_epoch: Optional[int] = None,
) -> ApplyResult:
    """Aplica regras compiladas a um payload raw.

    Aceita :class:`CompiledRules` (v1 e v2).  Em v1, ``preprocess_ops``
    é uma tupla vazia e o comportamento é idêntico ao legado.

    Fase de pré-processamento (v2):
        Cada op em ``preprocess_ops`` é executado uma vez, na ordem de
        declaração.  O resultado é gravado em ``extracted``.  Erros são
        tratados conforme ``op.tolerant``.

    Fase de regras (v1 e v2):
        Mesma lógica de antes, com ``_resolve_value`` roteando para raw
        ou extracted conforme ``rule.source_root``.

    Predicado ``when``:
        Se ``rule.when_predicate is not None``, o predicado é avaliado
        contra a raiz da regra (raw ou extracted, conforme
        ``rule.source_root``).  Se False, a regra é IGNORADA — o target
        NÃO é escrito.  Isso é semanticamente diferente de escrever None
        ou o ``default``: a chave simplesmente não existe no output.
        O ``default`` NÃO é aplicado quando a regra é ignorada.

    Ordem de aplicação por regra (quando não ignorada pelo when)::

        source/const -> default -> pre_cast -> value_map -> type_cast
    """
    output: Dict[str, Any] = {}
    consumed: set[str] = set()
    extracted: Dict[str, Any] = {}
    _default_hits: list[str] = []
    _ingest_fallback: list[str] = []

    # Phase 1: preprocess (v2 only; empty tuple for v1)
    for op in compiled.preprocess_ops:
        try:
            source_value = op.compiled_source.search(raw)
            result = apply_preprocess_op(source_value, op.op, tolerant=op.tolerant)
            extracted[op.target] = result
            if source_value is not None:
                consumed.add(f"source:{op.source_str}")
        except OperatorSizeError:
            # DoS guard — NUNCA silenciada por tolerant=True.
            # Propaga como MappingError para que o pipeline quarentene o evento.
            raise MappingError(
                f"preprocess {op.op}({op.source_str!r}) excedeu limite de tamanho: "
                "evento quarentenado (proteção DoS)"
            )
        except OperatorError as exc:
            if op.tolerant:
                extracted[op.target] = None
                logger.debug(
                    "preprocess %s(%s) tolerant error: %s", op.op, op.source_str, exc
                )
            else:
                raise MappingError(
                    f"preprocess {op.op}({op.source_str!r}) falhou: {exc}"
                ) from exc

    # Phase 2: rule loop
    for rule in compiled.rules:
        # ── array_builder dispatch ─────────────────────────────
        if isinstance(rule, CompiledArrayBuilderRule):
            result_list = apply_array_builder(rule, raw, extracted)
            # Register all item source paths for drift detection.
            for item in rule.items:
                consumed.add(f"source:{item.source_str}")
            _set_path(output, rule.target_path, result_list)
            continue

        # ── when predicate gate ────────────────────────────────
        # Evaluate before anything else.  If False, skip the rule entirely:
        # the target is NOT written (no None, no default).  The predicate
        # source paths are still added to consumed_paths for drift detection.
        if rule.when_predicate is not None:
            pred_root: Mapping[str, Any] = (
                extracted if rule.source_root == "extracted" else raw
            )
            if rule.predicate_source_strs:
                for ps in rule.predicate_source_strs:
                    consumed.add(f"source:{ps}")
            if not _evaluate_predicate(rule.when_predicate, pred_root):
                continue  # SKIP — target not written

        try:
            value = _resolve_value(rule, raw, extracted)
        except Exception as exc:
            raise MappingError(
                f"regra {rule.target!r}: erro ao resolver source: {exc}"
            ) from exc

        if rule.source is not None and value is not None:
            if rule.source_str:
                consumed.add(f"source:{rule.source_str}")
            consumed.add(rule.target)

        # Drift detection: register all fallback paths regardless of which one
        # resolved the value.  This prevents the drift detector from falsely
        # flagging fallback paths as "unmapped" fields.  Guard with truthiness
        # check so the empty-tuple case (all v1 rules) pays zero iteration cost.
        if rule.fallback_source_strs:
            for fb_str in rule.fallback_source_strs:
                consumed.add(f"source:{fb_str}")

        used_default = False
        if rule.default is not _MISSING:
            # contabiliza default hits ANTES de aplicar o fallback.
            # Somente scalar rules com default — array_builder é excluído acima.
            if value is None:
                _default_hits.append(rule.target)
                used_default = True
            value = apply_default(value, rule.default)

        # Resiliência temporal: fallback de ingestão. Aplicado após source +
        # fallback_source + default literal, ANTES do check ``required`` — um
        # campo temporal required nunca quarentena só por timestamp ausente.
        # Genérico no Core; a decisão de usar é declarativa (default_from).
        if (
            value is None
            and rule.default_from == _DEFAULT_FROM_INGEST_TIME
            and ingest_time_epoch is not None
        ):
            value = ingest_time_epoch
            _ingest_fallback.append(rule.target)

        # pre_cast + value_map: aplicados ao valor resolvido do SOURCE — NÃO a um
        # valor vindo do ``default``. O ``default`` é o fallback FINAL escolhido
        # pelo operador (já na forma de saída), não um source cru a normalizar:
        # aplicar um pre_cast de string (lowercase/uppercase/trim) a um ``default``
        # int/não-string levantaria OperatorError → MappingError → o evento INTEIRO
        # seria descartado/quarentenado (bug: um status_id ausente com default 1
        # derrubava a normalização toda). value_map também é pulado — o default já é
        # o valor de saída, não uma chave crua a mapear. ``type_cast`` (pós-value_map,
        # abaixo) SEGUE aplicando: um default pode legitimamente precisar de coerção.
        if rule.pre_cast and value is not None and not used_default:
            try:
                value = apply_type_cast(value, rule.pre_cast)
            except OperatorError as exc:
                raise MappingError(
                    f"regra {rule.target!r}: pre_cast {rule.pre_cast!r} falhou: {exc}"
                ) from exc

        if value is None and rule.required:
            raise MappingRequiredFieldError(rule.target)

        if rule.value_map is not None and value is not None and not used_default:
            try:
                value = apply_value_map(value, rule.value_map)
            except OperatorError as exc:
                raise MappingError(
                    f"regra {rule.target!r}: value_map falhou: {exc}"
                ) from exc

        if rule.type_cast and value is not None:
            try:
                value = apply_type_cast(value, rule.type_cast)
            except OperatorError as exc:
                raise MappingError(
                    f"regra {rule.target!r}: type_cast {rule.type_cast!r} falhou: {exc}"
                ) from exc

        _set_path(output, rule.target_path, value)

    # Redução de payload para o dispatch (v2 com raw_reduction). Roda DEPOIS
    # das regras → normalização viu o raw COMPLETO (fidelidade preservada).
    reduced_raw = (
        apply_raw_reduction(raw, compiled.raw_reduction)
        if compiled.raw_reduction
        else None
    )

    return ApplyResult(
        output=output,
        consumed_paths=frozenset(consumed),
        default_hits=tuple(_default_hits),
        ingest_fallback_targets=tuple(_ingest_fallback),
        reduced_raw=reduced_raw,
    )


# -- Engine com cache por (mapping_version_id, dsl_version) -----------

class MappingEngine:
    """Aplica mappings cacheando regras compiladas por versão.

    Uma instância por processo de worker é suficiente -- não há estado
    mutável além do cache. ``apply`` é seguro para chamadas concorrentes
    (asyncio coroutines no mesmo loop): a única região mutada é o dict
    ``_cache``, e sob CPython com GIL inserções dict são atômicas.

    Cache key: ``(mapping_version_id, dsl_version)`` -- garante que
    uma mudança de DSL version invalida a entrada sem precisar do ``invalidate``.
    """

    def __init__(self, max_cache: int = 256) -> None:
        # OrderedDict mantém ordem de inserção E suporta move_to_end para LRU.
        self._cache: OrderedDict[tuple[str, int], CompiledRules] = OrderedDict()
        self._max_cache = max_cache

    def get_compiled(
        self,
        mapping_version_id: str,
        rules: Union[Sequence[Any], Mapping[str, Any]],
        dsl_version: int = 1,
    ) -> CompiledRules:
        """Retorna regras compiladas para a versão, usando cache LRU.

        Cache hit: move a entrada para o final (mais recentemente usada).
        Cache miss: compila, evicta a entrada MAIS ANTIGA por acesso (LRU)
        se necessário, e insere ao final.
        """
        cache_key = (mapping_version_id, dsl_version)
        cached = self._cache.get(cache_key)
        if cached is not None:
            # Promoção LRU: move para o final (mais recentemente usada).
            self._cache.move_to_end(cache_key)
            return cached

        compiled = compile_rules(rules, dsl_version)

        if len(self._cache) >= self._max_cache:
            # Evicta o item menos recentemente usado: o primeiro (last=False).
            self._cache.popitem(last=False)

        self._cache[cache_key] = compiled
        return compiled

    def apply(
        self,
        mapping_version_id: str,
        rules: Union[Sequence[Any], Mapping[str, Any]],
        raw: Mapping[str, Any],
        dsl_version: int = 1,
        *,
        ingest_time_epoch: Optional[int] = None,
    ) -> ApplyResult:
        compiled = self.get_compiled(mapping_version_id, rules, dsl_version)
        return apply_compiled(compiled, raw, ingest_time_epoch=ingest_time_epoch)

    def invalidate(self, mapping_version_id: str) -> None:
        """Remove TODAS as entradas para mapping_version_id do cache."""
        keys_to_remove = [k for k in self._cache if k[0] == mapping_version_id]
        for k in keys_to_remove:
            self._cache.pop(k, None)


# Singleton de processo -- workers importam diretamente.
default_engine = MappingEngine()
