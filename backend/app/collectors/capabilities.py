"""Vocabulário canônico de capability-keys.

O capability model do plugin de integração usa string-keys em DOIS lugares:

- **catálogo** estático — ``PlatformRegistration.capabilities`` (descoberta/UI);
- **runtime** — ``BaseProvider.capabilities()`` (gating por kind no core).

Os dois vocabulários podiam divergir (typos / keys sem dono / formas
duplicadas) sem que nada validasse o conjunto. Este módulo é a FONTE ÚNICA das
keys válidas. Convenção: ``verb:noun`` em snake_case;
dois namespaces têm sufixo DINÂMICO — ``collect:<stream>`` e ``query:<dialect>``.

Tanto o catálogo quanto o runtime DEVEM ser subconjuntos deste vocabulário
(``register_platform`` avisa em key inválida; ``tests/test_capability_vocabulary``
trava no PR). Não são conjuntos iguais — o catálogo (o que a plataforma oferece)
⊇ runtime (o que ESTA integração/kind pode) — mas ambos saem do MESMO vocabulário.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Iterable, List, Optional, Tuple

# Keys EXATAS válidas (não-namespaced ou de namespace fechado).
EXACT_CAPABILITIES: frozenset = frozenset(
    {
        # marcadores de catálogo (só aparecem na registration, não no runtime)
        "catalog",
        "health",
        # auth / health de runtime
        "auth:test",
        "health:check",
        # descoberta MSSP (parent) — gating de children_count/bulk/delete/backfill
        "discover:children",
        "partner:sync_tenants",
        # NB: ``investigations:run`` (query síncrona legada do provider) foi REMOVIDA
        # — a query agora é o namespace dinâmico ``query:<dialect>``
        # (ver DYNAMIC_NAMESPACES + QueryCapability). Nenhum provider a emite mais.
        # NB: ``alerts:list``/``alerts:detail``/``alerts:search`` (superfície de
        # visualização de alertas Wazuh-only) foram REMOVIDAS — a busca federada
        # (``query:<dialect>``) + detections cobrem o caso vendor-neutro.
        # ``collect:alerts`` (ingestão) NÃO é afetada (namespace dinâmico).
        # preview de licença (Sophos child tenant)
        "licensing:list",
    }
)

# Namespaces cujo sufixo é DINÂMICO: ``collect:<stream>`` (alerts/cases/detections/
# incidents/activities/…) e ``query:<dialect>`` (opensearch_dsl/kql/fql/…). Novos
# vendors estendem por aqui, sem editar a lista exata.
DYNAMIC_NAMESPACES: frozenset = frozenset({"collect", "query"})

# ── Constantes nomeadas dos gates de runtime ─────
# Use ESTAS no core em vez de literais crus. Um typo numa constante vira
# NameError no import (falha cedo) em vez de um literal que NUNCA casa →
# guard pulado silenciosamente (fail-OPEN, ex.: bulk-delete sem proteção).
CAP_DISCOVER_CHILDREN = "discover:children"
CAP_LICENSING_LIST = "licensing:list"

# ── Dialetos de query canônicos ────────────────────────────
# Sufixo do namespace dinâmico ``query:<dialect>``. Cada vendor que suporta query
# declara EXATAMENTE um destes (passthrough do dialeto nativo:
# "TRADUTOR-POR-VENDOR + OCSF no resultado"). Novos dialetos entram aqui.
DIALECT_OPENSEARCH_DSL = "opensearch_dsl"   # Wazuh/Elastic (Indexer)
DIALECT_XDR_DATA_LAKE = "xdr_data_lake"     # Sophos XDR Query (Data Lake, async)
DIALECT_OSQUERY = "osquery"                 # Sophos Live Discover (live)
DIALECT_FQL = "fql"                         # CrowdStrike Falcon Query Language
DIALECT_CQL = "cql"                         # CrowdStrike LogScale / NG-SIEM
DIALECT_KQL = "kql"                         # Microsoft Defender (runHuntingQuery)
DIALECT_LAKE_FILTER = "lake_filter"         # Search-in-place no lake S3

CAP_QUERY_OPENSEARCH_DSL = f"query:{DIALECT_OPENSEARCH_DSL}"
CAP_QUERY_XDR_DATA_LAKE = f"query:{DIALECT_XDR_DATA_LAKE}"
CAP_QUERY_FQL = f"query:{DIALECT_FQL}"            # CrowdStrike Falcon
CAP_QUERY_CQL = f"query:{DIALECT_CQL}"            # CrowdStrike LogScale (futuro)
CAP_QUERY_KQL = f"query:{DIALECT_KQL}"            # Microsoft Defender
CAP_QUERY_LAKE_FILTER = f"query:{DIALECT_LAKE_FILTER}"  # lake search-in-place

# Modos de execução de uma query capability.
QUERY_MODE_LIVE = "live"            # síncrono/curto contra a fonte ao vivo
QUERY_MODE_DATA_LAKE = "data_lake"  # assíncrono (submete → poll → fetch)
QUERY_MODE_PASSTHROUGH = "passthrough"

# spec_kinds: forma do statement que o ``execute()`` aceita. ``passthrough`` = o
# analista manda o dialeto nativo; ``sigma``/``ocsf_queryspec`` = camada abstrata
# traduzida (coexiste com passthrough sem migração forçada).
SPEC_PASSTHROUGH = "passthrough"
SPEC_SIGMA = "sigma"
SPEC_OCSF_QUERYSPEC = "ocsf_queryspec"


def query_capability_key(dialect: str) -> str:
    """Capability-key canônica para um dialeto (``opensearch_dsl`` →
    ``query:opensearch_dsl``). Fonte única — evita literais divergentes."""
    return f"query:{dialect}"


@dataclass(frozen=True)
class QueryCapability:
    """Contrato declarado de uma capability de QUERY de um vendor.

    É o metadado ESTRUTURADO por trás da capability-key ``query:<dialect>`` (que
    gateia descoberta/autorização). Declarado pelo módulo de catálogo do vendor
    (``PlatformRegistration.query_capabilities``), espelhando como os destinos
    declaram ``DestinationRegistration``.

    **Invariante de execução canônica.** Este objeto NÃO
    duplica ``run_query`` — ele APONTA para o ponto de execução canônico do
    ``BaseProvider`` (``run_query`` / ``run_query_async``, assinatura
    ``(statement, from_ts, to_ts, **kwargs) -> QueryResult``). Proibido um 2º
    caminho de execução.

    Os limites (``max_window`` / ``rate_limit``) são ENFORCED no ``QueryService``
    central, não decorativos — ``passthrough`` nunca manda
    statement sem teto de janela/linhas (fecha o achado de poison-query)."""

    dialect: str
    # ("live",) | ("live","data_lake") | ("passthrough",) — ver QUERY_MODE_*.
    modes: Tuple[str, ...] = (QUERY_MODE_LIVE,)
    supports_async: bool = False
    # Teto de janela por query (ex.: Sophos Data Lake 30d). None ⇒ sem teto declarado.
    max_window: Optional[timedelta] = None
    # Limite de taxa central por (org, vendor), ex.: "45/min/tenant" (Defender). None
    # ⇒ sem limite declarado. ENFORCED no QueryService, não no execute.
    rate_limit: Optional[str] = None
    required_secrets: Tuple[str, ...] = ()
    # Versão do mapping OCSF de normalização do resultado (anti-drift).
    ocsf_mapping_version: str = "1"
    # Formas de statement aceitas — passthrough (default) e/ou abstrato.
    spec_kinds: Tuple[str, ...] = (SPEC_PASSTHROUGH,)

    def capability_key(self) -> str:
        """A capability-key ``query:<dialect>`` desta capability."""
        return query_capability_key(self.dialect)


_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def is_valid_capability(key: str) -> bool:
    """True se ``key`` pertence ao vocabulário canônico."""
    if not isinstance(key, str) or not key:
        return False
    if key in EXACT_CAPABILITIES:
        return True
    namespace, sep, suffix = key.partition(":")
    if not sep:
        return False  # key não-namespaced fora do conjunto exato
    return namespace in DYNAMIC_NAMESPACES and bool(_SLUG_RE.match(suffix))


def invalid_capabilities(keys: Iterable[str]) -> List[str]:
    """Subconjunto de ``keys`` que NÃO está no vocabulário (ordenado, p/ erro claro)."""
    return sorted({k for k in keys if not is_valid_capability(k)})


def validate_capability(key: str) -> str:
    """Devolve ``key`` se válida, senão levanta ``ValueError``.

    Usado nos GATES de runtime: um literal com typo (``discover:childrenn``)
    levanta aqui em vez de virar um ``in`` que nunca casa (fail-open silencioso)."""
    if not is_valid_capability(key):
        raise ValueError(
            f"capability desconhecida fora do vocabulário canônico: {key!r}"
        )
    return key
