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
import urllib.parse
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, model_validator

from ..clickhouse_sender import ClickHouseClient
from ..payload_shape import (
    DESCRICAO as PAYLOAD_DESCRICAO,
    DESCRICAO_EVENT_KEY,
    DESCRICAO_ROW_FIELDS,
    DESCRICAO_ROW_SHAPE,
    PayloadShape,
    RowShape,
)
from .registry import DestinationConfig, DestinationRegistration, register

logger = logging.getLogger(__name__)

KIND = "clickhouse"

#: Porta do protocolo NATIVO TCP do ClickHouse. Este sink é HTTP e nunca vai
#: falar com ela: o servidor responde "You must use port 8123 for HTTP" e fecha
#: o socket, o que vira erro de conexão, retry, e disjuntor aberto.
_PORTA_NATIVA = 9000


class ClickHouseConfig(BaseModel):
    """Schema de config do destino ClickHouse (exposto no catálogo da UI).

    A senha **não** está aqui: fica em ``secret_ref`` (cofre). ``ca_bundle`` é
    path no filesystem do collector, não secret.
    """

    url: str = Field(description="URL base da interface HTTP (ex: https://clickhouse:8443)")
    database: str = Field(default="default", description="Banco de destino")
    table: str = Field(default="centralops_events", description="Tabela de destino")
    username: str = Field(default="default", description="Usuário ClickHouse")
    payload: PayloadShape = Field(default="envelope", description=PAYLOAD_DESCRICAO)
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
    row_shape: RowShape = Field(default="flat", description=DESCRICAO_ROW_SHAPE)
    event_key: str = Field(default="event", description=DESCRICAO_EVENT_KEY)
    row_fields: Dict[str, str] = Field(default_factory=dict, description=DESCRICAO_ROW_FIELDS)
    verify_tls: bool = Field(default=True, description="Verificar certificado TLS")
    ca_bundle: Optional[str] = Field(
        default=None, description="Path do CA bundle PEM customizado (apenas com verify_tls=True)"
    )

    @model_validator(mode="after")
    def _barreiras(self) -> "ClickHouseConfig":
        """Recusa no save as configs que só falhariam em silêncio na entrega.

        Cada regra aqui corresponde a um modo de falha observado em produção,
        não a uma preferência de estilo. O gancho já existia (o router converte
        exceção de validação em 422); o que faltava era usá-lo.
        """
        # 1. Scheme. Sem isso, aiohttp levanta e vira "erro de conexão" genérico.
        partes = urllib.parse.urlparse(self.url)
        if partes.scheme not in {"http", "https"}:
            raise ValueError("url precisa começar com http:// ou https://")

        # 2. Porta nativa. Barreira dura: 9000 é reservada pela convenção do
        #    próprio ClickHouse para o protocolo nativo, e o custo de deixar
        #    passar já foi medido em dezenas de milhares de eventos na DLQ.
        try:
            porta = partes.port
        except ValueError:
            raise ValueError("porta inválida na url")
        if porta == _PORTA_NATIVA:
            raise ValueError(
                "porta 9000 é o protocolo nativo TCP do ClickHouse. Este destino usa a "
                "interface HTTP: use 8123 (ou 8443 com TLS)"
            )

        # 3. Nome qualificado no campo errado. Os identificadores são citados
        #    com crase separadamente, então o ponto vira parte LITERAL do nome
        #    e a tabela nunca resolve.
        if "." in self.table:
            banco, _, tabela = self.table.partition(".")
            raise ValueError(
                f"informe só o nome da tabela em 'table'; o banco vai em 'database'. "
                f"Você digitou '{self.table}': use database='{banco}' e table='{tabela}'"
            )
        if "." in self.database:
            raise ValueError("'database' não pode conter ponto")

        # 4-7. Coerência do wrapper.
        if self.row_shape == "wrapped":
            if not self.event_key.strip():
                raise ValueError("com row_shape='wrapped', event_key é obrigatório")
        else:
            # Recusar em vez de ignorar: campo preenchido que não tem efeito é
            # a mesma armadilha silenciosa, só que na config em vez de no fio.
            if self.row_fields:
                raise ValueError(
                    "row_fields só tem efeito com row_shape='wrapped'; hoje seria ignorado "
                    "em silêncio. Mude row_shape para 'wrapped' ou limpe row_fields"
                )
        for chave in self.row_fields:
            if not chave.strip():
                raise ValueError("row_fields tem uma coluna sem nome")
            if chave != chave.strip() or any(c.isspace() for c in chave):
                raise ValueError(f"nome de coluna inválido em row_fields: {chave!r}")
            if chave == self.event_key:
                raise ValueError(
                    f"row_fields não pode redefinir a coluna do evento ({self.event_key!r})"
                )
        return self


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
        payload=cfg.payload,
        row_shape=cfg.row_shape,
        event_key=cfg.event_key,
        row_fields=cfg.row_fields,
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
