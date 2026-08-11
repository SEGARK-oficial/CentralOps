"""Enricher **OpenCTI** — indicadores como TABELA LOCAL (ADR-LOCAL-0002).

OpenCTI é uma plataforma de threat intel do próprio cliente (self-hosted na
esmagadora maioria dos casos). A tentação é consultá-la por evento; a decisão aqui é
o contrário, e é deliberada:

**Por que snapshot→tabela local e não lookup remoto.** O laço de coleta é serial
(``pipeline.py:1018``). Um GraphQL round-trip de ~20 ms por evento a 5.000 EPS pediria
100 s de I/O por segundo de relógio. Além disso a base de indicadores de um cliente é
pequena o bastante para caber em memória (dezenas a centenas de milhares de
observáveis) e muda em minutos, não em milissegundos. Snapshot periódico é a
arquitetura certa — é o que Cribl chama de Lookup e o Vector de ``enrichment_tables``,
e o que o próprio Tenzir faz com ``context``.

O efeito colateral é o mais importante: sendo LOCAL, este enricher roda no seam por
evento e portanto **alimenta a classificação em voo** (``pipeline.py:1401``). Um
lookup remoto resolveria só no flush do lote e chegaria tarde demais para a detecção.

**Egresso: nenhum.** A infraestrutura é do cliente e o fluxo é de entrada (nós
buscamos a lista). Nenhum indicador do cliente sai para terceiro — o oposto do
VirusTotal. É por isso que este enricher é CE e aquele é gated.

⚠️ **Verificar contra a instância antes de usar em produção.** O schema GraphQL do
OpenCTI mudou entre as linhas 5.x e 6.x. A query default abaixo foi escrita para o
tipo ``stixCyberObservables`` (que expõe ``observable_value`` diretamente, evitando
ter que parsear padrões STIX de ``indicators``), mas o campo ``x_opencti_score`` e a
forma de ``objectMarking`` variam por versão. A config aceita ``query`` para
sobrescrever — é a saída de emergência para instâncias divergentes, e existe
justamente porque eu não posso validar contra a instância do cliente daqui.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Literal, Mapping, Optional

import aiohttp
from pydantic import BaseModel, Field, field_validator

from ..contract import EnrichContext, EnricherCapabilities, EnricherRegistration
from ..registry import register
from ..runtime import DictLookupTable
from ..stix import extract_observable_from_pattern, is_expired

logger = logging.getLogger(__name__)

#: Query dos OBSERVÁVEIS, com filtro de tipo no servidor. Pedir só o tipo que a
#: regra usa é a diferença entre baixar a base inteira e baixar o que interessa:
#: numa instância com 800 mil observáveis, uma tabela de IP não precisa carregar
#: hashes e URLs para depois descartá-los no cliente.
_OBSERVABLE_QUERY = """
query CentralOpsObservables($first: Int!, $after: ID, $types: [String]) {
  stixCyberObservables(first: $first, after: $after, types: $types, orderBy: created_at, orderMode: desc) {
    edges {
      node {
        id
        entity_type
        observable_value
        x_opencti_score
        created_at
        updated_at
        createdBy { ... on Identity { name } }
        objectMarking { edges { node { definition } } }
        objectLabel { edges { node { value } } }
      }
    }
    pageInfo { endCursor hasNextPage }
  }
}
"""

#: Query dos INDICADORES. É o que muda o enriquecimento de "esse IP aparece na
#: base" para "esse IP é C2 conhecido, ativo, com confiança 80". Traz três coisas
#: que o observável não tem e que decidem se vale alertar:
#:
#: - ``revoked`` e ``valid_until``: intel expirada é a maior fonte de falso
#:   positivo em feed de TI. Sem isso o alerta dispara por um IP que foi C2 há
#:   dois anos e hoje é de um CDN.
#: - ``confidence`` e ``x_opencti_detection``: separa o que o analista marcou
#:   como acionável do que entrou por importação automática.
#: - ``killChainPhases``: dá a fase (C2, entrega, exfiltração), que é o que
#:   transforma o hit em contexto acionável no SIEM.
_INDICATOR_QUERY = """
query CentralOpsIndicators($first: Int!, $after: ID) {
  indicators(first: $first, after: $after, orderBy: created_at, orderMode: desc) {
    edges {
      node {
        id
        name
        pattern
        pattern_type
        x_opencti_main_observable_type
        x_opencti_score
        x_opencti_detection
        confidence
        revoked
        valid_from
        valid_until
        created_at
        updated_at
        createdBy { ... on Identity { name } }
        objectMarking { edges { node { definition } } }
        objectLabel { edges { node { value } } }
        killChainPhases { edges { node { phase_name kill_chain_name } } }
      }
    }
    pageInfo { endCursor hasNextPage }
  }
}
"""

#: Preset → tipos de observável do OpenCTI. Evita que o operador precise saber
#: que "sha256" se chama ``StixFile`` no vocabulário STIX.
_PRESETS: Mapping[str, tuple] = {
    "ip": ("IPv4-Addr", "IPv6-Addr"),
    "domain": ("Domain-Name", "Hostname"),
    "url": ("Url",),
    "file_hash": ("StixFile", "Artifact"),
    "mac": ("Mac-Addr",),
    #: Tudo o que sabemos mapear. Útil para uma tabela única, ao custo de carregar
    #: a base inteira.
    "all_observables": tuple(),
    #: Indicadores STIX em vez de observáveis. Ver ``_INDICATOR_QUERY``.
    "indicators": tuple(),
}

#: Tipos de observável do OpenCTI → ``key_kind`` da nossa DSL. O mapa é explícito
#: porque um tipo não mapeado deve ser IGNORADO, não adivinhado: indexar um
#: ``Email-Addr`` como se fosse ``domain`` produziria hits errados silenciosos.
_ENTITY_TO_KIND: Mapping[str, str] = {
    "IPv4-Addr": "ip",
    "IPv6-Addr": "ip",
    "Domain-Name": "domain",
    "Hostname": "domain",
    "Url": "url",
    "StixFile": "file_hash",
    "Artifact": "file_hash",
    "Mac-Addr": "mac",
}


class OpenCTIConfig(BaseModel):
    """Config do enricher. Validada no commit da política, não em runtime."""

    url: str = Field(..., description="Base da instância OpenCTI, ex.: https://opencti.interno")

    #: O que buscar. Escolher o tipo aqui filtra NO SERVIDOR: uma tabela de IP não
    #: baixa hashes para descartar depois. ``indicators`` muda a fonte de
    #: observáveis para indicadores STIX, que trazem validade e fase de kill chain.
    #: ``Literal`` e não ``pattern``: o ``model_json_schema()`` do Pydantic emite
    #: ``enum`` para Literal e apenas uma string com regex para pattern. A UI é
    #: dirigida por esse schema, então com pattern o campo virava caixa de texto
    #: livre e o operador tinha que digitar o valor certo de cabeça.
    preset: Literal[
        "ip", "domain", "url", "file_hash", "mac", "all_observables", "indicators"
    ] = Field("ip", description="O que buscar no OpenCTI")
    page_size: int = Field(500, ge=1, le=5000)
    #: Teto de páginas por atualização. Sem ele, uma instância grande drenaria a
    #: tabela inteira num ciclo — o mesmo poison-loop que já derrubou coletores.
    max_pages: int = Field(40, ge=1, le=1000)
    #: Filtra por score mínimo. O default 0 traz tudo; instâncias com muito ruído
    #: de importação automática costumam querer >= 50.
    min_score: int = Field(0, ge=0, le=100)
    timeout_s: float = Field(30.0, gt=0, le=300)
    verify_tls: bool = True
    query: Optional[str] = Field(
        None, description="Sobrescreve a query GraphQL (saída para schemas divergentes)"
    )

    @field_validator("url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        """Passa a URL pelo guard de egresso do projeto (``core.url_policy``).

        Sem isto, a `url` — que passa a vir da fonte configurada por um admin de
        organização — iria direto para ``aiohttp.post`` COM o token no header
        ``Authorization``. Um endereço `http://169.254.169.254/…` ou um host
        externo qualquer transformaria o enricher em SSRF **com exfiltração de
        credencial**. O mesmo guard já protege okta, crowdstrike, veeam e wazuh
        (`normalize_service_url` recusa esquema fora de http/https, credencial
        embutida, query/fragment e path, e aplica as allowlists de host/CIDR de
        `OUTBOUND_URL_ALLOWED_*`).

        Validar no schema — e não no call-site — faz o 422 chegar a quem escreveu
        a config, no commit, em vez de virar um erro de rede no meio do ciclo.
        """
        from ....core.url_policy import normalize_service_url

        try:
            normalized = normalize_service_url(v)
        except ValueError as exc:
            raise ValueError(f"url inválida: {exc}") from exc
        if not normalized:
            raise ValueError("url é obrigatória")
        return normalized


_CAPS = EnricherCapabilities(
    key_kinds=frozenset({"ip", "domain", "url", "file_hash", "mac"}),
    mode="local",
    supports_bulk=True,
    # Orçamento da CARGA (I/O), não da aplicação. O wrapper de runtime desabilita
    # o enricher acima de 10× este valor.
    p99_budget_ms=5_000.0,
    suggested_ttl_s=900,
    suggested_negative_ttl_s=300,
    emits_pii=False,
    license="cliente (instância própria)",
    redistributable=False,
    egress="internal",
)


class OpenCTIEnricher:
    """Baixa observáveis do OpenCTI e materializa uma :class:`DictLookupTable`."""

    caps = _CAPS

    def __init__(self, config: Mapping[str, Any]) -> None:
        self._cfg = OpenCTIConfig(**dict(config or {}))

    async def load(self, ctx: EnrichContext) -> DictLookupTable:
        token = await _resolve_token(ctx)
        rows: Dict[str, Dict[str, Any]] = {}
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        is_indicators = self._cfg.preset == "indicators"
        # `query` custom continua vencendo: é a saída para instâncias 5.x/6.x com
        # schema divergente, e sem ela o operador ficaria preso ao nosso palpite.
        query = self._cfg.query or (
            _INDICATOR_QUERY if is_indicators else _OBSERVABLE_QUERY
        )
        types = list(_PRESETS.get(self._cfg.preset, ()))
        endpoint = self._cfg.url.rstrip("/") + "/graphql"
        timeout = aiohttp.ClientTimeout(total=self._cfg.timeout_s)
        connector = aiohttp.TCPConnector(ssl=None if self._cfg.verify_tls else False)

        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            cursor: Optional[str] = None
            for page in range(self._cfg.max_pages):
                variables: Dict[str, Any] = {
                    "first": self._cfg.page_size,
                    "after": cursor,
                }
                if types:
                    variables["types"] = types
                payload = {"query": query, "variables": variables}
                async with session.post(endpoint, json=payload, headers=headers) as resp:
                    if resp.status == 401 or resp.status == 403:
                        raise PermissionError(
                            f"OpenCTI recusou o token (HTTP {resp.status}) em {endpoint}"
                        )
                    resp.raise_for_status()
                    body = await resp.json()

                # GraphQL devolve 200 com `errors` — tratar como sucesso é o erro
                # clássico de integração GraphQL e produziria tabela vazia silenciosa.
                if body.get("errors"):
                    raise ValueError(
                        f"OpenCTI GraphQL devolveu erros: {body['errors'][:2]}"
                    )

                data = body.get("data") or {}
                container = (
                    data.get("indicators") if is_indicators
                    else data.get("stixCyberObservables")
                ) or {}
                edges = container.get("edges") or []
                for edge in edges:
                    node = (edge or {}).get("node") or {}
                    parsed = (
                        _parse_indicator(node, self._cfg.min_score) if is_indicators
                        else _parse_node(node, self._cfg.min_score)
                    )
                    if parsed is not None:
                        key, value = parsed
                        rows[key] = value

                page_info = container.get("pageInfo") or {}
                if not page_info.get("hasNextPage"):
                    break
                cursor = page_info.get("endCursor")
                if not cursor:
                    break
            else:
                logger.warning(
                    "OpenCTI: teto de %d páginas atingido — a tabela está TRUNCADA "
                    "(%d observáveis). Aumente max_pages ou filtre por min_score.",
                    self._cfg.max_pages, len(rows),
                    extra={"event": "enrich.opencti.truncated"},
                )

        return DictLookupTable(rows)


def _parse_node(node: Mapping[str, Any], min_score: int) -> Optional[tuple]:
    """Converte um nó do OpenCTI numa linha da tabela, ou ``None`` para descartar."""
    value = node.get("observable_value")
    if not isinstance(value, str) or not value.strip():
        return None
    entity_type = str(node.get("entity_type") or "")
    kind = _ENTITY_TO_KIND.get(entity_type)
    if kind is None:
        return None

    score = node.get("x_opencti_score")
    try:
        score_i = int(score) if score is not None else 0
    except (TypeError, ValueError):
        score_i = 0
    if score_i < min_score:
        return None

    markings = [
        (n or {}).get("node", {}).get("definition")
        for n in ((node.get("objectMarking") or {}).get("edges") or [])
    ]
    labels = [
        (n or {}).get("node", {}).get("value")
        for n in ((node.get("objectLabel") or {}).get("edges") or [])
    ]

    # Chave normalizada em minúsculas: a política declara ``normalize: ["lower"]``
    # do lado do evento, e as duas pontas TÊM que concordar — divergir aqui produz
    # miss de 100% sem erro nenhum, que é o modo de falha mais caro desta feature.
    key = value.strip().lower()
    return key, {
        "score": score_i,
        "entity_type": entity_type,
        "kind": kind,
        "opencti_id": node.get("id"),
        "created_at": node.get("created_at"),
        "updated_at": node.get("updated_at"),
        "created_by": ((node.get("createdBy") or {}) or {}).get("name"),
        "markings": [m for m in markings if m],
        "labels": [lb for lb in labels if lb],
        "source": "opencti",
    }


def _parse_indicator(node: Mapping[str, Any], min_score: int) -> Optional[tuple]:
    """Converte um Indicator STIX numa linha da tabela.

    Descarta o que não deve gerar alerta: revogado, fora da validade, abaixo do
    score, ou com padrão composto que não sabemos casar. Intel expirada é a maior
    fonte de falso positivo em feed de TI, e filtrar aqui (na carga) custa nada,
    enquanto filtrar no evento custaria uma condição por regra que ninguém lembra
    de escrever.
    """
    if node.get("revoked") is True:
        return None

    parsed = extract_observable_from_pattern(str(node.get("pattern") or ""))
    if parsed is None:
        return None
    kind, value = parsed

    try:
        score_i = int(node.get("x_opencti_score") or 0)
    except (TypeError, ValueError):
        score_i = 0
    if score_i < min_score:
        return None

    valid_until = node.get("valid_until")
    if is_expired(valid_until):
        return None

    markings = [
        (n or {}).get("node", {}).get("definition")
        for n in ((node.get("objectMarking") or {}).get("edges") or [])
    ]
    labels = [
        (n or {}).get("node", {}).get("value")
        for n in ((node.get("objectLabel") or {}).get("edges") or [])
    ]
    phases = [
        (n or {}).get("node", {}).get("phase_name")
        for n in ((node.get("killChainPhases") or {}).get("edges") or [])
    ]

    return value.strip().lower(), {
        "score": score_i,
        "kind": kind,
        "opencti_id": node.get("id"),
        "indicator_name": node.get("name"),
        "confidence": node.get("confidence"),
        "detection": bool(node.get("x_opencti_detection")),
        "valid_from": node.get("valid_from"),
        "valid_until": valid_until,
        "kill_chain_phases": [p for p in phases if p],
        "created_by": ((node.get("createdBy") or {}) or {}).get("name"),
        "created_at": node.get("created_at"),
        "updated_at": node.get("updated_at"),
        "markings": [m for m in markings if m],
        "labels": [lb for lb in labels if lb],
        "source": "opencti",
    }


async def _resolve_token(ctx: EnrichContext) -> Optional[str]:
    """Resolve a credencial no cofre. 1×/carga, JAMAIS por evento.

    **A referência vem SÓ do servidor** (``ctx.secret_ref``, preenchido a partir da
    ``EnrichmentSource`` escopada à org). Antes havia um campo ``*_secret_ref`` na
    config, e a config é escrita por um admin de organização via API — como
    ``core.secrets`` decifra qualquer ciphertext sem noção de org
    (``backend.decrypt(ciphertext)``), aceitar a referência pela config deixaria
    colar o blob da Org B e USAR a credencial dela. O campo foi removido do schema
    e ``_validate_source_config`` recusa qualquer chave com "secret".
    """
    ref = ctx.secret_ref
    if not ref:
        return None
    try:
        from ....core import secrets as secrets_mod

        backend = secrets_mod.get_default_backend()
        # O backend é síncrono; ``to_thread`` para não bloquear o event loop do ciclo.
        return await asyncio.to_thread(backend.decrypt, ref)
    except Exception as exc:  # noqa: BLE001
        raise PermissionError(f"não foi possível resolver o segredo {ref!r}: {exc}") from exc


register(
    EnricherRegistration(
        name="opencti",
        factory=lambda cfg: OpenCTIEnricher(cfg),
        caps=_CAPS,
        config_schema=OpenCTIConfig,
        required_secrets=("api_token",),
        label="OpenCTI",
        category="Threat Intel",
        description=(
            "Indicadores da sua instância OpenCTI, sincronizados como tabela local. "
            "Roda no caminho por evento, então alimenta as regras de detecção em "
            "pipeline. Nenhum dado sai da sua infraestrutura."
        ),
        icon_id="opencti",
        docs_url="https://docs.opencti.io/latest/deployment/integrations/",
        tier="beta",
        order=10,
        output_fields={
            "score": "Score do OpenCTI (0-100)",
            "entity_type": "Tipo do observável (IPv4-Addr, Domain-Name, StixFile, ...)",
            "kind": "Tipo de chave normalizado (ip, domain, url, file_hash, mac)",
            "opencti_id": "Id interno do observável no OpenCTI",
            "created_at": "Criação do observável",
            "updated_at": "Última atualização",
            "markings": "Marcações TLP/PAP",
            "labels": "Rótulos atribuídos no OpenCTI",
            "created_by": "Quem reportou (feed ou analista)",
            "source": "Constante 'opencti', proveniência",
            "indicator_name": "Nome do indicador (preset indicators)",
            "confidence": "Confiança 0-100 (preset indicators)",
            "detection": "Marcado como acionável para detecção (preset indicators)",
            "valid_from": "Início da validade (preset indicators)",
            "valid_until": "Fim da validade (preset indicators)",
            "kill_chain_phases": "Fases da kill chain, ex.: command-and-control (preset indicators)",
        },
    )
)
