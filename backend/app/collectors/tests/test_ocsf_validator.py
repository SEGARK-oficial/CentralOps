"""Structural OCSF validator (tier-1 gate).

Covers: manifest↔classes.py consistency (single source of truth), the full
gate matrix (one case per closed-enum reason), identity arithmetic, the
out_of_scope vs unknown_class distinction, advisory required-coverage, and the
critical invariant that NO default mapping is flagged invalid.

Imports use ``backend.app.*`` (compiled .so dual-root gotcha).
"""

from __future__ import annotations

import json
import re
from importlib import resources
from pathlib import Path

import pytest

from backend.app.collectors.normalize.defaults import (
    DEFAULT_MAPPING_FILES,
    load_default_rules,
)
from backend.app.collectors.normalize.ocsf import (
    ALLOWED_CLASS_UIDS,
    SEVERITY_ID,
)
from backend.app.collectors.normalize.ocsf.classes import CLASS_NAMES
from backend.app.collectors.normalize.ocsf import validator as V


# ── source of truth: manifest ↔ classes.py ────────────────────────────────────

def test_manifest_matches_classes_py() -> None:
    """The vendored manifest and the hand-written ocsf/classes.py must agree, so
    the two sources of truth cannot silently drift."""
    reg = V.get_registry("1.8.0")
    assert reg.class_uids == ALLOWED_CLASS_UIDS
    for uid in reg.class_uids:
        assert reg.spec_for(uid).name == CLASS_NAMES[uid], uid
        # class_uid == category_uid * 1000 + index  (identity invariant)
        assert reg.spec_for(uid).category_uid == uid // 1000, uid


def test_validator_severity_enum_matches_ocsf() -> None:
    assert V._SEVERITY_IDS == set(SEVERITY_ID.values())


def test_registry_is_immutable_and_cached() -> None:
    assert V.get_registry("1.8.0") is V.get_registry("1.8.0")  # lru_cache
    reg = V.get_registry("1.8.0")
    assert not hasattr(reg, "__dict__")  # __slots__ → no attribute injection


# ── identity arithmetic ───────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "class_uid,activity_id,expected",
    [(2004, 1, 200401), (3002, 1, 300201), (4001, 6, 400106), (6003, 2, 600302)],
)
def test_derive_type_uid(class_uid: int, activity_id: int, expected: int) -> None:
    assert V.derive_type_uid(class_uid, activity_id) == expected


# ── gate matrix: one case per closed-enum reason ──────────────────────────────

def _valid_authn() -> dict:
    # Authentication (3002) / Logon (1) / type 300201 / medium severity
    return {
        "class_uid": 3002, "category_uid": 3, "activity_id": 1,
        "type_uid": 300201, "severity_id": 3, "user": {"name": "alice"},
    }


def test_gate_valid_event() -> None:
    r = V.validate_normalized(_valid_authn())
    assert r.valid and r.in_scope and r.reason == V.REASON_OK
    assert r.type_uid == 300201 and r.class_name == "Authentication"
    assert r.missing_required == ()


def test_gate_unknown_class_when_missing() -> None:
    for bad in ({}, {"class_uid": None}, {"class_uid": 0}, {"class_uid": -5}, {"class_uid": "3002"}, {"class_uid": True}):
        r = V.validate_normalized(bad)
        assert not r.valid and r.in_scope and r.reason == V.REASON_UNKNOWN_CLASS, bad


def test_gate_out_of_scope_for_unvendored_class() -> None:
    # 1001 (Process Activity) is a real OCSF class we do not vendor → graceful.
    r = V.validate_normalized({"class_uid": 1001, "category_uid": 1, "activity_id": 1, "type_uid": 100101, "severity_id": 1})
    assert not r.in_scope and r.reason == V.REASON_OUT_OF_SCOPE and not r.valid


def test_gate_category_mismatch() -> None:
    ev = _valid_authn() | {"category_uid": 9}
    r = V.validate_normalized(ev)
    assert not r.valid and r.reason == V.REASON_CATEGORY_MISMATCH


def test_gate_bad_severity() -> None:
    for bad in (7, -1, None, 100):
        r = V.validate_normalized(_valid_authn() | {"severity_id": bad})
        assert not r.valid and r.reason == V.REASON_BAD_SEVERITY_ID, bad


def test_gate_bad_activity_id() -> None:
    r = V.validate_normalized(_valid_authn() | {"activity_id": 50, "type_uid": 300250})
    assert not r.valid and r.reason == V.REASON_BAD_ACTIVITY_ID


def test_gate_type_uid_mismatch() -> None:
    r = V.validate_normalized(_valid_authn() | {"type_uid": 999999})
    assert not r.valid and r.reason == V.REASON_TYPE_UID_MISMATCH


def test_gate_status_id_conditional() -> None:
    # absent status_id is fine
    assert V.validate_normalized(_valid_authn()).valid
    # present-and-valid (Success=1) is fine
    assert V.validate_normalized(_valid_authn() | {"status_id": 1}).valid
    # present-and-invalid fails
    r = V.validate_normalized(_valid_authn() | {"status_id": 88})
    assert not r.valid and r.reason == V.REASON_BAD_STATUS_ID


def test_gate_required_coverage_is_advisory_not_fatal() -> None:
    # 3002 requires 'user' (content). Missing it must NOT flip valid,
    # only surface in missing_required.
    ev = _valid_authn()
    del ev["user"]
    r = V.validate_normalized(ev)
    assert r.valid, "missing content-required must not fail the event"
    assert "user" in r.missing_required


@pytest.mark.parametrize(
    "field,badvalue,reason",
    [
        ("class_uid", [1], V.REASON_UNKNOWN_CLASS),
        ("severity_id", {}, V.REASON_BAD_SEVERITY_ID),
        ("severity_id", "high", V.REASON_BAD_SEVERITY_ID),
        ("activity_id", [1], V.REASON_BAD_ACTIVITY_ID),
        ("status_id", [1], V.REASON_BAD_STATUS_ID),
    ],
)
def test_gate_is_type_safe_never_crashes(field, badvalue, reason) -> None:
    """A malformed mapping output where an enum field is a non-int / unhashable
    value must be flagged INVALID, never crash (``x in frozenset`` raises TypeError
    on an unhashable value — and this gate runs on the hot path). Regression: an
    E2E mapping save 500'd on exactly this."""
    ev = {"class_uid": 2004, "category_uid": 2, "activity_id": 1, "type_uid": 200401,
          "severity_id": 3, "status_id": 1, field: badvalue}
    r = V.validate_normalized(ev)
    assert not r.valid and r.reason == reason


def test_bool_is_not_a_valid_ocsf_int() -> None:
    # bool is an int subclass; True must NOT satisfy severity_id==1 silently.
    r = V.validate_normalized(
        {"class_uid": 3002, "category_uid": 3, "activity_id": 1, "type_uid": 300201, "severity_id": True}
    )
    assert not r.valid and r.reason == V.REASON_BAD_SEVERITY_ID


def test_all_reasons_are_in_the_closed_enum() -> None:
    samples = [{}, {"class_uid": 1001}, _valid_authn() | {"category_uid": 9},
               _valid_authn() | {"severity_id": 7}, _valid_authn() | {"type_uid": 1}, _valid_authn()]
    for ev in samples:
        assert V.validate_normalized(ev).reason in V.OCSF_REASONS


def test_reason_constants_never_carry_a_value() -> None:
    """reason is a metric label + wire tag → must be a static token, never an
    interpolated event value (anti-PII, bounded cardinality)."""
    for reason in V.OCSF_REASONS:
        assert re.fullmatch(r"[a-z_]+", reason), reason


# ── the guarantee: no default mapping is structurally invalid ──────────

def _const_identity(dsl: object) -> dict:
    """Build a minimal normalized event from a default mapping's ``const`` identity
    rules (class_uid/category_uid/type_uid/activity_id/severity_id)."""
    rules = dsl.get("rules") if isinstance(dsl, dict) else dsl
    out: dict = {}
    for rule in rules or []:
        if not isinstance(rule, dict):
            continue
        tgt = rule.get("target", "")
        if tgt.startswith("normalized.") and "const" in rule:
            out[tgt[len("normalized."):]] = rule["const"]
    return out


@pytest.mark.parametrize("vendor,event_type", sorted(DEFAULT_MAPPING_FILES))
def test_no_default_mapping_is_structurally_invalid(vendor: str, event_type: str) -> None:
    """Every seeded default mapping's emitted OCSF identity must pass the structural
    gate — i.e. turning validation ON would quarantine ZERO defaults."""
    ident = _const_identity(load_default_rules(vendor, event_type))
    # severity_id is 'src'-mapped on some vendors (resolved at runtime); inject a
    # valid runtime value so the test isolates the IDENTITY invariants.
    ident.setdefault("severity_id", 1)
    r = V.validate_normalized(ident)
    assert r.valid, f"{vendor}/{event_type} → {r.reason} (identity {ident})"


# ── manifest hygiene ──────────────────────────────────────────────────────────

def test_pipeline_hook_delegates_the_drop_decision_to_policy() -> None:
    """The hot-path drop/keep decision must go through ``ocsf_policy.decide`` (which
    is exhaustively unit-tested) rather than ad-hoc branching, so the tag_and_pass=
    never-drop invariant lives in one tested place. Guards against a future edit
    re-inlining the decision with a wrong ``continue``.

    Source-level guard: it inspects ``pipeline.py``. In the Cython-compiled image the
    module ships as a ``.so`` with no ``.py`` source, so there is nothing to inspect —
    skip there (the guard still runs on every non-compiled dev/CI sweep)."""
    src_path = Path(__file__).resolve().parents[2] / "collectors" / "pipeline.py"
    if not src_path.exists():
        pytest.skip("pipeline.py source absent (compiled .so image) — nothing to inspect")
    src = src_path.read_text("utf-8")
    marker = "validação OCSF (structural gate"
    assert marker in src, "OCSF hook missing from pipeline.py"
    start = src.index(marker)
    end = src.index("customer_id obrigatório", start)
    block = src[start:end]
    assert "settings.OCSF_VALIDATION_ENABLED" in block, "hook must be flag-guarded"
    assert "ocsf_policy.decide(" in block, "hook must delegate the action to decide()"


def test_manifest_activity_and_type_uid_identity_hold() -> None:
    """For every class, 0/99 are present and type_uid==class*100+activity is derivable."""
    raw = json.loads(
        resources.files("backend.app.collectors.normalize.ocsf")
        .joinpath("schemas", "1.8.0", "manifest.json")
        .read_text("utf-8")
    )
    for uid_str, entry in raw["classes"].items():
        acts = {int(k) for k in entry["activity_ids"]}
        assert 0 in acts and 99 in acts, uid_str  # Unknown + Other always present
        assert int(uid_str) // 1000 == entry["category_uid"], uid_str
