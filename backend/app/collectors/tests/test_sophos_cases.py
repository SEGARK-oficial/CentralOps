"""Sophos Cases collector — paginação, cursor e dedupe por updatedAt."""

from __future__ import annotations

import re
from typing import Any, Dict
from unittest.mock import MagicMock

import aiohttp
import pytest
from ._aiohttp_mock import aioresponses

from ..base import CollectorContext
from ..vendors.sophos_cases import SophosCasesCollector, SophosCasesRateLimitedError

_URL_RE = re.compile(
    r"^https://api-eu03\.central\.sophos\.com/cases/v1/cases(\?.*)?$"
)


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
        headers={"Authorization": "Bearer x", "X-Region": "eu03", "X-Tenant-ID": "t-1"},
        session=session,
        cursor=cursor,
        domain_limiter=_NoopDomainLimiter(),
        rate_limiter=_NoopRateLimiter(),
        redis=MagicMock(),
    )


@pytest.mark.asyncio
async def test_iterates_cases_pages_offset_based() -> None:
    """Paginação offset-based (page=1,2,…) termina quando len(items) < pageSize.

    Fonte: Postman collection oficial da Sophos —
    ``docs/Sophos Central APIs.postman_collection.json`` → Cases API/Get cases.
    """
    # Simulamos _PAGE_SIZE=200 (default). A lib retorna 200 itens na p1 e
    # 1 item na p2 (página incompleta → termina).
    from ..vendors.sophos_cases import _PAGE_SIZE

    full_page = {
        "items": [
            {"id": f"c{i}", "updatedAt": "2026-04-23T10:00:00Z", "severity": "low"}
            for i in range(_PAGE_SIZE)
        ],
        "pages": {"current": 1, "size": _PAGE_SIZE, "total": _PAGE_SIZE + 1},
    }
    partial_page = {
        "items": [
            {"id": "c-final", "updatedAt": "2026-04-23T11:00:00Z", "severity": "critical"},
        ],
        "pages": {"current": 2, "size": _PAGE_SIZE, "total": _PAGE_SIZE + 1},
    }

    with aioresponses() as m:
        m.get(_URL_RE, payload=full_page)
        m.get(_URL_RE, payload=partial_page)

        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, cursor={"created_after": "2026-04-23T09:00:00Z"})
            collector = SophosCasesCollector(ctx)
            collected = [ev async for ev in collector.collect()]

    assert len(collected) == _PAGE_SIZE + 1
    assert collected[-1]["id"] == "c-final"
    # Cursor final: próxima janela começa do maior updatedAt visto,
    # paginação volta a 1.
    assert ctx.cursor == {
        "created_after": "2026-04-23T11:00:00Z",
        "page": 1,
    }


@pytest.mark.asyncio
async def test_429_signals_rate_limit_to_upstream() -> None:
    with aioresponses() as m:
        m.get(_URL_RE, status=429, headers={"Retry-After": "7"})

        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session)
            with pytest.raises(SophosCasesRateLimitedError) as exc:
                async for _ in SophosCasesCollector(ctx).collect():
                    pass

    assert exc.value.retry_after == 7


def test_message_id_uses_id_and_updatedAt() -> None:
    """Dedupe deve incluir updatedAt — permite rastrear updates."""
    ctx = _ctx(MagicMock())
    coll = SophosCasesCollector(ctx)
    msg_id = coll.extract_message_id(
        {"id": "case-123", "updatedAt": "2026-04-23T15:00:00Z"}
    )
    assert msg_id == "case-123::2026-04-23T15:00:00Z"
    # Sem updatedAt, cai no id simples
    assert coll.extract_message_id({"id": "case-123"}) == "case-123"


def _captured_params(m: aioresponses) -> Dict[str, Any]:
    """Retorna os query params do primeiro GET capturado pelo aioresponses."""
    all_calls = [call for calls in m.requests.values() for call in calls]
    assert all_calls, "no HTTP request was captured"
    return dict(all_calls[0].kwargs.get("params") or {})


@pytest.mark.asyncio
async def test_backfill_cursor_uses_backfill_window() -> None:
    """Backfill grava ``backfill_from_ts``/``backfill_to_ts`` no cursor —
    o collector deve mapeá-los para ``createdAfter``/``createdBefore`` em
    vez de cair no ``_default_lookback_iso()`` de 1h.

    Regressão: antes do fix, um backfill de 30d coletava apenas a última 1h
    porque o collector lia ``cursor['created_after']`` (não populado no
    backfill cursor) e ignorava ``backfill_from_ts``.
    """
    payload = {"items": [], "pages": {"current": 1, "size": 50, "total": 0}}

    with aioresponses() as m:
        m.get(_URL_RE, payload=payload)

        async with aiohttp.ClientSession() as session:
            ctx = _ctx(
                session,
                cursor={
                    "backfill_from_ts": "2026-03-01T00:00:00Z",
                    "backfill_to_ts": "2026-03-31T00:00:00Z",
                },
            )
            collected = [ev async for ev in SophosCasesCollector(ctx).collect()]

        assert collected == []
        params = _captured_params(m)
        assert params["createdAfter"] == "2026-03-01T00:00:00Z"
        assert params["createdBefore"] == "2026-03-31T00:00:00Z"


@pytest.mark.asyncio
async def test_scheduled_cursor_takes_precedence_over_backfill_window() -> None:
    """Se ambos ``created_after`` e ``backfill_from_ts`` estão no cursor
    (ex: backfill que retomou após primeira chunk), o ``created_after`` —
    que avança conforme ``latest_updated`` — vence."""
    payload = {"items": [], "pages": {"current": 1, "size": 50, "total": 0}}

    with aioresponses() as m:
        m.get(_URL_RE, payload=payload)

        async with aiohttp.ClientSession() as session:
            ctx = _ctx(
                session,
                cursor={
                    "created_after": "2026-04-01T12:00:00Z",
                    "backfill_from_ts": "2026-03-01T00:00:00Z",
                    "backfill_to_ts": "2026-03-31T00:00:00Z",
                },
            )
            _ = [ev async for ev in SophosCasesCollector(ctx).collect()]

        params = _captured_params(m)
        assert params["createdAfter"] == "2026-04-01T12:00:00Z"
        # createdBefore continua respeitando a janela superior do backfill.
        assert params["createdBefore"] == "2026-03-31T00:00:00Z"


@pytest.mark.asyncio
async def test_no_created_before_when_no_backfill_window() -> None:
    """Coleta agendada (sem backfill_to_ts) NÃO deve enviar ``createdBefore``
    — paginar até o presente é o comportamento correto."""
    payload = {"items": [], "pages": {"current": 1, "size": 50, "total": 0}}

    with aioresponses() as m:
        m.get(_URL_RE, payload=payload)

        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, cursor={"created_after": "2026-04-23T09:00:00Z"})
            _ = [ev async for ev in SophosCasesCollector(ctx).collect()]

        params = _captured_params(m)
        assert "createdBefore" not in params


@pytest.mark.asyncio
async def test_logs_info_when_zero_events_collected(caplog) -> None:
    """Distingue 'tenant sem MDR/XDR' de 'API quebrada' nos logs.

    Quando o vendor retorna 200 com ``items=[]``, o collector emite log
    INFO explícito — sem isso, é indistinguível de coleta saudável."""
    import logging

    payload = {"items": [], "pages": {"current": 1, "size": 50, "total": 0}}

    with aioresponses() as m:
        m.get(_URL_RE, payload=payload)

        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, cursor={"created_after": "2026-04-23T09:00:00Z"})
            with caplog.at_level(logging.INFO, logger="backend.app.collectors.vendors.sophos_cases"):
                _ = [ev async for ev in SophosCasesCollector(ctx).collect()]

    messages = [r.getMessage() for r in caplog.records]
    assert any("0 events collected" in msg for msg in messages), messages
