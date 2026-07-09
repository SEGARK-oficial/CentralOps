"""Wrapper Redis para o Threat Intel: cache TTL + blacklist set ops."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from ...core.redis_client import get_redis

logger = logging.getLogger(__name__)


CACHE_KEY_PREFIX = "ti:ip:"
BLACKLIST_KEY = "ti:blacklist"
BLACKLIST_STAGING_KEY = "ti:blacklist:new"


def _cache_key(ip: str) -> str:
    return f"{CACHE_KEY_PREFIX}{ip}"


class ThreatIntelCache:
    """Pequena fachada para isolar a forma como o Threat Intel usa o Redis."""

    async def get_ip(self, ip: str) -> Optional[dict[str, Any]]:
        client = get_redis()
        try:
            payload = await client.get(_cache_key(ip))
        except Exception as exc:  # pragma: no cover - degradação graciosa
            logger.warning("Falha lendo cache para %s: %s", ip, exc)
            return None
        if not payload:
            return None
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            logger.warning("Cache inválido para %s; descartando.", ip)
            return None

    async def set_ip(self, ip: str, data: dict[str, Any], ttl_seconds: int) -> None:
        client = get_redis()
        try:
            await client.setex(_cache_key(ip), max(int(ttl_seconds), 1), json.dumps(data))
        except Exception as exc:  # pragma: no cover - degradação graciosa
            logger.warning("Falha gravando cache para %s: %s", ip, exc)

    async def is_blacklisted(self, ip: str) -> bool:
        client = get_redis()
        try:
            return bool(await client.sismember(BLACKLIST_KEY, ip))
        except Exception as exc:  # pragma: no cover - degradação graciosa
            logger.warning("Falha consultando blacklist para %s: %s", ip, exc)
            return False

    async def blacklist_size(self) -> int:
        client = get_redis()
        try:
            return int(await client.scard(BLACKLIST_KEY) or 0)
        except Exception:  # pragma: no cover
            return 0

    async def replace_blacklist(self, ips: list[str]) -> int:
        """Substitui a blacklist atomicamente: escreve em staging e renomeia."""
        client = get_redis()
        try:
            await client.delete(BLACKLIST_STAGING_KEY)
            count = 0
            chunk_size = 1000
            for i in range(0, len(ips), chunk_size):
                chunk = [ip for ip in ips[i : i + chunk_size] if ip]
                if not chunk:
                    continue
                await client.sadd(BLACKLIST_STAGING_KEY, *chunk)
                count += len(chunk)
            try:
                await client.rename(BLACKLIST_STAGING_KEY, BLACKLIST_KEY)
            except Exception:
                # Caso o RENAME falhe (ex: chave staging vazia em redis real),
                # cai-se em delete + re-popular para preservar o estado anterior.
                await client.delete(BLACKLIST_KEY)
                if count > 0:
                    for i in range(0, len(ips), chunk_size):
                        chunk = [ip for ip in ips[i : i + chunk_size] if ip]
                        if chunk:
                            await client.sadd(BLACKLIST_KEY, *chunk)
            return count
        except Exception as exc:
            logger.error("Falha substituindo blacklist: %s", exc)
            raise


cache = ThreatIntelCache()


__all__ = ["ThreatIntelCache", "cache", "BLACKLIST_KEY", "CACHE_KEY_PREFIX"]
