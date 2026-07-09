"""Capture-session engine tests ("listening" mode).

Uses fakeredis (no real broker). Covers the session lifecycle + the record() tap:
start, vendor filter, org isolation, active/stopped gating, event_count, list/delete.
"""
from __future__ import annotations

import fakeredis.aioredis as fakeredis_aio
import pytest

from backend.app.collectors import capture_session as cs


@pytest.fixture
def redis():
    # decode_responses=True mirrors the app's _redis_client.
    return fakeredis_aio.FakeRedis(decode_responses=True)


def _ev(vendor: str, event_id: str) -> dict:
    return {"_centralops": {"vendor": vendor, "event_id": event_id}, "data": {"x": 1}}


@pytest.mark.asyncio
async def test_start_record_read_roundtrip(redis):
    meta = await cs.start_session(redis, 7, duration_seconds=300)
    assert meta["status"] == "active"
    assert meta["organization_id"] == 7
    assert meta["vendor"] is None

    await cs.record(redis, [_ev("sophos", "a"), _ev("wazuh", "b")], 7)

    events = await cs.read_events(redis, meta["id"])
    assert len(events) == 2
    # event_count is tracked on the session meta
    refreshed = await cs.get_session(redis, meta["id"])
    assert refreshed["event_count"] == 2


@pytest.mark.asyncio
async def test_vendor_filter_records_only_matching(redis):
    meta = await cs.start_session(redis, 7, vendor="sophos")
    await cs.record(redis, [_ev("sophos", "a"), _ev("wazuh", "b"), _ev("sophos", "c")], 7)
    events = await cs.read_events(redis, meta["id"])
    assert len(events) == 2
    assert all(e["vendor"] == "sophos" for e in events)


@pytest.mark.asyncio
async def test_record_isolated_by_org(redis):
    meta = await cs.start_session(redis, 7)
    await cs.record(redis, [_ev("sophos", "a")], 999)  # different org → no capture
    assert await cs.read_events(redis, meta["id"]) == []


@pytest.mark.asyncio
async def test_stopped_session_does_not_record(redis):
    meta = await cs.start_session(redis, 7)
    assert await cs.stop_session(redis, meta["id"], 7) is True
    await cs.record(redis, [_ev("sophos", "a")], 7)
    assert await cs.read_events(redis, meta["id"]) == []
    refreshed = await cs.get_session(redis, meta["id"])
    assert refreshed["status"] == "stopped"


@pytest.mark.asyncio
async def test_stop_session_rejects_other_org(redis):
    """Defense-in-depth: parar uma sessão de outro org é no-op (não confia só no
    gate HTTP)."""
    meta = await cs.start_session(redis, 7)
    assert await cs.stop_session(redis, meta["id"], 999) is False
    refreshed = await cs.get_session(redis, meta["id"])
    assert refreshed["status"] == "active"  # inalterada


@pytest.mark.asyncio
async def test_session_cap_enforced(redis):
    """Anti-abuso: além de MAX_SESSIONS_PER_ORG sessões, start levanta."""
    for _ in range(cs.MAX_SESSIONS_PER_ORG):
        await cs.start_session(redis, 7)
    with pytest.raises(cs.CaptureLimitReached):
        await cs.start_session(redis, 7)
    # outro org não é afetado pelo teto do org 7
    other = await cs.start_session(redis, 8)
    assert other["status"] == "active"


@pytest.mark.asyncio
async def test_list_and_delete(redis):
    m1 = await cs.start_session(redis, 7, vendor="sophos")
    sessions = await cs.list_sessions(redis, 7)
    assert any(s["id"] == m1["id"] and s["vendor"] == "sophos" for s in sessions)

    await cs.delete_session(redis, m1["id"], 7)
    assert await cs.get_session(redis, m1["id"]) is None
    assert await cs.read_events(redis, m1["id"]) == []


@pytest.mark.asyncio
async def test_record_redacts_sensitive_fields(redis):
    meta = await cs.start_session(redis, 7)
    await cs.record(
        redis,
        [{"_centralops": {"vendor": "x"}, "data": {"password": "s3cr3t-token"}}],
        7,
    )
    events = await cs.read_events(redis, meta["id"])
    assert len(events) == 1
    # PII/secret redaction (reused from audit_buffer) must scrub the raw secret value.
    assert "s3cr3t-token" not in str(events[0])
