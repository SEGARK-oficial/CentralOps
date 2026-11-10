"""AWS via Firehose — decodificação do pedido do HTTP endpoint em eventos.

O que se prova: cada caminho que a AWS usa para pôr um log no Firehose
(WAF direto, assinatura do CloudWatch Logs em gzip, EventBridge) sai do
decodificador com a MESMA forma que a integração de pull entrega, e por isso
o mesmo mapping produz o mesmo OCSF. Os registros de exemplo são os mesmos
dos testes do WAF e dos achados.
"""

from __future__ import annotations

import base64
import copy
import gzip
import json
from typing import Any, Dict, List

import pytest

from ..normalize import engine as E
from ..normalize.defaults import DEFAULT_MAPPING_FILES, load_default_rules
from ..normalize.ocsf.validator import validate_normalized
from ..registry import get_platform, supported_streams
from ..vendors import aws_firehose as fh
from .test_aws_findings import ASFF, GD
from .test_aws_waf import _BLOCK

_ACCT = "111122223333"
_SOURCE_ARN = f"arn:aws:firehose:sa-east-1:{_ACCT}:deliverystream/aws-waf-logs-centralops"

_CLOUDTRAIL = {
    "eventVersion": "1.09",
    "userIdentity": {"type": "IAMUser", "userName": "alice", "accountId": _ACCT},
    "eventTime": "2026-11-09T10:00:00Z",
    "eventSource": "iam.amazonaws.com",
    "eventName": "CreateAccessKey",
    "awsRegion": "us-east-1",
    "sourceIPAddress": "198.51.100.7",
    "userAgent": "aws-cli/2.15.0",
    "eventID": "3a1b2c3d-0000-4000-8000-000000000001",
    "eventType": "AwsApiCall",
    "recipientAccountId": _ACCT,
}


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _request(*payloads: bytes, request_id: str = "req-1") -> bytes:
    return json.dumps(
        {"requestId": request_id, "timestamp": 1762682400000, "records": [{"data": _b64(p)} for p in payloads]}
    ).encode()


def _cw_envelope(messages: List[str], *, log_group: str = "aws-cloudtrail-logs", message_type: str = "DATA_MESSAGE") -> bytes:
    env = {
        "messageType": message_type,
        "owner": _ACCT,
        "logGroup": log_group,
        "logStream": f"{_ACCT}_CloudTrail_us-east-1",
        "subscriptionFilters": ["centralops"],
        "logEvents": [
            {"id": f"3836000000000000000000000000000000000000000000000000000{i}", "timestamp": 1762682400000 + i, "message": m}
            for i, m in enumerate(messages)
        ],
    }
    return gzip.compress(json.dumps(env).encode())


def _camel(value: Any) -> Any:
    """Inverso do ``pascalize``: o formato do achado do GuardDuty no EventBridge."""
    if isinstance(value, dict):
        return {k[:1].lower() + k[1:]: _camel(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_camel(v) for v in value]
    return value


def _eventbridge(detail_type: str, source: str, detail: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "version": "0",
        "id": "c8d5f0a1-0000-4000-8000-000000000000",
        "detail-type": detail_type,
        "source": source,
        "account": _ACCT,
        "time": "2026-11-09T10:05:00Z",
        "region": "us-east-1",
        "resources": [],
        "detail": detail,
    }


def _normalize(platform: str, event_type: str, rec: Dict[str, Any]) -> Dict[str, Any]:
    rules = E.compile_rules(load_default_rules(platform, event_type))
    return E.apply_compiled(rules, rec).output["normalized"]


# ── registro da plataforma ───────────────────────────────────────────────


def test_platform_is_push_with_one_stream_per_log_type_and_a_mapping_each() -> None:
    reg = get_platform(fh.PLATFORM)
    assert reg is not None and reg.transport == "push"
    assert set(supported_streams(fh.PLATFORM)) == {"waf", "cloudtrail", "guardduty", "securityhub", "cloudwatch_logs"}
    for cls in fh.COLLECTORS:
        assert (fh.PLATFORM, cls.event_type) in DEFAULT_MAPPING_FILES, cls.event_type


# ── pedido ───────────────────────────────────────────────────────────────


def test_body_that_is_not_a_firehose_request_is_refused() -> None:
    with pytest.raises(fh.FirehoseRequestError):
        fh.decode_request(b"not json", stream="waf")
    with pytest.raises(fh.FirehoseRequestError):
        fh.decode_request(b'{"requestId": "x"}', stream="waf")


def test_bad_record_is_counted_and_the_rest_of_the_batch_survives() -> None:
    body = json.dumps({
        "requestId": "r",
        "records": [
            {"data": "%%% não é base64 %%%"},
            {"nodata": True},
            {"data": _b64(b"\x1f\x8b corrompido")},
            {"data": _b64(json.dumps(_BLOCK).encode())},
        ],
    }).encode()
    batch = fh.decode_request(body, stream="waf")
    assert batch.request_id == "r"
    assert batch.records == 4
    assert batch.errors == 3
    assert len(batch.events) == 1


def test_gzip_bomb_is_stopped_at_the_request_budget() -> None:
    bomb = gzip.compress(b"{" + b" " * (4 * 1024 * 1024) + b"}")
    with pytest.raises(fh.FirehoseTooLarge):
        fh.decode_request(_request(bomb), stream="cloudwatch_logs", budget=1024 * 1024)


def test_record_may_carry_several_json_concatenated_or_newline_delimited() -> None:
    a = dict(_BLOCK, timestamp=1)
    b = dict(_BLOCK, timestamp=2)
    c = dict(_BLOCK, timestamp=3)
    raw = (json.dumps(a) + json.dumps(b) + "\n" + json.dumps(c) + "\n").encode()
    batch = fh.decode_request(_request(raw), stream="waf")
    assert [e["timestamp"] for e in batch.events] == [1, 2, 3]
    assert batch.errors == 0


# ── WAF ──────────────────────────────────────────────────────────────────


def test_waf_direct_to_firehose_gets_account_and_region_from_the_web_acl() -> None:
    batch = fh.decode_request(_request(json.dumps(_BLOCK).encode() + b"\n"), stream="waf", source_arn=_SOURCE_ARN)
    (ev,) = batch.events
    assert ev["centralops"] == {
        "source": "firehose",
        "region": "ap-southeast-2",
        "accountId": _ACCT,
        "deliveryStream": "aws-waf-logs-centralops",
    }
    n = _normalize(fh.PLATFORM, "aws_firehose.waf", ev)
    pull = _normalize("aws_waf", "aws_waf.event", dict(_BLOCK, centralops={"source": "s3", "region": "ap-southeast-2", "accountId": _ACCT}))
    assert n["class_uid"] == 4002
    assert n["disposition_id"] == pull["disposition_id"]
    assert n["src_endpoint"] == pull["src_endpoint"]
    assert n["cloud"]["region"] == "ap-southeast-2"
    assert n["metadata"]["log_name"] == "firehose"
    assert validate_normalized(n).valid


def test_waf_through_a_cloudwatch_subscription_is_unwrapped() -> None:
    batch = fh.decode_request(
        _request(_cw_envelope([json.dumps(_BLOCK)], log_group="aws-waf-logs-prod")),
        stream="waf", source_arn=_SOURCE_ARN,
    )
    (ev,) = batch.events
    assert ev["action"] == "BLOCK"
    assert ev["centralops"]["source"] == "firehose"
    assert ev["centralops"]["logGroupName"] == "aws-waf-logs-prod"
    assert ev["centralops"]["accountId"] == _ACCT
    assert ev["centralops"]["region"] == "sa-east-1"


def test_waf_message_id_is_stable_across_redelivery() -> None:
    body = _request(json.dumps(_BLOCK).encode())
    a = fh.decode_request(body, stream="waf", source_arn=_SOURCE_ARN).events[0]
    b = fh.decode_request(body, stream="waf", source_arn=_SOURCE_ARN).events[0]
    from ..vendors.aws_waf import waf_message_id

    assert waf_message_id(a) == waf_message_id(b) != ""


# ── CloudTrail ───────────────────────────────────────────────────────────


def test_cloudtrail_from_a_log_group_subscription_matches_the_s3_record() -> None:
    raw = _cw_envelope([json.dumps(_CLOUDTRAIL)])
    batch = fh.decode_request(_request(raw), stream="cloudtrail", source_arn=_SOURCE_ARN)
    assert batch.events == [_CLOUDTRAIL]
    n = _normalize(fh.PLATFORM, "aws_firehose.cloudtrail", batch.events[0])
    assert n == _normalize("aws_cloudtrail", "aws_cloudtrail.event", _CLOUDTRAIL)
    assert n["class_uid"] == 6003


def test_cloudtrail_from_eventbridge_and_from_a_records_file() -> None:
    eb = _eventbridge("AWS API Call via CloudTrail", "aws.iam", _CLOUDTRAIL)
    file_ = {"Records": [_CLOUDTRAIL, dict(_CLOUDTRAIL, eventID="outro")]}
    batch = fh.decode_request(_request(json.dumps(eb).encode(), json.dumps(file_).encode()), stream="cloudtrail")
    assert [e["eventID"] for e in batch.events] == [_CLOUDTRAIL["eventID"], _CLOUDTRAIL["eventID"], "outro"]


def test_control_message_is_dropped_without_error() -> None:
    raw = _cw_envelope(["CWL CONTROL MESSAGE: Checking health of destination Firehose."], message_type="CONTROL_MESSAGE")
    batch = fh.decode_request(_request(raw), stream="cloudtrail")
    assert batch.events == [] and batch.errors == 0


def test_non_json_message_in_a_json_stream_is_counted() -> None:
    batch = fh.decode_request(_request(_cw_envelope(["linha solta"])), stream="cloudtrail")
    assert batch.events == [] and batch.errors == 1


# ── GuardDuty e Security Hub (EventBridge) ───────────────────────────────


def test_guardduty_eventbridge_camelcase_becomes_the_api_shape() -> None:
    # Cópia sem o bloco ``centralops``: o teste do coletor de pull o acrescenta
    # ao mesmo dict ``GD`` e, sob xdist, a ordem dos testes muda.
    gd = copy.deepcopy({k: v for k, v in GD.items() if k != "centralops"})
    detail = _camel(gd)
    assert detail["service"]["action"]["awsApiCallAction"]["remoteIpDetails"]["ipAddressV4"] == "198.51.100.7"
    eb = _eventbridge("GuardDuty Finding", "aws.guardduty", detail)
    batch = fh.decode_request(_request(json.dumps(eb).encode()), stream="guardduty")
    (ev,) = batch.events
    # Mesma forma do coletor de pull, inclusive o bloco que ele acrescenta.
    assert ev == dict(gd, centralops={"detectorId": "abc", "region": "us-east-1"})
    n = _normalize(fh.PLATFORM, "aws_firehose.guardduty", ev)
    assert n == _normalize("aws_guardduty", "aws_guardduty.finding", gd)
    assert n["class_uid"] == 2004
    assert validate_normalized(n).valid


def test_security_hub_eventbridge_yields_one_event_per_finding() -> None:
    second = dict(ASFF, Id=ASFF["Id"] + "-2")
    eb = _eventbridge("Security Hub Findings - Imported", "aws.securityhub", {"findings": [ASFF, second]})
    batch = fh.decode_request(_request(json.dumps(eb).encode()), stream="securityhub")
    assert batch.events == [ASFF, second]
    n = _normalize(fh.PLATFORM, "aws_firehose.securityhub", batch.events[0])
    assert n == _normalize("aws_securityhub", "aws_securityhub.finding", ASFF)


def test_eventbridge_event_on_the_wrong_stream_is_counted_not_shipped() -> None:
    eb = _eventbridge("GuardDuty Finding", "aws.guardduty", _camel(GD))
    batch = fh.decode_request(_request(json.dumps(eb).encode()), stream="waf")
    assert batch.events == [] and batch.errors == 1


# ── CloudWatch Logs genérico ─────────────────────────────────────────────


def test_cloudwatch_logs_stream_keeps_the_pull_collector_shape() -> None:
    raw = _cw_envelope(['{"level": "error", "msg": "falhou"}', "texto livre"], log_group="/app/api")
    batch = fh.decode_request(_request(raw), stream="cloudwatch_logs", source_arn=_SOURCE_ARN)
    first, second = batch.events
    assert first["logGroupName"] == "/app/api"
    assert first["awsAccountId"] == _ACCT and first["awsRegion"] == "sa-east-1"
    assert first["eventTime"] == "2025-11-09T10:00:00.000Z"
    assert second["message"] == "texto livre"
    n = _normalize(fh.PLATFORM, "aws_firehose.cloudwatch_logs", first)
    assert n["cloud"]["region"] == "sa-east-1"


def test_other_push_platform_only_gets_the_transport_unwrapped() -> None:
    eb = _eventbridge("GuardDuty Finding", "aws.guardduty", {"id": "x"})
    batch = fh.decode_request(_request(json.dumps(eb).encode(), b"texto"), stream="events", aws_specific=False)
    assert batch.events == [eb, {"message": "texto"}]


# ── coletores virtuais ───────────────────────────────────────────────────


def test_collectors_use_the_aws_native_id_and_fall_back_to_the_content_hash() -> None:
    def _id(cls, event):
        return cls.extract_message_id(object.__new__(cls), event)

    assert _id(fh.FirehoseCloudTrailCollector, _CLOUDTRAIL) == _CLOUDTRAIL["eventID"]
    assert _id(fh.FirehoseGuardDutyCollector, GD) == f"def|{GD['UpdatedAt']}"
    assert _id(fh.FirehoseSecurityHubCollector, ASFF) == f"{ASFF['Id']}|{ASFF['UpdatedAt']}"
    assert _id(fh.FirehoseCloudWatchCollector, {"eventId": "e1"}) == "e1"
    assert _id(fh.FirehoseCloudTrailCollector, {"_ingest": {"id": "hash"}}) == "hash"
