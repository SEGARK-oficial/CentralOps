"""Microsoft 365 — Management Activity API: assinatura, janelas, blobs, probe e mappings.

Os riscos que estes testes travam:

- **perder evento sem barulho** — janela que não sobrepõe, catch-up que pula
  trecho, teto sem cursor resumível, blob expirado que derruba o ciclo;
- **martelar a API** — ``subscriptions/start`` a cada ciclo (regra de 15 min),
  blob da sobreposição baixado de novo, chamada sem ``PublisherIdentifier``;
- **entregar o token do cliente a outro host** — ``NextPageUri``/``contentUri``
  fora de ``manage.office.com``;
- **vazar dado sensível** — valores detectados pelo DLP no bruto, segredo na UI.

Tudo afirmado pelas chamadas que os dublês registram, nunca pelo texto do fonte.
"""

from __future__ import annotations

import copy
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock

import aiohttp
import pytest

from . import _microsoft_365_fixtures as F
from ._aiohttp_mock import aioresponses
from ..base import CollectorContext
from ..normalize import engine as E
from ..normalize.defaults import load_default_rules
from ..normalize.ocsf import validator as V
from ..registry import get, get_platform
from ..vendors import microsoft_365 as M

_FEED = f"https://manage.office.com/api/v1.0/{F.TENANT}/activity/feed"
_START = re.compile(r"^https://manage\.office\.com/api/v1\.0/[^/]+/activity/feed/subscriptions/start\?")
_CONTENT = re.compile(r"^https://manage\.office\.com/api/v1\.0/[^/]+/activity/feed/subscriptions/content\?")
_TOKEN = f"https://login.microsoftonline.com/{F.TENANT}/oauth2/v2.0/token"
_LIST = re.compile(r"^https://manage\.office\.com/api/v1\.0/[^/]+/activity/feed/subscriptions/list")


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


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(
        M, "_load_settings",
        lambda _id: {"tenant_id": F.TENANT, "client_id": F.CLIENT, "client_secret": "s3gredo-de-teste"},
    )


def _ctx(session, cursor: Optional[Dict[str, Any]] = None, *, bounded: bool = True) -> CollectorContext:
    return CollectorContext(
        integration_id=77,
        organization_id=3,
        platform="microsoft_365",
        headers={"Authorization": "Bearer m365-teste"},
        session=session,
        cursor=cursor,
        domain_limiter=_NoopDomainLimiter(),
        rate_limiter=_NoopRateLimiter(),
        redis=MagicMock(),
        bounded_per_cycle=bounded,
    )


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _ago(minutes: float) -> str:
    return _iso(datetime.now(timezone.utc) - timedelta(minutes=minutes))


def _blob(cid: str, created: str, *, host: str = "manage.office.com") -> Dict[str, Any]:
    return {
        "contentType": "Audit.AzureActiveDirectory",
        "contentId": cid,
        "contentUri": f"https://{host}/api/v1.0/{F.TENANT}/activity/feed/audit/{cid}",
        "contentCreated": created,
        "contentExpiration": created,
    }


def _blob_url(cid: str) -> re.Pattern:
    return re.compile(rf"^https://manage\.office\.com/api/v1\.0/[^/]+/activity/feed/audit/{cid}(\?|$)")


def _calls(m, method: str, needle: str) -> List[Tuple[str, Dict[str, str]]]:
    out = []
    for (meth, url), calls in m.requests.items():
        if meth == method and needle in str(url):
            out.extend((str(url), dict(url.query)) for _ in calls)
    return out


def _parse(value: str) -> datetime:
    text = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _collect(collector) -> List[Dict[str, Any]]:
    return [ev async for ev in collector.collect()]


# ══ Assinatura ═════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_first_cycle_starts_the_subscription_and_reads_the_window() -> None:
    created = _ago(2)
    with aioresponses() as m:
        m.post(_START, payload={"contentType": "Audit.AzureActiveDirectory", "status": "enabled"})
        m.get(_CONTENT, payload=[_blob("c1", created)])
        m.get(_blob_url("c1"), payload=[F.USER_LOGGED_IN, F.USER_LOGIN_FAILED])
        async with aiohttp.ClientSession() as session:
            collector = M.Microsoft365AuditAadCollector(_ctx(session))
            events = await _collect(collector)

    assert [e["Id"] for e in events] == ["aad-login-ok-0001", "aad-login-fail-0001"]
    (start_url, start_q), = _calls(m, "POST", "/subscriptions/start")
    assert start_q == {"contentType": "Audit.AzureActiveDirectory", "PublisherIdentifier": F.TENANT}
    (_, list_q), = _calls(m, "GET", "/subscriptions/content")
    assert list_q["contentType"] == "Audit.AzureActiveDirectory"
    assert list_q["PublisherIdentifier"] == F.TENANT
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", list_q["startTime"])
    # Toda chamada (inclusive o blob) leva a cota dedicada.
    (_, blob_q), = _calls(m, "GET", "/audit/c1")
    assert blob_q == {"PublisherIdentifier": F.TENANT}

    cursor = collector.ctx.cursor
    assert cursor["subscribed"] is True
    nxt = _parse(cursor["startTime"])
    assert abs((datetime.now(timezone.utc) - nxt) - M._OVERLAP) < timedelta(seconds=30)
    # O blob está na sobreposição: a memória da fronteira o guarda.
    assert cursor["boundary"]["keys"] == ["c1"]
    assert not collector.ctx.hit_cycle_cap


@pytest.mark.asyncio
async def test_already_enabled_subscription_is_success() -> None:
    with aioresponses() as m:
        m.post(_START, status=400, payload={"error": {"code": "AF20024", "message": "already enabled"}})
        m.get(_CONTENT, payload=[])
        async with aiohttp.ClientSession() as session:
            collector = M.Microsoft365AuditExchangeCollector(_ctx(session))
            assert await _collect(collector) == []
    assert collector.ctx.cursor["subscribed"] is True


@pytest.mark.asyncio
async def test_subscribed_cursor_never_calls_start_again() -> None:
    """Chamar ``start`` todo ciclo esbarra na regra de 15 min da API."""
    with aioresponses() as m:
        m.get(_CONTENT, payload=[])
        async with aiohttp.ClientSession() as session:
            collector = M.Microsoft365AuditAadCollector(
                _ctx(session, {"subscribed": True, "startTime": _ago(30)})
            )
            await _collect(collector)
    assert _calls(m, "POST", "/subscriptions/start") == []


@pytest.mark.asyncio
async def test_listing_without_subscription_restarts_it_once() -> None:
    with aioresponses() as m:
        m.get(_CONTENT, status=400, payload={"error": {"code": "AF20022", "message": "no subscription"}})
        m.post(_START, payload={})
        m.get(_CONTENT, payload=[])
        async with aiohttp.ClientSession() as session:
            collector = M.Microsoft365AuditAadCollector(
                _ctx(session, {"subscribed": True, "startTime": _ago(30)})
            )
            await _collect(collector)
    assert len(_calls(m, "POST", "/subscriptions/start")) == 1
    assert len(_calls(m, "GET", "/subscriptions/content")) == 2


# ══ Paginação e hosts ══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_follows_next_page_uri_until_the_last_page() -> None:
    nxt = f"{_FEED}/subscriptions/content?contentType=Audit.AzureActiveDirectory&nextPage=2"
    with aioresponses() as m:
        m.get(re.compile(r"nextPage=2"), payload=[_blob("c2", _ago(40))])
        m.get(_CONTENT, payload=[_blob("c1", _ago(50))], headers={"NextPageUri": nxt})
        m.get(_blob_url("c1"), payload=[F.USER_LOGGED_IN])
        m.get(_blob_url("c2"), payload=[F.ADD_MEMBER_TO_ROLE])
        async with aiohttp.ClientSession() as session:
            collector = M.Microsoft365AuditAadCollector(
                _ctx(session, {"subscribed": True, "startTime": _ago(60)})
            )
            events = await _collect(collector)
    assert [e["Id"] for e in events] == ["aad-login-ok-0001", "aad-role-0001"]
    (_, q2), = _calls(m, "GET", "nextPage=2")
    assert q2["PublisherIdentifier"] == F.TENANT


@pytest.mark.asyncio
async def test_next_page_uri_on_another_host_is_refused() -> None:
    with aioresponses() as m:
        m.get(_CONTENT, payload=[], headers={"NextPageUri": "https://evil.example.net/steal"})
        async with aiohttp.ClientSession() as session:
            collector = M.Microsoft365AuditAadCollector(
                _ctx(session, {"subscribed": True, "startTime": _ago(30)})
            )
            with pytest.raises(RuntimeError, match="NextPageUri"):
                await _collect(collector)
    assert _calls(m, "GET", "evil.example.net") == []


@pytest.mark.asyncio
async def test_content_uri_on_another_host_never_gets_the_bearer(caplog) -> None:
    with aioresponses() as m:
        m.get(_CONTENT, payload=[_blob("evil", _ago(20), host="evil.example.net"), _blob("ok", _ago(20))])
        m.get(re.compile(r"evil\.example\.net"), payload=[F.USER_LOGGED_IN])
        m.get(_blob_url("ok"), payload=[F.RESET_PASSWORD])
        async with aiohttp.ClientSession() as session:
            collector = M.Microsoft365AuditAadCollector(
                _ctx(session, {"subscribed": True, "startTime": _ago(30)})
            )
            with caplog.at_level(logging.WARNING):
                events = await _collect(collector)
    assert [e["Id"] for e in events] == ["aad-reset-0001"]
    assert _calls(m, "GET", "evil.example.net") == []
    assert "contentUri fora de manage.office.com" in caplog.text


# ══ Janelas ════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_catch_up_reads_contiguous_windows_of_at_most_24h() -> None:
    start = datetime.now(timezone.utc) - timedelta(hours=50)
    with aioresponses() as m:
        m.get(_CONTENT, payload=[], repeat=True)
        async with aiohttp.ClientSession() as session:
            collector = M.Microsoft365AuditAadCollector(
                _ctx(session, {"subscribed": True, "startTime": _iso(start)})
            )
            await _collect(collector)

    windows = sorted(
        (_parse(q["startTime"]), _parse(q["endTime"])) for _, q in _calls(m, "GET", "/subscriptions/content")
    )
    assert len(windows) == 3
    assert windows[0][0] == start.replace(microsecond=0)
    for (s, e) in windows:
        assert timedelta(0) < e - s <= timedelta(hours=24)
    for (_, prev_end), (nxt_start, _) in zip(windows, windows[1:]):
        # Nenhum buraco entre janelas: a seguinte começa antes do fim da anterior.
        assert nxt_start == prev_end - M._OVERLAP
    assert abs(datetime.now(timezone.utc) - windows[-1][1]) < timedelta(seconds=30)


@pytest.mark.asyncio
async def test_cursor_older_than_seven_days_is_clamped_with_a_warning(caplog) -> None:
    with aioresponses() as m:
        m.get(_CONTENT, payload=[], repeat=True)
        async with aiohttp.ClientSession() as session:
            collector = M.Microsoft365AuditAadCollector(
                _ctx(session, {"subscribed": True, "startTime": _ago(9 * 24 * 60)})
            )
            with caplog.at_level(logging.WARNING):
                await _collect(collector)
    first = min(_parse(q["startTime"]) for _, q in _calls(m, "GET", "/subscriptions/content"))
    assert abs((datetime.now(timezone.utc) - first) - M._RETENTION) < timedelta(seconds=30)
    assert "perdido" in caplog.text


@pytest.mark.asyncio
async def test_overlap_blob_is_not_downloaded_twice() -> None:
    in_overlap = _ago(3)
    with aioresponses() as m:
        m.get(_CONTENT, payload=[_blob("c1", in_overlap)])
        m.get(_blob_url("c1"), payload=[F.USER_LOGGED_IN])
        async with aiohttp.ClientSession() as session:
            first = M.Microsoft365AuditAadCollector(_ctx(session, {"subscribed": True, "startTime": _ago(30)}))
            await _collect(first)
    cursor = first.ctx.cursor

    with aioresponses() as m:
        m.get(_CONTENT, payload=[_blob("c1", in_overlap), _blob("c2", _ago(1))])
        m.get(_blob_url("c2"), payload=[F.RESET_PASSWORD])
        async with aiohttp.ClientSession() as session:
            second = M.Microsoft365AuditAadCollector(_ctx(session, cursor))
            events = await _collect(second)
    assert [e["Id"] for e in events] == ["aad-reset-0001"]
    assert _calls(m, "GET", "/audit/c1") == []


@pytest.mark.asyncio
async def test_backfill_reads_the_requested_window_and_keeps_the_cursor() -> None:
    cursor = {
        "subscribed": True,
        "startTime": _ago(10),
        "backfill_from_ts": _ago(3 * 24 * 60),
        "backfill_to_ts": _ago(2 * 24 * 60),
    }
    with aioresponses() as m:
        m.get(_CONTENT, payload=[], repeat=True)
        async with aiohttp.ClientSession() as session:
            collector = M.Microsoft365AuditAadCollector(_ctx(session, dict(cursor), bounded=False))
            await _collect(collector)
    (_, q), = _calls(m, "GET", "/subscriptions/content")
    assert abs(_parse(q["startTime"]) - _parse(cursor["backfill_from_ts"])) < timedelta(seconds=2)
    assert collector.ctx.cursor == cursor, "backfill não move o cursor do polling"


# ══ Teto por ciclo ═════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_cap_leaves_a_resumable_cursor_and_signals_backlog(monkeypatch) -> None:
    monkeypatch.setattr(M, "_MAX_REQUESTS_PER_CYCLE", 3)
    start = _ago(60)
    blobs = [_blob(f"b{i}", _ago(50 - i)) for i in range(4)]
    with aioresponses() as m:
        m.get(_CONTENT, payload=blobs)
        for i in range(4):
            m.get(_blob_url(f"b{i}"), payload=[{**F.TEAMS_MEMBER_ADDED, "Id": f"ev-{i}"}])
        async with aiohttp.ClientSession() as session:
            first = M.Microsoft365AuditAadCollector(_ctx(session, {"subscribed": True, "startTime": start}))
            got = await _collect(first)

    assert [e["Id"] for e in got] == ["ev-0", "ev-1"]
    assert first.ctx.hit_cycle_cap
    cursor = first.ctx.cursor
    assert cursor["startTime"] == start and cursor["window_end"] and cursor["done"] == ["b0", "b1"]
    assert cursor["subscribed"] is True
    assert M.Microsoft365AuditAadCollector.watermark_at(cursor) == _parse(start).replace(tzinfo=None)

    monkeypatch.setattr(M, "_MAX_REQUESTS_PER_CYCLE", 300)
    with aioresponses() as m:
        m.get(_CONTENT, payload=blobs)
        # Depois da janela retomada, o catch-up segue até o relógio.
        m.get(_CONTENT, payload=[], repeat=True)
        for i in (2, 3):
            m.get(_blob_url(f"b{i}"), payload=[{**F.TEAMS_MEMBER_ADDED, "Id": f"ev-{i}"}])
        async with aiohttp.ClientSession() as session:
            second = M.Microsoft365AuditAadCollector(_ctx(session, cursor))
            rest = await _collect(second)
    assert [e["Id"] for e in rest] == ["ev-2", "ev-3"]
    resumed = [q for _, q in _calls(m, "GET", "/subscriptions/content") if _parse(q["startTime"]) == _parse(start)]
    assert len(resumed) == 1 and _parse(resumed[0]["endTime"]) == _parse(cursor["window_end"]), (
        "retoma a MESMA janela"
    )
    assert not second.ctx.hit_cycle_cap


@pytest.mark.asyncio
async def test_unbounded_run_ignores_the_cap(monkeypatch) -> None:
    monkeypatch.setattr(M, "_MAX_REQUESTS_PER_CYCLE", 1)
    with aioresponses() as m:
        m.get(_CONTENT, payload=[_blob("b0", _ago(20)), _blob("b1", _ago(19))])
        m.get(_blob_url("b0"), payload=[F.USER_LOGGED_IN])
        m.get(_blob_url("b1"), payload=[F.RESET_PASSWORD])
        async with aiohttp.ClientSession() as session:
            collector = M.Microsoft365AuditAadCollector(
                _ctx(session, {"subscribed": True, "startTime": _ago(30)}, bounded=False)
            )
            assert len(await _collect(collector)) == 2
    assert not collector.ctx.hit_cycle_cap


# ══ Erros ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,payload,headers",
    [
        (429, {"error": {"code": "AF429", "message": "Too many requests"}}, {"Retry-After": "17"}),
        (403, {"error": {"code": "AF429", "message": "throttled"}}, {"Retry-After": "17"}),
    ],
)
async def test_throttling_raises_rate_limited_with_retry_after(status, payload, headers) -> None:
    with aioresponses() as m:
        m.get(_CONTENT, status=status, payload=payload, headers=headers)
        async with aiohttp.ClientSession() as session:
            collector = M.Microsoft365AuditAadCollector(
                _ctx(session, {"subscribed": True, "startTime": _ago(30)})
            )
            with pytest.raises(M.Microsoft365RateLimitedError) as exc:
                await _collect(collector)
    assert exc.value.retry_after == 17


@pytest.mark.asyncio
async def test_401_surfaces_as_client_response_error_without_the_body() -> None:
    with aioresponses() as m:
        m.get(_CONTENT, status=401, payload={"error": {"code": "AF10001", "message": "token segredo-x"}})
        async with aiohttp.ClientSession() as session:
            collector = M.Microsoft365AuditAadCollector(
                _ctx(session, {"subscribed": True, "startTime": _ago(30)})
            )
            with pytest.raises(aiohttp.ClientResponseError) as exc:
                await _collect(collector)
    assert exc.value.status == 401
    assert "AF10001" in exc.value.message and "segredo-x" not in exc.value.message


@pytest.mark.asyncio
async def test_expired_blob_is_skipped_and_the_rest_is_read(caplog) -> None:
    with aioresponses() as m:
        m.get(_CONTENT, payload=[_blob("old", _ago(20)), _blob("new", _ago(19))])
        m.get(_blob_url("old"), status=400, payload={"error": {"code": "AF20051", "message": "expired"}})
        m.get(_blob_url("new"), payload=[F.RESET_PASSWORD])
        async with aiohttp.ClientSession() as session:
            collector = M.Microsoft365AuditAadCollector(
                _ctx(session, {"subscribed": True, "startTime": _ago(30)})
            )
            with caplog.at_level(logging.WARNING):
                events = await _collect(collector)
    assert [e["Id"] for e in events] == ["aad-reset-0001"]
    assert "AF20051" in caplog.text


@pytest.mark.asyncio
async def test_dlp_without_permission_is_a_quiet_no_op(caplog) -> None:
    with aioresponses() as m:
        m.post(_START, status=403, payload={"error": {"code": "AF10002", "message": "no dlp"}})
        async with aiohttp.ClientSession() as session:
            collector = M.Microsoft365DlpCollector(_ctx(session))
            with caplog.at_level(logging.WARNING):
                events = await _collect(collector)
    assert events == []
    assert collector.ctx.cursor is None
    assert "stream desligado" in caplog.text


@pytest.mark.asyncio
async def test_audit_stream_without_permission_is_an_error() -> None:
    with aioresponses() as m:
        m.post(_START, status=403, payload={"error": {"code": "AF10002", "message": "nope"}})
        async with aiohttp.ClientSession() as session:
            collector = M.Microsoft365AuditGeneralCollector(_ctx(session))
            with pytest.raises(aiohttp.ClientResponseError):
                await _collect(collector)


@pytest.mark.asyncio
async def test_dlp_detected_values_never_leave_the_collector() -> None:
    with aioresponses() as m:
        m.get(_CONTENT, payload=[_blob("d1", _ago(20))])
        m.get(_blob_url("d1"), payload=[copy.deepcopy(F.DLP_RULE_MATCH)])
        async with aiohttp.ClientSession() as session:
            collector = M.Microsoft365DlpCollector(_ctx(session, {"subscribed": True, "startTime": _ago(30)}))
            (event,) = await _collect(collector)
    assert "VALOR-SENSIVEL" not in repr(event)
    sensitive = event["PolicyDetails"][0]["Rules"][0]["ConditionsMatched"]["SensitiveInformation"][0]
    assert sensitive["SensitiveInformationDetections"]["DetectedValuesRedacted"] == 2
    assert sensitive["Count"] == 2


# ══ Credencial, registro e helpers ═════════════════════════════════════


def test_tenant_must_be_a_guid() -> None:
    assert M.validated_tenant_guid(F.TENANT.upper()) == F.TENANT
    for bad in ("contoso.onmicrosoft.com", "../x", F.TENANT + "/x", ""):
        with pytest.raises(ValueError):
            M.validated_tenant_guid(bad)


@pytest.mark.parametrize(
    "raw,ip",
    [
        ("203.0.113.5", "203.0.113.5"),
        ("203.0.113.5:51234", "203.0.113.5"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("2001:db8::1", "2001:db8::1"),
        ("", None),
        ("<null>", None),
        (None, None),
    ],
)
def test_client_ip_is_cleaned(raw, ip) -> None:
    assert M.clean_client_ip(raw) == ip


def test_registration_covers_the_five_content_types() -> None:
    plat = get_platform("microsoft_365")
    assert plat is not None and plat.icon_id == "microsoft365"
    # O PUT vendor-neutro só edita estas colunas.
    assert {f.key for f in plat.auth_fields} <= {"tenant_id", "client_id", "client_secret", "base_url"}
    expected = {
        "audit_aad": ("Audit.AzureActiveDirectory", "microsoft_365.audit"),
        "audit_exchange": ("Audit.Exchange", "microsoft_365.audit"),
        "audit_sharepoint": ("Audit.SharePoint", "microsoft_365.audit"),
        "audit_general": ("Audit.General", "microsoft_365.audit"),
        "dlp": ("DLP.All", "microsoft_365.dlp"),
    }
    for stream, (content_type, event_type) in expected.items():
        cls = get("microsoft_365", stream).collector_cls
        assert (cls._CONTENT_TYPE, cls.event_type) == (content_type, event_type)
        assert f"collect:{stream}" in plat.capabilities


def test_watermark_reads_the_window_start() -> None:
    got = M.Microsoft365DlpCollector.watermark_at({"startTime": "2026-07-24T10:00:00Z"})
    assert got == datetime(2026, 7, 24, 10, 0, 0)


# ══ Teste de conexão ═══════════════════════════════════════════════════


_PROBE_CFG = {"tenant_id": F.TENANT, "client_id": F.CLIENT, "client_secret": "s3gredo-de-teste"}


@pytest.mark.asyncio
async def test_probe_reports_each_content_type() -> None:
    with aioresponses() as m:
        m.post(_TOKEN, payload={"access_token": "tok", "expires_in": 3600})
        m.get(_LIST, payload=[
            {"contentType": "Audit.AzureActiveDirectory", "status": "enabled"},
            {"contentType": "Audit.Exchange", "status": "disabled"},
        ])
        result = await M._m365_probe(dict(_PROBE_CFG))
    assert result.ok, result.detail
    assert "audit_aad: assinatura ativa" in result.detail
    assert "audit_exchange: assinatura disabled" in result.detail
    assert "dlp: sem assinatura" in result.detail
    token_call = next(iter(c for (meth, url), cs in m.requests.items() if meth == "POST" for c in cs))
    assert token_call.kwargs["data"]["scope"] == "https://manage.office.com/.default"


@pytest.mark.asyncio
async def test_probe_shows_only_the_aadsts_code() -> None:
    with aioresponses() as m:
        m.post(_TOKEN, status=401, payload={
            "error": "invalid_client",
            "error_description": "AADSTS7000215: Invalid client secret s3gredo-de-teste provided.",
        })
        result = await M._m365_probe(dict(_PROBE_CFG))
    assert not result.ok
    assert "AADSTS7000215" in result.detail and "s3gredo" not in result.detail


@pytest.mark.asyncio
async def test_probe_explains_missing_activity_feed_permission() -> None:
    with aioresponses() as m:
        m.post(_TOKEN, payload={"access_token": "tok", "expires_in": 3600})
        m.get(_LIST, status=403, payload={"error": {"code": "AF10002", "message": "segredo interno"}})
        result = await M._m365_probe(dict(_PROBE_CFG))
    assert not result.ok
    assert "ActivityFeed.Read" in result.detail and "segredo interno" not in result.detail


@pytest.mark.asyncio
async def test_probe_refuses_a_domain_as_tenant() -> None:
    result = await M._m365_probe({**_PROBE_CFG, "tenant_id": "contoso.onmicrosoft.com"})
    assert not result.ok and "GUID" in result.detail


# ══ Mappings ═══════════════════════════════════════════════════════════


def _normalize(event_type: str, raw: Dict[str, Any]) -> Dict[str, Any]:
    event = copy.deepcopy(raw)
    ip = M.clean_client_ip(event.get("ClientIP"))
    if ip:
        event["ClientIPNormalized"] = ip
    compiled = E.compile_rules(load_default_rules("microsoft_365", event_type))
    n = E.apply_compiled(compiled, event).output["normalized"]
    r = V.validate_normalized(n)
    assert r.valid, (r.reason, n)
    return n


def _ident(n: Dict[str, Any]) -> Tuple[int, int, int]:
    return n["class_uid"], n["activity_id"], n["type_uid"]


def test_user_logged_in_is_a_successful_logon() -> None:
    n = _normalize("microsoft_365.audit", F.USER_LOGGED_IN)
    assert _ident(n) == (3002, 1, 300201)
    assert (n["category_uid"], n["status_id"], n["status"]) == (3, 1, "Success")
    assert n["user"]["name"] == "ana.silva@example.com"
    assert n["actor"]["user"]["email_addr"] == "ana.silva@example.com"
    assert n["src_endpoint"]["ip"] == "203.0.113.10"
    assert n["http_request"]["user_agent"].startswith("Mozilla/5.0")
    assert n["time"] == 1793106000000
    assert n["metadata"]["uid"] == "aad-login-ok-0001"
    assert n["metadata"]["product"]["feature"]["name"] == "AzureActiveDirectory"
    assert n["cloud"]["org"]["uid"] == F.ORG


def test_user_login_failed_is_a_failure_even_when_the_sts_says_success() -> None:
    n = _normalize("microsoft_365.audit", F.USER_LOGIN_FAILED)
    assert _ident(n) == (3002, 1, 300201)
    assert (n["status_id"], n["status"], n["severity_id"]) == (2, "Failure", 2)
    assert n["status_detail"] == "InvalidUserNameOrPassword"
    assert n["src_endpoint"]["ip"] == "198.51.100.77"


def test_role_grant_is_an_account_change_attach_policy() -> None:
    n = _normalize("microsoft_365.audit", F.ADD_MEMBER_TO_ROLE)
    assert _ident(n) == (3001, 7, 300107)
    assert n["user"]["name"] == "bruno.costa@example.com", "alvo = conta que ganhou o papel"
    assert n["actor"]["user"]["type_id"] == 2
    assert n["severity_id"] == 2
    assert "src_endpoint" not in n or not n["src_endpoint"].get("ip")


def test_password_reset_is_an_account_change() -> None:
    n = _normalize("microsoft_365.audit", F.RESET_PASSWORD)
    assert _ident(n) == (3001, 4, 300104)


def test_file_download_is_a_read_api_activity_with_ipv6() -> None:
    n = _normalize("microsoft_365.audit", F.FILE_DOWNLOADED)
    assert _ident(n) == (6003, 2, 600302)
    assert n["src_endpoint"]["ip"] == "2001:db8::10"
    assert n["api"] == {"operation": "FileDownloaded", "service": {"name": "OneDrive"}}
    assert n["resources"][0]["name"] == "plano.xlsx"


def test_inbox_rule_is_a_create_api_activity() -> None:
    n = _normalize("microsoft_365.audit", F.NEW_INBOX_RULE)
    assert _ident(n) == (6003, 1, 600301)
    assert (n["status_id"], n["src_endpoint"]["ip"]) == (1, "203.0.113.10")


@pytest.mark.parametrize(
    "operation,ident",
    [
        ("MailItemsAccessed", (6003, 2, 600302)),
        ("Set-Mailbox", (6003, 3, 600303)),
        ("HardDelete", (6003, 4, 600304)),
        ("Remove-MailboxPermission", (6003, 4, 600304)),
        ("AnonymousLinkCreated", (6003, 1, 600301)),
        ("Add service principal credentials.", (6003, 1, 600301)),
        ("Send", (6003, 99, 600399)),
        ("Delete user.", (3001, 6, 300106)),
        ("Disable account.", (3001, 5, 300105)),
    ],
)
def test_operation_verbs(operation, ident) -> None:
    assert _ident(_normalize("microsoft_365.audit", {**F.TEAMS_MEMBER_ADDED, "Operation": operation})) == ident


def test_dlp_match_is_a_detection_finding_without_sensitive_values() -> None:
    event = copy.deepcopy(F.DLP_RULE_MATCH)
    M._redact_detected_values(event)
    n = _normalize("microsoft_365.dlp", event)
    assert _ident(n) == (2004, 1, 200401)
    assert (n["severity_id"], n["severity"]) == (4, "High")
    assert n["finding_info"]["title"] == "Dados de cartão (PCI)"
    assert n["finding_info"]["desc"] == "Muitos números de cartão"
    assert n["resources"][0]["uid"].endswith("faturas.xlsx")
    assert "VALOR-SENSIVEL" not in repr(n)


@pytest.mark.parametrize("event_type", ["microsoft_365.audit", "microsoft_365.dlp"])
def test_mapping_survives_a_record_with_nothing_in_it(event_type) -> None:
    """Registro mínimo (só o tempo e o Id) não pode quarentenar por expressão que levanta."""
    raw = {"CreationTime": "2026-10-27T13:00:00", "Id": "x"}
    compiled = E.compile_rules(load_default_rules("microsoft_365", event_type))
    n = E.apply_compiled(compiled, raw).output["normalized"]
    assert V.validate_normalized(n).valid


@pytest.mark.parametrize(
    "operation,param",
    [
        ("New-InboxRule", "ForwardTo"),
        ("Set-InboxRule", "RedirectTo"),
        ("New-InboxRule", "ForwardAsAttachmentTo"),
        ("Set-Mailbox", "ForwardingSmtpAddress"),
    ],
)
def test_mail_forwarding_target_is_normalized(operation: str, param: str) -> None:
    """O destino do encaminhamento é o sinal de BEC: precisa estar fora do bruto."""
    raw = dict(F.NEW_INBOX_RULE, Operation=operation,
               Parameters=[{"Name": "Name", "Value": "x"}, {"Name": param, "Value": "externo@example.net"}])
    n = _normalize("microsoft_365.audit", raw)
    assert n["unmapped"]["m365_forward_to"] == "externo@example.net"
    assert {"name": "forward_to", "type": "Email Address", "type_id": 5, "value": "externo@example.net"} in n["observables"]
    assert n["api"]["request"]["data"] == raw["Parameters"]


def test_cmdlet_without_forwarding_has_no_forward_target() -> None:
    raw = dict(F.NEW_INBOX_RULE, Parameters=[{"Name": "DeleteMessage", "Value": "True"}])
    n = _normalize("microsoft_365.audit", raw)
    assert "m365_forward_to" not in (n.get("unmapped") or {})
    assert n["api"]["request"]["data"] == [{"Name": "DeleteMessage", "Value": "True"}]


@pytest.mark.parametrize("record", ["FILE_DOWNLOADED", "MAIL_ITEMS_ACCESSED", "TEAMS_MEMBER_ADDED"])
def test_api_activity_has_no_empty_target_user(record: str) -> None:
    """Fora de logon/conta não há usuário-alvo: nada de ``user: {name: null}``."""
    assert "user" not in _normalize("microsoft_365.audit", getattr(F, record))
