"""Registry self-registering de **enrichers** (ADR-LOCAL-0002).

Espelha ponto-a-ponto ``output/destinations/registry.py`` (destinos) e
``collectors/registry.py`` (fontes). O contrato é o mesmo: **adicionar uma fonte de
enriquecimento toca em 1 lugar** — um módulo em ``enrich/enrichers/<nome>.py`` chama
``register()`` no import, e o ``__init__`` do pacote só importa para disparar o
side-effect. Nenhuma edição em pipeline, beat, API ou frontend.

Duas recusas acontecem **aqui**, no registro, e não em runtime — porque uma falha de
configuração descoberta no hot path já é dado errado entregue ao cliente:

1. ``emits_pii=True`` ⇒ :class:`PiiEnricherRefused`. A saída do enriquecimento vive
   sob ``_centralops``, que ``routing/pii_redaction.ALLOWED_ROOTS`` blinda contra
   redação. Não há rota que torne isso seguro.
2. base de terceiro não-redistribuível marcada como embarcada ⇒ erro. GeoLite2 exige
   license key individual e as bases CC BY-SA conflitam com um artefato EE
   proprietário; "assar na imagem" é redistribuição, e a imagem vai para o GHCR.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from .contract import (
    EnricherRegistration,
    EnrichmentConfigError,
    PiiEnricherRefused,
)

logger = logging.getLogger(__name__)

_REGISTRY: Dict[str, EnricherRegistration] = {}


def register(reg: EnricherRegistration, *, embedded_dataset: bool = False) -> None:
    """Registra um enricher. Idempotente por nome; re-registro SOBRESCREVE com log.

    ``embedded_dataset=True`` declara que a base de dados do enricher é embarcada na
    imagem. Combinado com ``caps.redistributable=False``, é erro de registro — o
    ponto de detecção precisa ser o import, que roda no CI, e não o deploy.
    """
    if not reg.name or not reg.name.strip():
        raise EnrichmentConfigError("EnricherRegistration.name é obrigatório")

    if reg.caps.emits_pii:
        raise PiiEnricherRefused(
            f"enricher {reg.name!r} declara emits_pii=True e foi RECUSADO. "
            "A saída do enriquecimento é escrita sob `_centralops`, que "
            "`routing/pii_redaction.ALLOWED_ROOTS` blinda contra redação — o campo "
            "sairia em claro inclusive nas rotas configuradas para redigir PII. "
            "Remova os campos pessoais dos outputs ou aguarde o suporte a "
            "`target: normalized.*` (exige revalidação OCSF no mesmo PR)."
        )

    if embedded_dataset and not reg.caps.redistributable:
        raise EnrichmentConfigError(
            f"enricher {reg.name!r} embarca uma base marcada como NÃO "
            f"redistribuível (license={reg.caps.license!r}). As imagens são "
            "publicadas; embarcar é redistribuir. Baixe a base em runtime com "
            "license key por instalação."
        )

    if reg.name in _REGISTRY:
        logger.warning(
            "enricher %r re-registrado — sobrescrevendo o anterior", reg.name,
            extra={"event": "enrich.registry.overwrite", "enricher": reg.name},
        )
    _REGISTRY[reg.name] = reg


def get(name: str) -> Optional[EnricherRegistration]:
    """Registro do enricher, ou ``None``. O caller decide se 404 ou 422."""
    return _REGISTRY.get(name)


def require(name: str) -> EnricherRegistration:
    """Como :func:`get`, mas levanta com a lista do que existe.

    A mensagem carrega os nomes válidos de propósito: o consumidor primário deste
    erro é um agente de IA editando política via MCP, e "enricher desconhecido" sem
    o catálogo obriga uma segunda chamada só para descobrir o vocabulário.
    """
    reg = _REGISTRY.get(name)
    if reg is None:
        raise EnrichmentConfigError(
            f"enricher desconhecido: {name!r}. Registrados: {sorted(_REGISTRY)}"
        )
    return reg


def registered_names() -> List[str]:
    return sorted(_REGISTRY)


def all_registrations() -> List[EnricherRegistration]:
    """Ordenado por (order, label) — a ordem que a galeria da UI exibe."""
    return sorted(_REGISTRY.values(), key=lambda r: (r.order, r.label or r.name))


def describe_all() -> List[dict]:
    """Catálogo serializável para ``GET /collectors/enrichment/enrichers``."""
    return [r.describe() for r in all_registrations()]


def _reset_for_tests() -> None:
    """Limpa o registry. SÓ para testes — nunca chamado em runtime."""
    _REGISTRY.clear()
