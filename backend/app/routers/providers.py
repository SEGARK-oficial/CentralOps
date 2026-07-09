"""Providers router — metadata pública das plataformas suportadas.

GET /api/providers/platforms expõe o catálogo de plataformas para que o
frontend possa renderizar o formulário "Adicionar integração" de forma
data-driven, sem switch(platform) no código.

100% plugin-driven: o catálogo (display_name, category, description, icon, docs,
auth_fields) vem do registro self-describing de cada vendor
(``collectors.registry.PlatformRegistration``), NÃO de dicts hardcoded. Adicionar
um vendor = registrar a PlatformRegistration no módulo dele; este router não muda.

Não expõe valores de credenciais (AuthFieldRead.type="secret" nunca carrega
valor padrão). A autenticação exigida replica o padrão de /api/collectors/vendors
(require_authenticated_user — sem restrição de role).
"""
from __future__ import annotations

import logging
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..core import auth as app_auth
from ..core.errors import ApiError
from ..db import models
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
                spec_kinds=list(qc.spec_kinds),
                supported_by=sorted(entry["platforms"]),
            )
        )
    return out


@router.post("/{platform}/test-connection", response_model=ProviderTestResponse)
async def test_provider_connection(
    platform: str,
    body: ProviderTestRequest,
    _: models.AppUser = Depends(app_auth.require_authenticated_user),
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
    result = await plat.test_fn(body.config or {})
    return ProviderTestResponse(
        ok=bool(getattr(result, "ok", False)),
        detail=str(getattr(result, "detail", "")),
        latency_ms=getattr(result, "latency_ms", None),
    )
