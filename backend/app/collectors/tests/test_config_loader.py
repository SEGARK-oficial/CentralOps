"""Testes do config_loader — versionamento, cache, invalidação, fallback."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from ..config_loader import (
    CACHE_KEY,
    CollectorConfigSnapshot,
    _snapshot_from_env,
    get_collector_config,
    invalidate_collector_config,
)


def test_version_is_stable_for_same_snapshot() -> None:
    s = CollectorConfigSnapshot(
        wazuh_syslog_host="h",
        wazuh_syslog_port=6514,
        wazuh_syslog_use_tls=True,
        wazuh_ca_bundle="/x",
        wazuh_dispatch_mode="syslog",
        collector_jsonl_dir="/tmp/x",
    )
    assert s.config_version == s.config_version  # idempotente


def test_version_changes_on_versioned_field() -> None:
    s1 = CollectorConfigSnapshot(wazuh_syslog_host="a")
    s2 = CollectorConfigSnapshot(wazuh_syslog_host="b")
    assert s1.config_version != s2.config_version


def test_version_stable_when_only_unversioned_changes() -> None:
    """Mudar batch_size (não-versioned) NÃO muda a versão — porque
    batch_size não exige recriar o SyslogTCPClient singleton."""
    s1 = CollectorConfigSnapshot(wazuh_syslog_host="a", collector_batch_size=200)
    s2 = CollectorConfigSnapshot(wazuh_syslog_host="a", collector_batch_size=500)
    assert s1.config_version == s2.config_version


def test_version_changes_when_use_tls_toggles() -> None:
    s1 = CollectorConfigSnapshot(wazuh_syslog_host="a", wazuh_syslog_use_tls=False)
    s2 = CollectorConfigSnapshot(wazuh_syslog_host="a", wazuh_syslog_use_tls=True)
    assert s1.config_version != s2.config_version


@pytest.mark.asyncio
async def test_cache_hit_returns_cached(redis_client) -> None:
    snapshot_dict = CollectorConfigSnapshot(
        wazuh_syslog_host="cached-host", is_persisted=True
    ).to_dict()
    await redis_client.set(CACHE_KEY, json.dumps(snapshot_dict))

    # Patch _load_from_db_sync para detectar se foi chamado (não deveria).
    with patch("backend.app.collectors.config_loader._load_from_db_sync") as mock_db:
        snapshot = await get_collector_config(redis_client)

    assert snapshot.wazuh_syslog_host == "cached-host"
    assert snapshot.is_persisted is True
    mock_db.assert_not_called()


@pytest.mark.asyncio
async def test_cache_miss_loads_db_and_populates_cache(redis_client) -> None:
    # Cache vazio
    await redis_client.delete(CACHE_KEY)

    fake_snapshot = CollectorConfigSnapshot(
        wazuh_syslog_host="db-host", is_persisted=True
    )
    with patch(
        "backend.app.collectors.config_loader._load_from_db_sync",
        return_value=fake_snapshot,
    ) as mock_db:
        snapshot = await get_collector_config(redis_client)

    mock_db.assert_called_once()
    assert snapshot.wazuh_syslog_host == "db-host"

    # Cache populado
    cached = await redis_client.get(CACHE_KEY)
    assert cached is not None
    assert "db-host" in cached


@pytest.mark.asyncio
async def test_invalidate_removes_cache_key(redis_client) -> None:
    await redis_client.set(CACHE_KEY, '{"wazuh_syslog_host":"x"}')
    await invalidate_collector_config(redis_client)
    assert await redis_client.get(CACHE_KEY) is None


def test_snapshot_from_env_has_is_persisted_false() -> None:
    snap = _snapshot_from_env()
    assert snap.is_persisted is False


@pytest.mark.asyncio
async def test_cache_with_corrupted_value_falls_back_to_db(redis_client) -> None:
    await redis_client.set(CACHE_KEY, "not valid json {")
    fake = CollectorConfigSnapshot(wazuh_syslog_host="fallback")
    with patch(
        "backend.app.collectors.config_loader._load_from_db_sync",
        return_value=fake,
    ):
        snapshot = await get_collector_config(redis_client)
    assert snapshot.wazuh_syslog_host == "fallback"
