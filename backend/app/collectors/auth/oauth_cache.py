"""Cache de access_token OAuth2 com **lock distribuído** (RF08).

Problema: N workers coletando para a mesma integração simultaneamente
descobrem que o token expirou. Sem coordenação, **N** chamadas de
refresh ocorrem em paralelo — thundering herd no endpoint do IdP,
saturação de rate limit e risco de o vendor invalidar tokens antigos.

Solução: lock Redis com ``SET NX PX`` por integração. Apenas o primeiro
worker executa o refresh; os demais esperam (poll curto) e relêem do
cache assim que populado.

Contrato do ``RefreshFn`` (vendor-específico):

    async def refresh_fn(integration_id: int) -> dict:
        # retorna {"access_token": str, "expires_in": int, "refresh_token": str?}
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Awaitable, Callable

import redis.asyncio as redis_async

from ..metrics import OAUTH_EXPIRES

logger = logging.getLogger(__name__)

TOKEN_KEY = "oauth:token:{integration_id}"
LOCK_KEY = "oauth:lock:{integration_id}"

# Lock TTL precisa cobrir o pior caso do refresh HTTP (3–5s esperado).
LOCK_TTL_MS = 15_000

# Renovamos proativamente N segundos antes do vencimento real do token
# para evitar corrida com o vendor.
SKEW_SECONDS = 60

# Poll budget enquanto outro worker renova: 30 × 100ms = 3s total.
POLL_MAX_ITER = 30
POLL_SLEEP = 0.1

RefreshFn = Callable[[int], Awaitable[dict]]

# CAS seguro: só apaga o lock se o token ainda for nosso.
_UNLOCK_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] "
    "then return redis.call('del', KEYS[1]) else return 0 end"
)


async def get_or_refresh_token(
    redis: redis_async.Redis,
    integration_id: int,
    refresh_fn: RefreshFn,
    vendor: str = "",
) -> str:
    """Retorna um access_token válido; renova se necessário."""

    key = TOKEN_KEY.format(integration_id=integration_id)
    cached = await redis.get(key)
    if cached:
        return json.loads(cached)["access_token"]

    lock_key = LOCK_KEY.format(integration_id=integration_id)
    lock_token = uuid.uuid4().hex
    acquired = await redis.set(lock_key, lock_token, nx=True, px=LOCK_TTL_MS)

    if not acquired:
        # Outro worker está fazendo o refresh — aguarda o cache popular.
        for _ in range(POLL_MAX_ITER):
            await asyncio.sleep(POLL_SLEEP)
            cached = await redis.get(key)
            if cached:
                return json.loads(cached)["access_token"]
        raise TimeoutError(
            f"timeout aguardando refresh de token integration={integration_id}"
        )

    try:
        logger.info("oauth_cache: refreshing integration=%s", integration_id)
        tokens = await refresh_fn(integration_id)

        access_token = tokens["access_token"]
        expires_in = int(tokens.get("expires_in", 3600))
        ttl = max(60, expires_in - SKEW_SECONDS)

        await redis.set(
            key,
            json.dumps(
                {
                    "access_token": access_token,
                    "issued_at": int(time.time()),
                    "expires_in": expires_in,
                }
            ),
            ex=ttl,
        )

        if vendor:
            OAUTH_EXPIRES.labels(
                integration_id=str(integration_id), vendor=vendor
            ).set(ttl)

        return access_token
    finally:
        try:
            await redis.eval(_UNLOCK_LUA, 1, lock_key, lock_token)
        except Exception:  # pragma: no cover
            logger.exception("oauth_cache: falha ao liberar lock integration=%s", integration_id)


async def invalidate(redis: redis_async.Redis, integration_id: int) -> None:
    """Força próxima chamada a refrescar (ex: após 401 do vendor)."""
    await redis.delete(TOKEN_KEY.format(integration_id=integration_id))
