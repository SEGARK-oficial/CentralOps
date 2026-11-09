"""AWS WAF — logs da web ACL lidos do S3 (motor do CloudTrail) ou do CloudWatch Logs.

Sem rede: o client aioboto3 é um fake que entende ``Prefix``/``Delimiter``/
``StartAfter`` como o S3. O registro de exemplo é o da documentação da AWS
(regra SQLi, ação BLOCK).
"""

from __future__ import annotations

import copy
import gzip
import json
from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from ..auth.aws import AwsAuth
from ..base import CollectorContext
from ..normalize import engine as E
from ..normalize.defaults import load_default_rules
from ..normalize.ocsf.validator import validate_normalized
from ..vendors import aws_cloudtrail as ct
from ..vendors import aws_waf as waf
from ..vendors.aws_waf import AWSWafCollector

_ACCT = "111122223333"
_ROLE = f"arn:aws:iam::{_ACCT}:role/CentralOpsReadOnly"

# Registro oficial da AWS (logging-examples.html — SQLi, terminating).
_BLOCK = {
    "timestamp": 1576280412771,
    "formatVersion": 1,
    "webaclId": "arn:aws:wafv2:ap-southeast-2:111122223333:regional/webacl/STMTest/1EXAMPLE-2ARN-3ARN-4ARN-123456EXAMPLE",
    "terminatingRuleId": "STMTest_SQLi_XSS",
    "terminatingRuleType": "REGULAR",
    "action": "BLOCK",
    "terminatingRuleMatchDetails": [
        {"conditionType": "SQL_INJECTION", "sensitivityLevel": "HIGH", "location": "HEADER",
         "matchedData": ["10", "AND", "1"]}
    ],
    "httpSourceName": "-",
    "httpSourceId": "-",
    "ruleGroupList": [],
    "rateBasedRuleList": [],
    "nonTerminatingMatchingRules": [],
    "httpRequest": {
        "clientIp": "1.1.1.1",
        "country": "AU",
        "headers": [
            {"name": "Host", "value": "localhost:1989"},
            {"name": "User-Agent", "value": "curl/7.61.1"},
            {"name": "Accept", "value": "*/*"},
            {"name": "x-stm-test", "value": "10 AND 1=1"},
        ],
        "uri": "/myUri",
        "args": "",
        "httpVersion": "HTTP/1.1",
        "httpMethod": "GET",
        "requestId": "rid",
    },
    "labels": [{"name": "value"}],
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
        integration_id=7, organization_id=3, platform="aws_waf", headers={},
        session=MagicMock(), cursor=cursor, domain_limiter=_NoopDomainLimiter(),
        rate_limiter=_NoopRateLimiter(), redis=MagicMock(),
    )


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def read(self) -> bytes:
        return self._data


class FakeS3:
    """S3 em memória: ``Prefix``, ``Delimiter`` (CommonPrefixes) e ``StartAfter``."""

    def __init__(self, objects: Dict[str, bytes], when: datetime) -> None:
        self.objects = objects
        self.when = when
        self.calls: List[Dict[str, Any]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def list_objects_v2(self, **kw):
        self.calls.append(kw)
        prefix = kw.get("Prefix", "")
        keys = sorted(k for k in self.objects if k.startswith(prefix))
        if kw.get("StartAfter"):
            keys = [k for k in keys if k > kw["StartAfter"]]
        if kw.get("Delimiter"):
            dirs = sorted({prefix + k[len(prefix):].split("/", 1)[0] + "/" for k in keys if "/" in k[len(prefix):]})
            return {"CommonPrefixes": [{"Prefix": d} for d in dirs], "IsTruncated": False}
        return {"Contents": [{"Key": k, "LastModified": self.when} for k in keys], "IsTruncated": False}

    async def get_object(self, Bucket, Key):  # noqa: N803
        return {"Body": _Body(self.objects[Key])}


def _ndjson(*records) -> bytes:
    return gzip.compress("\n".join(json.dumps(r) for r in records).encode())


def _key(region: str, acl: str, n: int = 0) -> str:
    return (
        f"AWSLogs/{_ACCT}/WAFLogs/{region}/{acl}/2026/11/09/10/05/"
        f"{_ACCT}_waflogs_{region}_{acl}_20261109T1005Z_{n:04d}.log.gz"
    )


def _rec(rid: str, action: str = "BLOCK", **extra) -> Dict[str, Any]:
    r = copy.deepcopy(_BLOCK)
    r["action"] = action
    r["httpRequest"]["requestId"] = rid
    r.update(extra)
    return r


def _s3_conn(regions=("*",)) -> Dict[str, Any]:
    return waf.build_conn(
        AwsAuth.access_key("AKIA", "s"),
        {"mode": waf.MODE_S3, "bucket": "aws-waf-logs-x", "key_prefix": ""},
        ",".join(regions), _ACCT,
    )


async def _collect_s3(fake: FakeS3, conn, cursor=None):
    ctx = _ctx(cursor)
    collector = AWSWafCollector(ctx)
    with patch.object(AWSWafCollector, "_load_conn", return_value=conn), \
         patch.object(ct.AWSCloudTrailCollector, "_s3_client", return_value=fake), \
         patch.object(ct, "_date_prefixes", side_effect=lambda base, since, now: [f"{base}2026/11/09/"]):
        out = [ev async for ev in collector.collect()]
    return ctx, collector, out


def _normalize(rec: Dict[str, Any]) -> Dict[str, Any]:
    rules = E.compile_rules(load_default_rules("aws_waf", "aws_waf.event"))
    return E.apply_compiled(rules, rec).output["normalized"]


# ── destino ──────────────────────────────────────────────────────────────


def test_destination_accepts_the_arns_the_waf_console_shows() -> None:
    assert waf.parse_destination("arn:aws:s3:::aws-waf-logs-prod") == {
        "mode": "s3", "bucket": "aws-waf-logs-prod", "key_prefix": "",
    }
    assert waf.parse_destination("s3://aws-waf-logs-prod/central/") == {
        "mode": "s3", "bucket": "aws-waf-logs-prod", "key_prefix": "central/",
    }
    assert waf.parse_destination(
        f"arn:aws:logs:sa-east-1:{_ACCT}:log-group:aws-waf-logs-api:*"
    ) == {"mode": "cloudwatch", "region": "sa-east-1", "account_id": _ACCT, "log_group": "aws-waf-logs-api"}


@pytest.mark.parametrize("value", ["aws-waf-logs-prod", "", "https://exemplo"])
def test_ambiguous_destination_is_refused(value: str) -> None:
    """Bucket e log group usam o MESMO prefixo aws-waf-logs-: nome solto não diz qual é."""
    with pytest.raises(waf.WafDestinationError):
        waf.parse_destination(value)


def test_s3_conn_defaults_to_all_regions_and_role_account() -> None:
    conn = waf.build_conn(
        AwsAuth.role(_ROLE, "e", "s"),
        {"mode": "s3", "bucket": "b", "key_prefix": ""}, None, None,
    )
    assert conn["regions"] == ["*"]
    assert conn["account_id"] == _ACCT
    assert conn["region"] == "us-east-1"


def test_cloudwatch_conn_takes_region_and_account_from_the_arn() -> None:
    dest = waf.parse_destination(f"arn:aws:logs:sa-east-1:{_ACCT}:log-group:aws-waf-logs-api")
    conn = waf.build_conn(AwsAuth.access_key("AKIA", "s"), dest, "us-west-2", None)
    assert conn["region"] == "sa-east-1"
    assert conn["account_id"] == _ACCT
    assert conn["log_group"] == "aws-waf-logs-api"


# ── S3 ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_s3_discovers_every_region_and_web_acl() -> None:
    now = datetime.now(timezone.utc)
    fake = FakeS3({
        _key("us-east-1", "site"): _ndjson(_rec("a")),
        _key("cloudfront", "cdn"): _ndjson(_rec("b"), _rec("c", action="ALLOW")),
        f"AWSLogs/{_ACCT}/CloudTrail/us-east-1/2026/11/09/x.json.gz": b"nao e waf",
    }, now)

    ctx, collector, out = await _collect_s3(fake, _s3_conn())

    assert sorted(r["httpRequest"]["requestId"] for r in out) == ["a", "b", "c"]
    meta = next(r["centralops"] for r in out if r["httpRequest"]["requestId"] == "a")
    assert meta == {
        "source": "s3", "bucket": "aws-waf-logs-x", "key": _key("us-east-1", "site"),
        "line": 1, "region": "us-east-1", "accountId": _ACCT,
    }
    assert ctx.cursor["last_modified"] == now.isoformat()
    assert collector.domain == "s3.us-east-1.amazonaws.com"


@pytest.mark.asyncio
async def test_s3_explicit_regions_only_reads_those() -> None:
    now = datetime.now(timezone.utc)
    fake = FakeS3({
        _key("us-east-1", "site"): _ndjson(_rec("a")),
        _key("sa-east-1", "loja"): _ndjson(_rec("b")),
    }, now)
    _, _, out = await _collect_s3(fake, _s3_conn(regions=("sa-east-1",)))
    assert [r["httpRequest"]["requestId"] for r in out] == ["b"]


@pytest.mark.asyncio
async def test_s3_bad_line_is_dropped_alone_and_corrupt_object_is_recorded() -> None:
    now = datetime.now(timezone.utc)
    good_with_bad_line = gzip.compress(
        (json.dumps(_rec("a")) + "\n{nao-json\n" + json.dumps(_rec("b"))).encode()
    )
    fake = FakeS3({
        _key("us-east-1", "site", 1): good_with_bad_line,
        _key("us-east-1", "site", 2): b"nao e gzip",
    }, now)
    ctx, _, out = await _collect_s3(fake, _s3_conn(regions=("us-east-1",)))
    assert [r["httpRequest"]["requestId"] for r in out] == ["a", "b"]
    assert [s["key"] for s in ctx.cursor["skipped_objects"]] == [_key("us-east-1", "site", 2)]


@pytest.mark.asyncio
async def test_s3_read_failure_stops_the_cycle() -> None:
    """Mesma regra da Fase 0: objeto que não dá para ler não vira perda silenciosa."""

    class _Denied(Exception):
        response = {"Error": {"Code": "AccessDenied", "Message": "kms:Decrypt"}}

    now = datetime.now(timezone.utc)
    fake = FakeS3({_key("us-east-1", "site"): _ndjson(_rec("a"))}, now)

    async def _deny(Bucket, Key):  # noqa: N803
        raise _Denied()

    fake.get_object = _deny  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="waf: falha ao ler"):
        await _collect_s3(fake, _s3_conn(regions=("us-east-1",)), cursor={"last_modified": "2026-11-01T00:00:00+00:00"})


# ── CloudWatch ───────────────────────────────────────────────────────────


def test_cloudwatch_event_is_unwrapped_to_the_waf_record() -> None:
    ev = {
        "eventId": "3721", "timestamp": 1576280412771, "message": json.dumps(_BLOCK),
        "logStreamName": "s", "logGroupName": "aws-waf-logs-api",
        "awsRegion": "sa-east-1", "awsAccountId": _ACCT,
    }
    rec = waf.unwrap_cloudwatch_event(ev)
    assert rec["action"] == "BLOCK"
    assert rec["centralops"]["source"] == "cloudwatch"
    assert rec["centralops"]["eventId"] == "3721"
    assert rec["centralops"]["region"] == "sa-east-1"


def test_non_waf_cloudwatch_message_is_kept_and_marked() -> None:
    rec = waf.unwrap_cloudwatch_event({"eventId": "1", "timestamp": 5, "message": "texto solto"})
    assert rec["unparsedMessage"] == "texto solto"
    assert rec["timestamp"] == 5


@pytest.mark.asyncio
async def test_cloudwatch_mode_uses_the_cloudwatch_engine() -> None:
    dest = waf.parse_destination(f"arn:aws:logs:sa-east-1:{_ACCT}:log-group:aws-waf-logs-api")
    conn = waf.build_conn(AwsAuth.access_key("AKIA", "s"), dest, None, None)

    class _Logs:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def filter_log_events(self, **kw):
            assert kw["logGroupName"] == "aws-waf-logs-api"
            return {"events": [{"eventId": "e1", "timestamp": 1, "message": json.dumps(_BLOCK)}]}

    from ..vendors.aws_cloudwatch import AWSCloudWatchCollector

    ctx = _ctx()
    collector = AWSWafCollector(ctx)
    with patch.object(AWSWafCollector, "_load_conn", return_value=conn), \
         patch.object(AWSCloudWatchCollector, "_client", return_value=_Logs()):
        out = [ev async for ev in collector.collect()]
    assert out[0]["terminatingRuleId"] == "STMTest_SQLi_XSS"
    assert out[0]["centralops"]["logGroupName"] == "aws-waf-logs-api"
    assert "start_time_ms" in ctx.cursor
    assert collector.domain == "logs.sa-east-1.amazonaws.com"


# ── identidade e watermark ───────────────────────────────────────────────


def test_message_id_prefers_request_id_and_falls_back_to_origin_position() -> None:
    c = AWSWafCollector(_ctx())
    assert c.extract_message_id(_BLOCK) == f"{_BLOCK['webaclId']}|rid|1576280412771"
    no_id = _rec("null")
    no_id["centralops"] = {"source": "s3", "key": "k.log.gz", "line": 4}
    assert c.extract_message_id(no_id) == "s3|k.log.gz|4"
    no_id["centralops"] = {"source": "cloudwatch", "eventId": "e9"}
    assert c.extract_message_id(no_id) == "cw|e9"


def test_watermark_follows_the_active_engine_cursor() -> None:
    s3 = AWSWafCollector.watermark_at({"last_modified": "2026-11-09T10:00:00+00:00"})
    cw = AWSWafCollector.watermark_at({"start_time_ms": 1_782_036_000_000})
    assert s3 is not None and s3.year == 2026
    assert cw is not None and cw.year == 2026


# ── normalização OCSF 4002 ───────────────────────────────────────────────


def test_block_record_normalizes_to_valid_http_activity() -> None:
    rec = copy.deepcopy(_BLOCK)
    rec["centralops"] = {"source": "s3", "region": "ap-southeast-2", "accountId": _ACCT}
    n = _normalize(rec)

    assert (n["class_uid"], n["activity_id"], n["type_uid"]) == (4002, 3, 400203)
    assert (n["disposition_id"], n["disposition"]) == (2, "Blocked")
    assert (n["action_id"], n["severity_id"], n["is_alert"]) == (2, 2, True)
    assert n["time"] == 1576280412771
    assert n["src_endpoint"]["ip"] == "1.1.1.1"
    assert n["src_endpoint"]["location"]["country"] == "AU"
    assert n["http_request"]["user_agent"] == "curl/7.61.1"
    assert n["http_request"]["url"]["hostname"] == "localhost:1989"
    assert n["http_request"]["url"]["path"] == "/myUri"
    assert n["firewall_rule"]["uid"] == "STMTest_SQLi_XSS"
    assert n["firewall_rule"]["match_location"] == "HEADER"
    assert n["unmapped"]["aws_waf_attack_type"] == "SQL_INJECTION"
    assert n["cloud"]["account"]["uid"] == _ACCT
    assert n["metadata"]["labels"] == ["value"]
    result = validate_normalized(n)
    assert result.valid, result.reason


@pytest.mark.parametrize(
    "action,extra,disposition_id,severity_id",
    [
        ("ALLOW", {}, 1, 1),
        ("ALLOW", {"nonTerminatingMatchingRules": [{"ruleId": "regra-em-count", "action": "COUNT"}]}, 20, 3),
        ("CAPTCHA", {}, 22, 2),
        ("CHALLENGE", {}, 23, 2),
    ],
)
def test_action_maps_to_ocsf_disposition(action, extra, disposition_id, severity_id) -> None:
    n = _normalize(_rec("r1", action=action, **extra))
    assert n["disposition_id"] == disposition_id
    assert n["severity_id"] == severity_id
    assert validate_normalized(n).valid


def test_allowed_request_that_matched_a_count_rule_lists_the_rule() -> None:
    n = _normalize(_rec("r1", action="ALLOW", nonTerminatingMatchingRules=[{"ruleId": "SQLi-count", "action": "COUNT"}]))
    assert n["unmapped"]["aws_waf_count_rules"] == ["SQLi-count"]
    assert n["is_alert"] is True


def test_unknown_country_dash_is_not_written_as_a_country() -> None:
    rec = _rec("r1")
    rec["httpRequest"]["country"] = "-"
    n = _normalize(rec)
    assert (n.get("src_endpoint") or {}).get("location", {}).get("country") is None


def test_lowercase_http2_headers_are_found() -> None:
    rec = _rec("r1")
    rec["httpRequest"]["headers"] = [
        {"name": ":authority", "value": "api.exemplo.com"},
        {"name": "user-agent", "value": "Mozilla/5.0"},
    ]
    n = _normalize(rec)
    assert n["http_request"]["url"]["hostname"] == "api.exemplo.com"
    assert n["http_request"]["user_agent"] == "Mozilla/5.0"


# ── registro e probe ─────────────────────────────────────────────────────


def test_registered_with_role_onboarding() -> None:
    from ..capabilities import invalid_capabilities
    from ..registry import get, get_platform, has

    assert has("aws_waf", "events")
    plat = get_platform("aws_waf")
    assert plat.auth_setup == "aws_assume_role"
    assert invalid_capabilities(plat.capabilities) == []
    fields = {f.key: f for f in plat.auth_fields}
    assert fields["base_url"].required and not fields["secret_access_key"].required
    assert get("aws_waf", "events").collector_cls is AWSWafCollector


@pytest.mark.asyncio
async def test_probe_refuses_ambiguous_destination_before_network() -> None:
    result = await waf._waf_probe({"client_id": "AKIA", "secret_access_key": "s", "base_url": "aws-waf-logs-x"})
    assert result.ok is False
    assert "ARN" in result.detail
