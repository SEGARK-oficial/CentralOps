"""Serialização de export de captura (CSV / NDJSON) + máscara de PII."""
from __future__ import annotations

import json

from backend.app.collectors import capture_export as ex


def _entry(**over):
    base = {
        "event": {
            "raw": {"srcuser": "svc_backup", "src_ip": "10.0.0.9", "eventID": "4624"},
            "_centralops": {"organization_id": 7, "vendor": "sophos"},
        },
        "vendor": "sophos",
        "captured_at": 1_714_000_100,
        "outcome": "dropped",
        "route_id": "r-noise",
        "destination_id": "d-splunk",
    }
    base.update(over)
    return base


def test_csv_starts_with_bom_for_excel():
    out = "".join(ex.iter_csv([_entry()]))
    assert out.startswith("﻿")


def test_csv_separator_follows_locale():
    assert ex.csv_separator_for_locale("pt-BR") == ";"
    assert ex.csv_separator_for_locale("es") == ";"
    assert ex.csv_separator_for_locale("en-US") == ","
    assert ex.csv_separator_for_locale(None) == ","


def test_csv_masks_pii_by_default():
    out = "".join(ex.iter_csv([_entry()], separator=";"))
    assert "svc_backup" not in out
    assert "10.0.0.9" not in out
    assert "[PII]" in out
    # campo não-PII sobrevive
    assert "4624" in out


def test_csv_can_disable_mask():
    out = "".join(ex.iter_csv([_entry()], mask=False))
    assert "svc_backup" in out


def test_csv_has_route_and_outcome_columns():
    out = "".join(ex.iter_csv([_entry()], separator=";"))
    header = out.splitlines()[0].lstrip("﻿")
    assert "route_id" in header and "outcome" in header
    # a linha traz a rota que dropou
    assert "r-noise" in out


def test_ndjson_one_line_per_event_and_masks():
    lines = [l for l in "".join(ex.iter_ndjson([_entry(), _entry()])).splitlines() if l]
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["outcome"] == "dropped"
    assert rec["route_id"] == "r-noise"
    assert rec["event"]["raw"]["srcuser"] == "[PII]"
    assert rec["event"]["raw"]["eventID"] == "4624"


def test_csv_signals_truncation_in_the_body():
    entries = [_entry() for _ in range(5)]
    out = "".join(ex.iter_csv(entries, max_rows=2))
    assert "truncated" in out
    # 1 BOM/header + 2 linhas + 1 aviso
    data_lines = [l for l in out.splitlines() if l and not l.startswith("captured_at") and not l.startswith("﻿captured_at")]
    assert any("truncated" in l for l in data_lines)


def test_ndjson_signals_truncation():
    entries = [_entry() for _ in range(5)]
    lines = [l for l in "".join(ex.iter_ndjson(entries, max_rows=2)).splitlines() if l]
    assert json.loads(lines[-1]).get("__truncated__") is True


def test_mask_pii_is_recursive_and_non_mutating():
    original = {"a": {"user": "alice", "keep": 1}, "list": [{"ip": "1.2.3.4"}]}
    masked = ex.mask_pii(original)
    assert masked["a"]["user"] == "[PII]"
    assert masked["a"]["keep"] == 1
    assert masked["list"][0]["ip"] == "[PII]"
    # original intacto
    assert original["a"]["user"] == "alice"
