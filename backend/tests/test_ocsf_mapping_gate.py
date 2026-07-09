"""Commit-time OCSF gate helpers in routers/mappings.

Pure-function coverage of the shift-left analysis (emitted class_uid extraction +
dry-run output validation + declared-vs-emitted cross-check + blocking flag).
Imports use ``backend.app.*`` (compiled .so dual-root gotcha).
"""

from __future__ import annotations

from backend.app.routers import mappings as M


def _v2(class_uid: int = 3002) -> dict:
    return {
        "preprocess": [],
        "rules": [
            {"target": "normalized.class_uid", "const": class_uid},
            {"target": "normalized.time", "source": "t"},
        ],
    }


def _out(**over) -> dict:
    base = {
        "class_uid": 3002, "category_uid": 3, "activity_id": 1,
        "type_uid": 300201, "severity_id": 1, "user": {"name": "x"},
    }
    base.update(over)
    return {"normalized": base}


def test_emitted_class_uid_v1_and_v2() -> None:
    assert M._emitted_class_uid(_v2(4001)) == 4001
    # v1 (bare list)
    assert M._emitted_class_uid([{"target": "normalized.class_uid", "const": 2004}]) == 2004
    # no const class_uid → None
    assert M._emitted_class_uid({"rules": [{"target": "normalized.time", "source": "t"}]}) is None


def test_gate_valid_matching_declared_is_not_blocking() -> None:
    stats = M._ocsf_validate_commit(_v2(3002), [_out()], declared_class_uid=3002)
    assert stats["checked"] == 1 and stats["valid"] == 1
    assert stats["invalid_by_reason"] == {}
    assert stats["class_uid_mismatch"] is False
    assert stats["blocking"] is False


def test_gate_declared_vs_emitted_mismatch_blocks() -> None:
    stats = M._ocsf_validate_commit(_v2(3002), [_out()], declared_class_uid=4001)
    assert stats["class_uid_emitted"] == 3002 and stats["class_uid_declared"] == 4001
    assert stats["class_uid_mismatch"] is True and stats["blocking"] is True


def test_gate_invalid_output_blocks() -> None:
    stats = M._ocsf_validate_commit(_v2(3002), [_out(severity_id=7)], declared_class_uid=3002)
    assert stats["invalid_by_reason"] == {"bad_severity_id": 1}
    assert stats["blocking"] is True


def test_gate_out_of_scope_is_not_blocking() -> None:
    # class 1001 is a valid OCSF class we don't vendor → graceful, not a hard defect
    oos = {"normalized": {"class_uid": 1001, "category_uid": 1, "activity_id": 1,
                          "type_uid": 100101, "severity_id": 1}}
    stats = M._ocsf_validate_commit({"rules": []}, [oos], declared_class_uid=None)
    assert stats["invalid_by_reason"] == {"out_of_scope": 1}
    assert stats["blocking"] is False  # out_of_scope alone must not block a commit
