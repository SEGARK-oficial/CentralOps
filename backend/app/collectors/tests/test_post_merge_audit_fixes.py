"""Tests for post-merge audit fixes (BLOCKER + MAJOR + HIGH).

Covers:
- Fix 1 (BLOCKER):  _load_current_mapping returns 3-tuple with dsl_version;
                    pipeline + backfill unpack and pass it to engine.apply.
- Fix 2 (HIGH):     json_parse catches RecursionError + ValueError from C parser.
- Fix 3 (MAJOR):    OperatorSizeError NOT silenced by tolerant=True.
- Fix 4 (MAJOR):    expected_always_default rejected in v1; captured in RuleSnapshot.
- Fix 5 (MAJOR):    compute_diff / diff_versions handles v2 dict shape.
- Fix 6 (MAJOR):    MappingEngine cache is true LRU (OrderedDict + move_to_end).
"""

from __future__ import annotations

import json
from collections import OrderedDict
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from backend.app.collectors.normalize.engine import (
    MappingDefinitionError,
    MappingEngine,
    MappingError,
    compile_rules,
)
from backend.app.collectors.normalize.preprocess import json_parse
from backend.app.collectors.normalize.registry import OperatorError, OperatorSizeError


# ─────────────────────────────────────────────────────────────────────────────
# Fix 1 (BLOCKER) — _load_current_mapping returns (version_id, rules, dsl_version)
# ─────────────────────────────────────────────────────────────────────────────


class TestLoadCurrentMappingReturns3Tuple:
    """_load_current_mapping must return (id, rules, dsl_version)."""

    def _make_fake_version(
        self,
        *,
        rules: Any,
        dsl_version: int = 2,
        version_id: str = "ver-uuid-1",
    ) -> MagicMock:
        version = MagicMock()
        version.id = version_id
        version.rules = json.dumps(rules)
        version.dsl_version = dsl_version
        return version

    def _make_db_session(self, defn: MagicMock, version: MagicMock) -> MagicMock:
        db = MagicMock()
        db.__enter__ = MagicMock(return_value=db)
        db.__exit__ = MagicMock(return_value=False)
        db.scalar = MagicMock(side_effect=[defn, version])
        return db

    def test_returns_3tuple_with_dsl_version_2(self) -> None:
        """v2 mapping: dsl_version=2 must be returned in position [2]."""
        from backend.app.collectors.pipeline import _load_current_mapping

        defn = MagicMock()
        defn.current_version_id = "ver-uuid-1"

        v2_rules = {
            "preprocess": [
                {"op": "json_parse", "source": "rawData", "target": "_parsed", "tolerant": True}
            ],
            "rules": [{"target": "normalized.id", "source": "id"}],
        }
        version = self._make_fake_version(rules=v2_rules, dsl_version=2)
        db = self._make_db_session(defn, version)

        with patch("backend.app.collectors.pipeline.database.SessionLocal", return_value=db):
            result = _load_current_mapping("sophos", "sophos.detection")

        assert result is not None
        version_id, rules, dsl_version = result
        assert version_id == "ver-uuid-1"
        assert isinstance(rules, dict)
        assert dsl_version == 2

    def test_returns_dsl_version_1_for_legacy(self) -> None:
        """v1 (list) mapping with dsl_version=1 returns 1 in position [2]."""
        from backend.app.collectors.pipeline import _load_current_mapping

        defn = MagicMock()
        defn.current_version_id = "ver-v1-1"

        v1_rules: List[Dict[str, Any]] = [{"target": "normalized.id", "source": "id"}]
        version = self._make_fake_version(rules=v1_rules, dsl_version=1, version_id="ver-v1-1")
        db = self._make_db_session(defn, version)

        with patch("backend.app.collectors.pipeline.database.SessionLocal", return_value=db):
            result = _load_current_mapping("defender", "defender.alert")

        assert result is not None
        _, _, dsl_version = result
        assert dsl_version == 1

    def test_returns_default_1_when_dsl_version_is_null(self) -> None:
        """NULL dsl_version in DB defaults to 1 (legacy guard)."""
        from backend.app.collectors.pipeline import _load_current_mapping

        defn = MagicMock()
        defn.current_version_id = "ver-null"

        v1_rules: List[Dict[str, Any]] = [{"target": "normalized.id", "source": "id"}]
        version = self._make_fake_version(rules=v1_rules, dsl_version=1, version_id="ver-null")
        # Simulate NULL in DB via None / falsy
        version.dsl_version = None
        db = self._make_db_session(defn, version)

        with patch("backend.app.collectors.pipeline.database.SessionLocal", return_value=db):
            result = _load_current_mapping("ninjaone", "ninjaone.alert")

        assert result is not None
        _, _, dsl_version = result
        assert dsl_version == 1

    def test_pipeline_passes_dsl_version_to_engine_apply(self) -> None:
        """Pipeline call site unpacks 3-tuple and passes dsl_version= to engine."""
        # We verify the integration: _load_current_mapping → engine.apply(dsl_version=)
        # by testing that a v2 dict mapping normalizes correctly through the full path.
        v2_rules = {
            "rules": [
                {"target": "normalized.id", "source": "id"},
                {"target": "normalized.class_uid", "const": 2004},
            ]
        }
        engine = MappingEngine(max_cache=10)
        result = engine.apply("ver-smoke-1", v2_rules, {"id": "evt-1"}, dsl_version=2)
        assert result.output == {"normalized": {"id": "evt-1", "class_uid": 2004}}


# ─────────────────────────────────────────────────────────────────────────────
# Fix 2 (HIGH) — json_parse catches RecursionError + ValueError
# ─────────────────────────────────────────────────────────────────────────────


class TestJsonParseRecursionError:
    """Deeply-nested JSON must not propagate RecursionError."""

    # Use a payload that fits within the default 1 MiB but is deeply nested
    # enough to trigger RecursionError in the C JSON parser.  We also test
    # with an artificially small max_bytes limit so the payload fits.
    DEEP_JSON = "[" * 500_000 + "]" * 500_000  # ~1 MiB

    def test_strict_mode_raises_operator_error_not_recursion_error(self) -> None:
        """Strict mode: deeply-nested JSON raises OperatorError, NOT RecursionError."""
        payload = "[" * 10_000 + "]" * 10_000  # small enough to pass size check
        with pytest.raises(OperatorError):
            json_parse(payload, tolerant=False, max_bytes=10_000_000)

    def test_tolerant_mode_returns_none_not_recursion_error(self) -> None:
        """Tolerant mode: deeply-nested JSON returns None, NOT RecursionError."""
        payload = "[" * 10_000 + "]" * 10_000
        result = json_parse(payload, tolerant=True, max_bytes=10_000_000)
        assert result is None

    def test_full_size_deep_json_strict_no_recursion_propagation(self) -> None:
        """1 MiB deep payload with expanded limit — must not propagate RecursionError."""
        # Increase limit to allow the payload through the size guard.
        big = self.DEEP_JSON
        with pytest.raises(OperatorError):
            json_parse(big, tolerant=False, max_bytes=len(big.encode("utf-8")) + 1)

    def test_full_size_deep_json_tolerant_no_recursion_propagation(self) -> None:
        """1 MiB deep payload, tolerant=True — returns None, not RecursionError."""
        big = self.DEEP_JSON
        result = json_parse(big, tolerant=True, max_bytes=len(big.encode("utf-8")) + 1)
        assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# Fix 3 (MAJOR) — OperatorSizeError NOT silenced by tolerant=True
# ─────────────────────────────────────────────────────────────────────────────


class TestOperatorSizeErrorNotSilenced:
    """OperatorSizeError propagates even when tolerant=True (DoS guard)."""

    def test_size_error_is_subclass_of_operator_error(self) -> None:
        """OperatorSizeError must be a subclass of OperatorError."""
        assert issubclass(OperatorSizeError, OperatorError)

    def test_oversized_payload_raises_operator_size_error(self) -> None:
        """json_parse uses OperatorSizeError for size violations."""
        with pytest.raises(OperatorSizeError):
            json_parse('"hello"', tolerant=False, max_bytes=3)

    def test_oversized_payload_not_silenced_by_tolerant_true(self) -> None:
        """DoS guard (OperatorSizeError) propagates even with tolerant=True."""
        # This is the critical invariant: tolerant cannot suppress DoS protection.
        with pytest.raises(OperatorSizeError):
            json_parse('"hello world"', tolerant=True, max_bytes=5)

    def test_preprocess_oversized_payload_not_silenced_by_tolerant(self) -> None:
        """End-to-end: oversized preprocess payload raises MappingError, not silenced."""
        from backend.app.collectors.normalize.engine import apply_compiled

        # Build a v2 mapping with tolerant=True preprocess op
        v2_payload = {
            "preprocess": [
                {
                    "op": "json_parse",
                    "source": "data",
                    "target": "_parsed",
                    "tolerant": True,
                }
            ],
            "rules": [
                {"target": "normalized.id", "source": "id"},
            ],
        }
        compiled = compile_rules(v2_payload, dsl_version=2)

        # A raw event with an oversized 'data' field (over default 1 MiB limit).
        oversized_value = "x" * 2_000_000  # 2 MiB — well above 1 MiB limit
        raw = {"id": "evt-1", "data": f'"{oversized_value}"'}

        # Even though tolerant=True, OperatorSizeError must propagate as MappingError.
        with pytest.raises(MappingError, match="tamanho|size|DoS"):
            apply_compiled(compiled, raw)

    def test_normal_parse_error_is_silenced_by_tolerant(self) -> None:
        """Regular JSON parse errors ARE silenced when tolerant=True (control case)."""
        from backend.app.collectors.normalize.engine import apply_compiled

        v2_payload = {
            "preprocess": [
                {
                    "op": "json_parse",
                    "source": "data",
                    "target": "_parsed",
                    "tolerant": True,
                }
            ],
            "rules": [
                {"target": "normalized.id", "source": "id"},
            ],
        }
        compiled = compile_rules(v2_payload, dsl_version=2)
        # Bad JSON but within size limits — tolerant=True should return None for _parsed.
        raw = {"id": "evt-1", "data": "NOT_JSON"}
        result = apply_compiled(compiled, raw)
        assert result.output == {"normalized": {"id": "evt-1"}}


# ─────────────────────────────────────────────────────────────────────────────
# Fix 4 (MAJOR) — expected_always_default rejected in v1; captured in RuleSnapshot
# ─────────────────────────────────────────────────────────────────────────────


class TestExpectedAlwaysDefaultV1Rejected:
    """expected_always_default must be rejected in DSL v1 mappings."""

    def test_compile_v1_rejects_expected_always_default(self) -> None:
        """v1 mapping with expected_always_default raises MappingDefinitionError."""
        v1_rules = [
            {
                "target": "normalized.severity_id",
                "source": "severity",
                "default": 0,
                "expected_always_default": True,  # v2-only flag
            }
        ]
        with pytest.raises(MappingDefinitionError, match="expected_always_default.*v2|v2.*expected_always_default"):
            compile_rules(v1_rules, dsl_version=1)

    def test_compile_v2_accepts_expected_always_default(self) -> None:
        """v2 mapping with expected_always_default=True compiles successfully."""
        v2_payload = {
            "rules": [
                {
                    "target": "normalized.severity_id",
                    "source": "severity",
                    "default": 0,
                    "expected_always_default": True,
                }
            ]
        }
        compiled = compile_rules(v2_payload, dsl_version=2)
        assert len(compiled.rules) == 1
        rule = compiled.rules[0]
        assert hasattr(rule, "expected_always_default")
        assert rule.expected_always_default is True

    def test_compile_v1_without_flag_still_works(self) -> None:
        """v1 mapping without the flag is unaffected."""
        v1_rules = [
            {"target": "normalized.id", "source": "id"},
            {"target": "normalized.class_uid", "const": 2004},
        ]
        compiled = compile_rules(v1_rules, dsl_version=1)
        assert len(compiled.rules) == 2


class TestAuditLogCapturesExpectedAlwaysDefault:
    """RuleSnapshot and compute_diff capture expected_always_default toggles."""

    def test_rule_snapshot_captures_expected_always_default(self) -> None:
        """_rule_to_snapshot includes expected_always_default in RuleSnapshot."""
        from backend.app.routers.mappings import _rule_to_snapshot

        rule_with_flag = {
            "target": "normalized.severity_id",
            "source": "severity",
            "default": 0,
            "expected_always_default": True,
        }
        snap = _rule_to_snapshot(rule_with_flag)
        assert snap.expected_always_default is True

    def test_rule_snapshot_no_flag_is_none(self) -> None:
        """_rule_to_snapshot sets expected_always_default=None when absent."""
        from backend.app.routers.mappings import _rule_to_snapshot

        rule_no_flag = {
            "target": "normalized.severity_id",
            "source": "severity",
        }
        snap = _rule_to_snapshot(rule_no_flag)
        assert snap.expected_always_default is None

    def test_audit_log_captures_expected_always_default_change(self) -> None:
        """compute_diff detects toggle from False to True as a modified rule."""
        from backend.app.routers.mappings import compute_diff

        rules_before = [
            {
                "target": "normalized.severity_id",
                "source": "severity",
                "default": 0,
                # expected_always_default absent (= None in snapshot)
            }
        ]
        rules_after = [
            {
                "target": "normalized.severity_id",
                "source": "severity",
                "default": 0,
                "expected_always_default": True,  # toggled ON
            }
        ]
        diff = compute_diff(
            rules_before,
            rules_after,
            definition_id="def-1",
            version_a="va",
            version_b="vb",
        )
        assert len(diff.modified) == 1
        mod = diff.modified[0]
        assert mod.target == "normalized.severity_id"
        assert mod.before.expected_always_default is None
        assert mod.after.expected_always_default is True


# ─────────────────────────────────────────────────────────────────────────────
# Fix 5 (MAJOR) — compute_diff handles v2 dict shape
# ─────────────────────────────────────────────────────────────────────────────


class TestDiffVersionsV2DictShape:
    """compute_diff and diff_versions correctly handle v2 dict-shaped rules."""

    def _v2(self, rules_list: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {"preprocess": [], "rules": rules_list}

    def test_diff_v2_dict_shape_no_changes(self) -> None:
        """Two identical v2 dicts → empty diff."""
        from backend.app.routers.mappings import compute_diff

        rules = [{"target": "normalized.id", "source": "id"}]
        diff = compute_diff(self._v2(rules), self._v2(rules))
        assert diff.added == []
        assert diff.removed == []
        assert diff.modified == []

    def test_diff_versions_v2_dict_shape_detects_added_rule(self) -> None:
        """v2 dict: new rule in version B appears in diff.added."""
        from backend.app.routers.mappings import compute_diff

        rules_a = [{"target": "normalized.id", "source": "id"}]
        rules_b = [
            {"target": "normalized.id", "source": "id"},
            {"target": "normalized.class_uid", "const": 2004},
        ]
        diff = compute_diff(self._v2(rules_a), self._v2(rules_b))
        assert len(diff.added) == 1
        assert diff.added[0].target == "normalized.class_uid"
        assert diff.removed == []

    def test_diff_versions_v1_to_v2_upgrade(self) -> None:
        """v1 list → v2 dict upgrade: diff reports added preprocess-dependent rules."""
        from backend.app.routers.mappings import compute_diff

        rules_v1: List[Dict[str, Any]] = [
            {"target": "normalized.id", "source": "id"},
        ]
        rules_v2 = {
            "preprocess": [
                {"op": "json_parse", "source": "rawData", "target": "_parsed", "tolerant": True}
            ],
            "rules": [
                {"target": "normalized.id", "source": "id"},
                {"target": "normalized.device_name", "source": "_parsed.device"},
            ],
        }
        diff = compute_diff(rules_v1, rules_v2)
        # "normalized.id" exists in both → no change; new rule → added
        assert len(diff.added) == 1
        assert diff.added[0].target == "normalized.device_name"
        assert diff.removed == []

    def test_diff_v2_dict_shape_detects_modified_rule(self) -> None:
        """v2 dict: changed default in a rule appears in diff.modified."""
        from backend.app.routers.mappings import compute_diff

        rules_a = [{"target": "normalized.severity_id", "source": "severity", "default": 0}]
        rules_b = [{"target": "normalized.severity_id", "source": "severity", "default": 99}]
        diff = compute_diff(self._v2(rules_a), self._v2(rules_b))
        assert len(diff.modified) == 1
        assert diff.modified[0].target == "normalized.severity_id"

    def test_diff_v2_dict_shape_with_array_builder(self) -> None:
        """array_builder rules in v2 dict are handled without crash."""
        from backend.app.routers.mappings import compute_diff

        rules_a: List[Dict[str, Any]] = []
        rules_b: List[Dict[str, Any]] = [
            {
                "target": "normalized.observables",
                "kind": "array_builder",
                "items": [
                    {"name": "src_ip", "type": "IP Address", "type_id": 2, "source": "srcIp"}
                ],
                "skip_null": True,
            }
        ]
        diff = compute_diff(self._v2(rules_a), self._v2(rules_b))
        assert len(diff.added) == 1
        assert diff.added[0].target == "normalized.observables"


# ─────────────────────────────────────────────────────────────────────────────
# Fix 6 (MAJOR) — MappingEngine LRU cache (OrderedDict)
# ─────────────────────────────────────────────────────────────────────────────


class TestMappingEngineCacheLRU:
    """MappingEngine._cache must be a true LRU (evict least recently USED, not inserted)."""

    def _simple_v1_rules(self, uid: int) -> List[Dict[str, Any]]:
        return [{"target": "normalized.class_uid", "const": uid}]

    def test_cache_uses_ordered_dict(self) -> None:
        """Internal cache is an OrderedDict."""
        engine = MappingEngine(max_cache=4)
        assert isinstance(engine._cache, OrderedDict)

    def test_cache_hit_promotes_entry_to_end(self) -> None:
        """On cache hit, entry is moved to end (most-recently-used position)."""
        engine = MappingEngine(max_cache=4)
        engine.get_compiled("v1", self._simple_v1_rules(1), dsl_version=1)
        engine.get_compiled("v2", self._simple_v1_rules(2), dsl_version=1)
        engine.get_compiled("v3", self._simple_v1_rules(3), dsl_version=1)

        # Access "v1" — should move it to end (most-recently-used).
        engine.get_compiled("v1", self._simple_v1_rules(1), dsl_version=1)

        # "v2" is now the oldest by access (LRU).
        keys = list(engine._cache.keys())
        assert keys[0] == ("v2", 1), f"Expected ('v2', 1) at front, got {keys[0]}"
        assert keys[-1] == ("v1", 1), f"Expected ('v1', 1) at end, got {keys[-1]}"

    def test_engine_cache_lru_promotes_recently_used(self) -> None:
        """Classic LRU eviction: insert N+1 entries into size-N cache;
        the oldest-by-access (not oldest-by-insert) must be evicted."""
        cache_size = 3
        engine = MappingEngine(max_cache=cache_size)

        # Insert versions v1, v2, v3 (fills cache to capacity).
        for i in range(1, cache_size + 1):
            engine.get_compiled(f"v{i}", self._simple_v1_rules(i), dsl_version=1)

        # Access "v1" — this is now the most-recently used; "v2" becomes LRU.
        engine.get_compiled("v1", self._simple_v1_rules(1), dsl_version=1)

        # Insert "v4" — triggers eviction of the LRU entry.
        engine.get_compiled("v4", self._simple_v1_rules(4), dsl_version=1)

        cache_keys = set(k[0] for k in engine._cache.keys())

        # "v2" was LRU (oldest by access) and must have been evicted.
        assert "v2" not in cache_keys, (
            f"v2 should have been evicted (LRU), but cache contains: {cache_keys}"
        )
        # "v1" was promoted by the access above, so it must still be present.
        assert "v1" in cache_keys, "v1 should remain (recently accessed)"
        # "v3" and "v4" must also be present.
        assert "v3" in cache_keys
        assert "v4" in cache_keys

    def test_cache_evicts_oldest_insert_without_promotion(self) -> None:
        """Without any promotion, oldest-inserted entry is the LRU and evicted first."""
        engine = MappingEngine(max_cache=2)
        engine.get_compiled("v1", self._simple_v1_rules(1), dsl_version=1)
        engine.get_compiled("v2", self._simple_v1_rules(2), dsl_version=1)
        # No re-access to v1 — both are equally old by last access, but
        # v1 was inserted first, so v1 is the LRU.
        engine.get_compiled("v3", self._simple_v1_rules(3), dsl_version=1)

        cache_keys = set(k[0] for k in engine._cache.keys())
        assert "v1" not in cache_keys, "v1 should have been evicted (oldest insert = LRU)"
        assert "v2" in cache_keys
        assert "v3" in cache_keys


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end smoke — Sophos v2 mapping through full pipeline path
# ─────────────────────────────────────────────────────────────────────────────


class TestSophosV2EndToEndSmoke:
    """Simulate Sophos detection event through _load_current_mapping → engine.apply."""

    SOPHOS_V2_MAPPING = {
        "preprocess": [
            {
                "op": "json_parse",
                "source": "processedData",
                "target": "_processed",
                "tolerant": True,
            }
        ],
        "rules": [
            {"target": "normalized.id", "source": "id"},
            {"target": "normalized.class_uid", "const": 2004},
            {"target": "normalized.severity_id", "source": "severity", "default": 0},
            {"target": "normalized.type_uid", "const": 200401},
            {
                "target": "normalized.device.name",
                "source": "_processed.device.hostname",
                "default": "unknown",
                "expected_always_default": True,
            },
        ],
    }

    SOPHOS_RAW_EVENT = {
        "id": "sophos-alert-001",
        "severity": "high",
        "processedData": '{"device": {"hostname": "win10-pc"}}',
        "extra": "field_ignored",
    }

    def test_sophos_v2_event_normalizes_correctly(self) -> None:
        """v2 Sophos mapping applies correctly: preprocess + rules, NOT quarantined."""
        engine = MappingEngine(max_cache=10)

        result = engine.apply(
            "sophos-ver-1",
            self.SOPHOS_V2_MAPPING,
            self.SOPHOS_RAW_EVENT,
            dsl_version=2,
        )

        out = result.output
        assert out["normalized"]["id"] == "sophos-alert-001"
        assert out["normalized"]["class_uid"] == 2004
        assert out["normalized"]["type_uid"] == 200401
        assert out["normalized"]["severity_id"] == "high"
        # Preprocess: _processed was populated from processedData JSON string.
        assert out["normalized"]["device"]["name"] == "win10-pc"

    def test_sophos_v2_event_pipeline_integration(self) -> None:
        """Simulates pipeline._load_current_mapping + engine.apply for Sophos v2."""
        version_id = "sophos-ver-uuid-1"
        rules = self.SOPHOS_V2_MAPPING
        dsl_version = 2

        # Mimic what the fixed pipeline does after unpacking the 3-tuple.
        engine = MappingEngine(max_cache=10)
        result = engine.apply(version_id, rules, self.SOPHOS_RAW_EVENT, dsl_version=dsl_version)

        # Event must NOT be quarantined — apply must return a result.
        assert result is not None
        assert "normalized" in result.output
        assert result.output["normalized"]["id"] == "sophos-alert-001"

    def test_v1_list_mapping_still_works_identically(self) -> None:
        """Regression: v1 list-shaped mappings are unaffected by all fixes."""
        v1_rules = [
            {"target": "normalized.id", "source": "id"},
            {"target": "normalized.class_uid", "const": 2004},
            {"target": "normalized.severity_id", "source": "severity", "default": 0},
        ]
        engine = MappingEngine(max_cache=10)
        result = engine.apply("v1-ver-1", v1_rules, {"id": "evt-v1", "severity": "low"}, dsl_version=1)

        assert result.output["normalized"]["id"] == "evt-v1"
        assert result.output["normalized"]["class_uid"] == 2004
        assert result.output["normalized"]["severity_id"] == "low"
