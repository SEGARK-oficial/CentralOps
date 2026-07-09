"""Kind ``clickhouse`` — destino ClickHouse via HTTP ``JSONEachRow``.

ClickHouse é o armazenamento analítico colunar mais comum para SIEM/observabilidade
de alto volume (Cribl Lake, SigNoz, HyperDX e vários SOCs caseiros indexam nele).
A ingestão usa a interface HTTP (``INSERT … FORMAT JSONEachRow``), portável entre
ClickHouse OSS, ClickHouse Cloud e compatíveis.

A senha do usuário fica em ``secret_ref`` (cofre) — nunca na config. Sem senha
(``secret_ref`` ausente) o cliente ainda é construído (clusters sem auth), mas
``send_batch``/``test`` reportam o erro do servidor de forma descritiva.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from pydantic import BaseModel, Field

from ..clickhouse_sender import ClickHouseClient
from .registry import DestinationConfig, DestinationRegistration, register

logger = logging.getLogger(__name__)

KIND = "clickhouse"


class ClickHouseConfig(BaseModel):
    """Schema de config do destino ClickHouse (exposto no catálogo da UI).

    A senha **não** está aqui: fica em ``secret_ref`` (cofre). ``ca_bundle`` é
    path no filesystem do collector, não secret.
    """

    url: str = Field(description="URL base da interface HTTP (ex: https://clickhouse:8443)")
    database: str = Field(default="default", description="Banco de destino")
    table: str = Field(default="centralops_events", description="Tabela de destino")
    username: str = Field(default="default", description="Usuário ClickHouse")
    skip_unknown_fields: bool = Field(
        default=True,
        description="Ignora campos do envelope ausentes na tabela (input_format_skip_unknown_fields) "
        "em vez de derrubar o INSERT — recomendado.",
    )
    async_insert: bool = Field(
        default=False,
        description="Usa async_insert do servidor (agrupa micro-lotes). Mantém wait_for_async_insert=1 "
        "para confirmar persistência.",
    )
    verify_tls: bool = Field(default=True, description="Verificar certificado TLS")
    ca_bundle: Optional[str] = Field(
        default=None, description="Path do CA bundle PEM customizado (apenas com verify_tls=True)"
    )


def _factory(config: DestinationConfig, secrets: Optional[Any] = None) -> ClickHouseClient:
    cfg = ClickHouseConfig(**dict(config.config or {}))

    password: Optional[str] = None
    if secrets is not None and config.secret_ref:
        try:
            password = secrets.decrypt(config.secret_ref)
        except Exception as exc:
            # Não logar secret_ref nem o objeto exc (path da master key/cofre).
            logger.warning(
                "clickhouse: falha ao decifrar credencial (%s) — password=None (dormant)",
                type(exc).__name__,
            )

    return ClickHouseClient(
        url=cfg.url,
        password=password,
        database=cfg.database,
        table=cfg.table,
        username=cfg.username,
        skip_unknown_fields=cfg.skip_unknown_fields,
        async_insert=cfg.async_insert,
        verify_tls=cfg.verify_tls,
        ca_bundle=cfg.ca_bundle,
    )


register(
    DestinationRegistration(
        kind=KIND,
        factory=_factory,
        config_schema=ClickHouseConfig,
        default_queue="dispatch.clickhouse",
        # HTTP responde por lote (sem dedup nativo): at_least_once. NDJSON
        # paraleliza bem entre múltiplas conexões (E5).
        capabilities=frozenset({"tls", "batch", "test", "at_least_once"}),
        required_secrets=("clickhouse_password",),
        label="ClickHouse",
        delivery_defaults={"concurrency": 8},
        category="Data Lake",
        description="ClickHouse via HTTP (INSERT … FORMAT JSONEachRow) — armazenamento analítico colunar de alto volume.",
        icon_id="clickhouse",
        docs_url="https://clickhouse.com/docs/en/interfaces/http",
        tier="stable",
        order=75,
    )
)
