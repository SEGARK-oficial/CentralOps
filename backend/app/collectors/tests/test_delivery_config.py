"""Validated DeliveryConfig schema.

Covers: defaults, bounds, extra=forbid, per-kind deep-merge, lenient hot-path
parse, create/update API validation (422 on bad delivery), catalog exposure,
and circuit_breaker reading BreakerConfig as the single source of truth.

Additions: RetryConfig exponential-backoff fields (initial_ms, max_ms,
multiplier) + backoff_max_s legacy migration.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest

from backend.app.collectors.output.delivery_config import (
    BreakerConfig,
    DeliveryConfig,
    RetryConfig,
    backoff_delay_s,
    deep_merge,
    parse_delivery,
    parse_delivery_lenient,
)


# ── Schema defaults + bounds ──────────────────────────────────────────────


def test_delivery_config_defaults() -> None:
    d = DeliveryConfig()
    assert d.concurrency == 4
    assert d.backpressure == "persistent_queue"
    assert d.queue_ceiling == 0
    assert d.shadow is False
    assert d.breaker.failure_threshold == 5
    assert d.breaker.cooldown_s == 30
    assert d.breaker.window_s == 60
    assert d.batch.max_items == 500
    # new RetryConfig defaults
    assert d.retry.max_retries == 3
    assert d.retry.initial_ms == 200
    assert d.retry.max_ms == 5000
    assert d.retry.multiplier == 2.0
    assert d.timeout_ms == 30000


@pytest.mark.parametrize(
    "field,value",
    [
        ("concurrency", 0),
        ("concurrency", 257),
        ("queue_ceiling", -1),
        ("timeout_ms", 50),
    ],
)
def test_delivery_config_bounds_rejected(field, value) -> None:
    with pytest.raises(Exception):
        DeliveryConfig(**{field: value})


def test_delivery_config_unknown_key_rejected() -> None:
    """extra=forbid: a typo'd key is a hard error (not silently ignored)."""
    with pytest.raises(Exception):
        DeliveryConfig(concurency=4)  # typo


def test_breaker_bounds_rejected() -> None:
    with pytest.raises(Exception):
        BreakerConfig(failure_threshold=0)
    with pytest.raises(Exception):
        BreakerConfig(cooldown_s=999999)
    with pytest.raises(Exception):
        BreakerConfig(unknown_field=1)


def test_backpressure_enum_rejected() -> None:
    with pytest.raises(Exception):
        DeliveryConfig(backpressure="explode")


# ── deep_merge + per-kind defaults ────────────────────────────────────────


def test_deep_merge_nested_override_wins() -> None:
    base = {"breaker": {"failure_threshold": 5, "cooldown_s": 30}, "concurrency": 4}
    override = {"breaker": {"cooldown_s": 10}, "shadow": True}
    merged = deep_merge(base, override)
    assert merged["breaker"]["failure_threshold"] == 5  # kept from base
    assert merged["breaker"]["cooldown_s"] == 10  # override wins
    assert merged["concurrency"] == 4
    assert merged["shadow"] is True
    # inputs not mutated
    assert base["breaker"]["cooldown_s"] == 30


def test_parse_delivery_applies_kind_defaults() -> None:
    """jsonl declares concurrency=1; empty user delivery → kind default."""
    assert parse_delivery("jsonl", {}).concurrency == 1
    assert parse_delivery("splunk_hec", {}).concurrency == 8
    assert parse_delivery("syslog_rfc3164", {}).concurrency == 2


def test_parse_delivery_user_overrides_kind_default() -> None:
    assert parse_delivery("jsonl", {"concurrency": 16}).concurrency == 16


def test_parse_delivery_unknown_kind_uses_model_defaults() -> None:
    # unknown kind → no kind defaults → model default concurrency=4
    assert parse_delivery("does_not_exist", {}).concurrency == 4


def test_parse_delivery_strict_raises_on_bad() -> None:
    with pytest.raises(Exception):
        parse_delivery("splunk_hec", {"concurrency": 0})
    with pytest.raises(Exception):
        parse_delivery("splunk_hec", {"typo": 1})


def test_parse_delivery_lenient_never_raises() -> None:
    # invalid value → falls back to kind defaults, never raises
    assert parse_delivery_lenient("splunk_hec", {"concurrency": 0}).concurrency == 8
    assert parse_delivery_lenient("jsonl", {"typo": 1}).concurrency == 1
    # garbage type → model defaults
    assert parse_delivery_lenient("does_not_exist", {"concurrency": -5}).concurrency == 4


def test_empty_delivery_is_byte_identical_defaults() -> None:
    """Every existing row (incl. wazuh-default) has delivery={} → all defaults,
    never an error (byte-identity invariant)."""
    d = parse_delivery_lenient("splunk_hec", {})
    assert d.concurrency == 8  # kind default
    assert d.breaker.failure_threshold == 5  # model default
    assert d.shadow is False


# ── API create/update validation ──────────────────────────────────────────


def test_destination_create_rejects_bad_delivery() -> None:
    from backend.app.api.schemas_destinations import DestinationCreate

    with pytest.raises(Exception):
        DestinationCreate(
            name="bad",
            kind="splunk_hec",
            config={"url": "https://x:8088"},
            delivery={"concurrency": 0},  # out of range
        )
    with pytest.raises(Exception):
        DestinationCreate(
            name="typo",
            kind="splunk_hec",
            config={"url": "https://x:8088"},
            delivery={"concurency": 4},  # typo
        )


def test_destination_create_accepts_valid_delivery() -> None:
    from backend.app.api.schemas_destinations import DestinationCreate

    dc = DestinationCreate(
        name="good",
        kind="splunk_hec",
        config={"url": "https://x:8088"},
        delivery={"concurrency": 16, "breaker": {"failure_threshold": 3}},
    )
    assert dc.delivery["concurrency"] == 16


def test_destination_update_rejects_bad_delivery() -> None:
    from backend.app.api.schemas_destinations import DestinationUpdate

    with pytest.raises(Exception):
        DestinationUpdate(delivery={"backpressure": "nope"})
    # valid partial passes
    upd = DestinationUpdate(delivery={"shadow": True})
    assert upd.delivery["shadow"] is True


# ── Catalog exposure ───────────────────────────────────────────────────────


def test_catalog_exposes_delivery_schema_and_defaults() -> None:
    from backend.app.collectors.output.destinations import registry

    desc = registry.get("splunk_hec").describe()
    assert "delivery_schema" in desc
    assert desc["delivery_schema"]["type"] == "object"
    assert desc["delivery_defaults"] == {"concurrency": 8}
    # all kinds describe without error
    for entry in registry.describe_all():
        assert "delivery_schema" in entry
        assert "delivery_defaults" in entry


# ── circuit_breaker single-source-of-truth ─────────────────────────────────


def test_circuit_breaker_reads_breaker_config() -> None:
    from backend.app.collectors.circuit_breaker import _breaker_cfg

    assert _breaker_cfg({}) == (5, 30, 60)
    assert _breaker_cfg({"breaker": {"failure_threshold": 3, "cooldown_s": 10}}) == (
        3,
        10,
        60,
    )
    # invalid → defaults (lenient; create-time validation catches it earlier)
    assert _breaker_cfg({"breaker": {"failure_threshold": 99999}}) == (5, 30, 60)


def test_breaker_cfg_out_of_bounds_reverts_all_and_warns(caplog) -> None:
    """One out-of-bounds field reverts ALL breaker fields to stock and logs a
    WARNING (make the silent revert diagnosable)."""
    import logging

    from backend.app.collectors.circuit_breaker import _breaker_cfg

    with caplog.at_level(logging.WARNING):
        # cooldown_s=7200 is out of bounds (le=3600); failure_threshold=3 is valid
        result = _breaker_cfg({"breaker": {"failure_threshold": 3, "cooldown_s": 7200}})

    assert result == (5, 30, 60)  # ALL reverted, not just the offending field
    assert "inválida" in caplog.text


# ── RetryConfig — exponential backoff + legacy migration ──────────────────


def test_retry_config_defaults() -> None:
    r = RetryConfig()
    assert r.max_retries == 3
    assert r.initial_ms == 200
    assert r.max_ms == 5000
    assert r.multiplier == 2.0


def test_retry_config_backoff_delay_exponential() -> None:
    """backoff_delay_s honours initial_ms, multiplier, and caps at max_ms."""
    r = RetryConfig(initial_ms=100, max_ms=800, multiplier=2.0, max_retries=5)
    # attempt 0 → 100 ms
    assert backoff_delay_s(r,0) == pytest.approx(0.1)
    # attempt 1 → 200 ms
    assert backoff_delay_s(r,1) == pytest.approx(0.2)
    # attempt 2 → 400 ms
    assert backoff_delay_s(r,2) == pytest.approx(0.4)
    # attempt 3 → would be 800 ms = max_ms (not 800*2=1600)
    assert backoff_delay_s(r,3) == pytest.approx(0.8)
    # attempt 4 → capped at max_ms=800 ms
    assert backoff_delay_s(r,4) == pytest.approx(0.8)


def test_retry_config_backoff_delay_cap() -> None:
    """max_ms cap prevents runaway delays."""
    r = RetryConfig(initial_ms=1000, max_ms=3000, multiplier=3.0, max_retries=10)
    # attempt 0 → 1000 ms
    assert backoff_delay_s(r,0) == pytest.approx(1.0)
    # attempt 1 → 3000 ms (capped)
    assert backoff_delay_s(r,1) == pytest.approx(3.0)
    # attempt 9 → still capped
    assert backoff_delay_s(r,9) == pytest.approx(3.0)


@pytest.mark.parametrize(
    "backoff_max_s,expected_max_ms",
    [
        (10, 10000),
        (60, 60000),
        (1, 1000),
    ],
)
def test_retry_config_legacy_backoff_max_s_migrated(
    backoff_max_s: int, expected_max_ms: int
) -> None:
    """Old ``backoff_max_s`` is silently migrated to ``max_ms`` (back-compat)."""
    r = RetryConfig(backoff_max_s=backoff_max_s)
    assert r.max_ms == expected_max_ms


def test_retry_config_max_ms_wins_over_legacy() -> None:
    """When both ``max_ms`` and legacy ``backoff_max_s`` are present, ``max_ms`` wins."""
    r = RetryConfig(max_ms=1234, backoff_max_s=999)
    assert r.max_ms == 1234


def test_retry_config_extra_forbid() -> None:
    with pytest.raises(Exception):
        RetryConfig(unknown_key=1)


def test_retry_config_bounds_rejected() -> None:
    with pytest.raises(Exception):
        RetryConfig(max_retries=-1)
    with pytest.raises(Exception):
        RetryConfig(multiplier=0.5)  # ge=1.0
    with pytest.raises(Exception):
        RetryConfig(initial_ms=5)  # ge=10
