"""Credenciais AWS dos coletores: papel cross-account (AssumeRole) ou chave fixa.

**Papel cross-account (recomendado).** O cliente cria na conta DELE um papel
só-leitura cuja trust policy confia na identidade AWS do CentralOps e exige um
``sts:ExternalId``. O coletor chama ``sts:AssumeRole`` e trabalha com credenciais
temporárias (1h), renovadas sozinhas. Nada de chave de longa duração guardada.

**External ID — por que é o servidor que gera.** O External ID é o que impede o
*confused deputy*: sem ele, quem souber o ARN do papel de outro cliente poderia
cadastrá-lo numa integração própria e o CentralOps assumiria o papel alheio. A
proteção só vale se o valor for (1) único por cliente, (2) gerado pelo
CentralOps e (3) imutável. Por isso ele vive em ``Organization.aws_external_id``,
é criado aqui, uma vez, e NUNCA é lido do corpo de uma requisição.
Referências: https://docs.aws.amazon.com/IAM/latest/UserGuide/confused-deputy.html
e o pilar de segurança do Well-Architected (SEC03-BP09).

**Chave fixa (legado).** Access Key ID + Secret Access Key, como antes. Continua
aceita para não quebrar integração existente.

**Qual modo?** Inferido do próprio campo de identidade (``client_id``): um ARN de
papel (``arn:aws:iam::<conta>:role/...``) liga o modo papel; qualquer outra coisa
é Access Key ID. Assim o formulário não precisa de seletor de modo, a edição
pré-preenche a coluna de sempre e nenhuma coluna nova entra no ``Integration``.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

MODE_ROLE = "role"
MODE_ACCESS_KEY = "access_key"

#: ARN de papel IAM em qualquer partição (aws, aws-cn, aws-us-gov). O grupo 1 é a
#: conta. O path do papel é opcional (``role/caminho/Nome``).
_ROLE_ARN_RE = re.compile(r"^arn:aws(?:-[a-z]+)*:iam::(\d{12}):role/[\w+=,.@/-]{1,512}$")

#: Duração pedida ao AssumeRole. 1h é o máximo que vale para qualquer papel sem
#: mexer no ``MaxSessionDuration`` (e é o que o template cria).
_SESSION_SECONDS = 3600
#: Renova a credencial temporária quando faltar menos que isto para expirar. Um
#: ciclo de coleta pode durar até o soft-timeout (~12 min); a folga cobre isso.
_REFRESH_MARGIN_S = 15 * 60

# (role_arn, external_id, session_name) → (credenciais, expira_em_epoch)
_CRED_CACHE: Dict[Tuple[str, str, str], Tuple[Dict[str, str], float]] = {}
# Principal do CentralOps descoberto via STS. Só o SUCESSO é cacheado.
_PRINCIPAL_CACHE: Dict[str, str] = {}


class AwsAuthError(RuntimeError):
    """Falha de credencial AWS com mensagem segura para a UI e para a Saúde.

    A mensagem nunca carrega segredo (secret, session token, External ID)."""


@dataclass(frozen=True)
class AwsAuth:
    mode: str
    role_arn: str = ""
    external_id: str = field(default="", repr=False)
    session_name: str = ""
    access_key_id: str = ""
    secret_access_key: str = field(default="", repr=False)

    @classmethod
    def access_key(cls, access_key_id: str, secret_access_key: str) -> "AwsAuth":
        return cls(
            mode=MODE_ACCESS_KEY,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
        )

    @classmethod
    def role(cls, role_arn: str, external_id: str, session_name: str) -> "AwsAuth":
        return cls(
            mode=MODE_ROLE,
            role_arn=role_arn,
            external_id=external_id,
            session_name=session_name,
        )


def is_role_arn(value: Optional[str]) -> bool:
    return bool(value) and _ROLE_ARN_RE.match(value.strip()) is not None


def role_account_id(role_arn: str) -> Optional[str]:
    m = _ROLE_ARN_RE.match((role_arn or "").strip())
    return m.group(1) if m else None


def session_name_for(organization_id: int, integration_id: Optional[int] = None) -> str:
    """``RoleSessionName`` legível no CloudTrail DO CLIENTE: mostra qual org e
    qual integração do CentralOps fez cada chamada. Limite AWS: 2–64 chars."""
    suffix = f"i{integration_id}" if integration_id is not None else "probe"
    return f"centralops-o{organization_id}-{suffix}"[:64]


# ── External ID (por organização, gerado pelo servidor, imutável) ──────────


def get_or_create_external_id(db, organization_id: int) -> str:
    """Devolve o External ID da org, criando-o na primeira chamada.

    Corrida entre duas requisições simultâneas: o UPDATE só grava onde a coluna
    ainda é NULL, então a segunda não sobrescreve a primeira; as duas releem e
    devolvem o MESMO valor. Sobrescrever seria o pior caso: o cliente criaria o
    papel com um ID que já não é o nosso.
    """
    from sqlalchemy import update

    from ...db import models

    org = db.get(models.Organization, organization_id)
    if org is None:
        raise AwsAuthError("Organização não encontrada.")
    if org.aws_external_id:
        return org.aws_external_id
    candidate = f"centralops-{uuid.uuid4()}"
    db.execute(
        update(models.Organization)
        .where(models.Organization.id == organization_id)
        .where(models.Organization.aws_external_id.is_(None))
        .values(aws_external_id=candidate)
    )
    db.commit()
    db.expire(org)
    value = db.get(models.Organization, organization_id).aws_external_id
    if not value:  # pragma: no cover — só se a linha sumiu entre as duas leituras
        raise AwsAuthError("Não foi possível gerar o External ID da organização.")
    return value


def auth_for_integration(db, integ) -> AwsAuth:
    """Monta o ``AwsAuth`` de uma integração SALVA (lado do coletor)."""
    from ...services import integration_secrets

    identity = (integ.client_id or "").strip()
    if is_role_arn(identity):
        return AwsAuth.role(
            identity,
            get_or_create_external_id(db, integ.organization_id),
            session_name_for(integ.organization_id, integ.id),
        )
    secret = integration_secrets.read_secret(integ, "secret_access_key") or ""
    if not identity or not secret:
        raise AwsAuthError(
            "Informe o ARN do papel (recomendado) ou Access Key ID + Secret Access Key."
        )
    return AwsAuth.access_key(identity, secret)


def _external_id_from_db(organization_id: int) -> str:
    from ...db import database

    with database.SessionLocal() as db:
        return get_or_create_external_id(db, organization_id)


def auth_for_probe(
    cfg: Dict[str, Any],
    external_id_for: Callable[[int], str] = _external_id_from_db,
) -> AwsAuth:
    """Monta o ``AwsAuth`` do teste PRÉ-SAVE (credenciais digitadas, nada salvo).

    Modo papel exige ``organization_id``: o External ID é o DA ORG, lido do banco.
    O router do teste confere que o usuário enxerga essa org antes de chamar o
    probe — sem isso qualquer usuário poderia mandar o CentralOps assumir o papel
    de outro cliente usando o External ID de outra org. Um ``external_id`` vindo
    no ``cfg`` é IGNORADO de propósito.
    """
    identity = (cfg.get("client_id") or "").strip()
    if is_role_arn(identity):
        org_id = cfg.get("organization_id")
        if not isinstance(org_id, int) or isinstance(org_id, bool):
            raise AwsAuthError("Selecione a organização antes de testar o papel.")
        return AwsAuth.role(identity, external_id_for(org_id), session_name_for(org_id))
    secret = (cfg.get("secret_access_key") or "").strip()
    if not identity or not secret:
        raise AwsAuthError(
            "Informe o ARN do papel (recomendado) ou Access Key ID + Secret Access Key."
        )
    return AwsAuth.access_key(identity, secret)


# ── Credenciais para o client aioboto3 ────────────────────────────────────


async def client_kwargs(auth: AwsAuth, region: str) -> Dict[str, str]:
    """kwargs de ``aioboto3.Session().client(...)`` para este ``auth``."""
    if auth.mode == MODE_ACCESS_KEY:
        return {
            "aws_access_key_id": auth.access_key_id,
            "aws_secret_access_key": auth.secret_access_key,
            "region_name": region,
        }
    creds = await assumed_credentials(auth, region)
    return {
        "aws_access_key_id": creds["AccessKeyId"],
        "aws_secret_access_key": creds["SecretAccessKey"],
        "aws_session_token": creds["SessionToken"],
        "region_name": region,
    }


async def assumed_credentials(auth: AwsAuth, region: str) -> Dict[str, str]:
    """Credenciais temporárias do papel, do cache enquanto estiverem válidas."""
    key = (auth.role_arn, auth.external_id, auth.session_name)
    hit = _CRED_CACHE.get(key)
    if hit is not None and hit[1] - time.time() > _REFRESH_MARGIN_S:
        return hit[0]
    try:
        resp = await _assume_role(auth, region)
    except Exception as exc:  # noqa: BLE001 — traduzido para mensagem segura
        raise AwsAuthError(describe_error(exc, operation="AssumeRole")) from None
    raw = resp["Credentials"]
    creds = {
        "AccessKeyId": raw["AccessKeyId"],
        "SecretAccessKey": raw["SecretAccessKey"],
        "SessionToken": raw["SessionToken"],
    }
    expiration = raw.get("Expiration")
    expires_at = (
        expiration.timestamp() if hasattr(expiration, "timestamp") else time.time() + _SESSION_SECONDS
    )
    _CRED_CACHE[key] = (creds, expires_at)
    return creds


async def _assume_role(auth: AwsAuth, region: str) -> Dict[str, Any]:
    """Seam mockável: a chamada STS com a identidade PRÓPRIA do CentralOps
    (cadeia padrão do SDK). Import tardio, igual aos coletores."""
    import aioboto3  # noqa: PLC0415

    async with aioboto3.Session().client("sts", region_name=region or None) as sts:
        return await sts.assume_role(
            RoleArn=auth.role_arn,
            RoleSessionName=auth.session_name,
            ExternalId=auth.external_id,
            DurationSeconds=_SESSION_SECONDS,
        )


def reset_caches() -> None:
    """Para testes."""
    _CRED_CACHE.clear()
    _PRINCIPAL_CACHE.clear()


# ── Mensagens de erro ─────────────────────────────────────────────────────

_TRUST_HINT = (
    "confira se a trust policy do papel confia no principal do CentralOps "
    "e exige o External ID desta organização"
)
_KMS_HINT = "a identidade não pode usar a chave KMS que cifra os logs (kms:Decrypt)"
_READ_HINT = "a identidade não tem a permissão de leitura necessária neste recurso"
_HINTS = {
    "InvalidClientTokenId": "Access Key ID inválido ou desativado",
    "SignatureDoesNotMatch": "Secret Access Key não confere",
    "ExpiredToken": "credencial expirada",
    "NoSuchBucket": "o bucket não existe ou está em outra região",
    "ResourceNotFoundException": "o recurso (ex.: log group) não existe nesta região",
}
_DENIED_CODES = {"AccessDenied", "AccessDeniedException", "KMS.AccessDeniedException"}


def describe_error(exc: BaseException, *, operation: str = "") -> str:
    """Erro AWS → texto curto e seguro (sem segredo). Usado na UI e no cursor."""
    if isinstance(exc, AwsAuthError):
        return str(exc)
    name = type(exc).__name__
    prefix = f"{operation}: " if operation else ""
    if name in ("NoCredentialsError", "PartialCredentialsError", "NoRegionError"):
        if operation == "AssumeRole":
            return (
                f"{prefix}o servidor do CentralOps não tem identidade AWS própria para "
                "assumir o papel (configure as credenciais do SDK no ambiente do servidor)."
            )
        return f"{prefix}credenciais AWS ausentes ({name})."
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        err = response.get("Error") or {}
        code = str(err.get("Code") or name)
        hint = _HINTS.get(code)
        if code in _DENIED_CODES:
            # O mesmo código cobre três causas com conserto diferente: a trust
            # policy (no AssumeRole), a chave KMS (a AWS diz "kms" na mensagem) e
            # a política de leitura do papel/usuário.
            if operation == "AssumeRole":
                hint = _TRUST_HINT
            elif "kms" in str(err.get("Message", "")).lower() or code.startswith("KMS."):
                hint = _KMS_HINT
            else:
                hint = _READ_HINT
        return f"{prefix}{code}" + (f" — {hint}." if hint else ".")
    return f"{prefix}{name}."


# ── Onboarding: principal do CentralOps + template CloudFormation ──────────


def _iam_arn_from_caller(arn: str) -> str:
    """``arn:aws:sts::123:assumed-role/Nome/sessao`` → ``arn:aws:iam::123:role/Nome``.

    A trust policy precisa do ARN do PAPEL, não da sessão. O path do papel não
    vem no ARN da sessão — por isso existe ``AWS_TRUST_PRINCIPAL_ARN``."""
    m = re.match(r"^arn:(aws[a-z-]*):sts::(\d{12}):assumed-role/([^/]+)/.+$", arn or "")
    if m:
        return f"arn:{m.group(1)}:iam::{m.group(2)}:role/{m.group(3)}"
    return arn


async def trust_principal_arn() -> Tuple[Optional[str], Optional[str]]:
    """(ARN que o cliente deve confiar, erro). Configuração explícita vence."""
    from ...core.config import settings

    configured = (getattr(settings, "AWS_TRUST_PRINCIPAL_ARN", None) or "").strip()
    if configured:
        return configured, None
    cached = _PRINCIPAL_CACHE.get("arn")
    if cached:
        return cached, None
    try:
        ident = await _get_caller_identity()
    except Exception as exc:  # noqa: BLE001
        return None, describe_error(exc, operation="GetCallerIdentity")
    arn = _iam_arn_from_caller(str(ident.get("Arn") or ""))
    if not arn:
        return None, "GetCallerIdentity não devolveu ARN."
    _PRINCIPAL_CACHE["arn"] = arn
    return arn, None


async def _get_caller_identity() -> Dict[str, Any]:
    import aioboto3  # noqa: PLC0415

    async with aioboto3.Session().client("sts") as sts:
        return await sts.get_caller_identity()


def cloudformation_template(external_id: str, principal_arn: Optional[str]) -> Dict[str, Any]:
    """Template do papel só-leitura que o cliente cria na conta dele.

    O External ID e o principal entram como DEFAULT dos parâmetros: o cliente
    aplica sem editar nada. Permissões mínimas para as fontes de hoje (CloudTrail
    e WAF no S3, CloudWatch Logs — onde também cai o WAF entregue em log group);
    buckets e chave KMS são opcionais e, vazios, não geram a política correspondente.
    """
    principal_param: Dict[str, Any] = {
        "Type": "String",
        "Description": "ARN da identidade AWS do CentralOps (quem pode assumir o papel).",
        "AllowedPattern": r"^arn:aws[a-z-]*:(iam|sts)::\d{12}:.+$",
    }
    if principal_arn:
        principal_param["Default"] = principal_arn
    sub = "Fn::Sub"
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "CentralOps - papel somente leitura para coleta de logs (AssumeRole com External ID).",
        "Parameters": {
            "TrustedPrincipalArn": principal_param,
            "ExternalId": {
                "Type": "String",
                "Default": external_id,
                "Description": "External ID gerado pelo CentralOps para a sua organização. Não altere.",
                "AllowedPattern": r"^[\w+=,.@:/-]{2,1224}$",
            },
            "RoleName": {"Type": "String", "Default": "CentralOpsReadOnly"},
            "CloudTrailBucketName": {
                "Type": "String",
                "Default": "",
                "Description": "Bucket onde o CloudTrail entrega os logs (vazio = sem CloudTrail via S3).",
            },
            "WafLogsBucketName": {
                "Type": "String",
                "Default": "",
                "Description": "Bucket aws-waf-logs-* onde o WAF entrega os logs (vazio = sem WAF via S3).",
            },
            "KmsKeyArn": {
                "Type": "String",
                "Default": "",
                "Description": "Chave KMS que cifra os logs do bucket, se houver (vazio = sem KMS).",
            },
        },
        "Conditions": {
            "HasBucket": {"Fn::Not": [{"Fn::Equals": [{"Ref": "CloudTrailBucketName"}, ""]}]},
            "HasWafBucket": {"Fn::Not": [{"Fn::Equals": [{"Ref": "WafLogsBucketName"}, ""]}]},
            "HasKms": {"Fn::Not": [{"Fn::Equals": [{"Ref": "KmsKeyArn"}, ""]}]},
        },
        "Resources": {
            "CentralOpsRole": {
                "Type": "AWS::IAM::Role",
                "Properties": {
                    "RoleName": {"Ref": "RoleName"},
                    "MaxSessionDuration": _SESSION_SECONDS,
                    "AssumeRolePolicyDocument": {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Principal": {"AWS": {"Ref": "TrustedPrincipalArn"}},
                                "Action": "sts:AssumeRole",
                                "Condition": {"StringEquals": {"sts:ExternalId": {"Ref": "ExternalId"}}},
                            }
                        ],
                    },
                    "Policies": [
                        {
                            "PolicyName": "CentralOpsCloudWatchLogsRead",
                            "PolicyDocument": {
                                "Version": "2012-10-17",
                                "Statement": [
                                    {
                                        "Effect": "Allow",
                                        "Action": [
                                            "logs:DescribeLogGroups",
                                            "logs:FilterLogEvents",
                                        ],
                                        "Resource": "*",
                                    }
                                ],
                            },
                        },
                        {
                            "Fn::If": [
                                "HasBucket",
                                {
                                    "PolicyName": "CentralOpsCloudTrailS3Read",
                                    "PolicyDocument": {
                                        "Version": "2012-10-17",
                                        "Statement": [
                                            {
                                                "Effect": "Allow",
                                                "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
                                                "Resource": {sub: "arn:${AWS::Partition}:s3:::${CloudTrailBucketName}"},
                                            },
                                            {
                                                "Effect": "Allow",
                                                "Action": "s3:GetObject",
                                                "Resource": {sub: "arn:${AWS::Partition}:s3:::${CloudTrailBucketName}/*"},
                                            },
                                        ],
                                    },
                                },
                                {"Ref": "AWS::NoValue"},
                            ]
                        },
                        {
                            "Fn::If": [
                                "HasWafBucket",
                                {
                                    "PolicyName": "CentralOpsWafLogsS3Read",
                                    "PolicyDocument": {
                                        "Version": "2012-10-17",
                                        "Statement": [
                                            {
                                                "Effect": "Allow",
                                                "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
                                                "Resource": {sub: "arn:${AWS::Partition}:s3:::${WafLogsBucketName}"},
                                            },
                                            {
                                                "Effect": "Allow",
                                                "Action": "s3:GetObject",
                                                "Resource": {sub: "arn:${AWS::Partition}:s3:::${WafLogsBucketName}/*"},
                                            },
                                        ],
                                    },
                                },
                                {"Ref": "AWS::NoValue"},
                            ]
                        },
                        {
                            "Fn::If": [
                                "HasKms",
                                {
                                    "PolicyName": "CentralOpsLogsKmsDecrypt",
                                    "PolicyDocument": {
                                        "Version": "2012-10-17",
                                        "Statement": [
                                            {
                                                "Effect": "Allow",
                                                "Action": "kms:Decrypt",
                                                "Resource": {"Ref": "KmsKeyArn"},
                                            }
                                        ],
                                    },
                                },
                                {"Ref": "AWS::NoValue"},
                            ]
                        },
                    ],
                },
            }
        },
        "Outputs": {
            "RoleArn": {
                "Description": "Cole este ARN no campo de identidade da integração no CentralOps.",
                "Value": {"Fn::GetAtt": ["CentralOpsRole", "Arn"]},
            }
        },
    }
