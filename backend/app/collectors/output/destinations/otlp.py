"""Kind ``otlp`` — destino OTLP/HTTP.

Exporta eventos OCSF do CentralOps via OTLP/HTTP JSON para qualquer
coletor/backend compatível com OpenTelemetry (Grafana Alloy, OTel Collector,
Honeycomb, SigNoz, Datadog OTLP endpoint, etc.).

Protocolo: ``POST {endpoint}`` (tipicamente ``https://host:4318/v1/logs``)
com ``Content-Type: application/json`` e corpo ``ExportLogsServiceRequest``.

**Sem dependências opentelemetry-*:** o JSON OTLP é construído manualmente
(``otlp_sender.py``) — o core do CentralOps permanece livre de SDKs pesados.

**Ativo quando há um destino ``kind=otlp`` configurado** (multi-destino é
GA).  Sem headers de auth (o destino
pode ser aberto ou protegido por mTLS externo), o destino funciona normalmente
— a auth via ``secret_ref`` é opcional.

Capabilities declaradas: ``tls``, ``batch``, ``test``, ``at_least_once``.
NÃO declaramos ``erasure`` (OTLP não garante deleção de LogRecords) nem
``idempotent`` (sem dedup nativo no sender).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from ..otlp_sender import OtlpHttpClient
from .registry import DestinationConfig, DestinationRegistration, register

logger = logging.getLogger(__name__)

KIND = "otlp"


class OtlpConfig(BaseModel):
    """Schema de config do destino OTLP/HTTP.

    Campos expostos no catálogo da UI (``GET /collectors/destination-types``).
    Headers de autenticação opcionais ficam em ``headers``; credenciais
    sensíveis (Bearer token, API key) devem estar em ``secret_ref`` — o valor
    decifrado é injetado via ``_factory`` no header correto.

    ``resource_attrs`` são os atributos de recurso OTel (service.name,
    host.name, deployment.environment, etc.) incluídos em todos os batches.
    """

    endpoint: str = Field(
        description=(
            "URL completa do endpoint OTLP/HTTP /v1/logs "
            "(ex: https://otel.exemplo.com:4318/v1/logs)"
        )
    )
    headers: Dict[str, str] = Field(
        default_factory=dict,
        description="Headers HTTP adicionais (ex: {'X-Tenant-ID': 'acme'})",
    )
    resource_attrs: Dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Atributos de recurso OTel (ex: {'service.name': 'centralops', "
            "'deployment.environment': 'prod'})"
        ),
    )
    verify_tls: bool = Field(default=True, description="Verificar certificado TLS")
    ca_bundle: Optional[str] = Field(
        default=None,
        description="Path do CA bundle PEM customizado (apenas com verify_tls=True)",
    )


def _factory(config: DestinationConfig, secrets: Optional[Any] = None) -> OtlpHttpClient:
    """Constrói um ``OtlpHttpClient`` a partir da config resolvida.

    Quando ``secret_ref`` está presente, o segredo decifrado é tratado como
    Bearer token e injetado em ``Authorization: Bearer <token>``. O header
    pode ser sobrescrito pelos ``headers`` da config se necessário.
    Sem ``secret_ref`` (endpoint aberto ou mTLS) → sem Authorization header.
    """
    cfg = OtlpConfig(**dict(config.config or {}))

    merged_headers = dict(cfg.headers)

    if secrets is not None and config.secret_ref:
        try:
            token = secrets.decrypt(config.secret_ref)
            if token and "Authorization" not in merged_headers:
                merged_headers["Authorization"] = f"Bearer {token}"
        except Exception as exc:
            # Não logar secret_ref nem o objeto exc (path da master key/cofre).
            logger.warning(
                "otlp: falha ao decifrar credencial (%s) — sem auth header (dormant)",
                type(exc).__name__,
            )

    return OtlpHttpClient(
        endpoint=cfg.endpoint,
        headers=merged_headers,
        resource_attrs=cfg.resource_attrs,
        verify_tls=cfg.verify_tls,
        ca_bundle=cfg.ca_bundle,
    )


register(
    DestinationRegistration(
        kind=KIND,
        factory=_factory,
        config_schema=OtlpConfig,
        default_queue="dispatch.otlp",
        # "at_least_once": OTLP/HTTP não possui dedup nativo no sender.
        # O event_id é incluído como atributo centralops.event_id para
        # correlação downstream. Sem "idempotent" nem "erasure".
        capabilities=frozenset({"tls", "batch", "test", "at_least_once"}),
        required_secrets=(),  # auth é opcional (endpoint pode ser aberto/mTLS)
        label="OTLP/HTTP (OpenTelemetry)",
        # OTLP/HTTP é paralelizável — concorrência similar ao Splunk HEC.
        delivery_defaults={"concurrency": 8},
        # Campos de catálogo self-describing (galeria de destinos).
        category="Observabilidade",
        icon_id="opentelemetry",
        tier="stable",
        order=60,
        description="OpenTelemetry Protocol (OTLP) — logs via gRPC ou HTTP para qualquer backend OTel.",
    )
)
