"""Recovery de 401 in-flight — pipeline invalida cache OAuth e sinaliza retry."""

from __future__ import annotations

from unittest.mock import AsyncMock

import aiohttp
import pytest

from ..pipeline import VendorAuthError


@pytest.mark.asyncio
async def test_invalidate_called_when_vendor_returns_401(redis_client) -> None:
    """Simula o bloco except do pipeline: um 401 do vendor deve
    chamar ``invalidate_token`` + levantar ``VendorAuthError``.
    """
    from ..auth import oauth_cache

    # Popula o cache como se o refresher tivesse posto um token lá.
    await redis_client.set(
        oauth_cache.TOKEN_KEY.format(integration_id=42),
        '{"access_token":"stale","issued_at":0,"expires_in":3600}',
        ex=3600,
    )
    assert await redis_client.get(oauth_cache.TOKEN_KEY.format(integration_id=42))

    # Simula o fluxo do pipeline em caso de 401.
    fake_exc = aiohttp.ClientResponseError(
        request_info=None,  # type: ignore[arg-type]
        history=(),
        status=401,
        message="Unauthorized",
    )

    try:
        try:
            raise fake_exc
        except aiohttp.ClientResponseError as exc:
            if exc.status == 401:
                await oauth_cache.invalidate(redis_client, 42)
                raise VendorAuthError(42, "sophos") from exc
    except VendorAuthError as vauth:
        assert vauth.integration_id == 42
        assert vauth.platform == "sophos"

    # Cache foi removido.
    assert await redis_client.get(oauth_cache.TOKEN_KEY.format(integration_id=42)) is None


def test_vendor_auth_error_is_retryable_in_celery_tasks() -> None:
    """VendorAuthError deve estar na tupla de retryable das tasks."""
    from ..tasks import _RETRYABLE

    assert VendorAuthError in _RETRYABLE
