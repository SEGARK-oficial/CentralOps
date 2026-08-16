"""Kind ``nano`` — nano SIEM por ingestão OCSF direta.

O nano é um SIEM cujo armazenamento é ClickHouse e que aceita OCSF já
normalizado, sem passar pelo pipeline de parsing dele. Ele publica dois caminhos
de entrada: um para quem manda dado cru e quer que o nano parseie, e outro para
quem já normalizou e entrega OCSF pronto. Este destino é o segundo, e o nano fica
como camada de busca, detecção e correlação.

O CentralOps é, por construção, uma camada de normalização: coleta do vendor,
mapeia para OCSF 1.8, valida contra o manifesto, enriquece e reduz. Ou seja,
pertence a esse grupo por definição, e este kind é o atalho para isso.

**Por que um kind próprio e não "configure o ClickHouse".** Porque o contrato do
nano tem cinco decisões acopladas, e errar qualquer uma falha em silêncio:

* a interface é HTTP na 8123, não o protocolo nativo na 9000;
* o banco é ``nanosiem`` e a tabela ``ocsf_logs_raw``, em campos separados;
* o corpo é o OCSF puro, não o envelope canônico;
* a linha é ``{"event": <ocsf>, "source_type": "<feed>"}``, não o OCSF no topo;
* ``source_type`` é obrigatório na prática, porque sem ele as linhas caem no
  balde ``unknown`` e as detecções com escopo de fonte ignoram todas.

Com o kind ``clickhouse`` genérico isso são seis campos que o operador precisa
acertar juntos, lendo a documentação do nano em outra aba. Aqui são dois: onde,
e com que rótulo.

**Por que NÃO é um sender novo.** A entrega é idêntica à do ClickHouse, então
este módulo é só um schema de config e uma fábrica: reusa ``ClickHouseClient``
inteiro, com autenticação por header, TLS, CA bundle, classificação de DLQ,
disjuntor e leitura de ``X-ClickHouse-Summary``. Duplicar o sender para mudar um
dicionário seria abrir a porta para um kind por produto que fala ClickHouse,
com o catálogo apodrecendo em cópias do mesmo sender.

Referência: https://nano.rs/docs/ocsf/integrations/direct-ocsf/
"""

from __future__ import annotations

import logging
import urllib.parse
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator

from ..clickhouse_sender import ClickHouseClient
from .registry import DestinationConfig, DestinationRegistration, register

logger = logging.getLogger(__name__)

KIND = "nano"

# ── O contrato do nano, fixado aqui em vez de virar campo de formulário ──────
# Estes valores não são preferência: são o contrato publicado. Expor cada um
# como campo editável seria transferir para o operador a chance de errar
# exatamente aquilo que este kind existe para acertar.
_PAYLOAD = "ocsf"          # o nano quer OCSF 1.8 puro, sem o envelope canônico
_ROW_SHAPE = "wrapped"     # o OCSF vai DENTRO de uma coluna, não no topo
_EVENT_KEY = "event"       # nome da coluna que recebe o evento
_PORTA_NATIVA = 9000       # protocolo nativo TCP: este sink é HTTP


class NanoConfig(BaseModel):
    """Config do destino nano. Dois campos obrigatórios: onde, e com que rótulo.

    A senha fica em ``secret_ref`` (cofre), nunca aqui. É a
    ``CLICKHOUSE_INGEST_PASSWORD`` gerada pelo instalador do nano, do usuário
    ``nanosiem_ingest``, que é INSERT-only por desenho e não consegue ler
    conteúdo de log nem escrever em outras tabelas.
    """

    url: str = Field(
        description="Endereço HTTP do ClickHouse do nano, com a porta 8123 "
        "(ex: http://nano.interno:8123). A porta 9000 é o protocolo nativo e não serve aqui."
    )
    source_type: str = Field(
        description="Rótulo minúsculo deste feed no nano (ex: centralops_sophos). "
        "É a chave de escopo das detecções e dos painéis do lado do nano: sem ela "
        "as linhas caem como 'unknown' e as regras com escopo de fonte ignoram todas."
    )
    database: str = Field(default="nanosiem", description="Banco do nano (padrão nanosiem)")
    table: str = Field(
        default="ocsf_logs_raw",
        description="Tabela de aterrissagem. Use ocsf_logs_raw: a ocsf_logs_native_raw "
        "existe para o protocolo nativo na porta 9000, não para o caminho HTTP.",
    )
    username: str = Field(
        default="nanosiem_ingest",
        description="Usuário de ingestão do nano (INSERT-only). Nunca use o usuário "
        "da aplicação: ele lê e escreve o banco inteiro.",
    )
    verify_tls: bool = Field(default=True, description="Verificar certificado TLS (só com https)")
    ca_bundle: Optional[str] = Field(
        default=None, description="Path do CA bundle PEM customizado (apenas com verify_tls=True)"
    )

    @model_validator(mode="after")
    def _barreiras(self) -> "NanoConfig":
        partes = urllib.parse.urlparse(self.url)
        if partes.scheme not in {"http", "https"}:
            raise ValueError("url precisa começar com http:// ou https://")
        try:
            porta = partes.port
        except ValueError:
            raise ValueError("porta inválida na url")
        if porta == _PORTA_NATIVA:
            raise ValueError(
                "porta 9000 é o protocolo nativo TCP do ClickHouse, que é binário e exige "
                "driver próprio. Este destino fala HTTP: use 8123 (ou 8443 com TLS)"
            )

        # Nome qualificado no campo errado. Os identificadores são citados com
        # crase separadamente, então o ponto viraria parte literal do nome.
        if "." in self.table:
            banco, _, tabela = self.table.partition(".")
            raise ValueError(
                f"informe só o nome da tabela em 'table'; o banco vai em 'database'. "
                f"Você digitou '{self.table}': use database='{banco}' e table='{tabela}'"
            )
        if "." in self.database:
            raise ValueError("'database' não pode conter ponto")

        rotulo = self.source_type
        if not rotulo.strip():
            raise ValueError("source_type é obrigatório")
        if rotulo != rotulo.strip() or any(c.isspace() for c in rotulo):
            raise ValueError("source_type não pode ter espaço")
        if rotulo != rotulo.lower():
            # Recusar em vez de normalizar: o rótulo também é digitado do lado do
            # nano, nas regras de detecção. Corrigir em silêncio aqui criaria uma
            # divergência entre os dois lados que ninguém veria.
            raise ValueError(
                f"source_type precisa ser minúsculo (o nano separa facetas por caixa): "
                f"use '{rotulo.lower()}'"
            )
        return self


def _factory(config: DestinationConfig, secrets: Optional[Any] = None) -> ClickHouseClient:
    cfg = NanoConfig(**dict(config.config or {}))

    password: Optional[str] = None
    if secrets is not None and config.secret_ref:
        try:
            password = secrets.decrypt(config.secret_ref)
        except Exception as exc:
            # Não logar secret_ref nem o objeto exc (path da master key/cofre).
            logger.warning(
                "nano: falha ao decifrar credencial (%s) — password=None (dormant)",
                type(exc).__name__,
            )

    return ClickHouseClient(
        url=cfg.url,
        password=password,
        database=cfg.database,
        table=cfg.table,
        username=cfg.username,
        payload=_PAYLOAD,
        row_shape=_ROW_SHAPE,
        event_key=_EVENT_KEY,
        row_fields={"source_type": cfg.source_type},
        # Barulhento de propósito. A forma da linha é 100% controlada por este
        # módulo e casa exatamente com as colunas publicadas do nano, então um
        # campo desconhecido significa que o schema do outro lado mudou. Com o
        # skip ligado (o default do servidor) isso viraria HTTP 200 com linha
        # vazia; com 0 vira erro 117 explícito e o evento vai para a DLQ com a
        # mensagem do ClickHouse.
        skip_unknown_fields=False,
        # O servidor do nano já roda async_insert com wait_for_async_insert=1;
        # não há o que ligar deste lado.
        async_insert=False,
        verify_tls=cfg.verify_tls,
        ca_bundle=cfg.ca_bundle,
    )


register(
    DestinationRegistration(
        kind=KIND,
        factory=_factory,
        config_schema=NanoConfig,
        # Fila própria: o nano é um destino de SIEM e não deve dividir
        # concorrência com data lake analítico.
        default_queue="dispatch.nano",
        capabilities=frozenset({"tls", "batch", "test", "at_least_once"}),
        required_secrets=("clickhouse_ingest_password",),
        label="nano SIEM",
        delivery_defaults={"concurrency": 8},
        category="SIEM",
        description="nano SIEM por ingestão OCSF direta: entrega OCSF 1.8 já normalizado "
        "no ClickHouse do nano, sem passar pelo Vector dele.",
        icon_id="nano",
        docs_url="https://nano.rs/docs/ocsf/integrations/direct-ocsf/",
        tier="stable",
        order=35,
    )
)
