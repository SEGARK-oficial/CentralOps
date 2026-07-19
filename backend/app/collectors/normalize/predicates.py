"""Predicates for the DSL v2 ``when`` gate (Fase 2.3).

A predicate is a boolean guard that gates a rule.  When the predicate
evaluates to False, the rule is SKIPPED entirely — the target key is
absent from the output.  This is semantically different from a ``default``
(which still writes a value) or a null-source (which writes ``None``).

Predicate language
------------------
A predicate dict must have EXACTLY ONE of these keys:

- ``exists``: JMESPath expression; true if search result is not None.
  Empty list/dict counts as exists (truthy at the JSON level).
  ``None`` (missing path) = not exists.
- ``equals``: ``{source: <JMESPath>, value: <literal>}``.
  Type-strict: int 3 != str "3".
- ``in``: ``{source: <JMESPath>, values: [<literal>, ...]}``.
  Type-strict membership test.
- ``not``: nested predicate; negation.

More than one key or zero keys → ``MappingDefinitionError`` at compile time.

Compile-time guarantees
-----------------------
- JMESPath expressions inside predicates are compiled with
  ``jmespath.compile`` and will raise ``MappingDefinitionError`` for
  syntax errors.
- Predicate structure is validated eagerly so invalid DSL is caught before
  any event is processed.

Runtime safety
--------------
``evaluate_predicate`` never raises.  If a JMESPath search misses (returns
None), the predicate evaluates to False (or the logical equivalent depending
on kind).  This is the safe default — a missing path cannot satisfy
``exists`` or ``equals`` or ``in``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Optional, Union

import jmespath

from .dotpath import compile_source
from jmespath.parser import ParsedResult

from .exceptions import MappingDefinitionError

__all__ = [
    "CompiledPredicate",
    "compile_predicate",
    "evaluate_predicate",
    "collect_predicate_source_strs",
]

# _PRED_KEYS is the exhaustive set of valid top-level discriminators.
_PRED_KEYS: frozenset[str] = frozenset({"exists", "equals", "in", "not"})


@dataclass(frozen=True)
class CompiledPredicate:
    """Compile-time representation of a DSL predicate.

    Fields used per kind:
    - ``exists``:  compiled_source (required).
    - ``equals``:  compiled_source + literal (required).
    - ``in``:      compiled_source + literal (tuple, required).
    - ``not``:     child (required).

    Unused fields default to ``None`` / ``()`` so the dataclass is a single
    type regardless of kind; mypy knows which fields are meaningful from the
    ``kind`` discriminator.
    """

    kind: Literal["exists", "equals", "in", "not"]
    compiled_source: Optional[ParsedResult] = None
    source_str: Optional[str] = None  # original string for drift detection
    literal: Any = None  # scalar for ``equals``; tuple for ``in``
    child: Optional["CompiledPredicate"] = None  # for ``not``


def compile_predicate(spec: Any) -> CompiledPredicate:
    """Compile a raw predicate dict to ``CompiledPredicate``.

    Validates structure and JMESPath expressions at compile time.

    Args:
        spec: Raw dict from the DSL ``when`` field.

    Returns:
        A ``CompiledPredicate`` ready for ``evaluate_predicate``.

    Raises:
        MappingDefinitionError: On any structural or JMESPath syntax error.
    """
    if not isinstance(spec, Mapping):
        raise MappingDefinitionError(
            f"'when' predicate deve ser um objeto dict, recebeu {type(spec).__name__}"
        )

    keys_present = _PRED_KEYS & set(spec.keys())

    if len(keys_present) == 0:
        raise MappingDefinitionError(
            f"'when' predicate não tem nenhuma chave válida. "
            f"Esperado: uma de {sorted(_PRED_KEYS)}"
        )
    if len(keys_present) > 1:
        raise MappingDefinitionError(
            f"'when' predicate tem múltiplas chaves: {sorted(keys_present)}. "
            "Um predicate deve ter EXATAMENTE UMA chave discriminadora."
        )

    kind_str = next(iter(keys_present))

    if kind_str == "exists":
        expr_raw = spec["exists"]
        compiled_src, src_str = _compile_jmespath(expr_raw, context="exists")
        return CompiledPredicate(
            kind="exists",
            compiled_source=compiled_src,
            source_str=src_str,
        )

    if kind_str == "equals":
        inner = spec["equals"]
        if not isinstance(inner, Mapping):
            raise MappingDefinitionError(
                "'when.equals' deve ser um objeto com 'source' e 'value'"
            )
        if "source" not in inner:
            raise MappingDefinitionError("'when.equals' requer campo 'source'")
        if "value" not in inner:
            raise MappingDefinitionError("'when.equals' requer campo 'value'")
        compiled_src, src_str = _compile_jmespath(inner["source"], context="equals.source")
        return CompiledPredicate(
            kind="equals",
            compiled_source=compiled_src,
            source_str=src_str,
            literal=inner["value"],
        )

    if kind_str == "in":
        inner = spec["in"]
        if not isinstance(inner, Mapping):
            raise MappingDefinitionError(
                "'when.in' deve ser um objeto com 'source' e 'values'"
            )
        if "source" not in inner:
            raise MappingDefinitionError("'when.in' requer campo 'source'")
        if "values" not in inner:
            raise MappingDefinitionError("'when.in' requer campo 'values'")
        values_raw = inner["values"]
        if not isinstance(values_raw, (list, tuple)):
            raise MappingDefinitionError(
                f"'when.in.values' deve ser uma lista, recebeu {type(values_raw).__name__}"
            )
        compiled_src, src_str = _compile_jmespath(inner["source"], context="in.source")
        return CompiledPredicate(
            kind="in",
            compiled_source=compiled_src,
            source_str=src_str,
            literal=tuple(values_raw),  # immutable for frozen dataclass
        )

    # kind_str == "not"
    child_spec = spec["not"]
    child = compile_predicate(child_spec)  # recursive; nesting allowed
    return CompiledPredicate(kind="not", child=child)


def evaluate_predicate(
    pred: CompiledPredicate,
    data: Mapping[str, Any],
) -> bool:
    """Evaluate a compiled predicate against ``data``.

    Never raises at runtime.  JMESPath misses (None) are treated as
    falsy for ``exists``, ``equals``, and ``in``.

    Args:
        pred: The compiled predicate.
        data: Root mapping (``raw`` or ``extracted``, per rule.source_root).

    Returns:
        True if the predicate is satisfied, False otherwise.
    """
    if pred.kind == "exists":
        # None = missing path = does not exist.  Empty [] or {} = exists.
        result = _safe_search(pred.compiled_source, data)
        return result is not None

    if pred.kind == "equals":
        # Type-strict: int 3 != str "3".
        result = _safe_search(pred.compiled_source, data)
        return result == pred.literal

    if pred.kind == "in":
        # Type-strict membership.
        result = _safe_search(pred.compiled_source, data)
        return result in pred.literal  # type: ignore[operator]

    # kind == "not"
    assert pred.child is not None  # guaranteed by compile_predicate
    return not evaluate_predicate(pred.child, data)


def collect_predicate_source_strs(pred: CompiledPredicate) -> tuple[str, ...]:
    """Return all JMESPath source strings in a predicate tree (flat list).

    Used for drift detection: these paths are added to ``consumed_paths``
    so the drift detector does not flag them as unmapped.

    The returned tuple preserves depth-first order but the exact order
    does not matter — it is only used for set membership.
    """
    if pred.kind == "not":
        assert pred.child is not None
        return collect_predicate_source_strs(pred.child)

    # exists / equals / in — all have a single source_str
    if pred.source_str is not None:
        return (pred.source_str,)
    return ()


# ── Internal helpers ───────────────────────────────────────────────────


def _compile_jmespath(
    expr: Any,
    *,
    context: str,
) -> tuple[ParsedResult, str]:
    """Validate and compile a JMESPath expression string.

    Args:
        expr: Raw value from the DSL (expected str).
        context: Human-readable location string for error messages.

    Returns:
        ``(compiled_expr, expr_str)`` tuple.

    Raises:
        MappingDefinitionError: If ``expr`` is not a non-empty string or
            has invalid JMESPath syntax.
    """
    if not isinstance(expr, str) or not expr.strip():
        raise MappingDefinitionError(
            f"'when.{context}' deve ser string JMESPath não-vazia, "
            f"recebeu {type(expr).__name__!r}"
        )
    try:
        return compile_source(expr), expr
    except Exception as exc:
        raise MappingDefinitionError(
            f"'when.{context}': JMESPath inválido {expr!r}: {exc}"
        ) from exc


def _safe_search(
    compiled: Optional[ParsedResult],
    data: Mapping[str, Any],
) -> Any:
    """Execute a compiled JMESPath search, swallowing any runtime errors.

    JMESPath itself should not raise on well-formed data, but we guard
    defensively so predicate evaluation never propagates an exception.

    Returns:
        The search result, or None on any error.
    """
    if compiled is None:
        return None
    try:
        return compiled.search(data)
    except Exception:  # pragma: no cover — JMESPath is very stable
        return None
