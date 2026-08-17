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

**Um destino para N tenants (``source_type_from``).** ``source_type`` literal
custa um destino, uma rota e um segredo por cliente. Em 50 ou 100 isso não é
administrável — e o pior é que o modo de falha é humano: alguém aponta a rota do
cliente B para o destino do cliente A e os eventos aterrissam com o rótulo
errado, sem erro em lugar nenhum.

``source_type_from`` deriva o rótulo do próprio evento, então uma stack nano
única serve todos. O par natural é ``organization_vendor``
(``acme_sophos``, ``acme_wazuh``, ``beta_sophos``): separa por tenant, que é o
que importa para escopo, e mantém o vendor visível, que é o que as regras de
detecção filtram. ``source_type`` continua aceito e vira o FALLBACK — o rótulo
que sai quando o evento não carrega a origem.

Isto não substitui isolamento: um único ClickHouse com todos os tenants na mesma
tabela é uma decisão de topologia, e quem consulta lá vê tudo. O rótulo separa
os dados; quem separa o ACESSO é a política do lado do nano.

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
from ..payload_shape import RowFieldSource
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
    ``nanosiem_ingest``.

    **O que esse usuário realmente pode fazer** (medido numa instalação
    26.5.1, não deduzido do nome): além do INSERT na tabela de aterrissagem,
    ele tem ``SELECT`` em ``ocsf_logs``, ``ocsf_logs_raw`` e
    ``ocsf_logs_native_raw``, mais ``SELECT``/``dictGet`` em uma lista de
    tabelas de agregação e dicionários. Ou seja: NÃO é INSERT-only e LÊ
    conteúdo de log. A versão anterior deste docstring afirmava o contrário —
    era dedução a partir do nome, e estava errada. Quem trata essa credencial
    como "não vê dado" está dimensionando o risco para menos.

    A lista de grants importa por um motivo operacional: a tabela de
    aterrissagem do nano é ``Engine=Null`` e existe só para alimentar uma
    cadeia de materialized views. O INSERT roda com as permissões DESTE
    usuário ao longo de toda a cadeia, então basta uma tabela intermediária
    sem grant para todo INSERT que a alcance falhar com ``ACCESS_DENIED``
    (código 497) — enquanto conexão, tabela e colunas seguem perfeitas. Ver a
    ressalva de ``ClickHouseClient.test()``.
    """

    url: str = Field(
        description="Endereço da interface HTTP do ClickHouse do nano: 8123 em texto "
        "claro (ex: http://nano.interno:8123) ou 8443 com TLS "
        "(ex: https://nano.interno:8443). Confira qual das duas o seu deploy "
        "PUBLICOU: é comum o compose expor só a 8443 para fora do host. A porta "
        "9000 é o protocolo nativo e não serve aqui."
    )
    source_type: str = Field(
        default="",
        description="Rótulo minúsculo FIXO deste feed no nano (ex: centralops_sophos). "
        "É a chave de escopo das detecções e dos painéis do lado do nano: sem ela "
        "as linhas caem como 'unknown' e as regras com escopo de fonte ignoram todas. "
        "Deixe vazio se for usar 'Rótulo derivado de'; se preencher os dois, este "
        "vira o valor de reserva.",
    )
    source_type_from: Optional[RowFieldSource] = Field(
        default=None,
        description="Rótulo derivado de cada evento, em vez de fixo — é o que permite "
        "UMA stack nano servir vários clientes. Use 'organization_vendor' para "
        "separar por cliente mantendo o vendor visível (acme_sophos, beta_wazuh). "
        "O valor é normalizado para minúsculo com underscore automaticamente.",
    )
    database: str = Field(default="nanosiem", description="Banco do nano (padrão nanosiem)")
    table: str = Field(
        default="ocsf_logs_raw",
        description="Tabela de aterrissagem. Use ocsf_logs_raw: a ocsf_logs_native_raw "
        "existe para o protocolo nativo na porta 9000, não para o caminho HTTP.",
    )
    username: str = Field(
        default="nanosiem_ingest",
        description="Usuário de ingestão do nano. Apesar do nome, ele NÃO é "
        "INSERT-only: também lê as tabelas de log. Ainda assim é o certo aqui — o "
        "usuário da aplicação tem ALTER e DROP no banco inteiro, e o admin é "
        "superusuário do cluster.",
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
        if not rotulo.strip() and self.source_type_from is None:
            # Recusar os dois vazios é o ponto: sem rótulo o nano joga tudo no
            # balde 'unknown' e TODA regra com escopo de fonte ignora o feed —
            # entrega verde, detecção zero.
            raise ValueError(
                "informe um rótulo: 'source_type' para valor fixo, ou "
                "'source_type_from' para derivar de cada evento (recomendado "
                "quando o mesmo nano recebe mais de um cliente)"
            )
        if not rotulo.strip():
            return self
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
        # Literal só quando existe: mandar ``{"source_type": ""}`` gravaria
        # string vazia, que do lado do nano é indistinguível de coluna nunca
        # preenchida — o oposto do sentinela 'unresolved', que é consultável.
        row_fields=({"source_type": cfg.source_type} if cfg.source_type else {}),
        row_fields_from=(
            {"source_type": cfg.source_type_from} if cfg.source_type_from else {}
        ),
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
