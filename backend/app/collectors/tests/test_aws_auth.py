"""Credencial AWS dos coletores: papel cross-account (AssumeRole + External ID).

Sem rede: a chamada STS é o seam ``_assume_role`` / ``_get_caller_identity``.
O External ID vem do banco no produto; aqui o resolvedor é injetado.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest

from ..auth import aws as aws_auth
from ..auth.aws import AwsAuth, AwsAuthError

_ROLE = "arn:aws:iam::123456789012:role/CentralOpsReadOnly"


class _AwsError(Exception):
    """Imita ``botocore.exceptions.ClientError`` (só o atributo ``response``)."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.response = {"Error": {"Code": code, "Message": message}}


@pytest.fixture(autouse=True)
def _clean_caches():
    aws_auth.reset_caches()
    yield
    aws_auth.reset_caches()


def _sts_reply(expires_in_s: int = 3600) -> Dict[str, Any]:
    return {
        "Credentials": {
            "AccessKeyId": "ASIATEMP",
            "SecretAccessKey": "temp-secret",
            "SessionToken": "temp-token",
            "Expiration": datetime.now(timezone.utc) + timedelta(seconds=expires_in_s),
        }
    }


# ── Modo inferido do campo de identidade ──────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        _ROLE,
        "arn:aws:iam::123456789012:role/path/to/CentralOps",
        "arn:aws-us-gov:iam::123456789012:role/CentralOps",
        "arn:aws-cn:iam::123456789012:role/CentralOps",
    ],
)
def test_role_arn_is_detected(value: str) -> None:
    assert aws_auth.is_role_arn(value)
    assert aws_auth.role_account_id(value) == "123456789012"


@pytest.mark.parametrize(
    "value",
    [
        "AKIAIOSFODNN7EXAMPLE",
        "arn:aws:iam::123456789012:user/alguem",  # usuário, não papel
        "arn:aws:iam::12345:role/curta",  # conta sem 12 dígitos
        "",
        None,
    ],
)
def test_non_role_values_stay_access_key_mode(value) -> None:
    assert not aws_auth.is_role_arn(value)


def test_session_name_identifies_org_and_integration_within_aws_limit() -> None:
    assert aws_auth.session_name_for(5, 99) == "centralops-o5-i99"
    assert aws_auth.session_name_for(5) == "centralops-o5-probe"
    assert len(aws_auth.session_name_for(10**30, 10**30)) <= 64


# ── Probe pré-save: External ID vem do SERVIDOR, nunca do corpo ──────────


def test_probe_role_mode_uses_org_external_id_and_ignores_body_value() -> None:
    asked: List[int] = []

    def _resolver(org_id: int) -> str:
        asked.append(org_id)
        return "centralops-da-org-7"

    auth = aws_auth.auth_for_probe(
        {"client_id": _ROLE, "organization_id": 7, "external_id": "valor-forjado"},
        external_id_for=_resolver,
    )
    assert auth.mode == aws_auth.MODE_ROLE
    assert auth.external_id == "centralops-da-org-7"
    assert asked == [7]
    assert auth.session_name == "centralops-o7-probe"


@pytest.mark.parametrize("org", [None, "7", True, 0.5])
def test_probe_role_mode_requires_validated_integer_org(org) -> None:
    cfg: Dict[str, Any] = {"client_id": _ROLE}
    if org is not None:
        cfg["organization_id"] = org
    with pytest.raises(AwsAuthError, match="organização"):
        aws_auth.auth_for_probe(cfg, external_id_for=lambda _o: "nunca")


def test_probe_access_key_mode_requires_secret() -> None:
    with pytest.raises(AwsAuthError):
        aws_auth.auth_for_probe({"client_id": "AKIAX"}, external_id_for=lambda _o: "x")
    auth = aws_auth.auth_for_probe(
        {"client_id": "AKIAX", "secret_access_key": "s"}, external_id_for=lambda _o: "x"
    )
    assert auth == AwsAuth.access_key("AKIAX", "s")


def test_auth_repr_never_shows_secrets() -> None:
    text = repr(AwsAuth.role(_ROLE, "ext-secreto", "s")) + repr(AwsAuth.access_key("AKIA", "sk-secreto"))
    assert "ext-secreto" not in text
    assert "sk-secreto" not in text


# ── AssumeRole: cache, renovação e erro ──────────────────────────────────


def test_assume_role_passes_external_id_and_caches_until_near_expiry(monkeypatch) -> None:
    calls: List[AwsAuth] = []

    async def _fake(auth, region):
        calls.append(auth)
        return _sts_reply()

    monkeypatch.setattr(aws_auth, "_assume_role", _fake)
    auth = AwsAuth.role(_ROLE, "ext-1", "centralops-o1-i2")

    kw1 = asyncio.run(aws_auth.client_kwargs(auth, "us-east-1"))
    kw2 = asyncio.run(aws_auth.client_kwargs(auth, "us-east-1"))

    assert len(calls) == 1  # segunda vez veio do cache
    assert calls[0].external_id == "ext-1"
    assert kw1 == kw2 == {
        "aws_access_key_id": "ASIATEMP",
        "aws_secret_access_key": "temp-secret",
        "aws_session_token": "temp-token",
        "region_name": "us-east-1",
    }


def test_assume_role_renews_when_credential_is_about_to_expire(monkeypatch) -> None:
    calls: List[int] = []

    async def _fake(auth, region):
        calls.append(1)
        # Expira antes da folga de renovação ⇒ nunca serve do cache.
        return _sts_reply(expires_in_s=60)

    monkeypatch.setattr(aws_auth, "_assume_role", _fake)
    auth = AwsAuth.role(_ROLE, "ext-1", "s")
    asyncio.run(aws_auth.client_kwargs(auth, "us-east-1"))
    asyncio.run(aws_auth.client_kwargs(auth, "us-east-1"))
    assert len(calls) == 2


def test_cache_is_keyed_by_external_id(monkeypatch) -> None:
    """Duas orgs com o MESMO papel (erro de config de um cliente) não podem
    compartilhar credencial: a chave do cache inclui o External ID."""
    seen: List[str] = []

    async def _fake(auth, region):
        seen.append(auth.external_id)
        return _sts_reply()

    monkeypatch.setattr(aws_auth, "_assume_role", _fake)
    asyncio.run(aws_auth.client_kwargs(AwsAuth.role(_ROLE, "org-a", "s"), "us-east-1"))
    asyncio.run(aws_auth.client_kwargs(AwsAuth.role(_ROLE, "org-b", "s"), "us-east-1"))
    assert seen == ["org-a", "org-b"]


def test_assume_role_denied_becomes_safe_actionable_error(monkeypatch) -> None:
    async def _denied(auth, region):
        raise _AwsError("AccessDenied", "not authorized to perform sts:AssumeRole")

    monkeypatch.setattr(aws_auth, "_assume_role", _denied)
    auth = AwsAuth.role(_ROLE, "ext-nao-pode-vazar", "s")
    with pytest.raises(AwsAuthError) as err:
        asyncio.run(aws_auth.client_kwargs(auth, "us-east-1"))
    msg = str(err.value)
    assert "AssumeRole: AccessDenied" in msg
    assert "trust policy" in msg
    assert "ext-nao-pode-vazar" not in msg


def test_access_key_mode_never_calls_sts(monkeypatch) -> None:
    calls: List[int] = []

    async def _fake(auth, region):
        calls.append(1)
        return _sts_reply()

    monkeypatch.setattr(aws_auth, "_assume_role", _fake)
    kw = asyncio.run(aws_auth.client_kwargs(AwsAuth.access_key("AKIA", "sk"), "sa-east-1"))
    assert calls == []
    assert kw == {"aws_access_key_id": "AKIA", "aws_secret_access_key": "sk", "region_name": "sa-east-1"}


# ── Mensagens de erro ─────────────────────────────────────────────────────


def test_describe_error_separates_kms_from_read_permission() -> None:
    kms = aws_auth.describe_error(
        _AwsError("AccessDenied", "User is not authorized to perform kms:Decrypt"),
        operation="GetObject",
    )
    read = aws_auth.describe_error(_AwsError("AccessDenied", "Access Denied"), operation="GetObject")
    assert "kms:Decrypt" in kms
    assert "leitura" in read and "kms" not in read.lower()
    assert "trust policy" not in kms + read


def test_describe_error_without_server_identity() -> None:
    class NoCredentialsError(Exception):
        pass

    msg = aws_auth.describe_error(NoCredentialsError(), operation="AssumeRole")
    assert "identidade AWS própria" in msg


# ── Onboarding ───────────────────────────────────────────────────────────


def test_principal_from_assumed_role_session_becomes_role_arn(monkeypatch) -> None:
    async def _ident():
        return {"Arn": "arn:aws:sts::111122223333:assumed-role/centralops-broker/i-0abc"}

    monkeypatch.setattr(aws_auth, "_get_caller_identity", _ident)
    arn, err = asyncio.run(aws_auth.trust_principal_arn())
    assert err is None
    assert arn == "arn:aws:iam::111122223333:role/centralops-broker"


def test_principal_unavailable_reports_reason_instead_of_raising(monkeypatch) -> None:
    class NoCredentialsError(Exception):
        pass

    async def _ident():
        raise NoCredentialsError()

    monkeypatch.setattr(aws_auth, "_get_caller_identity", _ident)
    arn, err = asyncio.run(aws_auth.trust_principal_arn())
    assert arn is None
    assert err and "credenciais AWS ausentes" in err


def test_template_trust_requires_external_id_and_principal() -> None:
    tpl = aws_auth.cloudformation_template("centralops-abc", "arn:aws:iam::111122223333:role/broker")
    params = tpl["Parameters"]
    assert params["ExternalId"]["Default"] == "centralops-abc"
    assert params["TrustedPrincipalArn"]["Default"] == "arn:aws:iam::111122223333:role/broker"
    role = tpl["Resources"]["CentralOpsRole"]["Properties"]
    (stmt,) = role["AssumeRolePolicyDocument"]["Statement"]
    assert stmt["Action"] == "sts:AssumeRole"
    assert stmt["Principal"] == {"AWS": {"Ref": "TrustedPrincipalArn"}}
    assert stmt["Condition"] == {"StringEquals": {"sts:ExternalId": {"Ref": "ExternalId"}}}
    # Só leitura: nenhuma ação de escrita em lugar nenhum do template.
    import json

    actions = json.dumps(role["Policies"])
    for verb in ("Put", "Delete", "Create", "Update", "s3:*", '"*:*"'):
        assert verb not in actions, verb
    assert tpl["Outputs"]["RoleArn"]


def test_template_without_principal_leaves_parameter_for_the_customer() -> None:
    tpl = aws_auth.cloudformation_template("centralops-abc", None)
    assert "Default" not in tpl["Parameters"]["TrustedPrincipalArn"]


def test_cached_credential_expiration_without_datetime_falls_back_to_session_length(monkeypatch) -> None:
    async def _fake(auth, region):
        reply = _sts_reply()
        reply["Credentials"]["Expiration"] = None
        return reply

    monkeypatch.setattr(aws_auth, "_assume_role", _fake)
    asyncio.run(aws_auth.client_kwargs(AwsAuth.role(_ROLE, "e", "s"), "us-east-1"))
    (_, expires_at), = aws_auth._CRED_CACHE.values()
    assert expires_at > time.time() + 3000
