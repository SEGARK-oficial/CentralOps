"""RF08 — refresh OAuth coordenado: N workers concorrentes → 1 refresh."""

from __future__ import annotations

import asyncio

import pytest

from ..auth.oauth_cache import get_or_refresh_token, invalidate


class _RefreshCounter:
    def __init__(self, expires_in: int = 3600) -> None:
        self.calls = 0
        self.expires_in = expires_in

    async def __call__(self, integration_id: int) -> dict:
        self.calls += 1
        # Simula 50ms de latência de rede — dá tempo para concorrência real.
        await asyncio.sleep(0.05)
        return {
            "access_token": f"token-{integration_id}-{self.calls}",
            "expires_in": self.expires_in,
        }


@pytest.mark.asyncio
async def test_first_call_refreshes(redis_client) -> None:
    counter = _RefreshCounter()
    token = await get_or_refresh_token(redis_client, 1, counter)
    assert token == "token-1-1"
    assert counter.calls == 1


@pytest.mark.asyncio
async def test_subsequent_call_hits_cache(redis_client) -> None:
    counter = _RefreshCounter()
    await get_or_refresh_token(redis_client, 1, counter)
    await get_or_refresh_token(redis_client, 1, counter)
    assert counter.calls == 1  # segunda chamada serviu do cache


@pytest.mark.asyncio
async def test_thundering_herd_calls_refresh_only_once(redis_client) -> None:
    """20 workers paralelos solicitando token simultaneamente → 1 refresh HTTP."""
    counter = _RefreshCounter()
    tokens = await asyncio.gather(
        *[get_or_refresh_token(redis_client, 7, counter) for _ in range(20)]
    )
    assert counter.calls == 1, (
        f"esperado 1 refresh, foram {counter.calls} — lock distribuído falhou"
    )
    # Todos recebem o mesmo token.
    assert len(set(tokens)) == 1


@pytest.mark.asyncio
async def test_invalidate_forces_new_refresh(redis_client) -> None:
    counter = _RefreshCounter()
    await get_or_refresh_token(redis_client, 1, counter)
    await invalidate(redis_client, 1)
    await get_or_refresh_token(redis_client, 1, counter)
    assert counter.calls == 2
