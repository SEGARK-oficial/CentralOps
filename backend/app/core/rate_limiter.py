"""Sliding-window rate limiter respecting Sophos Central API quotas.

Limits tracked per-tenant:
  - Global API: 10/s, 100/m, 1000/h, 50000/d
  - XDR Query runs: 10/m, 500/d

The limiter blocks (via ``asyncio.sleep``) when a window is exhausted and
honours the ``Retry-After`` header returned by 429 responses.

AuthAttemptLimiter é respaldado por Redis quando ``settings.REDIS_URL`` está
configurado, garantindo estado compartilhado entre workers gunicorn. Quando Redis
não está disponível (dev local), recai em implementação in-memory.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List

from .config import settings

logger = logging.getLogger(__name__)

# Limite de usernames distintos por IP em uma janela de autenticação.
# Atacante que envia 1M usernames únicos de um único IP (DoS de cardinalidade)
# terá o IP lockado após exceder este limiar.
_MAX_DISTINCT_USERS_PER_IP = 50


@dataclass
class _Window:
    """A single sliding-window counter."""

    duration: float  # seconds
    max_requests: int
    timestamps: List[float] = field(default_factory=list)

    def _prune(self, now: float) -> None:
        cutoff = now - self.duration
        while self.timestamps and self.timestamps[0] < cutoff:
            self.timestamps.pop(0)

    def remaining(self) -> int:
        self._prune(time.monotonic())
        return max(0, self.max_requests - len(self.timestamps))

    def wait_time(self) -> float:
        """Seconds to wait before the next request is allowed."""
        now = time.monotonic()
        self._prune(now)
        if len(self.timestamps) < self.max_requests:
            return 0.0
        oldest = self.timestamps[0]
        return max(0.0, (oldest + self.duration) - now)

    def record(self) -> None:
        self.timestamps.append(time.monotonic())


class RateLimiter:
    """Per-tenant sliding-window rate limiter for the Sophos global API."""

    def __init__(self) -> None:
        self._locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._windows: Dict[str, List[_Window]] = {}
        self._retry_until: Dict[str, float] = {}

    def _get_windows(self, tenant_id: str) -> List[_Window]:
        if tenant_id not in self._windows:
            self._windows[tenant_id] = [
                _Window(duration=1, max_requests=settings.RATE_LIMIT_PER_SECOND),
                _Window(duration=60, max_requests=settings.RATE_LIMIT_PER_MINUTE),
                _Window(duration=3600, max_requests=settings.RATE_LIMIT_PER_HOUR),
                _Window(duration=86400, max_requests=settings.RATE_LIMIT_PER_DAY),
            ]
        return self._windows[tenant_id]

    async def acquire(self, tenant_id: str) -> None:
        """Wait until a request slot is available for *tenant_id*."""
        async with self._locks[tenant_id]:
            # Honour a previous 429 Retry-After
            retry_until = self._retry_until.get(tenant_id, 0)
            wait = retry_until - time.monotonic()
            if wait > 0:
                logger.info("Rate limiter: waiting %.1fs (429 backoff) for tenant %s", wait, tenant_id)
                await asyncio.sleep(wait)

            windows = self._get_windows(tenant_id)
            for win in windows:
                wait = win.wait_time()
                if wait > 0:
                    logger.info(
                        "Rate limiter: waiting %.1fs (window %.0fs) for tenant %s",
                        wait, win.duration, tenant_id,
                    )
                    await asyncio.sleep(wait)

            for win in windows:
                win.record()

    def handle_429(self, tenant_id: str, retry_after: int | None = None) -> None:
        """Record a 429 response so future calls wait accordingly."""
        delay = retry_after if retry_after and retry_after > 0 else 5
        self._retry_until[tenant_id] = time.monotonic() + delay
        logger.warning("Rate limiter: 429 received for tenant %s, backing off %ds", tenant_id, delay)


class QueryRunLimiter:
    """XDR-Query-specific limiter: 10 runs/min + 500 runs/day per tenant."""

    def __init__(self) -> None:
        self._locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._windows: Dict[str, List[_Window]] = {}

    def _get_windows(self, tenant_id: str) -> List[_Window]:
        if tenant_id not in self._windows:
            self._windows[tenant_id] = [
                _Window(duration=60, max_requests=settings.XDR_QUERY_MAX_RUNS_PER_MINUTE),
                _Window(duration=86400, max_requests=settings.XDR_QUERY_MAX_RUNS_PER_DAY),
            ]
        return self._windows[tenant_id]

    async def acquire(self, tenant_id: str) -> None:
        async with self._locks[tenant_id]:
            windows = self._get_windows(tenant_id)
            for win in windows:
                wait = win.wait_time()
                if wait > 0:
                    logger.info(
                        "Query run limiter: waiting %.1fs (window %.0fs) for tenant %s",
                        wait, win.duration, tenant_id,
                    )
                    await asyncio.sleep(wait)
            for win in windows:
                win.record()


# ---------------------------------------------------------------------------
# AuthAttemptLimiter — Redis-backed com fallback in-memory
# ---------------------------------------------------------------------------

class _InMemoryAttemptStore:
    """Backend in-memory para AuthAttemptLimiter. Usado em dev sem Redis.

    Preserva toda a lógica original baseada em _Window para compatibilidade
    com testes existentes.
    """

    def __init__(
        self,
        failure_limit: int,
        failure_window: int,
        lockout_seconds: int,
    ) -> None:
        self._lock = threading.Lock()
        self._windows: Dict[str, _Window] = {}
        self._blocked_until: Dict[str, float] = {}
        self._failure_limit = failure_limit
        self._failure_window = failure_window
        self._lockout_seconds = lockout_seconds

    def _get_window(self, key: str) -> _Window:
        if key not in self._windows:
            self._windows[key] = _Window(
                duration=self._failure_window,
                max_requests=self._failure_limit,
            )
        return self._windows[key]

    def _cleanup_key(self, key: str, now: float) -> None:
        blocked_until = self._blocked_until.get(key, 0)
        if blocked_until and blocked_until > now:
            return

        window = self._windows.get(key)
        if not window:
            self._blocked_until.pop(key, None)
            return

        window._prune(now)
        if window.timestamps:
            return

        self._windows.pop(key, None)
        self._blocked_until.pop(key, None)

    def retry_after(self, key: str) -> int | None:
        with self._lock:
            now = time.monotonic()
            blocked_until = self._blocked_until.get(key, 0)
            if blocked_until > now:
                return max(1, math.ceil(blocked_until - now))

            self._cleanup_key(key, now)
            return None

    def register_failure(self, key: str) -> tuple[int, int | None]:
        with self._lock:
            now = time.monotonic()

            blocked_until = self._blocked_until.get(key, 0)
            if blocked_until > now:
                window = self._windows.get(key)
                if window:
                    window._prune(now)
                    failure_count = len(window.timestamps)
                else:
                    failure_count = 0
                return failure_count, max(1, math.ceil(blocked_until - now))

            window = self._get_window(key)
            window._prune(now)
            window.record()
            window._prune(time.monotonic())
            failure_count = len(window.timestamps)

            lockout_wait = window.wait_time()
            if lockout_wait <= 0:
                return failure_count, None

            blocked_until = now + max(self._lockout_seconds, lockout_wait)
            self._blocked_until[key] = blocked_until
            return failure_count, max(1, math.ceil(blocked_until - now))

    def failure_count(self, key: str) -> int:
        with self._lock:
            now = time.monotonic()
            window = self._windows.get(key)
            if not window:
                self._cleanup_key(key, now)
                return 0

            window._prune(now)
            failure_count = len(window.timestamps)
            if failure_count == 0:
                self._cleanup_key(key, now)
            return failure_count

    def reset(self, key: str) -> None:
        with self._lock:
            self._windows.pop(key, None)
            self._blocked_until.pop(key, None)


class _RedisAttemptStore:
    """Backend Redis para AuthAttemptLimiter.

    Usa o cliente síncrono ``redis.Redis`` (não asyncio) pois a API pública do
    AuthAttemptLimiter é síncrona — os endpoints de auth são síncronos (FastAPI
    thread pool, não corrotinas).

    Estrutura de chaves no Redis:
      - ``auth_fail_cnt:{key}``              → contador de falhas (string numérico, com TTL)
      - ``auth_lockout:{key}``               → chave de lockout por falhas consecutivas
      - ``auth:ip-distinct-users:{ip}``      → SET de username-hashes por IP (cardinality cap)
      - ``auth_lockout:ip:{ip}``             → lockout de IP por DoS de cardinalidade

    O ``redis_client`` pode ser injetado (para testes com fakeredis); se omitido,
    é criado a partir de ``url``.
    """

    def __init__(
        self,
        url: str,
        failure_limit: int,
        failure_window: int,
        lockout_seconds: int,
        redis_client: Any | None = None,
    ) -> None:
        if redis_client is not None:
            self._r = redis_client
        else:
            from redis import Redis  # import local para não forçar dep em quem só usa fallback
            self._r: Any = Redis.from_url(url, decode_responses=True)
        self._failure_limit = failure_limit
        self._failure_window = failure_window
        self._lockout_seconds = lockout_seconds

    def _cnt_key(self, key: str) -> str:
        return f"auth_fail_cnt:{key}"

    def _lockout_key(self, key: str) -> str:
        return f"auth_lockout:{key}"

    @staticmethod
    def _ip_lockout_key(ip: str) -> str:
        """Chave de lockout de IP por excesso de usernames distintos (DoS)."""
        return f"auth_lockout:ip:{ip}"

    @staticmethod
    def _distinct_users_key(ip: str) -> str:
        """SET Redis de username-hashes distintos por IP."""
        return f"auth:ip-distinct-users:{ip}"

    def retry_after(self, key: str) -> int | None:
        """Retorna segundos restantes de lockout, ou None se não bloqueado."""
        ttl = self._r.ttl(self._lockout_key(key))
        # ttl == -2: chave não existe; ttl == -1: sem expiração (não deve ocorrer)
        if ttl > 0:
            return int(ttl)
        return None

    def retry_after_ip_lockout(self, ip: str) -> int | None:
        """Retorna segundos restantes do lockout de IP por cardinality DoS.

        Separado de retry_after para que o caller possa verificar o IP
        independentemente do username.
        """
        ttl = self._r.ttl(self._ip_lockout_key(ip))
        if ttl > 0:
            return int(ttl)
        return None

    def register_failure(self, key: str) -> tuple[int, int | None]:
        """Registra falha e retorna (contagem_atual, retry_after_ou_None)."""
        cnt_key = self._cnt_key(key)
        lockout_key = self._lockout_key(key)

        # Se já está em lockout, retorna TTL restante sem incrementar
        lockout_ttl = self._r.ttl(lockout_key)
        if lockout_ttl > 0:
            cnt_raw = self._r.get(cnt_key)
            count = int(cnt_raw) if cnt_raw else 0
            return count, int(lockout_ttl)

        # Incrementa contador; na primeira vez, seta TTL da janela
        count = self._r.incr(cnt_key)
        if count == 1:
            # Primeiro incr nesta janela — aplica TTL
            self._r.expire(cnt_key, self._failure_window)

        # Verifica se atingiu o limite
        if count >= self._failure_limit:
            self._r.set(lockout_key, "1", ex=self._lockout_seconds)
            return count, self._lockout_seconds

        return count, None

    def register_failure_with_cardinality(
        self,
        key: str,
        ip: str,
        username_hash: str,
    ) -> tuple[int, int | None]:
        """Registra falha com controle de cardinalidade de usernames por IP.

        Além do comportamento de register_failure, rastreia quantos usernames
        distintos (hasheados) foram usados neste IP na janela atual. Se exceder
        _MAX_DISTINCT_USERS_PER_IP, o IP inteiro é lockado (anti-DoS por enum
        de usernames).

        Args:
            key: chave de failcount (pode ser auth:ip:{ip} ou auth:ip-user:{ip}:{hash}).
            ip: endereço IP bruto (para a chave de cardinality + lockout de IP).
            username_hash: primeiros 16 hex do SHA-256 do username (evita armazenar
                           usernames plaintext no Redis).

        Returns:
            (contagem_atual, retry_after_segundos_ou_None)
        """
        cnt_key = self._cnt_key(key)
        lockout_key = self._lockout_key(key)
        distinct_key = self._distinct_users_key(ip)
        ip_lockout_key = self._ip_lockout_key(ip)

        # Se já está em lockout por falhas consecutivas, não incrementa.
        lockout_ttl = self._r.ttl(lockout_key)
        if lockout_ttl > 0:
            cnt_raw = self._r.get(cnt_key)
            count = int(cnt_raw) if cnt_raw else 0
            return count, int(lockout_ttl)

        # Pipeline atômico: incrementa contador + adiciona username-hash ao SET de cardinalidade.
        pipe = self._r.pipeline()
        pipe.incr(cnt_key)
        pipe.sadd(distinct_key, username_hash)
        pipe.expire(distinct_key, self._failure_window)
        pipe.scard(distinct_key)
        results = pipe.execute()

        count: int = int(results[0])
        distinct_count: int = int(results[3])

        # Aplica TTL no contador na primeira inserção.
        if count == 1:
            self._r.expire(cnt_key, self._failure_window)

        # Lockout de IP por excesso de usernames distintos (anti-DoS).
        if distinct_count > _MAX_DISTINCT_USERS_PER_IP:
            self._r.set(ip_lockout_key, "1", ex=self._lockout_seconds)
            logger.warning(
                "IP lockout por cardinality DoS: ip=%s distinct_users=%d",
                ip,
                distinct_count,
            )
            return count, self._lockout_seconds

        # Lockout normal por falhas consecutivas.
        if count >= self._failure_limit:
            self._r.set(lockout_key, "1", ex=self._lockout_seconds)
            return count, self._lockout_seconds

        return count, None

    def failure_count(self, key: str) -> int:
        """Retorna contagem de falhas atual (0 se expirado ou inexistente)."""
        raw = self._r.get(self._cnt_key(key))
        return int(raw) if raw else 0

    def reset(self, key: str) -> None:
        """Limpa contador e lockout após login bem-sucedido."""
        self._r.delete(self._cnt_key(key), self._lockout_key(key))


class AuthAttemptLimiter:
    """Limiter de tentativas de autenticação com estado compartilhado via Redis.

    Em produção (``redis_url`` configurado), usa Redis para que múltiplos workers
    gunicorn compartilhem os contadores de falha — eliminando o bypass que um
    atacante obteria distribuindo tentativas entre workers.

    Em desenvolvimento (``redis_url`` ausente), recai no backend in-memory
    original, preservando comportamento de dev sem Redis.

    A API pública é idêntica ao comportamento anterior para que ``routers/auth.py``
    não precise de alterações.
    """

    def __init__(
        self,
        *,
        failure_limit: int | None = None,
        failure_window: int | None = None,
        lockout_seconds: int | None = None,
        redis_url: str | None = None,
        redis_client: Any | None = None,
    ) -> None:
        _limit = failure_limit if failure_limit is not None else settings.AUTH_FAILURE_LIMIT
        _window = failure_window if failure_window is not None else settings.AUTH_FAILURE_WINDOW_SECONDS
        _lockout = lockout_seconds if lockout_seconds is not None else settings.AUTH_LOCKOUT_SECONDS

        # redis_client injetado tem precedência (testes com fakeredis)
        if redis_client is not None:
            self._store: _RedisAttemptStore | _InMemoryAttemptStore = _RedisAttemptStore(
                url="redis://localhost",  # ignorado quando redis_client injetado
                failure_limit=_limit,
                failure_window=_window,
                lockout_seconds=_lockout,
                redis_client=redis_client,
            )
        elif redis_url:
            self._store = _RedisAttemptStore(
                url=redis_url,
                failure_limit=_limit,
                failure_window=_window,
                lockout_seconds=_lockout,
            )
        else:
            self._store = _InMemoryAttemptStore(
                failure_limit=_limit,
                failure_window=_window,
                lockout_seconds=_lockout,
            )

    # ------------------------------------------------------------------
    # API pública — compatível com uso existente em routers/auth.py
    # ------------------------------------------------------------------

    def retry_after(self, key: str) -> int | None:
        """Retorna segundos restantes de lockout para ``key``, ou None."""
        return self._store.retry_after(key)

    def retry_after_ip_lockout(self, ip: str) -> int | None:
        """Retorna segundos restantes do lockout de IP por cardinality DoS.

        Disponível apenas quando o store é Redis. In-memory store retorna None
        (cardinality cap não aplicável em dev sem Redis).
        """
        if isinstance(self._store, _RedisAttemptStore):
            return self._store.retry_after_ip_lockout(ip)
        return None

    def register_failure(self, key: str) -> tuple[int, int | None]:
        """Registra falha para ``key``.

        Retorna (contagem_de_falhas, retry_after_ou_None).
        """
        return self._store.register_failure(key)

    def register_failure_with_cardinality(
        self,
        key: str,
        ip: str,
        username_hash: str,
    ) -> tuple[int, int | None]:
        """Registra falha com controle de cardinalidade de usernames por IP.

        Delega para _RedisAttemptStore quando disponível; in-memory store
        usa register_failure simples (sem cardinality cap).
        """
        if isinstance(self._store, _RedisAttemptStore):
            return self._store.register_failure_with_cardinality(
                key, ip, username_hash
            )
        # Fallback in-memory: sem cardinality cap (dev local).
        return self._store.register_failure(key)

    def failure_count(self, key: str) -> int:
        """Contagem atual de falhas para ``key``."""
        return self._store.failure_count(key)

    def reset(self, key: str) -> None:
        """Limpa estado de falhas/lockout para ``key`` (pós-login bem-sucedido)."""
        return self._store.reset(key)


# ---------------------------------------------------------------------------
# IntegrationRateLimiter — endpoint rate limit por user e por organização
# ---------------------------------------------------------------------------

class IntegrationRateLimiter:
    """Rate limiter para operações de criação/deleção de integrações.

    Protege POST /integrations e DELETE /integrations de abuso por admin
    comprometido que poderia enfileirar N×tasks no Redis/Beat de forma
    descontrolada.

    Janelas independentes por user_id:
      - POST: max_creates_per_minute (default 30)
      - DELETE: max_deletes_per_minute (default 5)

    Usa Redis quando disponível (estado compartilhado entre workers).
    Fallback in-memory para dev sem Redis.
    """

    def __init__(
        self,
        *,
        max_creates_per_minute: int = 30,
        max_deletes_per_minute: int = 5,
        redis_url: str | None = None,
        redis_client: Any | None = None,
    ) -> None:
        self._max_creates = max_creates_per_minute
        self._max_deletes = max_deletes_per_minute
        self._lock = threading.Lock()
        # Dicionários in-memory para fallback
        self._create_windows: Dict[str, _Window] = {}
        self._delete_windows: Dict[str, _Window] = {}
        self._redis: Any | None = None

        if redis_client is not None:
            self._redis = redis_client
        elif redis_url:
            try:
                from redis import Redis as _Redis
                self._redis = _Redis.from_url(redis_url, decode_responses=True)
            except Exception:
                logger.warning("IntegrationRateLimiter: falha ao conectar ao Redis, usando in-memory")

    # ------------------------------------------------------------------
    # Backend Redis
    # ------------------------------------------------------------------

    def _redis_check_and_increment(self, key: str, max_per_minute: int) -> int | None:
        """Incrementa contador Redis de 1 minuto.

        Retorna None se dentro do limite.
        Retorna seconds_remaining (>0) se limite excedido.
        """
        full_key = f"integ_ratelimit:{key}"
        try:
            count = self._redis.incr(full_key)
            if count == 1:
                self._redis.expire(full_key, 60)
            if count > max_per_minute:
                ttl = self._redis.ttl(full_key)
                return max(1, ttl)
            return None
        except Exception:
            logger.warning("IntegrationRateLimiter Redis error — falling back to in-memory")
            return None

    # ------------------------------------------------------------------
    # Backend in-memory
    # ------------------------------------------------------------------

    def _memory_check(self, windows: Dict[str, _Window], key: str, max_per_minute: int) -> int | None:
        with self._lock:
            if key not in windows:
                windows[key] = _Window(duration=60.0, max_requests=max_per_minute)
            win = windows[key]
            win._prune(time.monotonic())
            if len(win.timestamps) >= max_per_minute:
                wait = win.wait_time()
                return max(1, math.ceil(wait))
            win.record()
            return None

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    def check_create(self, user_id: int) -> int | None:
        """Verifica e registra um POST /integrations para user_id.

        Retorna None se dentro do limite.
        Retorna retry_after_seconds se limite excedido.
        """
        key = f"create:{user_id}"
        if self._redis is not None:
            result = self._redis_check_and_increment(key, self._max_creates)
            if result is not None:
                return result
            return None
        return self._memory_check(self._create_windows, key, self._max_creates)

    def check_delete(self, user_id: int) -> int | None:
        """Verifica e registra um DELETE /integrations para user_id.

        Retorna None se dentro do limite.
        Retorna retry_after_seconds se limite excedido.
        """
        key = f"delete:{user_id}"
        if self._redis is not None:
            result = self._redis_check_and_increment(key, self._max_deletes)
            if result is not None:
                return result
            return None
        return self._memory_check(self._delete_windows, key, self._max_deletes)


# ---------------------------------------------------------------------------
# TokenRateLimiter — rate limit por PAT
# ---------------------------------------------------------------------------

class TokenRateLimiter:
    """Rate limiter por Personal Access Token.

    Espelha o pattern de ``IntegrationRateLimiter`` (Redis-first com fallback
    in-memory + janela única de 1 min). Diferenças:
      - chaveado por ``token_id`` (não por user_id) — um user pode ter N
        tokens com cargas distintas e cada um tem orçamento próprio.
      - ``check`` único (não há POST/DELETE separado — cada PAT é um único
        canal de tráfego).

    Default: 60 requests/min por token. Suficiente para integrações
    típicas (poll a cada 5-10s) sem permitir scraping massivo de um único
    token comprometido.
    """

    def __init__(
        self,
        *,
        max_requests_per_minute: int = 60,
        redis_url: str | None = None,
        redis_client: Any | None = None,
        key_prefix: str = "pat_ratelimit",
    ) -> None:
        self._max = max_requests_per_minute
        self._prefix = key_prefix
        self._lock = threading.Lock()
        self._windows: Dict[str, _Window] = {}
        self._redis: Any | None = None

        if redis_client is not None:
            self._redis = redis_client
        elif redis_url:
            try:
                from redis import Redis as _Redis
                self._redis = _Redis.from_url(redis_url, decode_responses=True)
            except Exception:
                logger.warning("TokenRateLimiter: falha ao conectar ao Redis, usando in-memory")

    def _redis_check_and_increment(self, key: str) -> int | None:
        full_key = f"{self._prefix}:{key}"
        try:
            count = self._redis.incr(full_key)
            if count == 1:
                self._redis.expire(full_key, 60)
            if count > self._max:
                ttl = self._redis.ttl(full_key)
                return max(1, ttl)
            return None
        except Exception:
            logger.warning("TokenRateLimiter Redis error — falling back to in-memory")
            return None

    def _memory_check(self, key: str) -> int | None:
        with self._lock:
            if key not in self._windows:
                self._windows[key] = _Window(duration=60.0, max_requests=self._max)
            win = self._windows[key]
            win._prune(time.monotonic())
            if len(win.timestamps) >= self._max:
                wait = win.wait_time()
                return max(1, math.ceil(wait))
            win.record()
            return None

    def check(self, token_id: int) -> int | None:
        """Verifica e registra uma requisição autenticada via PAT.

        Retorna None se dentro do limite.
        Retorna retry_after_seconds se limite excedido.
        """
        key = f"token:{token_id}"
        if self._redis is not None:
            result = self._redis_check_and_increment(key)
            if result is not None:
                return result
            return None
        return self._memory_check(key)

    def reset(self, token_id: int) -> None:
        """Limpa contador (uso operacional / testes)."""
        key = f"token:{token_id}"
        with self._lock:
            self._windows.pop(key, None)
        if self._redis is not None:
            try:
                self._redis.delete(f"{self._prefix}:{key}")
            except Exception:
                pass


# Module-level singletons
rate_limiter = RateLimiter()
query_run_limiter = QueryRunLimiter()
# Usa Redis quando REDIS_URL está configurado; caso contrário fallback in-memory.
auth_attempt_limiter = AuthAttemptLimiter(redis_url=settings.REDIS_URL)
# Rate limiter para criação/deleção de integrações.
integration_rate_limiter = IntegrationRateLimiter(redis_url=settings.REDIS_URL)
# Rate limiter por PAT.
token_rate_limiter = TokenRateLimiter(redis_url=settings.REDIS_URL)
# Rate limiter por TOKEN DE INGESTÃO. Limite alto: edge-collectors
# enviam em LOTE (NDJSON), então poucas requisições/s por integração; 600/min
# (10/s) cobre flush agressivo e ainda barra flood por token vazado. Namespace
# Redis isolado do PAT via key_prefix.
ingest_rate_limiter = TokenRateLimiter(
    max_requests_per_minute=600, redis_url=settings.REDIS_URL, key_prefix="ingest_ratelimit"
)
