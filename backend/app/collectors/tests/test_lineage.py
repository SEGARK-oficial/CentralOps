"""Event lineage recorder (Redis, TTL-bounded).

Tests verify:
  - Successful delivery writes lineage per (org, event_id, destination).
  - Query returns correct entries and is org-scoped (cross-tenant isolation).
  - LINEAGE_ENABLED=False → no writes, no reads (sole gate; multi-destino é GA).
  - Redis down → fail-open (no exception, no delivery failure).
  - _record_lineage_for_batch integrates with the pipeline helper correctly.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict
from unittest.mock import patch

import fakeredis
import pytest

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

from backend.app.collectors.output import lineage as lin


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_lineage_client() -> None:
    """Reset the module-level cached Redis client between tests."""
    lin.reset()
    yield
    lin.reset()


def _fake_redis() -> fakeredis.FakeStrictRedis:
    return fakeredis.FakeStrictRedis(decode_responses=True)


def _envelope(event_id: str, org_id: int) -> Dict[str, Any]:
    """Minimal canonical envelope matching pipeline.build_envelope shape."""
    return {
        "_centralops": {
            "event_id": event_id,
            "organization_id": org_id,
            "vendor": "sophos",
            "schema_version": 1,
        },
        "normalized": {"message": "test"},
        "raw": {},
    }


# ── Unit tests: lineage.record_delivery ──────────────────────────────


def test_record_delivery_writes_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Successful delivery → entry in Redis list under org-scoped key."""
    monkeypatch.setattr("backend.app.collectors.output.lineage._is_enabled", lambda: True)
    r = _fake_redis()
    with patch.object(lin, "_redis", return_value=r):
        lin.record_delivery(
            org_id=42,
            event_id="evt-001",
            destination_id="dest-abc",
            kind="splunk_hec",
            ts=1_718_000_000.0,
        )
        key = lin._lineage_key(42, "evt-001")
        raw = r.lrange(key, 0, -1)

    assert len(raw) == 1
    entry = json.loads(raw[0])
    assert entry["destination_id"] == "dest-abc"
    assert entry["kind"] == "splunk_hec"
    assert entry["status"] == "delivered"
    assert entry["ts"] == 1_718_000_000.0


def test_record_delivery_multiple_destinations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two destinations → two entries for the same event_id + org."""
    monkeypatch.setattr("backend.app.collectors.output.lineage._is_enabled", lambda: True)
    r = _fake_redis()
    with patch.object(lin, "_redis", return_value=r):
        lin.record_delivery(org_id=1, event_id="e1", destination_id="d1", kind="jsonl")
        lin.record_delivery(org_id=1, event_id="e1", destination_id="d2", kind="otlp")
        entries = lin.query_lineage(1, "e1")

    dest_ids = {e["destination_id"] for e in entries}
    assert dest_ids == {"d1", "d2"}


def test_query_lineage_returns_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """query_lineage returns all recorded entries for an (org, event_id) pair."""
    monkeypatch.setattr("backend.app.collectors.output.lineage._is_enabled", lambda: True)
    r = _fake_redis()
    now = time.time()
    with patch.object(lin, "_redis", return_value=r):
        lin.record_delivery(org_id=10, event_id="e99", destination_id="dx", kind="syslog", ts=now)
        result = lin.query_lineage(10, "e99")

    assert len(result) == 1
    assert result[0]["destination_id"] == "dx"
    assert result[0]["status"] == "delivered"


def test_org_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Org A's events are not visible when querying Org B — cross-tenant isolation."""
    monkeypatch.setattr("backend.app.collectors.output.lineage._is_enabled", lambda: True)
    r = _fake_redis()
    with patch.object(lin, "_redis", return_value=r):
        lin.record_delivery(org_id=1, event_id="shared-evt", destination_id="d1", kind="jsonl")
        lin.record_delivery(org_id=2, event_id="shared-evt", destination_id="d2", kind="jsonl")

        result_org1 = lin.query_lineage(1, "shared-evt")
        result_org2 = lin.query_lineage(2, "shared-evt")

    assert all(e["destination_id"] == "d1" for e in result_org1)
    assert all(e["destination_id"] == "d2" for e in result_org2)


def test_lineage_disabled_does_not_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """When LINEAGE_ENABLED=False, record_delivery is a complete no-op."""
    monkeypatch.setattr("backend.app.collectors.output.lineage._is_enabled", lambda: False)
    r = _fake_redis()
    with patch.object(lin, "_redis", return_value=r):
        lin.record_delivery(org_id=5, event_id="e5", destination_id="d5", kind="otlp")
        result = lin.query_lineage(5, "e5")

    assert result == []


def test_lineage_gated_solely_by_own_flag() -> None:
    """Multi-destino é GA: _is_enabled() reflete APENAS
    LINEAGE_ENABLED — não há mais gate por flag de dispatch."""
    from backend.app.collectors.output.lineage import _is_enabled
    from backend.app.core.config import settings

    # Default: LINEAGE_ENABLED=False → recorder off.
    assert not _is_enabled()

    # Patch the instance attribute (Pydantic Settings instances allow setattr
    # because model_config has no frozen=True).
    with patch.object(settings, "LINEAGE_ENABLED", True):
        assert _is_enabled()

    # Restored after the context.
    assert not _is_enabled()


def test_redis_down_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-open: a dead Redis client must not propagate an exception."""
    monkeypatch.setattr("backend.app.collectors.output.lineage._is_enabled", lambda: True)

    class _BoomRedis:
        def pipeline(self, *_a: Any, **_kw: Any) -> "_BoomRedis":
            raise RuntimeError("connection refused")

        def lrange(self, *_a: Any, **_kw: Any) -> list:
            raise RuntimeError("connection refused")

    with patch.object(lin, "_redis", return_value=_BoomRedis()):
        lin.record_delivery(org_id=1, event_id="e1", destination_id="d1", kind="otlp")  # must not raise
        result = lin.query_lineage(1, "e1")  # must not raise

    assert result == []


def test_record_delivery_caps_list_at_max(monkeypatch: pytest.MonkeyPatch) -> None:
    """List is LTRIM-capped at _LINEAGE_LIST_MAX entries."""
    monkeypatch.setattr("backend.app.collectors.output.lineage._is_enabled", lambda: True)
    r = _fake_redis()
    with patch.object(lin, "_redis", return_value=r):
        for i in range(lin._LINEAGE_LIST_MAX + 10):
            lin.record_delivery(
                org_id=1,
                event_id="overflow-evt",
                destination_id=f"d{i}",
                kind="jsonl",
            )
        result = lin.query_lineage(1, "overflow-evt")

    assert len(result) <= lin._LINEAGE_LIST_MAX


def test_record_delivery_sets_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Redis key has a TTL set after writing."""
    monkeypatch.setattr("backend.app.collectors.output.lineage._is_enabled", lambda: True)
    r = _fake_redis()
    with patch.object(lin, "_redis", return_value=r):
        lin.record_delivery(org_id=7, event_id="ttl-evt", destination_id="d7", kind="otlp")
        key = lin._lineage_key(7, "ttl-evt")
        ttl = r.ttl(key)

    # TTL should be positive (key has an expiry set).
    assert ttl > 0


# ── Unit tests: pipeline._record_lineage_for_batch ───────────────────


def test_record_lineage_for_batch_calls_record_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    """_record_lineage_for_batch iterates the batch and records each event."""
    from backend.app.collectors.pipeline import _record_lineage_for_batch

    calls: list[dict] = []

    def _fake_record(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(
        "backend.app.collectors.output.lineage.record_delivery", _fake_record
    )

    batch = [_envelope("e1", 10), _envelope("e2", 10)]
    _record_lineage_for_batch(batch, "dest-xyz", "splunk_hec")

    assert len(calls) == 2
    assert calls[0]["event_id"] == "e1"
    assert calls[1]["event_id"] == "e2"
    assert all(c["destination_id"] == "dest-xyz" for c in calls)
    assert all(c["kind"] == "splunk_hec" for c in calls)
    assert all(c["org_id"] == 10 for c in calls)


def test_record_lineage_for_batch_skips_missing_event_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Envelopes without event_id or org_id are skipped silently."""
    from backend.app.collectors.pipeline import _record_lineage_for_batch

    calls: list[dict] = []

    def _fake_record(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(
        "backend.app.collectors.output.lineage.record_delivery", _fake_record
    )

    bad_batch = [
        {"_centralops": {"organization_id": 1}},   # missing event_id
        {"_centralops": {"event_id": "e3"}},        # missing org_id
        {},                                          # missing _centralops entirely
    ]
    _record_lineage_for_batch(bad_batch, "d1", "jsonl")

    assert calls == []


def test_record_lineage_for_batch_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """_record_lineage_for_batch must not raise even if record_delivery blows up."""
    from backend.app.collectors.pipeline import _record_lineage_for_batch

    def _boom(**kwargs: Any) -> None:
        raise RuntimeError("redis kaboom")

    monkeypatch.setattr(
        "backend.app.collectors.output.lineage.record_delivery", _boom
    )

    batch = [_envelope("e1", 5)]
    # Should not raise — the outer try/except in _record_lineage_for_batch catches it.
    _record_lineage_for_batch(batch, "d1", "otlp")


# ── lineage filtra fração rejeitada (4xx) ───────────────────────────────


def test_lineage_does_not_count_rejected_fraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lote de 3 chunks com rejeições parciais → lineage registra apenas os
    envelopes aceitos, não os event_ids que caíram no DLQ (4xx).

    Simula a lógica de filtragem introduzida em
    dispatch_batch_to_destination: rejected_event_ids acumulado cross-chunk
    é usado para excluir envelopes antes de chamar _record_lineage_for_batch.
    """
    from backend.app.collectors.pipeline import _record_lineage_for_batch

    calls: list[dict] = []

    def _fake_record(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(
        "backend.app.collectors.output.lineage.record_delivery", _fake_record
    )

    # 6 envelopes; eventos com índice ímpar (e1, e3, e5) serão "rejeitados".
    batch = [_envelope(f"e{i}", org_id=1) for i in range(6)]
    rejected_event_ids = {"e1", "e3", "e5"}

    # Replica a filtragem do dispatch_batch_to_destination.
    accepted_envelopes = [
        env for env in batch
        if (env.get("_centralops") or {}).get("event_id") not in rejected_event_ids
    ]

    _record_lineage_for_batch(accepted_envelopes, "dest-obs", "splunk_hec")

    # Apenas os 3 aceitos (e0, e2, e4) devem aparecer no lineage.
    recorded_ids = {c["event_id"] for c in calls}
    assert recorded_ids == {"e0", "e2", "e4"}, (
        f"lineage registrou IDs inesperados: {recorded_ids}"
    )
    assert len(calls) == 3


def test_lineage_full_batch_when_no_rejections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Quando não há rejeições, o batch inteiro é passado para lineage."""
    from backend.app.collectors.pipeline import _record_lineage_for_batch

    calls: list[dict] = []

    def _fake_record(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(
        "backend.app.collectors.output.lineage.record_delivery", _fake_record
    )

    batch = [_envelope(f"e{i}", org_id=2) for i in range(4)]
    # Sem rejected_event_ids → accepted_envelopes = batch.
    _record_lineage_for_batch(batch, "dest-full", "jsonl")

    assert len(calls) == 4
    assert {c["event_id"] for c in calls} == {"e0", "e1", "e2", "e3"}


@pytest.mark.parametrize("rejected_ids,total,expected_accepted", [
    (set(), 5, 5),                   # sem rejeições → todos registrados
    ({"e0", "e1", "e2"}, 5, 2),     # 3 rejeitados → 2 registrados
    ({"e0", "e1", "e2", "e3", "e4"}, 5, 0),  # todos rejeitados → 0 registrados
])
def test_lineage_filtering_parametrized(
    monkeypatch: pytest.MonkeyPatch,
    rejected_ids: set,
    total: int,
    expected_accepted: int,
) -> None:
    """Casos parametrizados de filtragem de lineage por rejected_event_ids."""
    from backend.app.collectors.pipeline import _record_lineage_for_batch

    calls: list[dict] = []

    def _fake_record(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(
        "backend.app.collectors.output.lineage.record_delivery", _fake_record
    )

    batch = [_envelope(f"e{i}", org_id=99) for i in range(total)]
    accepted_envelopes = [
        env for env in batch
        if (env.get("_centralops") or {}).get("event_id") not in rejected_ids
    ] if rejected_ids else batch

    _record_lineage_for_batch(accepted_envelopes, "dest-param", "otlp")

    assert len(calls) == expected_accepted, (
        f"rejected_ids={rejected_ids}: esperado {expected_accepted}, registrou {len(calls)}"
    )
