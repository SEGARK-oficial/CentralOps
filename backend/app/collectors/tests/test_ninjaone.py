"""NinjaOneActivitiesCollector — paginação keyset por ``after``/id crescente.

Cobre: coleta de página única + cursor final, página vazia mantendo o cursor, e
o TETO por ciclo (regressão do poison-loop de soft-timeout) — o cap salva o cursor
keyset RESUMÍVEL (``after`` exclusivo → o próximo ciclo retoma sem pular/duplicar).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List
from unittest.mock import MagicMock

import aiohttp
import pytest

from ._aiohttp_mock import aioresponses
from ..base import CollectorContext
from ..vendors.ninjaone import NinjaOneActivitiesCollector

_ACT_RE = re.compile(r"^https://app\.ninjarmm\.com/v2/activities")


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
        platform="ninjaone",
        headers={"Authorization": "Bearer tok"},
        session=session,
        cursor=cursor,
        domain_limiter=_NoopDomainLimiter(),
        rate_limiter=_NoopRateLimiter(),
        redis=MagicMock(),
    )


def _activity(act_id: int) -> Dict[str, Any]:
    return {"id": act_id, "activityType": "ACTION", "activityTime": 1719000000 + act_id}


@pytest.mark.asyncio
async def test_collects_single_page_and_updates_cursor() -> None:
    with aioresponses() as m:
        # Página curta (< _PAGE_SIZE=200) → fim; cursor avança para o maior id visto.
        m.get(_ACT_RE, payload=[_activity(10), _activity(20)])
        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, cursor={"activity_time_after": 1719000000})
            collector = NinjaOneActivitiesCollector(ctx)
            collected = [ev async for ev in collector.collect()]

    assert [e["id"] for e in collected] == [10, 20]
    assert collector.extract_message_id(collected[1]) == "20"
    # cursor keyset final: after_id no maior id, piso temporal inalterado.
    assert ctx.cursor == {"after_id": 20, "activity_time_after": 1719000000}
    assert collector.domain == "app.ninjarmm.com"


@pytest.mark.asyncio
async def test_empty_result_keeps_cursor() -> None:
    with aioresponses() as m:
        m.get(_ACT_RE, payload=[])
        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, cursor={"after_id": 5, "activity_time_after": 1719000000})
            collected = [ev async for ev in NinjaOneActivitiesCollector(ctx).collect()]

    assert collected == []
    # sem itens: nada a avançar (cursor permanece no after_id inicial).
    assert ctx.cursor == {"after_id": 5, "activity_time_after": 1719000000}


@pytest.mark.asyncio
async def test_caps_pages_per_cycle_and_saves_resumable_cursor(monkeypatch) -> None:
    """Teto por ciclo (regressão do poison-loop de soft-timeout): com backlog MAIOR que o
    teto, ``collect()`` PARA após ``_MAX_PAGES_PER_CYCLE`` páginas em vez de drenar tudo
    num único run, e salva o cursor keyset RESUMÍVEL (``after`` exclusivo = o maior id
    emitido) p/ o próximo ciclo RETOMAR de ``?after=after_id`` (sem pular/duplicar). Sem
    isto, um backlog grande estoura o soft-timeout (720s) → rollback total → loop sem
    progresso."""
    from ..vendors import ninjaone as no

    monkeypatch.setattr(no, "_MAX_PAGES_PER_CYCLE", 3)
    monkeypatch.setattr(no, "_PAGE_SIZE", 2)  # página "cheia" = 2 itens (não quebra cedo)

    # Cada página devolve 2 itens (page cheia, ids crescentes) → o loop nunca vê página
    # curta e continuaria drenando sem o teto. O cap corta em 3 páginas.
    def _full_page(k: int) -> List[Dict[str, Any]]:
        base = 2 * k
        return [_activity(base + 1), _activity(base + 2)]

    with aioresponses() as m:
        for k in range(8):  # registra páginas de sobra; o teto corta em 3
            m.get(_ACT_RE, payload=_full_page(k))
        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, cursor={"activity_time_after": 1719000000})
            collected = [ev async for ev in NinjaOneActivitiesCollector(ctx).collect()]

    # PAROU no teto: 3 páginas × 2 itens = 6 eventos (não drenou as 8 registradas).
    assert len(collected) == 3 * 2
    assert [e["id"] for e in collected] == [1, 2, 3, 4, 5, 6]
    # cursor RESUMÍVEL: after_id no maior id emitido (6), NÃO o watermark final; piso
    # temporal NÃO avançado. Próximo ciclo retoma em ?after=6 (id>6 = página 4).
    assert ctx.cursor == {"after_id": 6, "activity_time_after": 1719000000}
