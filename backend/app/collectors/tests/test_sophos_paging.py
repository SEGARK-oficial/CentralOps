"""RF03 — paginação automática até exaurir ``pages.nextKey``."""

from __future__ import annotations

import re
from typing import Any, Dict
from unittest.mock import MagicMock

import aiohttp
import pytest
from ._aiohttp_mock import aioresponses

from ..base import CollectorContext
from ..vendors.sophos import SophosAlertsCollector, SophosRateLimitedError

# Regex evita armadilha de double-encoding de query params no ``aioresponses``.
_URL_RE = re.compile(r"^https://api-eu03\.central\.sophos\.com/common/v1/alerts(\?.*)?$")


class _NoopDomainLimiter:
    def slot(self, domain):
        class _Ctx:
            async def __aenter__(self_inner):
                return None

            async def __aexit__(self_inner, *a):
                return False

        return _Ctx()


class _NoopRateLimiter:
    async def acquire(self, tenant_id, vendor):
        return None

    async def backoff(self, vendor, retry_after):
        return None


def _ctx(session: aiohttp.ClientSession, cursor: Dict[str, Any] | None = None) -> CollectorContext:
    return CollectorContext(
        integration_id=42,
        organization_id=7,
        platform="sophos",
        headers={"Authorization": "Bearer x", "X-Region": "eu03"},
        session=session,
        cursor=cursor,
        domain_limiter=_NoopDomainLimiter(),
        rate_limiter=_NoopRateLimiter(),
        redis=MagicMock(),
    )


@pytest.mark.asyncio
async def test_collector_iterates_through_all_pages() -> None:
    pages = [
        {
            "items": [{"id": "a1", "createdAt": "2026-04-23T10:00:00Z"}],
            "pages": {"nextKey": "k2"},
        },
        {
            "items": [{"id": "a2", "createdAt": "2026-04-23T11:00:00Z"}],
            "pages": {"nextKey": "k3"},
        },
        {
            "items": [{"id": "a3", "createdAt": "2026-04-23T12:00:00Z"}],
            "pages": {},  # sem nextKey → fim
        },
    ]

    with aioresponses() as m:
        for page in pages:
            m.get(_URL_RE, payload=page)

        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, cursor={"from_ts": "2026-04-23T09:00:00Z"})
            collector = SophosAlertsCollector(ctx)
            collected = [ev async for ev in collector.collect()]

    assert [e["id"] for e in collected] == ["a1", "a2", "a3"]
    # Cursor final avança para o maior createdAt visto.
    assert ctx.cursor == {"from_ts": "2026-04-23T12:00:00Z", "pageFromKey": None}


@pytest.mark.asyncio
async def test_collector_handles_429_with_retry_after() -> None:
    with aioresponses() as m:
        m.get(_URL_RE, status=429, headers={"Retry-After": "3"})

        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session)
            collector = SophosAlertsCollector(ctx)
            with pytest.raises(SophosRateLimitedError) as exc:
                async for _ in collector.collect():
                    pass

    assert exc.value.retry_after == 3


@pytest.mark.asyncio
async def test_collector_preserves_cursor_on_empty_page() -> None:
    with aioresponses() as m:
        m.get(_URL_RE, payload={"items": [], "pages": {}})

        async with aiohttp.ClientSession() as session:
            initial = "2026-04-23T09:00:00Z"
            ctx = _ctx(session, cursor={"from_ts": initial})
            collector = SophosAlertsCollector(ctx)
            collected = [ev async for ev in collector.collect()]

    assert collected == []
    assert ctx.cursor == {"from_ts": initial, "pageFromKey": None}
