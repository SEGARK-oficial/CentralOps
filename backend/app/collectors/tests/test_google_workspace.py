"""Google Workspace — Reports API, Alert Center, credencial, probe e mappings.

Os riscos que estes testes travam:

- **perder evento sem barulho** — janela que não sobrepõe o atraso de publicação
  da aplicação, teto que não deixa cursor resumível, uma aplicação sem acesso que
  derruba as outras, ``pageToken`` morto que trava o feed;
- **gastar a cota errada** — filtro (``eventName``/``filters``) na Reports API
  cai na cota de 250/min;
- **entregar o token do admin a outro host** — redirect seguido, URL da resposta;
- **vazar o segredo pela UI** — o probe devolve dica, nunca o texto do Google.

Tudo afirmado pelas chamadas que os dublês registram, nunca pelo texto do fonte.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock

import aiohttp
import pytest

from . import _google_workspace_fixtures as F
from ._aiohttp_mock import aioresponses
from ..base import CollectorContext
from ..normalize import engine as E
from ..normalize.defaults import load_default_rules
from ..normalize.ocsf import validator as V
from ..registry import get, get_platform
from ..vendors import google_workspace as W

_SA = {
    "type": "service_account",
    "client_email": "coletor@projeto-exemplo.iam.gserviceaccount.com",
    # Só precisa ser não-vazia: o mint é mockado. Um PEM aqui, mesmo falso,
    # reprova o openness-gate (detector de chave privada commitada).
    "private_key": "chave-falsa-de-teste",
    "token_uri": "https://oauth2.googleapis.com/token",
}


def _reports(app: str) -> re.Pattern:
    return re.compile(rf"^https://admin\.googleapis\.com/admin/reports/v1/activity/users/all/applications/{app}\?")


_ALERTS = re.compile(r"^https://alertcenter\.googleapis\.com/v1beta1/alerts\?")


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


def _ctx(session, cursor: Optional[Dict[str, Any]] = None, *, bounded: bool = True) -> CollectorContext:
    return CollectorContext(
        integration_id=55,
        organization_id=3,
        platform="google_workspace",
        headers={"Authorization": "Bearer ya29.teste"},
        session=session,
        cursor=cursor,
        domain_limiter=_NoopDomainLimiter(),
        rate_limiter=_NoopRateLimiter(),
        redis=MagicMock(),
        bounded_per_cycle=bounded,
    )


@pytest.fixture
def settings(monkeypatch):
    cfg = {"client_secret": json.dumps(_SA), "client_id": F.ADMIN, "tenant_id": "", "base_url": "login,admin"}
    monkeypatch.setattr(W, "_load_settings", lambda integration_id: cfg)
    return cfg


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _ago(minutes: float) -> str:
    return _iso(datetime.now(timezone.utc) - timedelta(minutes=minutes))


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _calls(m, host: str) -> List[Tuple[str, Dict[str, str], Dict[str, Any]]]:
    out = []
    for (method, url), calls in m.requests.items():
        if method == "GET" and url.host == host:
            out.extend((url.path, dict(url.query), c.kwargs) for c in calls)
    return out


async def _collect(collector) -> List[Dict[str, Any]]:
    return [ev async for ev in collector.collect()]


# ══ Configuração ═══════════════════════════════════════════════════════


def test_only_a_service_account_key_can_impersonate() -> None:
    assert W.parse_service_account(json.dumps(_SA))["client_email"].endswith("gserviceaccount.com")
    wif = {
        "type": "external_account",
        "audience": "//iam.googleapis.com/x",
        "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
        "token_url": "https://sts.googleapis.com/v1/token",
        "credential_source": {"url": "http://169.254.169.254/metadata/identity/oauth2/token"},
    }
    with pytest.raises(W.WorkspaceConfigError, match="service account"):
        W.parse_service_account(json.dumps(wif))
    with pytest.raises(W.WorkspaceConfigError, match="token_uri"):
        W.parse_service_account(json.dumps({**_SA, "token_uri": "https://coletor.example.org/token"}))
    with pytest.raises(W.WorkspaceConfigError, match="JSON"):
        W.parse_service_account("{nao json")


@pytest.mark.parametrize("email", ["", "admin", "admin@", "a b@example.com", "admin@example"])
def test_admin_email_is_validated(email) -> None:
    with pytest.raises(W.WorkspaceConfigError):
        W.validated_admin_email(email)


def test_application_list_defaults_and_rejects_unknown_names() -> None:
    assert W.parse_applications("") == W.DEFAULT_APPLICATIONS
    assert "drive" not in W.DEFAULT_APPLICATIONS and "gmail" not in W.DEFAULT_APPLICATIONS
    assert W.parse_applications(" Login, drive;login token ") == ("login", "drive", "token")
    # Vira PATH da URL: nada fora da lista da Reports API passa.
    for bad in ("../users", "login/../../x", "nao_existe"):
        with pytest.raises(W.WorkspaceConfigError, match="desconhecida"):
            W.parse_applications(bad)


def test_customer_id_is_optional_and_validated() -> None:
    assert W.validated_customer("") is None and W.validated_customer("my_customer") is None
    assert W.validated_customer(F.CUSTOMER) == F.CUSTOMER
    with pytest.raises(W.WorkspaceConfigError):
        W.validated_customer("C01/../x")


def test_form_only_uses_columns_the_put_can_edit() -> None:
    """O PUT vendor-neutro só edita estas colunas: campo fora delas seria
    criável e nunca mais editável (gotcha do CloudTrail)."""
    plat = get_platform("google_workspace")
    assert {f.key for f in plat.auth_fields} <= {"tenant_id", "client_id", "client_secret", "base_url"}
    assert plat.icon_id == "googleworkspace"
    assert get("google_workspace", "activity").collector_cls is W.GoogleWorkspaceActivityCollector
    assert get("google_workspace", "alerts").collector_cls is W.GoogleWorkspaceAlertsCollector


# ══ Activity ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_reads_each_application_with_a_pure_time_window(settings) -> None:
    login = F.activity(
        "login",
        F.event("login_success", "login", login_type="google_password"),
        F.event("login_verification", "login", login_challenge_status="Challenge Passed"),
        time=_ago(5),
    )
    admin = F.activity("admin", F.event("CREATE_USER", "USER_SETTINGS", USER_EMAIL="novo@example.com"), time=_ago(8))
    with aioresponses() as m:
        m.get(_reports("login"), payload=F.page(login))
        m.get(_reports("admin"), payload=F.page(admin))
        async with aiohttp.ClientSession() as session:
            collector = W.GoogleWorkspaceActivityCollector(_ctx(session))
            events = await _collect(collector)

    # Fan-out: um registro por item de events[], parâmetros achatados.
    assert [(e["id"]["applicationName"], e["event"]["name"], e["_event_index"]) for e in events] == [
        ("login", "login_success", 0), ("login", "login_verification", 1), ("admin", "CREATE_USER", 0),
    ]
    assert events[1]["event"]["params"] == {"login_challenge_status": "Challenge Passed"}
    assert "events" not in events[0]
    assert len({collector.extract_message_id(e) for e in events}) == 3

    calls = _calls(m, "admin.googleapis.com")
    assert [p.rsplit("/", 1)[1] for p, _, _ in calls] == ["login", "admin"]
    for _, query, kwargs in calls:
        # Só janela: qualquer filtro cairia na cota de 250 consultas/min.
        assert set(query) == {"startTime", "endTime", "maxResults"}
        assert kwargs["allow_redirects"] is False
    # As duas aplicações leem a MESMA janela no cold start.
    assert calls[0][1]["endTime"] == calls[1][1]["endTime"]

    apps = collector.ctx.cursor["apps"]
    for app in ("login", "admin"):
        nxt = _dt(apps[app]["start"])
        assert abs((datetime.now(timezone.utc) - nxt) - W._DEFAULT_LATE_ARRIVAL) < timedelta(seconds=30)
    assert collector.ctx.hit_cycle_cap is False


@pytest.mark.asyncio
async def test_slow_applications_overlap_by_their_publication_lag(settings) -> None:
    settings["base_url"] = "token"
    with aioresponses() as m:
        m.get(_reports("token"), payload=F.page())
        async with aiohttp.ClientSession() as session:
            start = _ago(600)
            collector = W.GoogleWorkspaceActivityCollector(_ctx(session, {"apps": {"token": {"start": start}}}))
            await _collect(collector)
    nxt = _dt(collector.ctx.cursor["apps"]["token"]["start"])
    # token/OAuth publica em horas: a próxima janela recomeça 4 h antes do fim.
    assert abs((datetime.now(timezone.utc) - nxt) - timedelta(hours=4)) < timedelta(seconds=30)


@pytest.mark.asyncio
async def test_overlap_never_reemits_what_a_previous_cycle_sent(settings) -> None:
    settings["base_url"] = "login"
    dup = F.activity("login", F.event("login_success", "login"), time=_ago(3), unique="-1")
    late = F.activity("login", F.event("login_failure", "login"), time=_ago(4), unique="-2")
    with aioresponses() as m:
        m.get(_reports("login"), payload=F.page(dup))
        m.get(_reports("login"), payload=F.page(late, dup))
        async with aiohttp.ClientSession() as session:
            first = W.GoogleWorkspaceActivityCollector(_ctx(session))
            assert [e["event"]["name"] for e in await _collect(first)] == ["login_success"]
            second = W.GoogleWorkspaceActivityCollector(_ctx(session, first.ctx.cursor))
            assert [e["event"]["name"] for e in await _collect(second)] == ["login_failure"]


@pytest.mark.asyncio
async def test_follows_only_the_page_token(settings) -> None:
    settings["base_url"] = "login"
    a = F.activity("login", F.event("login_success", "login"), time=_ago(10))
    b = F.activity("login", F.event("logout", "logout"), time=_ago(9))
    with aioresponses() as m:
        m.get(_reports("login"), payload=F.page(a, token="pag-2"))
        m.get(_reports("login"), payload=F.page(b))
        async with aiohttp.ClientSession() as session:
            events = await _collect(W.GoogleWorkspaceActivityCollector(_ctx(session)))
    assert [e["event"]["name"] for e in events] == ["login_success", "logout"]
    queries = [q for _, q, _ in _calls(m, "admin.googleapis.com")]
    assert "pageToken" not in queries[0] and queries[1]["pageToken"] == "pag-2"
    assert queries[0]["startTime"] == queries[1]["startTime"]
    assert queries[0]["endTime"] == queries[1]["endTime"]


@pytest.mark.asyncio
async def test_cap_keeps_the_window_and_resumes_on_the_next_cycle(settings, monkeypatch) -> None:
    monkeypatch.setattr(W, "_MAX_PAGES_PER_CYCLE", 1)
    start = _ago(60)
    p1 = F.activity("login", F.event("login_success", "login"), time=_ago(50))
    p2 = F.activity("login", F.event("logout", "logout"), time=_ago(49))
    adm = F.activity("admin", F.event("DELETE_USER", "USER_SETTINGS"), time=_ago(40))
    with aioresponses() as m:
        m.get(_reports("login"), payload=F.page(p1, token="pag-2"))
        m.get(_reports("login"), payload=F.page(p2))
        m.get(_reports("admin"), payload=F.page(adm))
        async with aiohttp.ClientSession() as session:
            cursor = {"apps": {"login": {"start": start}, "admin": {"start": _ago(30)}}}
            first = W.GoogleWorkspaceActivityCollector(_ctx(session, cursor))
            assert [e["event"]["name"] for e in await _collect(first)] == ["login_success"]
            assert first.ctx.hit_cycle_cap is True
            capped = first.ctx.cursor["apps"]
            assert capped["login"]["start"] == start, "no teto a janela NÃO anda"
            assert capped["login"]["pageToken"] == "pag-2"
            assert capped["admin"] == {"start": cursor["apps"]["admin"]["start"]}
            # Watermark honesto: a aplicação mais atrasada.
            assert W.GoogleWorkspaceActivityCollector.watermark_at(first.ctx.cursor) == _dt(start).replace(tzinfo=None)

            window_end = capped["login"]["window_end"]
            second = W.GoogleWorkspaceActivityCollector(_ctx(session, first.ctx.cursor))
            assert [e["event"]["name"] for e in await _collect(second)] == ["logout"]
            assert second.ctx.hit_cycle_cap is True  # admin ficou para o próximo

            # A aplicação mais atrasada vai primeiro: admin (30 min) antes de login
            # (20 min) — sem isso, uma aplicação volumosa que sempre bate o teto
            # deixaria as do fim da lista sem nunca serem lidas.
            third = W.GoogleWorkspaceActivityCollector(_ctx(session, second.ctx.cursor))
            assert [e["event"]["name"] for e in await _collect(third)] == ["DELETE_USER"]
            assert third.ctx.hit_cycle_cap is True
            assert "window_end" not in third.ctx.cursor["apps"]["admin"]

    login_q = [q for p, q, _ in _calls(m, "admin.googleapis.com") if p.endswith("/login")]
    assert login_q[1] == {**login_q[0], "pageToken": "pag-2"}
    assert login_q[1]["endTime"] == window_end, "a retomada usa o fim de janela GRAVADO"


@pytest.mark.asyncio
async def test_unbounded_run_drains_without_claiming_backlog(settings, monkeypatch) -> None:
    monkeypatch.setattr(W, "_MAX_PAGES_PER_CYCLE", 1)
    settings["base_url"] = "login"
    with aioresponses() as m:
        m.get(_reports("login"), payload=F.page(F.activity("login", F.event("login_success", "login"), time=_ago(9)), token="t"))
        m.get(_reports("login"), payload=F.page(F.activity("login", F.event("logout", "logout"), time=_ago(8))))
        async with aiohttp.ClientSession() as session:
            collector = W.GoogleWorkspaceActivityCollector(_ctx(session, bounded=False))
            assert len(await _collect(collector)) == 2
    assert collector.ctx.hit_cycle_cap is False


@pytest.mark.asyncio
async def test_forbidden_application_is_skipped_and_the_others_continue(settings) -> None:
    with aioresponses() as m:
        m.get(_reports("login"), status=403, payload={"error": {"code": 403}})
        m.get(_reports("admin"), payload=F.page(F.activity("admin", F.event("CREATE_USER", "USER_SETTINGS"), time=_ago(5))))
        async with aiohttp.ClientSession() as session:
            start = _ago(90)
            collector = W.GoogleWorkspaceActivityCollector(_ctx(session, {"apps": {"login": {"start": start}}}))
            events = await _collect(collector)
    assert [e["event"]["name"] for e in events] == ["CREATE_USER"]
    apps = collector.ctx.cursor["apps"]
    assert apps["login"]["start"] == start and apps["login"]["forbidden"] is True
    # Sem acesso não é atraso: a aplicação proibida sai do watermark.
    wm = W.GoogleWorkspaceActivityCollector.watermark_at(collector.ctx.cursor)
    assert wm == _dt(apps["admin"]["start"]).replace(tzinfo=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 503])
async def test_quota_is_a_rate_limit_with_retry_after(settings, status) -> None:
    settings["base_url"] = "login"
    with aioresponses() as m:
        m.get(_reports("login"), status=status, headers={"Retry-After": "42"}, payload={})
        async with aiohttp.ClientSession() as session:
            with pytest.raises(W.GoogleWorkspaceRateLimitedError) as exc:
                await _collect(W.GoogleWorkspaceActivityCollector(_ctx(session)))
    assert exc.value.retry_after == 42


@pytest.mark.asyncio
async def test_expired_token_surfaces_as_auth_error(settings) -> None:
    settings["base_url"] = "login"
    with aioresponses() as m:
        m.get(_reports("login"), status=401, payload={})
        async with aiohttp.ClientSession() as session:
            with pytest.raises(aiohttp.ClientResponseError) as exc:
                await _collect(W.GoogleWorkspaceActivityCollector(_ctx(session)))
    assert exc.value.status == 401


@pytest.mark.asyncio
async def test_redirect_is_never_followed(settings) -> None:
    settings["base_url"] = "login"
    with aioresponses() as m:
        m.get(_reports("login"), status=302, headers={"Location": "https://coletor.example.org/x"}, payload={})
        async with aiohttp.ClientSession() as session:
            with pytest.raises(aiohttp.ClientResponseError) as exc:
                await _collect(W.GoogleWorkspaceActivityCollector(_ctx(session)))
    assert exc.value.status == 302
    assert all(u.host == "admin.googleapis.com" for _, u in m.requests)


@pytest.mark.asyncio
async def test_rejected_page_token_restarts_the_window(settings) -> None:
    settings["base_url"] = "login"
    start, end = _ago(60), _ago(10)
    ev = F.activity("login", F.event("login_success", "login"), time=_ago(30))
    with aioresponses() as m:
        m.get(_reports("login"), status=400, payload={"error": {"code": 400}})
        m.get(_reports("login"), payload=F.page(ev))
        async with aiohttp.ClientSession() as session:
            cursor = {"apps": {"login": {"start": start, "window_end": end, "pageToken": "morto"}}}
            collector = W.GoogleWorkspaceActivityCollector(_ctx(session, cursor))
            assert len(await _collect(collector)) == 1
    queries = [q for _, q, _ in _calls(m, "admin.googleapis.com")]
    assert queries[0]["pageToken"] == "morto" and "pageToken" not in queries[1]
    assert queries[1]["startTime"] == start and queries[1]["endTime"] == end


@pytest.mark.asyncio
async def test_first_request_4xx_is_a_real_error(settings) -> None:
    """Sem token em jogo, 400 é configuração errada — não pode virar ciclo vazio."""
    settings["base_url"] = "login"
    with aioresponses() as m:
        m.get(_reports("login"), status=400, payload={})
        async with aiohttp.ClientSession() as session:
            with pytest.raises(aiohttp.ClientResponseError):
                await _collect(W.GoogleWorkspaceActivityCollector(_ctx(session)))


@pytest.mark.asyncio
async def test_customer_id_goes_as_query_parameter(settings) -> None:
    settings.update(base_url="login", tenant_id=F.CUSTOMER)
    with aioresponses() as m:
        m.get(_reports("login"), payload=F.page())
        async with aiohttp.ClientSession() as session:
            await _collect(W.GoogleWorkspaceActivityCollector(_ctx(session)))
    assert _calls(m, "admin.googleapis.com")[0][1]["customerId"] == F.CUSTOMER


@pytest.mark.asyncio
async def test_cursor_older_than_retention_resumes_at_the_limit(settings) -> None:
    settings["base_url"] = "login"
    with aioresponses() as m:
        m.get(_reports("login"), payload=F.page())
        async with aiohttp.ClientSession() as session:
            old = _iso(datetime.now(timezone.utc) - timedelta(days=400))
            await _collect(W.GoogleWorkspaceActivityCollector(_ctx(session, {"apps": {"login": {"start": old}}})))
    sent = _dt(_calls(m, "admin.googleapis.com")[0][1]["startTime"])
    assert abs((datetime.now(timezone.utc) - sent) - W._RETENTION) < timedelta(seconds=30)


@pytest.mark.asyncio
async def test_backfill_reads_the_requested_window_and_keeps_the_cursor(settings) -> None:
    settings["base_url"] = "login,admin"
    frm, to = _ago(3000), _ago(2000)
    with aioresponses() as m:
        m.get(_reports("login"), payload=F.page(F.activity("login", F.event("login_success", "login"), time=_ago(2500))))
        m.get(_reports("admin"), payload=F.page())
        async with aiohttp.ClientSession() as session:
            cursor = {"backfill_from_ts": frm, "backfill_to_ts": to}
            collector = W.GoogleWorkspaceActivityCollector(_ctx(session, cursor, bounded=False))
            assert len(await _collect(collector)) == 1
            assert collector.ctx.cursor == cursor
    for _, q, _ in _calls(m, "admin.googleapis.com"):
        assert (q["startTime"], q["endTime"]) == (frm, to)


def test_watermark_ignores_cursors_without_apps() -> None:
    assert W.GoogleWorkspaceActivityCollector.watermark_at({"qualquer": "coisa"}) is None
    assert W.GoogleWorkspaceActivityCollector.watermark_at(None) is None


def test_message_id_separates_events_of_the_same_activity() -> None:
    item = F.activity("login", F.event("login_success", "login"), F.event("login_verification", "login"),
                      time="2026-10-27T12:00:00Z", unique="-9")
    ids = [W.activity_message_id(r) for r in W.explode_activity(item)]
    assert ids == ["login:-9:2026-10-27T12:00:00Z:0", "login:-9:2026-10-27T12:00:00Z:1"]


def test_params_flattening_is_bounded() -> None:
    params = [{"name": f"p{i}", "value": str(i)} for i in range(500)]
    flat = W.flatten_params(params)
    assert len(flat) == W._MAX_PARAMS
    assert W.flatten_params([{"name": "n", "intValue": "7"}, {"name": "b", "boolValue": True},
                             {"name": "m", "multiValue": ["a", "b"]}]) == {"n": "7", "b": True, "m": ["a", "b"]}


# ══ Alert Center ═══════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_alerts_window_by_create_time_with_overlap(settings) -> None:
    settings["tenant_id"] = F.CUSTOMER
    a1 = F.alert("a-1", _ago(5))
    with aioresponses() as m:
        m.get(_ALERTS, payload=F.alerts_page(a1))
        m.get(_ALERTS, payload=F.alerts_page(F.alert("a-2", _ago(4)), a1))
        async with aiohttp.ClientSession() as session:
            first = W.GoogleWorkspaceAlertsCollector(_ctx(session))
            assert [a["alertId"] for a in await _collect(first)] == ["a-1"]
            second = W.GoogleWorkspaceAlertsCollector(_ctx(session, first.ctx.cursor))
            assert [a["alertId"] for a in await _collect(second)] == ["a-2"]

    q = _calls(m, "alertcenter.googleapis.com")[0][1]
    assert re.fullmatch(r'createTime >= "[^"]+Z" AND createTime < "[^"]+Z"', q["filter"])
    assert q["orderBy"] == "createTime asc"
    assert q["customerId"] == F.CUSTOMER[1:], "o Alert Center quer o Customer ID sem o C"
    nxt = _dt(first.ctx.cursor["createTime"])
    assert abs((datetime.now(timezone.utc) - nxt) - W._ALERT_LATE_ARRIVAL) < timedelta(seconds=30)


@pytest.mark.asyncio
async def test_alerts_cap_resumes_with_the_same_filter(settings, monkeypatch) -> None:
    monkeypatch.setattr(W, "_MAX_ALERT_PAGES_PER_CYCLE", 1)
    start = _ago(120)
    with aioresponses() as m:
        m.get(_ALERTS, payload=F.alerts_page(F.alert("a-1", _ago(100)), token="pag-2"))
        m.get(_ALERTS, payload=F.alerts_page(F.alert("a-2", _ago(90))))
        async with aiohttp.ClientSession() as session:
            first = W.GoogleWorkspaceAlertsCollector(_ctx(session, {"createTime": start}))
            assert [a["alertId"] for a in await _collect(first)] == ["a-1"]
            assert first.ctx.hit_cycle_cap is True
            assert first.ctx.cursor["createTime"] == start and first.ctx.cursor["pageToken"] == "pag-2"
            assert W.GoogleWorkspaceAlertsCollector.watermark_at(first.ctx.cursor) == _dt(start).replace(tzinfo=None)
            second = W.GoogleWorkspaceAlertsCollector(_ctx(session, first.ctx.cursor))
            assert [a["alertId"] for a in await _collect(second)] == ["a-2"]
            assert second.ctx.hit_cycle_cap is False
    queries = [q for _, q, _ in _calls(m, "alertcenter.googleapis.com")]
    assert queries[1] == {**queries[0], "pageToken": "pag-2"}


@pytest.mark.asyncio
async def test_alerts_without_access_keep_the_cursor(settings) -> None:
    start = _ago(30)
    with aioresponses() as m:
        m.get(_ALERTS, status=403, payload={})
        async with aiohttp.ClientSession() as session:
            collector = W.GoogleWorkspaceAlertsCollector(_ctx(session, {"createTime": start}))
            assert await _collect(collector) == []
    assert collector.ctx.cursor == {"createTime": start}


@pytest.mark.asyncio
async def test_alerts_backfill_uses_the_requested_window(settings) -> None:
    frm, to = _ago(5000), _ago(4000)
    with aioresponses() as m:
        m.get(_ALERTS, payload=F.alerts_page(F.alert("a-9", _ago(4500))))
        async with aiohttp.ClientSession() as session:
            cursor = {"backfill_from_ts": frm, "backfill_to_ts": to}
            collector = W.GoogleWorkspaceAlertsCollector(_ctx(session, cursor, bounded=False))
            assert [a["alertId"] for a in await _collect(collector)] == ["a-9"]
            assert collector.ctx.cursor == cursor
    assert _calls(m, "alertcenter.googleapis.com")[0][1]["filter"] == f'createTime >= "{frm}" AND createTime < "{to}"'


# ══ Token ══════════════════════════════════════════════════════════════


def test_mint_token_impersonates_the_admin_through_the_guarded_transport(monkeypatch) -> None:
    pytest.importorskip("google.auth")
    from google.oauth2 import service_account

    seen: Dict[str, Any] = {}

    class _Creds:
        token = "ya29.novo"
        expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=30)

        def refresh(self, request):
            seen["request"] = request

    def _from_info(info, scopes=None, subject=None):
        seen.update(info=info, scopes=scopes, subject=subject)
        return _Creds()

    monkeypatch.setattr(service_account.Credentials, "from_service_account_info", staticmethod(_from_info))
    token, expires_in = W._mint_token(_SA, F.ADMIN)
    assert token == "ya29.novo" and 1700 < expires_in <= 1800
    assert seen["subject"] == F.ADMIN
    assert seen["scopes"] == [
        "https://www.googleapis.com/auth/admin.reports.audit.readonly",
        "https://www.googleapis.com/auth/apps.alerts",
    ]
    assert type(seen["request"]).__name__ == "_Request", "o token tem de passar pelo transporte guardado"


@pytest.mark.asyncio
async def test_refresher_returns_one_token_for_both_streams(monkeypatch, settings) -> None:
    monkeypatch.setattr(W, "_mint_token", lambda info, subject: (f"ya29.{subject}", 3599))
    assert await W._workspace_refresher(55) == {"access_token": f"ya29.{F.ADMIN}", "expires_in": 3599}
    assert get("google_workspace", "activity").refresh_fn is get("google_workspace", "alerts").refresh_fn


@pytest.mark.asyncio
async def test_refresher_does_not_echo_the_google_error(monkeypatch, settings) -> None:
    def _boom(info, subject):
        raise ValueError("unauthorized_client: resposta com dado sensível")

    monkeypatch.setattr(W, "_mint_token", _boom)
    with pytest.raises(RuntimeError) as exc:
        await W._workspace_refresher(55)
    assert "sensível" not in str(exc.value) and "delegação" in str(exc.value)


@pytest.mark.asyncio
async def test_refresher_refuses_bad_config_before_any_network(monkeypatch, settings) -> None:
    calls: List[Any] = []
    monkeypatch.setattr(W, "_mint_token", lambda *a: calls.append(a))
    settings["client_id"] = "nao-e-email"
    with pytest.raises(W.WorkspaceConfigError):
        await W._workspace_refresher(55)
    assert calls == []


# ══ Teste de conexão ═══════════════════════════════════════════════════


def _cfg(**over: Any) -> Dict[str, Any]:
    return {"client_secret": json.dumps(_SA), "client_id": F.ADMIN, "tenant_id": "", "base_url": "", **over}


@pytest.mark.asyncio
async def test_probe_reports_each_stream(monkeypatch) -> None:
    monkeypatch.setattr(W, "_mint_token", lambda info, subject: ("ya29.probe", 3600))
    with aioresponses() as m:
        m.get(_reports("login"), payload=F.page())
        m.get(_ALERTS, status=403, payload={})
        result = await W._workspace_probe(_cfg())
    assert result.ok is True
    assert "activity (login): ok" in result.detail
    assert "alerts: sem permissão" in result.detail
    q = _calls(m, "admin.googleapis.com")[0][1]
    assert q == {"maxResults": "1"}


@pytest.mark.asyncio
async def test_probe_fails_when_nothing_is_readable(monkeypatch) -> None:
    monkeypatch.setattr(W, "_mint_token", lambda info, subject: ("ya29.probe", 3600))
    with aioresponses() as m:
        m.get(_reports("login"), status=403, payload={})
        m.get(_ALERTS, status=403, payload={})
        result = await W._workspace_probe(_cfg())
    assert result.ok is False and "Nenhum stream" in result.detail


@pytest.mark.asyncio
async def test_probe_explains_unauthorized_delegation_without_echoing(monkeypatch) -> None:
    def _boom(info, subject):
        raise ValueError("('unauthorized_client: Client is unauthorized ...', {'segredo': 'x'})")

    monkeypatch.setattr(W, "_mint_token", _boom)
    result = await W._workspace_probe(_cfg())
    assert result.ok is False
    assert "delegação em todo o domínio" in result.detail.lower() and "24 h" in result.detail
    assert "segredo" not in result.detail and "Client is unauthorized" not in result.detail


@pytest.mark.asyncio
async def test_probe_validates_input_before_the_network(monkeypatch) -> None:
    calls: List[Any] = []
    monkeypatch.setattr(W, "_mint_token", lambda *a: calls.append(a))
    assert (await W._workspace_probe(_cfg(client_id=""))).ok is False
    bad = await W._workspace_probe(_cfg(base_url="login,nada"))
    assert bad.ok is False and "desconhecida" in bad.detail
    assert (await W._workspace_probe(_cfg(client_secret="{nao json"))).ok is False
    assert calls == []


# ══ Mappings ═══════════════════════════════════════════════════════════


def _normalize(event_type: str, raw: Dict[str, Any]) -> Dict[str, Any]:
    compiled = E.compile_rules(load_default_rules("google_workspace", event_type))
    n = E.apply_compiled(compiled, copy.deepcopy(raw)).output["normalized"]
    r = V.validate_normalized(n)
    assert r.valid, (r.reason, n)
    return n


def _ident(n: Dict[str, Any]) -> Tuple[int, int, int]:
    return n["class_uid"], n["activity_id"], n["type_uid"]


@pytest.mark.parametrize(
    "record,ident,status_id,severity_id",
    [
        (F.LOGIN_SUCCESS, (3002, 1, 300201), 1, 1),
        (F.LOGIN_FAILURE, (3002, 1, 300201), 2, 2),
        (F.SUSPICIOUS_LOGIN, (3002, 1, 300201), 0, 4),
        (F.CHALLENGE_FAILED, (3002, 1, 300201), 2, 1),
        (F.LOGOUT, (3002, 2, 300202), 1, 1),
        (F.GRANT_ADMIN, (3001, 7, 300107), 1, 3),
        (F.SUSPEND_USER, (3001, 5, 300105), 1, 3),
        (F.TWO_SV_DISABLE, (3001, 11, 300111), 1, 3),
        (F.OAUTH_GRANT, (6003, 1, 600301), 1, 1),
        (F.DRIVE_DOWNLOAD, (6003, 2, 600302), 1, 1),
        (F.GROUP_SETTING, (6003, 3, 600303), 1, 1),
    ],
)
def test_activity_classes(record, ident, status_id, severity_id) -> None:
    n = _normalize("google_workspace.activity", record)
    assert _ident(n) == ident
    assert (n["status_id"], n["severity_id"]) == (status_id, severity_id)


def test_login_fields() -> None:
    n = _normalize("google_workspace.activity", F.LOGIN_FAILURE)
    assert n["actor"]["user"] == {"email_addr": "ana.silva@example.com", "uid": "100000000000000000001", "type": "USER"}
    assert n["src_endpoint"]["ip"] == "203.0.113.10"
    assert n["auth_protocol"] == "google_password"
    assert n["status_detail"] == "login_failure_invalid_password"
    assert n["metadata"]["uid"] == "-112" and n["metadata"]["event_code"] == "login_failure"
    assert n["cloud"] == {"provider": "Google Workspace", "org": {"uid": F.CUSTOMER}}
    assert n["time"] == int(datetime(2026, 10, 27, 12, 1, tzinfo=timezone.utc).timestamp() * 1000)


def test_challenge_without_result_stays_unknown() -> None:
    record = F.explode(F.activity("login", F.event("login_challenge", "login"), time="2026-10-27T12:00:00Z"))
    n = _normalize("google_workspace.activity", record)
    assert n["status_id"] == 0


def test_admin_action_targets_the_affected_user() -> None:
    n = _normalize("google_workspace.activity", F.GRANT_ADMIN)
    assert n["user"]["email_addr"] == "bruno.costa@example.com"
    assert n["actor"]["user"]["email_addr"] == F.ADMIN
    values = {o["value"] for o in n["observables"]}
    assert {"bruno.costa@example.com", F.ADMIN, "203.0.113.10"} <= values


def test_oauth_grant_names_the_app() -> None:
    n = _normalize("google_workspace.activity", F.OAUTH_GRANT)
    assert n["actor"]["app_name"] == "App de Teste"
    assert n["actor"]["app_uid"] == "123456789012.apps.googleusercontent.com"


def test_drive_event_carries_the_document() -> None:
    n = _normalize("google_workspace.activity", F.DRIVE_DOWNLOAD)
    assert n["resources"][0]["uid"] == "1AbCdEf" and n["resources"][0]["name"] == "planilha.xlsx"


@pytest.mark.parametrize(
    "name,ident",
    [
        # Nomes do catálogo oficial (admin-user-settings / admin-delegated-admin-settings):
        # o 2SV desligado PELO ADMIN não pode cair no ramo genérico de "TURN_OFF".
        ("TURN_OFF_2_STEP_VERIFICATION", (3001, 11, 300111)),
        ("USER_ENROLLED_IN_TWO_STEP_VERIFICATION", (3001, 10, 300110)),
        ("REVOKE_SECURITY_KEY", (3001, 11, 300111)),
        ("UNASSIGN_ROLE", (3001, 8, 300108)),
    ],
)
def test_admin_console_security_events_are_account_changes(name, ident) -> None:
    record = F.explode(F.activity("admin", F.event(name, "USER_SETTINGS"), time="2026-10-27T12:00:00Z"))
    assert _ident(_normalize("google_workspace.activity", record)) == ident


def test_activity_with_no_event_name_is_unknown_not_quarantined() -> None:
    record = F.explode(F.activity("admin", {"type": "X"}, time="2026-10-27T12:00:00Z"))
    n = _normalize("google_workspace.activity", record)
    assert _ident(n) == (6003, 99, 600399)


def test_alert_is_a_detection_finding() -> None:
    n = _normalize("google_workspace.alert", F.SUSPICIOUS_LOGIN_ALERT)
    assert _ident(n) == (2004, 1, 200401)
    assert (n["severity_id"], n["status_id"]) == (4, 1)
    assert n["finding_info"]["uid"] == "alerta-0001"
    assert n["finding_info"]["title"] == "Suspicious login"
    assert n["finding_info"]["types"] == ["Suspicious login", "Google identity"]
    assert {o["value"] for o in n["observables"]} == {"ana.silva@example.com", "198.51.100.77"}


def test_alert_without_severity_is_unknown_and_closed_is_resolved() -> None:
    n = _normalize("google_workspace.alert", F.LEAKED_PASSWORD_ALERT)
    assert (n["severity_id"], n["status_id"], n["status"]) == (0, 4, "Resolved")


def _login(name: str, **params: Any) -> Dict[str, Any]:
    return _normalize(
        "google_workspace.activity",
        F.explode(F.activity("login", F.event(name, "login", **params), time="2026-10-27T12:00:00Z")),
    )


def test_suspicious_success_stays_a_logon_but_becomes_an_alert() -> None:
    """``is_suspicious``: o Google deixou entrar e marcou — Success, mas High e alerta."""
    n = _login("login_success", login_type="google_password", is_suspicious=True)
    assert _ident(n) == (3002, 1, 300201)
    assert (n["status_id"], n["severity_id"], n["severity"]) == (1, 4, "High")
    assert n["is_alert"] is True


def test_plain_success_is_not_an_alert() -> None:
    n = _normalize("google_workspace.activity", F.LOGIN_SUCCESS)
    assert n["severity_id"] == 1 and "is_alert" not in n


@pytest.mark.parametrize(
    "params,is_mfa",
    [
        ({"login_challenge_method": ["password", "google_prompt"]}, True),
        ({"login_challenge_method": ["password", "security_key"]}, True),
        ({"login_challenge_method": "passkey"}, True),
        ({"login_challenge_method": ["password"]}, False),
        ({"login_challenge_method": ["password", "recaptcha"]}, False),
        ({"is_second_factor": True}, True),
    ],
)
def test_is_mfa_reads_the_challenge_methods(params: Dict[str, Any], is_mfa: bool) -> None:
    n = _login("login_success", **params)
    assert n["is_mfa"] is is_mfa


def test_events_without_challenge_carry_no_mfa_claim() -> None:
    assert "is_mfa" not in _login("logout")
    assert "is_mfa" not in _normalize("google_workspace.activity", F.GRANT_ADMIN)


def test_challenge_methods_are_kept_as_a_list() -> None:
    n = _normalize("google_workspace.activity", F.LOGIN_SUCCESS)
    assert n["unmapped"]["login_challenge_method"] == ["password", "google_prompt"]


def test_oauth_grant_carries_the_scopes() -> None:
    """O escopo é o que separa consentimento legítimo de phishing de consentimento."""
    n = _normalize("google_workspace.activity", F.OAUTH_GRANT)
    assert n["unmapped"]["oauth_scopes"] == ["https://mail.google.com/"]
    assert "oauth_scopes" not in (_login("login_success").get("unmapped") or {})
