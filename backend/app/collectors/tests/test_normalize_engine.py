"""Engine de mapping — interpretador da DSL (v1 e v2)."""

from __future__ import annotations

import json

import pytest

from backend.app.collectors.normalize.engine import (
    CompiledRules,
    MappingDefinitionError,
    MappingEngine,
    MappingError,
    MappingRequiredFieldError,
    apply_compiled,
    compile_rules,
)


# ── Compilação v1 (legado) ─────────────────────────────────────────────


def test_compile_basic_rules() -> None:
    rules = [
        {"target": "normalized.severity_id", "source": "severity"},
        {"target": "normalized.class_uid", "const": 2004},
    ]
    compiled = compile_rules(rules)
    assert isinstance(compiled, CompiledRules)
    assert len(compiled.rules) == 2
    assert compiled.preprocess_ops == ()
    assert compiled.rules[0].target_path == ("normalized", "severity_id")
    assert compiled.rules[1].const == 2004
    assert compiled.rules[1].source is None


def test_compile_rejects_source_and_const_together() -> None:
    with pytest.raises(MappingDefinitionError):
        compile_rules([{"target": "normalized.x", "source": "x", "const": 1}])


def test_compile_rejects_missing_source_and_const() -> None:
    with pytest.raises(MappingDefinitionError):
        compile_rules([{"target": "normalized.x"}])


def test_compile_rejects_missing_target() -> None:
    with pytest.raises(MappingDefinitionError):
        compile_rules([{"source": "x"}])


def test_compile_rejects_invalid_jmespath() -> None:
    # `[.invalid` é sintaxe inválida pra JMESPath.
    with pytest.raises(MappingDefinitionError):
        compile_rules([{"target": "normalized.x", "source": "[invalid["}])


def test_compile_rejects_non_dict_value_map() -> None:
    with pytest.raises(MappingDefinitionError):
        compile_rules([{"target": "normalized.x", "source": "x", "value_map": "no"}])


# ── Compilação v1 — não regressão (task: test_compile_v1_list_unchanged) ──


def test_compile_v1_list_unchanged() -> None:
    """list input ainda funciona identicamente — sem regressão."""
    rules = [
        {"target": "normalized.id", "source": "id"},
        {"target": "normalized.class_uid", "const": 2004},
    ]
    compiled = compile_rules(rules, dsl_version=1)
    assert isinstance(compiled, CompiledRules)
    assert compiled.preprocess_ops == ()
    assert len(compiled.rules) == 2
    assert compiled.rules[0].source_root == "raw"
    assert compiled.rules[1].const == 2004


# ── Compilação v2 ─────────────────────────────────────────────────────


def test_compile_v2_dict_with_preprocess() -> None:
    """Dict v2 completo compila: preprocess + rules."""
    payload = {
        "preprocess": [
            {"op": "json_parse", "source": "processedData", "target": "_processed", "tolerant": True},
        ],
        "rules": [
            {"target": "normalized.id", "source": "id"},
            {"target": "normalized.class_uid", "const": 2004},
        ],
    }
    compiled = compile_rules(payload, dsl_version=2)
    assert isinstance(compiled, CompiledRules)
    assert len(compiled.preprocess_ops) == 1
    assert compiled.preprocess_ops[0].op == "json_parse"
    assert compiled.preprocess_ops[0].target == "_processed"
    assert compiled.preprocess_ops[0].tolerant is True
    assert len(compiled.rules) == 2


def test_compile_v2_without_preprocess() -> None:
    """Dict v2 sem bloco preprocess (preprocess é opcional)."""
    payload = {
        "rules": [
            {"target": "normalized.id", "source": "id"},
        ],
    }
    compiled = compile_rules(payload, dsl_version=2)
    assert compiled.preprocess_ops == ()
    assert len(compiled.rules) == 1


def test_compile_v2_rejects_target_starting_with_underscore() -> None:
    """DSL v2 safeguard: rules block target nao pode comecar com _."""
    payload = {
        "rules": [
            {"target": "_reserved.field", "source": "x"},
        ],
    }
    with pytest.raises(MappingDefinitionError, match="_reserved"):
        compile_rules(payload, dsl_version=2)


def test_compile_v2_rejects_preprocess_target_not_starting_with_underscore() -> None:
    """Simetria: preprocess target DEVE comecar com _."""
    payload = {
        "preprocess": [
            {"op": "json_parse", "source": "processedData", "target": "processed"},
        ],
        "rules": [
            {"target": "normalized.id", "source": "id"},
        ],
    }
    with pytest.raises(MappingDefinitionError, match="'_'"):
        compile_rules(payload, dsl_version=2)


def test_compile_v2_rejects_unknown_preprocess_op() -> None:
    payload = {
        "preprocess": [
            {"op": "no_such_op", "source": "x", "target": "_out"},
        ],
        "rules": [
            {"target": "normalized.id", "source": "id"},
        ],
    }
    with pytest.raises(MappingDefinitionError, match="no_such_op"):
        compile_rules(payload, dsl_version=2)


def test_compile_v1_dict_raises() -> None:
    """DSL v1 com dict levanta MappingDefinitionError."""
    with pytest.raises(MappingDefinitionError, match="got dict"):
        compile_rules({"rules": [{"target": "normalized.id", "source": "id"}]}, dsl_version=1)


def test_compile_v2_list_raises() -> None:
    """DSL v2 com list levanta MappingDefinitionError."""
    with pytest.raises(MappingDefinitionError, match="dict"):
        compile_rules([{"target": "normalized.id", "source": "id"}], dsl_version=2)


def test_compile_unknown_dsl_version_raises() -> None:
    with pytest.raises(MappingDefinitionError, match="não suportada"):
        compile_rules([], dsl_version=99)


# ── Aplicação ─────────────────────────────────────────────────────────


def _apply(rules: list[dict], raw: dict) -> dict:
    compiled = compile_rules(rules)
    return apply_compiled(compiled, raw).output


def test_apply_simple_source() -> None:
    out = _apply(
        [{"target": "normalized.id", "source": "id"}],
        {"id": "alert-1", "extra": "ignored"},
    )
    assert out == {"normalized": {"id": "alert-1"}}


def test_apply_const() -> None:
    out = _apply(
        [{"target": "normalized.class_uid", "const": 2004}],
        {},
    )
    assert out == {"normalized": {"class_uid": 2004}}


def test_apply_value_map_then_type_cast() -> None:
    rules = [
        {
            "target": "normalized.severity_id",
            "source": "severity",
            "value_map": {
                "critical": 5,
                "high": 4,
                "medium": 3,
                "low": 2,
                "info": 1,
            },
        }
    ]
    assert _apply(rules, {"severity": "high"}) == {"normalized": {"severity_id": 4}}
    assert _apply(rules, {"severity": "Critical"}) == {"normalized": {"severity_id": 5}}


def test_apply_default_when_source_missing() -> None:
    rules = [
        {
            "target": "normalized.severity_id",
            "source": "severity",
            "default": 0,
        }
    ]
    assert _apply(rules, {}) == {"normalized": {"severity_id": 0}}


def test_apply_required_raises_when_missing() -> None:
    rules = [
        {
            "target": "normalized.id",
            "source": "alertId",
            "required": True,
        }
    ]
    with pytest.raises(MappingRequiredFieldError) as exc_info:
        _apply(rules, {})
    assert exc_info.value.target == "normalized.id"


def test_apply_iso_to_epoch_cast() -> None:
    rules = [
        {
            "target": "normalized.time",
            "source": "createdAt",
            "type_cast": "iso_to_epoch",
        }
    ]
    out = _apply(rules, {"createdAt": "2026-04-23T14:22:10Z"})
    # timestamp_t do OCSF é em MILISSEGUNDOS.
    assert out == {"normalized": {"time": 1776954130000}}


def test_apply_jmespath_nested_path() -> None:
    rules = [
        {
            "target": "normalized.actor.user.name",
            "source": "principal.user.displayName",
        }
    ]
    out = _apply(
        rules,
        {"principal": {"user": {"displayName": "alice"}}},
    )
    assert out == {"normalized": {"actor": {"user": {"name": "alice"}}}}


def test_apply_consumed_paths_track_used_targets() -> None:
    compiled = compile_rules(
        [
            {"target": "normalized.id", "source": "id"},
            {"target": "normalized.severity_id", "source": "severity", "default": 0},
        ]
    )
    res = apply_compiled(compiled, {"id": "abc", "severity": "high"})
    assert "normalized.id" in res.consumed_paths
    assert "normalized.severity_id" in res.consumed_paths


def test_apply_consumed_paths_excludes_default_only_resolution() -> None:
    # Quando o source resolve None e cai no default, o path NAO e
    # contabilizado como consumido -- porque nada do raw foi lido.
    compiled = compile_rules(
        [
            {
                "target": "normalized.severity_id",
                "source": "severity",
                "default": 0,
            }
        ]
    )
    res = apply_compiled(compiled, {})
    assert "normalized.severity_id" not in res.consumed_paths


# ── MappingEngine cache ───────────────────────────────────────────────


def test_engine_caches_compiled_by_version_id() -> None:
    engine = MappingEngine()
    rules = [{"target": "normalized.id", "source": "id"}]

    first = engine.get_compiled("ver-1", rules)
    second = engine.get_compiled("ver-1", rules)
    assert first is second  # mesmo objeto = veio do cache


def test_engine_invalidate_drops_cache() -> None:
    engine = MappingEngine()
    rules = [{"target": "normalized.id", "source": "id"}]

    first = engine.get_compiled("ver-1", rules)
    engine.invalidate("ver-1")
    second = engine.get_compiled("ver-1", rules)
    assert first is not second


def test_engine_apply_returns_result_with_output() -> None:
    engine = MappingEngine()
    rules = [{"target": "normalized.id", "source": "id"}]
    result = engine.apply("ver-2", rules, {"id": "abc"})
    assert result.output == {"normalized": {"id": "abc"}}


def test_engine_propagates_required_error() -> None:
    engine = MappingEngine()
    rules = [{"target": "normalized.id", "source": "id", "required": True}]
    with pytest.raises(MappingRequiredFieldError):
        engine.apply("ver-3", rules, {})


# ── Fase 1.2 -- pre_cast ──────────────────────────────────────────────


def test_compile_rejects_pre_cast_unknown() -> None:
    """pre_cast com nome nao registrado deve levantar MappingDefinitionError."""
    with pytest.raises(MappingDefinitionError, match="pre_cast"):
        compile_rules([
            {
                "target": "normalized.severity",
                "source": "severity",
                "pre_cast": "not_a_real_cast",
            }
        ])


def test_apply_pre_cast_runs_before_value_map() -> None:
    """Caso canonico Sophos: severity chega como int, value_map usa chaves str."""
    rules = [
        {
            "target": "normalized.severity",
            "source": "severity",
            "pre_cast": "to_str",
            "value_map": {"3": "high", "4": "critical"},
        }
    ]
    assert _apply(rules, {"severity": 3}) == {"normalized": {"severity": "high"}}
    assert _apply(rules, {"severity": 4}) == {"normalized": {"severity": "critical"}}


def test_apply_pre_cast_then_value_map_then_type_cast() -> None:
    """Cadeia completa: pre_cast -> value_map -> type_cast."""
    rules = [
        {
            "target": "normalized.severity_label",
            "source": "severity",
            "pre_cast": "to_str",
            "value_map": {"3": "medium", "4": "high"},
            "type_cast": "uppercase",
        }
    ]
    out = _apply(rules, {"severity": 3})
    assert out == {"normalized": {"severity_label": "MEDIUM"}}


def test_apply_pre_cast_error_wrapped_as_mapping_error() -> None:
    """Erro em pre_cast deve ser embrulhado como MappingError com nome do cast."""
    rules = [
        {
            "target": "normalized.severity_id",
            "source": "severity",
            "pre_cast": "to_int",
        }
    ]
    with pytest.raises(MappingError, match="pre_cast") as exc_info:
        _apply(rules, {"severity": "not_a_number"})
    assert "normalized.severity_id" in str(exc_info.value)


# ── Fase 2.1a — v2 preprocess apply ──────────────────────────────────

# Fixture sintetica Sophos Detection (RFC 5737 IPs, example.com emails)
_SOPHOS_DETECTION_RAW = {
    "id": "00000000-0000-0000-0000-000000000001",
    "detectionRule": "XDR-sophos-email-dlpviolation",
    "sensor": {"id": "sensor-001", "type": "email", "source": "Sophos"},
    "mitreAttacks": [{"tactic": {"id": "TA0010", "name": "Exfiltration"}}],
    "severity": 0,
    "processedData": json.dumps({
        "action": "queuedForDelivery",
        "alertScore": 0.1,
        "alertType": "dlpViolation",
        "parsedAlert": {
            "fields": {
                "mailFrom": "sender@example.com",
                "clientIp": "198.51.100.18",
                "envelopeRecipients": ["a@example.com", "b@example.org"],
                "attachments": [
                    {"name": "report.pdf", "checksum": "abc123", "sizeInBytes": 16413}
                ],
                "subject": "Q4 Report",
                "from": "Vendor <sender@example.com>",
                "to": ["a@example.com", "b@example.org"],
            }
        },
    }),
    "time": "2026-04-27T22:41:09Z",
    "type": "Threat",
}


def test_apply_v2_preprocess_extracts_field() -> None:
    """Preprocess basico: json_parse em processedData popula _processed."""
    payload = {
        "preprocess": [
            {
                "op": "json_parse",
                "source": "processedData",
                "target": "_processed",
                "tolerant": True,
            },
        ],
        "rules": [
            {"target": "normalized.id", "source": "id"},
        ],
    }
    compiled = compile_rules(payload, dsl_version=2)
    result = apply_compiled(compiled, _SOPHOS_DETECTION_RAW)
    assert result.output["normalized"]["id"] == "00000000-0000-0000-0000-000000000001"
    # preprocess nao vai para o output diretamente
    assert "_processed" not in result.output


def test_apply_v2_rule_consumes_extracted_field() -> None:
    """Regra que referencia _processed.parsedAlert.fields.mailFrom resolve corretamente."""
    payload = {
        "preprocess": [
            {
                "op": "json_parse",
                "source": "processedData",
                "target": "_processed",
                "tolerant": True,
            },
        ],
        "rules": [
            {"target": "normalized.id", "source": "id"},
            {
                "target": "normalized.email.from",
                "source": "_processed.parsedAlert.fields.mailFrom",
            },
            {
                "target": "normalized.client_ip",
                "source": "_processed.parsedAlert.fields.clientIp",
            },
        ],
    }
    compiled = compile_rules(payload, dsl_version=2)
    result = apply_compiled(compiled, _SOPHOS_DETECTION_RAW)
    assert result.output["normalized"]["email"]["from"] == "sender@example.com"
    assert result.output["normalized"]["client_ip"] == "198.51.100.18"


def test_apply_v2_preprocess_tolerant_invalid_json_does_not_quarantine() -> None:
    """JSON invalido com tolerant=True: campo fica None, sem excecao."""
    raw = {**_SOPHOS_DETECTION_RAW, "processedData": "NOT_VALID_JSON"}
    payload = {
        "preprocess": [
            {
                "op": "json_parse",
                "source": "processedData",
                "target": "_processed",
                "tolerant": True,
            },
        ],
        "rules": [
            {"target": "normalized.id", "source": "id"},
        ],
    }
    compiled = compile_rules(payload, dsl_version=2)
    # Nao deve levantar
    result = apply_compiled(compiled, raw)
    assert result.output["normalized"]["id"] == "00000000-0000-0000-0000-000000000001"


def test_apply_v2_preprocess_strict_invalid_json_raises() -> None:
    """JSON invalido com tolerant=False levanta MappingError."""
    raw = {**_SOPHOS_DETECTION_RAW, "processedData": "INVALID"}
    payload = {
        "preprocess": [
            {
                "op": "json_parse",
                "source": "processedData",
                "target": "_processed",
                "tolerant": False,
            },
        ],
        "rules": [
            {"target": "normalized.id", "source": "id"},
        ],
    }
    compiled = compile_rules(payload, dsl_version=2)
    with pytest.raises(MappingError, match="preprocess"):
        apply_compiled(compiled, raw)


def test_apply_v2_source_root_set_at_compile_time() -> None:
    """CompiledRule.source_root e set corretamente em compile time."""
    payload = {
        "preprocess": [
            {"op": "json_parse", "source": "processedData", "target": "_processed", "tolerant": True},
        ],
        "rules": [
            {"target": "normalized.a", "source": "_processed.x"},
            {"target": "normalized.b", "source": "rawField"},
        ],
    }
    compiled = compile_rules(payload, dsl_version=2)
    assert compiled.rules[0].source_root == "extracted"
    assert compiled.rules[1].source_root == "raw"


# ── Fase 2.2 — fallback_source compile-time validation ───────────────


def test_compile_v1_rejects_fallback_source() -> None:
    """v1 list mapping with fallback_source raises MappingDefinitionError."""
    rules = [
        {
            "target": "normalized.title",
            "source": "ruleDescription",
            "fallback_source": ["attackType"],
        }
    ]
    with pytest.raises(MappingDefinitionError, match="requires DSL v2"):
        compile_rules(rules, dsl_version=1)


def test_compile_v2_accepts_fallback_source() -> None:
    """v2 dict mapping with valid fallback_source compiles cleanly."""
    payload = {
        "rules": [
            {
                "target": "normalized.title",
                "source": "ruleDescription",
                "fallback_source": ["detectionRule", "attackType"],
            }
        ]
    }
    compiled = compile_rules(payload, dsl_version=2)
    rule = compiled.rules[0]
    assert len(rule.fallback_compiled_sources) == 2
    assert rule.fallback_source_strs == ("detectionRule", "attackType")


def test_compile_v2_rejects_fallback_without_source() -> None:
    """const-only rule with fallback_source raises MappingDefinitionError."""
    payload = {
        "rules": [
            {
                "target": "normalized.class_uid",
                "const": 2004,
                "fallback_source": ["something"],
            }
        ]
    }
    with pytest.raises(MappingDefinitionError, match="requires a primary source"):
        compile_rules(payload, dsl_version=2)


def test_compile_v2_rejects_fallback_root_mismatch_primary_raw_fallback_extracted() -> None:
    """Primary in raw (no _), fallback starts with _ (extracted) → compile error."""
    payload = {
        "preprocess": [
            {"op": "json_parse", "source": "processedData", "target": "_processed", "tolerant": True},
        ],
        "rules": [
            {
                "target": "normalized.title",
                "source": "ruleDescription",  # raw root
                "fallback_source": ["_processed.attackType"],  # extracted root
            }
        ],
    }
    with pytest.raises(MappingDefinitionError, match="raiz diferente"):
        compile_rules(payload, dsl_version=2)


def test_compile_v2_rejects_fallback_root_mismatch_primary_extracted_fallback_raw() -> None:
    """Primary starts with _ (extracted), fallback has no _ (raw) → compile error."""
    payload = {
        "preprocess": [
            {"op": "json_parse", "source": "processedData", "target": "_processed", "tolerant": True},
        ],
        "rules": [
            {
                "target": "normalized.title",
                "source": "_processed.ruleDescription",  # extracted root
                "fallback_source": ["attackType"],  # raw root
            }
        ],
    }
    with pytest.raises(MappingDefinitionError, match="raiz diferente"):
        compile_rules(payload, dsl_version=2)


def test_compile_v2_invalid_fallback_jmespath() -> None:
    """Bad JMESPath in fallback raises MappingDefinitionError with index."""
    payload = {
        "rules": [
            {
                "target": "normalized.title",
                "source": "ruleDescription",
                "fallback_source": ["detectionRule", "[[[invalid"],
            }
        ]
    }
    with pytest.raises(MappingDefinitionError, match=r"fallback_source\[1\]"):
        compile_rules(payload, dsl_version=2)


def test_compile_v2_rejects_fallback_source_non_list() -> None:
    """fallback_source that is a string (not list) raises MappingDefinitionError."""
    payload = {
        "rules": [
            {
                "target": "normalized.title",
                "source": "ruleDescription",
                "fallback_source": "attackType",  # should be a list
            }
        ]
    }
    with pytest.raises(MappingDefinitionError, match="lista"):
        compile_rules(payload, dsl_version=2)


# ── Fase 2.2 — fallback_source apply-time behavior ───────────────────


def _apply_v2(payload: dict, raw: dict) -> dict:
    compiled = compile_rules(payload, dsl_version=2)
    return apply_compiled(compiled, raw).output


def test_apply_v2_uses_primary_when_present() -> None:
    """Happy path: primary source resolves, fallbacks are never consulted."""
    payload = {
        "rules": [
            {
                "target": "normalized.title",
                "source": "ruleDescription",
                "fallback_source": ["detectionRule", "attackType"],
            }
        ]
    }
    out = _apply_v2(payload, {"ruleDescription": "primary-value", "detectionRule": "fallback0", "attackType": "fallback1"})
    assert out == {"normalized": {"title": "primary-value"}}


def test_apply_v2_falls_back_to_first_alternative() -> None:
    """Primary null, fallback[0] resolves — returns fallback[0] value."""
    payload = {
        "rules": [
            {
                "target": "normalized.title",
                "source": "ruleDescription",
                "fallback_source": ["detectionRule", "attackType"],
            }
        ]
    }
    out = _apply_v2(payload, {"detectionRule": "first-fallback", "attackType": "second-fallback"})
    assert out == {"normalized": {"title": "first-fallback"}}


def test_apply_v2_falls_back_through_chain() -> None:
    """Primary null, fallback[0] null, fallback[1] resolves."""
    payload = {
        "rules": [
            {
                "target": "normalized.title",
                "source": "ruleDescription",
                "fallback_source": ["detectionRule", "attackType"],
            }
        ]
    }
    out = _apply_v2(payload, {"attackType": "last-resort"})
    assert out == {"normalized": {"title": "last-resort"}}


def test_apply_v2_all_null_uses_default() -> None:
    """All sources (primary + all fallbacks) resolve None → default applies."""
    payload = {
        "rules": [
            {
                "target": "normalized.title",
                "source": "ruleDescription",
                "fallback_source": ["detectionRule", "attackType"],
                "default": "unknown",
            }
        ]
    }
    out = _apply_v2(payload, {})
    assert out == {"normalized": {"title": "unknown"}}


def test_apply_v2_all_null_no_default_writes_none() -> None:
    """All sources resolve None, no default configured → field written as None."""
    payload = {
        "rules": [
            {
                "target": "normalized.title",
                "source": "ruleDescription",
                "fallback_source": ["detectionRule", "attackType"],
            }
        ]
    }
    out = _apply_v2(payload, {})
    assert out == {"normalized": {"title": None}}


def test_apply_v2_consumed_paths_include_fallbacks() -> None:
    """Drift tracking: fallback source strings are always in consumed_paths."""
    payload = {
        "rules": [
            {
                "target": "normalized.title",
                "source": "ruleDescription",
                "fallback_source": ["detectionRule", "attackType"],
            }
        ]
    }
    compiled = compile_rules(payload, dsl_version=2)

    # Case: primary resolves — fallback paths still registered for drift.
    res_primary = apply_compiled(compiled, {"ruleDescription": "val"})
    assert "source:ruleDescription" in res_primary.consumed_paths
    assert "source:detectionRule" in res_primary.consumed_paths
    assert "source:attackType" in res_primary.consumed_paths

    # Case: fallback[1] resolves — all three paths still registered.
    res_fallback = apply_compiled(compiled, {"attackType": "val"})
    assert "source:ruleDescription" in res_fallback.consumed_paths
    assert "source:detectionRule" in res_fallback.consumed_paths
    assert "source:attackType" in res_fallback.consumed_paths

    # Case: all null — fallback paths still registered even when nothing resolves.
    res_all_null = apply_compiled(compiled, {})
    assert "source:detectionRule" in res_all_null.consumed_paths
    assert "source:attackType" in res_all_null.consumed_paths


def test_apply_v2_fallback_with_extracted_root() -> None:
    """Fallbacks respect source_root=extracted when primary starts with _."""
    payload = {
        "preprocess": [
            {
                "op": "json_parse",
                "source": "processedData",
                "target": "_processed",
                "tolerant": True,
            }
        ],
        "rules": [
            {
                "target": "normalized.title",
                "source": "_processed.ruleDescription",
                "fallback_source": ["_processed.attackType"],
            }
        ],
    }
    import json as _json

    # Only attackType is present in the parsed JSON
    raw = {"processedData": _json.dumps({"attackType": "Lateral Movement"})}
    out = _apply_v2(payload, raw)
    assert out == {"normalized": {"title": "Lateral Movement"}}


# ── Fase 2.2 — smoke test: Sophos multi-field resolution ──────────────


@pytest.mark.parametrize(
    "raw,expected_title",
    [
        # Only ruleDescription present
        ({"ruleDescription": "primary-rule", "id": "x", "severity": 3, "time": "2026-04-27T00:00:00Z"}, "primary-rule"),
        # Only detectionRule present
        ({"detectionRule": "fallback0-rule", "id": "x", "severity": 3, "time": "2026-04-27T00:00:00Z"}, "fallback0-rule"),
        # Only attackType present
        ({"attackType": "fallback1-type", "id": "x", "severity": 3, "time": "2026-04-27T00:00:00Z"}, "fallback1-type"),
    ],
    ids=["primary_only", "fallback0_only", "fallback1_only"],
)
def test_apply_v2_sophos_multifield_smoke(raw: dict, expected_title: str) -> None:
    """Smoke test: source=ruleDescription with fallback_source=[detectionRule, attackType]
    resolves correctly when each is the only one populated."""
    payload = {
        "rules": [
            {
                "target": "normalized.title",
                "source": "ruleDescription",
                "fallback_source": ["detectionRule", "attackType"],
                "default": "unknown",
            }
        ]
    }
    out = _apply_v2(payload, raw)
    assert out["normalized"]["title"] == expected_title


# ── Fase 2.3 — when predicate compile-time ───────────────────────────


def test_compile_v1_rejects_when() -> None:
    """v1 list mapping with 'when' raises MappingDefinitionError."""
    rules = [
        {
            "target": "normalized.email_from",
            "source": "mailFrom",
            "when": {"exists": "mailFrom"},
        }
    ]
    with pytest.raises(MappingDefinitionError, match="requires DSL v2"):
        compile_rules(rules, dsl_version=1)


def test_compile_v2_accepts_when() -> None:
    """v2 dict mapping with valid 'when' compiles cleanly."""
    payload = {
        "rules": [
            {
                "target": "normalized.email_from",
                "source": "mailFrom",
                "when": {"exists": "mailFrom"},
            }
        ]
    }
    compiled = compile_rules(payload, dsl_version=2)
    rule = compiled.rules[0]
    assert rule.when_predicate is not None
    assert rule.when_predicate.kind == "exists"
    assert rule.when_predicate.source_str == "mailFrom"
    assert rule.predicate_source_strs == ("mailFrom",)


def test_compile_v2_when_invalid_predicate_raises() -> None:
    """Invalid predicate (no discriminator key) raises MappingDefinitionError."""
    payload = {
        "rules": [
            {
                "target": "normalized.x",
                "source": "x",
                "when": {},  # no valid key
            }
        ]
    }
    with pytest.raises(MappingDefinitionError, match="nenhuma chave"):
        compile_rules(payload, dsl_version=2)


# ── Fase 2.3 — when predicate apply-time behavior ────────────────────


def test_apply_v2_when_true_applies_rule() -> None:
    """Predicate satisfied → target is written."""
    payload = {
        "rules": [
            {
                "target": "normalized.email_from",
                "source": "mailFrom",
                "when": {"exists": "mailFrom"},
            }
        ]
    }
    out = _apply_v2(payload, {"mailFrom": "alice@example.com"})
    assert out == {"normalized": {"email_from": "alice@example.com"}}


def test_apply_v2_when_false_skips_rule() -> None:
    """Predicate not satisfied → target is NOT written (not even None)."""
    payload = {
        "rules": [
            {
                "target": "normalized.email_from",
                "source": "mailFrom",
                "when": {"exists": "mailFrom"},
            }
        ]
    }
    out = _apply_v2(payload, {"subject": "hello"})
    assert "normalized" not in out or "email_from" not in out.get("normalized", {})


def test_apply_v2_when_skip_does_not_write_default() -> None:
    """Even with default configured, a skipped rule writes NOTHING."""
    payload = {
        "rules": [
            {
                "target": "normalized.email_from",
                "source": "mailFrom",
                "default": "unknown@example.com",
                "when": {"exists": "mailFrom"},
            }
        ]
    }
    out = _apply_v2(payload, {})
    # Target completely absent — not even the default is written.
    assert "normalized" not in out or "email_from" not in out.get("normalized", {})


def test_apply_v2_when_multiple_rules_partial_skip() -> None:
    """Only rules with true predicates are written; others are skipped."""
    payload = {
        "rules": [
            {"target": "normalized.id", "source": "id"},
            {
                "target": "normalized.email_from",
                "source": "mailFrom",
                "when": {"exists": "mailFrom"},
            },
            {
                "target": "normalized.class_uid",
                "const": 2004,
            },
        ]
    }
    # mailFrom absent → email_from skipped
    out = _apply_v2(payload, {"id": "abc-001"})
    assert out["normalized"]["id"] == "abc-001"
    assert out["normalized"]["class_uid"] == 2004
    assert "email_from" not in out["normalized"]

    # mailFrom present → email_from written
    out2 = _apply_v2(payload, {"id": "abc-002", "mailFrom": "bob@example.com"})
    assert out2["normalized"]["email_from"] == "bob@example.com"


def test_apply_v2_when_equals_predicate() -> None:
    """when.equals gates rule correctly."""
    payload = {
        "rules": [
            {
                "target": "normalized.dlp_flag",
                "const": True,
                "when": {"equals": {"source": "alertType", "value": "dlpViolation"}},
            }
        ]
    }
    out_match = _apply_v2(payload, {"alertType": "dlpViolation"})
    assert out_match["normalized"]["dlp_flag"] is True

    out_no_match = _apply_v2(payload, {"alertType": "malware"})
    assert "normalized" not in out_no_match or "dlp_flag" not in out_no_match.get("normalized", {})


def test_apply_v2_when_in_predicate() -> None:
    """when.in gates rule correctly."""
    payload = {
        "rules": [
            {
                "target": "normalized.critical",
                "const": True,
                "when": {"in": {"source": "severity", "values": ["high", "critical"]}},
            }
        ]
    }
    assert _apply_v2(payload, {"severity": "critical"})["normalized"]["critical"] is True
    assert _apply_v2(payload, {"severity": "high"})["normalized"]["critical"] is True
    out_low = _apply_v2(payload, {"severity": "low"})
    assert "critical" not in out_low.get("normalized", {})


def test_apply_v2_when_not_predicate() -> None:
    """when.not negates correctly."""
    payload = {
        "rules": [
            {
                "target": "normalized.no_mail",
                "const": True,
                "when": {"not": {"exists": "mailFrom"}},
            }
        ]
    }
    # mailFrom absent → predicate true → rule applied
    assert _apply_v2(payload, {})["normalized"]["no_mail"] is True
    # mailFrom present → predicate false → rule skipped
    assert "no_mail" not in _apply_v2(payload, {"mailFrom": "x@example.com"}).get("normalized", {})


def test_apply_v2_when_uses_extracted_root() -> None:
    """when predicate evaluates against extracted (not raw) for _ prefix sources."""
    payload = {
        "preprocess": [
            {
                "op": "json_parse",
                "source": "processedData",
                "target": "_processed",
                "tolerant": True,
            }
        ],
        "rules": [
            {
                "target": "normalized.mail_from",
                "source": "_processed.parsedAlert.fields.mailFrom",
                "when": {"exists": "_processed.parsedAlert.fields.mailFrom"},
            }
        ],
    }
    import json as _json

    raw_with_mail = {
        "processedData": _json.dumps(
            {"parsedAlert": {"fields": {"mailFrom": "sender@example.com"}}}
        )
    }
    out = _apply_v2(payload, raw_with_mail)
    assert out["normalized"]["mail_from"] == "sender@example.com"

    raw_without_mail = {
        "processedData": _json.dumps({"parsedAlert": {"fields": {}}})
    }
    out2 = _apply_v2(payload, raw_without_mail)
    assert "mail_from" not in out2.get("normalized", {})


def test_apply_v2_when_consumed_paths_include_predicate() -> None:
    """Predicate source paths are registered in consumed_paths for drift tracking."""
    payload = {
        "rules": [
            {
                "target": "normalized.email_from",
                "source": "mailFrom",
                "when": {"exists": "mailFrom"},
            }
        ]
    }
    compiled = compile_rules(payload, dsl_version=2)

    # Case: predicate true (mailFrom present)
    res_true = apply_compiled(compiled, {"mailFrom": "x@example.com"})
    assert "source:mailFrom" in res_true.consumed_paths

    # Case: predicate false (mailFrom absent) — path still registered for drift
    res_false = apply_compiled(compiled, {})
    assert "source:mailFrom" in res_false.consumed_paths


def test_apply_v2_when_default_without_when_still_works() -> None:
    """Existing default behavior is unaffected when no when predicate is set."""
    payload = {
        "rules": [
            {
                "target": "normalized.title",
                "source": "ruleDescription",
                "default": "untitled",
            }
        ]
    }
    out = _apply_v2(payload, {})
    assert out["normalized"]["title"] == "untitled"


# ── Fase 3.1 — array_builder engine integration ──────────────────────


def test_compile_v1_rejects_kind_field() -> None:
    """v1 list mapping with kind='array_builder' raises MappingDefinitionError."""
    rules = [
        {
            "target": "normalized.observables",
            "kind": "array_builder",
            "items": [],
        }
    ]
    with pytest.raises(MappingDefinitionError, match="requires DSL v2"):
        compile_rules(rules, dsl_version=1)


def test_compile_v2_unknown_kind_raises() -> None:
    """Unknown kind value raises MappingDefinitionError with the bad value."""
    payload = {
        "rules": [
            {
                "target": "normalized.x",
                "kind": "frobnicate",
                "items": [],
            }
        ]
    }
    with pytest.raises(MappingDefinitionError, match="frobnicate"):
        compile_rules(payload, dsl_version=2)


def test_compile_v2_dispatches_to_array_builder() -> None:
    """v2 rule with kind='array_builder' produces CompiledArrayBuilderRule."""
    from backend.app.collectors.normalize.array_builder import CompiledArrayBuilderRule

    payload = {
        "rules": [
            {
                "target": "normalized.observables",
                "kind": "array_builder",
                "items": [
                    {
                        "name": "src_ip",
                        "type": "IP Address",
                        "type_id": 2,
                        "source": "clientIp",
                    }
                ],
            }
        ]
    }
    compiled = compile_rules(payload, dsl_version=2)
    assert len(compiled.rules) == 1
    assert isinstance(compiled.rules[0], CompiledArrayBuilderRule)
    assert compiled.rules[0].target_path == ("normalized", "observables")
    assert compiled.rules[0].items[0].name == "src_ip"


def test_apply_v2_array_builder_writes_observables() -> None:
    """Full integration: apply_compiled with array_builder populates observables list."""
    payload = {
        "rules": [
            {
                "target": "normalized.id",
                "source": "id",
            },
            {
                "target": "normalized.observables",
                "kind": "array_builder",
                "items": [
                    {
                        "name": "src_ip",
                        "type": "IP Address",
                        "type_id": 2,
                        "source": "clientIp",
                    },
                    {
                        "name": "email_from",
                        "type": "Email Address",
                        "type_id": 5,
                        "source": "mailFrom",
                    },
                ],
                "skip_null": True,
                "dedup_by": ["value"],
            },
        ]
    }
    raw = {
        "id": "alert-001",
        "clientIp": "198.51.100.1",
        "mailFrom": "sender@example.com",
    }
    compiled = compile_rules(payload, dsl_version=2)
    result = apply_compiled(compiled, raw)
    assert result.output["normalized"]["id"] == "alert-001"
    observables = result.output["normalized"]["observables"]
    assert isinstance(observables, list)
    assert len(observables) == 2
    assert {"name": "src_ip", "type": "IP Address", "type_id": 2, "value": "198.51.100.1"} in observables
    assert {"name": "email_from", "type": "Email Address", "type_id": 5, "value": "sender@example.com"} in observables


def test_apply_v2_array_builder_consumed_paths_registered() -> None:
    """Item source paths are added to consumed_paths for drift tracking."""
    payload = {
        "rules": [
            {
                "target": "normalized.observables",
                "kind": "array_builder",
                "items": [
                    {"name": "src_ip", "type": "IP Address", "type_id": 2, "source": "clientIp"},
                    {"name": "email_from", "type": "Email Address", "type_id": 5, "source": "mailFrom"},
                ],
            }
        ]
    }
    compiled = compile_rules(payload, dsl_version=2)
    result = apply_compiled(compiled, {"clientIp": "1.2.3.4"})
    assert "source:clientIp" in result.consumed_paths
    assert "source:mailFrom" in result.consumed_paths


def test_apply_v2_array_builder_sophos_observables_end_to_end() -> None:
    """End-to-end: preprocess json_parse + scalar rules + array_builder → full OCSF envelope."""
    payload = {
        "preprocess": [
            {
                "op": "json_parse",
                "source": "processedData",
                "target": "_processed",
                "tolerant": True,
            },
        ],
        "rules": [
            {"target": "normalized.id", "source": "id"},
            {"target": "normalized.type_name", "source": "type"},
            {
                "target": "normalized.observables",
                "kind": "array_builder",
                "items": [
                    {
                        "name": "src_ip",
                        "type": "IP Address",
                        "type_id": 2,
                        "source": "_processed.parsedAlert.fields.clientIp",
                    },
                    {
                        "name": "email_from",
                        "type": "Email Address",
                        "type_id": 5,
                        "source": "_processed.parsedAlert.fields.mailFrom",
                    },
                    {
                        "name": "email_to",
                        "type": "Email Address",
                        "type_id": 5,
                        "source": "_processed.parsedAlert.fields.envelopeRecipients",
                        "explode": True,
                    },
                    {
                        "name": "file_hash",
                        "type": "Hash",
                        "type_id": 8,
                        "source": "_processed.parsedAlert.fields.attachments[*].checksum",
                        "explode": True,
                        "skip_null": True,
                    },
                ],
                "skip_null": True,
                "dedup_by": ["value"],
            },
        ],
    }

    compiled = compile_rules(payload, dsl_version=2)
    result = apply_compiled(compiled, _SOPHOS_DETECTION_RAW)

    # Scalar rules still work.
    assert result.output["normalized"]["id"] == "00000000-0000-0000-0000-000000000001"
    assert result.output["normalized"]["type_name"] == "Threat"

    # Observables list is populated (non-empty).
    observables = result.output["normalized"]["observables"]
    assert isinstance(observables, list)
    assert len(observables) > 0

    # Verify specific expected observables.
    values = {obs["value"] for obs in observables}
    assert "198.51.100.18" in values          # clientIp
    assert "sender@example.com" in values      # mailFrom
    assert "a@example.com" in values           # envelopeRecipients[0]
    assert "b@example.org" in values           # envelopeRecipients[1]
    assert "abc123" in values                  # attachments[0].checksum

    # Dedup: sender@example.com appears in both mailFrom and potentially envelopeRecipients
    # — it must appear only once if dedup_by=["value"] is working.
    from collections import Counter
    value_counts = Counter(obs["value"] for obs in observables)
    for val, count in value_counts.items():
        assert count == 1, f"Duplicate observable value {val!r} (count={count})"


def test_compile_v2_mixed_scalar_and_array_builder_rules() -> None:
    """Mixed rules: scalar + array_builder in same v2 mapping compile successfully."""
    payload = {
        "rules": [
            {"target": "normalized.id", "source": "id"},
            {
                "target": "normalized.observables",
                "kind": "array_builder",
                "items": [
                    {"name": "src_ip", "type": "IP Address", "type_id": 2, "source": "clientIp"},
                ],
            },
            {"target": "normalized.class_uid", "const": 2004},
        ]
    }
    compiled = compile_rules(payload, dsl_version=2)
    assert len(compiled.rules) == 3

    from backend.app.collectors.normalize.engine import CompiledRule
    from backend.app.collectors.normalize.array_builder import CompiledArrayBuilderRule

    assert isinstance(compiled.rules[0], CompiledRule)
    assert isinstance(compiled.rules[1], CompiledArrayBuilderRule)
    assert isinstance(compiled.rules[2], CompiledRule)


# ── Fase 4.1a — expected_always_default compile-time ─────────────────


def test_compile_rejects_expected_always_default_v1() -> None:
    """V1 list mapping with expected_always_default raises MappingDefinitionError (Fix 4).

    The flag is a v2-only feature — silently accepting it in v1 mappings was a
    security gap that allowed suppression of the 100%-default warning on legacy
    mappings.  Now explicitly rejected to prevent silent misconfiguration.
    """
    rules = [
        {
            "target": "normalized.placeholder",
            "source": "nonexistent_field",
            "default": "unknown",
            "expected_always_default": True,
        }
    ]
    with pytest.raises(MappingDefinitionError, match="expected_always_default.*v2|v2.*expected_always_default"):
        compile_rules(rules, dsl_version=1)


def test_compile_accepts_expected_always_default_v2() -> None:
    """V2 dict mapping with expected_always_default=True compiles cleanly."""
    payload = {
        "rules": [
            {
                "target": "normalized.placeholder",
                "source": "nonexistent_field",
                "default": "unknown",
                "expected_always_default": True,
            }
        ]
    }
    compiled = compile_rules(payload, dsl_version=2)
    assert compiled.rules[0].expected_always_default is True


def test_compile_rejects_expected_always_default_non_bool() -> None:
    """expected_always_default with non-bool value raises MappingDefinitionError.

    Must use DSL v2 (dict shape) since v1 now rejects the flag entirely (Fix 4).
    The non-bool type check runs inside v2 compilation after the version guard.
    """
    with pytest.raises(MappingDefinitionError, match="bool"):
        compile_rules(
            {
                "rules": [
                    {
                        "target": "normalized.x",
                        "source": "x",
                        "default": 0,
                        "expected_always_default": "yes",  # string instead of bool
                    }
                ]
            },
            dsl_version=2,
        )

    with pytest.raises(MappingDefinitionError, match="bool"):
        compile_rules(
            {
                "rules": [
                    {
                        "target": "normalized.x",
                        "source": "x",
                        "default": 0,
                        "expected_always_default": 1,  # int instead of bool
                    }
                ]
            },
            dsl_version=2,
        )


# ── Fase 4.1a — default_hits tracking apply-time ─────────────────────


def test_apply_records_default_hit_when_source_null() -> None:
    """Rule with source returning null + default: target_str appears in default_hits."""
    compiled = compile_rules(
        [
            {
                "target": "normalized.severity_id",
                "source": "missing_field",
                "default": 0,
            }
        ]
    )
    result = apply_compiled(compiled, {})
    # Value should be the default (0)
    assert result.output == {"normalized": {"severity_id": 0}}
    # And the target should be tracked as a default hit
    assert "normalized.severity_id" in result.default_hits


def test_apply_no_default_hit_when_value_present() -> None:
    """Happy path: source resolves a real value — target NOT in default_hits."""
    compiled = compile_rules(
        [
            {
                "target": "normalized.severity_id",
                "source": "severity",
                "default": 0,
            }
        ]
    )
    result = apply_compiled(compiled, {"severity": 4})
    assert result.output == {"normalized": {"severity_id": 4}}
    assert "normalized.severity_id" not in result.default_hits


def test_apply_no_default_hit_when_no_default_attr() -> None:
    """Rule without default configured: target NOT in default_hits even when value is null."""
    compiled = compile_rules(
        [
            {
                "target": "normalized.maybe_field",
                "source": "optional_field",
                # No 'default' key — engine writes None
            }
        ]
    )
    result = apply_compiled(compiled, {})
    assert result.output == {"normalized": {"maybe_field": None}}
    # No default to count — default_hits must be empty
    assert result.default_hits == ()


@pytest.mark.parametrize(
    "raw,expected_in_hits",
    [
        ({}, True),                         # source null → default hit
        ({"severity": 3}, False),           # source present → no hit
        ({"severity": None}, True),         # explicit None → default hit (JMESPath returns None)
    ],
    ids=["source_missing", "source_present", "source_explicit_null"],
)
def test_apply_default_hits_parametrized(raw: dict, expected_in_hits: bool) -> None:
    """Parametrized: default_hits tracking across null/present/explicit-null source."""
    compiled = compile_rules(
        [
            {
                "target": "normalized.severity_id",
                "source": "severity",
                "default": 0,
            }
        ]
    )
    result = apply_compiled(compiled, raw)
    hit = "normalized.severity_id" in result.default_hits
    assert hit == expected_in_hits


def test_apply_default_hits_empty_for_const_rule() -> None:
    """const rules have no source — default_hits is always empty."""
    compiled = compile_rules(
        [
            {
                "target": "normalized.class_uid",
                "const": 2004,
            }
        ]
    )
    result = apply_compiled(compiled, {})
    assert result.default_hits == ()


def test_apply_default_hits_excludes_array_builder() -> None:
    """array_builder rules are NOT counted in default_hits (no scalar default)."""
    payload = {
        "rules": [
            {
                "target": "normalized.observables",
                "kind": "array_builder",
                "items": [
                    {
                        "name": "src_ip",
                        "type": "IP Address",
                        "type_id": 2,
                        "source": "missing_ip_field",  # will resolve None
                    }
                ],
                "skip_null": True,
            }
        ]
    }
    compiled = compile_rules(payload, dsl_version=2)
    result = apply_compiled(compiled, {})
    # array_builder writes empty list (skip_null=True), no default_hits
    assert result.default_hits == ()


def test_apply_multiple_rules_default_hits_only_null_ones() -> None:
    """Multiple rules: only the ones with null source appear in default_hits."""
    compiled = compile_rules(
        [
            {"target": "normalized.id", "source": "id", "default": "unknown-id"},
            {"target": "normalized.severity_id", "source": "severity", "default": 0},
            {"target": "normalized.class_uid", "const": 2004},
        ]
    )
    # Only 'id' is provided; 'severity' is missing
    result = apply_compiled(compiled, {"id": "alert-123"})
    # 'severity' default was hit; 'id' was resolved; 'class_uid' is const
    assert "normalized.severity_id" in result.default_hits
    assert "normalized.id" not in result.default_hits
    assert "normalized.class_uid" not in result.default_hits


# ── default_from: "ingest_time" — rede de segurança temporal (F1b) ─────


def _ingest_time_mapping(extra: dict | None = None) -> dict:
    rule = {"target": "normalized.time", "source": "time", "required": True}
    if extra:
        rule.update(extra)
    return {"rules": [rule, {"target": "normalized.x", "const": 1}]}


def test_default_from_ingest_time_fills_when_missing() -> None:
    compiled = compile_rules(_ingest_time_mapping({"default_from": "ingest_time"}), 2)
    res = apply_compiled(compiled, {}, ingest_time_epoch=12345)
    assert res.output["normalized"]["time"] == 12345
    assert res.ingest_fallback_targets == ("normalized.time",)


def test_default_from_ingest_time_not_used_when_value_present() -> None:
    compiled = compile_rules(_ingest_time_mapping({"default_from": "ingest_time"}), 2)
    res = apply_compiled(compiled, {"time": 999}, ingest_time_epoch=12345)
    assert res.output["normalized"]["time"] == 999
    assert res.ingest_fallback_targets == ()


def test_default_from_ingest_time_still_required_without_ingest() -> None:
    # Sem ingest_time_epoch, um campo required ausente AINDA quarentena
    # (defesa em profundidade — não inventa timestamp do nada).
    compiled = compile_rules(_ingest_time_mapping({"default_from": "ingest_time"}), 2)
    with pytest.raises(MappingRequiredFieldError):
        apply_compiled(compiled, {})


def test_default_from_yields_to_fallback_source() -> None:
    # fallback_source (dado do vendor) tem prioridade sobre o ingest_time.
    mapping = {
        "rules": [
            {
                "target": "normalized.time",
                "source": "time",
                "fallback_source": ["sensorGeneratedAt"],
                "default_from": "ingest_time",
                "required": True,
            }
        ]
    }
    compiled = compile_rules(mapping, 2)
    res = apply_compiled(compiled, {"sensorGeneratedAt": 777}, ingest_time_epoch=12345)
    assert res.output["normalized"]["time"] == 777
    assert res.ingest_fallback_targets == ()


def test_default_from_rejected_in_v1() -> None:
    with pytest.raises(MappingDefinitionError):
        compile_rules(
            [{"target": "normalized.time", "source": "time", "default_from": "ingest_time"}]
        )


def test_default_from_invalid_value_rejected() -> None:
    with pytest.raises(MappingDefinitionError):
        compile_rules(
            {"rules": [{"target": "normalized.x", "source": "a", "default_from": "bogus"}]},
            2,
        )


# ── raw_reduction — redução de payload para o dispatch (F3) ────────────


def test_raw_reduction_clips_string_and_caps_list() -> None:
    mapping = {
        "rules": [{"target": "normalized.x", "const": 1}],
        "raw_reduction": [
            {"path": "blob", "max_bytes": 10},
            {"path": "arr", "max_items": 2},
            {"path": "nested.deep", "max_bytes": 5},
        ],
    }
    compiled = compile_rules(mapping, 2)
    assert len(compiled.raw_reduction) == 3
    raw = {
        "blob": "x" * 100,
        "arr": [1, 2, 3, 4, 5],
        "nested": {"deep": "abcdefghij"},
        "keep": "intact",
    }
    res = apply_compiled(compiled, raw)
    rr = res.reduced_raw
    assert rr is not None
    assert len(rr["blob"].encode("utf-8")) == 10
    assert rr["arr"] == [1, 2]
    assert len(rr["nested"]["deep"].encode("utf-8")) == 5
    assert rr["keep"] == "intact"  # campos não listados ficam intactos
    assert "_centralops_reduced" in rr
    # O raw ORIGINAL não é mutado (normalização viu o payload completo).
    assert raw["blob"] == "x" * 100
    assert raw["arr"] == [1, 2, 3, 4, 5]


def test_raw_reduction_none_when_under_caps() -> None:
    mapping = {
        "rules": [{"target": "normalized.x", "const": 1}],
        "raw_reduction": [{"path": "blob", "max_bytes": 1000}],
    }
    compiled = compile_rules(mapping, 2)
    res = apply_compiled(compiled, {"blob": "short"})
    assert res.reduced_raw is None  # nada reduzido → caller reusa o raw original


def test_raw_reduction_absent_for_v1() -> None:
    compiled = compile_rules([{"target": "normalized.x", "const": 1}])
    assert compiled.raw_reduction == ()


def test_raw_reduction_invalid_spec_rejected() -> None:
    with pytest.raises(MappingDefinitionError):
        compile_rules(
            {
                "rules": [{"target": "normalized.x", "const": 1}],
                "raw_reduction": [{"path": "blob"}],  # falta max_items/max_bytes
            },
            2,
        )


# ── Regressão: default não passa por pre_cast/value_map ────────────────────
# Bug (E2E 02-dry-run-live): uma regra com `value_map` + `pre_cast: lowercase` e
# um `default` NÃO-string (int) derrubava o EVENTO INTEIRO quando o source estava
# ausente — o default int chegava ao lowercase (`espera str, recebeu int`) →
# MappingError → sample descartado → envelope sem class_uid. O default é o valor de
# saída final, então pula pre_cast + value_map (mas ainda passa por type_cast).
def _status_id_rule(default):
    return {
        "target": "normalized.status_id",
        "source": "status",
        "pre_cast": "lowercase",
        "value_map": {"open": 1, "new": 1, "closed": 4},
        "default": default,
    }


def test_int_default_bypasses_lowercase_pre_cast_no_crash() -> None:
    # source ausente → default int 1 → NÃO deve levantar (bypass do pre_cast).
    compiled = compile_rules({"preprocess": [], "rules": [_status_id_rule(1)]}, 2)
    res = apply_compiled(compiled, {})  # sem `status`
    assert res.output["normalized"]["status_id"] == 1


def test_int_default_bypasses_value_map_emits_default_as_is() -> None:
    # default int NÃO é usado como chave crua do value_map — sai como está.
    compiled = compile_rules({"preprocess": [], "rules": [_status_id_rule(1)]}, 2)
    assert apply_compiled(compiled, {}).output["normalized"]["status_id"] == 1


def test_pre_cast_and_value_map_still_apply_when_source_present() -> None:
    # Com source presente, pre_cast (lowercase) + value_map continuam aplicando.
    compiled = compile_rules({"preprocess": [], "rules": [_status_id_rule(1)]}, 2)
    res = apply_compiled(compiled, {"status": "CLOSED"})
    assert res.output["normalized"]["status_id"] == 4  # lowercase→"closed"→4


def test_dry_run_style_sample_normalizes_with_class_uid() -> None:
    # Reproduz o cenário do dry-run E2E: class_uid const + status_id ausente com
    # default int não pode mais quarentenar o evento.
    rules = {
        "preprocess": [],
        "rules": [
            {"target": "normalized.class_uid", "const": 2004, "required": True},
            _status_id_rule(1),
        ],
    }
    compiled = compile_rules(rules, 2)
    out = apply_compiled(compiled, {"id": "x"}).output  # sem `status`
    assert out["normalized"]["class_uid"] == 2004
    assert out["normalized"]["status_id"] == 1


# ── Regressão: drift de enum (MISS do value_map) ──────────────────────────
# Defeito (auditoria de fidelidade OCSF): quando o vendor manda um valor que não
# está no `value_map`, o passthrough do operador devolvia o valor CRU — um str
# vazava para um campo que o OCSF tipa como int (ex. ninjaone
# severity=WARNING → severity_id="WARNING"). Com `default` declarado, o MISS
# passa a usar o default (o valor de saída final escolhido pelo operador).
# Sem `default`, o passthrough legado é mantido (compat retroativa).

_ENUM_VALUE_MAP = {"critical": 5, "high": 4, "medium": 3, "low": 2, "info": 1}


def _severity_rule(**extra):
    rule = {
        "target": "normalized.severity_id",
        "source": "severity",
        "value_map": dict(_ENUM_VALUE_MAP),
    }
    rule.update(extra)
    return rule


def test_value_map_miss_with_default_uses_default() -> None:
    # (i) MISS + default → default int, não o str cru do vendor.
    compiled = compile_rules([_severity_rule(default=1)])
    out = apply_compiled(compiled, {"severity": "BRAND_NEW_LEVEL"}).output
    assert out["normalized"]["severity_id"] == 1
    assert isinstance(out["normalized"]["severity_id"], int)


def test_value_map_miss_without_default_passthrough_backcompat() -> None:
    # (ii) MISS sem default → passthrough do valor cru (comportamento legado).
    compiled = compile_rules([_severity_rule()])
    out = apply_compiled(compiled, {"severity": "BRAND_NEW_LEVEL"}).output
    assert out["normalized"]["severity_id"] == "BRAND_NEW_LEVEL"


def test_value_map_hit_unaffected_by_default() -> None:
    # (iii) HIT normal continua ganhando do default.
    compiled = compile_rules([_severity_rule(default=1)])
    for vendor_value, expected in (("critical", 5), ("HIGH", 4), ("low", 2)):
        out = apply_compiled(compiled, {"severity": vendor_value}).output
        assert out["normalized"]["severity_id"] == expected


def test_value_map_hit_to_falsy_value_is_not_treated_as_miss() -> None:
    # HIT cujo valor mapeado é falsy (0) NÃO pode cair no default.
    compiled = compile_rules(
        [
            {
                "target": "normalized.severity_id",
                "source": "severity",
                "value_map": {"none": 0},
                "default": 1,
            }
        ]
    )
    out = apply_compiled(compiled, {"severity": "none"}).output
    assert out["normalized"]["severity_id"] == 0


def test_value_map_hit_mapping_to_same_value_is_not_treated_as_miss() -> None:
    # HIT identidade ("open" → "open") é HIT: não pode disparar o default.
    compiled = compile_rules(
        [
            {
                "target": "normalized.status",
                "source": "status",
                "value_map": {"open": "open"},
                "default": "Other",
            }
        ]
    )
    out = apply_compiled(compiled, {"status": "open"}).output
    assert out["normalized"]["status"] == "open"


def test_value_map_miss_default_still_goes_through_type_cast() -> None:
    # Contrato: o default pula pre_cast + value_map, mas ainda passa por type_cast.
    compiled = compile_rules(
        [
            {
                "target": "normalized.severity_id",
                "source": "severity",
                "pre_cast": "lowercase",
                "value_map": dict(_ENUM_VALUE_MAP),
                "type_cast": "to_str",
                "default": 1,
            }
        ]
    )
    out = apply_compiled(compiled, {"severity": "BRAND_NEW_LEVEL"}).output
    assert out["normalized"]["severity_id"] == "1"


def test_value_map_miss_default_is_not_recast_by_pre_cast() -> None:
    # O default int NÃO pode chegar ao pre_cast de string (OperatorError → o
    # evento inteiro seria quarentenado). MISS com pre_cast + default int passa.
    compiled = compile_rules(
        [
            {
                "target": "normalized.severity_id",
                "source": "severity",
                "pre_cast": "lowercase",
                "value_map": dict(_ENUM_VALUE_MAP),
                "default": 1,
            }
        ]
    )
    out = apply_compiled(compiled, {"severity": "BRAND_NEW_LEVEL"}).output
    assert out["normalized"]["severity_id"] == 1


def test_value_map_miss_with_default_works_in_dsl_v2() -> None:
    compiled = compile_rules({"preprocess": [], "rules": [_severity_rule(default=1)]}, 2)
    out = apply_compiled(compiled, {"severity": "WARNING"}).output
    assert out["normalized"]["severity_id"] == 1


def test_ninjaone_severity_drift_never_leaks_string_into_severity_id() -> None:
    """(iv) Caso real ninjaone: severity fora do mapa → severity_id int."""
    ninjaone_rule = {
        "target": "normalized.severity_id",
        "source": "severity",
        "value_map": {
            "critical": 5,
            "high": 4,
            "medium": 3,
            "low": 2,
            "info": 1,
            "informational": 1,
            "none": 0,
        },
        "default": 1,
    }
    compiled = compile_rules([ninjaone_rule])

    # Valores canônicos continuam mapeando corretamente.
    for vendor_value, expected in (("critical", 5), ("none", 0), ("informational", 1)):
        out = apply_compiled(compiled, {"severity": vendor_value}).output
        assert out["normalized"]["severity_id"] == expected

    # Drift de enum: valores que o vendor manda mas não estão no mapa.
    for drifted in ("WARNING", "BRAND_NEW_LEVEL", "Moderate"):
        out = apply_compiled(compiled, {"severity": drifted}).output
        severity_id = out["normalized"]["severity_id"]
        assert isinstance(severity_id, int), f"{drifted!r} vazou {severity_id!r}"
        assert severity_id == 1


def test_ninjaone_default_mapping_file_severity_drift_is_int() -> None:
    """O mapping default versionado no repo não vaza str em severity_id."""
    import pathlib

    path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "normalize"
        / "defaults"
        / "ninjaone_activity.json"
    )
    rules = json.loads(path.read_text(encoding="utf-8"))
    compiled = compile_rules(rules)
    out = apply_compiled(compiled, {"severity": "WARNING"}).output
    assert isinstance(out["normalized"]["severity_id"], int)
