"""Semáforo global por domínio (RNF08).

Limite máximo de requisições **concorrentes** contra um host específico
(ex: ``api-eu03.central.sophos.com``) independente de quantos workers
existam. Evita bloqueios por DoS / WAF do vendor.

Implementação: Sorted Set + Lua (leaky-lease). Cada aquisição registra
um ``(timestamp_ms, token_uuid)``; entradas expiradas (timestamp mais
velho que ``LEASE_MS``) são removidas automaticamente — isso impede
deadlocks se um worker morrer sem liberar o slot.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from typing import AsyncIterator, Dict, Mapping

import redis.asyncio as redis_async

logger = logging.getLogger(__name__)

# Lease maior que o timeout HTTP da coleta.
LEASE_MS = 30_000
POLL_SLEEP = 0.2
POLL_JITTER_MS = 100

_ACQUIRE_LUA = """
local key   = KEYS[1]
local now   = tonumber(ARGV[1])
local lease = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local token = ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, 0, now - lease)
local count = redis.call('ZCARD', key)
if count < limit then
    redis.call('ZADD', key, now, token)
    redis.call('PEXPIRE', key, lease)
    return 1
end
return 0
"""


class DomainLimiter:
    def __init__(
        self,
        redis: redis_async.Redis,
        limits_by_vendor: Mapping[str, int],
        *,
        default_limit: int = 10,
        max_wait_seconds: float = 60.0,
    ) -> None:
        self.redis = redis
        self.limits = limits_by_vendor
        self.default_limit = default_limit
        self.max_wait_seconds = max_wait_seconds

    def _limit_for_domain(self, domain: str) -> int:
        # Match simples: primeiro vendor cujo nome aparece no domínio.
        for vendor, limit in self.limits.items():
            if vendor in domain:
                return int(limit)
        return self.default_limit

    @contextlib.asynccontextmanager
    async def slot(self, domain: str) -> AsyncIterator[None]:
        limit = self._limit_for_domain(domain)
        key = f"domain_sem:{domain}"
        token = uuid.uuid4().hex

        deadline = time.monotonic() + self.max_wait_seconds
        while True:
            now_ms = int(time.time() * 1000)
            acquired = await self.redis.eval(
                _ACQUIRE_LUA, 1, key, now_ms, LEASE_MS, limit, token
            )
            if acquired:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"domain_limiter: timeout aquirindo slot domain={domain}"
                )
            await asyncio.sleep(POLL_SLEEP)

        try:
            yield
        finally:
            try:
                await self.redis.zrem(key, token)
            except Exception:  # pragma: no cover
                logger.exception("domain_limiter: falha ao liberar slot %s", domain)
