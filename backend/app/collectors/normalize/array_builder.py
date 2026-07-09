"""array_builder rule kind for DSL v2.

Builds arrays of OCSF observable dicts from multiple source fields.
This is the mechanism that populates ``normalized.observables`` with
IPs, emails, hashes, and other IOC values extracted from vendor payloads.

DSL shape (v2 only)::

    {
      "target": "normalized.observables",
      "kind": "array_builder",
      "items": [
        {
          "name": "src_ip",
          "type": "IP Address",
          "type_id": 2,
          "source": "_processed.parsedAlert.fields.clientIp"
        },
        {
          "name": "email_to",
          "type": "Email Address",
          "type_id": 5,
          "source": "_processed.parsedAlert.fields.envelopeRecipients",
          "explode": true
        },
        {
          "name": "file_hash",
          "type": "Hash",
          "type_id": 8,
          "source": "_processed.parsedAlert.fields.attachments[*].checksum",
          "explode": true,
          "skip_null": true
        }
      ],
      "skip_null": true,
      "dedup_by": ["value"]
    }

Item fields
-----------
- ``name``, ``type`` (str label), ``type_id`` (int OCSF code) — required.
- ``source`` — required, JMESPath expression; may target raw or ``_extracted``
  (determined by ``_`` prefix, same as scalar rules).
- ``explode`` (bool, default False) — if True and source resolves to a list,
  one observable is produced per element.  If source is null with explode,
  the item is silently skipped.  If source is scalar with explode, it is
  wrapped in a single-element list (one observable).
- ``skip_null`` (bool) — if True, items where source resolves to None are
  omitted.  If False, an observable with ``value: None`` IS produced.
  Item-level overrides rule-level ``skip_null``.

Rule-level fields
-----------------
- ``target`` — required.
- ``kind: "array_builder"`` — required.
- ``items`` — required list of item dicts.  Empty list is valid and produces
  an empty output array.
- ``skip_null`` (bool, default True) — inherited by items without their own
  ``skip_null``.
- ``dedup_by`` (list[str], default []) — field names within each observable
  dict to dedup on; first-wins.  Example: ``["value"]``.

Forbidden at rule level (raises ``MappingDefinitionError``):
  ``source``, ``const``, ``value_map``, ``default``, ``type_cast``,
  ``pre_cast``, ``fallback_source``, ``when``.

Forbidden in items (raises ``MappingDefinitionError``):
  ``required`` — items are pure pass-through; quarantine semantics do not
  apply to individual observables.

Note: ``kind: "array_builder"`` is a DSL v2-only feature.  The engine's
``_compile_v1`` path rejects any rule containing a ``kind`` field.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Mapping, Optional

import jmespath
from jmespath.parser import ParsedResult

from .exceptions import MappingDefinitionError

__all__ = [
    "CompiledArrayBuilderItem",
    "CompiledArrayBuilderRule",
    "compile_array_builder_rule",
    "apply_array_builder",
]

# Fields that are legal on a scalar rule but FORBIDDEN on array_builder rules.
_SCALAR_RULE_FIELDS_FORBIDDEN = frozenset({
    "source",
    "const",
    "value_map",
    "default",
    "type_cast",
    "pre_cast",
    "fallback_source",
    "when",
})

# Legal top-level fields for an array_builder rule.
_ARRAY_BUILDER_RULE_FIELDS = frozenset({
    "target",
    "kind",
    "items",
    "skip_null",
    "dedup_by",
})

# Required fields in each item.
_ITEM_REQUIRED_FIELDS = frozenset({"name", "type", "type_id", "source"})

# Legal fields in each item.
_ITEM_LEGAL_FIELDS = frozenset({
    "name",
    "type",
    "type_id",
    "source",
    "explode",
    "skip_null",
})


@dataclass(frozen=True)
class CompiledArrayBuilderItem:
    """Compile-time representation of one item in an array_builder rule.

    Attributes:
        name: Observable name label (e.g. "src_ip").
        type: OCSF type label string (e.g. "IP Address").
        type_id: OCSF type_id integer (e.g. 2 for IP Address).
        compiled_source: JMESPath expression compiled from ``source``.
        source_str: Original ``source`` string (for drift detection).
        source_root: Where to resolve the JMESPath — ``"raw"`` for fields
            that do not start with ``_``, ``"extracted"`` for ``_`` prefixed
            fields (populated by preprocess).
        explode: If True, a list source yields one observable per element.
        skip_null: If True, null resolved values produce no observable.
            If False, an observable with ``value: None`` is produced.
    """

    name: str
    type: str
    type_id: int
    compiled_source: ParsedResult
    source_str: str
    source_root: Literal["raw", "extracted"]
    explode: bool
    skip_null: bool


@dataclass(frozen=True)
class CompiledArrayBuilderRule:
    """Compile-time representation of an array_builder rule.

    Attributes:
        target_path: Tuple of path segments from ``target`` (e.g.
            ``("normalized", "observables")``).
        target_str: Original ``target`` string.
        items: Tuple of compiled items, in declaration order.
        dedup_by: Tuple of observable dict field names used for
            deduplication (first-wins).  Empty tuple means no dedup.
    """

    target_path: tuple[str, ...]
    target_str: str
    items: tuple[CompiledArrayBuilderItem, ...]
    dedup_by: tuple[str, ...]


# ---------------------------------------------------------------------------
# Compilation
# ---------------------------------------------------------------------------


def _validate_target_path(target: Any) -> tuple[str, ...]:
    """Validate ``target`` and return its dot-split tuple."""
    if not isinstance(target, str) or not target.strip():
        raise MappingDefinitionError(
            "array_builder rule: 'target' é obrigatório e deve ser string não-vazia"
        )
    parts = tuple(p for p in target.split(".") if p)
    if not parts:
        raise MappingDefinitionError(
            f"array_builder rule: target inválido: {target!r}"
        )
    return parts


def _compile_item(idx: int, item_dict: Any, *, rule_skip_null: bool) -> CompiledArrayBuilderItem:
    """Validate and compile a single item dict.

    Args:
        idx: Zero-based index in the items list (for error messages).
        item_dict: Raw dict from DSL.
        rule_skip_null: Rule-level skip_null default (used when item
            does not specify its own ``skip_null``).

    Returns:
        A :class:`CompiledArrayBuilderItem`.

    Raises:
        MappingDefinitionError: On any structural or JMESPath error.
    """
    if not isinstance(item_dict, Mapping):
        raise MappingDefinitionError(
            f"array_builder items[{idx}]: deve ser um objeto, "
            f"recebeu {type(item_dict).__name__}"
        )

    # Reject ``required`` — not applicable to items.
    if "required" in item_dict:
        raise MappingDefinitionError(
            f"array_builder items[{idx}]: campo 'required' não é permitido em items. "
            "Items são pass-through; use skip_null para controlar o comportamento "
            "quando o source resolve None."
        )

    # Reject unknown fields.
    unknown = set(item_dict.keys()) - _ITEM_LEGAL_FIELDS
    if unknown:
        raise MappingDefinitionError(
            f"array_builder items[{idx}]: campos desconhecidos {sorted(unknown)}. "
            f"Campos permitidos: {sorted(_ITEM_LEGAL_FIELDS)}"
        )

    # Validate required fields.
    missing = _ITEM_REQUIRED_FIELDS - set(item_dict.keys())
    if missing:
        raise MappingDefinitionError(
            f"array_builder items[{idx}]: campos obrigatórios ausentes: "
            f"{sorted(missing)}"
        )

    name = item_dict["name"]
    if not isinstance(name, str) or not name.strip():
        raise MappingDefinitionError(
            f"array_builder items[{idx}]: 'name' deve ser string não-vazia"
        )

    type_label = item_dict["type"]
    if not isinstance(type_label, str) or not type_label.strip():
        raise MappingDefinitionError(
            f"array_builder items[{idx}] ({name!r}): 'type' deve ser string não-vazia"
        )

    type_id = item_dict["type_id"]
    if not isinstance(type_id, int):
        raise MappingDefinitionError(
            f"array_builder items[{idx}] ({name!r}): 'type_id' deve ser int, "
            f"recebeu {type(type_id).__name__}"
        )

    source_raw = item_dict["source"]
    if not isinstance(source_raw, str) or not source_raw.strip():
        raise MappingDefinitionError(
            f"array_builder items[{idx}] ({name!r}): 'source' deve ser string "
            "JMESPath não-vazia"
        )

    try:
        compiled_source = jmespath.compile(source_raw)
    except Exception as exc:
        raise MappingDefinitionError(
            f"array_builder items[{idx}] ({name!r}): JMESPath inválido "
            f"{source_raw!r}: {exc}"
        ) from exc

    source_root: Literal["raw", "extracted"] = (
        "extracted" if source_raw.startswith("_") else "raw"
    )

    explode = bool(item_dict.get("explode", False))

    # Item-level skip_null overrides rule-level when explicitly set.
    if "skip_null" in item_dict:
        skip_null = bool(item_dict["skip_null"])
    else:
        skip_null = rule_skip_null

    return CompiledArrayBuilderItem(
        name=name,
        type=type_label,
        type_id=type_id,
        compiled_source=compiled_source,
        source_str=source_raw,
        source_root=source_root,
        explode=explode,
        skip_null=skip_null,
    )


def compile_array_builder_rule(rule_dict: Any) -> CompiledArrayBuilderRule:
    """Validate and compile an ``array_builder`` rule dict.

    Called by ``engine._compile_v2`` when ``rule.get("kind") == "array_builder"``.

    Args:
        rule_dict: Raw rule dict from the DSL ``rules`` list.

    Returns:
        A :class:`CompiledArrayBuilderRule` ready for :func:`apply_array_builder`.

    Raises:
        MappingDefinitionError: On any structural, type, or JMESPath error.
    """
    if not isinstance(rule_dict, Mapping):
        raise MappingDefinitionError(
            "array_builder rule: regra deve ser um objeto dict, "
            f"recebeu {type(rule_dict).__name__}"
        )

    target = rule_dict.get("target")
    target_path = _validate_target_path(target)

    # Reject scalar-rule fields at the rule level.
    forbidden_present = _SCALAR_RULE_FIELDS_FORBIDDEN & set(rule_dict.keys())
    if forbidden_present:
        raise MappingDefinitionError(
            f"array_builder rule {target!r}: campos não permitidos em "
            f"kind='array_builder': {sorted(forbidden_present)}. "
            "Use 'items' para configurar sources individuais."
        )

    # Reject unknown top-level fields.
    unknown_top = set(rule_dict.keys()) - _ARRAY_BUILDER_RULE_FIELDS
    if unknown_top:
        raise MappingDefinitionError(
            f"array_builder rule {target!r}: campos desconhecidos no topo: "
            f"{sorted(unknown_top)}. "
            f"Campos permitidos: {sorted(_ARRAY_BUILDER_RULE_FIELDS)}"
        )

    items_raw = rule_dict.get("items")
    if items_raw is None:
        raise MappingDefinitionError(
            f"array_builder rule {target!r}: 'items' é obrigatório"
        )
    if not isinstance(items_raw, (list, tuple)):
        raise MappingDefinitionError(
            f"array_builder rule {target!r}: 'items' deve ser uma lista, "
            f"recebeu {type(items_raw).__name__}"
        )

    # rule-level skip_null: default True (most conservative — skip nulls).
    rule_skip_null = bool(rule_dict.get("skip_null", True))

    # dedup_by: validate it's a list of strings.
    dedup_raw = rule_dict.get("dedup_by", [])
    if not isinstance(dedup_raw, (list, tuple)):
        raise MappingDefinitionError(
            f"array_builder rule {target!r}: 'dedup_by' deve ser uma lista de strings, "
            f"recebeu {type(dedup_raw).__name__}"
        )
    for d_idx, field_name in enumerate(dedup_raw):
        if not isinstance(field_name, str) or not field_name.strip():
            raise MappingDefinitionError(
                f"array_builder rule {target!r}: 'dedup_by[{d_idx}]' deve ser "
                "string não-vazia"
            )
    dedup_by = tuple(str(f) for f in dedup_raw)

    # Compile items. Empty items list is valid.
    compiled_items: List[CompiledArrayBuilderItem] = []
    for idx, item_dict in enumerate(items_raw):
        compiled_items.append(
            _compile_item(idx, item_dict, rule_skip_null=rule_skip_null)
        )

    return CompiledArrayBuilderRule(
        target_path=target_path,
        target_str=str(target),
        items=tuple(compiled_items),
        dedup_by=dedup_by,
    )


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


def apply_array_builder(
    rule: CompiledArrayBuilderRule,
    raw: Mapping[str, Any],
    extracted: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    """Execute an array_builder rule and return the observables list.

    For each item:
    1. Resolve ``source`` from ``raw`` or ``extracted`` per ``source_root``.
    2. If ``explode`` is True:
       - Null source → skip silently (no observables for this item).
       - List source → one observable per element (elements that are null
         and item.skip_null is True are individually skipped).
       - Scalar source → wrap in single-element list → one observable.
    3. If ``explode`` is False:
       - Null source and ``skip_null`` True → skip.
       - Otherwise → one observable with the resolved value (even if None).

    After all items are processed, apply ``dedup_by`` (first-wins).

    Args:
        rule: Compiled array_builder rule.
        raw: The original vendor payload dict.
        extracted: The preprocess-populated extracted namespace.

    Returns:
        List of observable dicts, each with keys:
        ``{"name": str, "type": str, "type_id": int, "value": Any}``.
    """
    result: List[Dict[str, Any]] = []

    for item in rule.items:
        root: Mapping[str, Any] = extracted if item.source_root == "extracted" else raw
        value = item.compiled_source.search(root)

        if item.explode:
            # Null source with explode → silently produce no observables.
            if value is None:
                continue

            # Scalar source with explode → treat as single-element list.
            if not isinstance(value, list):
                value = [value]

            for element in value:
                if element is None and item.skip_null:
                    continue
                result.append(
                    {
                        "name": item.name,
                        "type": item.type,
                        "type_id": item.type_id,
                        "value": element,
                    }
                )
        else:
            if value is None and item.skip_null:
                continue
            result.append(
                {
                    "name": item.name,
                    "type": item.type,
                    "type_id": item.type_id,
                    "value": value,
                }
            )

    if rule.dedup_by:
        seen: set[tuple[Any, ...]] = set()
        deduped: List[Dict[str, Any]] = []
        for obs in result:
            key = tuple(obs.get(k) for k in rule.dedup_by)
            if key not in seen:
                seen.add(key)
                deduped.append(obs)
        result = deduped

    return result
