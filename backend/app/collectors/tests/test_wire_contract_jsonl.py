"""Wire-contract (byte-level) test for kind ``jsonl``.

Mirrors the spirit of the Wazuh / syslog golden tests, but for the
NDJSON-file destination. We call the *pure* formatter
``_jsonl_format`` directly (no sockets, no writer, no ``send_batch``)
and assert the EXACT wire bytes a downstream NDJSON consumer relies on.

THE CONTRACT (read from ``destinations/jsonl.py`` + ``jsonl_writer.py``):

    json.dumps(event, separators=(",", ":"), default=str,
               ensure_ascii=False).encode("utf-8")

  1. UTF-8 encoded bytes.
  2. Compact JSON — separators ``(",", ":")``: NO space after ``,`` or ``:``.
  3. ``ensure_ascii=False`` — non-ASCII (unicode/multibyte) is emitted as raw
     UTF-8 bytes, NOT ``\\uXXXX`` escapes.
  4. ``default=str`` — objects json can't serialize natively are stringified.
  5. Key order == dict insertion order (json preserves it).
  6. NO line framing in the formatter: the bytes carry NO trailing ``\\n``
     (LF) and NO ``\\r\\n`` (CRLF). Line framing is the *writer's* job — it
     appends exactly one ``b"\\n"`` per line (see ``JSONLWriter``). Asserting
     this here protects against a regression that would yield ``}\\n\\n``
     double-newlines and corrupt NDJSON line framing downstream.

Everything ``_jsonl_format`` touches is deterministic: unlike RFC3164/5424
there is NO embedded timestamp / hostname / pid, so every byte below is
asserted literally. ``freezegun`` is not needed (and is not installed).
"""

from __future__ import annotations

import json
import os

# Defensive: some imports in this package build settings on import. The
# formatter itself does not need them, but keep parity with sibling tests.
os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

from backend.app.collectors.output.destinations.jsonl import _jsonl_format


# ── Canonical event ──────────────────────────────────────────────────────


def _canonical_event() -> dict:
    """A representative normalized envelope (insertion order is the wire order)."""
    return {
        "event_type": "sophos.alert",
        "severity": "Critical",
        "_centralops": {
            "event_id": "evt-1",
            "vendor": "sophos",
            "customer_id": 7,
            "stream": "alerts",
        },
        "class_uid": 2004,
        "severity_id": 5,
    }


def test_canonical_event_exact_wire_bytes() -> None:
    """Byte-for-byte golden of the canonical envelope.

    Compact (no spaces), key order preserved, ends with ``}`` and NO newline.
    """
    out = _jsonl_format(_canonical_event())

    expected = (
        b'{"event_type":"sophos.alert","severity":"Critical",'
        b'"_centralops":{"event_id":"evt-1","vendor":"sophos",'
        b'"customer_id":7,"stream":"alerts"},'
        b'"class_uid":2004,"severity_id":5}'
    )
    assert out == expected


def test_returns_bytes_utf8() -> None:
    out = _jsonl_format(_canonical_event())
    assert isinstance(out, bytes)
    # Must be valid UTF-8 and valid JSON that round-trips to the same object.
    assert json.loads(out.decode("utf-8")) == _canonical_event()


def test_compact_separators_no_whitespace() -> None:
    """No space after ``,`` or ``:`` — the compact-separator contract."""
    out = _jsonl_format(_canonical_event())
    assert b", " not in out
    assert b": " not in out
    # ``json.dumps`` default would emit ``", "``/``": "`` — guard against drift.
    assert out == json.dumps(
        _canonical_event(), separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def test_no_line_framing_no_trailing_newline() -> None:
    """The formatter must NOT add line framing.

    Framing (the single LF) is the writer's responsibility. If the formatter
    ever appended ``\\n`` the writer's ``+ b"\\n"`` would double it and break
    NDJSON. Also assert NO CRLF anywhere.
    """
    out = _jsonl_format(_canonical_event())
    assert not out.endswith(b"\n")
    assert not out.endswith(b"\r\n")
    assert b"\r\n" not in out
    assert b"\r" not in out
    # First and last byte are the JSON object delimiters — nothing wraps them.
    assert out[:1] == b"{"
    assert out[-1:] == b"}"


# ── EDGE CASE 1: empty event ─────────────────────────────────────────────


def test_empty_event_is_empty_object() -> None:
    assert _jsonl_format({}) == b"{}"


# ── EDGE CASE 2: missing _centralops.event_id ────────────────────────────


def test_missing_event_id_does_not_synthesize_fields() -> None:
    """The formatter is a pure dump — it never injects an ``event_id``.

    An envelope whose ``_centralops`` lacks ``event_id`` is serialized as-is;
    no key is added, removed, or reordered.
    """
    event = {"_centralops": {"vendor": "acme"}, "msg": "hello"}
    out = _jsonl_format(event)
    assert out == b'{"_centralops":{"vendor":"acme"},"msg":"hello"}'
    assert b"event_id" not in out


# ── EDGE CASE 3: unicode / multibyte (ensure_ascii=False) ────────────────


def test_unicode_multibyte_is_raw_utf8_not_escaped() -> None:
    """``ensure_ascii=False`` — multibyte chars are raw UTF-8, never ``\\uXXXX``.

    Covers Latin accents (2-byte), CJK (3-byte), and an emoji (4-byte / surrogate
    pair territory). The wire MUST contain the literal UTF-8 byte sequences.
    """
    event = {"msg": "café 日本語 🚀"}
    out = _jsonl_format(event)

    expected = '{"msg":"café 日本語 🚀"}'.encode("utf-8")
    assert out == expected
    # No ASCII-escaped unicode leaked through.
    assert b"\\u" not in out
    # Spot-check the actual multibyte bytes are present.
    assert "café".encode("utf-8") in out          # 0x63 0x61 0x66 0xC3 0xA9
    assert "日本語".encode("utf-8") in out          # 3 x 3-byte
    assert "🚀".encode("utf-8") in out             # 0xF0 0x9F 0x9A 0x80


# ── EDGE CASE 4: special chars — newline/quote/backslash/tab/control ──────


def test_special_chars_are_json_escaped_and_do_not_break_framing() -> None:
    """An embedded newline/quote/backslash in a VALUE must be JSON-escaped so
    the single-line NDJSON framing survives.

    A literal ``\\n`` in the message must serialize to the 2-byte escape
    sequence ``\\`` + ``n`` (0x5C 0x6E), NOT a raw 0x0A, otherwise it would
    split one logical event across two NDJSON lines for a downstream consumer.
    """
    event = {
        "msg": 'line1\nline2\ttab "quoted" back\\slash\r',
    }
    out = _jsonl_format(event)

    expected = (
        b'{"msg":"line1\\nline2\\ttab \\"quoted\\" back\\\\slash\\r"}'
    )
    assert out == expected
    # The wire is a single NDJSON line: exactly zero raw control bytes.
    assert b"\n" not in out          # no raw LF
    assert b"\r" not in out          # no raw CR
    assert b"\t" not in out          # no raw TAB
    # Escapes ARE present as their two-byte forms.
    assert b"\\n" in out
    assert b"\\r" in out
    assert b"\\t" in out
    assert b'\\"' in out             # escaped quote
    assert b"\\\\" in out            # escaped backslash


def test_embedded_newline_plus_writer_framing_yields_two_byte_split() -> None:
    """Whole-contract guard: formatter output + the writer's single ``b"\\n"``
    must produce EXACTLY one NDJSON physical line (one trailing LF), even when
    the message body contains newlines.
    """
    event = {"msg": "a\nb"}
    line = _jsonl_format(event) + b"\n"  # writer appends exactly this
    # Exactly one physical line break == one trailing LF, body untouched.
    assert line.count(b"\n") == 1
    assert line.endswith(b"\n")
    assert line == b'{"msg":"a\\nb"}\n'


# ── EDGE CASE 5: very large message ──────────────────────────────────────


def test_very_large_message_round_trips_byte_exact() -> None:
    """A ~1 MiB payload survives intact (no truncation, no re-encoding)."""
    big = "A" * (1024 * 1024)
    event = {"_centralops": {"event_id": "big-1"}, "msg": big}
    out = _jsonl_format(event)

    # Exact prefix/suffix and full length are pinned.
    assert out.startswith(b'{"_centralops":{"event_id":"big-1"},"msg":"')
    assert out.endswith(b'"}')
    assert out.count(b"A") == 1024 * 1024
    # Round-trips to the original object with no data loss.
    assert json.loads(out.decode("utf-8"))["msg"] == big


# ── EDGE CASE 6: non-JSON-native value → default=str ─────────────────────


def test_non_serializable_value_is_stringified_via_default() -> None:
    """``default=str`` — a value json can't natively encode (e.g. ``set``,
    ``bytes``-like custom object) is coerced with ``str()`` rather than raising.
    """

    class _Opaque:
        def __str__(self) -> str:  # deterministic stringification
            return "opaque-repr"

    event = {"a_set": {1}, "obj": _Opaque()}
    out = _jsonl_format(event)
    # set -> str("{1}"), object -> str("opaque-repr"); both as JSON strings.
    assert out == b'{"a_set":"{1}","obj":"opaque-repr"}'


# ── Key ORDER is preserved (insertion order == wire order) ────────────────


def test_key_order_is_insertion_order() -> None:
    event = {}
    event["z"] = 1
    event["a"] = 2
    event["m"] = 3
    out = _jsonl_format(event)
    # Not sorted — preserves the order keys were inserted.
    assert out == b'{"z":1,"a":2,"m":3}'
