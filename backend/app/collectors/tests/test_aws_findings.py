"""Achados da AWS: Security Hub (ASFF) e GuardDuty → OCSF Detection Finding (2004).

Sem rede: o client aioboto3 é um fake por serviço. Amostras sintéticas no formato
documentado (contas e IPs de documentação).
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from ..auth.aws import AwsAuth
from ..base import CollectorContext
from ..normalize import engine as E
from ..normalize.defaults import load_default_rules
from ..normalize.ocsf.validator import validate_normalized
from ..vendors import aws_findings_base as fb
from ..vendors.aws_guardduty import AWSGuardDutyCollector
from ..vendors.aws_securityhub import AWSSecurityHubCollector

_ACCT = "111122223333"

ASFF = {
    "SchemaVersion": "2018-10-08",
    "Id": f"arn:aws:guardduty:us-east-1:{_ACCT}:detector/abc/finding/def",
    "ProductArn": "arn:aws:securityhub:us-east-1::product/aws/guardduty",
    "ProductName": "GuardDuty",
    "CompanyName": "Amazon",
    "Region": "us-east-1",
    "GeneratorId": f"arn:aws:guardduty:us-east-1:{_ACCT}:detector/abc",
    "AwsAccountId": _ACCT,
    "Types": ["TTPs/Initial Access/UnauthorizedAccess:EC2-SSHBruteForce"],
    "FirstObservedAt": "2026-11-09T09:00:00.000Z",
    "LastObservedAt": "2026-11-09T10:00:00.000Z",
    "CreatedAt": "2026-11-09T09:05:00.000Z",
    "UpdatedAt": "2026-11-09T10:05:00.000Z",
    "Severity": {"Label": "HIGH", "Normalized": 70},
    "Title": "SSH brute force contra i-0abc",
    "Description": "198.51.100.7 tenta SSH em i-0abc.",
    "Resources": [{"Type": "AwsEc2Instance", "Id": f"arn:aws:ec2:us-east-1:{_ACCT}:instance/i-0abc", "Region": "us-east-1"}],
    "Workflow": {"Status": "NEW"},
    "RecordState": "ACTIVE",
    "Remediation": {"Recommendation": {"Text": "Restrinja a porta 22.", "Url": "https://docs.aws.amazon.com/guardduty/"}},
}

GD = {
    "AccountId": _ACCT,
    "Arn": f"arn:aws:guardduty:us-east-1:{_ACCT}:detector/abc/finding/def",
    "CreatedAt": "2026-11-09T09:05:00.000Z",
    "Description": "A API ListBuckets foi chamada de um IP de saída Tor.",
    "Id": "def",
    "Partition": "aws",
    "Region": "us-east-1",
    "Resource": {
        "ResourceType": "AccessKey",
        "AccessKeyDetails": {"AccessKeyId": "ASIAEXAMPLE", "PrincipalId": "AROAEXAMPLE",
                             "UserName": "deploy-bot", "UserType": "AssumedRole"},
    },
    "SchemaVersion": "2.0",
    "Service": {
        "Action": {
            "ActionType": "AWS_API_CALL",
            "AwsApiCallAction": {
                "Api": "ListBuckets", "ServiceName": "s3.amazonaws.com", "CallerType": "Remote IP",
                "RemoteIpDetails": {"IpAddressV4": "198.51.100.7", "Country": {"CountryCode": "NL"}},
            },
        },
        "Archived": False,
        "Count": 3,
        "DetectorId": "abc",
        "EventFirstSeen": "2026-11-09T09:00:00.000Z",
        "EventLastSeen": "2026-11-09T10:00:00.000Z",
        "ServiceName": "guardduty",
    },
    "Severity": 8,
    "Title": "Chamada de API a partir de nó Tor",
    "Type": "UnauthorizedAccess:IAMUser/TorIPCaller",
    "UpdatedAt": "2026-11-09T10:05:00.000Z",
}


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


class _Client:
    """Fake de client aioboto3: registra as chamadas e responde por página."""

    def __init__(self, pages: List[Dict[str, Any]], **extra) -> None:
        self.pages = list(pages)
        self.calls: List[Dict[str, Any]] = []
        self.extra = extra

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    # Security Hub
    async def get_findings(self, **kw):
        self.calls.append(("get_findings", kw))
        if "FindingIds" in kw:  # GuardDuty
            return {"Findings": [f for f in self.extra["gd_findings"] if f["Id"] in kw["FindingIds"]]}
        return self.pages.pop(0) if self.pages else {"Findings": []}

    # GuardDuty
    async def list_detectors(self):
        self.calls.append(("list_detectors", {}))
        return {"DetectorIds": self.extra.get("detectors", [])}

    async def list_findings(self, **kw):
        self.calls.append(("list_findings", kw))
        return self.pages.pop(0) if self.pages else {"FindingIds": []}

    # EC2
    async def describe_regions(self, **kw):
        return {"Regions": [{"RegionName": r} for r in self.extra.get("regions", [])]}


def _sh(**over) -> Dict[str, Any]:
    f = copy.deepcopy(ASFF)
    f.update(over)
    return f


async def _run(cls, clients: Dict[str, _Client], regions=("us-east-1",), cursor=None):
    ctx = _ctx(cursor)
    conn = {"auth": AwsAuth.access_key("AKIA", "s"), "regions": list(regions)}
    collector = cls(ctx)
    order: List[str] = []

    def _client(self, service, kwargs):
        order.append(f"{service}@{kwargs['region_name']}")
        return clients[f"{service}@{kwargs['region_name']}"] if f"{service}@{kwargs['region_name']}" in clients else clients[service]

    with patch.object(cls, "_load_conn", return_value=conn), patch.object(cls, "_client", _client):
        out = [ev async for ev in collector.collect()]
    return ctx, collector, out, order


def _normalize(vendor: str, event_type: str, rec: Dict[str, Any]) -> Dict[str, Any]:
    rules = E.compile_rules(load_default_rules(vendor, event_type))
    return E.apply_compiled(rules, rec).output["normalized"]


# ── Security Hub: coleta ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_securityhub_pages_a_closed_window_sorted_ascending() -> None:
    client = _Client([
        {"Findings": [_sh(Id="a", UpdatedAt="2026-11-09T10:00:00.000Z")], "NextToken": "t2"},
        {"Findings": [_sh(Id="b", UpdatedAt="2026-11-09T10:01:00.000Z")]},
    ])
    ctx, collector, out, _ = await _run(AWSSecurityHubCollector, {"securityhub": client})

    assert [f["Id"] for f in out] == ["a", "b"]
    first, second = client.calls[0][1], client.calls[1][1]
    assert first["SortCriteria"] == [{"Field": "UpdatedAt", "SortOrder": "asc"}]
    assert first["MaxResults"] == 100
    # Janela FECHADA e a mesma nas duas páginas.
    assert first["Filters"] == second["Filters"]
    assert second["NextToken"] == "t2"
    assert set(ctx.cursor["regions"]) == {"us-east-1"}
    assert collector.extract_message_id(out[0]) == "a|2026-11-09T10:00:00.000Z"


@pytest.mark.asyncio
async def test_window_starts_before_the_watermark_to_catch_late_indexing() -> None:
    wm = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)
    client = _Client([{"Findings": []}])
    await _run(AWSSecurityHubCollector, {"securityhub": client},
               cursor={"regions": {"us-east-1": {"updated_at": fb.iso(wm)}}})
    start = fb.parse_ts(client.calls[0][1]["Filters"]["UpdatedAt"][0]["Start"])
    assert start == wm - fb._OVERLAP


@pytest.mark.asyncio
async def test_drained_window_advances_watermark_without_jumping_to_now() -> None:
    """Sem achados: o watermark anda até fim-da-janela − tolerância, não até o fim
    — o que for indexado atrasado ainda cai na próxima janela."""
    old = datetime.now(timezone.utc) - timedelta(days=2)
    client = _Client([{"Findings": []}])
    ctx, _, _, _ = await _run(AWSSecurityHubCollector, {"securityhub": client},
                              cursor={"regions": {"us-east-1": {"updated_at": fb.iso(old)}}})
    wm = fb.parse_ts(ctx.cursor["regions"]["us-east-1"]["updated_at"])
    end = fb.parse_ts(client.calls[0][1]["Filters"]["UpdatedAt"][0]["End"])
    assert wm == end - fb._LATENESS


@pytest.mark.asyncio
async def test_cap_saves_exact_position_and_does_not_advance(monkeypatch) -> None:
    monkeypatch.setattr(fb, "_MAX_PAGES_PER_CYCLE", 1)
    wm = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)
    client = _Client([{"Findings": [_sh(Id="a")], "NextToken": "t2"}])
    ctx, _, out, _ = await _run(AWSSecurityHubCollector, {"securityhub": client},
                                cursor={"regions": {"us-east-1": {"updated_at": fb.iso(wm)}}})
    st = ctx.cursor["regions"]["us-east-1"]
    assert st["next_token"] == "t2"
    assert fb.parse_ts(st["updated_at"]) == wm  # não avançou
    assert ctx.hit_cycle_cap is True

    # Retomada: mesma janela + token.
    client2 = _Client([{"Findings": [_sh(Id="b")]}])
    ctx2, _, out2, _ = await _run(AWSSecurityHubCollector, {"securityhub": client2}, cursor=ctx.cursor)
    call = client2.calls[0][1]
    assert call["NextToken"] == "t2"
    assert call["Filters"]["UpdatedAt"][0] == {"Start": st["window_start"], "End": st["window_end"]}
    assert [f["Id"] for f in out2] == ["b"]
    assert "next_token" not in ctx2.cursor["regions"]["us-east-1"]


@pytest.mark.asyncio
async def test_all_regions_star_uses_describe_regions() -> None:
    clients = {
        "ec2": _Client([], regions=["sa-east-1", "us-east-1"]),
        "securityhub@sa-east-1": _Client([{"Findings": [_sh(Id="sp", Region="sa-east-1")]}]),
        "securityhub@us-east-1": _Client([{"Findings": [_sh(Id="us")]}]),
    }
    ctx, _, out, _ = await _run(AWSSecurityHubCollector, clients, regions=("*",))
    assert sorted(f["Id"] for f in out) == ["sp", "us"]
    assert set(ctx.cursor["regions"]) == {"sa-east-1", "us-east-1"}


def test_watermark_is_the_most_delayed_region() -> None:
    cur = {"regions": {"a": {"updated_at": "2026-11-09T10:00:00.000Z"},
                       "b": {"updated_at": "2026-11-01T10:00:00.000Z"}}}
    assert AWSSecurityHubCollector.watermark_at(cur) == datetime(2026, 11, 1, 10, 0)
    assert AWSSecurityHubCollector.watermark_at({}) is None


# ── GuardDuty: coleta ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_guardduty_lists_ids_then_gets_findings_with_epoch_ms_window() -> None:
    client = _Client([{"FindingIds": ["def"]}], detectors=["abc"], gd_findings=[GD])
    _, collector, out, _ = await _run(AWSGuardDutyCollector, {"guardduty": client})

    assert [f["Id"] for f in out] == ["def"]
    assert out[0]["centralops"] == {"detectorId": "abc", "region": "us-east-1"}
    listed = next(kw for name, kw in client.calls if name == "list_findings")
    crit = listed["FindingCriteria"]["Criterion"]["updatedAt"]
    assert isinstance(crit["GreaterThanOrEqual"], int) and crit["GreaterThanOrEqual"] > 10**12
    assert listed["SortCriteria"] == {"AttributeName": "updatedAt", "OrderBy": "ASC"}
    assert listed["MaxResults"] == 50
    assert collector.extract_message_id(out[0]) == "def|2026-11-09T10:05:00.000Z"


@pytest.mark.asyncio
async def test_region_without_detector_is_skipped_not_failed() -> None:
    clients = {
        "guardduty@sa-east-1": _Client([], detectors=[]),
        "guardduty@us-east-1": _Client([{"FindingIds": ["def"]}], detectors=["abc"], gd_findings=[GD]),
    }
    ctx, _, out, _ = await _run(AWSGuardDutyCollector, clients, regions=("sa-east-1", "us-east-1"))
    assert [f["Id"] for f in out] == ["def"]
    assert not any(name == "list_findings" for name, _ in clients["guardduty@sa-east-1"].calls)
    assert set(ctx.cursor["regions"]) == {"sa-east-1", "us-east-1"}


# ── normalização ─────────────────────────────────────────────────────────


def test_asff_finding_normalizes_to_valid_detection_finding() -> None:
    n = _normalize("aws_securityhub", "aws_securityhub.finding", ASFF)
    assert (n["class_uid"], n["activity_id"], n["type_uid"]) == (2004, 2, 200402)
    assert (n["severity_id"], n["status_id"], n["is_alert"]) == (4, 1, True)
    assert n["finding_info"]["uid"] == ASFF["Id"]
    assert n["finding_info"]["types"] == ASFF["Types"]
    assert n["finding_info"]["product"]["name"] == "GuardDuty"
    assert n["resources"][0]["type"] == "AwsEc2Instance"
    assert n["cloud"]["account"]["uid"] == _ACCT
    assert n["remediation"]["references"] == ["https://docs.aws.amazon.com/guardduty/"]
    assert n["time"] == int(datetime(2026, 11, 9, 10, 5, tzinfo=timezone.utc).timestamp() * 1000)
    result = validate_normalized(n)
    assert result.valid, result.reason


@pytest.mark.parametrize(
    "over,activity_id,status_id,is_alert",
    [
        ({"CreatedAt": "2026-11-09T10:05:00.000Z"}, 1, 1, True),  # nunca atualizado = Create
        ({"RecordState": "ARCHIVED"}, 3, 5, True),
        ({"Workflow": {"Status": "SUPPRESSED"}}, 2, 3, True),
        ({"Severity": {"Label": "INFORMATIONAL"}, "Compliance": {"Status": "PASSED"}}, 2, 1, False),
    ],
)
def test_asff_lifecycle_and_noise(over, activity_id, status_id, is_alert) -> None:
    n = _normalize("aws_securityhub", "aws_securityhub.finding", _sh(**over))
    assert n["activity_id"] == activity_id
    assert n["status_id"] == status_id
    assert n["is_alert"] is is_alert
    assert validate_normalized(n).valid


def test_guardduty_finding_normalizes_to_valid_detection_finding() -> None:
    n = _normalize("aws_guardduty", "aws_guardduty.finding", GD)
    assert (n["class_uid"], n["severity_id"], n["severity"]) == (2004, 4, "High")
    assert n["finding_info"]["types"] == ["UnauthorizedAccess:IAMUser/TorIPCaller"]
    assert n["src_endpoint"]["ip"] == "198.51.100.7"
    assert n["src_endpoint"]["location"]["country"] == "NL"
    assert n["actor"]["user"]["name"] == "deploy-bot"
    assert n["resources"] == [{"type": "AccessKey", "uid": "ASIAEXAMPLE"}]
    assert n["count"] == 3
    assert n["unmapped"]["aws_guardduty_api"] == "ListBuckets"
    assert n["is_alert"] is True
    result = validate_normalized(n)
    assert result.valid, result.reason


@pytest.mark.parametrize("sev,expected", [(1.0, 2), (3.9, 2), (4.0, 3), (6.9, 3), (7.0, 4), (8.9, 4), (9.0, 5), (10, 5)])
def test_guardduty_severity_bands(sev, expected) -> None:
    g = copy.deepcopy(GD)
    g["Severity"] = sev
    assert _normalize("aws_guardduty", "aws_guardduty.finding", g)["severity_id"] == expected


def test_archived_guardduty_finding_is_closed_and_not_an_alert() -> None:
    g = copy.deepcopy(GD)
    g["Service"]["Archived"] = True
    n = _normalize("aws_guardduty", "aws_guardduty.finding", g)
    assert (n["activity_id"], n["status_id"], n["is_alert"]) == (3, 5, False)


# ── registro ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("platform", ["aws_securityhub", "aws_guardduty"])
def test_registered_with_role_onboarding(platform) -> None:
    from ..capabilities import invalid_capabilities
    from ..registry import get, get_platform

    plat = get_platform(platform)
    assert plat.auth_setup == "aws_assume_role"
    assert invalid_capabilities(plat.capabilities) == []
    assert get(platform, "findings").collector_cls.stream == "findings"


def test_template_grants_read_only_findings_access() -> None:
    from ..auth import aws as aws_auth

    tpl = aws_auth.cloudformation_template("e", None)
    policy = next(
        p for p in tpl["Resources"]["CentralOpsRole"]["Properties"]["Policies"]
        if p.get("PolicyName") == "CentralOpsFindingsRead"
    )
    actions = set(policy["PolicyDocument"]["Statement"][0]["Action"])
    assert actions == {
        "securityhub:GetFindings", "guardduty:ListDetectors", "guardduty:ListFindings",
        "guardduty:GetFindings", "ec2:DescribeRegions",
    }
