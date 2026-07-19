"""``sample_redis_health`` — visibilidade de evicção silenciosa do dedupe.

Duas frentes:

- happy path: ``INFO`` retorna evicted_keys/used_memory/maxmemory/policy e a
  função computa ``memory_used_ratio`` corretamente e alimenta os gauges.
- falha do Redis: best-effort — nunca propaga, retorna ``None``. Isto é
  exercitado com o Redis FAKE de verdade (``fakeredis``, via a fixture
  ``redis_client`` já usada pelo resto da suíte de dedupe) porque
  ``fakeredis`` não implementa o comando ``INFO`` — o caminho de erro não
  precisa de mock, é um erro real do double.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from ..state.dedupe import RedisHealth, sample_redis_health


class _StubRedis:
    """Double mínimo do cliente Redis externo — só o que ``sample_redis_health``
    usa (``.info()``). Mock de dependência EXTERNA (rede), não de código do
    próprio módulo sob teste."""

    def __init__(self, info: Dict[str, Any]) -> None:
        self._info = info

    async def info(self) -> Dict[str, Any]:
        return self._info


@pytest.mark.asyncio
async def test_sample_redis_health_computes_memory_ratio() -> None:
    stub = _StubRedis(
        {
            "evicted_keys": 310_000,
            "used_memory": 400 * 1024 * 1024,
            "maxmemory": 512 * 1024 * 1024,
            "maxmemory_policy": "volatile-lru",
        }
    )
    health = await sample_redis_health(stub)  # type: ignore[arg-type]
    assert health == RedisHealth(
        evicted_keys=310_000,
        used_memory_bytes=400 * 1024 * 1024,
        maxmemory_bytes=512 * 1024 * 1024,
        maxmemory_policy="volatile-lru",
    )
    assert health.memory_used_ratio == pytest.approx(400 / 512)


@pytest.mark.asyncio
async def test_sample_redis_health_zero_ratio_when_maxmemory_unset() -> None:
    """``maxmemory=0`` (sem teto) não deve virar ZeroDivisionError."""
    stub = _StubRedis(
        {
            "evicted_keys": 0,
            "used_memory": 123,
            "maxmemory": 0,
            "maxmemory_policy": "noeviction",
        }
    )
    health = await sample_redis_health(stub)  # type: ignore[arg-type]
    assert health is not None
    assert health.memory_used_ratio == 0.0


@pytest.mark.asyncio
async def test_sample_redis_health_missing_fields_default_to_zero() -> None:
    """``INFO`` de versões/builds distintos pode omitir chaves — não deve
    quebrar, deve tratar como 0/"" (best-effort, é observabilidade)."""
    health = await sample_redis_health(_StubRedis({}))  # type: ignore[arg-type]
    assert health == RedisHealth(
        evicted_keys=0, used_memory_bytes=0, maxmemory_bytes=0, maxmemory_policy=""
    )


@pytest.mark.asyncio
async def test_sample_redis_health_returns_none_on_redis_error(redis_client) -> None:
    """fakeredis não implementa ``INFO`` — exercita o caminho best-effort real
    (erro de Redis de verdade, não simulado) sem derrubar o chamador."""
    health = await sample_redis_health(redis_client)
    assert health is None
