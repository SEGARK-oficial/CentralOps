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
    assert ctx.cursor == {"createdDateTime": "2026-06-20T11:00:00Z", "@odata.nextLink": None}

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
    assert ctx.cursor == {"activityDateTime": "2026-06-20T10:01:19Z", "@odata.nextLink": None}
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
