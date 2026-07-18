"""Veeam Backup & Replication — collector de sessões (VBR REST API).

Cobre: OAuth2 password grant auto-contido (token dentro do collect(), honrando
verify_ssl), paginação skip/limit sobre ``{"data": [...], "pagination": {...}}``,
cursor (janela ``createdAfterFilter`` + ``skip`` resumível), dedupe por ``id``,
collect→OCSF, e o TETO por ciclo (regressão do poison-loop de soft-timeout).
Vendor 1-módulo zero-core.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import aiohttp
import pytest

from ._aiohttp_mock import aioresponses
from ..base import CollectorContext
from ..normalize import engine as E
from ..normalize.defaults import load_default_rules
from ..vendors import veeam as vm
from ..vendors.veeam import VeeamSessionsCollector

_TOKEN_RE = re.compile(r"^https://vbr\.local:9419/api/oauth2/token")
_SESSIONS_RE = re.compile(r"^https://vbr\.local:9419/api/v1/sessions")

_CONN = {
    "base_url": "https://vbr.local:9419",
    "username": "ACME\\svc_centralops",
    "password": "s3cr3t",
    "api_version": "1.2-rev0",
    "verify_ssl": False,
}
_TOKEN = {"access_token": "tok-abc", "expires_in": 900, "refresh_token": "ref-xyz"}


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


def _ctx(session, cursor: Dict[str, Any] | None = None, *, bounded: bool = True) -> CollectorContext:
    return CollectorContext(
        integration_id=77,
        organization_id=3,
        platform="veeam",
        headers={},
        session=session,
        cursor=cursor,
        domain_limiter=_NoopDomainLimiter(),
        rate_limiter=_NoopRateLimiter(),
        redis=MagicMock(),
        bounded_per_cycle=bounded,
    )


def _session(sid: str, created: str, result: str = "Success", stype: str = "BackupJob"):
    return {
        "id": sid,
        "name": "Daily Backup - SQL",
        "jobId": "9c1f0f2e-0000-4000-8000-000000000001",
        "sessionType": stype,
        "state": "Stopped",
        "creationTime": created,
        "endTime": "2026-07-18T02:14:31.000Z",
        "progressPercent": 100,
        "usn": 4711,
        "resourceId": "1c0e0000-0000-4000-8000-00000000000a",
        "resourceReference": "/api/v1/jobs/9c1f0f2e-0000-4000-8000-000000000001",
        "result": {"result": result, "message": "", "isCanceled": False},
    }


def _page(items: List[Dict[str, Any]], total: int, skip: int, limit: int):
    return {
        "data": items,
        "pagination": {"total": total, "count": len(items), "skip": skip, "limit": limit},
    }


@pytest.mark.asyncio
async def test_collects_sessions_and_normalizes_ocsf() -> None:
    items = [
        _session("s-1", "2026-07-18T02:00:00.000Z"),
        _session("s-2", "2026-07-18T02:10:00.000Z", result="Failed", stype="ReplicaJob"),
    ]
    with aioresponses() as m:
        m.post(_TOKEN_RE, payload=dict(_TOKEN))
        m.get(_SESSIONS_RE, payload=_page(items, total=2, skip=0, limit=200))
        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, cursor={"created_after": "2026-07-18T00:00:00Z", "skip": 0})
            with patch.object(VeeamSessionsCollector, "_load_conn", return_value=dict(_CONN)):
                collector = VeeamSessionsCollector(ctx)
                collected = [ev async for ev in collector.collect()]

    assert [e["id"] for e in collected] == ["s-1", "s-2"]
    assert collector.extract_message_id(collected[1]) == "s-2"
    assert collector.domain == "vbr.local"
    # Watermark = maior creationTime, recuado pela sobreposição (filtro EXCLUSIVO);
    # offset zerado porque a janela foi drenada.
    assert ctx.cursor == {"created_after": "2026-07-18T02:09:58Z", "skip": 0}

    rules = E.compile_rules(load_default_rules("veeam", "veeam.session"))
    norm = E.apply_compiled(rules, collected[0]).output["normalized"]
    # OCSF 1.8.0: uma sessão de job de backup é 1006 Scheduled Job Activity
    # (category 1 System Activity) — NÃO 6003 API Activity. Ver ocsf/classes.py.
    assert norm["class_uid"] == 1006
    assert norm["category_uid"] == 1
    assert norm["activity_id"] == 6 and norm["type_uid"] == 100606  # 6 = Start
    # ``job`` e ``device`` são REQUIRED em 1006 (manifesto oficial).
    assert norm["job"]["name"]
    assert norm["job"]["run_state_id"] in {1, 2, 3, 4, 99}
    assert norm["device"]["type_id"] == 1
    assert norm["metadata"]["uid"] == "s-1"
    assert norm["severity_id"] == 1 and norm["status_id"] == 1
    assert norm["time"]

    failed = E.apply_compiled(rules, collected[1]).output["normalized"]
    assert failed["severity_id"] == 4  # result.result == "Failed"
    assert failed["status_id"] == 2


@pytest.mark.asyncio
async def test_paginates_via_skip_and_stops_on_short_page(monkeypatch) -> None:
    monkeypatch.setattr(vm, "_PAGE_SIZE", 2)
    p1 = [_session("s-1", "2026-07-18T02:00:00.000Z"), _session("s-2", "2026-07-18T02:01:00.000Z")]
    p2 = [_session("s-3", "2026-07-18T02:02:00.000Z")]

    with aioresponses() as m:
        m.post(_TOKEN_RE, payload=dict(_TOKEN))
        m.get(_SESSIONS_RE, payload=_page(p1, total=3, skip=0, limit=2))
        m.get(_SESSIONS_RE, payload=_page(p2, total=3, skip=2, limit=2))
        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, cursor={"created_after": "2026-07-18T00:00:00Z", "skip": 0})
            with patch.object(VeeamSessionsCollector, "_load_conn", return_value=dict(_CONN)):
                collected = [ev async for ev in VeeamSessionsCollector(ctx).collect()]

        skips = sorted(
            url.query.get("skip")
            for (method, url) in m.requests
            if method == "GET"
        )

    assert [e["id"] for e in collected] == ["s-1", "s-2", "s-3"]
    assert skips == ["0", "2"]  # o offset avançou entre as páginas
    assert ctx.cursor == {"created_after": "2026-07-18T02:01:58Z", "skip": 0}


@pytest.mark.asyncio
async def test_caps_pages_per_cycle_and_saves_resumable_cursor(monkeypatch) -> None:
    """Teto por ciclo (regressão do poison-loop de soft-timeout): com backlog MAIOR que
    o teto, ``collect()`` PARA após ``_MAX_PAGES_PER_CYCLE`` páginas em vez de paginar
    até exaurir o VBR num único run, e salva um cursor RESUMÍVEL — a MESMA janela
    ``created_after`` + o ``skip`` da PRÓXIMA página. O watermark NÃO avança: se
    avançasse, o skip zerado descartaria as páginas ainda não lidas desta janela
    (perda de dado). Sem isto, um backlog grande estoura o soft-timeout (720s) →
    rollback p/ cursor_before → loop sem progresso."""
    monkeypatch.setattr(vm, "_PAGE_SIZE", 2)
    monkeypatch.setattr(vm, "_MAX_PAGES_PER_CYCLE", 2)
    window = "2026-07-01T00:00:00Z"
    full = [_session("s-a", "2026-07-18T02:00:00.000Z"), _session("s-b", "2026-07-18T02:01:00.000Z")]

    with aioresponses() as m:
        m.post(_TOKEN_RE, payload=dict(_TOKEN))
        # Backlog "infinito": toda página vem cheia (total alto) — o teto é o único freio.
        m.get(_SESSIONS_RE, payload=_page(full, total=10_000, skip=0, limit=2), repeat=True)
        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, cursor={"created_after": window, "skip": 0})
            with patch.object(VeeamSessionsCollector, "_load_conn", return_value=dict(_CONN)):
                collected = [ev async for ev in VeeamSessionsCollector(ctx).collect()]

    assert len(collected) == 2 * 2  # parou no teto (2 páginas), não drenou o backlog
    # Cursor resumível: janela INTACTA + offset da próxima página (≠ watermark final).
    assert ctx.cursor == {"created_after": window, "skip": 4}


@pytest.mark.asyncio
async def test_backfill_ignores_cap_and_drains(monkeypatch) -> None:
    """``bounded_per_cycle=False`` (backfill one-shot) DRENA a janela — capar ali
    truncaria o job silenciosamente."""
    monkeypatch.setattr(vm, "_PAGE_SIZE", 2)
    monkeypatch.setattr(vm, "_MAX_PAGES_PER_CYCLE", 1)
    pages = [
        [_session("s-1", "2026-07-18T02:00:00.000Z"), _session("s-2", "2026-07-18T02:01:00.000Z")],
        [_session("s-3", "2026-07-18T02:02:00.000Z"), _session("s-4", "2026-07-18T02:03:00.000Z")],
        [_session("s-5", "2026-07-18T02:04:00.000Z")],
    ]
    with aioresponses() as m:
        m.post(_TOKEN_RE, payload=dict(_TOKEN))
        for idx, page in enumerate(pages):
            m.get(_SESSIONS_RE, payload=_page(page, total=5, skip=idx * 2, limit=2))
        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, cursor=None, bounded=False)
            with patch.object(VeeamSessionsCollector, "_load_conn", return_value=dict(_CONN)):
                collected = [ev async for ev in VeeamSessionsCollector(ctx).collect()]

    assert len(collected) == 5
    assert ctx.cursor["skip"] == 0


@pytest.mark.asyncio
async def test_empty_window_keeps_watermark_parked() -> None:
    """Ciclo sem eventos NÃO recua o watermark (senão a janela andaria para trás
    a cada ciclo vazio)."""
    window = "2026-07-18T02:00:00Z"
    with aioresponses() as m:
        m.post(_TOKEN_RE, payload=dict(_TOKEN))
        m.get(_SESSIONS_RE, payload=_page([], total=0, skip=0, limit=200))
        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, cursor={"created_after": window, "skip": 0})
            with patch.object(VeeamSessionsCollector, "_load_conn", return_value=dict(_CONN)):
                collected = [ev async for ev in VeeamSessionsCollector(ctx).collect()]

    assert collected == []
    assert ctx.cursor == {"created_after": window, "skip": 0}


@pytest.mark.asyncio
async def test_sends_bearer_token_and_api_version_header() -> None:
    """Token do password-grant vira ``Authorization: Bearer`` e o ``x-api-version``
    (obrigatório — sem ele a request falha) acompanha TODA request."""
    with aioresponses() as m:
        m.post(_TOKEN_RE, payload=dict(_TOKEN))
        m.get(_SESSIONS_RE, payload=_page([], total=0, skip=0, limit=200))
        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, cursor=None)
            with patch.object(VeeamSessionsCollector, "_load_conn", return_value=dict(_CONN)):
                _ = [ev async for ev in VeeamSessionsCollector(ctx).collect()]

        post_call = next(c[0] for (mth, _u), c in m.requests.items() if mth == "POST")
        get_call = next(c[0] for (mth, _u), c in m.requests.items() if mth == "GET")

    assert post_call.kwargs["data"]["grant_type"] == "password"
    assert post_call.kwargs["data"]["username"] == "ACME\\svc_centralops"
    assert post_call.kwargs["headers"]["x-api-version"] == "1.2-rev0"
    assert get_call.kwargs["headers"]["Authorization"] == "Bearer tok-abc"
    assert get_call.kwargs["headers"]["x-api-version"] == "1.2-rev0"
    # verify_ssl=False na integração → ssl desligado nas DUAS chamadas (VBR sai de
    # fábrica com certificado auto-assinado).
    assert post_call.kwargs["ssl"] is False and get_call.kwargs["ssl"] is False
    assert get_call.kwargs["params"]["orderColumn"] == "CreationTime"


def test_registered_zero_core_with_password_grant_probe() -> None:
    from ..registry import get, get_platform, has
    from ..capabilities import invalid_capabilities

    assert has("veeam", "sessions")
    plat = get_platform("veeam")
    assert plat is not None and plat.test_fn is not None
    assert invalid_capabilities(plat.capabilities) == []
    assert "collect:sessions" in plat.capabilities
    # segredo 'password' no store; refresher no-op (token obtido no collect())
    assert "password" in {f.key for f in plat.auth_fields if f.type == "secret"}
    assert plat.required_secrets == ("password",)
    keys = {f.key for f in plat.auth_fields}
    assert {"base_url", "client_id", "region", "verify_ssl"} <= keys  # reuso genérico
    assert get("veeam", "sessions").refresh_fn.__name__ == "_veeam_refresher"


def test_watermark_compares_datetime_not_string() -> None:
    """O VBR devolve ``creationTime`` com fração+offset; o watermark que geramos não
    tem fração. Comparar as STRINGS faria o watermark travar (re-coleta infinita da
    mesma janela) — a comparação é por datetime, normalizada em UTC."""
    assert vm._max_iso("2026-07-18T13:38:22Z", "2026-07-18T17:10:00.000+02:00") == (
        "2026-07-18T15:10:00.000Z"
    )
    # candidato ANTERIOR ao watermark não recua o cursor
    assert vm._max_iso("2026-07-18T13:38:22Z", "2026-07-18T02:10:00.000Z") == (
        "2026-07-18T13:38:22Z"
    )
    # lixo/ausente é ignorado
    assert vm._max_iso("2026-07-18T13:38:22Z", None) == "2026-07-18T13:38:22Z"


def test_default_mapping_registered() -> None:
    rules = load_default_rules("veeam", "veeam.session")
    assert any(r["target"] == "normalized.metadata.uid" for r in rules["rules"])
