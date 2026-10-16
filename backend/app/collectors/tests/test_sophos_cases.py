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
            {
                "id": f"c{i}",
                "createdAt": "2026-04-23T10:00:00Z",
                "updatedAt": "2026-04-23T12:00:00Z",
                "severity": "low",
            }
            for i in range(_PAGE_SIZE)
        ],
        "pages": {"current": 1, "size": _PAGE_SIZE, "total": _PAGE_SIZE + 1},
    }
    partial_page = {
        "items": [
            {
                "id": "c-final",
                "createdAt": "2026-04-23T11:00:00.250Z",
                "updatedAt": "2026-04-23T13:00:00Z",
                "severity": "critical",
            },
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
    # Cursor final: próxima janela começa do maior CREATEDAT visto (o campo do
    # filtro ``createdAfter``), em segundos — NÃO do updatedAt (13:00), que
    # pularia case criado antes e indexado depois. Paginação volta a 1, e a
    # memória da fronteira guarda o case que o próximo pedido vai devolver.
    assert ctx.cursor == {
        "created_after": "2026-04-23T11:00:00Z",
        "page": 1,
        "boundary": {
            "at": "2026-04-23T11:00:00Z",
            "keys": ["c-final::2026-04-23T13:00:00Z"],
        },
    }


# ── Janela parada no último case (incidente out/2026) ─────────────────────────
#
# ``createdAfter`` só aceita segundos inteiros. Um case criado às 17:06:39.978
# fazia o pedido seguinte começar em 17:06:39 e trazer o MESMO case. Sem case
# mais novo, a janela não andava: o case voltava a cada ciclo (3 min) e era
# reenviado a cada vencimento do TTL do dedupe (1 dia por padrão).

_STUCK = {
    "id": "case-parado",
    "createdAt": "2026-10-15T17:06:39.978Z",
    "updatedAt": "2026-10-15T17:06:39.978Z",
    "severity": "high",
}


def _page(*items: Dict[str, Any]) -> Dict[str, Any]:
    return {"items": list(items), "pages": {"current": 1, "size": 50, "total": len(items)}}


async def _cycle(cursor: Dict[str, Any] | None, *items: Dict[str, Any]):
    """Um ciclo de coleta contra uma página com ``items``; devolve
    (eventos emitidos, cursor gravado, params enviados à Sophos)."""
    with aioresponses() as m:
        m.get(_URL_RE, payload=_page(*items))
        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, cursor=cursor)
            collected = [ev async for ev in SophosCasesCollector(ctx).collect()]
        params = _captured_params(m)
    return collected, ctx.cursor, params


@pytest.mark.asyncio
async def test_last_case_does_not_come_back_every_cycle() -> None:
    """O case da fronteira sai UMA vez; nos ciclos seguintes a Sophos ainda o
    devolve (createdAfter truncado), mas o coletor o pula — e isso não depende do
    TTL do dedupe, então não há reenvio diário."""
    emitted_1, cursor_1, _ = await _cycle({"created_after": "2026-10-15T17:00:00Z"}, _STUCK)
    assert [e["id"] for e in emitted_1] == ["case-parado"]
    assert cursor_1["created_after"] == "2026-10-15T17:06:39Z"

    emitted_2, cursor_2, params_2 = await _cycle(cursor_1, _STUCK)
    # A janela recomeça no mesmo segundo (nada se perde) …
    assert params_2["createdAfter"] == "2026-10-15T17:06:39Z"
    # … mas o case já emitido NÃO sai de novo.
    assert emitted_2 == []

    # E continua lembrado: o terceiro ciclo (e o 480º do dia) também não reemite.
    emitted_3, cursor_3, _ = await _cycle(cursor_2, _STUCK)
    assert emitted_3 == []
    assert cursor_3 == cursor_2


@pytest.mark.asyncio
async def test_update_of_the_boundary_case_is_still_emitted() -> None:
    """A memória usa a chave do dedupe (``id::updatedAt``): um case da fronteira
    que MUDOU de status sai de novo, como sempre saiu."""
    _, cursor_1, _ = await _cycle({"created_after": "2026-10-15T17:00:00Z"}, _STUCK)

    updated = dict(_STUCK, updatedAt="2026-10-15T18:30:00.100Z", status="resolved")
    emitted, cursor_2, _ = await _cycle(cursor_1, updated)

    assert [e["status"] for e in emitted] == ["resolved"]
    assert cursor_2["boundary"]["keys"] == ["case-parado::2026-10-15T18:30:00.100Z"]


@pytest.mark.asyncio
async def test_case_created_in_the_same_second_but_indexed_later_is_not_lost() -> None:
    """Por que não somar 1s ao cursor: um case criado no MESMO segundo, que a
    Sophos só expõe depois do pedido, seria perdido. Com a memória ele sai, e só o
    já emitido é pulado."""
    _, cursor_1, _ = await _cycle({"created_after": "2026-10-15T17:00:00Z"}, _STUCK)

    late = {
        "id": "case-late",
        "createdAt": "2026-10-15T17:06:39.990Z",
        "updatedAt": "2026-10-15T17:06:39.990Z",
    }
    emitted, cursor_2, _ = await _cycle(cursor_1, _STUCK, late)

    assert [e["id"] for e in emitted] == ["case-late"]
    assert cursor_2["boundary"]["keys"] == [
        "case-late::2026-10-15T17:06:39.990Z",
        "case-parado::2026-10-15T17:06:39.978Z",
    ]


@pytest.mark.asyncio
async def test_window_advances_when_a_newer_case_arrives() -> None:
    """Case mais novo empurra a janela, e a memória passa a ser só a da nova
    fronteira — a antiga não cresce para sempre no cursor."""
    _, cursor_1, _ = await _cycle({"created_after": "2026-10-15T17:00:00Z"}, _STUCK)

    newer = {"id": "case-new", "createdAt": "2026-10-15T17:10:00.500Z", "updatedAt": "2026-10-15T17:10:00.500Z"}
    emitted, cursor_2, _ = await _cycle(cursor_1, _STUCK, newer)

    assert [e["id"] for e in emitted] == ["case-new"]
    assert cursor_2 == {
        "created_after": "2026-10-15T17:10:00Z",
        "page": 1,
        "boundary": {"at": "2026-10-15T17:10:00Z", "keys": ["case-new::2026-10-15T17:10:00.500Z"]},
    }


@pytest.mark.asyncio
async def test_cursor_written_before_this_fix_keeps_working() -> None:
    """Cursor antigo (sem ``boundary``, ``created_after`` vindo do updatedAt) não
    pula nada no primeiro ciclo e passa a gravar a memória. A janela não anda
    para trás: só cresce a partir do ``created_after`` gravado."""
    emitted, cursor, params = await _cycle(
        {"created_after": "2026-10-15T17:06:39Z", "page": 1}, _STUCK
    )
    assert params["createdAfter"] == "2026-10-15T17:06:39Z"
    assert [e["id"] for e in emitted] == ["case-parado"]  # sai uma última vez (dedupe segura)
    assert cursor["boundary"]["keys"] == ["case-parado::2026-10-15T17:06:39.978Z"]


@pytest.mark.asyncio
async def test_boundary_memory_of_another_window_does_not_apply() -> None:
    """Memória gravada para outra janela (cursor editado, reset para reprocessar)
    não se aplica: na dúvida emite, e o dedupe decide."""
    cursor = {
        "created_after": "2026-10-15T16:00:00Z",
        "page": 1,
        "boundary": {"at": "2026-10-15T17:06:39Z", "keys": ["case-parado::2026-10-15T17:06:39.978Z"]},
    }
    emitted, _, _ = await _cycle(cursor, _STUCK)
    assert [e["id"] for e in emitted] == ["case-parado"]


@pytest.mark.asyncio
async def test_resumable_cursor_keeps_the_boundary_memory(monkeypatch) -> None:
    """Teto por ciclo no meio da janela: o cursor resumível leva a memória da
    MESMA janela, senão a retomada reemitiria o case da fronteira."""
    from ..vendors import sophos_cases as sc

    monkeypatch.setattr(sc, "_MAX_PAGES_PER_CYCLE", 1)
    monkeypatch.setattr(sc, "_PAGE_SIZE", 1)

    boundary = {"at": "2026-10-15T17:06:39Z", "keys": ["case-parado::2026-10-15T17:06:39.978Z"]}
    emitted, cursor, _ = await _cycle(
        {"created_after": "2026-10-15T17:06:39Z", "page": 1, "boundary": boundary}, _STUCK
    )

    assert emitted == []
    assert cursor == {"created_after": "2026-10-15T17:06:39Z", "page": 2, "boundary": boundary}


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
    que avança conforme o maior ``createdAt`` visto — vence."""
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


@pytest.mark.asyncio
async def test_caps_pages_per_cycle_and_saves_resumable_cursor(monkeypatch) -> None:
    """Teto por ciclo (regressão do poison-loop de soft-timeout): com backlog MAIOR que o
    teto, ``collect()`` PARA após ``_MAX_PAGES_PER_CYCLE`` páginas em vez de drenar tudo
    num único run, e salva o cursor RESUMÍVEL (token da PRÓXIMA página + janela intacta)
    p/ o próximo ciclo RETOMAR — NÃO o watermark final (``created_after=latest_updated,
    page=1``), que descartaria as páginas não lidas. Sem isto, um backlog grande estoura o
    ``task_soft_time_limit`` (720s) → o pipeline reverte p/ ``cursor_before`` e solta as
    claims → loop sem progresso.

    Cobre TAMBÉM a preservação de ``backfill_to_ts`` no cursor resumível: sem ela, uma
    retomada mid-backfill perderia o teto superior (``createdBefore``) e paginaria até o
    presente."""
    from ..vendors import sophos_cases as sc

    monkeypatch.setattr(sc, "_MAX_PAGES_PER_CYCLE", 3)
    monkeypatch.setattr(sc, "_PAGE_SIZE", 2)  # página "cheia" = 2 items (não quebra cedo)

    # Cada página devolve 2 items (page cheia = _PAGE_SIZE) → o loop nunca vê página curta
    # e continuaria paginando até o presente sem o teto.
    full_page = {
        "items": [
            {"id": "c1", "updatedAt": "2026-04-23T10:00:00Z", "severity": "low"},
            {"id": "c2", "updatedAt": "2026-04-23T10:00:01Z", "severity": "low"},
        ],
        "pages": {"current": 1, "size": 2, "total": 999},
    }

    with aioresponses() as m:
        m.get(_URL_RE, payload=full_page, repeat=True)  # o teto corta em 3 fetches

        async with aiohttp.ClientSession() as session:
            ctx = _ctx(
                session,
                cursor={
                    "created_after": "2026-04-23T09:00:00Z",
                    "backfill_to_ts": "2026-05-01T00:00:00Z",
                },
            )
            collector = SophosCasesCollector(ctx)
            collected = [ev async for ev in collector.collect()]

    # PAROU no teto: 3 páginas × 2 items = 6 eventos (não drenou até o presente).
    assert len(collected) == 3 * 2
    # Cursor RESUMÍVEL: created_after FIXO (não avançado p/ latest_updated), page = a
    # PRÓXIMA página (4, não 1), e backfill_to_ts PRESERVADO (teto superior intacto).
    assert ctx.cursor == {
        "created_after": "2026-04-23T09:00:00Z",
        "page": 4,
        "backfill_to_ts": "2026-05-01T00:00:00Z",
    }
