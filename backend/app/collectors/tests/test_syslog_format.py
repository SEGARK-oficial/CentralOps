"""RF06, RNF06 — formatação RFC 5424 + RFC 3164 + STRUCTURED-DATA + escape seguro."""

from __future__ import annotations

import re

import pytest

from ..output.rfc3164_sender import format_rfc3164
from ..output.syslog_sender import format_rfc5424


def _sample_event(**overrides) -> dict:
    base = {
        "_centralops": {
            "integration_id": 42,
            "customer_id": 7,
            "vendor": "sophos",
            "stream": "alerts",
            "event_type": "sophos.alert",
            "collected_at": "2026-04-23T14:22:10Z",
        },
        "normalized": {"class_uid": 2004, "severity_id": 4},
        "raw": {"id": "alert-1", "severity": "high"},
    }
    base["_centralops"].update(overrides)
    return base


def test_format_has_rfc5424_header() -> None:
    line = format_rfc5424(_sample_event())
    # <PRI>VERSION TIMESTAMP HOSTNAME APP-NAME PROCID MSGID ...
    # severity_id=4 (high) → PRI=131; PRI is now dynamic per event.
    assert re.match(r"^<131>1 2026-04-23T14:22:10Z \S+ centralops-collector - 42 ", line)


def test_structured_data_includes_customer_id() -> None:
    line = format_rfc5424(_sample_event())
    assert 'customer_id="7"' in line
    assert 'integration_id="42"' in line
    assert 'vendor="sophos"' in line
    assert 'stream="alerts"' in line
    assert 'event_type="sophos.alert"' in line


def test_format_accepts_legacy_platform_field() -> None:
    """Fixtures legadas que usam ``platform`` (envelope pre-Fase 1)
    continuam formatando — facilita migração de testes.
    """
    legacy = {
        "_centralops": {
            "integration_id": 1,
            "customer_id": 1,
            "platform": "sophos",
            "stream": "alerts",
            "collected_at": "2026-04-23T14:22:10Z",
        },
    }
    line = format_rfc5424(legacy)
    assert 'vendor="sophos"' in line


def test_missing_centralops_raises() -> None:
    with pytest.raises(ValueError, match="_centralops"):
        format_rfc5424({"id": "no-meta"})


def test_structured_data_escapes_special_chars() -> None:
    """``"``, ``\\``, ``]`` devem ser escapados no SD (RFC 5424 §6.3.3)."""
    evt = _sample_event(vendor='weird"\\]value')
    line = format_rfc5424(evt)
    assert 'vendor="weird\\"\\\\\\]value"' in line


def test_msg_is_single_line_json() -> None:
    """Mesmo com newlines no payload, o MSG é JSON compacto single-line."""
    evt = _sample_event()
    evt["payload"] = "line1\nline2"
    line = format_rfc5424(evt)
    # Tudo após o SD deve caber em uma linha lógica (sem LF físico).
    assert line.count("\n") == 0


# ── RFC 5424 — PRI dinâmico por severity_id ──────────────────────────


def test_format_rfc5424_uses_event_severity_id_critical() -> None:
    """severity_id=5 (critical) → PRI=130."""
    evt = _sample_event()
    evt["normalized"]["severity_id"] = 5
    line = format_rfc5424(evt)
    assert line.startswith("<130>1 ")


def test_format_rfc5424_uses_event_severity_id_high() -> None:
    """severity_id=4 (high) → PRI=131."""
    evt = _sample_event()
    evt["normalized"]["severity_id"] = 4
    line = format_rfc5424(evt)
    assert line.startswith("<131>1 ")


def test_format_rfc5424_default_pri_when_no_severity_id() -> None:
    """normalized sem severity_id → PRI=134 (info, default)."""
    evt = _sample_event()
    evt["normalized"].pop("severity_id", None)
    line = format_rfc5424(evt)
    assert line.startswith("<134>1 ")


# ── RFC 3164 — formato e PRI dinâmico ────────────────────────────────


def _sample_event_3164(**overrides) -> dict:
    base = {
        "_centralops": {
            "integration_id": 42,
            "customer_id": 7,
            "vendor": "centralops",
            "stream": "scheduled_query",
            "event_type": "centralops.scheduled_query.match",
            "collected_at": "2026-04-23T14:22:10Z",
        },
        "normalized": {"severity_id": 5},
        "raw": {},
    }
    base["_centralops"].update(overrides)
    return base


def test_format_rfc3164_uses_event_severity_id_critical() -> None:
    """severity_id=5 (critical) → PRI=130 no RFC 3164."""
    event = _sample_event_3164()
    event["normalized"]["severity_id"] = 5
    line = format_rfc3164(event)
    assert line.startswith("<130>")


def test_format_rfc3164_uses_event_severity_id_high() -> None:
    """severity_id=4 (high) → PRI=131 no RFC 3164."""
    event = _sample_event_3164()
    event["normalized"]["severity_id"] = 4
    line = format_rfc3164(event)
    assert line.startswith("<131>")


def test_format_rfc3164_default_pri_when_no_severity_id() -> None:
    """normalized sem severity_id → PRI=134 (info, default) no RFC 3164."""
    event = _sample_event_3164()
    event["normalized"].pop("severity_id", None)
    line = format_rfc3164(event)
    assert line.startswith("<134>")


def test_format_rfc3164_header_structure() -> None:
    """Cabeçalho RFC 3164: <PRI>Mmm dd HH:MM:SS hostname app[pid]: {json}."""
    event = _sample_event_3164()
    line = format_rfc3164(event)
    # Exemplo: <134>May  6 12:34:56 hostname centralops[1234]: {...}
    assert re.match(r"^<\d+>[A-Z][a-z]{2} [ \d]\d \d{2}:\d{2}:\d{2} \S+ centralops\[\d+\]: \{", line)


def test_format_rfc3164_raises_on_empty_event() -> None:
    with pytest.raises(ValueError):
        format_rfc3164({})


def test_format_rfc3164_msg_is_compact_json() -> None:
    """MSG do RFC 3164 é JSON compacto sem LF."""
    event = _sample_event_3164()
    line = format_rfc3164(event)
    assert line.count("\n") == 0
