"""Providers router — metadata pública das plataformas suportadas.

GET /api/providers/platforms expõe o catálogo de plataformas para que o
frontend possa renderizar o formulário "Adicionar integração" de forma
data-driven, sem switch(platform) no código.

100% plugin-driven: o catálogo (display_name, category, description, icon, docs,
auth_fields) vem do registro self-describing de cada vendor
(``collectors.registry.PlatformRegistration``), NÃO de dicts hardcoded. Adicionar
um vendor = registrar a PlatformRegistration no módulo dele; este router não muda.
O mesmo vale para os filtros de COLETA que cada stream declara
(``CollectorRegistration.filters``): eles saem em ``streams[].filters`` e a tela
os renderiza sozinha — um vendor que passe a filtrar na origem não custa uma
linha de backend nem de frontend.

Não expõe valores de credenciais (AuthFieldRead.type="secret" nunca carrega
valor padrão). A autenticação exigida replica o padrão de /api/collectors/vendors
(require_authenticated_user — sem restrição de role).
"""
from __future__ import annotations

import logging
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..api import schemas
from ..core import auth as app_auth
from ..core.errors import ApiError
from ..db import database, models
from ..collectors import capabilities
from ..collectors import registry as collector_registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/providers", tags=["providers"])


# ── Schema ─────────────────────────────────────────────────────────────────────

class AuthFieldRead(BaseModel):
    key: str
    label: str
    type: Literal["string", "secret", "url", "bool", "select"]
    required: bool = False
    help_text: Optional[str] = None
    options: Optional[List[str]] = None


class StreamRead(BaseModel):
    stream: str
    schedule_seconds: int
    #: Filtros de coleta que ESTE stream sabe empurrar para o fornecedor. Lista
    #: vazia ⇒ o stream não filtra na origem e a UI não mostra a seção. Vendor novo
    #: que declare filtros aparece na tela sem tocar neste router nem no frontend.
    filters: List[schemas.CollectionFilterFieldRead] = Field(default_factory=list)


class ProviderPlatformRead(BaseModel):
    platform: str
    display_name: str
    category: str = "Outros"
    description: str = ""
    icon_id: Optional[str] = None
    docs_url: Optional[str] = None
    auth_fields: List[AuthFieldRead]
    streams: List[StreamRead]
    # True ⇒ a plataforma suporta "Testar conexão" pré-save (tem test_fn).
    supports_test: bool = False
    # "pull" (poll de API, default) | "push" (fonte empurra p/ /api/ingest). A UI
    # renderiza token de ingestão + endpoint + snippet de edge-collector quando "push".
    transport: str = "pull"
    # Painel de onboarding de credencial que a UI mostra junto dos campos
    # (ex.: "aws_assume_role"). Vazio ⇒ só os campos.
    auth_setup: str = ""


class QueryCapabilityRead(BaseModel):
    """Metadado de catálogo de um dialeto de query.

    Agregado POR dialeto: quais plataformas o oferecem (``supported_by``) + os
    limites declarados (``max_window_seconds``/``rate_limit``) que o QueryService
    enforça. Consumido pela UI para descobrir "quem suporta query e como"."""

    dialect: str
    capability: str  # "query:opensearch_dsl"
    modes: List[str]
    supports_async: bool
    max_window_seconds: Optional[int] = None
    rate_limit: Optional[str] = None
    required_secrets: List[str]
    ocsf_mapping_version: str
    spec_kinds: List[str]
    supported_by: List[str]  # plataformas que oferecem este dialeto


class ProviderTestRequest(BaseModel):
    """Credenciais CRUAS digitadas na tela (ainda não salvas) para o probe."""

    config: dict


class AwsRoleSetupRead(BaseModel):
    """O que o cliente precisa para criar o papel cross-account na conta dele."""

    organization_id: int
    #: Gerado pelo servidor, único por organização e imutável.
    external_id: str
    #: ARN que a trust policy deve confiar. ``None`` quando o servidor não tem
    #: identidade AWS própria — ``principal_error`` diz o porquê.
    trust_principal_arn: Optional[str] = None
    principal_error: Optional[str] = None
    #: Template CloudFormation (JSON) com External ID e principal já preenchidos.
    template: dict
    template_filename: str = "centralops-readonly-role.json"


class ProviderTestResponse(BaseModel):
    ok: bool
    detail: str = ""
    latency_ms: Optional[float] = None


def _load_streams(platform: str) -> List[StreamRead]:
    """Deriva streams do collector registry para a plataforma."""
    try:
        return [
            StreamRead(
                stream=reg.stream,
                schedule_seconds=int(reg.schedule.total_seconds()),
                filters=[
                    schemas.CollectionFilterFieldRead.from_field(f) for f in reg.filters
                ],
            )
            for reg in collector_registry.iter_for_platform(platform)
        ]
    except Exception as exc:
        logger.warning("providers: falha ao carregar streams para platform=%r: %s", platform, exc)
        return []


# ── Endpoint ───────────────────────────────────────────────────────────────────

@router.get("/platforms", response_model=List[ProviderPlatformRead])
def list_provider_platforms(
    _: models.AppUser = Depends(app_auth.require_authenticated_user),
) -> List[ProviderPlatformRead]:
    """Lista as plataformas suportadas (catálogo) com auth fields e streams.

    100% PLUGIN-DRIVEN: o catálogo vem do registro self-describing de cada vendor
    (``collectors.registry.all_platforms()`` / ``PlatformRegistration``) — não há
    mais dicts hardcoded aqui nem no frontend. Adicionar um vendor = registrar a
    ``PlatformRegistration`` no módulo dele; este endpoint NÃO muda.

    Metadados públicos (sem credenciais). ``auth_fields[].type="secret"`` é só
    metadata de tipo (o frontend renderiza input password, sem valor).
    """
    out: List[ProviderPlatformRead] = []
    for plat in collector_registry.all_platforms():
        out.append(
            ProviderPlatformRead(
                platform=plat.platform,
                display_name=plat.display_name,
                category=plat.category,
                description=plat.description,
                icon_id=plat.icon_id,
                docs_url=plat.docs_url,
                auth_fields=[
                    AuthFieldRead(
                        key=f.key,
                        label=f.label,
                        type=f.type,  # type: ignore[arg-type]
                        required=f.required,
                        help_text=f.help_text,
                        options=list(f.options) if f.options else None,
                    )
                    for f in plat.auth_fields
                ],
                streams=_load_streams(plat.platform),
                supports_test=plat.test_fn is not None,
                transport=getattr(plat, "transport", "pull"),
                auth_setup=getattr(plat, "auth_setup", ""),
            )
        )
    return out


@router.get("/query-capabilities", response_model=List[QueryCapabilityRead])
def list_query_capabilities(
    _: models.AppUser = Depends(app_auth.require_authenticated_user),
) -> List[QueryCapabilityRead]:
    """Catálogo de dialetos de query suportados.

    100% PLUGIN-DRIVEN: lê ``PlatformRegistration.query_capabilities`` de cada vendor
    (mesmo padrão de ``GET /collectors/destinations/destination-types``). Plataforma
    sem query não aparece — a UI esconde a aba de query para ela. Agrega por dialeto:
    se N vendors falam o mesmo dialeto, vira uma linha com ``supported_by`` listando-os.

    NB: este é o metadado de CATÁLOGO (estático). O gate real por integração é
    server-side (``integration_query_capability`` + RBAC ``QUERY_RUN`` + escopo de org)."""
    by_dialect: dict = {}
    for plat in collector_registry.all_platforms():
        for qc in plat.query_capabilities or ():
            entry = by_dialect.setdefault(qc.dialect, {"qc": qc, "platforms": []})
            entry["platforms"].append(plat.platform)

    out: List[QueryCapabilityRead] = []
    for dialect, entry in sorted(by_dialect.items()):
        qc = entry["qc"]
        out.append(
            QueryCapabilityRead(
                dialect=qc.dialect,
                capability=qc.capability_key(),
                modes=list(qc.modes),
                supports_async=qc.supports_async,
                max_window_seconds=(
                    int(qc.max_window.total_seconds()) if qc.max_window else None
                ),
                rate_limit=qc.rate_limit,
                required_secrets=list(qc.required_secrets),
                ocsf_mapping_version=qc.ocsf_mapping_version,
                spec_kinds=capabilities.available_spec_kinds(qc.spec_kinds),
                supported_by=sorted(entry["platforms"]),
            )
        )
    return out


@router.get("/aws/role-setup", response_model=AwsRoleSetupRead)
async def aws_role_setup(
    organization_id: int = Query(..., ge=1),
    db: Session = Depends(database.get_session),
    current_user: models.AppUser = Depends(
        app_auth.require_permission(app_auth.Permission.INTEGRATION_WRITE)
    ),
) -> AwsRoleSetupRead:
    """External ID da organização + principal do CentralOps + template do papel.

    Cria o External ID na primeira chamada (e nunca mais o troca). Escopo de
    tenant ANTES de tudo: o External ID é o que separa um cliente do outro, então
    só quem administra integrações daquela org pode vê-lo.
    """
    import asyncio

    from ..collectors.auth import aws as aws_auth
    from ..core import tenant

    tenant.require_subtree_access(current_user, organization_id, db)
    try:
        external_id = await asyncio.to_thread(
            aws_auth.get_or_create_external_id, db, organization_id
        )
    except aws_auth.AwsAuthError:
        raise ApiError(
            "organization.not_found",
            404,
            messages={
                "pt": "Organização não encontrada.",
                "en": "Organization not found.",
                "es": "Organización no encontrada.",
            },
        ) from None
    principal, principal_error = await aws_auth.trust_principal_arn()
    return AwsRoleSetupRead(
        organization_id=organization_id,
        external_id=external_id,
        trust_principal_arn=principal,
        principal_error=principal_error,
        template=aws_auth.cloudformation_template(external_id, principal),
    )


@router.post("/{platform}/test-connection", response_model=ProviderTestResponse)
async def test_provider_connection(
    platform: str,
    body: ProviderTestRequest,
    current_user: models.AppUser = Depends(app_auth.require_authenticated_user),
) -> ProviderTestResponse:
    """Testa as credenciais CRUAS (pré-save) de uma plataforma — stateless, NÃO
    persiste nada. Plugin-driven: roda o ``test_fn`` self-describing do vendor
    (probe de auth). 404 se a plataforma não existe; 422 se não suporta teste.

    O probe nunca vaza o secret na mensagem. Same-shape do teste de destinos."""
    plat = collector_registry.get_platform(platform)
    if plat is None:
        raise ApiError(
            "provider.platform_unknown",
            404,
            messages={
                "pt": "plataforma {platform!r} desconhecida",
                "en": "unknown platform {platform!r}",
                "es": "plataforma {platform!r} desconocida",
            },
            params={"platform": platform},
        )
    if plat.test_fn is None:
        raise ApiError(
            "provider.test_connection_unsupported",
            422,
            messages={
                "pt": "plataforma {platform!r} não suporta teste de conexão pré-save",
                "en": "platform {platform!r} does not support pre-save connection testing",
                "es": "la plataforma {platform!r} no admite la prueba de conexión antes de guardar",
            },
            params={"platform": platform},
        )
    config = dict(body.config or {})
    # ``organization_id`` no config: o probe vai agir EM NOME dessa org (ex.: o
    # AssumeRole da AWS usa o External ID dela). Confere o escopo aqui, antes do
    # probe — senão um usuário de uma org testaria o papel de outra com o External
    # ID alheio. Valor que não é inteiro positivo é descartado, nunca repassado.
    raw_org = config.pop("organization_id", None)
    if raw_org not in (None, ""):
        try:
            org_id = int(raw_org)
        except (TypeError, ValueError):
            org_id = 0
        if org_id >= 1 and not isinstance(raw_org, bool):
            from ..core import tenant

            tenant.require_subtree_access(current_user, org_id)
            config["organization_id"] = org_id
    result = await plat.test_fn(config)
    return ProviderTestResponse(
        ok=bool(getattr(result, "ok", False)),
        detail=str(getattr(result, "detail", "")),
        latency_ms=getattr(result, "latency_ms", None),
    )
