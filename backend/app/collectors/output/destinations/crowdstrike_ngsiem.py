"""Kind ``crowdstrike_ngsiem`` — destino CrowdStrike Falcon Next-Gen SIEM.

O Falcon Next-Gen SIEM é construído sobre o LogScale e aceita dados de terceiros
por um conector **HEC** (``Authorization: Bearer <ingest-token>`` + NDJSON de
``{"event": <evento>}``). A URL do conector HEC e o token são gerados no console
do Falcon NG-SIEM (Data connectors → HEC), então o destino é autônomo (token de
ingestão independente, sem OAuth) — compartilha o cliente da família LogScale com
``crowdstrike_logscale``.

O token fica em ``secret_ref`` (cofre) — nunca na config.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from ..logscale_sender import LogScaleHecClient
from .registry import DestinationConfig, DestinationRegistration, register

logger = logging.getLogger(__name__)

KIND = "crowdstrike_ngsiem"


class CrowdStrikeNgSiemConfig(BaseModel):
    """Schema de config do destino CrowdStrike Falcon Next-Gen SIEM."""

    endpoint: str = Field(
        description="URL do conector HEC do Falcon NG-SIEM "
        "(gerada no console: Data connectors → HEC)",
    )
    sourcetype: str = Field(
        default="centralops", description="sourcetype HEC (default: centralops)"
    )
    source: Optional[str] = Field(default=None, description="Campo source do HEC (opcional)")
    extra_headers: Dict[str, str] = Field(
        default_factory=dict, description="Headers HTTP adicionais (opcional)"
    )
    verify_tls: bool = Field(default=True, description="Verificar certificado TLS")
    ca_bundle: Optional[str] = Field(
        default=None, description="Path do CA bundle PEM customizado (apenas com verify_tls=True)"
    )


def _factory(config: DestinationConfig, secrets: Optional[Any] = None) -> LogScaleHecClient:
    cfg = CrowdStrikeNgSiemConfig(**dict(config.config or {}))

    token: Optional[str] = None
    if secrets is not None and config.secret_ref:
        try:
            token = secrets.decrypt(config.secret_ref)
        except Exception as exc:
            logger.warning(
                "crowdstrike_ngsiem: falha ao decifrar credencial (%s) — token=None (dormant)",
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
        config_schema=CrowdStrikeNgSiemConfig,
        default_queue="dispatch.crowdstrike_ngsiem",
        capabilities=frozenset({"tls", "batch", "test", "at_least_once"}),
        required_secrets=("ingest_token",),
        label="CrowdStrike Falcon Next-Gen SIEM",
        delivery_defaults={"concurrency": 8},
        category="SIEM",
        description="CrowdStrike Falcon Next-Gen SIEM via conector HEC — detecção e correlação no Falcon.",
        icon_id="crowdstrike",
        docs_url="https://www.crowdstrike.com/platform/next-gen-siem/",
        tier="stable",
        order=24,
    )
)
