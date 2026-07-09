"""Testes para HIGH 3 — AuthAttemptLimiter username hash + cardinality cap.

Cenários cobertos:
- 51 usernames distintos do mesmo IP → IP lockado (anti-DoS de cardinalidade).
- Username hasheado: chave Redis não expõe username em plaintext.
- IP lockout via retry_after_ip_lockout().
- Fallback in-memory: register_failure_with_cardinality usa register_failure simples.
"""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock

import pytest

try:
    import fakeredis
    HAS_FAKEREDIS = True
except ImportError:
    HAS_FAKEREDIS = False


from backend.app.core.rate_limiter import (
    _MAX_DISTINCT_USERS_PER_IP,
    AuthAttemptLimiter,
    _RedisAttemptStore,
)
from backend.app.routers.auth import _hash_username


# ── Testes de _hash_username ──────────────────────────────────────────


def test_hash_username_is_hex_16_chars() -> None:
    """Hash deve ter exatamente 16 caracteres hexadecimais."""
    h = _hash_username("admin")
    assert len(h) == 16
    assert all(c in "0123456789abcdef" for c in h)


def test_hash_username_deterministic() -> None:
    """Mesmo username sempre produz mesmo hash."""
    assert _hash_username("admin") == _hash_username("admin")


def test_hash_username_different_users_different_hashes() -> None:
    """Usernames diferentes produzem hashes diferentes."""
    hashes = {_hash_username(f"user_{i}") for i in range(100)}
    # Com 16 hex chars (64-bit espaço), 100 usuários não devem colidir.
    assert len(hashes) == 100


def test_hash_username_does_not_contain_plaintext() -> None:
    """Hash não deve conter o username original."""
    username = "superadmin"
    h = _hash_username(username)
    assert username not in h


# ── Testes de _RedisAttemptStore com fakeredis ────────────────────────


@pytest.mark.skipif(not HAS_FAKEREDIS, reason="fakeredis não instalado")
def test_cardinality_cap_locks_ip_after_threshold() -> None:
    """51 usernames distintos do mesmo IP devem ativar lockout de IP."""
    fake_r = fakeredis.FakeRedis(decode_responses=True)
    store = _RedisAttemptStore(
        url="redis://localhost",
        failure_limit=100,  # limite alto para não disparar por falhas normais
        failure_window=300,
        lockout_seconds=300,
        redis_client=fake_r,
    )

    ip = "192.168.1.1"
    threshold = _MAX_DISTINCT_USERS_PER_IP  # 50

    # Envia exatamente o threshold — ainda não deve locar.
    for i in range(threshold):
        username_hash = hashlib.sha256(f"user_{i}".encode()).hexdigest()[:16]
        key = f"auth:ip-user:{ip}:{username_hash}"
        count, ra = store.register_failure_with_cardinality(key, ip, username_hash)
        # Ainda dentro do limite.
        assert ra is None, f"Não deve estar lockado em i={i}, distinct={i+1}"

    # O (threshold + 1)-ésimo username distinto deve ativar lockout de IP.
    extra_hash = hashlib.sha256(f"user_{threshold}".encode()).hexdigest()[:16]
    extra_key = f"auth:ip-user:{ip}:{extra_hash}"
    count, retry_after = store.register_failure_with_cardinality(
        extra_key, ip, extra_hash
    )

    assert retry_after is not None, "IP deveria estar lockado após exceder cardinality cap"
    assert retry_after > 0

    # Confirma que retry_after_ip_lockout detecta o lockout.
    ip_lockout = store.retry_after_ip_lockout(ip)
    assert ip_lockout is not None and ip_lockout > 0


@pytest.mark.skipif(not HAS_FAKEREDIS, reason="fakeredis não instalado")
def test_ip_lockout_persists_across_calls() -> None:
    """Após lockout de IP, retry_after_ip_lockout deve continuar retornando > 0."""
    fake_r = fakeredis.FakeRedis(decode_responses=True)
    store = _RedisAttemptStore(
        url="redis://localhost",
        failure_limit=100,
        failure_window=300,
        lockout_seconds=600,
        redis_client=fake_r,
    )
    ip = "10.0.0.1"

    # Força lockout manualmente via SET direto.
    fake_r.set(f"auth_lockout:ip:{ip}", "1", ex=600)

    result = store.retry_after_ip_lockout(ip)
    assert result is not None and result > 0


@pytest.mark.skipif(not HAS_FAKEREDIS, reason="fakeredis não instalado")
def test_username_key_uses_hash_not_plaintext() -> None:
    """Chaves Redis de username devem conter hash (16 hex), não plaintext."""
    fake_r = fakeredis.FakeRedis(decode_responses=True)
    store = _RedisAttemptStore(
        url="redis://localhost",
        failure_limit=5,
        failure_window=300,
        lockout_seconds=300,
        redis_client=fake_r,
    )

    ip = "1.2.3.4"
    username = "sensitive_admin_user"
    username_hash = hashlib.sha256(username.encode()).hexdigest()[:16]
    key = f"auth:ip-user:{ip}:{username_hash}"

    store.register_failure_with_cardinality(key, ip, username_hash)

    # Verifica que nenhuma chave Redis contém o username em plaintext.
    all_keys = fake_r.keys("*")
    for k in all_keys:
        assert username not in k, f"Username plaintext encontrado na chave Redis: {k}"


# ── Testes de AuthAttemptLimiter (alto nível) ─────────────────────────


@pytest.mark.skipif(not HAS_FAKEREDIS, reason="fakeredis não instalado")
def test_auth_limiter_cardinality_via_public_api() -> None:
    """AuthAttemptLimiter.register_failure_with_cardinality funciona via API pública."""
    fake_r = fakeredis.FakeRedis(decode_responses=True)
    limiter = AuthAttemptLimiter(
        failure_limit=100,
        failure_window=300,
        lockout_seconds=300,
        redis_client=fake_r,
    )

    ip = "172.16.0.1"
    threshold = _MAX_DISTINCT_USERS_PER_IP

    for i in range(threshold + 1):
        h = hashlib.sha256(f"attacker_user_{i}".encode()).hexdigest()[:16]
        key = f"auth:ip-user:{ip}:{h}"
        limiter.register_failure_with_cardinality(key, ip, h)

    # Após threshold+1 usuários distintos, IP deve estar lockado.
    ip_lockout = limiter.retry_after_ip_lockout(ip)
    assert ip_lockout is not None and ip_lockout > 0


def test_auth_limiter_fallback_memory_no_cardinality() -> None:
    """In-memory store: register_failure_with_cardinality funciona sem Redis."""
    # Limiter sem redis_url → usa _InMemoryAttemptStore.
    limiter = AuthAttemptLimiter(
        failure_limit=5,
        failure_window=300,
        lockout_seconds=300,
    )

    # Deve funcionar normalmente (sem cardinality cap).
    count, ra = limiter.register_failure_with_cardinality(
        key="auth:ip:127.0.0.1",
        ip="127.0.0.1",
        username_hash="abcdef1234567890",
    )
    assert count == 1
    assert ra is None  # sem lockout ainda

    # retry_after_ip_lockout retorna None para in-memory.
    assert limiter.retry_after_ip_lockout("127.0.0.1") is None
