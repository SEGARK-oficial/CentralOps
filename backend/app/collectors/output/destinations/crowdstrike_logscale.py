"""Kind ``crowdstrike_logscale`` — destino CrowdStrike Falcon LogScale (ex-Humio).

LogScale ingere via endpoint HEC-compatível com ``Authorization: Bearer
<ingest-token>`` e corpo NDJSON de ``{"event": <evento>}``. O token de ingestão é
**independente** (não OAuth) e tipicamente longevo — o destino é autônomo, não
depende de uma Integration CrowdStrike. A URL completa de ingestão é fornecida
pelo console do LogScale (varia por região/cloud/self-hosted), então é um campo
explícito da config.

O token fica em ``secret_ref`` (cofre) — nunca na config.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from ..logscale_sender import LogScaleHecClient
from .registry import DestinationConfig, DestinationRegistration, register

logger = logging.getLogger(__name__)

KIND = "crowdstrike_logscale"


class CrowdStrikeLogScaleConfig(BaseModel):
    """Schema de config do destino CrowdStrike Falcon LogScale."""

    endpoint: str = Field(
        description="URL completa de ingestão HEC do LogScale "
        "(ex: https://cloud.us.humio.com/api/v1/ingest/hec)",
    )
    sourcetype: Optional[str] = Field(default=None, description="sourcetype HEC (opcional)")
    source: Optional[str] = Field(default=None, description="Campo source / atribuição de host (opcional)")
    extra_headers: Dict[str, str] = Field(
        default_factory=dict, description="Headers HTTP adicionais (opcional)"
    )
    verify_tls: bool = Field(default=True, description="Verificar certificado TLS")
    ca_bundle: Optional[str] = Field(
        default=None, description="Path do CA bundle PEM customizado (apenas com verify_tls=True)"
    )


def _factory(config: DestinationConfig, secrets: Optional[Any] = None) -> LogScaleHecClient:
    cfg = CrowdStrikeLogScaleConfig(**dict(config.config or {}))

    token: Optional[str] = None
    if secrets is not None and config.secret_ref:
        try:
            token = secrets.decrypt(config.secret_ref)
        except Exception as exc:
            logger.warning(
                "crowdstrike_logscale: falha ao decifrar credencial (%s) — token=None (dormant)",
                type(exc).__name__,
            )

    return LogScaleHecClient(
        endpoint=cfg.endpoint,
        token=token,
        kind=KIND,
        sourcetype=cfg.sourcetype,
        source=cfg.source,
        verify_tls=cfg.verify_tls,
        ca_bundle=cfg.ca_bundle,
        extra_headers=cfg.extra_headers,
    )


register(
    DestinationRegistration(
        kind=KIND,
        factory=_factory,
        config_schema=CrowdStrikeLogScaleConfig,
        default_queue="dispatch.crowdstrike_logscale",
        # HEC Bearer sem dedup nativo: at_least_once. NDJSON paraleliza bem (E5).
        capabilities=frozenset({"tls", "batch", "test", "at_least_once"}),
        required_secrets=("ingest_token",),
        label="CrowdStrike Falcon LogScale",
        delivery_defaults={"concurrency": 8},
        category="SIEM",
        description="CrowdStrike Falcon LogScale (ex-Humio) via HEC — busca de logs em escala de longo prazo.",
        icon_id="crowdstrike",
        docs_url="https://library.humio.com/integrations/ingesting-hec.html",
        tier="stable",
        order=25,
    )
)
