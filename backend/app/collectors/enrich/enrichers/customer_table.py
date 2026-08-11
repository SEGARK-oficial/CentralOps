"""Enrichers de **tabela do cliente** — ``table_exact`` e ``table_cidr``.

São as fontes mais baratas de valor do catálogo (ADR-LOCAL-0002 §7, Fase 2): não
dependem de nenhum terceiro, não têm rate limit, não têm licença, não fazem egresso —
e resolvem o que o cliente mais pede, que é ver o próprio contexto no evento:
criticidade do ativo, site, zona de rede, allowlist de scanner autorizado, watchlist
de desligamento.

``table_exact``
    Igualdade de chave. Dict — O(1).

``table_cidr``
    **Longest-prefix** por radix tree (:mod:`..radix`). Com ``10.0.0.0/16`` → matriz
    e ``10.0.5.0/24`` → filial, sondar ``10.0.5.7`` devolve a FILIAL. Nem Vector nem
    Cribl fazem isso nativamente; é o diferencial mais concreto do catálogo.

**Escopo por organização é estrutural, não configurável.** A tabela é resolvida por
``(organization_id, name)`` — nunca por nome só. É a diferença que fez o ADR descartar
estender a DSL de mapping: ``MappingDefinition`` não tem ``organization_id``, e um CMDB
de cliente numa tabela global vazaria entre tenants.

**Versionamento e ponto-no-tempo.** A carga resolve pelo PONTEIRO
``current_version_id`` — nunca por "maior version_number", porque rollback re-aponta
para uma versão ANTIGA. Com ``ctx.as_of`` preenchido (reprocessamento/backfill),
resolve a versão vigente NAQUELA data: enriquecer eventos de maio com o inventário de
agosto produz atribuição errada, e é o erro que o Tenzir resolve com *time-travel*.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Mapping, Optional

from pydantic import BaseModel, Field

from ..contract import EnrichContext, EnricherCapabilities, EnricherRegistration
from ..radix import RadixTree
from ..registry import register

logger = logging.getLogger(__name__)


class CustomerTableConfig(BaseModel):
    """Sem config: tudo o que o enricher precisa vem da regra (``table``) e do ctx."""

    model_config = {"extra": "forbid"}


class _RadixLookupTable:
    """:class:`~..contract.LookupTable` sobre a radix tree. Pura."""

    __slots__ = ("_tree", "_invalid")

    def __init__(self, tree: RadixTree, invalid: int) -> None:
        self._tree = tree
        self._invalid = invalid

    def lookup(self, key: str) -> Optional[Mapping[str, Any]]:
        return self._tree.search(key)

    @property
    def entry_count(self) -> int:
        return self._tree.entry_count

    @property
    def approx_bytes(self) -> int:
        return self._tree.approx_bytes

    @property
    def invalid_rows(self) -> int:
        return self._invalid


def _load_rows(ctx: EnrichContext, table_name: str) -> Dict[str, Any]:
    """Carrega o conteúdo da versão vigente da tabela da ORG. Síncrono.

    Chamado 1×/ciclo, fora do laço de eventos (o ``load`` do enricher roda em
    ``await`` no runtime). Uma tabela ausente devolve ``{}`` e o runtime trata como
    "regra não respondida" — NÃO como miss, porque a distinção é o que impede um
    ``miss_rate`` de 100% de parecer problema de dado quando é problema de config.
    """
    from ....db import database, models

    with database.SessionLocal() as db:
        table = (
            db.query(models.EnrichmentTable)
            .filter(
                models.EnrichmentTable.organization_id == ctx.organization_id,
                models.EnrichmentTable.name == table_name,
            )
            .first()
        )
        if table is None:
            raise LookupError(
                f"tabela de enriquecimento {table_name!r} não existe na org "
                f"{ctx.organization_id}"
            )

        version_id = table.current_version_id
        if ctx.as_of is not None:
            # Ponto-no-tempo: a versão vigente NA DATA do evento. ``as_of`` é epoch
            # em ms (mesma unidade de ``timestamp_t`` do OCSF).
            from datetime import datetime, timezone

            moment = datetime.fromtimestamp(ctx.as_of / 1000.0, tz=timezone.utc).replace(
                tzinfo=None
            )
            historical = (
                db.query(models.EnrichmentTableVersion)
                .filter(
                    models.EnrichmentTableVersion.table_id == table.id,
                    models.EnrichmentTableVersion.created_at <= moment,
                )
                .order_by(models.EnrichmentTableVersion.created_at.desc())
                .first()
            )
            if historical is not None:
                version_id = historical.id
            else:
                # Evento anterior à PRIMEIRA versão da tabela. Cair na versão
                # corrente seria o erro que ``as_of`` existe para evitar; devolver
                # vazio é a resposta honesta ("não havia inventário nessa data").
                logger.info(
                    "enrich: tabela %r não tinha versão em as_of=%s — nada a "
                    "enriquecer neste evento",
                    table_name, ctx.as_of,
                    extra={"event": "enrich.table_before_first_version"},
                )
                return {}

        if not version_id:
            raise LookupError(
                f"tabela {table_name!r} (org {ctx.organization_id}) não tem versão "
                "vigente — publique uma versão antes de referenciá-la numa política"
            )
        version = (
            db.query(models.EnrichmentTableVersion)
            .filter(models.EnrichmentTableVersion.id == version_id)
            .first()
        )
        if version is None:
            raise LookupError(
                f"tabela {table_name!r}: current_version_id {version_id!r} aponta "
                "para uma versão inexistente"
            )
        raw = version.rows

    try:
        rows = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"tabela {table_name!r}: conteúdo não é JSON válido: {exc}") from exc
    if not isinstance(rows, dict):
        raise ValueError(
            f"tabela {table_name!r}: conteúdo deve ser objeto {{chave: {{campo: valor}}}}, "
            f"recebeu {type(rows).__name__}"
        )
    return rows


_EXACT_CAPS = EnricherCapabilities(
    # Todos os kinds: a tabela do cliente pode ser chaveada por qualquer coisa —
    # hostname, usuário, hash, CVE. É o enricher genérico do catálogo.
    key_kinds=frozenset({"ip", "domain", "url", "file_hash", "cve", "mac", "user", "container_id"}),
    mode="local",
    supports_bulk=True,
    p99_budget_ms=2_000.0,
    suggested_ttl_s=3600,
    suggested_negative_ttl_s=300,
    emits_pii=False,
    license="dado do cliente",
    redistributable=False,
    egress="none",
)

_CIDR_CAPS = EnricherCapabilities(
    key_kinds=frozenset({"ip"}),
    mode="local",
    supports_bulk=True,
    p99_budget_ms=2_000.0,
    suggested_ttl_s=3600,
    suggested_negative_ttl_s=300,
    emits_pii=False,
    license="dado do cliente",
    redistributable=False,
    egress="none",
)


class ExactTableEnricher:
    """Lookup por igualdade sobre a tabela versionada da org."""

    caps = _EXACT_CAPS

    def __init__(self, config: Mapping[str, Any]) -> None:
        CustomerTableConfig(**dict(config or {}))

    async def load(self, ctx: EnrichContext):
        import asyncio

        from ..runtime import DictLookupTable

        if not ctx.table:
            raise ValueError("table_exact exige o campo 'table' na regra da política")
        rows = await asyncio.to_thread(_load_rows, ctx, ctx.table)
        # Chaves normalizadas em minúsculas no CARREGAMENTO. As duas pontas têm de
        # concordar: a regra declara ``normalize: ["lower"]`` do lado do evento, e
        # divergir aqui produz miss de 100% sem erro nenhum.
        return DictLookupTable({str(k).strip().lower(): v for k, v in rows.items()})


class CidrTableEnricher:
    """Lookup por **longest-prefix** sobre a tabela versionada da org."""

    caps = _CIDR_CAPS

    def __init__(self, config: Mapping[str, Any]) -> None:
        CustomerTableConfig(**dict(config or {}))

    async def load(self, ctx: EnrichContext) -> _RadixLookupTable:
        import asyncio

        if not ctx.table:
            raise ValueError("table_cidr exige o campo 'table' na regra da política")
        rows = await asyncio.to_thread(_load_rows, ctx, ctx.table)
        tree, invalid = RadixTree.from_rows(rows)
        if invalid:
            # Linha inválida é contada, nunca silenciada: sem este log o operador
            # vê "meu CIDR não casa" e não tem como saber que a linha foi descartada.
            logger.warning(
                "enrich: tabela %r tem %d linha(s) com CIDR inválido — ignoradas "
                "(%d prefixo(s) carregado(s))",
                ctx.table, invalid, tree.entry_count,
                extra={"event": "enrich.table_invalid_rows", "table": ctx.table},
            )
        return _RadixLookupTable(tree, invalid)


_COMMON_OUTPUTS = {
    "*": "Qualquer campo presente no valor da linha da tabela (definido pelo cliente)",
}

register(
    EnricherRegistration(
        name="table_exact",
        factory=lambda cfg: ExactTableEnricher(cfg),
        caps=_EXACT_CAPS,
        config_schema=CustomerTableConfig,
        label="Tabela do cliente (chave exata)",
        category="Tabela do cliente",
        description=(
            "Sua própria tabela de contexto, versionada na UI e casada por "
            "igualdade de chave. Sem rede, sem terceiros, sem rate limit."
        ),
        icon_id="table",
        tier="beta",
        order=1,
        # Vazio de propósito: os campos são definidos pelo cliente na tabela, então o
        # compilador da DSL não pode validar `from` contra uma lista fechada.
        output_fields={},
    )
)

register(
    EnricherRegistration(
        name="table_cidr",
        factory=lambda cfg: CidrTableEnricher(cfg),
        caps=_CIDR_CAPS,
        config_schema=CustomerTableConfig,
        label="Tabela do cliente (CIDR, prefixo mais específico)",
        category="Tabela do cliente",
        description=(
            "Plano de endereçamento, inventário de rede ou lista de bloqueio em CIDR, "
            "casados pelo prefixo MAIS ESPECÍFICO. Com 10.0.0.0/16 e 10.0.5.0/24 na "
            "tabela, o IP 10.0.5.7 recebe o /24."
        ),
        icon_id="network",
        tier="beta",
        order=2,
        output_fields={},
    )
)
