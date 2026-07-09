"""Testes do TokenRateLimiter — janela in-memory + chave por token_id."""

from __future__ import annotations

import pytest

from backend.app.core.rate_limiter import TokenRateLimiter


def test_first_request_passes():
    limiter = TokenRateLimiter(max_requests_per_minute=5)
    assert limiter.check(1) is None


def test_blocks_after_max():
    limiter = TokenRateLimiter(max_requests_per_minute=3)
    assert limiter.check(7) is None
    assert limiter.check(7) is None
    assert limiter.check(7) is None
    blocked = limiter.check(7)
    assert blocked is not None
    assert blocked >= 1  # retry_after em segundos


def test_independent_buckets_per_token_id():
    limiter = TokenRateLimiter(max_requests_per_minute=2)
    assert limiter.check(1) is None
    assert limiter.check(1) is None
    # Token 1 deve estar bloqueado
    assert limiter.check(1) is not None
    # Mas token 2 começou agora — passa.
    assert limiter.check(2) is None


def test_reset_clears_window():
    limiter = TokenRateLimiter(max_requests_per_minute=1)
    assert limiter.check(99) is None
    assert limiter.check(99) is not None
    limiter.reset(99)
    assert limiter.check(99) is None


def test_with_fakeredis_client():
    """Smoke-test do branch Redis usando fakeredis."""
    fakeredis = pytest.importorskip("fakeredis")
    redis_client = fakeredis.FakeRedis(decode_responses=True)
    limiter = TokenRateLimiter(max_requests_per_minute=2, redis_client=redis_client)
    assert limiter.check(123) is None
    assert limiter.check(123) is None
    blocked = limiter.check(123)
    assert blocked is not None
    assert blocked >= 1
