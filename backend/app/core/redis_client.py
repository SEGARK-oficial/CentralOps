"""Redis async client com fallback in-memory para o Threat Intel Middleware.

Quando ``settings.REDIS_URL`` não está configurada (ou a conexão falha), o
sistema continua respondendo com um backend em memória que implementa o
subconjunto de comandos usado pelo Threat Intel. Isto satisfaz o
Requisito Não-Funcional 3 (degradação graciosa) e permite rodar a stack
sem o serviço Redis em desenvolvimento local.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Iterable, Optional, Set

try:  # pragma: no cover - import opcional
    import redis.asyncio as redis_async  # type: ignore
    from redis.exceptions import RedisError  # type: ignore
except ImportError:  # pragma: no cover
    redis_async = None  # type: ignore[assignment]

    class RedisError(Exception):  # type: ignore[no-redef]
        pass


from .config import settings

logger = logging.getLogger(__name__)


class _InMemoryRedisFallback:
    """Backend em memória usado quando Redis não está disponível.

    Implementa os comandos exercitados pelo Threat Intel (``get``, ``setex``,
    ``delete``, ``sismember``, ``sadd``, ``srem``, ``scard``, ``smembers``,
    ``rename``, ``exists``, ``ping``). TTL é avaliado em leitura.
    """

    def __init__(self) -> None:
        self._strings: dict[str, tuple[str, Optional[float]]] = {}
        self._sets: dict[str, Set[str]] = {}
        self._lock = asyncio.Lock()

    def _is_expired(self, key: str) -> bool:
        record = self._strings.get(key)
        if not record:
            return True
        _value, expires_at = record
        if expires_at is not None and time.monotonic() > expires_at:
            self._strings.pop(key, None)
            return True
        return False

    async def get(self, key: str) -> Optional[str]:
        async with self._lock:
            if self._is_expired(key):
                return None
            return self._strings[key][0]

    async def setex(self, key: str, ttl_seconds: int, value: str) -> bool:
        async with self._lock:
            expires_at = time.monotonic() + max(int(ttl_seconds), 0)
            self._strings[key] = (str(value), expires_at)
            return True

    async def set(self, key: str, value: str, ex: Optional[int] = None) -> bool:
        async with self._lock:
            expires_at = time.monotonic() + int(ex) if ex else None
            self._strings[key] = (str(value), expires_at)
            return True

    async def delete(self, *keys: str) -> int:
        removed = 0
        async with self._lock:
            for key in keys:
                if key in self._strings:
                    self._strings.pop(key)
                    removed += 1
                if key in self._sets:
                    self._sets.pop(key)
                    removed += 1
        return removed

    async def exists(self, key: str) -> int:
        async with self._lock:
            if key in self._strings and not self._is_expired(key):
                return 1
            if key in self._sets:
                return 1
            return 0

    async def sismember(self, key: str, member: str) -> bool:
        async with self._lock:
            return member in self._sets.get(key, set())

    async def sadd(self, key: str, *members: str) -> int:
        async with self._lock:
            bucket = self._sets.setdefault(key, set())
            before = len(bucket)
            bucket.update(str(m) for m in members)
            return len(bucket) - before

    async def srem(self, key: str, *members: str) -> int:
        async with self._lock:
            bucket = self._sets.get(key)
            if not bucket:
                return 0
            removed = 0
            for member in members:
                if member in bucket:
                    bucket.remove(member)
                    removed += 1
            return removed

    async def scard(self, key: str) -> int:
        async with self._lock:
            return len(self._sets.get(key, set()))

    async def smembers(self, key: str) -> Set[str]:
        async with self._lock:
            return set(self._sets.get(key, set()))

    async def rename(self, src: str, dst: str) -> bool:
        async with self._lock:
            if src in self._sets:
                self._sets[dst] = self._sets.pop(src)
                return True
            if src in self._strings:
                self._strings[dst] = self._strings.pop(src)
                return True
            return False

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:  # pragma: no cover - sem recurso a liberar
        return None


class RedisManager:
    """Wrapper compartilhado para o cliente Redis com health-check e fallback."""

    def __init__(self) -> None:
        self._client: Any = None
        self._mode: str = "uninitialized"  # "redis" | "in_memory" | "uninitialized"
        self._last_error: Optional[str] = None

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def is_degraded(self) -> bool:
        return self._mode != "redis"

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def client(self) -> Any:
        if self._client is None:
            # Inicialização lazy para tornar o módulo seguro mesmo se o
            # lifespan ainda não conectou (testes, scripts ad-hoc).
            self._client = _InMemoryRedisFallback()
            self._mode = "in_memory"
        return self._client

    async def connect(self, url: Optional[str] = None) -> None:
        """Tenta conectar ao Redis; em caso de falha, usa fallback in-memory."""
        target_url = url if url is not None else settings.REDIS_URL

        if not target_url:
            logger.info("REDIS_URL não configurada — usando backend in-memory para Threat Intel.")
            self._client = _InMemoryRedisFallback()
            self._mode = "in_memory"
            self._last_error = None
            return

        if redis_async is None:
            logger.warning(
                "Pacote 'redis' não instalado; usando backend in-memory para Threat Intel."
            )
            self._client = _InMemoryRedisFallback()
            self._mode = "in_memory"
            self._last_error = "redis package not installed"
            return

        try:
            client = redis_async.from_url(
                target_url,
                decode_responses=True,
                socket_timeout=5,
                socket_connect_timeout=5,
                health_check_interval=30,
            )
            await client.ping()
            self._client = client
            self._mode = "redis"
            self._last_error = None
            logger.info("Redis conectado em %s.", target_url)
        except (RedisError, OSError, ValueError) as exc:
            logger.warning(
                "Falha conectando ao Redis (%s); usando fallback in-memory.", exc
            )
            self._client = _InMemoryRedisFallback()
            self._mode = "in_memory"
            self._last_error = str(exc)

    async def disconnect(self) -> None:
        client = self._client
        self._client = None
        self._mode = "uninitialized"
        if client is None:
            return
        try:
            close = getattr(client, "aclose", None) or getattr(client, "close", None)
            if close is not None:
                result = close()
                if asyncio.iscoroutine(result):
                    await result
        except Exception:  # pragma: no cover - best effort
            logger.exception("Erro ao desconectar Redis.")

    async def healthcheck(self) -> bool:
        try:
            return bool(await self.client.ping())
        except Exception as exc:  # pragma: no cover - best effort
            self._last_error = str(exc)
            return False


redis_manager = RedisManager()


def get_redis() -> Any:
    """Retorna o cliente Redis (real ou fallback). Use sempre via este helper."""
    return redis_manager.client


__all__ = ["redis_manager", "get_redis", "RedisManager", "_InMemoryRedisFallback"]
