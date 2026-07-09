"""Testes da função pura compute_diff — sem precisar de router/DB."""

from __future__ import annotations

import pytest
from backend.app.routers.mappings import compute_diff, MappingVersionDiff


# ── Helpers ───────────────────────────────────────────────────────────


def _rule(target: str, **kwargs) -> dict:
    return {"target": target, **kwargs}


# ── Casos básicos ─────────────────────────────────────────────────────


def test_diff_empty_rules():
    diff = compute_diff([], [])
    assert diff.added == []
    assert diff.removed == []
    assert diff.modified == []
    assert diff.reordered_only is False


def test_diff_identical_rules():
    rules = [_rule("class_uid", const=2004), _rule("severity_id", source="sev")]
    diff = compute_diff(rules, rules)
    assert diff.added == []
    assert diff.removed == []
    assert diff.modified == []
    assert diff.reordered_only is False


def test_diff_added_rule():
    a = [_rule("class_uid", const=2004)]
    b = [_rule("class_uid", const=2004), _rule("severity_id", source="sev")]
    diff = compute_diff(a, b)
    assert len(diff.added) == 1
    assert diff.added[0].target == "severity_id"
    assert diff.removed == []
    assert diff.modified == []


def test_diff_removed_rule():
    a = [_rule("class_uid", const=2004), _rule("severity_id", source="sev")]
    b = [_rule("class_uid", const=2004)]
    diff = compute_diff(a, b)
    assert diff.added == []
    assert len(diff.removed) == 1
    assert diff.removed[0].target == "severity_id"
    assert diff.modified == []


def test_diff_modified_rule():
    a = [_rule("class_uid", const=2004)]
    b = [_rule("class_uid", const=2005)]
    diff = compute_diff(a, b)
    assert diff.added == []
    assert diff.removed == []
    assert len(diff.modified) == 1
    mod = diff.modified[0]
    assert mod.target == "class_uid"
    assert mod.before.const == 2004
    assert mod.after.const == 2005


def test_diff_reordered_only():
    a = [_rule("class_uid", const=2004), _rule("severity_id", source="sev")]
    b = [_rule("severity_id", source="sev"), _rule("class_uid", const=2004)]
    diff = compute_diff(a, b)
    assert diff.reordered_only is True
    assert diff.added == []
    assert diff.removed == []
    assert diff.modified == []


def test_diff_reordered_only_false_when_content_also_changed():
    a = [_rule("class_uid", const=2004), _rule("severity_id", source="sev")]
    b = [_rule("severity_id", source="sev"), _rule("class_uid", const=9999)]
    diff = compute_diff(a, b)
    assert diff.reordered_only is False
    assert len(diff.modified) == 1


def test_diff_combined_add_remove_modify():
    a = [
        _rule("class_uid", const=2004),
        _rule("to_be_removed", source="x"),
        _rule("to_be_modified", source="old"),
    ]
    b = [
        _rule("class_uid", const=2004),
        _rule("to_be_modified", source="new"),
        _rule("newly_added", source="y"),
    ]
    diff = compute_diff(a, b)
    added_targets = {r.target for r in diff.added}
    removed_targets = {r.target for r in diff.removed}
    modified_targets = {r.target for r in diff.modified}

    assert added_targets == {"newly_added"}
    assert removed_targets == {"to_be_removed"}
    assert modified_targets == {"to_be_modified"}
    assert diff.reordered_only is False


def test_diff_required_field_change():
    a = [_rule("severity_id", source="sev", required=False)]
    b = [_rule("severity_id", source="sev", required=True)]
    diff = compute_diff(a, b)
    assert len(diff.modified) == 1
    assert diff.modified[0].before.required is False
    assert diff.modified[0].after.required is True


def test_diff_preserves_metadata():
    diff = compute_diff(
        [],
        [],
        definition_id="def-123",
        version_a="va-1",
        version_b="vb-2",
        version_a_number=1,
        version_b_number=2,
    )
    assert diff.definition_id == "def-123"
    assert diff.version_a == "va-1"
    assert diff.version_b == "vb-2"
    assert diff.version_a_number == 1
    assert diff.version_b_number == 2


@pytest.mark.parametrize("const_a,const_b,should_modify", [
    (2004, 2004, False),
    (2004, 2005, True),
    (None, None, False),
    (None, "x", True),
    ("x", None, True),
])
def test_diff_const_comparison(const_a, const_b, should_modify):
    a = [_rule("class_uid", const=const_a)]
    b = [_rule("class_uid", const=const_b)]
    diff = compute_diff(a, b)
    assert bool(diff.modified) == should_modify


@pytest.mark.parametrize("value_map_a,value_map_b,should_modify", [
    ({"a": 1}, {"a": 1}, False),
    ({"a": 1}, {"a": 2}, True),
    (None, {"a": 1}, True),
    ({"a": 1}, None, True),
])
def test_diff_value_map_comparison(value_map_a, value_map_b, should_modify):
    a = [_rule("severity_id", value_map=value_map_a)]
    b = [_rule("severity_id", value_map=value_map_b)]
    diff = compute_diff(a, b)
    assert bool(diff.modified) == should_modify
