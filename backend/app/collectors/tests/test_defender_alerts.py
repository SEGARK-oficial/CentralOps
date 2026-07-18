"""Defender Alerts v2 collector — paginação Graph e dedupe por lastUpdateDateTime."""

from __future__ import annotations

import re
from typing import Any, Dict
from unittest.mock import MagicMock

import aiohttp
import pytest
from ._aiohttp_mock import aioresponses

from ..base import CollectorContext
from ..vendors.defender_alerts import (
    DefenderAlertsRateLimitedError,
    DefenderAlertsV2Collector,
)

_URL_RE = re.compile(
    r"^https://graph\.microsoft\.com/v1\.0/security/alerts_v2(\?.*)?$"
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
        integration_id=99,
        organization_id=7,
        platform="microsoft_defender",
        headers={"Authorization": "Bearer graphjwt"},
        session=session,
        cursor=cursor,
        domain_limiter=_NoopDomainLimiter(),
        rate_limiter=_NoopRateLimiter(),
        redis=MagicMock(),
    )


@pytest.mark.asyncio
async def test_paginates_via_odata_next_link() -> None:
    next_link = "https://graph.microsoft.com/v1.0/security/alerts_v2?$skiptoken=abc"
    with aioresponses() as m:
        m.get(
            _URL_RE,
            payload={
                "value": [
                    {
                        "id": "A1",
                        "lastUpdateDateTime": "2026-04-23T10:00:00Z",
                        "severity": "high",
                    }
                ],
                "@odata.nextLink": next_link,
            },
        )
        m.get(
            next_link,
            payload={
                "value": [
                    {
                        "id": "A2",
                        "lastUpdateDateTime": "2026-04-23T11:00:00Z",
                        "severity": "critical",
                    }
                ]
            },
        )

        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, cursor={"lastUpdateDateTime": "2026-04-23T09:00:00Z"})
            collected = [
                ev async for ev in DefenderAlertsV2Collector(ctx).collect()
            ]

    assert [e["id"] for e in collected] == ["A1", "A2"]
    assert ctx.cursor == {
        "lastUpdateDateTime": "2026-04-23T11:00:00Z",
        "@odata.nextLink": None,
    }


@pytest.mark.asyncio
async def test_caps_pages_per_cycle_and_saves_resumable_cursor(monkeypatch) -> None:
    """Teto por ciclo (regressão do poison-loop de soft-timeout): com backlog MAIOR que o
    teto, ``collect()`` PARA após ``_MAX_PAGES_PER_CYCLE`` páginas em vez de drenar todo o
    ``@odata.nextLink`` num único run, e salva o cursor RESUMÍVEL (o nextLink da PRÓXIMA
    página + ``lastUpdateDateTime`` = INÍCIO do run) p/ o próximo ciclo RETOMAR sem pular.

    A armadilha crítica: no cap-hit NÃO pode cair na escrita final que avança o watermark
    (``lastUpdateDateTime=latest_seen``) e zera o token (``@odata.nextLink=None``) — isso
    descartaria as páginas não lidas. Salvar ``last_update`` (início) garante que, se o
    skiptoken expirar, o fallback ``$filter ge last_update`` re-varre sem perder (dedupe
    absorve). Sem o teto, um backlog grande estoura o soft-timeout (720s) → rollback total
    → loop sem progresso."""
    from ..vendors import defender_alerts as da

    monkeypatch.setattr(da, "_MAX_PAGES_PER_CYCLE", 3)

    # Cada página devolve 1 alerta E um @odata.nextLink (página "cheia") → o loop nunca
    # vê fim de paginação e continuaria drenando indefinidamente sem o teto. Registro de
    # sobra (8 páginas); o teto corta em 3. Os nextLinks casam com _URL_RE (mesmo host).
    def _page(i: int) -> Dict[str, Any]:
        return {
            "value": [
                {
                    "id": f"A{i}",
                    "lastUpdateDateTime": f"2026-04-23T1{i}:00:00Z",
                    "severity": "high",
                }
            ],
            "@odata.nextLink": (
                f"https://graph.microsoft.com/v1.0/security/alerts_v2?$skiptoken=p{i}"
            ),
        }

    with aioresponses() as m:
        for i in range(1, 9):
            m.get(_URL_RE, payload=_page(i))
        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, cursor={"lastUpdateDateTime": "2026-04-23T09:00:00Z"})
            collected = [
                ev async for ev in DefenderAlertsV2Collector(ctx).collect()
            ]

    # PAROU no teto: 3 páginas × 1 alerta = 3 eventos (não drenou as 8 páginas).
    assert [e["id"] for e in collected] == ["A1", "A2", "A3"]
    # Cursor RESUMÍVEL: nextLink da 3ª página (próxima não lida) + lastUpdateDateTime do
    # INÍCIO do run (2026-04-23T09:00:00Z), NÃO o latest_seen (2026-04-23T13:00:00Z) nem
    # nextLink=None — provando que NÃO caiu na escrita final que descartaria o backlog.
    assert ctx.cursor == {
        "lastUpdateDateTime": "2026-04-23T09:00:00Z",
        "@odata.nextLink": "https://graph.microsoft.com/v1.0/security/alerts_v2?$skiptoken=p3",
    }


@pytest.mark.asyncio
async def test_429_signals_rate_limit() -> None:
    with aioresponses() as m:
        m.get(_URL_RE, status=429, headers={"Retry-After": "10"})

        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session)
            with pytest.raises(DefenderAlertsRateLimitedError) as exc:
                async for _ in DefenderAlertsV2Collector(ctx).collect():
                    pass

    assert exc.value.retry_after == 10


def test_message_id_tracks_updates_via_lastUpdateDateTime() -> None:
    ctx = _ctx(MagicMock())
    coll = DefenderAlertsV2Collector(ctx)
    msg = coll.extract_message_id(
        {
            "id": "da637551227677560813_-961444813",
            "lastUpdateDateTime": "2026-04-23T15:00:00Z",
        }
    )
    assert msg == "da637551227677560813_-961444813::2026-04-23T15:00:00Z"


def test_message_id_falls_back_to_provider_alert_id() -> None:
    ctx = _ctx(MagicMock())
    coll = DefenderAlertsV2Collector(ctx)
    # Sem id — precisa cair para providerAlertId
    assert coll.extract_message_id({"providerAlertId": "P-1"}) == "P-1"
