"""Protocolos e capabilities do enriquecimento (ADR-LOCAL-0002).

Módulo deliberadamente MAGRO e sem dependência pesada: ele é importado pelo
``applier``, que está sob guard de CI de pureza. Qualquer coisa que toque o mundo
(httpx, redis, sqlalchemy) entra em ``runtime`` ou no módulo do enricher, nunca aqui.

O contrato central é **bulk-first**. Um enricher nunca é perguntado por uma chave: é
perguntado por um conjunto. Isso não é ergonomia — é o que permite que o custo de
rede seja amortizado sobre o lote inteiro em vez de multiplicado pelo número de
eventos, que é a única forma de caber num laço serial.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Iterable,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    runtime_checkable,
)

#: Tipos de chave reconhecidos. Fechado de propósito: o ``kind`` seleciona a
#: normalização aplicada à chave antes do lookup (ver ``applier._normalize_key``),
#: e um kind desconhecido significaria comparar strings cruas contra uma tabela
#: normalizada — miss silencioso de 100%, o modo de falha mais caro desta feature.
KEY_KINDS: frozenset[str] = frozenset(
    {"ip", "domain", "url", "file_hash", "cve", "mac", "user", "container_id"}
)

#: Modo de execução declarado pelo enricher.
#: ``local``  — o estado cabe em memória; carregado 1×/ciclo; aplicado no seam E1
#:              (por evento, puro), logo alimenta a classificação em voo.
#: ``remote`` — exige I/O por chave desconhecida; resolvido em bulk no flush do
#:              lote (seam E2); NÃO alimenta a classificação em voo.
ENRICHER_MODES: frozenset[str] = frozenset({"local", "remote"})

#: Classificação de egresso do dado do cliente. É informação de PRIVACIDADE, não
#: de rede: ``third_party`` significa que um indicador do cliente (um IP, um hash)
#: sai da infraestrutura dele para um terceiro. A UI exibe e exige opt-in por org.
EGRESS_KINDS: frozenset[str] = frozenset({"none", "internal", "third_party"})


class PiiEnricherRefused(ValueError):
    """Enricher que declara ``emits_pii`` foi recusado no registro.

    Não é configurável e não tem override. A saída do enriquecimento vive sob
    ``_centralops``, e ``routing/pii_redaction.ALLOWED_ROOTS`` é
    ``{"raw", "normalized"}`` — logo nenhuma spec de redação de rota alcança o que
    escrevemos. Aceitar um enricher de PII aqui seria produzir dado pessoal num
    caminho que o operador configurou justamente para proteger, sem nenhum aviso.
    """


class EnrichmentConfigError(ValueError):
    """Política/tabela de enriquecimento inválida. Vira 422 na API."""


@dataclass(frozen=True, slots=True)
class EnricherCapabilities:
    """O que um enricher declara sobre si.

    Vários destes campos são **contratos de segurança verificados no registro**, não
    metadados de catálogo: ``emits_pii`` recusa o registro, ``redistributable``
    impede embarcar base de terceiro em imagem, e ``mode``/``p99_budget_ms`` são o
    que o runtime usa para decidir se o enricher pode rodar no seam puro.
    """

    #: Tipos de chave que este enricher sabe resolver. Uma regra cujo ``key.kind``
    #: não está aqui é recusada na compilação da política, não em runtime.
    key_kinds: frozenset[str]
    mode: str  # ∈ ENRICHER_MODES
    supports_bulk: bool = True
    #: Orçamento declarado de latência p99 da RESOLUÇÃO. Para ``mode="local"`` é
    #: também o teto que o wrapper de runtime usa para desabilitar o enricher que
    #: mentiu sobre não fazer I/O (ver runtime._guarded_load) — ``is_local``
    #: auto-declarado não pode ser a única defesa.
    p99_budget_ms: float = 10.0
    suggested_ttl_s: int = 3600
    suggested_negative_ttl_s: int = 300
    #: True ⇒ registro RECUSADO. Ver :class:`PiiEnricherRefused`.
    emits_pii: bool = False
    #: Licença da base de dados que o enricher consome (não do código).
    license: str = "proprietary"
    #: False ⇒ a base NÃO pode ser embarcada em imagem publicada. GeoLite2 (EULA
    #: com license key individual) e CC BY-SA num artefato EE proprietário são os
    #: dois casos concretos que motivaram este campo.
    redistributable: bool = False
    egress: str = "none"  # ∈ EGRESS_KINDS

    def __post_init__(self) -> None:
        if self.mode not in ENRICHER_MODES:
            raise EnrichmentConfigError(
                f"mode inválido: {self.mode!r}. Esperado um de {sorted(ENRICHER_MODES)}"
            )
        if self.egress not in EGRESS_KINDS:
            raise EnrichmentConfigError(
                f"egress inválido: {self.egress!r}. Esperado um de {sorted(EGRESS_KINDS)}"
            )
        unknown = set(self.key_kinds) - KEY_KINDS
        if unknown:
            raise EnrichmentConfigError(
                f"key_kinds desconhecidos: {sorted(unknown)}. Válidos: {sorted(KEY_KINDS)}"
            )
        if not self.key_kinds:
            raise EnrichmentConfigError("key_kinds não pode ser vazio")


@dataclass(frozen=True, slots=True)
class EnrichContext:
    """O que o resolvedor sabe sobre o ciclo corrente.

    ``organization_id`` é obrigatório e não-nulo por design: ele entra na chave de
    cache (L1 e L2). Não há afinidade por tenant no data-plane
    (``queues.dispatch_dest_shard_queue`` particiona por ``sha1(destination_id)``) e
    ``worker_max_tasks_per_child=500`` faz o mesmo fork atender centenas de orgs —
    uma chave de cache sem org serviria o CMDB da Org A para a Org B no ciclo
    seguinte, dentro do mesmo processo.

    ``as_of`` é o ponto-no-tempo para reprocessamento. Enriquecimento é
    time-dependent: reprocessar 90 dias de backlog com as tabelas de HOJE produz
    atribuição errada. ``None`` = usar a versão corrente (caminho ao vivo).
    """

    organization_id: int
    integration_id: Optional[int] = None
    #: epoch em MILISSEGUNDOS (mesma unidade de ``timestamp_t`` do OCSF), ou None.
    as_of: Optional[int] = None
    #: Nome da ``EnrichmentTable`` da regra que está carregando. Vem da REGRA, não
    #: da config do enricher — a mesma instância de ``table_cidr`` serve N tabelas
    #: diferentes na mesma política. O runtime injeta com ``dataclasses.replace``
    #: por regra; enricher que não usa tabela ignora.
    table: Optional[str] = None
    #: Referência de segredo no cofre — NUNCA o segredo em claro. Resolvido pelo
    #: runtime 1×/ciclo, jamais por evento.
    secret_ref: Optional[str] = None
    config: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class LookupTable(Protocol):
    """Estado residente de um enricher local. **Puro** — só leitura de memória.

    É o único objeto que o ``applier`` toca no caminho de um enricher local, e é por
    isso que não existe caminho de *miss* que precise sair para o mundo: a tabela
    está inteira em memória, então "não achei" é um ``dict.get`` devolvendo ``None``,
    não uma ida ao Redis.

    Isso só é viável porque **o ciclo de coleta é mono-tenant** (``pipeline.py:885``:
    *"o ciclo é mono-tenant (uma integração), então organization_id é autoritativo"*).
    A tabela residente é a da org do ciclo, não a de todas as orgs. Se essa
    invariante cair, o modelo de memória deste pacote cai junto — daí o
    ``assert_mono_tenant`` em ``runtime``.
    """

    def lookup(self, key: str) -> Optional[Mapping[str, Any]]:
        """Valor da chave, ou ``None``. NÃO pode fazer I/O, logar nem levantar."""
        ...

    @property
    def entry_count(self) -> int:
        """Número de entradas — alimenta o gauge e o teto de memória."""
        ...

    @property
    def approx_bytes(self) -> int:
        """Bytes residentes aproximados. Usado pelo teto por container."""
        ...


class LocalEnricher(Protocol):
    """Enricher cujo estado cabe em memória e é carregado 1×/ciclo.

    ``load`` PODE fazer I/O (é chamado fora do laço, em ``asyncio.to_thread`` ou
    ``await``). O que não pode fazer I/O é a :class:`LookupTable` que ele devolve.
    """

    caps: EnricherCapabilities

    async def load(self, ctx: EnrichContext) -> LookupTable:
        ...


class RemoteEnricher(Protocol):
    """Enricher que exige I/O por chave desconhecida.

    ``resolve`` recebe as chaves DISTINTAS do lote e devolve um mapa. Uma chave
    ausente do retorno é *miss confirmado*; uma chave mapeada para ``None`` também.
    A distinção entre "miss confirmado" e "não consegui resolver" é feita levantando
    exceção (não resolvido ⇒ o runtime registra ``error`` e o applier degrada por
    ``on_error``), nunca omitindo silenciosamente.
    """

    caps: EnricherCapabilities

    async def resolve(
        self, keys: Sequence[str], ctx: EnrichContext
    ) -> Mapping[str, Optional[Mapping[str, Any]]]:
        ...


#: Factory de um enricher: (config_resolvida) → instância.
#: ``Any`` no retorno porque a instância satisfaz LocalEnricher OU RemoteEnricher —
#: tipar a união aqui obrigaria o registry a conhecer os dois protocolos em tempo
#: de checagem, e o registry deve ser agnóstico.
EnricherFactory = Callable[[Mapping[str, Any]], Any]


@dataclass(frozen=True)
class EnricherRegistration:
    """Metadado completo de um enricher — o mesmo formato self-describing de
    ``DestinationRegistration`` e ``PlatformRegistration``.

    É o que torna a galeria de fontes de enriquecimento 100% plugin-driven e
    simétrica à de integrações e destinos: o autor do enricher declara aqui
    label/ícone/categoria/descrição, e o frontend lê via
    ``GET /collectors/enrichment/enrichers`` SEM hardcode. Adicionar um enricher =
    registrar isto no módulo dele; ZERO mudança no frontend, no pipeline ou no beat.
    """

    name: str
    factory: EnricherFactory
    caps: EnricherCapabilities
    #: Schema pydantic da config. ``None`` = enricher sem config.
    config_schema: Optional[type] = None
    #: nomes lógicos de segredos exigidos (api key, token).
    required_secrets: Tuple[str, ...] = ()
    label: str = ""
    #: agrupa na galeria: "Threat Intel", "Geo / Rede", "Identidade",
    #: "Ativos / CMDB", "Vulnerabilidade", "Nuvem", "Tabela do cliente".
    category: str = "Outros"
    description: str = ""
    #: id de ícone de marca (ver ``brand-icons`` no frontend).
    icon_id: Optional[str] = None
    docs_url: Optional[str] = None
    #: maturidade: "stable" | "beta" | "generic".
    tier: str = "beta"
    order: int = 100
    #: Campos que o enricher devolve, para a UI montar o editor de `outputs`
    #: sem o usuário adivinhar. Chave → descrição curta.
    output_fields: Mapping[str, str] = field(default_factory=dict)

    def describe(self) -> Dict[str, Any]:
        """Forma serializável para o endpoint de catálogo (a UI lê isto)."""
        return {
            "name": self.name,
            "label": self.label or self.name,
            "category": self.category,
            "description": self.description,
            "icon_id": self.icon_id,
            "docs_url": self.docs_url,
            "tier": self.tier,
            "order": self.order,
            "mode": self.caps.mode,
            "key_kinds": sorted(self.caps.key_kinds),
            "supports_bulk": self.caps.supports_bulk,
            "p99_budget_ms": self.caps.p99_budget_ms,
            "suggested_ttl_s": self.caps.suggested_ttl_s,
            "suggested_negative_ttl_s": self.caps.suggested_negative_ttl_s,
            "license": self.caps.license,
            "redistributable": self.caps.redistributable,
            # Exibido com destaque na UI: é consentimento de privacidade, não
            # detalhe técnico. "third_party" significa que um indicador do
            # cliente sai da infra dele.
            "egress": self.caps.egress,
            "required_secrets": list(self.required_secrets),
            "output_fields": dict(self.output_fields),
            "config_schema": (
                self.config_schema.model_json_schema()  # type: ignore[union-attr]
                if self.config_schema is not None
                else None
            ),
        }


class Resolution(Protocol):
    """Fonte de valores já resolvidos, vista pelo ``applier``.

    Existe para que haja **um único aplicador** em vez de dois meio-prontos: o seam
    local e o seam remoto diferem apenas em QUEM preencheu o mapa, nunca em como o
    evento é escrito. Ver ``runtime.TableResolution`` (local) e
    ``runtime.BulkResolution`` (remoto).
    """

    def get(self, rule_id: str, key: str) -> Tuple[bool, Optional[Mapping[str, Any]]]:
        """``(resolvido, valor)``.

        ``(False, None)`` = esta resolução não sabe responder por esta regra (ex.: o
        applier local encontrando uma regra remota) ⇒ o applier PULA sem contar miss.
        ``(True, None)``  = miss confirmado ⇒ aplica ``on_miss``.
        ``(True, {...})`` = hit.

        Distinguir os dois primeiros importa: contar "regra não avaliada neste seam"
        como miss produziria um miss_rate de 100% num enricher que funciona — o tipo
        de métrica que faz alguém desligar a feature certa pelo motivo errado.
        """
        ...


def iter_key_kinds(caps: EnricherCapabilities) -> Iterable[str]:
    """Ordem estável dos kinds — para mensagens de erro determinísticas."""
    return sorted(caps.key_kinds)
