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


@pytest.mark.asyncio
async def test_caps_pages_per_cycle_and_saves_resumable_cursor(monkeypatch) -> None:
    """Teto por ciclo (regressão do poison-loop de soft-timeout): com backlog MAIOR que o
    teto, ``collect()`` PARA após ``_MAX_PAGES_PER_CYCLE`` páginas em vez de drenar tudo
    num único run, e salva o cursor RESUMÍVEL — o ``pageFromKey`` da PRÓXIMA página, NÃO o
    watermark final. Sem isto, um backlog grande estoura o soft-timeout (720s) → o pipeline
    reverte o cursor p/ cursor_before e solta as claims → loop sem progresso.

    ARMADILHA verificada: como o endpoint NÃO aceita ``sort``, cair na escrita final
    (``{"from_ts": latest_ts, "pageFromKey": None}``) avançaria ``from`` p/ o maior
    createdAt visto e zeraria o token de página — descartando as páginas não lidas. O
    cursor salvo no cap-hit DEVE manter o ``from_ts`` original e o ``pageFromKey`` da
    próxima página."""
    from ..vendors import sophos as sm

    monkeypatch.setattr(sm, "_MAX_PAGES_PER_CYCLE", 3)

    # Cada página traz nextKey → o loop nunca vê fim e continuaria até exaurir o vendor
    # sem o teto. createdAt crescentes: se a escrita final (bug) rodasse, o cursor
    # avançaria p/ o maior ts e zeraria o pageFromKey (perda de dados).
    pages = [
        {"items": [{"id": "a1", "createdAt": "2026-04-23T10:00:00Z"}], "pages": {"nextKey": "k2"}},
        {"items": [{"id": "a2", "createdAt": "2026-04-23T11:00:00Z"}], "pages": {"nextKey": "k3"}},
        {"items": [{"id": "a3", "createdAt": "2026-04-23T12:00:00Z"}], "pages": {"nextKey": "k4"}},
        # páginas de sobra — o teto (3) corta antes de chegar aqui.
        {"items": [{"id": "a4", "createdAt": "2026-04-23T13:00:00Z"}], "pages": {"nextKey": "k5"}},
        {"items": [{"id": "a5", "createdAt": "2026-04-23T14:00:00Z"}], "pages": {"nextKey": "k6"}},
    ]

    with aioresponses() as m:
        for page in pages:
            m.get(_URL_RE, payload=page)

        async with aiohttp.ClientSession() as session:
            initial = "2026-04-23T09:00:00Z"
            ctx = _ctx(session, cursor={"from_ts": initial})
            collector = SophosAlertsCollector(ctx)
            collected = [ev async for ev in collector.collect()]

    # PAROU no teto: 3 páginas × 1 item = 3 eventos (não drenou o backlog inteiro).
    assert [e["id"] for e in collected] == ["a1", "a2", "a3"]
    # Cursor RESUMÍVEL: token da PRÓXIMA página (nextKey da 3ª = "k4"), NÃO o watermark
    # final. ``from_ts`` permanece no original (não avançou p/ o maior createdAt) e
    # ``pageFromKey`` NÃO é None (senão pularia páginas por falta de ``sort``).
    assert ctx.cursor == {"from_ts": initial, "pageFromKey": "k4"}
