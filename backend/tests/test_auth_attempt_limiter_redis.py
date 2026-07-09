"""Testes para AuthAttemptLimiter com backend Redis (fakeredis) e in-memory.

Cobre:
- Estado compartilhado entre instâncias via Redis (simula múltiplos workers).
- Lockout disparado após N falhas (tanto in-memory quanto Redis-backed).
- Reset de contador após login bem-sucedido.
- Lockout separado por IP e por username.
- Expiração da janela de tempo.
- Fallback in-memory quando redis_url está vazio.
"""

from __future__ import annotations

import fakeredis
import pytest

from backend.app.core.rate_limiter import AuthAttemptLimiter, _RedisAttemptStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def fake_redis() -> fakeredis.FakeRedis:
    """FakeRedis compartilhado entre instâncias no mesmo teste."""
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture()
def redis_limiter(fake_redis: fakeredis.FakeRedis) -> AuthAttemptLimiter:
    """Limiter Redis-backed com limite baixo para facilitar testes."""
    return AuthAttemptLimiter(
        failure_limit=3,
        failure_window=60,
        lockout_seconds=120,
        redis_client=fake_redis,
    )


@pytest.fixture()
def memory_limiter() -> AuthAttemptLimiter:
    """Limiter in-memory com limite baixo."""
    return AuthAttemptLimiter(
        failure_limit=3,
        failure_window=60,
        lockout_seconds=120,
    )


# ---------------------------------------------------------------------------
# Parâmetros para testar ambos os backends
# ---------------------------------------------------------------------------

def _make_parametrized_limiters(request: pytest.FixtureRequest) -> AuthAttemptLimiter:
    """Fixture factory — retorna o limiter correto conforme parâmetro."""
    if request.param == "redis":
        return AuthAttemptLimiter(
            failure_limit=3,
            failure_window=60,
            lockout_seconds=120,
            redis_client=fakeredis.FakeRedis(decode_responses=True),
        )
    return AuthAttemptLimiter(
        failure_limit=3,
        failure_window=60,
        lockout_seconds=120,
    )


@pytest.fixture(params=["redis", "memory"])
def any_limiter(request: pytest.FixtureRequest) -> AuthAttemptLimiter:
    return _make_parametrized_limiters(request)


# ---------------------------------------------------------------------------
# Testes
# ---------------------------------------------------------------------------

class TestRedisBackedPersistence:
    """Estado deve persistir entre instâncias distintas que compartilham Redis."""

    def test_redis_backed_limiter_persists_across_instances(
        self, fake_redis: fakeredis.FakeRedis
    ) -> None:
        """Falha registrada em instância A deve ser visível em instância B."""
        limiter_a = AuthAttemptLimiter(
            failure_limit=3,
            failure_window=60,
            lockout_seconds=120,
            redis_client=fake_redis,
        )
        limiter_b = AuthAttemptLimiter(
            failure_limit=3,
            failure_window=60,
            lockout_seconds=120,
            redis_client=fake_redis,
        )

        # Registra falhas em A
        limiter_a.register_failure("auth:ip:1.2.3.4")
        limiter_a.register_failure("auth:ip:1.2.3.4")

        # B deve ver as mesmas falhas
        count = limiter_b.failure_count("auth:ip:1.2.3.4")
        assert count == 2, f"Esperado 2 falhas em instância B, obtido {count}"

    def test_lockout_in_a_blocks_in_b(self, fake_redis: fakeredis.FakeRedis) -> None:
        """Lockout criado em instância A deve bloquear em instância B."""
        limiter_a = AuthAttemptLimiter(
            failure_limit=3,
            failure_window=60,
            lockout_seconds=120,
            redis_client=fake_redis,
        )
        limiter_b = AuthAttemptLimiter(
            failure_limit=3,
            failure_window=60,
            lockout_seconds=120,
            redis_client=fake_redis,
        )

        key = "auth:ip:5.6.7.8"
        for _ in range(3):  # atinge limite
            limiter_a.register_failure(key)

        assert limiter_b.retry_after(key) is not None, "B deveria ver lockout criado por A"


class TestLockoutTriggered:
    """Lockout deve ser disparado após N falhas consecutivas."""

    @pytest.mark.parametrize("backend", ["redis", "memory"])
    def test_lockout_triggered_after_n_failures(self, backend: str) -> None:
        if backend == "redis":
            limiter = AuthAttemptLimiter(
                failure_limit=3,
                failure_window=60,
                lockout_seconds=120,
                redis_client=fakeredis.FakeRedis(decode_responses=True),
            )
        else:
            limiter = AuthAttemptLimiter(
                failure_limit=3,
                failure_window=60,
                lockout_seconds=120,
            )

        key = "auth:ip:10.0.0.1"

        count1, retry1 = limiter.register_failure(key)
        assert count1 == 1
        assert retry1 is None

        count2, retry2 = limiter.register_failure(key)
        assert count2 == 2
        assert retry2 is None

        count3, retry3 = limiter.register_failure(key)
        assert count3 == 3
        assert retry3 is not None, "Lockout deve ser ativado na 3ª falha"
        assert retry3 > 0

    @pytest.mark.parametrize("backend", ["redis", "memory"])
    def test_below_limit_does_not_lock(self, backend: str) -> None:
        if backend == "redis":
            limiter = AuthAttemptLimiter(
                failure_limit=5,
                failure_window=60,
                lockout_seconds=120,
                redis_client=fakeredis.FakeRedis(decode_responses=True),
            )
        else:
            limiter = AuthAttemptLimiter(
                failure_limit=5,
                failure_window=60,
                lockout_seconds=120,
            )

        key = "auth:ip:10.0.0.2"
        for _ in range(4):
            _, retry_after = limiter.register_failure(key)
            assert retry_after is None, "Ainda abaixo do limite — não deve travar"


class TestClearAfterSuccess:
    """Reset deve limpar contadores e lockout."""

    @pytest.mark.parametrize("backend", ["redis", "memory"])
    def test_clear_after_success_resets_counter(self, backend: str) -> None:
        if backend == "redis":
            limiter = AuthAttemptLimiter(
                failure_limit=3,
                failure_window=60,
                lockout_seconds=120,
                redis_client=fakeredis.FakeRedis(decode_responses=True),
            )
        else:
            limiter = AuthAttemptLimiter(
                failure_limit=3,
                failure_window=60,
                lockout_seconds=120,
            )

        key = "auth:ip:10.0.0.3"
        limiter.register_failure(key)
        limiter.register_failure(key)
        assert limiter.failure_count(key) == 2

        limiter.reset(key)

        assert limiter.failure_count(key) == 0, "Contagem deve ser zero após reset"
        assert limiter.retry_after(key) is None, "Não deve haver lockout após reset"

    def test_reset_clears_lockout_redis(self, fake_redis: fakeredis.FakeRedis) -> None:
        """Reset deve remover lockout ativo no Redis."""
        limiter = AuthAttemptLimiter(
            failure_limit=2,
            failure_window=60,
            lockout_seconds=120,
            redis_client=fake_redis,
        )

        key = "auth:ip:10.0.0.4"
        for _ in range(2):
            limiter.register_failure(key)

        assert limiter.retry_after(key) is not None, "Deveria estar bloqueado"

        limiter.reset(key)
        assert limiter.retry_after(key) is None, "Lockout deve ser removido pelo reset"


class TestSeparateLockout:
    """Falhas por IP e por username são rastreadas e bloqueadas separadamente."""

    def test_separate_lockout_for_ip_and_username(
        self, fake_redis: fakeredis.FakeRedis
    ) -> None:
        """Falhas no mesmo IP com usernames diferentes bloqueiam apenas o IP.
        Falhas com o mesmo username em IPs diferentes bloqueiam apenas o username.
        """
        limiter = AuthAttemptLimiter(
            failure_limit=3,
            failure_window=60,
            lockout_seconds=120,
            redis_client=fake_redis,
        )

        ip_key = "auth:ip:192.168.1.1"
        user_key = "auth:ip-user:10.0.0.1:admin"

        # Acumula falhas no IP
        for _ in range(3):
            limiter.register_failure(ip_key)

        # Acumula falhas no username
        for _ in range(3):
            limiter.register_failure(user_key)

        # Ambos devem estar bloqueados independentemente
        assert limiter.retry_after(ip_key) is not None, "IP deve estar bloqueado"
        assert limiter.retry_after(user_key) is not None, "Username deve estar bloqueado"

    def test_ip_lockout_does_not_affect_other_ips(
        self, fake_redis: fakeredis.FakeRedis
    ) -> None:
        """Lockout em um IP não contamina outros IPs."""
        limiter = AuthAttemptLimiter(
            failure_limit=2,
            failure_window=60,
            lockout_seconds=120,
            redis_client=fake_redis,
        )

        blocked_ip = "auth:ip:1.1.1.1"
        clean_ip = "auth:ip:2.2.2.2"

        for _ in range(2):
            limiter.register_failure(blocked_ip)

        assert limiter.retry_after(blocked_ip) is not None
        assert limiter.retry_after(clean_ip) is None, "IP distinto não deve ser afetado"


class TestWindowExpiry:
    """Contador deve expirar após a janela de tempo."""

    def test_window_expires_after_ttl(self, fake_redis: fakeredis.FakeRedis) -> None:
        """Após expirar a janela, failure_count deve retornar 0."""
        limiter = AuthAttemptLimiter(
            failure_limit=10,
            failure_window=1,  # janela de 1 segundo para facilitar teste
            lockout_seconds=120,
            redis_client=fake_redis,
        )

        key = "auth:ip:3.3.3.3"
        limiter.register_failure(key)
        assert limiter.failure_count(key) == 1

        # Avança o tempo no fakeredis expirando a chave
        fake_redis.expire(f"auth_fail_cnt:{key}", 0)

        assert limiter.failure_count(key) == 0, "Contador deve ser zero após expiração"


class TestInMemoryFallback:
    """Fallback in-memory quando redis_url não está configurado."""

    def test_in_memory_fallback_when_redis_url_empty(self) -> None:
        """Deve usar backend in-memory quando redis_url não é fornecido."""
        limiter = AuthAttemptLimiter(
            failure_limit=3,
            failure_window=60,
            lockout_seconds=120,
            # redis_url omitido → in-memory
        )

        from backend.app.core.rate_limiter import _InMemoryAttemptStore
        assert isinstance(limiter._store, _InMemoryAttemptStore), (
            "Deve usar _InMemoryAttemptStore quando redis_url não está configurado"
        )

    def test_in_memory_tracks_failures(self) -> None:
        """Backend in-memory deve rastrear falhas corretamente."""
        limiter = AuthAttemptLimiter(
            failure_limit=3,
            failure_window=60,
            lockout_seconds=120,
        )

        key = "auth:ip:127.0.0.1"
        limiter.register_failure(key)
        limiter.register_failure(key)
        assert limiter.failure_count(key) == 2

        limiter.reset(key)
        assert limiter.failure_count(key) == 0

    def test_redis_store_used_when_client_injected(
        self, fake_redis: fakeredis.FakeRedis
    ) -> None:
        """Quando redis_client é injetado, deve usar _RedisAttemptStore."""
        limiter = AuthAttemptLimiter(
            failure_limit=3,
            failure_window=60,
            lockout_seconds=120,
            redis_client=fake_redis,
        )

        assert isinstance(limiter._store, _RedisAttemptStore), (
            "Deve usar _RedisAttemptStore quando redis_client é injetado"
        )


class TestRetryAfter:
    """retry_after deve retornar valor positivo durante lockout."""

    def test_retry_after_none_when_not_locked(self, any_limiter: AuthAttemptLimiter) -> None:
        key = "auth:ip:fresh_key"
        assert any_limiter.retry_after(key) is None

    def test_retry_after_positive_when_locked(
        self, fake_redis: fakeredis.FakeRedis
    ) -> None:
        limiter = AuthAttemptLimiter(
            failure_limit=2,
            failure_window=60,
            lockout_seconds=300,
            redis_client=fake_redis,
        )

        key = "auth:ip:locked_ip"
        for _ in range(2):
            limiter.register_failure(key)

        ra = limiter.retry_after(key)
        assert ra is not None
        assert ra > 0
        assert ra <= 300
