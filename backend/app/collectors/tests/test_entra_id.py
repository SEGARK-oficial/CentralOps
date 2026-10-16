"""Microsoft Entra ID collectors — Graph signIns + audit.

Paginação @odata.nextLink, cursor incremental por createdDateTime/activityDateTime,
dedupe por id, collect→OCSF. Reusa OAuth Graph do Defender (zero refresher/probe novo).
"""

from __future__ import annotations

import re
from typing import Any, Dict
from unittest.mock import MagicMock

import aiohttp
import pytest

from ._aiohttp_mock import aioresponses
from ..base import CollectorContext
from ..normalize import engine as E
from ..normalize.defaults import load_default_rules
from ..vendors.entra_id import EntraDirectoryAuditCollector, EntraSignInsCollector

_SIGNINS_RE = re.compile(r"^https://graph\.microsoft\.com/v1\.0/auditLogs/signIns(\?.*)?$")
_AUDIT_RE = re.compile(r"^https://graph\.microsoft\.com/v1\.0/auditLogs/directoryAudits(\?.*)?$")


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


def _ctx(session, cursor: Dict[str, Any] | None = None) -> CollectorContext:
    return CollectorContext(
        integration_id=88,
        organization_id=3,
        platform="entra_id",
        headers={"Authorization": "Bearer graph-jwt"},
        session=session,
        cursor=cursor,
        domain_limiter=_NoopDomainLimiter(),
        rate_limiter=_NoopRateLimiter(),
        redis=MagicMock(),
    )


@pytest.mark.asyncio
async def test_signins_paginate_via_odata_nextlink_and_cursor() -> None:
    next_link = "https://graph.microsoft.com/v1.0/auditLogs/signIns?$skiptoken=abc"
    with aioresponses() as m:
        m.get(_SIGNINS_RE, payload={
            "value": [
                {"id": "s1", "createdDateTime": "2026-06-20T10:00:00Z", "userPrincipalName": "a@x.com",
                 "ipAddress": "1.1.1.1", "status": {"errorCode": 0}},
            ],
            "@odata.nextLink": next_link,
        })
        m.get(next_link, payload={
            "value": [
                {"id": "s2", "createdDateTime": "2026-06-20T11:00:00Z", "userPrincipalName": "b@x.com",
                 "ipAddress": "2.2.2.2", "status": {"errorCode": 50126, "failureReason": "Invalid password"}},
            ],
        })
        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, cursor={"createdDateTime": "2026-06-20T09:00:00Z"})
            collected = [ev async for ev in EntraSignInsCollector(ctx).collect()]

    assert [e["id"] for e in collected] == ["s1", "s2"]
    assert ctx.cursor == {
        "createdDateTime": "2026-06-20T11:00:00Z",
        "@odata.nextLink": None,
        # + memória da fronteira: o evento que o próximo ``ge`` vai devolver.
        "boundary": {"at": "2026-06-20T11:00:00Z", "keys": ["s2"]},
    }

    norm = E.apply_compiled(
        E.compile_rules(load_default_rules("entra_id", "entra_id.signin")), collected[0]
    ).output["normalized"]
    assert norm["class_uid"] == 3002
    assert norm["user"]["name"] == "a@x.com"
    assert norm["metadata"]["uid"] == "s1"
    assert norm["time"]


@pytest.mark.asyncio
async def test_directory_audit_collects_and_normalizes() -> None:
    with aioresponses() as m:
        m.get(_AUDIT_RE, payload={
            "value": [
                {"id": "a1", "activityDateTime": "2026-06-20T10:01:19Z",
                 "activityDisplayName": "Add member to group", "category": "GroupManagement",
                 "result": "success", "initiatedBy": {"user": {"userPrincipalName": "bob@x.com"}}},
            ],
        })
        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, cursor={"activityDateTime": "2026-06-20T09:00:00Z"})
            collected = [ev async for ev in EntraDirectoryAuditCollector(ctx).collect()]

    assert [e["id"] for e in collected] == ["a1"]
    assert ctx.cursor == {
        "activityDateTime": "2026-06-20T10:01:19Z",
        "@odata.nextLink": None,
        "boundary": {"at": "2026-06-20T10:01:19Z", "keys": ["a1"]},
    }
    norm = E.apply_compiled(
        E.compile_rules(load_default_rules("entra_id", "entra_id.audit")), collected[0]
    ).output["normalized"]
    assert norm["class_uid"] == 3001
    assert norm["actor"]["user"]["name"] == "bob@x.com"


@pytest.mark.parametrize(
    "collector_cls, endpoint_re, cursor_field",
    [
        (EntraSignInsCollector, _SIGNINS_RE, "createdDateTime"),
        (EntraDirectoryAuditCollector, _AUDIT_RE, "activityDateTime"),
    ],
)
@pytest.mark.asyncio
async def test_caps_pages_per_cycle_and_saves_resumable_cursor(
    monkeypatch, collector_cls, endpoint_re, cursor_field
) -> None:
    """Teto por ciclo (regressão do poison-loop de soft-timeout): com backlog MAIOR que o
    teto, ``collect()`` PARA após ``_MAX_PAGES_PER_CYCLE`` páginas em vez de drenar a cadeia
    de ``@odata.nextLink`` inteira num único run, e salva o cursor RESUMÍVEL (o nextLink da
    PRÓXIMA página, preservando ``last_ts`` — NÃO o watermark ``latest_seen`` nem
    nextLink=None) p/ o próximo ciclo RETOMAR sem descartar o resto do backlog. Sem isto,
    um backlog grande estoura o soft-timeout (720s) → rollback total → loop sem progresso.
    Cobre a subclasse ``EntraDirectoryAuditCollector`` (reusa o mesmo ``collect()``)."""
    from ..vendors import entra_id as ei

    monkeypatch.setattr(ei, "_MAX_PAGES_PER_CYCLE", 3)

    base = collector_cls._ENDPOINT
    # Cadeia de nextLinks: cada página "cheia" aponta a PRÓXIMA → o loop nunca vê página
    # final (nextLink ausente) e drenaria a cadeia inteira sem o teto.
    links = [f"{base}?$skiptoken=p{i}" for i in range(1, 8)]

    def _page(idx: int, next_link: str) -> Dict[str, Any]:
        return {
            "value": [{"id": f"e{idx}", cursor_field: f"2026-06-20T10:0{idx}:00Z"}],
            "@odata.nextLink": next_link,
        }

    with aioresponses() as m:
        # 1ª página: endpoint + $filter (casa o regex) → nextLink=links[0].
        m.get(endpoint_re, payload=_page(1, links[0]))
        # páginas subsequentes fetchadas pelo nextLink exato; cada uma aponta a próxima.
        for i in range(len(links) - 1):
            m.get(links[i], payload=_page(i + 2, links[i + 1]))
        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, cursor={cursor_field: "2026-06-20T09:00:00Z"})
            collected = [ev async for ev in collector_cls(ctx).collect()]

    # PAROU no teto: 3 páginas × 1 item = 3 eventos (não drenou a cadeia de 7 páginas).
    assert len(collected) == 3
    assert [e["id"] for e in collected] == ["e1", "e2", "e3"]
    # cursor RESUMÍVEL: aponta o @odata.nextLink da PRÓXIMA página (links[2]) e PRESERVA o
    # last_ts inicial — NÃO avança p/ latest_seen ("2026-06-20T10:03:00Z") nem zera o token.
    assert ctx.cursor == {cursor_field: "2026-06-20T09:00:00Z", "@odata.nextLink": links[2]}


def test_registered_zero_core_reusing_defender_oauth() -> None:
    from ..registry import get, get_platform, has
    from ..capabilities import invalid_capabilities

    assert has("entra_id", "signins") and has("entra_id", "audit")
    plat = get_platform("entra_id")
    assert plat is not None and plat.test_fn is not None
    assert invalid_capabilities(plat.capabilities) == []
    # refresher e probe REUSADOS do Defender (zero novo)
    assert get("entra_id", "signins").refresh_fn.__name__ == "defender_refresher"
    assert plat.test_fn.__name__ == "defender_probe"


# ── Janela parada no último evento (incidente out/2026, mesma classe do Sophos) ──
#
# ``$filter=<campo> ge <max>`` é INCLUSIVO: o evento cujo timestamp é o próprio
# máximo volta em todo ciclo. Num tenant quieto a janela não anda e ele era
# reenviado a cada vencimento do TTL do dedup. O cursor agora lembra a fronteira.

_BOUNDARY_CASES = [
    (EntraSignInsCollector, _SIGNINS_RE, "createdDateTime"),
    (EntraDirectoryAuditCollector, _AUDIT_RE, "activityDateTime"),
]
_BOUNDARY_PARAMS = pytest.mark.parametrize(
    "collector_cls, endpoint_re, field", _BOUNDARY_CASES
)
_T0 = "2026-10-15T17:00:00Z"
_T1 = "2026-10-15T17:06:39.1234567Z"  # 7 casas, como o Graph devolve


async def _cycle(collector_cls, endpoint_re, field, cursor, *items):
    """Um ciclo contra uma página com ``items``; devolve (emitidos, cursor, $filter)."""
    with aioresponses() as m:
        m.get(endpoint_re, payload={"value": list(items)})
        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, cursor=cursor)
            collected = [ev async for ev in collector_cls(ctx).collect()]
        calls = [c for cs in m.requests.values() for c in cs]
        assert calls, "nenhuma requisição capturada"
        flt = (calls[0].kwargs.get("params") or {}).get("$filter")
    return collected, ctx.cursor, flt


@_BOUNDARY_PARAMS
@pytest.mark.asyncio
async def test_last_event_does_not_come_back_every_cycle(collector_cls, endpoint_re, field) -> None:
    ev = {"id": "e1", field: _T1}
    emitted_1, cursor_1, _ = await _cycle(collector_cls, endpoint_re, field, {field: _T0}, ev)
    assert [e["id"] for e in emitted_1] == ["e1"]
    assert cursor_1 == {
        field: _T1,
        "@odata.nextLink": None,
        "boundary": {"at": _T1, "keys": ["e1"]},
    }

    # O Graph devolve o mesmo evento (``ge``): a janela recomeça no MESMO ponto
    # (nada se perde), mas o evento já emitido não sai de novo.
    emitted_2, cursor_2, filter_2 = await _cycle(collector_cls, endpoint_re, field, cursor_1, ev)
    assert filter_2 == f"{field} ge {_T1}"
    assert emitted_2 == []
    assert cursor_2 == cursor_1

    emitted_3, cursor_3, filter_3 = await _cycle(collector_cls, endpoint_re, field, cursor_2, ev)
    assert emitted_3 == []
    assert filter_3 == filter_2
    assert cursor_3 == cursor_2


@_BOUNDARY_PARAMS
@pytest.mark.asyncio
async def test_new_event_at_same_instant_is_emitted_old_one_skipped(
    collector_cls, endpoint_re, field
) -> None:
    old = {"id": "e1", field: _T1}
    _, cursor_1, _ = await _cycle(collector_cls, endpoint_re, field, {field: _T0}, old)
    late = {"id": "e2", field: _T1}

    emitted, cursor_2, _ = await _cycle(collector_cls, endpoint_re, field, cursor_1, old, late)

    assert [e["id"] for e in emitted] == ["e2"]
    assert cursor_2["boundary"] == {"at": _T1, "keys": ["e1", "e2"]}


@_BOUNDARY_PARAMS
@pytest.mark.asyncio
async def test_cursor_advances_by_instant_not_by_string(collector_cls, endpoint_re, field) -> None:
    """``"…39Z" > "…39.5Z"`` em string (``Z`` > ``.``): comparar texto deixaria o
    cursor no timestamp MAIS ANTIGO. O máximo é por instante."""
    early = {"id": "e1", field: "2026-10-15T17:06:39Z"}
    later = {"id": "e2", field: "2026-10-15T17:06:39.5Z"}

    _, cursor, _ = await _cycle(collector_cls, endpoint_re, field, {field: _T0}, early, later)

    assert cursor[field] == "2026-10-15T17:06:39.5Z"
    assert cursor["boundary"]["keys"] == ["e2"]


@_BOUNDARY_PARAMS
@pytest.mark.asyncio
async def test_resumable_cursor_keeps_the_boundary_memory(
    monkeypatch, collector_cls, endpoint_re, field
) -> None:
    """Teto por ciclo no meio da janela: o cursor resumível leva a memória da
    MESMA janela, senão a retomada reemitiria o evento da fronteira."""
    from ..vendors import entra_id as ei

    monkeypatch.setattr(ei, "_MAX_PAGES_PER_CYCLE", 1)
    boundary = {"at": _T0, "keys": ["e1"]}
    link = f"{collector_cls._ENDPOINT}?$skiptoken=p2"
    with aioresponses() as m:
        m.get(endpoint_re, payload={"value": [{"id": "e1", field: _T0}], "@odata.nextLink": link})
        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, cursor={field: _T0, "boundary": boundary})
            collected = [ev async for ev in collector_cls(ctx).collect()]

    assert collected == []  # e1 estava na memória
    assert ctx.cursor == {field: _T0, "@odata.nextLink": link, "boundary": boundary}
