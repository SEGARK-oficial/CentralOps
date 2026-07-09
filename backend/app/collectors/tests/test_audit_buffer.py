"""Tests do ring buffer de auditoria — escopado por tenant."""

from __future__ import annotations

import pytest

from ..audit_buffer import _audit_key, clear, read_recent, record_batch

_ORG = 1  # organization_id usado nos testes single-tenant


def _ev(platform: str, stream: str, id_: str, **extras):
    return {
        "id": id_,
        **extras,
        "_centralops": {
            "integration_id": 42,
            "customer_id": 7,
            "organization_id": _ORG,
            "platform": platform,
            "stream": stream,
            "collected_at": "2026-04-23T14:22:10Z",
        },
    }


@pytest.mark.asyncio
async def test_record_and_read_roundtrip(redis_client) -> None:
    batch = [_ev("sophos", "alerts", "a1"), _ev("sophos", "alerts", "a2")]
    await record_batch(redis_client, batch, _ORG)

    entries = await read_recent(redis_client, _ORG, limit=10)
    assert len(entries) == 2
    ids = {e["event"]["id"] for e in entries}
    assert ids == {"a1", "a2"}
    for entry in entries:
        assert entry["envelope"]["hostname"]
        assert entry["envelope"]["pri"] == 134


@pytest.mark.asyncio
async def test_ring_trimming_respects_max_size(redis_client) -> None:
    RING_SIZE = 5
    big = [_ev("sophos", "alerts", f"id-{i}") for i in range(20)]
    await record_batch(redis_client, big, _ORG, ring_size=RING_SIZE)

    entries = await read_recent(redis_client, _ORG, limit=100)
    assert len(entries) == RING_SIZE


@pytest.mark.asyncio
async def test_filter_by_platform(redis_client) -> None:
    await record_batch(
        redis_client,
        [
            _ev("sophos", "alerts", "s1"),
            _ev("microsoft_defender", "incidents", "d1"),
            _ev("sophos", "cases", "c1"),
        ],
        _ORG,
    )
    sophos_only = await read_recent(redis_client, _ORG, platform="sophos", limit=10)
    assert {e["event"]["id"] for e in sophos_only} == {"s1", "c1"}


@pytest.mark.asyncio
async def test_filter_by_stream(redis_client) -> None:
    await record_batch(
        redis_client,
        [_ev("sophos", "alerts", "a1"), _ev("sophos", "cases", "c1")],
        _ORG,
    )
    cases_only = await read_recent(redis_client, _ORG, stream="cases", limit=10)
    assert [e["event"]["id"] for e in cases_only] == ["c1"]


@pytest.mark.asyncio
async def test_empty_batch_noop(redis_client) -> None:
    await record_batch(redis_client, [], _ORG)
    assert await read_recent(redis_client, _ORG) == []


@pytest.mark.asyncio
async def test_clear_empties_buffer(redis_client) -> None:
    await record_batch(redis_client, [_ev("sophos", "alerts", "x")], _ORG)
    removed = await clear(redis_client, _ORG)
    assert removed == 1
    assert await read_recent(redis_client, _ORG) == []


@pytest.mark.asyncio
async def test_record_survives_corrupt_event_in_read(redis_client) -> None:
    """Entrada não-JSON no ring não deve quebrar o read."""
    await redis_client.lpush(_audit_key(_ORG), "garbage-not-json{")
    await record_batch(redis_client, [_ev("sophos", "alerts", "good")], _ORG)
    entries = await read_recent(redis_client, _ORG, limit=10)
    assert [e["event"]["id"] for e in entries] == ["good"]


@pytest.mark.asyncio
async def test_read_handles_legacy_entries_without_envelope(redis_client) -> None:
    import json
    legacy = _ev("sophos", "alerts", "legacy-1")
    await redis_client.lpush(_audit_key(_ORG), json.dumps(legacy))
    await record_batch(redis_client, [_ev("sophos", "alerts", "new-2")], _ORG)

    entries = await read_recent(redis_client, _ORG, limit=10)
    by_id = {e["event"]["id"]: e for e in entries}
    assert by_id["legacy-1"]["envelope"] == {}
    assert by_id["new-2"]["envelope"].get("hostname")
    assert by_id["new-2"]["envelope"].get("pri") == 134


# ── isolamento por tenant + redação PII ─────────────────


@pytest.mark.asyncio
async def test_audit_isolated_per_organization(redis_client) -> None:
    """Tenant A grava no ring; tenant B lê ZERO de A (vazamento fechado)."""
    await record_batch(redis_client, [_ev("sophos", "alerts", "a-secret")], 1)
    # Tenant 2 lê o próprio ring (vazio).
    b_entries = await read_recent(redis_client, 2, limit=100)
    assert b_entries == [], "tenant 2 NÃO pode ver auditoria do tenant 1"
    # Controle positivo: tenant 1 vê o próprio evento.
    a_entries = await read_recent(redis_client, 1, limit=100)
    assert [e["event"]["id"] for e in a_entries] == ["a-secret"]


@pytest.mark.asyncio
async def test_pii_is_redacted_in_audit_ring(redis_client) -> None:
    """Segredos/PII não são gravados em claro no ring."""
    ev = _ev(
        "sophos", "alerts", "with-secret",
        client_secret="super-secret-value",
        nested={"access_token": "tok-123", "ok": "keep"},
    )
    await record_batch(redis_client, [ev], _ORG)
    entries = await read_recent(redis_client, _ORG, limit=10)
    stored = entries[0]["event"]
    assert stored["client_secret"] == "[REDACTED]"
    assert stored["nested"]["access_token"] == "[REDACTED]"
    assert stored["nested"]["ok"] == "keep"  # não-sensível preservado


# ── syslog_format ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_and_read_syslog_format_rfc3164(redis_client) -> None:
    await record_batch(
        redis_client, [_ev("sophos", "alerts", "fmt-rfc3164")], _ORG,
        syslog_format="rfc3164",
    )
    entries = await read_recent(redis_client, _ORG, limit=10)
    assert len(entries) == 1
    assert entries[0]["syslog_format"] == "rfc3164"


@pytest.mark.asyncio
async def test_record_and_read_syslog_format_rfc5424(redis_client) -> None:
    await record_batch(
        redis_client, [_ev("sophos", "alerts", "fmt-rfc5424")], _ORG,
        syslog_format="rfc5424",
    )
    entries = await read_recent(redis_client, _ORG, limit=10)
    assert len(entries) == 1
    assert entries[0]["syslog_format"] == "rfc5424"


@pytest.mark.asyncio
async def test_record_without_syslog_format_returns_none(redis_client) -> None:
    await record_batch(redis_client, [_ev("sophos", "alerts", "no-fmt")], _ORG)
    entries = await read_recent(redis_client, _ORG, limit=10)
    assert len(entries) == 1
    assert entries[0]["syslog_format"] is None


@pytest.mark.asyncio
async def test_legacy_entry_pre_adr008_has_no_syslog_format(redis_client) -> None:
    import json
    import socket

    legacy_item = json.dumps(
        {
            "envelope": {"hostname": socket.gethostname(), "pri": 134},
            "event": _ev("sophos", "alerts", "pre-adr008"),
        },
        separators=(",", ":"),
    )
    await redis_client.lpush(_audit_key(_ORG), legacy_item)
    entries = await read_recent(redis_client, _ORG, limit=10)
    assert len(entries) == 1
    assert entries[0]["syslog_format"] is None
