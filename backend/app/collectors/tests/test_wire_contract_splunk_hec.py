"""BYTE-LEVEL WIRE CONTRACT test for kind ``splunk_hec``.

Mirrors the Wazuh golden-output tests, one layer deeper than the sibling
``test_splunk_hec.py``: that file asserts *behaviour* (which keys, retryable
statuses, DLQ wiring). THIS file pins the *exact on-wire bytes* a downstream
Splunk HEC endpoint receives, so any silent drift in framing, separators,
encoding, key presence, or envelope shape fails loudly.

Two surfaces are contracted:

1. The pure formatter ``format_hec_event`` / ``SplunkHecClient.format`` →
   the HEC envelope *dict* shape: ``{"event", "sourcetype", index?, source?,
   host?}`` with optional keys omitted when None and ``time`` NEVER set.

2. The real on-wire payload that ``send_batch`` POSTs: **NDJSON** — one
   compact JSON object per line, ``separators=(",",":")``,
   ``ensure_ascii=False`` (raw UTF-8 multibyte, not ``\\uXXXX``),
   ``default=str``, joined by a single ``\\n`` with NO trailing newline and
   NO commas between objects (it is NOT a JSON array). We capture the exact
   ``data=`` argument send_batch hands aiohttp via a fully-mocked session —
   no sockets, no network — and assert it byte-for-byte against golden lines.

DETERMINISM: ``splunk_hec`` embeds NO timestamp / hostname / pid (unlike
RFC3164/5424). The formatter explicitly never sets ``time`` (Splunk uses
receipt time), and dict insertion order is stable, so json.dumps output is
fully deterministic. Nothing to freeze/monkeypatch — golden strings are exact.

The formatter needs no settings; we import ONLY the formatter + client and
still set APP_MASTER_KEY/APP_ENV defaults to match sibling-test convention in
case any transitive import builds settings.
"""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

from backend.app.collectors.output.splunk_hec_sender import (  # noqa: E402
    SplunkHecClient,
    format_hec_event,
)


# ── helpers ──────────────────────────────────────────────────────────────────


def _mock_response(status: int, body: dict) -> MagicMock:
    """aiohttp ClientResponse usable as an async context manager."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=body)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=None)
    return resp


def _mock_session(response: MagicMock) -> MagicMock:
    session = MagicMock()
    session.closed = False
    session.post = MagicMock(return_value=response)
    session.close = AsyncMock()
    return session


async def _capture_wire(client: SplunkHecClient, batch: list) -> str:
    """Drive the REAL send_batch through a mocked session and return the exact
    ``data=`` string it handed to ``session.post`` — i.e. the on-wire payload.

    Asserts a 200/code=0 round trip so we know the happy path (which builds the
    payload) ran; the payload itself is captured regardless.
    """
    resp = _mock_response(200, {"text": "Success", "code": 0})
    session = _mock_session(resp)
    client._session = session

    result = await client.send_batch(batch)
    assert result.accepted == len(batch)

    call = session.post.call_args
    # send_batch posts positionally (url) + data= kw.
    assert call.kwargs.get("data") is not None, f"no data= in post call: {call!r}"
    return call.kwargs["data"]


# Canonical event used across formatter + wire tests.
_CANONICAL = {
    "_centralops": {
        "vendor": "sophos",
        "integration_id": 1,
        "customer_id": 7,
        "stream": "alerts",
        "event_type": "sophos.alert",
        "event_id": "evt-abc123",
    },
    "data": {"id": "evt-1", "severity": "Critical"},
}


# A client with ALL optional fields populated → maximal envelope.
def _full_client() -> SplunkHecClient:
    return SplunkHecClient(
        url="https://splunk.test.local:8088/",  # trailing slash → must be normalised
        token="test-hec-token",
        index="centralops",
        sourcetype="centralops",
        source="centralops-collector",
        host="centralops-host",
        verify_tls=False,
    )


# A client with only the required fields → minimal envelope.
def _minimal_client() -> SplunkHecClient:
    return SplunkHecClient(
        url="https://splunk.test.local:8088",
        token=None,
        sourcetype="centralops",
    )


# ═══════════════════════════════════════════════════════════════════════════
# PART 1 — formatter envelope DICT shape (pure, no I/O)
# ═══════════════════════════════════════════════════════════════════════════


def test_envelope_canonical_full_exact_dict() -> None:
    """Maximal config → every optional key present, exact key set + values,
    event nested verbatim, ``time`` ABSENT and ``fields`` WITH event_id.

    O event_id do _centralops é exposto em fields._centralops_event_id
    para dedup no indexer Splunk (não dedup automático no sender — at_least_once).
    """
    wrapper = format_hec_event(
        _CANONICAL,
        sourcetype="centralops",
        index="centralops",
        source="centralops-collector",
        host="centralops-host",
    )
    assert wrapper == {
        "event": _CANONICAL,
        "sourcetype": "centralops",
        "index": "centralops",
        "source": "centralops-collector",
        "host": "centralops-host",
        "fields": {"_centralops_event_id": "evt-abc123"},
    }
    # event deve ser o envelope canônico, intocado.
    assert wrapper["event"] is _CANONICAL
    # CONTRACT: time nunca injetado por nós.
    assert "time" not in wrapper
    # CONTRACT: fields expõe o event_id para dedup no indexer.
    assert wrapper["fields"]["_centralops_event_id"] == "evt-abc123"


def test_envelope_minimal_exact_dict() -> None:
    """No index/source/host → exactly two keys: event + sourcetype.
    No event_id in _centralops → fields key is ABSENT (no spurious payload).
    """
    ev_no_id = {"data": {"id": "x"}}  # sem _centralops → sem fields
    wrapper = format_hec_event(ev_no_id, sourcetype="centralops")
    assert wrapper == {"event": ev_no_id, "sourcetype": "centralops"}
    assert set(wrapper) == {"event", "sourcetype"}
    assert "fields" not in wrapper


def test_envelope_with_event_id_adds_fields() -> None:
    """Evento com _centralops.event_id → fields presente com o id correto."""
    wrapper = format_hec_event(_CANONICAL, sourcetype="centralops")
    assert "fields" in wrapper
    assert wrapper["fields"] == {"_centralops_event_id": "evt-abc123"}


def test_envelope_without_event_id_no_fields() -> None:
    """Evento sem _centralops.event_id → sem campo fields no wrapper HEC."""
    ev = {"data": "x", "_centralops": {"vendor": "test"}}  # sem event_id
    wrapper = format_hec_event(ev, sourcetype="centralops")
    assert "fields" not in wrapper


def test_client_format_matches_config() -> None:
    """SplunkHecClient.format() threads the instance config into the envelope."""
    assert _full_client().format(_CANONICAL) == {
        "event": _CANONICAL,
        "sourcetype": "centralops",
        "index": "centralops",
        "source": "centralops-collector",
        "host": "centralops-host",
        "fields": {"_centralops_event_id": "evt-abc123"},
    }
    ev_no_id = {"data": "x"}
    assert _minimal_client().format(ev_no_id) == {
        "event": ev_no_id,
        "sourcetype": "centralops",
    }


@pytest.mark.parametrize(
    "index,source,host,expected_optional_keys",
    [
        (None, None, None, set()),
        ("idx", None, None, {"index"}),
        (None, "src", None, {"source"}),
        (None, None, "h", {"host"}),
        ("idx", "src", "h", {"index", "source", "host"}),
    ],
)
def test_optional_key_presence_matrix(
    index: Any, source: Any, host: Any, expected_optional_keys: set
) -> None:
    """Each optional field appears IFF non-None — adversarial against a refactor
    that emits empty-string or null placeholders.

    Usa evento SEM event_id para isolar os opcionais (index/source/host)
    do campo fields (adicionado apenas quando event_id presente).
    """
    ev_no_id = {"data": "x"}  # sem _centralops → sem fields
    wrapper = format_hec_event(
        ev_no_id, sourcetype="centralops", index=index, source=source, host=host
    )
    assert set(wrapper) == {"event", "sourcetype"} | expected_optional_keys
    # Never a literal null for an omitted field.
    for k in ("index", "source", "host"):
        if k not in expected_optional_keys:
            assert k not in wrapper
    # fields ausente (evento sem event_id).
    assert "fields" not in wrapper


def test_empty_string_optionals_are_kept_not_dropped() -> None:
    """Only None omits a key — empty string is a real value and stays. Pins the
    `is not None` semantics (a `if index:` truthiness refactor would break it)."""
    wrapper = format_hec_event(
        _CANONICAL, sourcetype="centralops", index="", source="", host=""
    )
    assert wrapper["index"] == ""
    assert wrapper["source"] == ""
    assert wrapper["host"] == ""


# ═══════════════════════════════════════════════════════════════════════════
# PART 2 — on-wire NDJSON byte contract (real send_batch, mocked transport)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_wire_single_event_exact_bytes() -> None:
    """One canonical event → exactly one compact JSON line, byte-for-byte.

    Pins: compact separators (no spaces), key order (event, sourcetype, index,
    source, host, fields), no trailing newline, no `time`.

    ``fields._centralops_event_id`` é incluído para dedup no indexer
    Splunk (o sender NÃO faz dedup automático — capability = at_least_once).
    """
    wire = await _capture_wire(_full_client(), [_CANONICAL])
    expected = (
        '{"event":{"_centralops":{"vendor":"sophos","integration_id":1,'
        '"customer_id":7,"stream":"alerts","event_type":"sophos.alert",'
        '"event_id":"evt-abc123"},"data":{"id":"evt-1","severity":"Critical"}},'
        '"sourcetype":"centralops","index":"centralops",'
        '"source":"centralops-collector","host":"centralops-host",'
        '"fields":{"_centralops_event_id":"evt-abc123"}}'
    )
    assert wire == expected
    # No framing surprises.
    assert "\n" not in wire
    assert not wire.endswith("\n")
    # Re-parseable e round-trips para o mesmo envelope.
    assert json.loads(wire)["event"] == _CANONICAL
    # fields presente com o event_id correto.
    assert json.loads(wire)["fields"]["_centralops_event_id"] == "evt-abc123"


@pytest.mark.asyncio
async def test_wire_is_ndjson_not_json_array() -> None:
    """Two events → two newline-separated objects. NOT a JSON array: the whole
    payload must NOT itself parse as a list, and there is no comma BETWEEN the
    top-level objects (only the single \\n).

    Cada linha inclui fields._centralops_event_id quando event_id
    presente — exposto para dedup no indexer Splunk.
    """
    ev2 = {
        "_centralops": {"event_id": "evt-2"},
        "data": {"id": "evt-2"},
    }
    wire = await _capture_wire(_minimal_client(), [_CANONICAL, ev2])

    lines = wire.split("\n")
    assert len(lines) == 2, f"expected 2 NDJSON lines, got {len(lines)}: {wire!r}"
    # Exactly one separator newline, no trailing newline.
    assert wire.count("\n") == 1
    assert not wire.endswith("\n")
    # Each line is an independent JSON object (a dict, not a fragment).
    for line in lines:
        assert isinstance(json.loads(line), dict)
    # The concatenation is NOT a JSON array.
    with pytest.raises(json.JSONDecodeError):
        json.loads(wire)
    # The byte between the two objects is the newline, not a comma.
    assert lines[0].endswith("}")
    assert lines[1].startswith("{")
    # Second object pinned exactly (minimal client → no index/source/host;
    # mas fields presente pois ev2 tem event_id).
    assert lines[1] == (
        '{"event":{"_centralops":{"event_id":"evt-2"},'
        '"data":{"id":"evt-2"}},"sourcetype":"centralops",'
        '"fields":{"_centralops_event_id":"evt-2"}}'
    )


@pytest.mark.asyncio
async def test_wire_empty_batch_emits_nothing() -> None:
    """Empty batch → ok(0) and NO HTTP call at all (no empty POST to HEC)."""
    client = _minimal_client()
    session = _mock_session(_mock_response(200, {"text": "Success", "code": 0}))
    client._session = session

    result = await client.send_batch([])

    assert result.accepted == 0
    session.post.assert_not_called()


# ── edge cases inside the event payload (the deterministic part of the wire) ──


@pytest.mark.asyncio
async def test_wire_unicode_multibyte_raw_utf8() -> None:
    """ensure_ascii=False → multibyte chars stay as raw UTF-8, NOT \\uXXXX
    escapes. A downstream consumer decoding UTF-8 must see the literal glyphs."""
    ev = {"_centralops": {"event_id": "u1"}, "msg": "café—日本語—🛡"}
    wire = await _capture_wire(_minimal_client(), [ev])
    assert '"msg":"café—日本語—🛡"' in wire
    assert "\\u" not in wire  # no JSON unicode-escape sequences
    # The raw bytes really are UTF-8 multibyte.
    assert "café—日本語—🛡".encode("utf-8") in wire.encode("utf-8")
    assert json.loads(wire)["event"]["msg"] == "café—日本語—🛡"


@pytest.mark.asyncio
async def test_wire_special_chars_escaped_minimally() -> None:
    """Newlines / quotes / backslashes / tabs in the message are JSON-escaped
    (so they cannot break NDJSON framing) but NOT over-escaped. A literal newline
    inside a value must become \\n in the wire — never a real line break that
    would split the record into two NDJSON lines."""
    ev = {
        "_centralops": {"event_id": "esc1"},
        "msg": 'line1\nline2\ttab "quote" back\\slash',
    }
    wire = await _capture_wire(_minimal_client(), [ev])
    # Still ONE line — the embedded \n did not create a second NDJSON record.
    assert wire.count("\n") == 0
    assert '"msg":"line1\\nline2\\ttab \\"quote\\" back\\\\slash"' in wire
    # And it round-trips back to the original literal string.
    assert json.loads(wire)["event"]["msg"] == 'line1\nline2\ttab "quote" back\\slash'


@pytest.mark.asyncio
async def test_wire_missing_and_empty_fields() -> None:
    """Event with no _centralops at all, plus null/empty values, serialises
    deterministically — null stays null, empty dict/string stay as-is."""
    ev = {"data": None, "note": "", "tags": [], "meta": {}}
    wire = await _capture_wire(_minimal_client(), [ev])
    assert wire == (
        '{"event":{"data":null,"note":"","tags":[],"meta":{}},'
        '"sourcetype":"centralops"}'
    )


@pytest.mark.asyncio
async def test_wire_large_message_passthrough() -> None:
    """A very large message is serialised verbatim (no truncation in the
    formatter — size limits are the transport/HEC's concern, not format())."""
    big = "A" * 200_000
    ev = {"_centralops": {"event_id": "big"}, "msg": big}
    wire = await _capture_wire(_minimal_client(), [ev])
    assert f'"msg":"{big}"' in wire
    assert json.loads(wire)["event"]["msg"] == big
    assert len(wire) > 200_000


@pytest.mark.asyncio
async def test_wire_non_json_native_value_via_default_str() -> None:
    """send_batch uses default=str → a non-JSON-native value (datetime) is
    stringified rather than raising. Pins the default=str escape hatch."""
    import datetime

    ev = {
        "_centralops": {"event_id": "dt1"},
        "observed_at": datetime.datetime(2026, 6, 17, 12, 0, 0),
    }
    wire = await _capture_wire(_minimal_client(), [ev])
    assert '"observed_at":"2026-06-17 12:00:00"' in wire


# ── event_id extraction contract (used for DLQ forensics on rejection) ────────


@pytest.mark.asyncio
async def test_event_id_extracted_from_centralops_on_reject() -> None:
    """On a deterministic 400, the rejected event_id is pulled from
    _centralops.event_id — the forensic key a DLQ row is built from."""
    client = _minimal_client()
    client._session = _mock_session(
        _mock_response(400, {"text": "Invalid data format", "code": 6})
    )
    result = await client.send_batch([_CANONICAL])
    assert result.rejected[0].event_id == "evt-abc123"


@pytest.mark.parametrize(
    "centralops,expected_id",
    [
        ({}, "?"),                       # present but no event_id
        ({"event_id": None}, "?"),       # explicit null
        ({"event_id": ""}, "?"),         # falsy empty string
        (None, "?"),                     # _centralops missing entirely
        ({"event_id": "evt-X"}, "evt-X"),
    ],
)
@pytest.mark.asyncio
async def test_event_id_fallback_question_mark(
    centralops: Any, expected_id: str
) -> None:
    """Missing/empty _centralops.event_id → '?' sentinel (never crashes, never
    emits an empty event_id into the DLQ)."""
    ev: dict[str, Any] = {"data": "x"}
    if centralops is not None:
        ev["_centralops"] = centralops
    client = _minimal_client()
    client._session = _mock_session(
        _mock_response(400, {"text": "bad", "code": 6})
    )
    result = await client.send_batch([ev])
    assert result.rejected[0].event_id == expected_id
