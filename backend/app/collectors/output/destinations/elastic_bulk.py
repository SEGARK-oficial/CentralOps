"""Kind ``elastic_bulk`` — destino Elasticsearch/OpenSearch ``_bulk``.

O destino "lago/SIEM" mais comum do mercado. A API ``_bulk`` é
compatível entre Elasticsearch e OpenSearch e devolve status POR ITEM — encaixe
nativo na falha-parcial. Credencial (API key Elastic, ou ``user:pass`` para
basic) fica em ``secret_ref`` (cofre), nunca na config.

**Ativo quando há um destino kind=elastic_bulk configurado** (multi-destino é
GA). Sem ``secret_ref`` (e auth != none),
``send_batch``/``test`` falham de forma descritiva — destino dormant sem credencial.
"""

from __future__ import annotations

import logging
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from ..elastic_bulk_sender import ElasticBulkClient
from .registry import DestinationConfig, DestinationRegistration, register

logger = logging.getLogger(__name__)

KIND = "elastic_bulk"


class ElasticBulkConfig(BaseModel):
    """Schema de config do destino Elastic/OpenSearch ``_bulk``.

    A credencial **não** está aqui: API key (Elastic) ou ``user:pass`` (basic)
    ficam em ``secret_ref``. ``ca_bundle`` é path no filesystem do collector.
    """

    url: str = Field(description="URL base do cluster (ex: https://es:9200)")
    index: str = Field(default="centralops", description="Índice/alias de destino")
    auth_scheme: Literal["api_key", "basic", "none"] = Field(
        default="api_key",
        description="api_key (Elastic) | basic (user:pass) | none (sem auth)",
    )
    verify_tls: bool = Field(default=True, description="Verificar certificado TLS")
    ca_bundle: Optional[str] = Field(
        default=None,
        description="Path do CA bundle PEM customizado (apenas com verify_tls=True)",
    )


def _factory(config: DestinationConfig, secrets: Optional[Any] = None) -> ElasticBulkClient:
    """Constrói um ``ElasticBulkClient`` a partir da config resolvida.

    A credencial é decifrada via ``secrets.decrypt(config.secret_ref)`` quando
    ambos presentes. Ausente (dormant) ou auth=none → ``secret=None``; send/test
    falham de forma descritiva sem levantar aqui (fail-closed controlado).
    """
    cfg = ElasticBulkConfig(**dict(config.config or {}))

    secret: Optional[str] = None
    if secrets is not None and config.secret_ref:
        try:
            secret = secrets.decrypt(config.secret_ref)
        except Exception as exc:
            # Não logar secret_ref nem o objeto exc: a exceção do decrypt pode
            # conter o path da master key/estrutura do cofre. Só o tipo.
            logger.warning(
                "elastic_bulk: falha ao decifrar credencial (%s) — secret=None (dormant)",
                type(exc).__name__,
            )

    return ElasticBulkClient(
        url=cfg.url,
        secret=secret,
        index=cfg.index,
        auth_scheme=cfg.auth_scheme,
        verify_tls=cfg.verify_tls,
        ca_bundle=cfg.ca_bundle,
    )


register(
    DestinationRegistration(
        kind=KIND,
        factory=_factory,
        config_schema=ElasticBulkConfig,
        default_queue="dispatch.elastic_bulk",
        # ``_bulk`` é idempotente via create+_id e paraleliza bem.
        # ``erasure``: delete by _id via _bulk delete action.
        # ``erasure_by_query``: _delete_by_query por organization_id —
        # cobre dados ENTREGUES que não estão na DLQ (purge LGPD completo).
        capabilities=frozenset({"tls", "batch", "test", "idempotent", "partial_batch", "erasure", "erasure_by_query"}),
        required_secrets=("api_key",),
        label="Elasticsearch / OpenSearch (_bulk)",
        delivery_defaults={"concurrency": 8},
        # Campos de catálogo self-describing (galeria de destinos).
        category="SIEM",
        icon_id="elastic",
        tier="stable",
        order=20,
        description="Elasticsearch ou OpenSearch via API _bulk — indexação direta com isolamento de falha por item.",
    )
)
