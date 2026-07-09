"""Testes da matriz OCSF severity_id ↔ syslog PRI."""

from __future__ import annotations

import pytest

from ..normalize.severity_map import (
    OCSF_TO_SYSLOG_SEVERITY,
    PRI_DEFAULT,
    pri_for_event,
    pri_for_severity_id,
)


@pytest.mark.parametrize("sid,expected_pri", [
    (0, 134),
    (1, 134),
    (2, 133),
    (3, 132),
    (4, 131),
    (5, 130),
    (6, 128),
    (99, 134),
])
def test_pri_for_severity_id_matrix(sid: int, expected_pri: int) -> None:
    assert pri_for_severity_id(sid) == expected_pri


def test_pri_for_severity_id_none_returns_default() -> None:
    assert pri_for_severity_id(None) == 134


def test_pri_for_severity_id_invalid_string_returns_default() -> None:
    assert pri_for_severity_id("nope") == 134  # type: ignore[arg-type]


def test_pri_for_severity_id_negative_returns_default() -> None:
    # -1 is not in the mapping, so returns PRI_DEFAULT
    assert pri_for_severity_id(-1) == 134


def test_pri_for_severity_id_unknown_id_returns_default() -> None:
    assert pri_for_severity_id(999) == 134


def test_pri_for_event_extracts_from_normalized() -> None:
    event = {"normalized": {"severity_id": 5}}
    assert pri_for_event(event) == 130


def test_pri_for_event_critical_is_130() -> None:
    event = {"_centralops": {"vendor": "centralops"}, "normalized": {"severity_id": 5}, "raw": {}}
    assert pri_for_event(event) == 130


def test_pri_for_event_high_is_131() -> None:
    event = {"normalized": {"severity_id": 4}}
    assert pri_for_event(event) == 131


def test_pri_for_event_missing_severity_returns_default() -> None:
    assert pri_for_event({"normalized": {}}) == 134


def test_pri_for_event_empty_event_returns_default() -> None:
    assert pri_for_event({}) == 134


def test_pri_for_event_no_normalized_key_returns_default() -> None:
    assert pri_for_event({"raw": {"foo": "bar"}}) == 134


def test_pri_for_event_non_mapping_returns_default() -> None:
    # Non-mapping input should return PRI_DEFAULT safely
    assert pri_for_event("not a dict") == 134  # type: ignore[arg-type]
    assert pri_for_event(None) == 134  # type: ignore[arg-type]


def test_pri_default_value() -> None:
    assert PRI_DEFAULT == 134


def test_ocsf_to_syslog_severity_covers_all_ids() -> None:
    """Todos os severity_ids conhecidos do OCSF estão mapeados."""
    known_ids = {0, 1, 2, 3, 4, 5, 6, 99}
    assert set(OCSF_TO_SYSLOG_SEVERITY.keys()) == known_ids
