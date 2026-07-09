"""Byte-level WIRE-CONTRACT test for kind ``syslog_rfc5424``.

Mirrors the Wazuh golden tests: asserts the EXACT RFC 5424 wire string a
downstream syslog/SIEM consumer parses, not just substrings. The sibling
``test_syslog_format.py`` checks header *shape* and individual SD fields;
THIS file adds the full-line golden-contract layer:

  - exact framing of the HEADER fields:
        <PRI>VERSION SP TIMESTAMP SP HOSTNAME SP APP-NAME SP PROCID SP MSGID
    where VERSION is the literal digit ``1``, PROCID is the literal ``-``,
    APP-NAME is ``centralops-collector`` and MSGID is the integration_id;
  - the STRUCTURED-DATA element (SD-ID ``centralops@32473``) with its
    param order and RFC 5424 §6.3.3 escaping of ``\\  "  ]`` inside values;
  - the deterministic compact JSON MSG (json.dumps, separators (",",":"),
    ensure_ascii=False, default=str) appended after a single SP;
  - NO BOM is emitted before MSG, and SD is never collapsed to ``-``.

NON-DETERMINISM: the formatter pulls HOSTNAME from ``socket.gethostname()``
(monkeypatched here to a frozen value) and the TIMESTAMP from the event's
``_centralops.collected_at`` (deterministic — asserted literally). PROCID is
the literal ``-`` (no os.getpid()), so nothing else needs freezing. One test
exercises the ``_now_iso()`` fallback path and asserts its SHAPE via regex.

Run:
    PYTHONPATH=/Users/dathan/Github/CentralOps:/Users/dathan/Github/CentralOps/backend \
      /Users/dathan/Github/CentralOps/backend/.venv/bin/python -m pytest \
      backend/app/collectors/tests/test_wire_contract_syslog_rfc5424.py -p no:cacheprovider -q
"""

from __future__ import annotations

import json
import os
import re

import pytest

# The formatter is pure (no settings build), but set defaults defensively to
# match the sibling delivery tests in case any import path touches settings.
os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

from backend.app.collectors.output import syslog_sender
from backend.app.collectors.output.syslog_sender import format_rfc5424

# Frozen non-deterministic header field. The formatter reads hostname from
# ``socket.gethostname()`` at call time; pin it so the wire is reproducible.
FROZEN_HOSTNAME = "centralops-host-01"

# RFC 5424 §6.2.4 TIMESTAMP — supplied deterministically via collected_at.
TS = "2026-04-23T14:22:10Z"

# SD-ID registered for CentralOps (private enterprise number 32473).
SD_ID = "centralops@32473"


@pytest.fixture(autouse=True)
def _freeze_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin socket.gethostname so HOSTNAME is a stable wire token.

    Patch the symbol on the module under test (it does ``import socket`` and
    calls ``socket.gethostname()``), so this is robust to import style.
    """
    monkeypatch.setattr(syslog_sender.socket, "gethostname", lambda: FROZEN_HOSTNAME)


def _event(**meta_overrides):
    """A canonical enriched envelope. ``normalized.severity_id`` drives PRI."""
    meta = {
        "integration_id": 42,
        "customer_id": 7,
        "vendor": "sophos",
        "stream": "alerts",
        "event_type": "sophos.alert",
        "collected_at": TS,
    }
    meta.update(meta_overrides)
    return {
        "_centralops": meta,
        "normalized": {"class_uid": 2004, "severity_id": 4},
        "raw": {"id": "alert-1", "severity": "high"},
    }


def _expected_msg(event) -> str:
    """The MSG must be byte-identical to this json.dumps invocation."""
    return json.dumps(event, separators=(",", ":"), default=str, ensure_ascii=False)


# ── 1. Canonical event — FULL golden line, byte-for-byte ─────────────


def test_canonical_full_wire_line_is_byte_exact() -> None:
    evt = _event()
    line = format_rfc5424(evt)

    # severity_id=4 (high) → syslog severity 3 (err), facility local0=16
    # → PRI = 16*8 + 3 = 131. VERSION digit is literal '1'. PROCID '-'.
    # MSGID = integration_id = 42.
    expected_header = (
        f"<131>1 {TS} {FROZEN_HOSTNAME} centralops-collector - 42"
    )
    expected_sd = (
        f'[{SD_ID} '
        f'integration_id="42" '
        f'customer_id="7" '
        f'vendor="sophos" '
        f'stream="alerts" '
        f'event_type="sophos.alert"]'
    )
    expected = f"{expected_header} {expected_sd} {_expected_msg(evt)}"

    assert line == expected
    # No BOM precedes the MSG (formatter emits none); the byte after the SD's
    # closing ']' and the single SP must be the JSON object's '{'.
    assert " ﻿" not in line
    sp_then_msg = line[len(expected_header) + 1 + len(expected_sd):]
    assert sp_then_msg.startswith(" {")
    # Whole wire is single logical line (octet-counting framing relies on it,
    # but the MSG itself must carry no physical LF for the canonical case).
    assert "\n" not in line


# ── 2. Header token grammar (positions, separators, version digit) ───


def test_header_token_grammar_and_version_digit() -> None:
    line = format_rfc5424(_event())
    # Split the HEADER off at the SD opening bracket.
    header = line[: line.index(" [")]
    # HEADER = <PRI>VERSION SP TS SP HOST SP APPNAME SP PROCID SP MSGID
    assert header.startswith("<131>1 "), "PRI immediately followed by VERSION '1' then SP"
    pri_version, ts, host, appname, procid, msgid = header.split(" ")
    assert pri_version == "<131>1"          # NILVALUE-free PRI + version digit
    assert ts == TS
    assert host == FROZEN_HOSTNAME
    assert appname == "centralops-collector"
    assert procid == "-"                     # literal NILVALUE for PROCID
    assert msgid == "42"
    # Exactly one space between each of the six header fields → 5 separators.
    assert header.count(" ") == 5


# ── 3. SD §6.3.3 escaping: ] \ " inside a param value ────────────────


def test_sd_escapes_close_bracket_backslash_quote() -> None:
    # Raw value contains, in order: a quote, a backslash, a close bracket.
    evt = _event(vendor='weird"\\]value')
    line = format_rfc5424(evt)
    # Per RFC 5424 §6.3.3 each of " \ ] is prefixed with a backslash.
    #   "  -> \"
    #   \  -> \\
    #   ]  -> \]
    assert 'vendor="weird\\"\\\\\\]value"' in line
    # The escaped ']' must NOT prematurely terminate the SD element: the
    # element's real terminator is the final unescaped ']' before the MSG SP.
    sd = line[line.index("[") : line.rindex("] ") + 1]
    assert sd.endswith('event_type="sophos.alert"]')
    # The raw close-bracket inside the value is preceded by a backslash.
    assert "\\]value" in sd


# ── 4. Missing fields → empty SD values + '-' MSGID ──────────────────


def test_missing_fields_emit_empty_sd_values_and_dash_msgid() -> None:
    # integration_id absent → MSGID falls back to literal '-'.
    # customer_id / vendor / stream / event_type absent → param='' (empty).
    evt = {
        "_centralops": {"collected_at": TS},
        "normalized": {},  # no severity_id → PRI default 134 (info)
        "raw": {},
    }
    line = format_rfc5424(evt)
    expected_header = f"<134>1 {TS} {FROZEN_HOSTNAME} centralops-collector - -"
    expected_sd = (
        f'[{SD_ID} '
        f'integration_id="" '
        f'customer_id="" '
        f'vendor="" '
        f'stream="" '
        f'event_type=""]'
    )
    expected = f"{expected_header} {expected_sd} {_expected_msg(evt)}"
    assert line == expected
    # Two trailing '-' tokens: PROCID then MSGID, each a real NILVALUE.
    assert " - -" in line


# ── 5. Unicode / multibyte preserved verbatim (ensure_ascii=False) ───


def test_unicode_multibyte_preserved_in_sd_and_msg() -> None:
    evt = _event(vendor="café—日本語", stream="straße")
    line = format_rfc5424(evt)
    # SD param values keep the raw unicode (no \uXXXX escaping in SD).
    assert 'vendor="café—日本語"' in line
    assert 'stream="straße"' in line
    # MSG is ensure_ascii=False, so the same codepoints appear unescaped.
    assert '"vendor":"café—日本語"' in line
    assert "\\u" not in line  # nothing got ASCII-escaped on this wire
    # And the MSG byte-matches the canonical json.dumps for this event.
    assert line.endswith(_expected_msg(evt))


# ── 6. Newlines / quotes / backslashes in MSG payload stay in MSG ────


def test_special_chars_in_payload_are_json_escaped_not_raw_lf() -> None:
    evt = _event()
    # Adversarial payload: a physical newline, a quote, and a backslash.
    evt["payload"] = 'line1\nline2 "q" back\\slash'
    line = format_rfc5424(evt)
    # The JSON string-escapes the LF as \n — no raw LF reaches the wire, so
    # octet-counting framing in send_batch stays correct.
    assert "\n" not in line
    assert '"payload":"line1\\nline2 \\"q\\" back\\\\slash"' in line
    # Full MSG still equals the canonical json.dumps of the mutated event.
    assert line.endswith(_expected_msg(evt))


# ── 7. Very large message — framing/encoding hold at scale ───────────


def test_very_large_message_wire_is_exact() -> None:
    evt = _event()
    big = "X" * 200_000  # 200 KiB single field
    evt["payload"] = big
    line = format_rfc5424(evt)
    expected_msg = _expected_msg(evt)
    assert line.endswith(expected_msg)
    assert f'"payload":"{big}"' in line
    # utf-8 byte length is what octet-counting framing would prepend; sanity
    # check that the formatter returns a str whose utf-8 length is well-defined.
    assert len(line.encode("utf-8")) >= len(big)
    assert "\n" not in line


# ── 8. PRI is driven by severity_id (full sweep of header digit) ─────


@pytest.mark.parametrize(
    "severity_id, pri",
    [
        (5, 130),  # critical → crit(2)   → 16*8+2
        (4, 131),  # high     → err(3)
        (3, 132),  # medium   → warning(4)
        (2, 133),  # low      → notice(5)
        (1, 134),  # info     → info(6)
        (6, 128),  # fatal    → emerg(0)
        (None, 134),  # absent → default info
    ],
)
def test_pri_matches_severity_map(severity_id, pri) -> None:
    evt = _event()
    if severity_id is None:
        evt["normalized"].pop("severity_id", None)
    else:
        evt["normalized"]["severity_id"] = severity_id
    line = format_rfc5424(evt)
    assert line.startswith(f"<{pri}>1 ")


# ── 9. Missing _centralops.event_id is irrelevant; missing namespace ─


def test_missing_event_id_does_not_break_wire() -> None:
    """``_centralops.event_id`` is not part of the RFC 5424 header at all;
    its absence must not change the wire. MSGID derives from integration_id."""
    evt = _event()
    evt["_centralops"].pop("event_id", None)  # never present anyway
    line = format_rfc5424(evt)
    assert line.startswith(f"<131>1 {TS} {FROZEN_HOSTNAME} centralops-collector - 42 ")


def test_missing_centralops_namespace_raises() -> None:
    with pytest.raises(ValueError, match="_centralops"):
        format_rfc5424({"raw": {"id": "no-meta"}})


# ── 10. Timestamp fallback SHAPE when collected_at is absent ─────────


def test_timestamp_fallback_shape_when_collected_at_missing() -> None:
    """Without collected_at the formatter stamps _now_iso() (non-det). Assert
    only its SHAPE (RFC 5424-ish ISO-8601, 'Z'-suffixed), never a literal."""
    evt = _event()
    evt["_centralops"].pop("collected_at", None)
    line = format_rfc5424(evt)
    # <131>1 <TIMESTAMP> centralops-host-01 ...
    m = re.match(
        r"^<131>1 (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) "
        rf"{re.escape(FROZEN_HOSTNAME)} centralops-collector - 42 \[",
        line,
    )
    assert m, f"timestamp fallback shape mismatch: {line[:80]!r}"
