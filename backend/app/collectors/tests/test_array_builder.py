"""Tests for array_builder rule kind (Fase 3.1).

Covers:
- compile_array_builder_rule: validation, rejection, structure.
- apply_array_builder: explode, skip_null, dedup_by, source_root routing.
"""

from __future__ import annotations

from typing import Any

import pytest

from backend.app.collectors.normalize.array_builder import (
    CompiledArrayBuilderItem,
    CompiledArrayBuilderRule,
    apply_array_builder,
    compile_array_builder_rule,
)
from backend.app.collectors.normalize.exceptions import MappingDefinitionError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rule(
    *,
    target: str = "normalized.observables",
    items: list[dict[str, Any]] | None = None,
    skip_null: bool | None = None,
    dedup_by: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build a minimal rule dict for compile_array_builder_rule."""
    d: dict[str, Any] = {
        "target": target,
        "kind": "array_builder",
        "items": items if items is not None else [],
    }
    if skip_null is not None:
        d["skip_null"] = skip_null
    if dedup_by is not None:
        d["dedup_by"] = dedup_by
    d.update(extra)
    return d


def _item(
    *,
    name: str = "src_ip",
    type: str = "IP Address",
    type_id: int = 2,
    source: str = "clientIp",
    explode: bool | None = None,
    skip_null: bool | None = None,
) -> dict[str, Any]:
    """Build a minimal item dict."""
    d: dict[str, Any] = {
        "name": name,
        "type": type,
        "type_id": type_id,
        "source": source,
    }
    if explode is not None:
        d["explode"] = explode
    if skip_null is not None:
        d["skip_null"] = skip_null
    return d


# ---------------------------------------------------------------------------
# Compilation tests
# ---------------------------------------------------------------------------


def test_compile_simple() -> None:
    """Minimal builder with 2 items, no explode, no dedup compiles cleanly."""
    rule = compile_array_builder_rule(
        _rule(
            items=[
                _item(name="src_ip", type="IP Address", type_id=2, source="clientIp"),
                _item(name="email_from", type="Email Address", type_id=5, source="mailFrom"),
            ]
        )
    )
    assert isinstance(rule, CompiledArrayBuilderRule)
    assert rule.target_path == ("normalized", "observables")
    assert rule.target_str == "normalized.observables"
    assert len(rule.items) == 2
    assert rule.items[0].name == "src_ip"
    assert rule.items[0].type == "IP Address"
    assert rule.items[0].type_id == 2
    assert rule.items[0].source_str == "clientIp"
    assert rule.items[0].source_root == "raw"
    assert rule.items[0].explode is False
    assert rule.items[1].name == "email_from"
    assert rule.dedup_by == ()


def test_compile_empty_items_is_valid() -> None:
    """Empty items list compiles successfully (produces empty array at apply time)."""
    rule = compile_array_builder_rule(_rule(items=[]))
    assert rule.items == ()


def test_compile_rejects_required_in_item() -> None:
    """required: true on an item raises MappingDefinitionError."""
    item = {**_item(), "required": True}
    with pytest.raises(MappingDefinitionError, match="required"):
        compile_array_builder_rule(_rule(items=[item]))


def test_compile_rejects_unknown_top_field() -> None:
    """Unknown field at the rule level raises MappingDefinitionError."""
    with pytest.raises(MappingDefinitionError, match="desconhecidos"):
        compile_array_builder_rule(_rule(totally_unknown_field=True))


def test_compile_rejects_scalar_fields_with_kind_array_builder_source() -> None:
    """'source' at the rule level raises MappingDefinitionError."""
    with pytest.raises(MappingDefinitionError, match="não permitidos"):
        compile_array_builder_rule({
            "target": "normalized.observables",
            "kind": "array_builder",
            "items": [],
            "source": "clientIp",
        })


@pytest.mark.parametrize("forbidden_field", [
    "const",
    "value_map",
    "default",
    "type_cast",
    "pre_cast",
    "fallback_source",
    "when",
])
def test_compile_rejects_scalar_fields_with_kind_array_builder(forbidden_field: str) -> None:
    """All scalar-rule fields are rejected at the array_builder rule level."""
    rule_dict = {
        "target": "normalized.observables",
        "kind": "array_builder",
        "items": [],
        forbidden_field: "anything",
    }
    with pytest.raises(MappingDefinitionError, match="não permitidos"):
        compile_array_builder_rule(rule_dict)


def test_compile_rejects_empty_item_name() -> None:
    """Missing or empty 'name' in item raises MappingDefinitionError."""
    with pytest.raises(MappingDefinitionError, match="name"):
        compile_array_builder_rule(_rule(items=[{
            "name": "",
            "type": "IP Address",
            "type_id": 2,
            "source": "clientIp",
        }]))


def test_compile_rejects_missing_item_type() -> None:
    """Missing 'type' in item raises MappingDefinitionError."""
    with pytest.raises(MappingDefinitionError, match="ausentes"):
        compile_array_builder_rule(_rule(items=[{
            "name": "src_ip",
            "type_id": 2,
            "source": "clientIp",
        }]))


def test_compile_rejects_missing_item_type_id() -> None:
    """Missing 'type_id' in item raises MappingDefinitionError."""
    with pytest.raises(MappingDefinitionError, match="ausentes"):
        compile_array_builder_rule(_rule(items=[{
            "name": "src_ip",
            "type": "IP Address",
            "source": "clientIp",
        }]))


def test_compile_rejects_missing_item_source() -> None:
    """Missing 'source' in item raises MappingDefinitionError."""
    with pytest.raises(MappingDefinitionError, match="ausentes"):
        compile_array_builder_rule(_rule(items=[{
            "name": "src_ip",
            "type": "IP Address",
            "type_id": 2,
        }]))


def test_compile_rejects_invalid_jmespath_in_item() -> None:
    """Bad JMESPath in item raises MappingDefinitionError with item index."""
    with pytest.raises(MappingDefinitionError, match=r"items\[0\]"):
        compile_array_builder_rule(_rule(items=[
            _item(source="[[[invalid_jmespath"),
        ]))


def test_compile_item_index_in_error_for_second_item() -> None:
    """JMESPath error in the second item (idx=1) includes index in message."""
    with pytest.raises(MappingDefinitionError, match=r"items\[1\]"):
        compile_array_builder_rule(_rule(items=[
            _item(name="ok", source="clientIp"),
            _item(name="bad", source="[[[invalid"),
        ]))


def test_compile_source_root_extracted_for_underscore_prefix() -> None:
    """Item with _ prefix source gets source_root='extracted'."""
    rule = compile_array_builder_rule(_rule(items=[
        _item(source="_processed.parsedAlert.fields.clientIp"),
    ]))
    assert rule.items[0].source_root == "extracted"


def test_compile_source_root_raw_for_no_underscore() -> None:
    """Item without _ prefix source gets source_root='raw'."""
    rule = compile_array_builder_rule(_rule(items=[_item(source="clientIp")]))
    assert rule.items[0].source_root == "raw"


def test_compile_dedup_by_stored() -> None:
    """dedup_by is compiled to a tuple and stored."""
    rule = compile_array_builder_rule(_rule(items=[], dedup_by=["value"]))
    assert rule.dedup_by == ("value",)


def test_compile_dedup_by_multi_key() -> None:
    rule = compile_array_builder_rule(_rule(items=[], dedup_by=["name", "value"]))
    assert rule.dedup_by == ("name", "value")


def test_compile_skip_null_inherits_rule_level_default() -> None:
    """Item without skip_null inherits the rule-level default (True)."""
    rule = compile_array_builder_rule(_rule(items=[_item()], skip_null=True))
    assert rule.items[0].skip_null is True


def test_compile_skip_null_item_overrides_rule_level() -> None:
    """Item-level skip_null=False overrides rule-level skip_null=True."""
    rule = compile_array_builder_rule(_rule(
        items=[_item(skip_null=False)],
        skip_null=True,
    ))
    assert rule.items[0].skip_null is False


def test_compile_rejects_type_id_as_string() -> None:
    """type_id must be int; string raises MappingDefinitionError."""
    with pytest.raises(MappingDefinitionError, match="type_id"):
        compile_array_builder_rule(_rule(items=[{
            "name": "src_ip",
            "type": "IP Address",
            "type_id": "2",  # wrong type
            "source": "clientIp",
        }]))


def test_compile_rejects_unknown_item_field() -> None:
    """Unknown field inside an item raises MappingDefinitionError."""
    with pytest.raises(MappingDefinitionError, match="desconhecidos"):
        compile_array_builder_rule(_rule(items=[{
            "name": "src_ip",
            "type": "IP Address",
            "type_id": 2,
            "source": "clientIp",
            "unknown_field": True,
        }]))


# ---------------------------------------------------------------------------
# Application tests
# ---------------------------------------------------------------------------


def _compile(
    items: list[dict[str, Any]],
    *,
    skip_null: bool = True,
    dedup_by: list[str] | None = None,
    target: str = "normalized.observables",
) -> CompiledArrayBuilderRule:
    return compile_array_builder_rule(_rule(
        target=target,
        items=items,
        skip_null=skip_null,
        dedup_by=dedup_by or [],
    ))


def test_apply_simple_two_items() -> None:
    """Two items with raw sources produce 2 observables."""
    rule = _compile([
        _item(name="src_ip", type="IP Address", type_id=2, source="clientIp"),
        _item(name="email_from", type="Email Address", type_id=5, source="mailFrom"),
    ])
    raw = {"clientIp": "198.51.100.1", "mailFrom": "sender@example.com"}
    result = apply_array_builder(rule, raw, {})
    assert result == [
        {"name": "src_ip", "type": "IP Address", "type_id": 2, "value": "198.51.100.1"},
        {"name": "email_from", "type": "Email Address", "type_id": 5, "value": "sender@example.com"},
    ]


def test_apply_empty_items_produces_empty_list() -> None:
    """No items → always returns empty list."""
    rule = _compile([])
    result = apply_array_builder(rule, {"anything": "value"}, {})
    assert result == []


def test_apply_explode_array() -> None:
    """explode=True with a list source yields one observable per element."""
    rule = _compile([
        _item(
            name="email_to",
            type="Email Address",
            type_id=5,
            source="envelopeRecipients",
            explode=True,
        ),
    ])
    raw = {"envelopeRecipients": ["alice@example.com", "bob@example.org"]}
    result = apply_array_builder(rule, raw, {})
    assert result == [
        {"name": "email_to", "type": "Email Address", "type_id": 5, "value": "alice@example.com"},
        {"name": "email_to", "type": "Email Address", "type_id": 5, "value": "bob@example.org"},
    ]


def test_apply_explode_null_skipped() -> None:
    """Null source with explode=True produces no observables (silent skip)."""
    rule = _compile([
        _item(name="email_to", type="Email Address", type_id=5, source="missing_field", explode=True),
    ])
    result = apply_array_builder(rule, {}, {})
    assert result == []


def test_apply_explode_scalar_wraps() -> None:
    """Scalar source with explode=True wraps to single-element list → 1 observable."""
    rule = _compile([
        _item(
            name="src_ip",
            type="IP Address",
            type_id=2,
            source="clientIp",
            explode=True,
        ),
    ])
    raw = {"clientIp": "198.51.100.1"}
    result = apply_array_builder(rule, raw, {})
    assert result == [
        {"name": "src_ip", "type": "IP Address", "type_id": 2, "value": "198.51.100.1"},
    ]


def test_apply_skip_null_at_item_level() -> None:
    """Item with null source and skip_null=True is not in output."""
    rule = _compile([
        _item(name="src_ip", type="IP Address", type_id=2, source="missing", skip_null=True),
    ])
    result = apply_array_builder(rule, {}, {})
    assert result == []


def test_apply_skip_null_false_at_item_level_includes_null() -> None:
    """Item with null source and skip_null=False produces observable with value=None."""
    rule = _compile([
        _item(name="src_ip", type="IP Address", type_id=2, source="missing", skip_null=False),
    ])
    result = apply_array_builder(rule, {}, {})
    assert result == [
        {"name": "src_ip", "type": "IP Address", "type_id": 2, "value": None},
    ]


def test_apply_skip_null_inherits_rule_level() -> None:
    """Item without skip_null follows rule-level default (True → skip nulls)."""
    # Rule-level skip_null=True; item has no skip_null → inherits True.
    rule = _compile(
        [_item(name="src_ip", source="missing")],
        skip_null=True,
    )
    result = apply_array_builder(rule, {}, {})
    assert result == []


def test_apply_skip_null_rule_level_false_includes_null() -> None:
    """Rule-level skip_null=False; item without skip_null includes null observable."""
    rule = _compile(
        [_item(name="src_ip", source="missing")],
        skip_null=False,
    )
    result = apply_array_builder(rule, {}, {})
    assert result == [
        {"name": "src_ip", "type": "IP Address", "type_id": 2, "value": None},
    ]


def test_apply_dedup_by_value() -> None:
    """Duplicate values are deduped (first-wins) when dedup_by=['value']."""
    rule = _compile(
        [
            _item(name="src_ip", source="ip1"),
            _item(name="src_ip", source="ip2"),
            _item(name="src_ip", source="ip3"),
        ],
        dedup_by=["value"],
    )
    raw = {"ip1": "1.2.3.4", "ip2": "1.2.3.4", "ip3": "5.6.7.8"}
    result = apply_array_builder(rule, raw, {})
    assert result == [
        {"name": "src_ip", "type": "IP Address", "type_id": 2, "value": "1.2.3.4"},
        {"name": "src_ip", "type": "IP Address", "type_id": 2, "value": "5.6.7.8"},
    ]


def test_apply_dedup_multi_key() -> None:
    """dedup_by=['name', 'value'] deduplicates on composite key."""
    rule = _compile(
        [
            _item(name="src_ip", source="ip1"),
            _item(name="dst_ip", source="ip2"),
            _item(name="src_ip", source="ip3"),
        ],
        dedup_by=["name", "value"],
    )
    # ip1 and ip3 resolve same IP but different name → both kept.
    # ip1 and ip3 same name+value → second dropped.
    raw = {"ip1": "1.2.3.4", "ip2": "9.9.9.9", "ip3": "1.2.3.4"}
    result = apply_array_builder(rule, raw, {})
    assert result == [
        {"name": "src_ip", "type": "IP Address", "type_id": 2, "value": "1.2.3.4"},
        {"name": "dst_ip", "type": "IP Address", "type_id": 2, "value": "9.9.9.9"},
    ]


def test_apply_extracted_source_root() -> None:
    """Items with _ prefix sources resolve from extracted (not raw)."""
    rule = _compile([
        _item(
            name="src_ip",
            type="IP Address",
            type_id=2,
            source="_processed.parsedAlert.fields.clientIp",
        ),
    ])
    extracted = {"_processed": {"parsedAlert": {"fields": {"clientIp": "203.0.113.5"}}}}
    result = apply_array_builder(rule, {}, extracted)
    assert result == [
        {"name": "src_ip", "type": "IP Address", "type_id": 2, "value": "203.0.113.5"},
    ]


def test_apply_mixed_roots() -> None:
    """Some items from raw, some from extracted — both resolve correctly."""
    rule = compile_array_builder_rule(_rule(items=[
        {
            "name": "raw_ip",
            "type": "IP Address",
            "type_id": 2,
            "source": "clientIp",
        },
        {
            "name": "extracted_email",
            "type": "Email Address",
            "type_id": 5,
            "source": "_processed.fields.mailFrom",
        },
    ]))
    raw = {"clientIp": "192.0.2.1"}
    extracted = {"_processed": {"fields": {"mailFrom": "user@example.com"}}}
    result = apply_array_builder(rule, raw, extracted)
    assert result == [
        {"name": "raw_ip", "type": "IP Address", "type_id": 2, "value": "192.0.2.1"},
        {"name": "extracted_email", "type": "Email Address", "type_id": 5, "value": "user@example.com"},
    ]


def test_apply_explode_with_null_elements_skip_null_true() -> None:
    """Explode list containing None elements with skip_null=True skips those elements."""
    rule = _compile([
        _item(
            name="hashes",
            type="Hash",
            type_id=8,
            source="hashes",
            explode=True,
            skip_null=True,
        ),
    ])
    raw = {"hashes": ["abc123", None, "def456"]}
    result = apply_array_builder(rule, raw, {})
    assert result == [
        {"name": "hashes", "type": "Hash", "type_id": 8, "value": "abc123"},
        {"name": "hashes", "type": "Hash", "type_id": 8, "value": "def456"},
    ]


def test_apply_explode_with_null_elements_skip_null_false() -> None:
    """Explode list with None elements and skip_null=False includes null observables."""
    rule = _compile([
        _item(
            name="hashes",
            type="Hash",
            type_id=8,
            source="hashes",
            explode=True,
            skip_null=False,
        ),
    ])
    raw = {"hashes": ["abc123", None]}
    result = apply_array_builder(rule, raw, {})
    assert result == [
        {"name": "hashes", "type": "Hash", "type_id": 8, "value": "abc123"},
        {"name": "hashes", "type": "Hash", "type_id": 8, "value": None},
    ]


def test_apply_jmespath_wildcard_in_item() -> None:
    """JMESPath wildcard expression in item source resolves correctly with explode."""
    rule = _compile([
        _item(
            name="file_hash",
            type="Hash",
            type_id=8,
            source="attachments[*].checksum",
            explode=True,
        ),
    ])
    raw = {
        "attachments": [
            {"checksum": "hash1"},
            {"checksum": "hash2"},
        ]
    }
    result = apply_array_builder(rule, raw, {})
    assert result == [
        {"name": "file_hash", "type": "Hash", "type_id": 8, "value": "hash1"},
        {"name": "file_hash", "type": "Hash", "type_id": 8, "value": "hash2"},
    ]


@pytest.mark.parametrize("source,raw,expected_count", [
    # Non-null scalar: 1 observable
    ("clientIp", {"clientIp": "1.2.3.4"}, 1),
    # Null: skip (skip_null=True default)
    ("clientIp", {}, 0),
    # List (no explode): 1 observable (the whole list as value)
    ("ips", {"ips": ["a", "b"]}, 1),
])
def test_apply_no_explode_parametrize(source: str, raw: dict, expected_count: int) -> None:
    """Parametrized: without explode, a source resolving to any value is 1 observable."""
    rule = _compile([_item(source=source, skip_null=True)])
    result = apply_array_builder(rule, raw, {})
    assert len(result) == expected_count
