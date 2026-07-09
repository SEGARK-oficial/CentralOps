"""Sanidade do subset OCSF v1.3 usado pelo CentralOps."""

from __future__ import annotations

import re

from backend.app.collectors.normalize import (
    ENVELOPE_SCHEMA_VERSION,
    OCSF_VERSION,
)
from backend.app.collectors.normalize.defaults import (
    DEFAULT_MAPPING_FILES,
    load_default_rules,
)
from backend.app.collectors.normalize.ocsf import (
    ALLOWED_CLASS_UIDS,
    CLASS_UID_ACCOUNT_CHANGE,
    CLASS_UID_API_ACTIVITY,
    CLASS_UID_AUTHENTICATION,
    CLASS_UID_DETECTION_FINDING,
    CLASS_UID_INCIDENT_FINDING,
    CLASS_UID_NETWORK_ACTIVITY,
    SEVERITY_ID,
    STATUS_ID,
    class_name_for,
    is_valid_class_uid,
    is_valid_severity_id,
)


SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def test_envelope_and_ocsf_versions_are_semver() -> None:
    assert SEMVER_RE.match(ENVELOPE_SCHEMA_VERSION)
    assert SEMVER_RE.match(OCSF_VERSION)
    # Bumped 1.3.0 → 1.8.0 (the version the vendored validator manifest
    # targets and the classes the default mappings emit).
    assert OCSF_VERSION == "1.8.0"


def test_allowed_class_uids_cover_all_five_streams() -> None:
    # Cada (vendor, event_type) seedado em ``mapping_definitions`` precisa
    # ter o class_uid presente no subset suportado, senão o engine vai
    # rejeitar o envelope produzido.
    expected = {
        CLASS_UID_DETECTION_FINDING,  # sophos.alert, defender.alert
        CLASS_UID_INCIDENT_FINDING,   # sophos.case, defender.incident
        CLASS_UID_API_ACTIVITY,       # ninjaone.activity
    }
    assert expected.issubset(ALLOWED_CLASS_UIDS)


def _rule_list(dsl: object) -> list:
    """Extrai a lista de regras de uma DSL v1 (list) ou v2 (dict com 'rules')."""
    if isinstance(dsl, list):
        return dsl
    if isinstance(dsl, dict):
        rules = dsl.get("rules")
        return rules if isinstance(rules, list) else []
    return []


def _emitted_class_uid(dsl: object) -> object:
    """class_uid ``const`` que o mapping emite (v1 e v2), ou ``None``."""
    for rule in _rule_list(dsl):
        if isinstance(rule, dict) and rule.get("target") == "normalized.class_uid":
            return rule.get("const")
    return None


def test_allowed_class_uids_cover_all_default_mappings() -> None:
    """TODA class_uid emitida por um mapping
    default DEVE estar em ALLOWED_CLASS_UIDS. Se um default novo emitir uma classe
    ausente aqui, uma futura validação OCSF fail-closed quarentenaria 100% desse
    stream (uma dessincronia real já ocorreu: os defaults
    emitiam 3001/3002/4001 mas ALLOWED só tinha 2004/2005/6003)."""
    missing: dict[str, object] = {}
    for (vendor, event_type) in DEFAULT_MAPPING_FILES:
        dsl = load_default_rules(vendor, event_type)
        uid = _emitted_class_uid(dsl)
        assert uid is not None, (
            f"{vendor}/{event_type}: mapping default não emite um class_uid const"
        )
        if uid not in ALLOWED_CLASS_UIDS:
            missing[f"{vendor}/{event_type}"] = uid
    assert not missing, (
        f"class_uid emitido por mapping default mas AUSENTE de ALLOWED_CLASS_UIDS "
        f"(adicione em ocsf/classes.py CLASS_NAMES): {missing}"
    )


def test_iam_and_network_classes_resolve() -> None:
    # Classes emitidas por entra.audit (3001), entra.signin /
    # okta / windows (3002) e fortigate (4001) — antes ausentes de CLASS_NAMES.
    assert class_name_for(CLASS_UID_ACCOUNT_CHANGE) == "Account Change"
    assert class_name_for(CLASS_UID_AUTHENTICATION) == "Authentication"
    assert class_name_for(CLASS_UID_NETWORK_ACTIVITY) == "Network Activity"
    for uid in (
        CLASS_UID_ACCOUNT_CHANGE,
        CLASS_UID_AUTHENTICATION,
        CLASS_UID_NETWORK_ACTIVITY,
    ):
        assert is_valid_class_uid(uid), uid


def test_class_name_for_known_and_unknown() -> None:
    assert class_name_for(CLASS_UID_DETECTION_FINDING) == "Detection Finding"
    assert class_name_for(CLASS_UID_INCIDENT_FINDING) == "Incident Finding"
    assert class_name_for(CLASS_UID_API_ACTIVITY) == "API Activity"
    assert class_name_for(9999) == ""


def test_severity_id_universal_keys_present() -> None:
    # OCSF spec: severity_id 0..6 + 99. Mapping deve cobrir todos.
    expected_values = {0, 1, 2, 3, 4, 5, 6, 99}
    assert set(SEVERITY_ID.values()) == expected_values
    assert SEVERITY_ID["critical"] == 5
    assert SEVERITY_ID["informational"] == 1


def test_is_valid_helpers() -> None:
    assert is_valid_class_uid(CLASS_UID_DETECTION_FINDING)
    assert not is_valid_class_uid(0)
    assert not is_valid_class_uid(9999)

    assert is_valid_severity_id(5)
    assert is_valid_severity_id(99)
    assert not is_valid_severity_id(7)
    assert not is_valid_severity_id(-1)


def test_status_id_finding_lifecycle() -> None:
    # Status semântico de Detection/Incident Finding — required para que
    # o engine produza envelope válido.
    assert STATUS_ID["new"] == 1
    assert STATUS_ID["resolved"] == 4
    assert STATUS_ID["unknown"] == 0
