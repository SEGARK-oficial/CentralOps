"""AWS CloudTrail pelo Event history (``LookupEvents``) — coleta sem bucket.

Sem rede: o client aioboto3 é um fake. Amostras no formato documentado do
``LookupEvents`` (contas e IPs de documentação).
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from ..auth.aws import AwsAuth, cloudformation_template
from ..base import CollectorContext
from ..normalize import engine as E
from ..normalize.defaults import load_default_rules
from ..normalize.ocsf.validator import validate_normalized
from ..vendors import aws_cloudtrail_lookup as lk
from ..vendors import aws_findings_base as fb
from ..vendors.aws_cloudtrail_lookup import AWSCloudTrailLookupCollector, to_record

_ACCT = "111122223333"

# Chamada de inferência do Bedrock: management event no bedrock-runtime.
INVOKE = {
    "eventVersion": "1.09",
    "userIdentity": {
        "type": "IAMUser",
        "principalId": "AIDAEXAMPLE",
        "arn": f"arn:aws:iam::{_ACCT}:user/ci-deploy",
        "accountId": _ACCT,
        "accessKeyId": "AKIAEXAMPLE",
        "userName": "ci-deploy",
    },
    "eventTime": "2026-11-23T10:00:00Z",
    "eventSource": "bedrock.amazonaws.com",
    "eventName": "InvokeModel",
    "awsRegion": "us-east-1",
    "sourceIPAddress": "198.51.100.7",
    "userAgent": "python-requests/2.32",
    "requestParameters": {"modelId": "anthropic.claude-3-sonnet-20240229-v1:0"},
    "responseElements": None,
    "eventID": "3f1c0000-0000-4000-8000-000000000001",
    "readOnly": True,
    "eventType": "AwsApiCall",
    "managementEvent": True,
    "recipientAccountId": _ACCT,
    "eventCategory": "Management",
}


def _item(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Um item do ``LookupEvents``: campos de topo + o registro inteiro em string."""
    return {
        "EventId": rec["eventID"],
        "EventName": rec["eventName"],
        "ReadOnly": "true" if rec.get("readOnly") else "false",
        "EventTime": datetime.fromisoformat(rec["eventTime"].replace("Z", "+00:00")),
        "EventSource": rec["eventSource"],
        "Username": rec["userIdentity"].get("userName"),
        "CloudTrailEvent": json.dumps(rec),
    }


def _rec(**over) -> Dict[str, Any]:
    r = copy.deepcopy(INVOKE)
    r.update(over)
    return r


# ── fakes ────────────────────────────────────────────────────────────────


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


def _ctx(cursor=None) -> CollectorContext:
    return CollectorContext(
        integration_id=9, organization_id=3, platform="aws", headers={},
        session=MagicMock(), cursor=cursor, domain_limiter=_NoopDomainLimiter(),
        rate_limiter=_NoopRateLimiter(), redis=MagicMock(),
    )


class _AwsError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code, "Message": code}}


class _Client:
    """Fake de client aioboto3: registra as chamadas e responde por página."""

    def __init__(self, pages: List[Any], regions: List[str] = ()) -> None:
        self.pages = list(pages)
        self.calls: List[Dict[str, Any]] = []
        self.regions = list(regions)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def lookup_events(self, **kw):
        self.calls.append(dict(kw))
        page = self.pages.pop(0) if self.pages else {"Events": []}
        if isinstance(page, Exception):
            raise page
        return page

    async def describe_regions(self, **kw):
        return {"Regions": [{"RegionName": r} for r in self.regions]}


@pytest.fixture(autouse=True)
def _no_pacing(monkeypatch):
    monkeypatch.setattr(lk, "_MIN_INTERVAL_S", 0)


async def _run(clients: Dict[str, _Client], regions=("us-east-1",), cursor=None):
    ctx = _ctx(cursor)
    conn = {"auth": AwsAuth.access_key("AKIA", "s"), "regions": list(regions)}
    collector = AWSCloudTrailLookupCollector(ctx)

    def _client(self, service, kwargs):
        key = f"{service}@{kwargs['region_name']}"
        return clients[key] if key in clients else clients[service]

    cls = AWSCloudTrailLookupCollector
    with patch.object(cls, "_load_conn", return_value=conn), patch.object(cls, "_client", _client):
        out = [ev async for ev in collector.collect()]
    return ctx, collector, out


def _normalize(rec: Dict[str, Any]) -> Dict[str, Any]:
    rules = E.compile_rules(
        load_default_rules("aws_cloudtrail_lookup", "aws_cloudtrail_lookup.event")
    )
    return E.apply_compiled(rules, rec).output["normalized"]


# ── coleta ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pages_a_closed_window_and_emits_the_full_cloudtrail_record() -> None:
    a = _rec(eventID="a", eventTime="2026-11-23T10:01:00Z")
    b = _rec(eventID="b", eventTime="2026-11-23T10:00:00Z")
    client = _Client([
        {"Events": [_item(a)], "NextToken": "t2"},
        {"Events": [_item(b)]},
    ])
    ctx, collector, out = await _run({"cloudtrail": client})

    assert [e["eventID"] for e in out] == ["a", "b"]
    # O registro é o do bucket (mesmo shape), mais a origem.
    assert out[0]["requestParameters"]["modelId"].startswith("anthropic.")
    assert out[0]["centralops"] == {"source": "lookup_events", "region": "us-east-1"}
    first, second = client.calls
    assert first["MaxResults"] == 50
    # Janela FECHADA: o NextToken só vale com os mesmos parâmetros.
    assert (first["StartTime"], first["EndTime"]) == (second["StartTime"], second["EndTime"])
    assert "NextToken" not in first and second["NextToken"] == "t2"
    assert collector.extract_message_id(out[0]) == "a"
    assert set(ctx.cursor["regions"]) == {"us-east-1"}


@pytest.mark.asyncio
async def test_watermark_follows_the_newest_event_even_though_pages_come_newest_first() -> None:
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0)
    newest = datetime.now(timezone.utc) - timedelta(minutes=1)
    newest_iso = newest.strftime("%Y-%m-%dT%H:%M:%SZ")
    client = _Client([{"Events": [
        _item(_rec(eventID="n", eventTime=newest_iso)),
        _item(_rec(eventID="o", eventTime="2026-11-23T09:00:00Z")),
    ]}])
    ctx, _, _ = await _run({"cloudtrail": client},
                           cursor={"regions": {"us-east-1": {"updated_at": fb.iso(old)}}})
    wm = fb.parse_ts(ctx.cursor["regions"]["us-east-1"]["updated_at"])
    assert wm == newest.replace(microsecond=0)
    # A janela começou ANTES do watermark: evento que aparece atrasado é relido.
    assert client.calls[0]["StartTime"] == old - fb._OVERLAP


@pytest.mark.asyncio
async def test_cap_saves_exact_position_and_resumes_with_same_window(monkeypatch) -> None:
    monkeypatch.setattr(AWSCloudTrailLookupCollector, "_max_pages_per_cycle", 1)
    wm = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)
    client = _Client([{"Events": [_item(_rec(eventID="a"))], "NextToken": "t2"}])
    ctx, _, _ = await _run({"cloudtrail": client},
                           cursor={"regions": {"us-east-1": {"updated_at": fb.iso(wm)}}})
    st = ctx.cursor["regions"]["us-east-1"]
    assert st["next_token"] == "t2"
    assert fb.parse_ts(st["updated_at"]) == wm  # não avançou
    assert ctx.hit_cycle_cap is True

    client2 = _Client([{"Events": [_item(_rec(eventID="b"))]}])
    ctx2, _, out2 = await _run({"cloudtrail": client2}, cursor=ctx.cursor)
    call = client2.calls[0]
    assert call["NextToken"] == "t2"
    assert fb.iso(call["StartTime"]) == st["window_start"]
    assert fb.iso(call["EndTime"]) == st["window_end"]
    assert [e["eventID"] for e in out2] == ["b"]
    assert "next_token" not in ctx2.cursor["regions"]["us-east-1"]


def test_cloudtrail_raises_the_fleet_page_cap() -> None:
    """Página de 50 eventos: o teto dos achados (50 páginas) seria 2.500 eventos
    por ciclo, pouco para uma conta movimentada."""
    assert AWSCloudTrailLookupCollector._max_pages_per_cycle == lk._MAX_PAGES
    assert lk._MAX_PAGES > fb._MAX_PAGES_PER_CYCLE
    # O ritmo da AWS (2/s) cabe no soft-timeout do ciclo (720 s).
    assert lk._MAX_PAGES * 0.55 < 720


@pytest.mark.asyncio
async def test_rejected_next_token_rereads_the_window_instead_of_failing_forever() -> None:
    wm = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)
    cursor = {"regions": {"us-east-1": {
        "updated_at": fb.iso(wm),
        "window_start": fb.iso(wm - fb._OVERLAP),
        "window_end": fb.iso(wm + timedelta(hours=1)),
        "next_token": "velho",
    }}}
    client = _Client([_AwsError("InvalidNextTokenException"), {"Events": [_item(_rec(eventID="a"))]}])
    ctx, _, out = await _run({"cloudtrail": client}, cursor=cursor)
    assert client.calls[0]["NextToken"] == "velho"
    assert "NextToken" not in client.calls[1]
    assert client.calls[1]["StartTime"] == client.calls[0]["StartTime"]
    assert [e["eventID"] for e in out] == ["a"]
    assert "next_token" not in ctx.cursor["regions"]["us-east-1"]


@pytest.mark.asyncio
async def test_access_denied_fails_the_cycle_with_a_safe_message() -> None:
    """Sem ``cloudtrail:LookupEvents`` o ciclo FALHA (Saúde vermelha); não vira
    ciclo vazio com o watermark andando."""
    client = _Client([_AwsError("AccessDeniedException")])
    cursor = {"regions": {"us-east-1": {"updated_at": "2026-11-23T09:00:00.000Z"}}}
    ctx = _ctx(cursor)
    collector = AWSCloudTrailLookupCollector(ctx)
    conn = {"auth": AwsAuth.access_key("AKIA", "s"), "regions": ["us-east-1"]}
    cls = AWSCloudTrailLookupCollector
    with patch.object(cls, "_load_conn", return_value=conn), \
            patch.object(cls, "_client", lambda self, s, k: client):
        with pytest.raises(RuntimeError, match="LookupEvents em us-east-1"):
            [ev async for ev in collector.collect()]
    assert ctx.cursor == cursor  # nada avançou


@pytest.mark.asyncio
async def test_all_regions_star_uses_describe_regions() -> None:
    clients = {
        "ec2": _Client([], regions=["sa-east-1", "us-east-1"]),
        "cloudtrail@sa-east-1": _Client([{"Events": [_item(_rec(eventID="sp", awsRegion="sa-east-1"))]}]),
        "cloudtrail@us-east-1": _Client([{"Events": [_item(_rec(eventID="us"))]}]),
    }
    ctx, _, out = await _run(clients, regions=("*",))
    assert sorted(e["eventID"] for e in out) == ["sp", "us"]
    assert set(ctx.cursor["regions"]) == {"sa-east-1", "us-east-1"}


@pytest.mark.asyncio
async def test_calls_are_spaced_to_respect_the_aws_rate(monkeypatch) -> None:
    """2 chamadas/s por conta e região: o coletor espaça, não martela."""
    monkeypatch.setattr(lk, "_MIN_INTERVAL_S", 0.5)
    sleeps: List[float] = []

    async def _sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(lk.asyncio, "sleep", _sleep)
    client = _Client([
        {"Events": [], "NextToken": "t2"},
        {"Events": [], "NextToken": "t3"},
        {"Events": []},
    ])
    await _run({"cloudtrail": client})
    assert len(client.calls) == 3
    assert len(sleeps) >= 2 and all(0 < s <= 0.5 for s in sleeps)


# ── registro ─────────────────────────────────────────────────────────────


def test_item_without_parseable_cloudtrail_event_is_kept_as_minimal_record() -> None:
    item = _item(INVOKE)
    item["CloudTrailEvent"] = "{quebrado"
    rec = to_record(item, "us-east-1")
    assert rec["eventID"] == INVOKE["eventID"]
    assert rec["eventName"] == "InvokeModel"
    assert rec["eventTime"].startswith("2026-11-23T10:00:00")
    assert rec["userIdentity"] == {"userName": "ci-deploy"}
    assert rec["readOnly"] is True
    assert to_record({"CloudTrailEvent": None}, "us-east-1") is None


def test_bedrock_invocation_normalizes_like_the_bucket_record() -> None:
    rec = to_record(_item(INVOKE), "us-east-1")
    n = _normalize(rec)
    assert (n["class_uid"], n["type_uid"]) == (6003, 600300)
    assert n["api"]["operation"] == "InvokeModel"
    assert n["api"]["service"]["name"] == "bedrock.amazonaws.com"
    assert n["actor"]["user"]["name"] == "ci-deploy"
    assert n["src_endpoint"]["ip"] == "198.51.100.7"
    assert n["cloud"]["account"]["uid"] == _ACCT
    assert n["metadata"]["uid"] == INVOKE["eventID"]
    # Mesmo resultado do registro lido do bucket (sem o bloco de origem).
    bucket = E.apply_compiled(
        E.compile_rules(load_default_rules("aws_cloudtrail", "aws_cloudtrail.event")), INVOKE
    ).output["normalized"]
    assert n == bucket
    result = validate_normalized(n)
    assert result.valid, result.reason


# ── catálogo e onboarding ────────────────────────────────────────────────


def test_platform_is_registered_with_role_onboarding() -> None:
    from ..registry import get, get_platform, has

    assert has("aws_cloudtrail_lookup", "events")
    plat = get_platform("aws_cloudtrail_lookup")
    assert plat.auth_setup == "aws_assume_role"
    assert plat.required_secrets == ()
    assert {f.key for f in plat.auth_fields} == {"client_id", "secret_access_key", "region"}
    assert get("aws_cloudtrail_lookup", "events").collector_cls is AWSCloudTrailLookupCollector


def test_role_template_grants_lookup_events_without_any_bucket() -> None:
    tpl = cloudformation_template("ext-id", f"arn:aws:iam::{_ACCT}:role/CentralOps")
    policies = tpl["Resources"]["CentralOpsRole"]["Properties"]["Policies"]
    plain = [p for p in policies if "PolicyName" in p]
    actions = [
        st["Action"] for p in plain for st in p["PolicyDocument"]["Statement"]
    ]
    assert "cloudtrail:LookupEvents" in actions
