"""Cloudflare security events (GraphQL Analytics, ``firewallEventsAdaptive``).

Os riscos que estes testes travam:

- **perder ou repetir evento** — limite inferior inclusivo sem memória de fronteira,
  página cheia que não continua, mais de uma página no mesmo segundo (laço
  infinito ou perda calada), teto por ciclo sem cursor resumível, janela além da
  retenção sem dizer quanto se perdeu, cursor de zona removida/nova;
- **estourar um limite da zona** — campos acima de ``maxNumberOfFields``, janela
  acima de ``maxDuration``, zona com o dataset desligado derrubando as demais;
- **vazar o token** — mensagem de erro, resultado do teste de conexão ou
  ``request_info`` com o segredo ou o header ``Authorization``;
- **falar com o host errado** — redirect seguido.

Tudo afirmado pelas chamadas que os dublês registram, nunca pelo texto do fonte.
"""

from __future__ import annotations

import copy
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import aiohttp
import pytest

from . import _cloudflare_fixtures as F
from ._aiohttp_mock import aioresponses
from ..base import CollectorContext
from ..normalize import engine as E
from ..normalize.defaults import load_default_rules
from ..normalize.ocsf import validator as V
from ..registry import get, get_platform, supported_streams
from ..vendors import cloudflare as C

_GQL = re.compile(r"/client/v4/graphql$")
_VERIFY = re.compile(r"/client/v4/user/tokens/verify$")
_ZONES = re.compile(r"/client/v4/zones")


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
        integration_id=88,
        organization_id=3,
        platform="cloudflare",
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
    state = {"token": F.TOKEN, "zone_ids": F.ZONE_A}
    monkeypatch.setattr(C, "_load_settings", lambda integration_id: dict(state))
    return state


def _posts(m) -> List[Dict[str, Any]]:
    """Cada POST na GraphQL, na ordem em que saiu: consulta, variáveis e kwargs."""
    out: List[Dict[str, Any]] = []
    for (method, url), calls in m.requests.items():
        if method == "POST" and url.path.endswith("/client/v4/graphql"):
            for call in calls:
                body = call.kwargs["json"]
                out.append({"query": body["query"], "vars": body["variables"], "kwargs": call.kwargs})
    return out


def _event_posts(m) -> List[Dict[str, Any]]:
    return [p for p in _posts(m) if "ListFirewallEvents" in p["query"]]


def _gets(m, fragment: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for (method, url), calls in m.requests.items():
        if method == "GET" and fragment in url.path:
            out.extend({"url": url, "kwargs": c.kwargs} for c in calls)
    return out


async def _collect(collector) -> List[Dict[str, Any]]:
    return [ev async for ev in collector.collect()]


async def _run(cursor: Optional[Dict[str, Any]] = None, *, bounded: bool = True):
    """Um ciclo: devolve (coletor, eventos). O mock já deve estar ativo."""
    async with aiohttp.ClientSession() as session:
        collector = C.CloudflareSecurityEventsCollector(_ctx(session, cursor, bounded=bounded))
        return collector, await _collect(collector)


def _near(value: str, expected: datetime, tolerance_s: int = 30) -> bool:
    got = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    return abs((got - expected).total_seconds()) <= tolerance_s


def _key(raw: Dict[str, Any], zone: str = F.ZONE_A) -> str:
    c = C.CloudflareSecurityEventsCollector.__new__(C.CloudflareSecurityEventsCollector)
    return c.extract_message_id({**raw, "zoneTag": zone})


# ══ Zonas e limites (funções puras) ════════════════════════════════════


def test_zone_ids_are_validated_lowercased_and_deduped() -> None:
    upper = F.ZONE_A.upper()
    assert C.parse_zone_ids(f"{upper}, {F.ZONE_B};{F.ZONE_A}") == [F.ZONE_A, F.ZONE_B]
    assert C.parse_zone_ids("") == [] and C.parse_zone_ids("  ") == []


@pytest.mark.parametrize("raw", ["abc", "g" * 32, F.ZONE_A + "0", F.ZONE_A + "/../x", "https://x.example.com"])
def test_malformed_zone_ids_are_refused(raw: str) -> None:
    with pytest.raises(C.CloudflareCredentialError):
        C.parse_zone_ids(raw)


def test_more_than_fifty_zones_are_refused() -> None:
    many = ",".join(f"{i:032x}" for i in range(51))
    with pytest.raises(C.CloudflareCredentialError):
        C.parse_zone_ids(many)


def test_fields_are_the_available_ones_in_priority_order() -> None:
    node = F.settings_payload(fields=["clientIP", "action", "rayName", "source", "datetime", "ruleId", "naoExiste"])
    plan = C.plan_zone(C.settings_node(node["data"]))
    assert isinstance(plan, C.ZonePlan)
    assert plan.fields == ("datetime", "rayName", "action", "source", "ruleId", "clientIP")


def test_field_cap_keeps_the_mandatory_fields_even_when_they_rank_low() -> None:
    node = F.settings_payload(max_fields=7)
    plan = C.plan_zone(C.settings_node(node["data"]))
    assert isinstance(plan, C.ZonePlan)
    assert len(plan.fields) == 7
    assert set(C._MANDATORY_FIELDS) <= set(plan.fields), "clientIP é o 10º da prioridade mas é obrigatório"
    assert list(plan.fields) == [f for f in C._FIELD_PRIORITY if f in plan.fields], "ordem de prioridade"


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"enabled": False}, "desativado"),
        ({"fields": ["datetime", "rayName", "action", "source"]}, "clientIP"),
        ({"max_fields": 3}, "maxNumberOfFields=3"),
        ({"max_page_size": 0}, "maxPageSize"),
    ],
)
def test_unusable_zones_come_with_a_reason(kwargs: Dict[str, Any], reason: str) -> None:
    plan = C.plan_zone(C.settings_node(F.settings_payload(**kwargs)["data"]))
    assert isinstance(plan, str) and reason in plan


def test_page_size_follows_the_zone_but_has_a_memory_ceiling() -> None:
    small = C.plan_zone(C.settings_node(F.settings_payload(max_page_size=500)["data"]))
    huge = C.plan_zone(C.settings_node(F.settings_payload(max_page_size=5_000_000)["data"]))
    assert isinstance(small, C.ZonePlan) and isinstance(huge, C.ZonePlan)
    assert small.page_size == 500 and huge.page_size == C._PAGE_SIZE_CEILING


def test_events_query_refuses_anything_that_is_not_a_field_name() -> None:
    with pytest.raises(ValueError):
        C.events_query(("datetime", "x } } mutation { y"), 10)
    with pytest.raises(ValueError):
        C.events_query(("datetime",), 0)


# ══ Coleta ═════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_cold_start_reads_the_last_hour_up_to_the_ingestion_lag() -> None:
    t = F.ago(seconds=600)
    with aioresponses() as m:
        m.post(_GQL, payload=F.settings_payload())
        m.post(_GQL, payload=F.events_payload([F.event("r1", t), F.event("r2", t + timedelta(seconds=5))]))
        collector, events = await _run()

    settings_call, events_call = _posts(m)
    assert "ZoneSettings" in settings_call["query"] and settings_call["vars"] == {"zoneTag": F.ZONE_A}
    assert "datetime_ASC" in events_call["query"]
    flt = events_call["vars"]["filter"]
    assert _near(flt["datetime_geq"], F.ago(hours=1)) and _near(flt["datetime_lt"], F.ago(seconds=60))
    assert [e["rayName"] for e in events] == ["r1", "r2"]
    assert all(e["zoneTag"] == F.ZONE_A for e in events)
    assert collector.ctx.cursor == {"zones": {F.ZONE_A: {"since": flt["datetime_lt"]}}}
    assert not collector.ctx.hit_cycle_cap
    # O token vai no header, o transporte é HTTPS sem redirect, a zona vai como variável.
    kwargs = events_call["kwargs"]
    assert kwargs["headers"]["Authorization"] == f"Bearer {F.TOKEN}"
    assert kwargs["allow_redirects"] is False
    assert F.ZONE_A not in events_call["query"]
    assert all(str(url).startswith("https://api.cloudflare.com/") for _, url in m.requests)


@pytest.mark.asyncio
async def test_query_asks_only_the_selected_fields_and_the_zone_page_size() -> None:
    avail = ["datetime", "rayName", "action", "source", "clientIP", "ruleId", "userAgent", "campoDeOutroPlano"]
    with aioresponses() as m:
        m.post(_GQL, payload=F.settings_payload(fields=avail, max_page_size=777))
        m.post(_GQL, payload=F.events_payload([]))
        await _run()
    (call,) = _event_posts(m)
    body = call["query"].split("firewallEventsAdaptive(", 1)[1]
    assert "limit: 777" in body
    selected = body.split("{", 1)[1].split("}", 1)[0].split()
    assert selected == ["datetime", "rayName", "action", "source", "ruleId", "clientIP", "userAgent"]


@pytest.mark.asyncio
async def test_window_is_never_wider_than_max_duration() -> None:
    since = F.ago(minutes=30)
    with aioresponses() as m:
        m.post(_GQL, payload=F.settings_payload(max_duration=600))
        for _ in range(3):
            m.post(_GQL, payload=F.events_payload([]))
        collector, _ = await _run({"zones": {F.ZONE_A: {"since": F.iso(since)}}})

    windows = [
        (datetime.strptime(p["vars"]["filter"]["datetime_geq"], "%Y-%m-%dT%H:%M:%SZ"),
         datetime.strptime(p["vars"]["filter"]["datetime_lt"], "%Y-%m-%dT%H:%M:%SZ"))
        for p in _event_posts(m)
    ]
    assert len(windows) == 3
    assert all((hi - lo).total_seconds() <= 600 for lo, hi in windows)
    assert windows[0][0] == since and windows[1][0] == windows[0][1], "janelas contíguas"
    assert _near(F.iso(windows[-1][1]), F.ago(seconds=60)), "a última termina na latência de ingestão"
    assert collector.ctx.cursor["zones"][F.ZONE_A]["since"] == F.iso(windows[-1][1])


@pytest.mark.asyncio
async def test_inclusive_lower_bound_does_not_reemit_what_the_boundary_remembers() -> None:
    t = F.ago(seconds=300)
    seen, fresh, later = F.event("r-seen", t), F.event("r-fresh", t), F.event("r-later", t + timedelta(seconds=10))
    cursor = {"zones": {F.ZONE_A: {"since": F.iso(t), "boundary": {"at": F.iso(t), "keys": [_key(seen)]}}}}
    with aioresponses() as m:
        m.post(_GQL, payload=F.settings_payload())
        m.post(_GQL, payload=F.events_payload([seen, fresh, later]))
        collector, events = await _run(cursor)

    assert _event_posts(m)[0]["vars"]["filter"]["datetime_geq"] == F.iso(t), "o limite inferior é inclusivo"
    assert [e["rayName"] for e in events] == ["r-fresh", "r-later"]


@pytest.mark.asyncio
async def test_capped_cursor_resumes_without_reemitting_the_boundary(monkeypatch) -> None:
    monkeypatch.setattr(C, "_MAX_PAGES_PER_CYCLE", 1)
    t = F.ago(seconds=300)
    a, b = F.event("r-a", t), F.event("r-b", t + timedelta(seconds=1))
    with aioresponses() as m:
        m.post(_GQL, payload=F.settings_payload(max_page_size=2))
        m.post(_GQL, payload=F.events_payload([a, b]))
        collector, first = await _run({"zones": {F.ZONE_A: {"since": F.iso(t)}}})
    assert [e["rayName"] for e in first] == ["r-a", "r-b"] and collector.ctx.hit_cycle_cap
    saved = copy.deepcopy(collector.ctx.cursor)

    c = F.event("r-c", t + timedelta(seconds=3))
    with aioresponses() as m:
        m.post(_GQL, payload=F.settings_payload(max_page_size=3))
        m.post(_GQL, payload=F.events_payload([b, c]))
        collector, second = await _run(saved)
    assert _event_posts(m)[0]["vars"]["filter"]["datetime_geq"] == F.iso(t + timedelta(seconds=1))
    assert [e["rayName"] for e in second] == ["r-c"], "r-b já saiu no ciclo anterior"


@pytest.mark.asyncio
async def test_full_page_continues_from_the_last_event_instant() -> None:
    t = F.ago(seconds=600)
    rows = [F.event(f"r{i}", t + timedelta(seconds=i)) for i in range(3)]
    tail = [rows[2], F.event("r3", t + timedelta(seconds=2))]
    with aioresponses() as m:
        m.post(_GQL, payload=F.settings_payload(max_page_size=3))
        m.post(_GQL, payload=F.events_payload(rows))
        m.post(_GQL, payload=F.events_payload(tail))
        collector, events = await _run({"zones": {F.ZONE_A: {"since": F.iso(t)}}})

    first, second = _event_posts(m)
    assert second["vars"]["filter"]["datetime_geq"] == F.iso(t + timedelta(seconds=2))
    assert second["vars"]["filter"]["datetime_lt"] == first["vars"]["filter"]["datetime_lt"]
    assert [e["rayName"] for e in events] == ["r0", "r1", "r2", "r3"], "r2 volta na página 2 e não é reemitido"
    assert collector.ctx.cursor["zones"][F.ZONE_A]["since"] == second["vars"]["filter"]["datetime_lt"]


@pytest.mark.asyncio
async def test_more_than_a_page_in_the_same_second_warns_and_moves_on(caplog) -> None:
    t = F.ago(seconds=300)
    with aioresponses() as m:
        m.post(_GQL, payload=F.settings_payload(max_page_size=2))
        m.post(_GQL, payload=F.events_payload([F.event("x1", t), F.event("x2", t)]))
        m.post(_GQL, payload=F.events_payload([]))
        with caplog.at_level(logging.WARNING, logger=C.__name__):
            collector, events = await _run({"zones": {F.ZONE_A: {"since": F.iso(t)}}})

    first, second = _event_posts(m)
    assert len(_event_posts(m)) == 2, "não fica em laço pedindo a mesma página"
    assert second["vars"]["filter"]["datetime_geq"] == F.iso(t + timedelta(seconds=1))
    assert [e["rayName"] for e in events] == ["x1", "x2"]
    assert any("perda parcial" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)


@pytest.mark.asyncio
async def test_cursor_older_than_the_retention_is_clamped_and_the_loss_is_logged(caplog) -> None:
    old = F.ago(days=40)
    retention = 31 * 86400
    with aioresponses() as m:
        m.post(_GQL, payload=F.settings_payload(max_duration=retention, not_older_than=retention))
        m.post(_GQL, payload=F.events_payload([]))
        with caplog.at_level(logging.WARNING, logger=C.__name__):
            await _run({"zones": {F.ZONE_A: {"since": F.iso(old)}}})

    (call,) = _event_posts(m)
    assert _near(call["vars"]["filter"]["datetime_geq"], F.ago(days=31) + timedelta(minutes=5)), "nunca pede além da retenção"
    assert any("passou da retenção" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_cursors_are_per_zone_added_and_removed_with_the_configuration(_settings) -> None:
    _settings["zone_ids"] = f"{F.ZONE_A},{F.ZONE_B}"
    a_since = F.ago(seconds=300)
    cursor = {"zones": {
        F.ZONE_A: {"since": F.iso(a_since)},
        F.ZONE_C: {"since": F.iso(F.ago(seconds=100))},  # zona removida da configuração
    }}
    with aioresponses() as m:
        for _ in range(2):  # B (a frio, a mais atrasada) primeiro, depois A
            m.post(_GQL, payload=F.settings_payload())
            m.post(_GQL, payload=F.events_payload([]))
        collector, _ = await _run(cursor)

    posts = _posts(m)
    assert [p["vars"]["zoneTag"] for p in posts] == [F.ZONE_B, F.ZONE_B, F.ZONE_A, F.ZONE_A]
    assert _near(posts[1]["vars"]["filter"]["datetime_geq"], F.ago(hours=1)), "zona nova começa a frio"
    assert posts[3]["vars"]["filter"]["datetime_geq"] == F.iso(a_since)
    assert set(collector.ctx.cursor["zones"]) == {F.ZONE_A, F.ZONE_B}, "o cursor da zona removida some"


@pytest.mark.asyncio
async def test_zone_with_the_dataset_off_is_skipped_without_breaking_the_others(_settings, caplog) -> None:
    _settings["zone_ids"] = f"{F.ZONE_A},{F.ZONE_B}"
    t = F.ago(seconds=200)
    with aioresponses() as m:
        m.post(_GQL, payload=F.settings_payload(enabled=False))
        m.post(_GQL, payload=F.settings_payload())
        m.post(_GQL, payload=F.events_payload([F.event("r-b", t)]))
        with caplog.at_level(logging.WARNING, logger=C.__name__):
            collector, events = await _run()

    assert [e["zoneTag"] for e in events] == [F.ZONE_B]
    assert set(collector.ctx.cursor["zones"]) == {F.ZONE_B}
    assert any(F.ZONE_A in r.getMessage() and "desativado" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_every_zone_unusable_fails_the_cycle_instead_of_going_green() -> None:
    with aioresponses() as m:
        m.post(_GQL, payload=F.settings_payload(enabled=False))
        with pytest.raises(RuntimeError, match="nenhuma zona utilizável"):
            await _run()


@pytest.mark.asyncio
async def test_zone_the_token_cannot_see_is_skipped(_settings, caplog) -> None:
    _settings["zone_ids"] = f"{F.ZONE_A},{F.ZONE_B}"
    with aioresponses() as m:
        m.post(_GQL, payload=F.settings_payload(zones=False))
        m.post(_GQL, payload=F.settings_payload())
        m.post(_GQL, payload=F.events_payload([]))
        with caplog.at_level(logging.WARNING, logger=C.__name__):
            collector, _ = await _run()
    assert set(collector.ctx.cursor["zones"]) == {F.ZONE_B}
    assert any("não tem acesso a esta zona" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_empty_zone_config_discovers_zones_every_cycle(_settings) -> None:
    _settings["zone_ids"] = ""
    with aioresponses() as m:
        m.get(_ZONES, payload=F.zones_payload([F.ZONE_A], page=1, total_pages=2))
        m.get(_ZONES, payload=F.zones_payload([F.ZONE_B], page=2, total_pages=2))
        for _ in range(2):
            m.post(_GQL, payload=F.settings_payload())
            m.post(_GQL, payload=F.events_payload([]))
        collector, _ = await _run()

    pages = _gets(m, "/client/v4/zones")
    assert [g["kwargs"]["params"] for g in pages] == [{"per_page": 50, "page": 1}, {"per_page": 50, "page": 2}]
    assert {p["vars"]["zoneTag"] for p in _posts(m)} == {F.ZONE_A, F.ZONE_B}
    assert set(collector.ctx.cursor["zones"]) == {F.ZONE_A, F.ZONE_B}


@pytest.mark.asyncio
async def test_cycle_cap_stops_with_a_resumable_cursor_and_says_so() -> None:
    since = F.ago(minutes=30)
    pages = C._MAX_PAGES_PER_CYCLE + 3
    rows = [F.event(f"c{i}", since + timedelta(seconds=2 * (i + 1))) for i in range(pages)]
    with aioresponses() as m:
        m.post(_GQL, payload=F.settings_payload(max_page_size=1))
        for row in rows:
            m.post(_GQL, payload=F.events_payload([row]))
        collector, events = await _run({"zones": {F.ZONE_A: {"since": F.iso(since)}}})

    assert len(events) == C._MAX_PAGES_PER_CYCLE and len(_event_posts(m)) == C._MAX_PAGES_PER_CYCLE
    assert collector.ctx.hit_cycle_cap
    last = rows[C._MAX_PAGES_PER_CYCLE - 1]
    zstate = collector.ctx.cursor["zones"][F.ZONE_A]
    assert zstate["since"] == last["datetime"], "o próximo ciclo pede a partir do último evento lido"
    assert zstate["boundary"] == {"at": last["datetime"], "keys": [_key(last)]}


@pytest.mark.asyncio
async def test_unbounded_run_drains_past_the_cap() -> None:
    since = F.ago(minutes=30)
    total = C._MAX_PAGES_PER_CYCLE + 2
    with aioresponses() as m:
        m.post(_GQL, payload=F.settings_payload(max_page_size=1))
        for i in range(total):
            m.post(_GQL, payload=F.events_payload([F.event(f"u{i}", since + timedelta(seconds=2 * (i + 1)))]))
        m.post(_GQL, payload=F.events_payload([]))
        collector, events = await _run({"zones": {F.ZONE_A: {"since": F.iso(since)}}}, bounded=False)
    assert len(events) == total and not collector.ctx.hit_cycle_cap


@pytest.mark.asyncio
async def test_quiet_zone_does_not_claim_backlog() -> None:
    with aioresponses() as m:
        m.post(_GQL, payload=F.settings_payload())
        m.post(_GQL, payload=F.events_payload([]))
        collector, events = await _run()
    assert events == [] and not collector.ctx.hit_cycle_cap


# ══ Backfill ═══════════════════════════════════════════════════════════


def _epoch(moment: datetime) -> int:
    return int(moment.replace(tzinfo=timezone.utc).timestamp())


@pytest.mark.asyncio
async def test_backfill_reads_slices_of_max_duration_without_touching_the_cursor() -> None:
    lo, hi = F.ago(minutes=25), F.ago(minutes=5)
    cursor = {"backfill_from_ts": _epoch(lo), "backfill_to_ts": _epoch(hi)}
    with aioresponses() as m:
        m.post(_GQL, payload=F.settings_payload(max_duration=600))
        m.post(_GQL, payload=F.events_payload([F.event("b1", lo + timedelta(seconds=30))]))
        m.post(_GQL, payload=F.events_payload([F.event("b2", lo + timedelta(seconds=700))]))
        collector, events = await _run(copy.deepcopy(cursor), bounded=False)

    first, second = [p["vars"]["filter"] for p in _event_posts(m)]
    assert (first["datetime_geq"], second["datetime_lt"]) == (F.iso(lo), F.iso(hi))
    assert first["datetime_lt"] == second["datetime_geq"] == F.iso(lo + timedelta(seconds=600))
    assert [e["rayName"] for e in events] == ["b1", "b2"] and all(e["zoneTag"] == F.ZONE_A for e in events)
    assert collector.ctx.cursor == cursor, "backfill não mexe no cursor do polling"


@pytest.mark.asyncio
async def test_backfill_splits_a_full_slice_in_two() -> None:
    lo, hi = F.ago(minutes=20), F.ago(minutes=10)
    t = lo + timedelta(seconds=10)
    with aioresponses() as m:
        m.post(_GQL, payload=F.settings_payload(max_duration=600, max_page_size=2))
        m.post(_GQL, payload=F.events_payload([F.event("full1", t), F.event("full2", t)]))
        m.post(_GQL, payload=F.events_payload([F.event("h1", t)]))
        m.post(_GQL, payload=F.events_payload([F.event("h2", t + timedelta(seconds=400))]))
        _, events = await _run({"backfill_from_ts": _epoch(lo), "backfill_to_ts": _epoch(hi)}, bounded=False)

    whole, half_a, half_b = [p["vars"]["filter"] for p in _event_posts(m)]
    assert half_a["datetime_geq"] == whole["datetime_geq"] and half_b["datetime_lt"] == whole["datetime_lt"]
    assert half_a["datetime_lt"] == half_b["datetime_geq"], "as metades se encontram sem sobrepor"
    assert [e["rayName"] for e in events] == ["h1", "h2"], "a fatia cheia não é emitida: volta como duas metades"


@pytest.mark.asyncio
async def test_backfill_before_the_retention_is_clamped_and_logged(caplog) -> None:
    retention = 31 * 86400
    with aioresponses() as m:
        m.post(_GQL, payload=F.settings_payload(max_duration=retention, not_older_than=retention))
        m.post(_GQL, payload=F.events_payload([]))
        with caplog.at_level(logging.WARNING, logger=C.__name__):
            await _run({"backfill_from_ts": _epoch(F.ago(days=45)), "backfill_to_ts": _epoch(F.ago(days=44))}, bounded=False)
    # Todo o pedido era anterior à retenção: nada a consultar, e o log diz quanto se perdeu.
    assert _event_posts(m) == []
    assert any("não podem mais ser lidos" in r.getMessage() for r in caplog.records)


# ══ Erros ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_http_429_raises_with_retry_after() -> None:
    with aioresponses() as m:
        m.post(_GQL, status=429, headers={"Retry-After": "17"}, payload=F.errors_payload("slow down"))
        with pytest.raises(C.CloudflareRateLimitedError) as exc:
            await _run()
    assert exc.value.retry_after == 17 and exc.value.vendor == "cloudflare"


@pytest.mark.asyncio
async def test_http_429_without_header_defaults_to_sixty_seconds() -> None:
    with aioresponses() as m:
        m.post(_GQL, status=429, payload={})
        with pytest.raises(C.CloudflareRateLimitedError) as exc:
            await _run()
    assert exc.value.retry_after == 60


@pytest.mark.asyncio
async def test_graphql_budget_error_on_http_200_is_a_rate_limit() -> None:
    with aioresponses() as m:
        m.post(_GQL, payload=F.errors_payload("rate limiter budget depleted, try again after 5 minutes"))
        with pytest.raises(C.CloudflareRateLimitedError) as exc:
            await _run()
    assert exc.value.retry_after == 300


@pytest.mark.asyncio
async def test_graphql_error_carries_only_the_message_and_never_the_token() -> None:
    with aioresponses() as m:
        m.post(_GQL, payload=F.errors_payload(f"unknown field boom Bearer {F.TOKEN}"))
        with pytest.raises(C.CloudflareGraphQLError) as exc:
            await _run()
    assert "unknown field boom" in str(exc.value) and F.TOKEN not in str(exc.value)


@pytest.mark.asyncio
async def test_graphql_permission_error_names_the_missing_permission() -> None:
    with aioresponses() as m:
        m.post(_GQL, payload=F.errors_payload("does not have access to the path"))
        with pytest.raises(C.CloudflareApiError) as exc:
            await _run()
    assert exc.value.status == 403 and "Zone > Analytics > Read" in exc.value.message


@pytest.mark.asyncio
async def test_unauthorized_http_status_carries_no_secret_nor_header() -> None:
    with aioresponses() as m:
        m.post(_GQL, status=401, payload=F.errors_payload(f"Unauthorized {F.TOKEN}"))
        with pytest.raises(C.CloudflareApiError) as exc:
            await _run()
    err = exc.value
    text = f"{err} {err.message} {err.request_info!r}"
    assert err.status == 401 and "token inválido" in err.message
    assert F.TOKEN not in text and "Bearer" not in text


@pytest.mark.asyncio
async def test_forbidden_http_status_names_the_permission() -> None:
    with aioresponses() as m:
        m.post(_GQL, status=403, payload=F.errors_payload("zones [x] are not authorized"))
        with pytest.raises(C.CloudflareApiError) as exc:
            await _run()
    assert exc.value.status == 403 and "Zone > Analytics > Read" in exc.value.message


@pytest.mark.asyncio
async def test_redirect_is_never_followed() -> None:
    with aioresponses() as m:
        m.post(_GQL, status=302, headers={"Location": "https://evil.example.com/x"})
        with pytest.raises(C.CloudflareApiError):
            await _run()
    assert all("evil" not in str(url) for _, url in m.requests)
    assert _posts(m)[0]["kwargs"]["allow_redirects"] is False


@pytest.mark.asyncio
async def test_missing_token_never_reaches_the_network(_settings) -> None:
    _settings["token"] = "  "
    with aioresponses() as m:
        with pytest.raises(C.CloudflareCredentialError):
            await _run()
    assert not m.requests


@pytest.mark.asyncio
async def test_malformed_zone_id_never_reaches_the_network(_settings) -> None:
    _settings["zone_ids"] = "nao-e-uma-zona"
    with aioresponses() as m:
        with pytest.raises(C.CloudflareCredentialError):
            await _run()
    assert not m.requests


# ══ Identidade e watermark ═════════════════════════════════════════════


def test_one_ray_with_two_rules_yields_two_distinct_message_ids() -> None:
    t = datetime(2026, 11, 12, 10, 0, 3)
    first = F.event("ray-1", t, source="firewallmanaged", rule_id="r-1")
    second = F.event("ray-1", t, source="firewallcustom", rule_id="r-2")
    same_rule_other_action = F.event("ray-1", t, source="firewallmanaged", rule_id="r-1", action="log")
    other_match = F.event("ray-1", t, source="firewallmanaged", rule_id="r-1", match_index=1)
    ids = {_key(e) for e in (first, second, same_rule_other_action, other_match)}
    assert len(ids) == 4
    assert _key(first) == _key(copy.deepcopy(first)), "estável"
    assert _key(first, F.ZONE_A) != _key(first, F.ZONE_B), "a zona entra na chave"
    assert _key(first).startswith("ray-1:")


def test_watermark_is_the_oldest_zone_since() -> None:
    cursor = {"zones": {
        F.ZONE_A: {"since": "2026-07-24T11:00:00Z"},
        F.ZONE_B: {"since": "2026-07-24T10:00:00Z", "boundary": {"at": "2026-07-24T10:00:00Z", "keys": ["k"]}},
    }}
    wm = C.CloudflareSecurityEventsCollector.watermark_at(cursor)
    assert wm == datetime(2026, 7, 24, 10, 0, 0) and wm.tzinfo is None
    assert C.CloudflareSecurityEventsCollector.watermark_at({}) is None
    assert C.CloudflareSecurityEventsCollector.watermark_at({"zones": {}}) is None
    assert C.CloudflareSecurityEventsCollector.watermark_at(None) is None


# ══ Teste de conexão ═══════════════════════════════════════════════════

_CFG = {"client_secret": F.TOKEN, "tenant_id": f"{F.ZONE_A},{F.ZONE_B}"}


@pytest.mark.asyncio
async def test_probe_passes_when_the_token_is_active_and_every_zone_is_usable() -> None:
    with aioresponses() as m:
        m.get(_VERIFY, payload=F.verify_payload())
        m.post(_GQL, payload=F.settings_payload(), repeat=True)
        result = await C._cloudflare_probe(dict(_CFG))

    assert result.ok, result.detail
    assert f"zona {F.ZONE_A}: ok" in result.detail and f"zona {F.ZONE_B}: ok" in result.detail
    assert F.TOKEN not in result.detail
    assert [p["vars"]["zoneTag"] for p in _posts(m)] == [F.ZONE_A, F.ZONE_B]
    (verify,) = _gets(m, "/user/tokens/verify")
    assert verify["kwargs"]["headers"]["Authorization"] == f"Bearer {F.TOKEN}"


@pytest.mark.asyncio
async def test_probe_fails_when_the_token_is_not_active_and_stops_there() -> None:
    with aioresponses() as m:
        m.get(_VERIFY, payload=F.verify_payload("expired"))
        result = await C._cloudflare_probe(dict(_CFG))
    assert not result.ok and "expired" in result.detail and F.TOKEN not in result.detail
    assert _posts(m) == []


@pytest.mark.asyncio
async def test_probe_continues_when_the_user_verify_endpoint_does_not_know_the_token() -> None:
    """Token de conta não é verificável em /user/tokens/verify: a prova real é a zona."""
    with aioresponses() as m:
        m.get(_VERIFY, status=401, payload={"success": False, "errors": [{"message": "Invalid API Token"}]})
        m.post(_GQL, payload=F.settings_payload(), repeat=True)
        result = await C._cloudflare_probe(dict(_CFG))
    assert result.ok and "não verificável" in result.detail


@pytest.mark.asyncio
async def test_probe_invalid_token_fails_on_the_zone_even_if_verify_was_inconclusive() -> None:
    with aioresponses() as m:
        m.get(_VERIFY, status=401, payload={"success": False})
        m.post(_GQL, status=401, payload=F.errors_payload("Unauthorized"), repeat=True)
        result = await C._cloudflare_probe(dict(_CFG))
    assert not result.ok and "token inválido" in result.detail


@pytest.mark.asyncio
async def test_probe_missing_permission_names_it_per_zone() -> None:
    with aioresponses() as m:
        m.get(_VERIFY, payload=F.verify_payload())
        m.post(_GQL, payload=F.settings_payload())
        m.post(_GQL, status=403, payload=F.errors_payload(f"zones [{F.ZONE_B}] are not authorized {F.TOKEN}"))
        result = await C._cloudflare_probe(dict(_CFG))
    assert not result.ok
    assert f"zona {F.ZONE_A}: ok" in result.detail
    assert f"zona {F.ZONE_B}: sem permissão" in result.detail and "Zone > Analytics > Read" in result.detail
    assert F.TOKEN not in result.detail


@pytest.mark.asyncio
async def test_probe_reports_a_zone_whose_plan_does_not_expose_the_dataset() -> None:
    with aioresponses() as m:
        m.get(_VERIFY, payload=F.verify_payload())
        m.post(_GQL, payload=F.settings_payload())
        m.post(_GQL, payload=F.settings_payload(enabled=False))
        result = await C._cloudflare_probe(dict(_CFG))
    assert not result.ok and f"zona {F.ZONE_B}: dataset firewallEventsAdaptive desativado" in result.detail


@pytest.mark.asyncio
async def test_probe_reports_missing_mandatory_fields() -> None:
    with aioresponses() as m:
        m.get(_VERIFY, payload=F.verify_payload())
        m.post(_GQL, payload=F.settings_payload(fields=["datetime", "rayName", "action"]), repeat=True)
        result = await C._cloudflare_probe(dict(_CFG))
    assert not result.ok and "campos obrigatórios indisponíveis" in result.detail
    assert "source" in result.detail and "clientIP" in result.detail


@pytest.mark.asyncio
async def test_probe_without_zone_ids_discovers_them() -> None:
    with aioresponses() as m:
        m.get(_VERIFY, payload=F.verify_payload())
        m.get(_ZONES, payload=F.zones_payload([F.ZONE_A, F.ZONE_B]))
        m.post(_GQL, payload=F.settings_payload(), repeat=True)
        result = await C._cloudflare_probe({"client_secret": F.TOKEN, "tenant_id": ""})
    assert result.ok, result.detail
    assert "2 zona(s) descoberta(s)" in result.detail
    assert [p["vars"]["zoneTag"] for p in _posts(m)] == [F.ZONE_A, F.ZONE_B]


@pytest.mark.asyncio
async def test_probe_discovery_without_zone_read_says_which_permission_is_missing() -> None:
    with aioresponses() as m:
        m.get(_VERIFY, payload=F.verify_payload())
        m.get(_ZONES, status=403, payload={"success": False, "errors": [{"message": "forbidden"}]})
        result = await C._cloudflare_probe({"client_secret": F.TOKEN, "tenant_id": ""})
    assert not result.ok and "Zone > Zone > Read" in result.detail


@pytest.mark.asyncio
async def test_probe_discovery_with_no_zone_fails() -> None:
    with aioresponses() as m:
        m.get(_VERIFY, payload=F.verify_payload())
        m.get(_ZONES, payload=F.zones_payload([]))
        result = await C._cloudflare_probe({"client_secret": F.TOKEN, "tenant_id": ""})
    assert not result.ok and "nenhuma zona" in result.detail


@pytest.mark.asyncio
async def test_probe_rejects_bad_input_without_network_or_echo() -> None:
    with aioresponses() as m:
        bad_zone = await C._cloudflare_probe({**_CFG, "tenant_id": "abc"})
        no_token = await C._cloudflare_probe({**_CFG, "client_secret": ""})
    assert not bad_zone.ok and "32 caracteres" in bad_zone.detail
    assert not no_token.ok
    assert not m.requests


@pytest.mark.asyncio
async def test_probe_network_failure_is_reported_without_the_token() -> None:
    with aioresponses() as m:
        m.get(_VERIFY, exception=aiohttp.ClientConnectionError(f"boom {F.TOKEN}"))
        result = await C._cloudflare_probe(dict(_CFG))
    assert not result.ok and "ClientConnectionError" in result.detail and F.TOKEN not in result.detail


# ══ Registro ═══════════════════════════════════════════════════════════


def test_platform_is_registered_with_editable_columns_only() -> None:
    plat = get_platform("cloudflare")
    assert plat.display_name == "Cloudflare (WAF e segurança)" and plat.category == "Rede / Firewall"
    assert plat.icon_id == "cloudflare"
    assert plat.docs_url == "https://developers.cloudflare.com/analytics/graphql-api/"
    assert plat.capabilities == frozenset({"catalog", "auth:test", "collect:security_events"})
    assert plat.required_secrets == ("client_secret",)
    fields = {f.key: f for f in plat.auth_fields}
    assert set(fields) == {"client_secret", "tenant_id"}
    assert fields["client_secret"].type == "secret" and fields["client_secret"].required
    assert fields["tenant_id"].required is False, "vazio = descobrir as zonas"
    assert supported_streams("cloudflare") == ["security_events"]
    reg = get("cloudflare", "security_events")
    assert reg.collector_cls is C.CloudflareSecurityEventsCollector
    assert reg.schedule == timedelta(minutes=1)
    assert C.CloudflareSecurityEventsCollector.event_type == "cloudflare.security_events"
    assert C.CloudflareSecurityEventsCollector.__new__(C.CloudflareSecurityEventsCollector).domain == "api.cloudflare.com"


# ══ Mapping ════════════════════════════════════════════════════════════


def _normalize(raw: Dict[str, Any]) -> Dict[str, Any]:
    compiled = E.compile_rules(load_default_rules("cloudflare", "cloudflare.security_events"))
    n = E.apply_compiled(compiled, copy.deepcopy(raw)).output["normalized"]
    r = V.validate_normalized(n)
    assert r.valid, (r.reason, n)
    return n


def test_blocked_request_is_an_http_activity_with_the_real_client_and_the_rule() -> None:
    n = _normalize(F.BLOCK_EVENT)
    assert (n["class_uid"], n["activity_id"], n["type_uid"]) == (4002, 3, 400203)
    assert (n["disposition_id"], n["action_id"], n["severity_id"], n["is_alert"]) == (2, 2, 2, True)
    assert n["src_endpoint"]["ip"] == "203.0.113.7"
    assert n["src_endpoint"]["location"]["country"] == "BR"
    assert n["src_endpoint"]["autonomous_system"] == {"number": 64500, "name": "EXAMPLE-AS"}
    assert n["http_request"]["url"] == {
        "hostname": "www.example.com", "path": "/login", "query_string": "?next=%2F", "scheme": "https",
    }
    assert n["http_request"]["http_method"] == "GET" and n["http_request"]["version"] == "HTTP/2"
    assert n["http_request"]["user_agent"] == "Mozilla/5.0 (teste)"
    assert n["http_request"]["referrer"] == "example.org"
    assert n["http_response"]["code"] == 403
    assert n["firewall_rule"] == {"uid": "7f3a91c2", "name": "Bloqueia scanners conhecidos", "type": "firewallcustom"}
    assert n["metadata"]["uid"] == "8a1b2c3d4e5f6a7b" and n["metadata"]["product"]["vendor_name"] == "Cloudflare"
    assert n["time"] == int(datetime(2026, 11, 12, 10, 0, 3, tzinfo=timezone.utc).timestamp() * 1000)
    assert n["message"] == "Cloudflare firewallcustom block: Bloqueia scanners conhecidos"
    assert {"src_endpoint.ip", "http_request.user_agent"} <= {o["name"] for o in n["observables"]}


def test_fields_without_a_home_in_ocsf_go_to_unmapped_and_the_zone_is_kept() -> None:
    n = _normalize(F.BLOCK_EVENT)
    assert n["unmapped"] == {
        "cloudflare_zone_id": F.ZONE_A,
        "cloudflare_kind": "firewall",
        "cloudflare_match_index": 0,
        "cloudflare_ruleset_id": "2c0d5e8b",
        "cloudflare_client_ip_class": "noRecord",
        "cloudflare_edge_colo": "GRU",
        "cloudflare_origin_response_status": 0,
        "cloudflare_sample_interval": 1,
        "cloudflare_ref": "",
    }


@pytest.mark.parametrize(
    "action,disposition,action_id,severity,is_alert",
    [
        ("block", 2, 2, 2, True),
        ("connection_close", 2, 2, 2, True),
        ("force_connection_close", 2, 2, 2, True),
        ("challenge_failed", 2, 2, 2, True),
        ("managed_challenge", 23, 99, 2, True),
        ("jschallenge", 23, 99, 2, True),
        ("challenge", 23, 99, 2, True),
        ("managed_challenge_interactive_solved", 1, 1, 1, False),
        ("jschallenge_bypassed", 1, 1, 1, False),
        ("managed_challenge_skipped", 1, 1, 1, False),
        ("log", 17, 1, 3, True),
        ("skip", 1, 1, 1, False),
        ("allow", 1, 1, 1, False),
        ("bypass", 1, 1, 1, False),
        ("rewrite", 99, 99, 1, False),
    ],
)
def test_disposition_action_and_severity_follow_the_cloudflare_action(
    action: str, disposition: int, action_id: int, severity: int, is_alert: bool
) -> None:
    n = _normalize({**F.BLOCK_EVENT, "action": action})
    assert (n["disposition_id"], n["action_id"], n["severity_id"], n["is_alert"]) == (
        disposition, action_id, severity, is_alert,
    )
    assert n["metadata"]["event_code"] == action


def test_missing_action_is_unknown_not_a_crash() -> None:
    raw = {k: v for k, v in F.BLOCK_EVENT.items() if k != "action"}
    n = _normalize(raw)
    assert (n["disposition_id"], n["action_id"], n["severity_id"]) == (0, 0, 0)


@pytest.mark.parametrize("method,activity", [("POST", 6), ("PUT", 7), ("DELETE", 2), ("PATCH", 9), ("PROPFIND", 99)])
def test_activity_follows_the_http_method(method: str, activity: int) -> None:
    n = _normalize({**F.BLOCK_EVENT, "clientRequestHTTPMethodName": method})
    assert (n["activity_id"], n["type_uid"]) == (activity, 400200 + activity)


def test_absent_optional_fields_leave_no_nulls_behind() -> None:
    raw = {k: v for k, v in F.BLOCK_EVENT.items() if k not in ("clientRefererHost", "ref", "kind", "userAgent")}
    n = _normalize(raw)
    assert "referrer" not in n["http_request"]
    assert "cloudflare_ref" not in n["unmapped"] and "cloudflare_kind" not in n["unmapped"]
    assert "user_agent" not in {o["name"] for o in n["observables"]}
