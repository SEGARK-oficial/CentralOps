"""Akamai SIEM Integration API — credencial, assinatura, offset, 416, probe e mapping.

Os riscos que estes testes travam:

- **perder evento sem barulho** — offset que não avança, página cortada pela
  borda usada pela metade, teto sem cursor resumível, offset expirado que trava
  o feed (ou que volta além das 12 h de retenção sem dizer quanto se perdeu);
- **assinar para o host errado** — credencial com host fora de
  ``akamaiapis.net``, HTTP, redirect seguido;
- **vazar a credencial** — mensagem de erro/probe com o segredo ou o header
  ``Authorization``.

Tudo afirmado pelas chamadas que os dublês registram, nunca pelo texto do fonte.
"""

from __future__ import annotations

import base64
import copy
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import aiohttp
import pytest

from . import _akamai_fixtures as F
from ._aiohttp_mock import aioresponses
from ..base import CollectorContext
from ..normalize import engine as E
from ..normalize.defaults import load_default_rules
from ..normalize.ocsf import validator as V
from ..registry import get, get_platform, supported_streams
from ..vendors import akamai as A

_SIEM = re.compile(rf"^https://{re.escape(F.HOST)}/siem/v1/configs/")


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
        platform="akamai",
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
    state = {"credential": F.EDGERC, "config_ids": f"{F.CONFIG_A}, {F.CONFIG_B}"}
    monkeypatch.setattr(A, "_load_settings", lambda integration_id: dict(state))
    return state


def _calls(m) -> List[Dict[str, Any]]:
    """Cada GET na SIEM API: path, query e kwargs, na ordem em que saiu."""
    out = []
    for (method, url), calls in m.requests.items():
        if method == "GET" and "/siem/v1/configs/" in url.path:
            for call in calls:
                raw = str(call.args[1])
                parts = urlsplit(raw)
                out.append({
                    "raw": raw,
                    "path": parts.path,
                    "query": {k: v[0] for k, v in parse_qs(parts.query).items()},
                    "kwargs": call.kwargs,
                })
    return out


async def _collect(collector) -> List[Dict[str, Any]]:
    return [ev async for ev in collector.collect()]


def _now() -> int:
    return int(time.time())


# ══ Credencial ═════════════════════════════════════════════════════════


def test_edgerc_section_is_parsed_and_host_normalized() -> None:
    text = F.EDGERC + "\n[outra]\nhost = akab-zzz.luna.akamaiapis.net\nclient_token = x\n"
    cred = A.parse_edgegrid_credential(text)
    assert cred.host == F.HOST, "barra final tirada; só a PRIMEIRA seção vale"
    assert cred.client_secret == F.SECRET
    assert cred.access_token == "akab-atoken-0000000000000000"
    assert F.SECRET not in repr(cred)


def test_json_and_dashed_keys_are_accepted() -> None:
    cred = A.parse_edgegrid_credential(json.dumps({
        "host": f"https://{F.HOST}/", "client-token": "ct", "client_secret": "cs", "access_token": "at",
    }))
    assert (cred.host, cred.client_token, cred.client_secret, cred.access_token) == (F.HOST, "ct", "cs", "at")


@pytest.mark.parametrize("text", ["", "lixo", "[default]\nhost = x\n", "[1, 2]", "{nao json"])
def test_garbage_credential_is_refused(text: str) -> None:
    with pytest.raises(A.AkamaiCredentialError):
        A.parse_edgegrid_credential(text)


def test_missing_keys_are_named_without_values() -> None:
    with pytest.raises(A.AkamaiCredentialError) as exc:
        A.parse_edgegrid_credential(f"host = {F.HOST}\nclient_secret = {F.SECRET}\n")
    assert "client_token" in str(exc.value) and "access_token" in str(exc.value)
    assert F.SECRET not in str(exc.value)


@pytest.mark.parametrize(
    "host",
    [
        "http://akab-x.luna.akamaiapis.net",
        "evil.example.com",
        "akamaiapis.net.evil.example.com",
        "akab-x.luna.akamaiapis.net.evil.example.com",
        "akab-x.luna.akamaiapis.net:8443",
        "akab-x.luna.akamaiapis.net/siem",
        "user@akab-x.luna.akamaiapis.net",
        "akab-x.luna.akamaiapis.net@evil.example.com",
        "akamaiapis.net",
    ],
)
def test_host_outside_akamaiapis_is_refused(host: str) -> None:
    with pytest.raises(A.AkamaiCredentialError):
        A.validated_host(host)


@pytest.mark.parametrize(
    "host", ["akab-abc.luna.akamaiapis.net", "https://akab-abc.cloudsecurity.akamaiapis.net/"]
)
def test_akamai_hosts_are_accepted(host: str) -> None:
    assert A.validated_host(host).endswith(".akamaiapis.net")


def test_config_ids_accept_mixed_separators_and_dedupe() -> None:
    assert A.parse_config_ids("111, 222;333 111") == ["111", "222", "333"]
    for bad in ("", "abc", "111;../x", "1/2"):
        with pytest.raises(A.AkamaiCredentialError):
            A.parse_config_ids(bad)


# ══ Assinatura EdgeGrid ════════════════════════════════════════════════

_EG = A.EdgeGridCredential(F.EG_HOST, F.EG_CLIENT_TOKEN, F.EG_CLIENT_SECRET, F.EG_ACCESS_TOKEN)


@pytest.mark.parametrize(
    "path,signature",
    [
        # testcases.json do SDK oficial (github.com/akamai/AkamaiOPEN-edgegrid-python,
        # akamai/edgegrid/test/testcases.json): "simple GET" e "GET with querystring".
        ("/", "tL+y4hxyHxgWVD30X3pWnGKHcPzmrIF+LThiAOhMxYU="),
        ("/testapi/v1/t1?p1=1&p2=2", "hKDH1UlnQySSHjvIcZpDMbQHihTQ0XyVAKZaApabdeA="),
    ],
)
def test_signature_matches_the_official_sdk_vectors(path: str, signature: str) -> None:
    header = A.edgegrid_authorization(_EG, "GET", path, timestamp=F.EG_TIMESTAMP, nonce=F.EG_NONCE)
    assert header == (
        f"EG1-HMAC-SHA256 client_token={F.EG_CLIENT_TOKEN};access_token={F.EG_ACCESS_TOKEN};"
        f"timestamp={F.EG_TIMESTAMP};nonce={F.EG_NONCE};signature={signature}"
    )


# ══ Coleta ═════════════════════════════════════════════════════════════


def _resign(raw_url: str, header: str) -> str:
    """Refaz a assinatura com o timestamp/nonce do header — prova o que foi assinado."""
    fields = dict(kv.split("=", 1) for kv in header.split(" ", 1)[1].split(";") if "=" in kv)
    parts = urlsplit(raw_url)
    cred = A.parse_edgegrid_credential(F.EDGERC)
    return A.edgegrid_authorization(
        cred, "GET", f"{parts.path}?{parts.query}", timestamp=fields["timestamp"], nonce=fields["nonce"]
    )


@pytest.mark.asyncio
async def test_cold_start_reads_the_last_hour_then_stores_the_offset() -> None:
    start = _now() - 600
    with aioresponses() as m:
        m.get(_SIEM, body=F.body([F.event("r1", start), F.event("r2", start + 5)], offset="off-1"))
        async with aiohttp.ClientSession() as session:
            collector = A.AkamaiSiemCollector(_ctx(session))
            events = await _collect(collector)

    assert [e["httpMessage"]["requestId"] for e in events] == ["r1", "r2"]
    (call,) = _calls(m)
    assert call["path"] == f"/siem/v1/configs/{F.CONFIG_A};{F.CONFIG_B}", "uma chamada para as duas configurações"
    assert "offset" not in call["query"]
    assert abs(int(call["query"]["from"]) - (_now() - 3600)) < 30
    assert collector.ctx.cursor == {"offset": "off-1", "last_ts_ms": (start + 5) * 1000}
    assert not collector.ctx.hit_cycle_cap
    # Assinado para o host da credencial, sobre EXATAMENTE o path+query que saiu.
    header = call["kwargs"]["headers"]["Authorization"]
    assert header == _resign(call["raw"], header)
    assert call["kwargs"]["allow_redirects"] is False


@pytest.mark.asyncio
async def test_steady_state_uses_the_offset_and_follows_pages_while_limit_is_hit() -> None:
    t = _now() - 300
    with aioresponses() as m:
        m.get(_SIEM, body=F.body([F.event("p1", t)], offset="off-2", limit=5000))
        m.get(_SIEM, body=F.body([F.event("p2", t + 1)], offset="off-3", limit=5000))
        m.get(_SIEM, body=F.body([], offset="off-4"))
        async with aiohttp.ClientSession() as session:
            collector = A.AkamaiSiemCollector(_ctx(session, {"offset": "off-1", "last_ts_ms": 1}))
            events = await _collect(collector)

    assert [e["httpMessage"]["requestId"] for e in events] == ["p1", "p2"]
    offsets = [c["query"].get("offset") for c in _calls(m)]
    assert offsets == ["off-1", "off-2", "off-3"]
    assert all("from" not in c["query"] for c in _calls(m))
    assert collector.ctx.cursor == {"offset": "off-4", "last_ts_ms": (t + 1) * 1000}


@pytest.mark.asyncio
async def test_cap_stops_with_a_resumable_offset_and_says_so() -> None:
    t = _now() - 300
    with aioresponses() as m:
        for i in range(A._MAX_PAGES_PER_CYCLE + 3):
            m.get(_SIEM, body=F.body([F.event(f"c{i}", t + i)], offset=f"off-{i + 1}", limit=5000))
        async with aiohttp.ClientSession() as session:
            collector = A.AkamaiSiemCollector(_ctx(session, {"offset": "off-0"}))
            events = await _collect(collector)

    assert len(events) == A._MAX_PAGES_PER_CYCLE
    assert len(_calls(m)) == A._MAX_PAGES_PER_CYCLE
    assert collector.ctx.hit_cycle_cap
    # O próximo ciclo pede exatamente a página que ficou de fora.
    assert collector.ctx.cursor["offset"] == f"off-{A._MAX_PAGES_PER_CYCLE}"


@pytest.mark.asyncio
async def test_unbounded_run_drains_past_the_cap() -> None:
    t = _now() - 300
    with aioresponses() as m:
        for i in range(A._MAX_PAGES_PER_CYCLE + 2):
            m.get(_SIEM, body=F.body([F.event(f"u{i}", t)], offset=f"o{i}", limit=5000))
        m.get(_SIEM, body=F.body([], offset="fim"))
        async with aiohttp.ClientSession() as session:
            collector = A.AkamaiSiemCollector(_ctx(session, {"offset": "o"}, bounded=False))
            events = await _collect(collector)
    assert len(events) == A._MAX_PAGES_PER_CYCLE + 2
    assert not collector.ctx.hit_cycle_cap


@pytest.mark.asyncio
async def test_expired_offset_falls_back_to_time_mode_from_the_last_event() -> None:
    last = _now() - 1800
    with aioresponses() as m:
        m.get(_SIEM, status=416, payload={"title": "Invalid range request"})
        m.get(_SIEM, body=F.body([F.event("depois", last + 10)], offset="novo"))
        async with aiohttp.ClientSession() as session:
            collector = A.AkamaiSiemCollector(_ctx(session, {"offset": "morto", "last_ts_ms": last * 1000}))
            events = await _collect(collector)

    first, second = _calls(m)
    assert first["query"]["offset"] == "morto"
    assert "offset" not in second["query"] and int(second["query"]["from"]) == last
    assert [e["httpMessage"]["requestId"] for e in events] == ["depois"]
    assert collector.ctx.cursor == {"offset": "novo", "last_ts_ms": (last + 10) * 1000}


@pytest.mark.asyncio
async def test_expired_offset_beyond_retention_is_clamped_and_the_loss_is_logged(caplog) -> None:
    last = _now() - 20 * 3600
    with aioresponses() as m:
        m.get(_SIEM, status=416, payload={"title": "Invalid range request"})
        m.get(_SIEM, body=F.body([], offset="novo"))
        async with aiohttp.ClientSession() as session:
            collector = A.AkamaiSiemCollector(_ctx(session, {"offset": "morto", "last_ts_ms": last * 1000}))
            with caplog.at_level(logging.WARNING, logger=A.__name__):
                await _collect(collector)

    floor = _now() - int((A._RETENTION - A._RETENTION_MARGIN).total_seconds())
    assert abs(int(_calls(m)[1]["query"]["from"]) - floor) < 30, "nunca pede além das 12 h"
    assert any("guarda só 12 h" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_time_mode_refused_after_a_dead_offset_raises_instead_of_looping() -> None:
    with aioresponses() as m:
        m.get(_SIEM, status=416, payload={"title": "Invalid range request"}, repeat=True)
        async with aiohttp.ClientSession() as session:
            collector = A.AkamaiSiemCollector(_ctx(session, {"offset": "morto", "last_ts_ms": (_now() - 60) * 1000}))
            with pytest.raises(A.AkamaiApiError) as exc:
                await _collect(collector)
    assert exc.value.status == 416
    assert len(_calls(m)) == 2


@pytest.mark.asyncio
async def test_416_on_the_first_time_mode_call_is_a_real_error() -> None:
    with aioresponses() as m:
        m.get(_SIEM, status=416, payload={"title": "Invalid range request"})
        async with aiohttp.ClientSession() as session:
            with pytest.raises(A.AkamaiApiError):
                await _collect(A.AkamaiSiemCollector(_ctx(session)))


@pytest.mark.asyncio
async def test_truncated_response_is_not_used_and_is_retried_with_a_smaller_limit() -> None:
    t = _now() - 60
    cut = json.dumps(F.event("parcial", t)) + "\n" + json.dumps(F.event("cortado", t))[:40]
    with aioresponses() as m:
        m.get(_SIEM, body=cut)
        m.get(_SIEM, body=F.body([F.event("inteiro", t)], offset="o2"))
        async with aiohttp.ClientSession() as session:
            collector = A.AkamaiSiemCollector(_ctx(session, {"offset": "o1"}))
            events = await _collect(collector)

    assert [e["httpMessage"]["requestId"] for e in events] == ["inteiro"]
    first, second = _calls(m)
    assert second["query"]["offset"] == "o1", "repete a MESMA página"
    assert int(second["query"]["limit"]) < int(first["query"]["limit"])


@pytest.mark.asyncio
async def test_rate_limit_raises_with_retry_after() -> None:
    with aioresponses() as m:
        m.get(_SIEM, status=429, headers={"Retry-After": "17"}, payload={"title": "Too Many Requests"})
        async with aiohttp.ClientSession() as session:
            with pytest.raises(A.AkamaiRateLimitedError) as exc:
                await _collect(A.AkamaiSiemCollector(_ctx(session, {"offset": "o"})))
    assert exc.value.retry_after == 17


@pytest.mark.asyncio
async def test_auth_error_carries_only_the_problem_title() -> None:
    problem = {"type": "https://problems.example/auth", "title": "Not authorized", "detail": "ECHO " + F.SECRET}
    with aioresponses() as m:
        m.get(_SIEM, status=401, payload=problem)
        async with aiohttp.ClientSession() as session:
            with pytest.raises(A.AkamaiApiError) as exc:
                await _collect(A.AkamaiSiemCollector(_ctx(session, {"offset": "o"})))
    err = exc.value
    assert err.status == 401 and err.message == "Not authorized"
    text = f"{err} {err.request_info!r}"
    assert F.SECRET not in text and "EG1-HMAC" not in text and "ECHO" not in text


@pytest.mark.asyncio
async def test_redirect_is_never_followed() -> None:
    with aioresponses() as m:
        m.get(_SIEM, status=302, headers={"Location": "https://evil.example.com/x"})
        async with aiohttp.ClientSession() as session:
            with pytest.raises(A.AkamaiApiError):
                await _collect(A.AkamaiSiemCollector(_ctx(session, {"offset": "o"})))
    assert all("evil" not in str(url) for _, url in m.requests)
    assert _calls(m)[0]["kwargs"]["allow_redirects"] is False


@pytest.mark.asyncio
async def test_credential_with_foreign_host_never_reaches_the_network(_settings) -> None:
    _settings["credential"] = F.EDGERC.replace(F.HOST, "evil.example.com")
    with aioresponses() as m:
        async with aiohttp.ClientSession() as session:
            with pytest.raises(A.AkamaiCredentialError):
                await _collect(A.AkamaiSiemCollector(_ctx(session)))
    assert not m.requests


@pytest.mark.asyncio
async def test_backfill_reads_time_slices_and_bisects_a_full_slice() -> None:
    now = _now()
    t0 = now - 1800
    with aioresponses() as m:
        m.get(_SIEM, body=F.body([], offset="x", limit=5000))  # 1ª fatia cheia → bisseção
        m.get(_SIEM, body=F.body([F.event("b1", t0 + 10)], offset="x"), repeat=True)
        async with aiohttp.ClientSession() as session:
            cursor = {"backfill_from_ts": t0, "backfill_to_ts": now - 60}
            collector = A.AkamaiSiemCollector(_ctx(session, cursor))
            events = await _collect(collector)

    calls = _calls(m)
    assert all("offset" not in c["query"] and "to" in c["query"] for c in calls)
    first, second, third = calls[:3]
    assert (int(second["query"]["from"]), int(third["query"]["to"])) == (
        int(first["query"]["from"]), int(first["query"]["to"]),
    ), "a fatia cheia volta como duas metades"
    assert events and collector.ctx.cursor == cursor, "backfill não mexe no cursor do polling"


# ══ attackData ═════════════════════════════════════════════════════════


def test_rule_fields_are_url_then_base64_decoded_by_position() -> None:
    ev = A.decode_attack_data(copy.deepcopy(F.XSS_DENY))
    rules = ev["attackData"]["decoded_rules"]
    assert rules[0] == {
        "id": "950004", "version": "4", "message": "Cross-site Scripting (XSS) Attack",
        "tag": "WEB_ATTACK/XSS", "data": "<script>alert(1)</script>", "selector": "ARGS:q", "action": "deny",
    }
    assert rules[1]["selector"] == "", "seletor vazio mantém a posição"
    assert ev["httpMessage"]["decoded_user_agent"] == "Mozilla/5.0 (teste)"
    assert ev["attackData"]["rules"] == F.XSS_DENY["attackData"]["rules"], "o original fica"


def test_plus_in_base64_survives_url_decoding() -> None:
    # "\xfb\xef" em base64 é "++8=": com unquote_plus o '+' viraria espaço.
    raw = base64.b64encode(b"\xfb\xef\xbe").decode()
    assert "+" in raw
    assert A.decode_rule_field(raw) == [b"\xfb\xef\xbe".decode("utf-8", "replace")]
    assert A.decode_rule_field("T0s%3D%3BT0s%3D") == ["OK", "OK"]


def test_invalid_or_empty_chunks_keep_their_position() -> None:
    assert A.decode_rule_field("T0s=;;@@@;T0s=") == ["OK", "", "", "OK"]
    assert A.decode_rule_field(None) == [] and A.decode_rule_field("") == []


def test_rule_decoding_is_bounded() -> None:
    hostile = ";".join(["T0s="] * 10_000)
    assert len(A.decode_rule_field(hostile)) == A._MAX_RULES_PER_EVENT
    ev = {"attackData": {"rules": hostile}}
    assert len(A.decode_attack_data(ev)["attackData"]["decoded_rules"]) == A._MAX_RULES_PER_EVENT


def test_message_id_is_stable_and_separates_configs_and_rule_sets() -> None:
    c = A.AkamaiSiemCollector.__new__(A.AkamaiSiemCollector)
    base = F.event("req-1", 1761652800)
    assert c.extract_message_id(base) == c.extract_message_id(copy.deepcopy(base))
    other_cfg = F.event("req-1", 1761652800, config_id=F.CONFIG_B)
    other_rules = F.event("req-1", 1761652800, rules=["970001"], messages=["x"])
    ids = {c.extract_message_id(e) for e in (base, other_cfg, other_rules)}
    assert len(ids) == 3


def test_watermark_is_the_newest_event_read() -> None:
    wm = A.AkamaiSiemCollector.watermark_at({"offset": "o", "last_ts_ms": 1761652800000})
    assert wm is not None and wm.isoformat().startswith("2025-10-28T12:00:00")
    assert A.AkamaiSiemCollector.watermark_at({"offset": "o"}) is None


# ══ Teste de conexão ═══════════════════════════════════════════════════

_CFG = {"client_secret": F.EDGERC, "tenant_id": f"{F.CONFIG_A},{F.CONFIG_B}"}


@pytest.mark.asyncio
async def test_probe_passes_when_every_config_answers() -> None:
    with aioresponses() as m:
        m.get(_SIEM, body=F.body([], offset="o"), repeat=True)
        result = await A._akamai_probe(dict(_CFG))
    assert result.ok, result.detail
    assert f"config {F.CONFIG_A}: ok" in result.detail and f"config {F.CONFIG_B}: ok" in result.detail
    paths = sorted(c["path"] for c in _calls(m))
    assert paths == [f"/siem/v1/configs/{F.CONFIG_A}", f"/siem/v1/configs/{F.CONFIG_B}"]
    assert all(c["query"]["limit"] == "1" for c in _calls(m))


@pytest.mark.asyncio
async def test_probe_fails_with_the_permission_hint_when_one_config_is_forbidden() -> None:
    with aioresponses() as m:
        m.get(re.compile(rf".*/configs/{F.CONFIG_A}\?"), body=F.body([], offset="o"))
        m.get(
            re.compile(rf".*/configs/{F.CONFIG_B}\?"), status=403,
            payload={"title": "Forbidden", "detail": "token " + F.SECRET},
        )
        result = await A._akamai_probe(dict(_CFG))
    assert not result.ok
    assert "SIEM API em READ-WRITE" in result.detail and "Data collection for SIEM Integrations" in result.detail
    assert F.SECRET not in result.detail


@pytest.mark.asyncio
async def test_probe_rejects_a_bad_credential_without_echoing_it() -> None:
    bad = F.EDGERC.replace(F.HOST, "evil.example.com")
    with aioresponses() as m:
        result = await A._akamai_probe({**_CFG, "client_secret": bad})
    assert not result.ok and not m.requests
    assert F.SECRET not in result.detail


@pytest.mark.asyncio
async def test_probe_rejects_bad_config_ids() -> None:
    result = await A._akamai_probe({**_CFG, "tenant_id": "abc"})
    assert not result.ok and "dígitos" in result.detail


# ══ Registro ═══════════════════════════════════════════════════════════


def test_platform_is_registered_with_editable_columns_only() -> None:
    plat = get_platform("akamai")
    assert plat.icon_id == "akamai"
    assert {f.key for f in plat.auth_fields} <= {"tenant_id", "client_id", "client_secret", "base_url"}
    assert plat.required_secrets == ("client_secret",)
    assert supported_streams("akamai") == ["siem"]
    assert get("akamai", "siem").collector_cls is A.AkamaiSiemCollector


# ══ Mapping ════════════════════════════════════════════════════════════


def _normalize(raw: Dict[str, Any]) -> Dict[str, Any]:
    compiled = E.compile_rules(load_default_rules("akamai", "akamai.siem"))
    n = E.apply_compiled(compiled, A.decode_attack_data(copy.deepcopy(raw))).output["normalized"]
    r = V.validate_normalized(n)
    assert r.valid, (r.reason, n)
    return n


def test_blocked_xss_is_an_http_activity_with_the_rule() -> None:
    n = _normalize(F.XSS_DENY)
    assert (n["class_uid"], n["activity_id"], n["type_uid"]) == (4002, 3, 400203)
    assert (n["disposition_id"], n["action_id"], n["severity_id"]) == (2, 2, 2)
    assert n["is_alert"] is True
    assert n["src_endpoint"]["ip"] == "192.0.2.10"
    assert n["src_endpoint"]["location"]["country"] == "BR"
    assert n["src_endpoint"]["autonomous_system"]["number"] == 64500
    assert n["http_request"]["url"]["hostname"] == "www.example.com"
    assert n["http_request"]["url"]["port"] == 443
    assert n["http_request"]["user_agent"] == "Mozilla/5.0 (teste)"
    assert n["http_response"]["code"] == 403
    assert n["firewall_rule"]["uid"] == "950004"
    assert n["firewall_rule"]["match_details"] == ["950004", "990011"]
    assert n["time"] == 1761652800000
    assert n["metadata"]["uid"] == "2a5e8f0c"
    assert n["message"] == "Akamai WAF deny: Cross-site Scripting (XSS) Attack"


@pytest.mark.parametrize(
    "method,activity", [("POST", 6), ("PUT", 7), ("DELETE", 2), ("PATCH", 9), ("PROPFIND", 99)]
)
def test_activity_follows_the_http_method(method: str, activity: int) -> None:
    n = _normalize(F.event("m", 1761652800, method=method))
    assert (n["activity_id"], n["type_uid"]) == (activity, 400200 + activity)


@pytest.mark.parametrize(
    "action,disposition,severity",
    [("alert", 19, 3), ("monitor", 17, 3), ("deny_custom_68372", 2, 2), ("tarpit", 2, 2), ("challenge", 99, 2)],
)
def test_disposition_and_severity_follow_the_applied_action(action: str, disposition: int, severity: int) -> None:
    n = _normalize(F.event("a", 1761652800, action=action))
    assert (n["disposition_id"], n["severity_id"]) == (disposition, severity)
