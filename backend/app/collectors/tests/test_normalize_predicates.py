"""Tests for predicates.py — Fase 2.3 ``when`` predicate language."""

from __future__ import annotations

import pytest

from backend.app.collectors.normalize.engine import MappingDefinitionError
from backend.app.collectors.normalize.predicates import (
    CompiledPredicate,
    collect_predicate_source_strs,
    compile_predicate,
    evaluate_predicate,
)


# ── exists ─────────────────────────────────────────────────────────────


def test_exists_truthy_when_field_present() -> None:
    pred = compile_predicate({"exists": "mailFrom"})
    assert evaluate_predicate(pred, {"mailFrom": "sender@example.com"}) is True


def test_exists_falsy_when_field_absent() -> None:
    pred = compile_predicate({"exists": "mailFrom"})
    assert evaluate_predicate(pred, {"otherField": "value"}) is False


def test_exists_truthy_for_empty_list_dict() -> None:
    """Empty list [] and empty dict {} are NOT None — they exist."""
    pred_list = compile_predicate({"exists": "tags"})
    assert evaluate_predicate(pred_list, {"tags": []}) is True

    pred_dict = compile_predicate({"exists": "meta"})
    assert evaluate_predicate(pred_dict, {"meta": {}}) is True


def test_exists_falsy_for_null() -> None:
    """Explicit JSON null (Python None) is treated as not-exists."""
    pred = compile_predicate({"exists": "field"})
    assert evaluate_predicate(pred, {"field": None}) is False


def test_exists_nested_jmespath() -> None:
    pred = compile_predicate({"exists": "parsedAlert.fields.mailFrom"})
    data = {"parsedAlert": {"fields": {"mailFrom": "alice@example.com"}}}
    assert evaluate_predicate(pred, data) is True
    assert evaluate_predicate(pred, {"parsedAlert": {"fields": {}}}) is False


# ── equals ─────────────────────────────────────────────────────────────


def test_equals_strict_match() -> None:
    pred = compile_predicate({"equals": {"source": "status", "value": "active"}})
    assert evaluate_predicate(pred, {"status": "active"}) is True
    assert evaluate_predicate(pred, {"status": "inactive"}) is False


def test_equals_no_implicit_coercion() -> None:
    """Type-strict: int 3 does NOT equal str '3'."""
    pred_int = compile_predicate({"equals": {"source": "severity", "value": 3}})
    assert evaluate_predicate(pred_int, {"severity": 3}) is True
    assert evaluate_predicate(pred_int, {"severity": "3"}) is False

    pred_str = compile_predicate({"equals": {"source": "severity", "value": "3"}})
    assert evaluate_predicate(pred_str, {"severity": 3}) is False
    assert evaluate_predicate(pred_str, {"severity": "3"}) is True


def test_equals_missing_field_returns_false() -> None:
    pred = compile_predicate({"equals": {"source": "missing", "value": "x"}})
    assert evaluate_predicate(pred, {}) is False


def test_equals_null_value_matches_null() -> None:
    """equals with value: null matches None only if the field is explicitly null."""
    pred = compile_predicate({"equals": {"source": "field", "value": None}})
    assert evaluate_predicate(pred, {"field": None}) is True
    assert evaluate_predicate(pred, {}) is True  # missing path → None == None
    assert evaluate_predicate(pred, {"field": "x"}) is False


# ── in ─────────────────────────────────────────────────────────────────


def test_in_membership() -> None:
    pred = compile_predicate({"in": {"source": "type", "values": ["alert", "incident", "case"]}})
    assert evaluate_predicate(pred, {"type": "alert"}) is True
    assert evaluate_predicate(pred, {"type": "incident"}) is True
    assert evaluate_predicate(pred, {"type": "unknown"}) is False


def test_in_type_strict() -> None:
    """in is also type-strict: int 1 not in ['1', '2']."""
    pred = compile_predicate({"in": {"source": "code", "values": ["1", "2"]}})
    assert evaluate_predicate(pred, {"code": "1"}) is True
    assert evaluate_predicate(pred, {"code": 1}) is False


def test_in_missing_field_returns_false() -> None:
    pred = compile_predicate({"in": {"source": "absent", "values": ["a", "b"]}})
    assert evaluate_predicate(pred, {}) is False


def test_in_stores_values_as_tuple() -> None:
    """Compiled predicate stores values as immutable tuple."""
    pred = compile_predicate({"in": {"source": "x", "values": [1, 2, 3]}})
    assert isinstance(pred.literal, tuple)
    assert pred.literal == (1, 2, 3)


# ── not ────────────────────────────────────────────────────────────────


def test_not_negation() -> None:
    pred = compile_predicate({"not": {"exists": "mailFrom"}})
    assert evaluate_predicate(pred, {}) is True                           # mailFrom absent → not absent = True
    assert evaluate_predicate(pred, {"mailFrom": "x@example.com"}) is False  # mailFrom present → not present = False


def test_not_wraps_equals() -> None:
    pred = compile_predicate({"not": {"equals": {"source": "status", "value": "closed"}}})
    assert evaluate_predicate(pred, {"status": "open"}) is True
    assert evaluate_predicate(pred, {"status": "closed"}) is False


# ── nested predicates ──────────────────────────────────────────────────


def test_nested_not_in_exists() -> None:
    """Double negation: not(not(exists(x))) == exists(x)."""
    pred = compile_predicate({"not": {"not": {"exists": "x"}}})
    assert evaluate_predicate(pred, {"x": "val"}) is True
    assert evaluate_predicate(pred, {}) is False


def test_nested_not_in_in() -> None:
    """not(in([...])): true when value is NOT in list."""
    pred = compile_predicate({"not": {"in": {"source": "status", "values": ["closed", "resolved"]}}})
    assert evaluate_predicate(pred, {"status": "open"}) is True
    assert evaluate_predicate(pred, {"status": "closed"}) is False


# ── compile-time validation ────────────────────────────────────────────


def test_compile_rejects_no_keys() -> None:
    with pytest.raises(MappingDefinitionError, match="nenhuma chave"):
        compile_predicate({})


def test_compile_rejects_multiple_keys() -> None:
    with pytest.raises(MappingDefinitionError, match="múltiplas chaves"):
        compile_predicate({"exists": "x", "not": {"exists": "y"}})


def test_compile_rejects_non_dict() -> None:
    with pytest.raises(MappingDefinitionError, match="dict"):
        compile_predicate("exists:x")  # type: ignore[arg-type]


def test_compile_rejects_invalid_jmespath() -> None:
    with pytest.raises(MappingDefinitionError, match="JMESPath inválido"):
        compile_predicate({"exists": "[[[invalid"})


def test_compile_rejects_invalid_jmespath_in_equals() -> None:
    with pytest.raises(MappingDefinitionError, match="JMESPath inválido"):
        compile_predicate({"equals": {"source": "[[[invalid", "value": "x"}})


def test_compile_rejects_equals_missing_source() -> None:
    with pytest.raises(MappingDefinitionError, match="'source'"):
        compile_predicate({"equals": {"value": "x"}})


def test_compile_rejects_equals_missing_value() -> None:
    with pytest.raises(MappingDefinitionError, match="'value'"):
        compile_predicate({"equals": {"source": "x"}})


def test_compile_rejects_equals_non_dict_inner() -> None:
    with pytest.raises(MappingDefinitionError, match="objeto"):
        compile_predicate({"equals": "not_a_dict"})


def test_compile_rejects_in_missing_values() -> None:
    with pytest.raises(MappingDefinitionError, match="'values'"):
        compile_predicate({"in": {"source": "x"}})


def test_compile_rejects_in_values_not_list() -> None:
    with pytest.raises(MappingDefinitionError, match="lista"):
        compile_predicate({"in": {"source": "x", "values": "not_a_list"}})


# ── evaluate_predicate runtime safety ─────────────────────────────────


def test_evaluate_handles_missing_path_gracefully() -> None:
    """Deep path miss must not raise — returns false."""
    pred = compile_predicate({"exists": "a.b.c.d.e.f"})
    # None of the nested keys exist
    assert evaluate_predicate(pred, {"a": {"b": None}}) is False
    assert evaluate_predicate(pred, {}) is False


@pytest.mark.parametrize(
    "spec,data,expected",
    [
        ({"exists": "x"}, {"x": 0}, True),    # 0 is not None → exists
        ({"exists": "x"}, {"x": False}, True),  # False is not None → exists
        ({"exists": "x"}, {"x": ""}, True),   # "" is not None → exists
        ({"equals": {"source": "x", "value": 0}}, {"x": 0}, True),
        ({"equals": {"source": "x", "value": False}}, {"x": False}, True),
    ],
    ids=["exists_zero", "exists_false", "exists_empty_str", "equals_zero", "equals_false"],
)
def test_evaluate_edge_values(spec: dict, data: dict, expected: bool) -> None:
    """Falsy-but-not-None values are handled correctly."""
    pred = compile_predicate(spec)
    assert evaluate_predicate(pred, data) is expected


# ── collect_predicate_source_strs ──────────────────────────────────────


def test_collect_sources_exists() -> None:
    pred = compile_predicate({"exists": "mailFrom"})
    assert collect_predicate_source_strs(pred) == ("mailFrom",)


def test_collect_sources_equals() -> None:
    pred = compile_predicate({"equals": {"source": "status", "value": "active"}})
    assert collect_predicate_source_strs(pred) == ("status",)


def test_collect_sources_in() -> None:
    pred = compile_predicate({"in": {"source": "type", "values": ["a", "b"]}})
    assert collect_predicate_source_strs(pred) == ("type",)


def test_collect_sources_not_delegates_to_child() -> None:
    pred = compile_predicate({"not": {"exists": "x"}})
    assert collect_predicate_source_strs(pred) == ("x",)


def test_collect_sources_nested_not() -> None:
    pred = compile_predicate({"not": {"not": {"equals": {"source": "y", "value": 1}}}})
    assert collect_predicate_source_strs(pred) == ("y",)
