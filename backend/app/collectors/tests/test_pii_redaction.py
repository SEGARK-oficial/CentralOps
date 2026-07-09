"""Testes do módulo puro de redação de PII por rota."""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest

from backend.app.collectors.routing.pii_redaction import (
    PiiRedactionError,
    apply_pii_redaction,
    compile_pii_redaction,
    validate_pii_redaction,
)


def _env() -> dict:
    return {
        "_centralops": {"event_id": "e1", "organization_id": 7},
        "normalized": {"actor": {"name": "Alice"}},
        "raw": {
            "user": {"email": "alice@example.com"},
            "src": {"ip": "203.0.113.5"},
            "body": {"ssn": "123-45-6789"},
            "headers": {"cookie": "session=abc"},
        },
    }


# ── compile / validation (FAIL-CLOSED at write) ─────────────────────────


def test_bare_list_is_version_1():
    rules = compile_pii_redaction([{"path": "raw.user.email", "action": "mask"}])
    assert len(rules) == 1
    assert rules[0].action == "mask"


def test_versioned_object_form():
    spec = {"version": 1, "rules": [{"path": "raw.src.ip", "action": "partial", "octets": 2}]}
    rules = compile_pii_redaction(spec)
    assert rules[0].params["octets"] == 2


def test_none_and_empty_compile_to_nothing():
    assert compile_pii_redaction(None) == ()
    assert compile_pii_redaction([]) == ()
    assert compile_pii_redaction({"version": 1, "rules": []}) == ()


def test_reject_path_not_rooted_at_allowlist():
    with pytest.raises(PiiRedactionError):
        compile_pii_redaction([{"path": "data.x", "action": "mask"}])


def test_reject_centralops_target():
    """_centralops carries event_id/org_id — redacting it would break routing/
    idempotency/audit. MUST be forbidden."""
    with pytest.raises(PiiRedactionError):
        compile_pii_redaction([{"path": "_centralops.event_id", "action": "drop_field"}])


def test_reject_bare_root_without_field():
    with pytest.raises(PiiRedactionError):
        compile_pii_redaction([{"path": "raw", "action": "mask"}])


def test_reject_unknown_action():
    with pytest.raises(PiiRedactionError):
        compile_pii_redaction([{"path": "raw.x", "action": "encrypt"}])


def test_reject_partial_without_disclosure_mode():
    with pytest.raises(PiiRedactionError):
        compile_pii_redaction([{"path": "raw.x", "action": "partial"}])


def test_reject_bad_fixed_len_and_octets():
    with pytest.raises(PiiRedactionError):
        compile_pii_redaction([{"path": "raw.x", "action": "mask", "fixed_len": 0}])
    with pytest.raises(PiiRedactionError):
        compile_pii_redaction([{"path": "raw.x", "action": "partial", "octets": -1}])


def test_validate_helper_raises_value_error():
    with pytest.raises(ValueError):
        validate_pii_redaction([{"path": "evil._centralops", "action": "mask"}])


# ── apply: strategies ───────────────────────────────────────────────────


def test_mask_replaces_with_sentinel():
    rules = compile_pii_redaction([{"path": "raw.user.email", "action": "mask"}])
    out = apply_pii_redaction(_env(), rules)
    assert out["raw"]["user"]["email"] == "[REDACTED]"
    assert out["_centralops_redacted"] == ["raw.user.email:mask"]


def test_mask_fixed_len_constant_width_hides_length():
    rules = compile_pii_redaction(
        [{"path": "raw.headers.cookie", "action": "mask", "fixed_len": 8, "mask_char": "*"}]
    )
    out = apply_pii_redaction(_env(), rules)
    assert out["raw"]["headers"]["cookie"] == "********"


def test_mask_whole_subtree_when_target_is_dict():
    """Masking a dict masks the WHOLE subtree to the sentinel (never partial-
    recurse — that is a leak vector)."""
    rules = compile_pii_redaction([{"path": "raw.user", "action": "mask"}])
    out = apply_pii_redaction(_env(), rules)
    assert out["raw"]["user"] == "[REDACTED]"


def test_hash_is_deterministic_pseudonym():
    rules = compile_pii_redaction([{"path": "raw.user.email", "action": "hash"}])
    a = apply_pii_redaction(_env(), rules)
    b = apply_pii_redaction(_env(), rules)
    assert a["raw"]["user"]["email"].startswith("sha256:")
    assert a["raw"]["user"]["email"] == b["raw"]["user"]["email"]  # determinístico


def test_hash_salt_changes_digest():
    r1 = compile_pii_redaction([{"path": "raw.user.email", "action": "hash"}])
    r2 = compile_pii_redaction([{"path": "raw.user.email", "action": "hash", "salt": "pepper"}])
    assert apply_pii_redaction(_env(), r1)["raw"]["user"]["email"] != \
        apply_pii_redaction(_env(), r2)["raw"]["user"]["email"]


def test_hash_is_idempotent_no_double_hash():
    """Re-applying hash to an already-hashed value (sha256: prefix) is a no-op —
    guards against double-hashing under retry."""
    rules = compile_pii_redaction([{"path": "raw.user.email", "action": "hash"}])
    once = apply_pii_redaction(_env(), rules)
    twice = apply_pii_redaction(once, rules)
    # second pass finds an already-hashed value → unchanged (no 'redacted' marker churn)
    assert twice is None or twice["raw"]["user"]["email"] == once["raw"]["user"]["email"]


def test_partial_ip_keeps_leading_octets():
    rules = compile_pii_redaction([{"path": "raw.src.ip", "action": "partial", "octets": 2}])
    out = apply_pii_redaction(_env(), rules)
    assert out["raw"]["src"]["ip"] == "203.0.*.*"


def test_partial_string_keep_prefix_suffix():
    env = {"raw": {"card": "4111111111111234"}}
    rules = compile_pii_redaction(
        [{"path": "raw.card", "action": "partial", "keep_suffix": 4}]
    )
    out = apply_pii_redaction(env, rules)
    assert out["raw"]["card"].endswith("1234")
    assert out["raw"]["card"].startswith("*")
    assert len(out["raw"]["card"]) == len("4111111111111234")


def test_partial_fail_closed_when_would_disclose_all():
    """keep >= len → FULL mask (never disclose everything)."""
    env = {"raw": {"x": "ab"}}
    rules = compile_pii_redaction(
        [{"path": "raw.x", "action": "partial", "keep_prefix": 5}]
    )
    out = apply_pii_redaction(env, rules)
    assert out["raw"]["x"] == "[REDACTED]"


def test_partial_fail_closed_on_wrong_type():
    env = {"raw": {"x": 12345}}
    rules = compile_pii_redaction([{"path": "raw.x", "action": "partial", "octets": 2}])
    out = apply_pii_redaction(env, rules)
    assert out["raw"]["x"] == "[REDACTED]"


def test_drop_field_removes_key():
    rules = compile_pii_redaction([{"path": "raw.body.ssn", "action": "drop_field"}])
    out = apply_pii_redaction(_env(), rules)
    assert "ssn" not in out["raw"]["body"]  # key GONE, not null


# ── isolation / no-op / multi-rule ──────────────────────────────────────


def test_returns_none_when_nothing_matched():
    """Absent path → None → caller reuses original (no deepcopy cost, preserves
    byte-identity of the non-redacted branch)."""
    rules = compile_pii_redaction([{"path": "raw.nonexistent.field", "action": "mask"}])
    assert apply_pii_redaction(_env(), rules) is None


def test_does_not_mutate_input_envelope():
    """COPY ISOLATION: the source envelope is untouched (the fan-out lake/wazuh
    copy must stay byte-identical)."""
    env = _env()
    rules = compile_pii_redaction([{"path": "raw.user.email", "action": "mask"}])
    out = apply_pii_redaction(env, rules)
    assert env["raw"]["user"]["email"] == "alice@example.com"  # original intact
    assert out["raw"]["user"]["email"] == "[REDACTED]"
    assert out is not env


def test_multi_rule_all_applied_and_marked():
    rules = compile_pii_redaction([
        {"path": "raw.user.email", "action": "mask"},
        {"path": "raw.src.ip", "action": "partial", "octets": 2},
        {"path": "raw.body.ssn", "action": "drop_field"},
    ])
    out = apply_pii_redaction(_env(), rules)
    assert out["raw"]["user"]["email"] == "[REDACTED]"
    assert out["raw"]["src"]["ip"] == "203.0.*.*"
    assert "ssn" not in out["raw"]["body"]
    assert set(out["_centralops_redacted"]) == {
        "raw.user.email:mask", "raw.src.ip:partial", "raw.body.ssn:drop_field"
    }


def test_centralops_namespace_never_touched():
    rules = compile_pii_redaction([{"path": "raw.user.email", "action": "mask"}])
    out = apply_pii_redaction(_env(), rules)
    assert out["_centralops"] == {"event_id": "e1", "organization_id": 7}


# ── list-nested path → FAIL-CLOSED ───────────────


def test_list_nested_path_fail_closed_masks_whole_branch():
    """Path que entra numa LISTA (não navegável) NÃO passa em claro — mascara a
    subárvore inteira (PII pode estar dentro da lista)."""
    env = {"raw": {"users": [{"email": "a@b.com"}, {"email": "c@d.com"}]}}
    rules = compile_pii_redaction([{"path": "raw.users.email", "action": "mask"}])
    out = apply_pii_redaction(env, rules)
    assert out is not None
    assert out["raw"]["users"] == "[REDACTED]"  # lista inteira mascarada
    assert any("blocked" in m for m in out["_centralops_redacted"])


def test_list_nested_drop_field_removes_list():
    env = {"raw": {"items": [{"ssn": "1"}]}}
    rules = compile_pii_redaction([{"path": "raw.items.ssn", "action": "drop_field"}])
    out = apply_pii_redaction(env, rules)
    assert "items" not in out["raw"]


def test_genuinely_absent_path_is_noop_not_masked():
    """Campo realmente AUSENTE (não lista) → None (reusa original), não mascara
    nada espúrio."""
    env = {"raw": {"user": {"name": "x"}}}
    rules = compile_pii_redaction([{"path": "raw.user.email", "action": "mask"}])
    assert apply_pii_redaction(env, rules) is None
