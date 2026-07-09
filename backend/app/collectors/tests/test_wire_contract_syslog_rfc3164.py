"""BYTE-LEVEL wire-contract test for kind ``syslog_rfc3164``.

This complements ``test_syslog_format.py`` (which asserts header SHAPE and PRI
derivation) by pinning the EXACT bytes a downstream consumer relies on. The
contract under test is what Wazuh's native ``JSON_Decoder`` and any RFC 3164
syslog collector parse off the wire:

    <PRI>Mmm dd HH:MM:SS HOSTNAME centralops[PID]: {json}\n(framing added by sender)

We call the PURE formatter ``format_rfc3164`` directly — no sockets, no
``send_batch``. The framing LF is added by ``Rfc3164JsonClient.send_batch``
(``line + b"\\n"``), so ``format_rfc3164`` itself MUST NOT emit a trailing LF
and MUST keep the whole record on a single physical line.

NON-DETERMINISM HANDLING
------------------------
RFC 3164 embeds a wall-clock timestamp (``datetime.utcnow()``), the host
(``socket.gethostname()``) and the pid (``os.getpid()``). freezegun is NOT
installed in this venv, so we monkeypatch the THREE names as the module
references them:
  * ``rfc3164_sender.datetime``      (imported via ``from datetime import datetime``)
  * ``rfc3164_sender.socket.gethostname``
  * ``rfc3164_sender.os.getpid``
With those frozen, the ENTIRE line is deterministic and we assert it byte-for-
byte. The JSON object inside the MSG is deterministic regardless of freezing
(``json.dumps`` with fixed separators) and is asserted exactly in every case.
"""

from __future__ import annotations

import datetime as _datetime
import json
import os
import re

import pytest

# Defaults in case any imported module builds settings (the formatter does not,
# but keep parity with the sibling delivery test to be safe).
os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

from backend.app.collectors.output import rfc3164_sender as mod
from backend.app.collectors.output.rfc3164_sender import format_rfc3164

# ── Frozen non-determinism knobs ──────────────────────────────────────────────

_FROZEN_HOST = "wire-test-host"
_FROZEN_PID = 4242
# 2026-04-06 12:34:56 UTC — the 6th exercises the single-digit-day padding
# (RFC 3164 §4.1.2: day is space-padded to width 2 → "Apr  6", two spaces).
_FROZEN_UTC = _datetime.datetime(2026, 4, 6, 12, 34, 56)


class _FrozenDateTime(_datetime.datetime):
    """datetime subclass with a fixed utcnow() (what the formatter calls)."""

    @classmethod
    def utcnow(cls) -> "_FrozenDateTime":  # type: ignore[override]
        return cls(2026, 4, 6, 12, 34, 56)


@pytest.fixture()
def frozen(monkeypatch: pytest.MonkeyPatch):
    """Freeze timestamp, hostname and pid so the FULL line is deterministic."""
    monkeypatch.setattr(mod, "datetime", _FrozenDateTime)
    monkeypatch.setattr(mod.socket, "gethostname", lambda: _FROZEN_HOST)
    monkeypatch.setattr(mod.os, "getpid", lambda: _FROZEN_PID)
    yield


# The exact header that the frozen knobs produce. Note the DOUBLE space before
# the single-digit day "6" — this is part of the RFC 3164 contract.
_HEADER = f"<{{pri}}>Apr  6 12:34:56 {_FROZEN_HOST} centralops[{_FROZEN_PID}]: "


def _wire(pri: int, json_body: str) -> str:
    """Build the exact expected wire line for a given PRI + JSON MSG."""
    return _HEADER.format(pri=pri) + json_body


def _canonical_json(event: dict) -> str:
    """The deterministic MSG body: compact separators, UTF-8, str fallback.

    Mirrors format_rfc3164's json.dumps call EXACTLY. If the formatter's
    serialization drifts (e.g. someone adds spaces or flips ensure_ascii),
    this independently-computed expectation diverges and the test fails.
    """
    return json.dumps(event, separators=(",", ":"), default=str, ensure_ascii=False)


# ── 1. Canonical event — full byte-exact line ─────────────────────────────────


def test_canonical_event_exact_wire_bytes(frozen) -> None:
    """A canonical critical-severity envelope serializes to EXACT bytes."""
    event = {
        "_centralops": {
            "event_id": "evt-abc-123",
            "organization_id": 7,
            "vendor": "sophos",
            "stream": "alerts",
            "event_type": "sophos.alert",
            "collected_at": "2026-04-06T12:34:56Z",
        },
        "normalized": {"class_uid": 2004, "severity_id": 5},
        "raw": {"id": "alert-1", "severity": "critical"},
    }

    line = format_rfc3164(event)

    expected_json = (
        '{"_centralops":{"event_id":"evt-abc-123","organization_id":7,'
        '"vendor":"sophos","stream":"alerts","event_type":"sophos.alert",'
        '"collected_at":"2026-04-06T12:34:56Z"},'
        '"normalized":{"class_uid":2004,"severity_id":5},'
        '"raw":{"id":"alert-1","severity":"critical"}}'
    )
    # severity_id=5 (critical) → PRI 130 (local0=16 → 16*8 + 2).
    expected = f"<130>Apr  6 12:34:56 {_FROZEN_HOST} centralops[{_FROZEN_PID}]: " + expected_json

    assert line == expected
    # Independently-recomputed JSON must match the inlined golden too.
    assert _canonical_json(event) == expected_json

    # ── Structural invariants the downstream consumer relies on ──
    # PRI is the very first thing, wrapped in angle brackets, then header.
    assert line.startswith("<130>")
    # The MSG (after "app[pid]: ") starts with '{' — the Wazuh JSON_Decoder
    # ``^{`` prematch. Split on the FIRST ": " that ends the header tag.
    header, sep, msg = line.partition(": ")
    assert sep == ": "
    assert msg.startswith("{"), "MSG must start with '{' for Wazuh JSON_Decoder prematch"
    assert msg.endswith("}")
    # The MSG is exactly the canonical JSON — byte-for-byte, no trailing junk.
    assert msg == expected_json
    # The header tag is APP_NAME[pid] (no extra whitespace, single colon-space).
    assert header == f"<130>Apr  6 12:34:56 {_FROZEN_HOST} centralops[{_FROZEN_PID}]"
    # Whole record is ONE physical line — no LF (framing LF is the SENDER's job).
    assert "\n" not in line
    assert not line.endswith("\n")
    # And it round-trips back to the original envelope.
    assert json.loads(msg) == event


# ── 2. Header SHAPE with frozen + regex (guards the non-deterministic fields) ──


def test_header_shape_regex(frozen) -> None:
    """Even frozen, assert the RFC 3164 BSD header SHAPE via regex so a future
    de-freeze still pins PRI/month/day-pad/host/tag/pid structure."""
    event = {"normalized": {"severity_id": 4}, "raw": {}}
    line = format_rfc3164(event)
    # <PRI>Mmm _d|dd HH:MM:SS HOST centralops[PID]: {
    pattern = (
        r"^<(?P<pri>\d{1,3})>"
        r"(?P<mon>[A-Z][a-z]{2}) (?P<day>[ \d]\d) "
        r"(?P<time>\d{2}:\d{2}:\d{2}) "
        r"(?P<host>\S+) centralops\[(?P<pid>\d+)\]: \{"
    )
    m = re.match(pattern, line)
    assert m is not None, f"header did not match RFC 3164 shape: {line!r}"
    # severity_id=4 (high) → PRI 131.
    assert m.group("pri") == "131"
    assert m.group("day") == " 6"  # space-padded single digit
    assert m.group("host") == _FROZEN_HOST
    assert m.group("pid") == str(_FROZEN_PID)


# ── 3. EDGE: missing _centralops.event_id (and minimal envelope) ──────────────


def test_edge_missing_event_id_and_default_pri(frozen) -> None:
    """An envelope with NO event_id and NO severity_id still serializes, uses
    the default PRI 134 (info), and the MSG is the exact canonical JSON."""
    event = {"_centralops": {"vendor": "acme"}, "normalized": {}, "raw": {}}
    line = format_rfc3164(event)

    expected_json = '{"_centralops":{"vendor":"acme"},"normalized":{},"raw":{}}'
    assert line == _wire(134, expected_json)
    assert _canonical_json(event) == expected_json
    # No KeyError, no synthetic event_id injected into the wire.
    assert "event_id" not in line


# ── 4. EDGE: empty / missing fields (empty nested objects, null, empty string) ─


def test_edge_empty_and_null_fields(frozen) -> None:
    """Empty strings, nulls and empty containers survive verbatim in the JSON."""
    event = {
        "_centralops": {"event_id": "", "organization_id": None},
        "normalized": {"severity_id": 0},  # 0 (unknown) → default PRI 134
        "raw": {},
        "tags": [],
    }
    line = format_rfc3164(event)

    expected_json = (
        '{"_centralops":{"event_id":"","organization_id":null},'
        '"normalized":{"severity_id":0},"raw":{},"tags":[]}'
    )
    assert line == _wire(134, expected_json)
    assert _canonical_json(event) == expected_json
    # null is the JSON literal, not the Python "None".
    assert '"organization_id":null' in line
    assert "None" not in line


# ── 5. EDGE: unicode / multibyte — ensure_ascii=False keeps raw UTF-8 ─────────


def test_edge_unicode_multibyte_raw_utf8(frozen) -> None:
    """Multibyte chars must appear RAW (ensure_ascii=False), not \\uXXXX. The
    UTF-8 byte length must match the codepoints so the encoded wire is correct."""
    event = {
        "_centralops": {"event_id": "evt-uni"},
        "normalized": {"severity_id": 1},  # informational → PRI 134
        "raw": {"msg": "café ☃ 日本語 — 𝔘𝔫𝔦𝔠𝔬𝔡𝔢"},
    }
    line = format_rfc3164(event)

    expected_json = (
        '{"_centralops":{"event_id":"evt-uni"},"normalized":{"severity_id":1},'
        '"raw":{"msg":"café ☃ 日本語 — 𝔘𝔫𝔦𝔠𝔬𝔡𝔢"}}'
    )
    assert line == _wire(134, expected_json)
    assert _canonical_json(event) == expected_json
    # Raw codepoints present, NO ASCII escaping of them.
    assert "café ☃ 日本語" in line
    assert "\\u" not in line.split("centralops[")[1], "unicode must NOT be \\u-escaped"
    # The UTF-8 wire bytes round-trip exactly back to the envelope.
    wire_bytes = line.encode("utf-8")
    _, _, msg_bytes = wire_bytes.partition(b": ")
    assert json.loads(msg_bytes.decode("utf-8")) == event


# ── 6. EDGE: special chars in message — newline/quote/backslash/tab/CR ────────


def test_edge_special_chars_stay_single_line(frozen) -> None:
    """Newlines, CRs, tabs, quotes and backslashes inside a string value are
    JSON-escaped — the physical wire line stays SINGLE-line (critical: a raw LF
    here would split one event into two syslog frames)."""
    event = {
        "_centralops": {"event_id": "evt-special"},
        "normalized": {"severity_id": 2},  # low → PRI 133
        "raw": {"msg": 'a"b\\c\nd\re\tf', "path": 'C:\\Temp\\"x"'},
    }
    line = format_rfc3164(event)

    expected_json = (
        '{"_centralops":{"event_id":"evt-special"},'
        '"normalized":{"severity_id":2},'
        '"raw":{"msg":"a\\"b\\\\c\\nd\\re\\tf","path":"C:\\\\Temp\\\\\\"x\\""}}'
    )
    assert line == _wire(133, expected_json)
    assert _canonical_json(event) == expected_json

    # The wire MUST be one physical line: no raw LF / CR / TAB leaked through.
    assert "\n" not in line
    assert "\r" not in line
    assert "\t" not in line
    # The escape sequences are the 2-char JSON forms, not control bytes.
    assert "\\n" in line and "\\r" in line and "\\t" in line
    assert '\\"' in line and "\\\\" in line
    # Round-trips back to the ORIGINAL message (control chars restored).
    _, _, msg = line.partition(": ")
    assert json.loads(msg)["raw"]["msg"] == 'a"b\\c\nd\re\tf'


# ── 7. EDGE: very large message — header is constant, MSG scales, single line ─


def test_edge_very_large_message(frozen) -> None:
    """A very large payload must still be a single line; the header/tag are
    unchanged and the entire (large) MSG equals the canonical JSON."""
    big = "X" * 200_000
    event = {
        "_centralops": {"event_id": "evt-big"},
        "normalized": {"severity_id": 3},  # medium → PRI 132
        "raw": {"blob": big},
    }
    line = format_rfc3164(event)

    header, sep, msg = line.partition(": ")
    assert sep == ": "
    assert header == f"<132>Apr  6 12:34:56 {_FROZEN_HOST} centralops[{_FROZEN_PID}]"
    assert msg == _canonical_json(event)
    assert msg.startswith('{"_centralops":{"event_id":"evt-big"}')
    assert big in msg
    # Still one physical line despite the size.
    assert "\n" not in line
    # Sanity: the wire is at least as big as the blob (no truncation).
    assert len(line) >= len(big)


# ── 8. EDGE: non-JSON-native value forced through default=str ────────────────


def test_edge_non_serializable_value_uses_default_str(frozen) -> None:
    """A datetime (not JSON-native) is coerced via default=str — the wire stays
    valid JSON and matches the canonical serialization exactly."""
    event = {
        "_centralops": {"event_id": "evt-dt"},
        "normalized": {"severity_id": 5},
        "raw": {"when": _FROZEN_UTC},  # datetime → str(...) fallback
    }
    line = format_rfc3164(event)

    expected_json = (
        '{"_centralops":{"event_id":"evt-dt"},"normalized":{"severity_id":5},'
        '"raw":{"when":"2026-04-06 12:34:56"}}'
    )
    assert line == _wire(130, expected_json)
    assert _canonical_json(event) == expected_json
    # The MSG is still parseable JSON (no bare repr leaked).
    _, _, msg = line.partition(": ")
    assert json.loads(msg)["raw"]["when"] == "2026-04-06 12:34:56"


# ── 9. CONTRACT: empty/non-dict event raises (guards malformed input) ─────────


def test_empty_event_raises() -> None:
    with pytest.raises(ValueError):
        format_rfc3164({})


def test_non_dict_event_raises() -> None:
    with pytest.raises(ValueError):
        format_rfc3164([])  # type: ignore[arg-type]


# ── 10. DETERMINISM: the JSON MSG is byte-stable across calls (no freezing) ───


def test_msg_json_is_deterministic_without_freezing() -> None:
    """Even WITHOUT frozen clock/host/pid, the MSG (everything after the header
    "app[pid]: ") is byte-stable across repeated calls — the contract a parser
    keys on is the JSON, which never depends on wall-clock state."""
    event = {
        "_centralops": {"event_id": "evt-stable", "organization_id": 9},
        "normalized": {"severity_id": 5},
        "raw": {"k": "v"},
    }
    msg1 = format_rfc3164(event).partition(": ")[2]
    msg2 = format_rfc3164(event).partition(": ")[2]
    assert msg1 == msg2 == _canonical_json(event)
    # And the header still matches the RFC 3164 shape (non-frozen).
    line = format_rfc3164(event)
    assert re.match(
        r"^<130>[A-Z][a-z]{2} [ \d]\d \d{2}:\d{2}:\d{2} \S+ centralops\[\d+\]: \{",
        line,
    )
