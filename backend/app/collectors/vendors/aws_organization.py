"""Pais de AWS Organization: uma integração por conta, criada pelo EE.

Três cards de catálogo, um por fonte, todos ``variant="organization"`` (o create
grava ``kind=organization``). O pai não coleta (o scheduler pula
partner/organization); o provider (``providers/aws``) lista as contas e diz como
cada filha nasce. A criação das filhas é da edição Enterprise — na Community o pai
é criado e a descoberta registra ``enterprise_required``.

Credencial: papel com External ID numa conta que pode chamar
``organizations:ListAccounts`` — a de gerenciamento ou uma administradora delegada.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict

from ..auth import aws as aws_auth


def _provider(integration):
    from ...providers.aws.provider import AwsOrganizationProvider

    return AwsOrganizationProvider(integration)


async def _probe(cfg: Dict[str, Any]):
    """AssumeRole + 1 página de ``ListAccounts``: prova credencial E permissão."""
    from ..output.base import TestResult

    if not aws_auth.is_role_arn(cfg.get("client_id")):
        return TestResult.failed(
            "O modo AWS Organization exige o ARN de um papel (arn:aws:iam::<conta>:role/<nome>)."
        )
    t0 = time.perf_counter()
    try:
        auth = await asyncio.to_thread(aws_auth.auth_for_probe, cfg)
        kwargs = await aws_auth.client_kwargs(auth, "us-east-1")
    except aws_auth.AwsAuthError as exc:
        return TestResult.failed(str(exc))
    try:
        import aioboto3  # noqa: PLC0415

        async with aioboto3.Session().client("organizations", **kwargs) as org:
            resp = await org.list_accounts(MaxResults=1)
        ms = (time.perf_counter() - t0) * 1000.0
        n = len(resp.get("Accounts") or [])
        more = " (e mais)" if resp.get("NextToken") else ""
        return TestResult.passed(
            f"Conexão OK (papel assumido) — a Organization respondeu com {n} conta(s){more}.",
            latency_ms=ms,
        )
    except Exception as exc:  # noqa: BLE001
        msg = aws_auth.describe_error(exc, operation="ListAccounts")
        if "AWSOrganizationsNotInUse" in msg or "AccessDenied" in msg:
            msg += (" O papel precisa estar na conta de gerenciamento ou numa conta "
                    "administradora delegada, com organizations:ListAccounts.")
        return TestResult.failed(f"Falha ao listar as contas: {msg}")


def _register() -> None:
    from ..registry import AuthField, PlatformRegistration, register_platform

    role_field = AuthField(
        key="client_id", label="ARN do papel (conta de gerenciamento)", type="string",
        required=True,
        help_text="Papel numa conta que pode chamar organizations:ListAccounts — a de "
                  "gerenciamento ou uma administradora delegada.",
    )
    caps = frozenset({"catalog", "auth:test", "discover:children"})
    common = dict(
        icon_id="aws",
        category="Cloud-audit",
        auth_setup="aws_assume_role",
        test_fn=_probe,
        required_secrets=(),
        capabilities=caps,
        variant="organization",
        provider_factory=_provider,
        docs_url="https://docs.aws.amazon.com/organizations/latest/APIReference/API_ListAccounts.html",
    )
    register_platform(PlatformRegistration(
        platform="aws_cloudtrail_org",
        display_name="AWS CloudTrail — AWS Organization",
        description=("Trail de Organization: uma integração de CloudTrail por conta da "
                     "Organization, lendo o bucket central."),
        order=55,
        auth_fields=(
            AuthField(
                key="client_id", label="ARN do papel (com acesso ao bucket da trail)",
                type="string", required=True,
                help_text="Papel que lista as contas (organizations:ListAccounts) E lê o "
                          "bucket da trail de Organization — em geral a conta de log "
                          "archive registrada como administradora delegada.",
            ),
            AuthField(
                key="base_url", label="Bucket da trail de Organization", type="string",
                required=True,
                help_text="bucket ou bucket/prefixo. Cada filha lê AWSLogs/o-…/<conta>/.",
            ),
        ),
        **common,
    ))
    register_platform(PlatformRegistration(
        platform="aws_guardduty_org",
        display_name="Amazon GuardDuty — AWS Organization",
        description=("Uma integração de GuardDuty por conta da Organization, cada uma "
                     "assumindo o papel de mesmo nome na conta membro."),
        order=56,
        auth_fields=(role_field,),
        **common,
    ))
    register_platform(PlatformRegistration(
        platform="aws_securityhub_org",
        display_name="AWS Security Hub — AWS Organization",
        description=("Uma integração de Security Hub por conta da Organization, cada uma "
                     "assumindo o papel de mesmo nome na conta membro."),
        order=57,
        auth_fields=(role_field,),
        **common,
    ))


_register()
