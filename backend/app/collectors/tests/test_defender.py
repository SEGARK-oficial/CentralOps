"""DefenderIncidentsCollector — paginação Graph via ``@odata.nextLink``.

Cobre: paginação nextLink + cursor final (watermark avança em ``latest_seen``,
nextLink zerado) e o TETO por ciclo (regressão do poison-loop de soft-timeout):
com backlog maior que o teto, ``collect()`` PARA e salva o cursor RESUMÍVEL — o
``@odata.nextLink`` da PRÓXIMA página, com ``lastUpdateDateTime`` preservado em
``last_ts`` (NÃO no watermark ``latest_seen``) — de modo que o próximo ciclo
retome sem pular nem descartar as páginas ainda não lidas.
"""

from __future__ import annotations

import re
from typing import Any, Dict
from unittest.mock import MagicMock

import aiohttp
import pytest

from ._aiohttp_mock import aioresponses
from ..base import CollectorContext
from ..vendors.defender import DefenderIncidentsCollector

_INCIDENTS_RE = re.compile(
    r"^https://graph\.microsoft\.com/v1\.0/security/incidents(\?.*)?$"
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


def _incident(inc_id: str, ts: str) -> Dict[str, Any]:
    return {"id": inc_id, "lastUpdateDateTime": ts, "status": "active"}


@pytest.mark.asyncio
async def test_paginates_via_odata_next_link_and_finalizes_cursor() -> None:
    next_link = "https://graph.microsoft.com/v1.0/security/incidents?$skiptoken=abc"
    with aioresponses() as m:
        m.get(
            _INCIDENTS_RE,
            payload={
                "value": [_incident("I1", "2026-04-23T10:00:00Z")],
                "@odata.nextLink": next_link,
            },
        )
        m.get(
            _INCIDENTS_RE,
            payload={"value": [_incident("I2", "2026-04-23T11:00:00Z")]},
        )
        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, cursor={"lastUpdateDateTime": "2026-04-23T09:00:00Z"})
            collected = [ev async for ev in DefenderIncidentsCollector(ctx).collect()]

    assert [e["id"] for e in collected] == ["I1", "I2"]
    # Conclusão real (backlog drenado: sem nextLink na última página) → cursor FINAL
    # avança o watermark p/ latest_seen e zera o token.
    assert ctx.cursor == {
        "lastUpdateDateTime": "2026-04-23T11:00:00Z",
        "@odata.nextLink": None,
        # + memória da fronteira: o incidente que o próximo ``ge`` vai devolver.
        "boundary": {
            "at": "2026-04-23T11:00:00Z",
            "keys": ["I2@2026-04-23T11:00:00Z"],
        },
    }


@pytest.mark.asyncio
async def test_caps_pages_per_cycle_and_saves_resumable_cursor(monkeypatch) -> None:
    """Teto por ciclo (regressão do poison-loop de soft-timeout): com backlog MAIOR que o
    teto, ``collect()`` PARA após ``_MAX_PAGES_PER_CYCLE`` páginas em vez de seguir o
    ``@odata.nextLink`` até drenar tudo num único run, e salva o cursor RESUMÍVEL (o
    nextLink da PRÓXIMA página) p/ o próximo ciclo RETOMAR. A ARMADILHA evitada: NÃO cair
    na escrita FINAL do cursor (que avança o watermark p/ ``latest_seen`` e ZERA o
    nextLink), o que descartaria as páginas não lidas. Sem o teto, um backlog grande
    estoura o soft-timeout (720s) → o pipeline reverte p/ cursor_before → loop sem
    progresso."""
    from ..vendors import defender as dfn

    monkeypatch.setattr(dfn, "_MAX_PAGES_PER_CYCLE", 3)

    def _page(n: int) -> Dict[str, Any]:
        # Cada página traz nextLink p/ a seguinte (backlog "infinito") → o loop só para
        # pelo teto. ts crescentes: latest_seen avançaria, mas o cap-hit preserva last_ts.
        return {
            "value": [_incident(f"I{n}", f"2026-04-23T10:00:0{n}Z")],
            "@odata.nextLink": (
                f"https://graph.microsoft.com/v1.0/security/incidents?$skiptoken=page{n + 1}"
            ),
        }

    with aioresponses() as m:
        for n in range(1, 8):  # registra páginas de sobra; o teto corta em 3
            m.get(_INCIDENTS_RE, payload=_page(n))
        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, cursor={"lastUpdateDateTime": "2026-04-23T09:00:00Z"})
            collected = [ev async for ev in DefenderIncidentsCollector(ctx).collect()]

    # PAROU no teto: 3 páginas × 1 incidente = 3 eventos (não drenou o backlog inteiro).
    assert [e["id"] for e in collected] == ["I1", "I2", "I3"]
    # Cursor RESUMÍVEL: nextLink da PRÓXIMA página (page4, retornado pela 3ª página) e
    # lastUpdateDateTime PRESERVADO em last_ts (NÃO o watermark latest_seen=...:03Z, que
    # descartaria páginas). O próximo ciclo retoma exatamente deste nextLink.
    assert ctx.cursor == {
        "lastUpdateDateTime": "2026-04-23T09:00:00Z",
        "@odata.nextLink": "https://graph.microsoft.com/v1.0/security/incidents?$skiptoken=page4",
    }


# ── Janela parada no último incidente (incidente out/2026, mesma classe do Sophos) ─
#
# ``$filter=lastUpdateDateTime ge <max>`` é INCLUSIVO: o incidente cujo update é o
# próprio máximo volta em todo ciclo. Num tenant quieto a janela não anda e ele era
# reenviado a cada vencimento do TTL do dedup. O cursor agora lembra a fronteira.

_STUCK = {
    "id": "inc-1",
    "lastUpdateDateTime": "2026-10-15T17:06:39.1234567Z",
    "status": "active",
}


async def _cycle(cursor: Dict[str, Any] | None, *items: Dict[str, Any]):
    """Um ciclo contra uma página com ``items``; devolve (emitidos, cursor, $filter)."""
    with aioresponses() as m:
        m.get(_INCIDENTS_RE, payload={"value": list(items)})
        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, cursor=cursor)
            collected = [ev async for ev in DefenderIncidentsCollector(ctx).collect()]
        calls = [c for cs in m.requests.values() for c in cs]
        assert calls, "nenhuma requisição capturada"
        flt = (calls[0].kwargs.get("params") or {}).get("$filter")
    return collected, ctx.cursor, flt


@pytest.mark.asyncio
async def test_last_incident_does_not_come_back_every_cycle() -> None:
    start = "2026-10-15T17:00:00Z"
    emitted_1, cursor_1, _ = await _cycle({"lastUpdateDateTime": start}, _STUCK)
    assert [e["id"] for e in emitted_1] == ["inc-1"]
    assert cursor_1 == {
        "lastUpdateDateTime": "2026-10-15T17:06:39.1234567Z",
        "@odata.nextLink": None,
        "boundary": {
            "at": "2026-10-15T17:06:39.1234567Z",
            "keys": ["inc-1@2026-10-15T17:06:39.1234567Z"],
        },
    }

    # O Graph devolve o mesmo incidente (``ge``): a janela recomeça no MESMO ponto
    # (nada se perde), mas o incidente já emitido não sai de novo.
    emitted_2, cursor_2, filter_2 = await _cycle(cursor_1, _STUCK)
    assert filter_2 == "lastUpdateDateTime ge 2026-10-15T17:06:39.1234567Z"
    assert emitted_2 == []
    assert cursor_2 == cursor_1

    emitted_3, cursor_3, filter_3 = await _cycle(cursor_2, _STUCK)
    assert emitted_3 == []
    assert filter_3 == filter_2
    assert cursor_3 == cursor_2


@pytest.mark.asyncio
async def test_new_incident_at_same_instant_is_emitted_old_one_skipped() -> None:
    _, cursor_1, _ = await _cycle({"lastUpdateDateTime": "2026-10-15T17:00:00Z"}, _STUCK)
    late = _incident("inc-late", _STUCK["lastUpdateDateTime"])

    emitted, cursor_2, _ = await _cycle(cursor_1, _STUCK, late)

    assert [e["id"] for e in emitted] == ["inc-late"]
    assert cursor_2["boundary"]["keys"] == [
        "inc-1@2026-10-15T17:06:39.1234567Z",
        "inc-late@2026-10-15T17:06:39.1234567Z",
    ]


@pytest.mark.asyncio
async def test_update_of_boundary_incident_is_still_emitted() -> None:
    """Chave = ``id@lastUpdateDateTime``: incidente da fronteira que MUDOU de
    estado (novo lastUpdateDateTime) sai de novo, como sempre saiu."""
    _, cursor_1, _ = await _cycle({"lastUpdateDateTime": "2026-10-15T17:00:00Z"}, _STUCK)
    updated = dict(_STUCK, lastUpdateDateTime="2026-10-15T18:30:00Z", status="resolved")

    emitted, cursor_2, _ = await _cycle(cursor_1, updated)

    assert [e["status"] for e in emitted] == ["resolved"]
    assert cursor_2["lastUpdateDateTime"] == "2026-10-15T18:30:00Z"
    assert cursor_2["boundary"]["keys"] == ["inc-1@2026-10-15T18:30:00Z"]


@pytest.mark.asyncio
async def test_cursor_advances_by_instant_not_by_string() -> None:
    """``"…39Z" > "…39.5Z"`` em string (``Z`` > ``.``): comparar texto deixaria o
    cursor no timestamp MAIS ANTIGO. O máximo é por instante."""
    early = _incident("inc-a", "2026-10-15T17:06:39Z")
    later = _incident("inc-b", "2026-10-15T17:06:39.5Z")

    _, cursor, _ = await _cycle({"lastUpdateDateTime": "2026-10-15T17:00:00Z"}, early, later)

    assert cursor["lastUpdateDateTime"] == "2026-10-15T17:06:39.5Z"
    assert cursor["boundary"]["keys"] == ["inc-b@2026-10-15T17:06:39.5Z"]


@pytest.mark.asyncio
async def test_resumable_cursor_keeps_the_boundary_memory(monkeypatch) -> None:
    """Teto por ciclo no meio da janela: o cursor resumível leva a memória da
    MESMA janela, senão a retomada reemitiria o incidente da fronteira."""
    from ..vendors import defender as dfn

    monkeypatch.setattr(dfn, "_MAX_PAGES_PER_CYCLE", 1)
    boundary = {"at": "2026-10-15T17:06:39Z", "keys": ["inc-1@2026-10-15T17:06:39Z"]}
    link = "https://graph.microsoft.com/v1.0/security/incidents?$skiptoken=p2"
    with aioresponses() as m:
        m.get(_INCIDENTS_RE, payload={"value": [], "@odata.nextLink": link})
        async with aiohttp.ClientSession() as session:
            ctx = _ctx(
                session, cursor={"lastUpdateDateTime": "2026-10-15T17:06:39Z", "boundary": boundary}
            )
            collected = [ev async for ev in DefenderIncidentsCollector(ctx).collect()]

    assert collected == []
    assert ctx.cursor == {
        "lastUpdateDateTime": "2026-10-15T17:06:39Z",
        "@odata.nextLink": link,
        "boundary": boundary,
    }
