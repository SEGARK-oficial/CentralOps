"""RNF08 — limite de concorrência por domínio (semáforo distribuído)."""

from __future__ import annotations

import asyncio

import pytest

from ..domain_limiter import DomainLimiter


@pytest.mark.asyncio
async def test_respects_limit_per_domain(redis_client) -> None:
    limiter = DomainLimiter(redis_client, {"sophos": 2}, max_wait_seconds=3.0)

    acquired: list[int] = []
    released: list[int] = []

    async def worker(idx: int) -> None:
        async with limiter.slot("api-eu03.central.sophos.com"):
            acquired.append(idx)
            await asyncio.sleep(0.1)
            released.append(idx)

    # Dispara 5 workers concorrentes num limite de 2 — em qualquer
    # instante deve haver no máximo 2 slots ativos.
    await asyncio.gather(*[worker(i) for i in range(5)])

    assert sorted(acquired) == list(range(5))
    assert sorted(released) == list(range(5))


@pytest.mark.asyncio
async def test_unknown_domain_uses_default_limit(redis_client) -> None:
    limiter = DomainLimiter(
        redis_client, {"sophos": 10}, default_limit=3, max_wait_seconds=2.0
    )
    # Apenas verifica que não lança com domínio desconhecido.
    async with limiter.slot("api.desconhecido.com"):
        pass
