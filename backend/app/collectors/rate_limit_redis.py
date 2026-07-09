"""Rate limiter distribuído por (tenant, vendor) com sliding window (RF05).

Implementação via Sorted Set do Redis + script Lua atômico:

    Key:   rl:{vendor}:{tenant}:{window}   (ex: rl:sophos:42:s, :m, :h)
    Value: ZSET — score = unix_ms_now, member = uuid

O script remove entradas fora da janela e, se ``ZCARD < limite``,
adiciona o novo hit. Para 429 explícito retornado pelo vendor, usamos
uma segunda chave ``rl:backoff:{vendor}`` com TTL — antes de qualquer
tentativa consultamos seu TTL e dormimos se necessário (coordena todos
os workers).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Dict, Mapping

import redis.asyncio as redis_async

from .metrics import RATE_LIMIT_BACKOFFS

logger = logging.getLogger(__name__)

# Sliding window + check + add, tudo atômico no servidor.
_SLIDING_WINDOW_LUA = """
local key   = KEYS[1]
local now   = tonumber(ARGV[1])
local win   = tonumber(ARGV[2])  -- em milissegundos
local limit = tonumber(ARGV[3])
local token = ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, 0, now - win)
local count = redis.call('ZCARD', key)
if count < limit then
    redis.call('ZADD', key, now, token)
    redis.call('PEXPIRE', key, win)
    return 1
end
return 0
"""


class RedisRateLimiter:
    """Limiter por múltiplas janelas (s / m / h) por (vendor, tenant)."""

    def __init__(
        self,
        redis: redis_async.Redis,
        limits_by_vendor: Mapping[str, Mapping[str, int]],
        *,
        max_wait_seconds: float = 30.0,
    ) -> None:
        self.redis = redis
        self.limits = limits_by_vendor
        self.max_wait_seconds = max_wait_seconds

    # ── 429 global por vendor ─────────────────────────────────────────

    async def backoff(self, vendor: str, retry_after: int) -> None:
        """Registra um backoff 429 para **todos** os workers respeitarem."""
        key = f"rl:backoff:{vendor}"
        await self.redis.set(key, "1", ex=max(1, int(retry_after)))
        RATE_LIMIT_BACKOFFS.labels(vendor=vendor).inc()

    async def _wait_if_vendor_backoff(self, vendor: str) -> None:
        ttl = await self.redis.ttl(f"rl:backoff:{vendor}")
        if ttl and ttl > 0:
            logger.info(
                "rate_limit: aguardando backoff vendor=%s retry_in=%ss", vendor, ttl
            )
            await asyncio.sleep(min(ttl, self.max_wait_seconds))

    # ── Sliding window por vendor+tenant ──────────────────────────────

    async def acquire(self, tenant_id: int, vendor: str) -> None:
        """Bloqueia até haver budget em todas as janelas configuradas."""
        await self._wait_if_vendor_backoff(vendor)

        limits = self.limits.get(vendor, {})
        if not limits:
            return  # sem limits configurados → não limita (conservador)

        deadline = time.monotonic() + self.max_wait_seconds
        while True:
            ok = True
            for window_name, limit in limits.items():
                win_ms = _WINDOW_MS.get(window_name)
                if not win_ms or limit <= 0:
                    continue
                key = f"rl:{vendor}:{tenant_id}:{window_name}"
                now_ms = int(time.time() * 1000)
                token = uuid.uuid4().hex
                allowed = await self.redis.eval(
                    _SLIDING_WINDOW_LUA, 1, key, now_ms, win_ms, limit, token
                )
                if not allowed:
                    ok = False
                    break
            if ok:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"rate_limit: deadline excedido vendor={vendor} tenant={tenant_id}"
                )
            # Pequeno sleep com jitter mínimo — evita livelock.
            await asyncio.sleep(0.2)


_WINDOW_MS: Dict[str, int] = {
    "per_second": 1_000,
    "per_minute": 60_000,
    "per_hour": 3_600_000,
    "per_day": 86_400_000,
}
