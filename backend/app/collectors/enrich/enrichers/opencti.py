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
from typing import Any, Dict, Mapping, Optional

import aiohttp
from pydantic import BaseModel, Field

from ..contract import EnrichContext, EnricherCapabilities, EnricherRegistration
from ..registry import register
from ..runtime import DictLookupTable

logger = logging.getLogger(__name__)

#: Query default. ``first``/``after`` é a paginação por cursor do OpenCTI (Relay).
#: Pede o mínimo necessário: buscar campos que não viram output é banda e memória
#: gastas em toda atualização de tabela.
_DEFAULT_QUERY = """
query CentralOpsObservables($first: Int!, $after: ID) {
  stixCyberObservables(first: $first, after: $after, orderBy: created_at, orderMode: desc) {
    edges {
      node {
        id
        entity_type
        observable_value
        x_opencti_score
        created_at
        updated_at
        objectMarking { edges { node { definition } } }
        objectLabel { edges { node { value } } }
      }
    }
    pageInfo { endCursor hasNextPage }
  }
}
"""

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
    #: Referência ao cofre. NUNCA o token em claro — o runtime resolve 1×/ciclo.
    token_secret_ref: Optional[str] = Field(
        None, description="Referência do token de API no cofre de segredos"
    )
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
        token = await _resolve_token(self._cfg.token_secret_ref, ctx)
        rows: Dict[str, Dict[str, Any]] = {}
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        query = self._cfg.query or _DEFAULT_QUERY
        endpoint = self._cfg.url.rstrip("/") + "/graphql"
        timeout = aiohttp.ClientTimeout(total=self._cfg.timeout_s)
        connector = aiohttp.TCPConnector(ssl=None if self._cfg.verify_tls else False)

        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            cursor: Optional[str] = None
            for page in range(self._cfg.max_pages):
                payload = {
                    "query": query,
                    "variables": {"first": self._cfg.page_size, "after": cursor},
                }
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

                container = (body.get("data") or {}).get("stixCyberObservables") or {}
                edges = container.get("edges") or []
                for edge in edges:
                    node = (edge or {}).get("node") or {}
                    parsed = _parse_node(node, self._cfg.min_score)
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
        "markings": [m for m in markings if m],
        "labels": [lb for lb in labels if lb],
        "source": "opencti",
    }


async def _resolve_token(secret_ref: Optional[str], ctx: EnrichContext) -> Optional[str]:
    """Resolve o token no cofre. 1×/carga, JAMAIS por evento."""
    ref = secret_ref or ctx.secret_ref
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
            "source": "Constante 'opencti' — proveniência",
        },
    )
)
