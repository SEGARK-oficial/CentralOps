"""cache multi-singleton de destinos.

A lógica de loop/versão que resolve o "Event loop is closed" do Celery
prefork vale **por destino**, e o cache mantém N destinos vivos
simultaneamente (a capacidade nova).
"""

from __future__ import annotations

import asyncio

import pytest

from backend.app.collectors.output import destination_cache
from backend.app.collectors.output.destinations.registry import (
    DestinationConfig,
    compute_config_version,
)


def _cfg(dest_id: str = "d1", host: str = "h1", version: str | None = None) -> DestinationConfig:
    config = {"host": host, "port": 514, "use_tls": False, "ca_bundle": None}
    return DestinationConfig(
        destination_id=dest_id,
        kind="syslog_rfc3164",
        config=config,
        config_version=version or compute_config_version(config, {}),
    )


@pytest.fixture(autouse=True)
def _reset_cache():
    destination_cache._cache.clear()
    destination_cache._lock = None
    destination_cache._lock_loop = None
    yield
    destination_cache._cache.clear()
    destination_cache._lock = None
    destination_cache._lock_loop = None


def test_reused_same_loop_same_version() -> None:
    async def _two():
        a = await destination_cache.get_destination(_cfg())
        b = await destination_cache.get_destination(_cfg())
        return a, b

    a, b = asyncio.run(_two())
    assert a is b


def test_recreated_when_event_loop_changes() -> None:
    """Cenário Celery prefork: dois asyncio.run() consecutivos."""
    async def _grab():
        return await destination_cache.get_destination(_cfg())

    t1 = asyncio.run(_grab())
    t2 = asyncio.run(_grab())
    assert t1 is not t2


def test_recreated_when_version_changes_same_loop() -> None:
    async def _two():
        a = await destination_cache.get_destination(_cfg(version="v1"))
        b = await destination_cache.get_destination(_cfg(host="h2", version="v2"))
        return a, b

    a, b = asyncio.run(_two())
    assert a is not b


def test_multiple_destinations_live_simultaneously() -> None:
    """A capacidade nova: 2 destinos distintos vivos ao mesmo tempo —
    impossível num singleton único."""
    async def _both():
        d1 = await destination_cache.get_destination(_cfg(dest_id="d1", host="h1"))
        d2 = await destination_cache.get_destination(_cfg(dest_id="d2", host="h2"))
        # E ambos continuam resolvíveis (cache não evicta um pelo outro).
        d1b = await destination_cache.get_destination(_cfg(dest_id="d1", host="h1"))
        return d1, d2, d1b

    d1, d2, d1b = asyncio.run(_both())
    assert d1 is not d2
    assert d1 is d1b
    assert len(destination_cache._cache) == 2


def test_reset_destinations_clears_cache() -> None:
    async def _flow():
        await destination_cache.get_destination(_cfg())
        assert destination_cache._cache
        await destination_cache.reset_destinations()
        return len(destination_cache._cache)

    assert asyncio.run(_flow()) == 0
