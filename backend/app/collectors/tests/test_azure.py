"""Microsoft Azure — Activity Log (ARM), Event Hubs (Kafka), credencial, probe e mappings.

Os riscos que estes testes travam:

- **perder evento sem barulho** — janela do Activity Log que não sobrepõe, teto
  que não deixa cursor resumível, offset que avança antes da entrega, retenção do
  Event Hub vencida sem log;
- **entregar o token do cliente a outro host** — namespace fora de
  ``servicebus.windows.net``, ``nextLink`` fora do ARM, tenant que vira PATH;
- **vazar o segredo pela UI** — o probe devolve só o código AADSTS.

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
from aiokafka.structs import TopicPartition
from yarl import URL

from . import _azure_fixtures as F
from ._aiohttp_mock import aioresponses
from ..base import CollectorContext
from ..normalize import engine as E
from ..normalize.defaults import load_default_rules
from ..normalize.ocsf import validator as V
from ..registry import get, get_platform, supported_streams
from ..vendors import azure as A

_SUBS = "https://management.azure.com/subscriptions?api-version=2022-12-01"
_TOKEN = f"https://login.microsoftonline.com/{F.TENANT}/oauth2/v2.0/token"


def _activity_url(sub: str) -> re.Pattern:
    return re.compile(
        rf"^https://management\.azure\.com/subscriptions/{sub}/providers/"
        r"Microsoft\.Insights/eventtypes/management/values\?"
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


def _ctx(session, cursor: Optional[Dict[str, Any]] = None, *, bounded: bool = True) -> CollectorContext:
    return CollectorContext(
        integration_id=91,
        organization_id=3,
        platform="azure",
        headers={"Authorization": "Bearer arm-teste"},
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


def _subs_page(*subs: str, next_link: Optional[str] = None) -> Dict[str, Any]:
    page: Dict[str, Any] = {"value": [{"subscriptionId": s, "state": "Enabled"} for s in subs]}
    if next_link:
        page["nextLink"] = next_link
    return page


def _activity_calls(m) -> List[Tuple[str, Dict[str, Any]]]:
    out = []
    for (method, url), calls in m.requests.items():
        if method == "GET" and "eventtypes" in url.path:
            out.extend((url.path, dict(url.query)) for _ in calls)
    return out


async def _collect(collector) -> List[Dict[str, Any]]:
    return [ev async for ev in collector.collect()]


# ══ Activity Log ═══════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_reads_every_visible_subscription_and_overlaps_the_next_window() -> None:
    ev_a = F.activity("ev-a", _ago(5), sub=F.SUB_A)
    ev_b = F.activity("ev-b", _ago(30), sub=F.SUB_B)
    with aioresponses() as m:
        m.get(_SUBS, payload=_subs_page(F.SUB_B, F.SUB_A))
        m.get(_activity_url(F.SUB_A), payload={"value": [ev_a]})
        m.get(_activity_url(F.SUB_B), payload={"value": [ev_b]})
        async with aiohttp.ClientSession() as session:
            collector = A.AzureActivityLogCollector(_ctx(session))
            events = await _collect(collector)

    assert sorted(e["eventDataId"] for e in events) == ["ev-a", "ev-b"]
    filters = [q["$filter"] for _, q in _activity_calls(m)]
    assert len(filters) == 2 and filters[0] == filters[1], "as duas assinaturas leem a MESMA janela"
    assert filters[0].startswith("eventTimestamp ge '") and " and eventTimestamp le '" in filters[0]

    cursor = collector.ctx.cursor
    nxt = datetime.fromisoformat(cursor["eventTimestamp"].replace("Z", "+00:00"))
    # A próxima janela recomeça 20 min antes do fim desta (sobreposição).
    assert abs((datetime.now(timezone.utc) - nxt) - A._LATE_ARRIVAL) < timedelta(seconds=30)
    # Só o evento DENTRO da sobreposição fica na memória da fronteira.
    assert cursor["boundary"] == {"at": cursor["eventTimestamp"], "keys": ["ev-a"]}
    assert not collector.ctx.hit_cycle_cap


@pytest.mark.asyncio
async def test_overlap_never_reemits_what_a_previous_cycle_sent() -> None:
    ev = F.activity("ev-dup", _ago(3))
    late = F.activity("ev-late", _ago(4), submitted=_ago(1))
    with aioresponses() as m:
        m.get(_SUBS, payload=_subs_page(F.SUB_A))
        m.get(_activity_url(F.SUB_A), payload={"value": [ev]})
        m.get(_SUBS, payload=_subs_page(F.SUB_A))
        # Segundo ciclo: o mesmo evento volta pela sobreposição + um atrasado novo.
        m.get(_activity_url(F.SUB_A), payload={"value": [late, ev]})
        async with aiohttp.ClientSession() as session:
            first = A.AzureActivityLogCollector(_ctx(session))
            assert [e["eventDataId"] for e in await _collect(first)] == ["ev-dup"]
            second = A.AzureActivityLogCollector(_ctx(session, first.ctx.cursor))
            assert [e["eventDataId"] for e in await _collect(second)] == ["ev-late"]


@pytest.mark.asyncio
async def test_cap_keeps_the_window_and_resumes_on_the_next_cycle(monkeypatch) -> None:
    monkeypatch.setattr(A, "_MAX_PAGES_PER_CYCLE", 1)
    start = _ago(60)
    with aioresponses() as m:
        m.get(_SUBS, payload=_subs_page(F.SUB_A, F.SUB_B))
        m.get(_activity_url(F.SUB_A), payload={"value": [F.activity("a1", _ago(50))]})
        m.get(_activity_url(F.SUB_B), payload={"value": [F.activity("b1", _ago(40))]})
        async with aiohttp.ClientSession() as session:
            first = A.AzureActivityLogCollector(_ctx(session, {"eventTimestamp": start}))
            assert [e["eventDataId"] for e in await _collect(first)] == ["a1"]
            capped = first.ctx.cursor
            assert first.ctx.hit_cycle_cap is True
            assert capped["eventTimestamp"] == start, "no teto a janela NÃO anda"
            assert capped["pending"] == [F.SUB_B] and capped["nextLink"] is None
            # O próximo ciclo roda minutos depois: o fim da janela é o GRAVADO,
            # não o relógio de quando ele roda.
            capped = {**capped, "window_end": _ago(45)}
            window_end = capped["window_end"]

            # Retomada: não relista assinaturas e usa o MESMO fim de janela.
            second = A.AzureActivityLogCollector(_ctx(session, capped))
            assert [e["eventDataId"] for e in await _collect(second)] == ["b1"]

    assert len(m.requests[("GET", URL(_SUBS))]) == 1
    b_filter = [q["$filter"] for p, q in _activity_calls(m) if F.SUB_B in p][0]
    assert b_filter == f"eventTimestamp ge '{start}' and eventTimestamp le '{window_end}'"
    assert second.ctx.hit_cycle_cap is False
    # Janela de 15 min < sobreposição de 20: a próxima recomeça no MESMO início,
    # nunca antes dele (fim − 20 min cairia antes).
    assert second.ctx.cursor["eventTimestamp"] == start


@pytest.mark.asyncio
async def test_cap_mid_subscription_resumes_on_its_next_link(monkeypatch) -> None:
    monkeypatch.setattr(A, "_MAX_PAGES_PER_CYCLE", 1)
    link = f"https://management.azure.com/subscriptions/{F.SUB_A}/providers/Microsoft.Insights/eventtypes/management/values?api-version=2015-04-01&$skiptoken=abc"
    with aioresponses() as m:
        m.get(_SUBS, payload=_subs_page(F.SUB_A))
        m.get(_activity_url(F.SUB_A), payload={"value": [F.activity("p1", _ago(50))], "nextLink": link})
        m.get(link, payload={"value": [F.activity("p2", _ago(49))]})
        async with aiohttp.ClientSession() as session:
            first = A.AzureActivityLogCollector(_ctx(session, {"eventTimestamp": _ago(60)}))
            await _collect(first)
            assert first.ctx.cursor["pending"] == [F.SUB_A]
            assert first.ctx.cursor["nextLink"] == link
            second = A.AzureActivityLogCollector(_ctx(session, first.ctx.cursor))
            assert [e["eventDataId"] for e in await _collect(second)] == ["p2"]


@pytest.mark.asyncio
async def test_expired_next_link_restarts_the_subscription_from_the_window() -> None:
    link = f"https://management.azure.com/subscriptions/{F.SUB_A}/providers/Microsoft.Insights/eventtypes/management/values?$skiptoken=velho"
    cursor = {
        "eventTimestamp": _ago(60), "window_end": _ago(55), "pending": [F.SUB_A], "nextLink": link,
    }
    with aioresponses() as m:
        m.get(link, status=400, body="skiptoken expired")
        m.get(_activity_url(F.SUB_A), payload={"value": [F.activity("r1", _ago(58))]})
        async with aiohttp.ClientSession() as session:
            collector = A.AzureActivityLogCollector(_ctx(session, cursor))
            assert [e["eventDataId"] for e in await _collect(collector)] == ["r1"]


@pytest.mark.asyncio
async def test_next_link_outside_arm_is_refused() -> None:
    with aioresponses() as m:
        m.get(_SUBS, payload=_subs_page(F.SUB_A))
        m.get(_activity_url(F.SUB_A), payload={"value": [], "nextLink": "https://coletor.example.net/roubar"})
        async with aiohttp.ClientSession() as session:
            with pytest.raises(RuntimeError, match="management.azure.com"):
                await _collect(A.AzureActivityLogCollector(_ctx(session)))
    assert not any("example.net" in str(url) for _, url in m.requests), "o bearer não pode sair do ARM"


@pytest.mark.asyncio
async def test_tampered_cursor_next_link_is_not_followed() -> None:
    cursor = {
        "eventTimestamp": _ago(60), "window_end": _ago(55), "pending": [F.SUB_A],
        "nextLink": "https://coletor.example.net/roubar",
    }
    with aioresponses() as m:
        m.get(_activity_url(F.SUB_A), payload={"value": []})
        async with aiohttp.ClientSession() as session:
            await _collect(A.AzureActivityLogCollector(_ctx(session, cursor)))
    assert not any("example.net" in str(url) for _, url in m.requests)


@pytest.mark.asyncio
async def test_forbidden_subscription_is_skipped_and_the_others_continue(caplog) -> None:
    with aioresponses() as m:
        m.get(_SUBS, payload=_subs_page(F.SUB_A, F.SUB_B))
        m.get(_activity_url(F.SUB_A), status=403, payload={"error": {"code": "AuthorizationFailed"}})
        m.get(_activity_url(F.SUB_B), payload={"value": [F.activity("b-ok", _ago(5), sub=F.SUB_B)]})
        async with aiohttp.ClientSession() as session:
            with caplog.at_level(logging.WARNING):
                events = await _collect(A.AzureActivityLogCollector(_ctx(session)))
    assert [e["eventDataId"] for e in events] == ["b-ok"]
    assert "assinatura pulada" in caplog.text


@pytest.mark.asyncio
async def test_unauthorized_surfaces_as_401_for_the_pipeline() -> None:
    with aioresponses() as m:
        m.get(_SUBS, status=401, payload={"error": {"code": "InvalidAuthenticationToken"}})
        async with aiohttp.ClientSession() as session:
            with pytest.raises(aiohttp.ClientResponseError) as exc:
                await _collect(A.AzureActivityLogCollector(_ctx(session)))
    assert exc.value.status == 401


@pytest.mark.asyncio
async def test_throttled_arm_raises_the_vendor_429() -> None:
    with aioresponses() as m:
        m.get(_SUBS, payload=_subs_page(F.SUB_A))
        m.get(_activity_url(F.SUB_A), status=429, headers={"Retry-After": "17"})
        async with aiohttp.ClientSession() as session:
            with pytest.raises(A.AzureRateLimitedError) as exc:
                await _collect(A.AzureActivityLogCollector(_ctx(session)))
    assert exc.value.retry_after == 17


@pytest.mark.asyncio
async def test_no_visible_subscription_is_a_warned_empty_cycle(caplog) -> None:
    with aioresponses() as m:
        m.get(_SUBS, payload={"value": []})
        async with aiohttp.ClientSession() as session:
            with caplog.at_level(logging.WARNING):
                collector = A.AzureActivityLogCollector(_ctx(session))
                assert await _collect(collector) == []
    assert "Reader" in caplog.text
    assert "eventTimestamp" in collector.ctx.cursor


@pytest.mark.asyncio
async def test_cursor_older_than_retention_is_clamped(caplog) -> None:
    with aioresponses() as m:
        m.get(_SUBS, payload=_subs_page(F.SUB_A))
        m.get(_activity_url(F.SUB_A), payload={"value": []})
        async with aiohttp.ClientSession() as session:
            with caplog.at_level(logging.WARNING):
                await _collect(A.AzureActivityLogCollector(_ctx(session, {"eventTimestamp": "2025-01-01T00:00:00Z"})))
    flt = _activity_calls(m)[0][1]["$filter"]
    start = datetime.fromisoformat(flt.split("'")[1].replace("Z", "+00:00"))
    assert datetime.now(timezone.utc) - start <= A._ACTIVITY_RETENTION + timedelta(seconds=30)
    assert "retenção" in caplog.text


@pytest.mark.asyncio
async def test_backfill_reads_exactly_the_requested_window_without_a_cap(monkeypatch) -> None:
    monkeypatch.setattr(A, "_MAX_PAGES_PER_CYCLE", 1)
    frm, to = _ago(600), _ago(300)
    cursor = {"backfill_from_ts": frm.replace("Z", "+00:00"), "backfill_to_ts": to.replace("Z", "+00:00")}
    link = f"https://management.azure.com/subscriptions/{F.SUB_A}/next?$skiptoken=1"
    with aioresponses() as m:
        m.get(_SUBS, payload=_subs_page(F.SUB_A))
        m.get(_activity_url(F.SUB_A), payload={"value": [F.activity("bf1", _ago(500))], "nextLink": link})
        m.get(link, payload={"value": [F.activity("bf2", _ago(400))]})
        async with aiohttp.ClientSession() as session:
            collector = A.AzureActivityLogCollector(_ctx(session, dict(cursor), bounded=False))
            events = await _collect(collector)
    assert [e["eventDataId"] for e in events] == ["bf1", "bf2"]
    assert _activity_calls(m)[0][1]["$filter"] == f"eventTimestamp ge '{frm}' and eventTimestamp le '{to}'"
    assert collector.ctx.cursor == cursor, "backfill não mexe no cursor do polling"


@pytest.mark.asyncio
async def test_event_later_than_the_overlap_is_reported(caplog) -> None:
    late = F.activity("tarde", _ago(45), submitted=_ago(1))
    with aioresponses() as m:
        m.get(_SUBS, payload=_subs_page(F.SUB_A))
        m.get(_activity_url(F.SUB_A), payload={"value": [late]})
        async with aiohttp.ClientSession() as session:
            with caplog.at_level(logging.WARNING):
                await _collect(A.AzureActivityLogCollector(_ctx(session)))
    assert "podem não ser lidos" in caplog.text


def test_activity_message_id_is_the_event_data_id() -> None:
    collector = A.AzureActivityLogCollector(_ctx(MagicMock()))
    assert collector.extract_message_id(F.ROLE_ASSIGNMENT) == F.ROLE_ASSIGNMENT["eventDataId"]


# ══ Event Hubs (Kafka) ═════════════════════════════════════════════════


class _Msg:
    def __init__(self, offset: int, value: bytes, timestamp: int) -> None:
        self.offset, self.value, self.timestamp = offset, value, timestamp


class _FakeConsumer:
    """Dublê do AIOKafkaConsumer: hubs → partição → mensagens (offset = posição)."""

    def __init__(
        self,
        hubs: Dict[str, Dict[int, List[_Msg]]],
        *,
        first: Optional[Dict[int, int]] = None,
        fail_on_fetch: bool = False,
    ) -> None:
        self.hubs = hubs
        self.first = first or {}
        self.fail_on_fetch = fail_on_fetch
        self.pos: Dict[TopicPartition, int] = {}
        self.started = self.stopped = False
        self.assigned: List[TopicPartition] = []

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def topics(self):
        return set(self.hubs)

    def partitions_for_topic(self, topic):
        return set(self.hubs.get(topic, {})) or None

    def assign(self, tps) -> None:
        self.assigned = list(tps)

    async def beginning_offsets(self, tps):
        return {tp: self.first.get(tp.partition, 0) for tp in tps}

    async def end_offsets(self, tps):
        out = {}
        for tp in tps:
            msgs = self.hubs[tp.topic][tp.partition]
            out[tp] = (msgs[-1].offset + 1) if msgs else self.first.get(tp.partition, 0)
        return out

    def seek(self, tp, offset) -> None:
        self.pos[tp] = offset

    async def getmany(self, *tps, timeout_ms=0, max_records=None):
        if self.fail_on_fetch:
            raise RuntimeError("broker caiu")
        out = {}
        for tp in tps:
            msgs = [m for m in self.hubs[tp.topic][tp.partition] if m.offset >= self.pos[tp]]
            msgs = msgs[: (max_records or len(msgs))]
            if msgs:
                out[tp] = msgs
                self.pos[tp] = msgs[-1].offset + 1
        return out


_T0 = int(datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)


def _hub(*bodies: List[Dict[str, Any]], start: int = 0) -> List[_Msg]:
    return [
        _Msg(start + i, F.eventhub_body(records), _T0 + (start + i) * 1000)
        for i, records in enumerate(bodies)
    ]


@pytest.fixture
def hub_env(monkeypatch):
    """Configuração da integração + consumer dublê; devolve o setter do dublê."""
    state: Dict[str, Any] = {"base_url": "ns-seguranca", "consumer": None, "built": []}

    def _settings(integration_id):
        return {
            "tenant_id": F.TENANT, "client_id": F.CLIENT,
            "client_secret": "segredo-de-teste", "base_url": state["base_url"],
        }

    def _factory(fqdn, provider):
        state["built"].append((fqdn, provider))
        return state["consumer"]

    monkeypatch.setattr(A, "_load_settings", _settings)
    monkeypatch.setattr(A, "_make_consumer", _factory)
    return state


@pytest.mark.asyncio
async def test_first_cycle_reads_the_retention_and_records_every_offset(hub_env) -> None:
    hub_env["consumer"] = consumer = _FakeConsumer({
        "centralops-entra": {
            0: _hub([F.SIGNIN_OK, F.NONINTERACTIVE], [F.SP_SIGNIN]),
            1: _hub([F.ADD_ROLE_MEMBER]),
        }
    })
    collector = A.AzureEventHubEntraCollector(_ctx(MagicMock()))
    events = await _collect(collector)

    assert [e["properties"]["id"] for e in events] == ["sid-0001", "sid-0003", "sid-sp-0001", "aud-0001"]
    assert collector.ctx.cursor["offsets"] == {"0": 2, "1": 1}
    wm = datetime.fromisoformat(collector.ctx.cursor["watermark"].replace("Z", "+00:00"))
    assert datetime.now(timezone.utc) - wm < timedelta(seconds=30), "hub lido até o fim → watermark = agora"
    assert consumer.stopped and not collector.ctx.hit_cycle_cap
    assert hub_env["built"][0][0] == "ns-seguranca.servicebus.windows.net"


@pytest.mark.asyncio
async def test_resumes_exactly_at_the_saved_offsets(hub_env) -> None:
    hub_env["consumer"] = _FakeConsumer({
        "centralops-entra": {0: _hub([F.SIGNIN_OK], [F.SIGNIN_FAILED_RISKY], [F.NONINTERACTIVE])}
    })
    collector = A.AzureEventHubEntraCollector(_ctx(MagicMock(), {"offsets": {"0": 2}}))
    events = await _collect(collector)
    assert [e["properties"]["id"] for e in events] == ["sid-0003"]
    assert collector.ctx.cursor["offsets"] == {"0": 3}


@pytest.mark.asyncio
async def test_offsets_only_move_in_the_cursor_the_pipeline_saves(hub_env) -> None:
    """Sem commit no Kafka: o que garante at-least-once é o cursor gravado DEPOIS
    do despacho. Ciclo abortado = cursor antigo = mesma leitura de novo."""
    hub_env["consumer"] = _FakeConsumer({"centralops-entra": {0: _hub([F.SIGNIN_OK])}})
    collector = A.AzureEventHubEntraCollector(_ctx(MagicMock(), {"offsets": {"0": 0}}))
    gen = collector.collect()
    first = await gen.__anext__()
    assert first["properties"]["id"] == "sid-0001"
    # No meio do ciclo (evento entregue, ainda não despachado) o cursor é o antigo.
    assert collector.ctx.cursor == {"offsets": {"0": 0}}
    await gen.aclose()
    assert hub_env["consumer"].stopped
    for hook in (collector.on_cycle_committed, collector.on_cycle_aborted):
        await hook()  # no-op: não há ack para dar nem perder


@pytest.mark.asyncio
async def test_retention_gap_is_an_error_not_silence(hub_env, caplog) -> None:
    hub_env["consumer"] = _FakeConsumer(
        {"centralops-resource": {0: _hub([F.KEYVAULT_SECRET_GET], start=10)}}, first={0: 10}
    )
    collector = A.AzureEventHubResourceCollector(_ctx(MagicMock(), {"offsets": {"0": 4}}))
    with caplog.at_level(logging.ERROR):
        events = await _collect(collector)
    assert len(events) == 1 and collector.ctx.cursor["offsets"] == {"0": 11}
    assert "perdeu 6 mensagem(ns) por retenção" in caplog.text


@pytest.mark.asyncio
async def test_recreated_hub_rereads_from_the_beginning(hub_env, caplog) -> None:
    hub_env["consumer"] = _FakeConsumer({"centralops-resource": {0: _hub([F.KEYVAULT_SECRET_GET])}})
    collector = A.AzureEventHubResourceCollector(_ctx(MagicMock(), {"offsets": {"0": 900}}))
    with caplog.at_level(logging.WARNING):
        assert len(await _collect(collector)) == 1
    assert "hub recriado" in caplog.text


@pytest.mark.asyncio
async def test_missing_hub_turns_the_stream_off(hub_env) -> None:
    hub_env["consumer"] = consumer = _FakeConsumer({"outro-hub": {0: []}})
    collector = A.AzureEventHubEntraCollector(_ctx(MagicMock(), {"offsets": {"0": 7}}))
    assert await _collect(collector) == []
    assert collector.ctx.cursor == {"offsets": {"0": 7}}, "hub ausente não zera a posição"
    assert consumer.stopped


@pytest.mark.asyncio
async def test_without_a_namespace_nothing_connects(hub_env) -> None:
    hub_env["base_url"] = ""
    collector = A.AzureEventHubEntraCollector(_ctx(MagicMock()))
    assert await _collect(collector) == []
    assert hub_env["built"] == []


@pytest.mark.asyncio
async def test_namespace_outside_servicebus_never_connects(hub_env) -> None:
    hub_env["base_url"] = "kafka.example.net"
    with pytest.raises(ValueError, match="namespace"):
        await _collect(A.AzureEventHubEntraCollector(_ctx(MagicMock())))
    assert hub_env["built"] == []


@pytest.mark.asyncio
async def test_cap_stops_the_cycle_and_signals_backlog(hub_env, monkeypatch) -> None:
    monkeypatch.setattr(A, "_MAX_MESSAGES_PER_CYCLE", 1)
    monkeypatch.setattr(A, "_FETCH_MAX_RECORDS", 1)
    hub_env["consumer"] = _FakeConsumer({"centralops-entra": {0: _hub([F.SIGNIN_OK], [F.NONINTERACTIVE])}})
    collector = A.AzureEventHubEntraCollector(_ctx(MagicMock()))
    events = await _collect(collector)
    assert len(events) == 1
    assert collector.ctx.hit_cycle_cap is True
    assert collector.ctx.cursor == {"offsets": {"0": 1}, "watermark": "2026-10-27T12:00:00Z"}


@pytest.mark.asyncio
async def test_unbounded_run_drains_past_the_cap(hub_env, monkeypatch) -> None:
    monkeypatch.setattr(A, "_MAX_MESSAGES_PER_CYCLE", 1)
    monkeypatch.setattr(A, "_FETCH_MAX_RECORDS", 1)
    hub_env["consumer"] = _FakeConsumer({"centralops-entra": {0: _hub([F.SIGNIN_OK], [F.NONINTERACTIVE])}})
    collector = A.AzureEventHubEntraCollector(_ctx(MagicMock(), bounded=False))
    assert len(await _collect(collector)) == 2
    assert not collector.ctx.hit_cycle_cap


@pytest.mark.asyncio
async def test_consumer_is_closed_when_the_fetch_fails(hub_env) -> None:
    hub_env["consumer"] = consumer = _FakeConsumer(
        {"centralops-entra": {0: _hub([F.SIGNIN_OK])}}, fail_on_fetch=True
    )
    collector = A.AzureEventHubEntraCollector(_ctx(MagicMock(), {"offsets": {"0": 0}}))
    with pytest.raises(RuntimeError, match="broker caiu"):
        await _collect(collector)
    assert consumer.stopped and collector.ctx.cursor == {"offsets": {"0": 0}}


@pytest.mark.asyncio
async def test_consumer_is_closed_when_the_handshake_fails(hub_env) -> None:
    class _RefusedConsumer(_FakeConsumer):
        async def start(self) -> None:
            raise ConnectionError("handshake SASL recusado")

    hub_env["consumer"] = consumer = _RefusedConsumer({})
    with pytest.raises(ConnectionError):
        await _collect(A.AzureEventHubEntraCollector(_ctx(MagicMock())))
    assert consumer.stopped


@pytest.mark.asyncio
async def test_each_stream_reads_its_own_hub(hub_env) -> None:
    hub_env["consumer"] = _FakeConsumer({
        "centralops-entra": {0: _hub([F.SIGNIN_OK])},
        "centralops-resource": {0: _hub([F.KEYVAULT_SECRET_GET])},
    })
    entra = await _collect(A.AzureEventHubEntraCollector(_ctx(MagicMock())))
    hub_env["consumer"].pos.clear()
    resource = await _collect(A.AzureEventHubResourceCollector(_ctx(MagicMock())))
    assert [e["category"] for e in entra] == ["SignInLogs"]
    assert [e["category"] for e in resource] == ["AuditEvent"]


@pytest.mark.asyncio
async def test_backfill_window_never_touches_the_hub(hub_env) -> None:
    hub_env["consumer"] = _FakeConsumer({"centralops-entra": {0: _hub([F.SIGNIN_OK])}})
    collector = A.AzureEventHubEntraCollector(_ctx(MagicMock(), {"backfill_from_ts": _ago(60)}))
    assert await _collect(collector) == []
    assert hub_env["built"] == []


def test_records_envelope_is_unwrapped_and_aks_log_decoded() -> None:
    records = A._decode_records(F.eventhub_body([F.AKS_EXEC, F.KEYVAULT_SECRET_GET]))
    assert [r["category"] for r in records] == ["kube-audit", "AuditEvent"]
    assert records[0]["properties"]["log"]["verb"] == "create"
    # O fixture original não é mutado pela decodificação.
    assert isinstance(F.AKS_EXEC["properties"]["log"], str)


def test_unexpected_bodies_are_kept_not_dropped() -> None:
    assert A._decode_records(b"texto solto") == [{"message": "texto solto"}]
    assert A._decode_records(b'{"category": "X"}') == [{"category": "X"}]
    assert A._decode_records(b"[1, 2]") == [{"message": "[1, 2]"}]
    assert A._decode_records(None) == []
    bad_kube = {**F.AKS_EXEC, "properties": {"log": "{não-json"}}
    assert A._decode_records(F.eventhub_body([bad_kube]))[0]["properties"]["log"] == "{não-json"


def test_message_ids() -> None:
    entra = A.AzureEventHubEntraCollector(_ctx(MagicMock()))
    assert entra.extract_message_id(F.SIGNIN_OK) == "SignInLogs|sid-0001"
    assert entra.extract_message_id({"category": "X"}) == ""
    resource = A.AzureEventHubResourceCollector(_ctx(MagicMock()))
    assert resource.extract_message_id(F.KEYVAULT_SECRET_GET) == ""


# ══ Credencial e superfície de rede ════════════════════════════════════


@pytest.mark.parametrize(
    "raw,fqdn",
    [
        ("ns-seguranca", "ns-seguranca.servicebus.windows.net"),
        ("NS-Seguranca.servicebus.windows.net", "ns-seguranca.servicebus.windows.net"),
        ("sb://ns-seguranca.servicebus.windows.net/", "ns-seguranca.servicebus.windows.net"),
        ("ns-seguranca.servicebus.windows.net:9093", "ns-seguranca.servicebus.windows.net"),
        ("", None),
        (None, None),
    ],
)
def test_namespace_forms_the_portal_shows_are_accepted(raw, fqdn) -> None:
    assert A.eventhub_fqdn(raw) == fqdn


@pytest.mark.parametrize(
    "raw",
    [
        "kafka.example.net",
        "ns-seguranca.servicebus.windows.net.example.net",
        "ns-seguranca.example.net/.servicebus.windows.net",
        "user@ns-seguranca.servicebus.windows.net",
        "http://169.254.169.254",
        "ns",
        "../ns-seguranca",
        "ns-seguranca.servicebus.usgovcloudapi.net",
    ],
)
def test_namespace_outside_the_public_servicebus_is_refused(raw) -> None:
    with pytest.raises(ValueError):
        A.eventhub_fqdn(raw)


@pytest.mark.parametrize("tenant", ["../common", "a/b", "contoso", "", "x" * 300, "abc?x=1"])
def test_tenant_cannot_escape_the_token_path(tenant) -> None:
    with pytest.raises(ValueError):
        A.validated_tenant(tenant)


def test_tenant_accepts_guid_and_verified_domain() -> None:
    assert A.validated_tenant(F.TENANT) == F.TENANT
    assert A.validated_tenant("exemplo.onmicrosoft.com") == "exemplo.onmicrosoft.com"


@pytest.mark.parametrize(
    "url,ok",
    [
        ("https://management.azure.com/subscriptions/x?$skiptoken=1", True),
        ("https://management.azure.com:443/x", True),
        ("http://management.azure.com/x", False),
        ("https://management.azure.com.example.net/x", False),
        ("https://management.azure.com:8443/x", False),
        ("https://user@example.net/x", False),
        ("not a url", False),
    ],
)
def test_arm_link_allowlist(url, ok) -> None:
    assert A._arm_url_allowed(url) is ok


@pytest.mark.asyncio
async def test_token_error_carries_only_the_aad_code_and_never_redirects() -> None:
    body = {
        "error": "invalid_client",
        "error_description": "AADSTS7000215: Invalid client secret provided. Trace ID: 123 detalhe-interno",
        "error_codes": [7000215],
    }
    with aioresponses() as m:
        m.post(_TOKEN, status=401, payload=body)
        async with aiohttp.ClientSession() as session:
            with pytest.raises(A.AzureTokenError) as exc:
                await A._client_credentials_token(session, F.TENANT, F.CLIENT, "segredo-de-teste", A._ARM_SCOPE)
    assert exc.value.status == 401
    assert exc.value.reason_code == "invalid_client (AADSTS7000215)"
    assert "detalhe-interno" not in str(exc.value) and "segredo-de-teste" not in str(exc.value)
    call = m.requests[("POST", URL(_TOKEN))][0]
    assert call.kwargs["allow_redirects"] is False


@pytest.mark.asyncio
async def test_refresher_asks_the_arm_audience(monkeypatch) -> None:
    monkeypatch.setattr(A, "_load_settings", lambda _id: {
        "tenant_id": F.TENANT, "client_id": F.CLIENT, "client_secret": "s", "base_url": "",
    })
    with aioresponses() as m:
        m.post(_TOKEN, payload={"access_token": "arm-tok", "expires_in": 3599})
        assert await A._arm_refresher(91) == {"access_token": "arm-tok", "expires_in": 3599}
    data = m.requests[("POST", URL(_TOKEN))][0].kwargs["data"]
    assert data["scope"] == "https://management.azure.com/.default"
    assert data["grant_type"] == "client_credentials"


@pytest.mark.asyncio
async def test_event_hub_token_audience_is_the_namespace_itself() -> None:
    provider = A._EventHubTokenProvider(F.TENANT, F.CLIENT, "s", "ns-seguranca.servicebus.windows.net")
    with aioresponses() as m:
        m.post(_TOKEN, payload={"access_token": "eh-tok", "expires_in": 3599})
        assert await provider.token() == "eh-tok"
        assert await provider.token() == "eh-tok"  # cache: um POST só
    calls = m.requests[("POST", URL(_TOKEN))]
    assert len(calls) == 1
    assert calls[0].kwargs["data"]["scope"] == "https://ns-seguranca.servicebus.windows.net/.default"


def test_aiokafka_adapter_is_a_real_token_provider() -> None:
    from aiokafka.abc import AbstractTokenProvider

    adapter = A._as_aiokafka_provider(A._EventHubTokenProvider(F.TENANT, F.CLIENT, "s", "x.servicebus.windows.net"))
    assert isinstance(adapter, AbstractTokenProvider)


# ══ Teste de conexão ═══════════════════════════════════════════════════

_CFG = {
    "tenant_id": F.TENANT, "client_id": F.CLIENT, "client_secret": "segredo-de-teste",
    "base_url": "ns-seguranca",
}


@pytest.fixture
def probe_hubs(monkeypatch):
    state: Dict[str, Any] = {"consumer": _FakeConsumer({"centralops-entra": {0: []}})}
    monkeypatch.setattr(A, "_make_consumer", lambda fqdn, provider: state["consumer"])
    return state


@pytest.mark.asyncio
async def test_probe_reports_each_stream(probe_hubs) -> None:
    with aioresponses() as m:
        m.post(_TOKEN, payload={"access_token": "t", "expires_in": 3599})
        m.get(_SUBS, payload=_subs_page(F.SUB_A, F.SUB_B))
        result = await A._azure_probe(dict(_CFG))
    assert result.ok, result.detail
    assert "activity: ok (2 assinatura(s))" in result.detail
    assert "entra: ok" in result.detail
    assert "resource: não existe (stream desligado)" in result.detail
    assert probe_hubs["consumer"].stopped


@pytest.mark.asyncio
async def test_probe_passes_with_activity_only_when_no_namespace() -> None:
    with aioresponses() as m:
        m.post(_TOKEN, payload={"access_token": "t", "expires_in": 3599})
        m.get(_SUBS, payload=_subs_page(F.SUB_A))
        result = await A._azure_probe({**_CFG, "base_url": ""})
    assert result.ok and "sem namespace" in result.detail


@pytest.mark.asyncio
async def test_probe_fails_when_nothing_is_usable(probe_hubs) -> None:
    probe_hubs["consumer"] = _FakeConsumer({})
    with aioresponses() as m:
        m.post(_TOKEN, payload={"access_token": "t", "expires_in": 3599})
        m.get(_SUBS, payload={"value": []})
        result = await A._azure_probe(dict(_CFG))
    assert not result.ok
    assert "nenhuma assinatura visível" in result.detail


@pytest.mark.asyncio
async def test_probe_turns_a_broker_failure_into_a_state(monkeypatch) -> None:
    def _boom(fqdn, provider):
        raise ConnectionError("porta 9093 bloqueada")

    monkeypatch.setattr(A, "_make_consumer", _boom)
    with aioresponses() as m:
        m.post(_TOKEN, payload={"access_token": "t", "expires_in": 3599})
        m.get(_SUBS, payload=_subs_page(F.SUB_A))
        result = await A._azure_probe(dict(_CFG))
    assert result.ok, "o Activity Log continua utilizável"
    assert "inacessível (ConnectionError)" in result.detail and "9093" in result.detail


@pytest.mark.asyncio
async def test_probe_reports_the_aad_code_not_the_body() -> None:
    with aioresponses() as m:
        m.post(_TOKEN, status=400, payload={
            "error": "unauthorized_client",
            "error_description": "AADSTS700016: Application not found. texto-do-corpo",
        })
        result = await A._azure_probe(dict(_CFG))
    assert not result.ok
    assert "AADSTS700016" in result.detail and "texto-do-corpo" not in result.detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cfg,motivo",
    [
        ({**_CFG, "tenant_id": ""}, "Informe"),
        ({**_CFG, "tenant_id": "../common"}, "Tenant ID"),
        ({**_CFG, "client_id": "nao-e-guid"}, "Client ID"),
        ({**_CFG, "base_url": "kafka.example.net"}, "namespace"),
    ],
)
async def test_probe_validates_input_before_the_network(cfg, motivo) -> None:
    with aioresponses() as m:
        result = await A._azure_probe(cfg)
    assert not result.ok and motivo in result.detail
    assert m.requests == {}


# ══ Registro ═══════════════════════════════════════════════════════════


def test_platform_and_streams_are_self_registered() -> None:
    plat = get_platform("azure")
    assert plat is not None and plat.category == "Cloud-audit" and plat.icon_id == "azure"
    assert plat.test_fn is A._azure_probe
    assert {f.key for f in plat.auth_fields} == {"tenant_id", "client_id", "client_secret", "base_url"}
    assert next(f for f in plat.auth_fields if f.key == "client_secret").type == "secret"
    assert supported_streams("azure") == ["activity", "entra", "resource"]
    assert {"collect:activity", "collect:entra", "collect:resource"} <= plat.capabilities
    for stream in ("activity", "entra", "resource"):
        reg = get("azure", stream)
        assert reg.refresh_fn is A._arm_refresher
        assert reg.collector_cls.event_type == f"azure.{stream}"


def test_editable_columns_cover_every_field() -> None:
    """O PUT vendor-neutro só edita estas colunas: campo fora delas seria
    criável e nunca mais editável (gotcha do CloudTrail)."""
    plat = get_platform("azure")
    assert {f.key for f in plat.auth_fields} <= {"tenant_id", "client_id", "client_secret", "base_url"}


# ══ Mappings ═══════════════════════════════════════════════════════════


def _normalize(event_type: str, raw: Dict[str, Any]) -> Dict[str, Any]:
    compiled = E.compile_rules(load_default_rules("azure", event_type))
    n = E.apply_compiled(compiled, copy.deepcopy(raw)).output["normalized"]
    r = V.validate_normalized(n)
    assert r.valid, (r.reason, n)
    return n


def _ident(n: Dict[str, Any]) -> Tuple[int, int, int]:
    return n["class_uid"], n["activity_id"], n["type_uid"]


def test_role_assignment_is_an_update_api_activity() -> None:
    n = _normalize("azure.activity", F.ROLE_ASSIGNMENT)
    assert _ident(n) == (6003, 3, 600303)
    assert (n["status_id"], n["status"], n["severity_id"]) == (1, "Success", 1)
    assert n["actor"]["user"]["email_addr"] == "ana.silva@example.com"
    assert n["actor"]["app_uid"] == "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
    assert n["src_endpoint"]["ip"] == "203.0.113.10"
    assert n["api"]["operation"] == "Microsoft.Authorization/roleAssignments/write"
    assert n["cloud"] == {"provider": "Azure", "account": {"uid": F.SUB_A}, "org": {"uid": F.TENANT}}
    assert n["time"] == 1793102400123
    assert n["metadata"]["uid"] == F.ROLE_ASSIGNMENT["eventDataId"]


def test_uppercase_list_keys_is_an_action() -> None:
    n = _normalize("azure.activity", F.STORAGE_LISTKEYS)
    assert _ident(n) == (6003, 99, 600399)
    assert n["api"]["service"]["name"] == "MICROSOFT.STORAGE"


def test_failed_delete_is_a_failure_with_error_severity() -> None:
    n = _normalize("azure.activity", F.FAILED_DELETE)
    assert _ident(n) == (6003, 4, 600304)
    assert (n["status_id"], n["status"], n["status_code"], n["severity_id"]) == (2, "Failure", "Forbidden", 3)


@pytest.mark.parametrize(
    "operation,ident",
    [
        ("Microsoft.Compute/virtualMachines/runCommand/action", (6003, 99, 600399)),
        ("MICROSOFT.COMPUTE/VIRTUALMACHINES/RUNCOMMAND/ACTION", (6003, 99, 600399)),
        ("Microsoft.Compute/virtualMachineScaleSets/virtualMachines/runCommand/action", (6003, 99, 600399)),
        ("Microsoft.Compute/virtualMachines/runCommands/write", (6003, 3, 600303)),
        ("Microsoft.HybridCompute/machines/runCommands/write", (6003, 3, 600303)),
        ("Microsoft.Compute/virtualMachines/extensions/write", (6003, 3, 600303)),
        ("Microsoft.ContainerService/managedClusters/runCommand/action", (6003, 99, 600399)),
    ],
)
def test_run_command_operations_keep_who_where_and_which_vm(operation, ident) -> None:
    """A tabela de Run Command da doc (microsoft-azure.md) promete isto."""
    raw = F.activity("rc-1", "2026-10-27T12:00:00Z", operation=operation)
    raw["resourceId"] = f"/subscriptions/{F.SUB_A}/resourceGroups/rg-app/providers/Microsoft.Compute/virtualMachines/vm-prod-01"
    n = _normalize("azure.activity", raw)
    assert _ident(n) == ident
    assert n["api"]["operation"] == operation
    assert n["actor"]["user"]["name"] == "ana.silva@example.com"
    assert n["src_endpoint"]["ip"] == "203.0.113.10"
    assert n["resources"][0]["uid"].endswith("/virtualMachines/vm-prod-01")


def test_started_operation_is_other_status() -> None:
    n = _normalize("azure.activity", F.activity("s1", "2026-10-27T12:00:00Z", status="Started"))
    assert (n["status_id"], n["status"]) == (99, "Other")


def test_failed_risky_signin_without_mfa() -> None:
    n = _normalize("azure.entra", F.SIGNIN_FAILED_RISKY)
    assert _ident(n) == (3002, 1, 300201)
    assert (n["status_id"], n["status"], n["status_code"]) == (2, "Failure", "50126")
    assert n["severity_id"] == 4 and n["is_mfa"] is False
    assert n["user"]["email_addr"] == "bruno.costa@example.com"
    assert n["src_endpoint"]["ip"] == "198.51.100.23"
    assert n["src_endpoint"]["location"] == {"city": "São Paulo", "country": "BR"}


@pytest.mark.parametrize("raw", [F.SIGNIN_OK, F.NONINTERACTIVE], ids=["interativo", "nao-interativo"])
def test_every_user_signin_is_a_successful_logon(raw) -> None:
    n = _normalize("azure.entra", raw)
    assert _ident(n) == (3002, 1, 300201) and n["status_id"] == 1 and n["is_mfa"] is True


def test_service_principal_signin_names_the_principal() -> None:
    n = _normalize("azure.entra", F.SP_SIGNIN)
    assert _ident(n) == (3002, 1, 300201)
    assert n["user"]["name"] == "ci-deploy"
    assert n["user"]["uid"] == "55555555-5555-4555-8555-555555555555"


@pytest.mark.parametrize(
    "raw,ident",
    [
        (F.ADD_ROLE_MEMBER, (3001, 7, 300107)),
        (F.RESET_PASSWORD, (3001, 4, 300104)),
        (F.entra_audit("Disable account", "Update"), (3001, 5, 300105)),
        (F.entra_audit("Add user"), (3001, 1, 300101)),
        (F.entra_audit("Delete user", "Delete"), (3001, 6, 300106)),
        (F.ADD_SP_CREDENTIAL, (6003, 3, 600303)),
        (F.entra_audit("Add application"), (6003, 1, 600301)),
        (F.entra_audit("Consent to application", "Assign"), (6003, 99, 600399)),
    ],
)
def test_entra_audit_routes_account_changes_and_api_activity(raw, ident) -> None:
    n = _normalize("azure.entra", raw)
    assert _ident(n) == ident
    assert n["actor"]["user"]["name"] == "ana.silva@example.com"


def test_account_change_user_is_the_target_not_the_admin() -> None:
    n = _normalize("azure.entra", F.ADD_ROLE_MEMBER)
    assert n["user"]["name"] == "bruno.costa@example.com"
    assert n["actor"]["user"]["name"] == "ana.silva@example.com"


def test_failed_audit_operation_is_a_failure() -> None:
    raw = F.entra_audit("Add member to role")
    raw["properties"]["result"] = "failure"
    assert _normalize("azure.entra", raw)["status_id"] == 2


def test_key_vault_secret_read_and_forbidden() -> None:
    ok = _normalize("azure.resource", F.KEYVAULT_SECRET_GET)
    assert _ident(ok) == (6003, 2, 600302) and ok["status_id"] == 1
    assert ok["actor"]["user"]["name"] == "ana.silva@example.com"
    denied = _normalize("azure.resource", F.KEYVAULT_FORBIDDEN)
    assert denied["status_id"] == 2 and denied["status_code"] == "403"


def test_kubectl_exec_on_aks() -> None:
    raw = A._decode_records(F.eventhub_body([F.AKS_EXEC]))[0]
    n = _normalize("azure.resource", raw)
    assert _ident(n) == (6003, 1, 600301)
    assert n["api"]["operation"] == "create pods exec"
    assert n["actor"]["user"]["name"] == "carla.dias@example.com"
    assert n["src_endpoint"]["ip"] == "198.51.100.77"
    assert n["metadata"]["uid"] == "f0000000-0000-4000-8000-000000000001"


def test_aks_forbidden_request_is_a_failure() -> None:
    raw = A._decode_records(F.eventhub_body([F.AKS_EXEC]))[0]
    raw["properties"]["log"]["responseStatus"]["code"] = 403
    assert _normalize("azure.resource", raw)["status_id"] == 2


def test_sql_failed_login_is_a_failed_authentication() -> None:
    n = _normalize("azure.resource", F.SQL_FAILED_LOGIN)
    assert _ident(n) == (3002, 1, 300201)
    assert (n["status_id"], n["user"]["name"], n["src_endpoint"]["ip"]) == (2, "app_user", "203.0.113.99")


def test_sql_batch_is_a_datastore_query() -> None:
    n = _normalize("azure.resource", F.SQL_BATCH)
    assert _ident(n) == (6005, 4, 600504)
    assert n["query_info"]["query_string"] == "GRANT CONTROL ON DATABASE::vendas TO app_user"
    assert n["database"]["name"] == "vendas"


def test_unknown_category_is_a_base_event_with_endpoints() -> None:
    n = _normalize("azure.resource", F.FIREWALL_RULE)
    assert _ident(n) == (0, 0, 0) and n["category_uid"] == 0
    assert n["src_endpoint"]["ip"] == "10.0.0.4" and n["dst_endpoint"] == {"ip": "192.0.2.80", "port": 443}


def test_storage_ip_with_port_is_not_an_ip() -> None:
    raw = {
        "time": "2026-10-27T13:10:00Z", "category": "StorageRead", "operationName": "GetBlob",
        "callerIpAddress": "203.0.113.5:51234", "statusCode": 200,
        "identity": {"type": "OAuth", "requester": {"upn": "ana.silva@example.com"}},
    }
    n = _normalize("azure.resource", raw)
    assert _ident(n) == (6003, 2, 600302)
    assert n["src_endpoint"]["ip"] is None
    assert n["actor"]["user"]["name"] == "ana.silva@example.com"


@pytest.mark.parametrize(
    "operation,ident",
    [
        ("SecretSet", (6003, 3, 600303)),
        ("KeyCreate", (6003, 1, 600301)),
        ("SecretPurge", (6003, 4, 600304)),
        ("KeyDecrypt", (6003, 99, 600399)),
        ("VaultGet", (6003, 2, 600302)),
    ],
)
def test_key_vault_operation_verbs(operation, ident) -> None:
    assert _ident(_normalize("azure.resource", {**F.KEYVAULT_SECRET_GET, "operationName": operation})) == ident


@pytest.mark.parametrize("event_type", ["azure.activity", "azure.entra", "azure.resource"])
def test_mapping_survives_a_record_with_nothing_in_it(event_type) -> None:
    """Registro mínimo (só o tempo) não pode quarentenar por expressão que levanta."""
    raw = {"time": "2026-10-27T13:00:00Z", "eventTimestamp": "2026-10-27T13:00:00Z"}
    compiled = E.compile_rules(load_default_rules("azure", event_type))
    n = E.apply_compiled(compiled, raw).output["normalized"]
    assert V.validate_normalized(n).valid
