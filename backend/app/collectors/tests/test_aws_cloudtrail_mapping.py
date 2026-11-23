"""Mapping CloudTrail → OCSF API Activity: o que o SOC precisa ler do evento.

O mesmo mapping serve o bucket (``aws_cloudtrail``) e o Firehose
(``aws_firehose.cloudtrail``). Amostras no formato documentado do CloudTrail,
com conta e IP de documentação.
"""

from __future__ import annotations

import copy
from typing import Any, Dict

import pytest

from ..normalize import engine as E
from ..normalize.defaults import load_default_rules
from ..normalize.ocsf.validator import validate_normalized

_ACCT = "111122223333"

_RULES = E.compile_rules(load_default_rules("aws_cloudtrail", "aws_cloudtrail.event"))

# Login no console que FALHOU: a AWS não preenche errorCode, a falha vem na resposta.
CONSOLE_LOGIN_FAILED: Dict[str, Any] = {
    "eventVersion": "1.09",
    "userIdentity": {
        "type": "IAMUser",
        "principalId": "AIDAEXAMPLE",
        "accountId": _ACCT,
        "accessKeyId": "",
        "userName": "ana",
    },
    "eventTime": "2026-11-23T10:00:00Z",
    "eventSource": "signin.amazonaws.com",
    "eventName": "ConsoleLogin",
    "awsRegion": "us-east-1",
    "sourceIPAddress": "198.51.100.7",
    "userAgent": "Mozilla/5.0",
    "errorMessage": "Failed authentication",
    "requestParameters": None,
    "responseElements": {"ConsoleLogin": "Failure"},
    "additionalEventData": {
        "LoginTo": "https://console.aws.amazon.com/console/home",
        "MobileVersion": "No",
        "MFAUsed": "No",
    },
    "eventID": "11111111-0000-4000-8000-000000000001",
    "readOnly": False,
    "eventType": "AwsConsoleSignIn",
    "managementEvent": True,
    "recipientAccountId": _ACCT,
    "eventCategory": "Management",
}

# Inferência no Bedrock com chave de longa duração (o padrão do LLMjacking).
INVOKE_MODEL: Dict[str, Any] = {
    "eventVersion": "1.09",
    "userIdentity": {
        "type": "IAMUser",
        "principalId": "AIDAEXAMPLE",
        "arn": f"arn:aws:iam::{_ACCT}:user/ci-deploy",
        "accountId": _ACCT,
        "accessKeyId": "AKIAIOSFODNN7EXAMPLE",
        "userName": "ci-deploy",
    },
    "eventTime": "2026-11-23T10:05:00Z",
    "eventSource": "bedrock.amazonaws.com",
    "eventName": "InvokeModel",
    "awsRegion": "us-east-1",
    "sourceIPAddress": "203.0.113.9",
    "userAgent": "python-requests/2.32",
    "requestParameters": {"modelId": "anthropic.claude-3-sonnet-20240229-v1:0"},
    "responseElements": None,
    "eventID": "11111111-0000-4000-8000-000000000002",
    "readOnly": True,
    "eventType": "AwsApiCall",
    "managementEvent": True,
    "recipientAccountId": _ACCT,
    "eventCategory": "Management",
}


def _n(rec: Dict[str, Any]) -> Dict[str, Any]:
    out = E.apply_compiled(_RULES, rec).output["normalized"]
    result = validate_normalized(out)
    assert result.valid, result.reason
    return out


def _with(base: Dict[str, Any], **over) -> Dict[str, Any]:
    r = copy.deepcopy(base)
    r.update(over)
    return r


# ── status ───────────────────────────────────────────────────────────────


def test_failed_console_login_is_a_failure_even_without_error_code() -> None:
    n = _n(CONSOLE_LOGIN_FAILED)
    assert (n["status"], n["status_id"]) == ("Failure", 2)
    assert n["status_detail"] == "Failed authentication"
    assert n["actor"]["session"]["is_mfa"] is False
    # O ConsoleLogin traz accessKeyId vazio: não vira credencial "".
    assert n["actor"]["session"].get("credential_uid") is None


def test_successful_console_login_with_mfa() -> None:
    ok = _with(
        CONSOLE_LOGIN_FAILED,
        responseElements={"ConsoleLogin": "Success"},
        additionalEventData={"MFAUsed": "Yes"},
    )
    ok.pop("errorMessage")
    n = _n(ok)
    assert (n["status"], n["status_id"]) == ("Success", 1)
    assert n["actor"]["session"]["is_mfa"] is True


def test_api_error_is_failure_with_the_aws_code_in_status_code() -> None:
    denied = _with(
        INVOKE_MODEL,
        errorCode="AccessDeniedException",
        errorMessage="User is not authorized to perform: bedrock:InvokeModel",
    )
    n = _n(denied)
    assert (n["status"], n["status_id"]) == ("Failure", 2)
    assert n["status_code"] == "AccessDeniedException"
    assert n["status_detail"].startswith("User is not authorized")
    assert n["api"]["response"]["error"] == "AccessDeniedException"


def test_successful_api_call() -> None:
    n = _n(INVOKE_MODEL)
    assert (n["status"], n["status_id"]) == ("Success", 1)
    assert n.get("status_code") is None


# ── credencial, MFA e recurso ────────────────────────────────────────────


def test_access_key_is_the_session_credential() -> None:
    n = _n(INVOKE_MODEL)
    assert n["actor"]["session"]["credential_uid"] == "AKIAIOSFODNN7EXAMPLE"


@pytest.mark.parametrize("raw,expected", [("true", True), ("false", False)])
def test_mfa_from_the_session_context_of_api_calls(raw: str, expected: bool) -> None:
    rec = _with(INVOKE_MODEL)
    rec["userIdentity"]["sessionContext"] = {"attributes": {"mfaAuthenticated": raw}}
    assert _n(rec)["actor"]["session"]["is_mfa"] is expected


def test_no_mfa_information_leaves_is_mfa_out() -> None:
    session = _n(INVOKE_MODEL)["actor"].get("session") or {}
    assert "is_mfa" not in session


def test_bedrock_model_is_the_resource_of_the_call() -> None:
    n = _n(INVOKE_MODEL)
    assert n["resources"] == [
        {"type": "AWS::Bedrock::Model", "uid": "anthropic.claude-3-sonnet-20240229-v1:0"}
    ]
    assert n["api"]["service"]["name"] == "bedrock.amazonaws.com"
    assert n["api"]["operation"] == "InvokeModel"


def test_call_without_model_has_no_resources() -> None:
    assert "resources" not in _n(CONSOLE_LOGIN_FAILED)


def test_firehose_cloudtrail_shares_the_same_mapping() -> None:
    fh = E.compile_rules(load_default_rules("aws_firehose", "aws_firehose.cloudtrail"))
    assert E.apply_compiled(fh, INVOKE_MODEL).output["normalized"] == _n(INVOKE_MODEL)
