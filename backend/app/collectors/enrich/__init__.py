"""Enriquecimento em stream (ADR-LOCAL-0002).

Acrescenta contexto ao evento **dentro do pipeline**, antes do fan-out e antes da
classificação em voo — geo/ASN, reputação, inventário do cliente, threat intel — de
modo que a detecção, o roteamento e o destino recebam o evento já contextualizado.

A divisão dos módulos É o contrato deste pacote, e existe por uma restrição medida,
não por gosto arquitetural:

``applier``
    PURO. Sem I/O, sem estado, sem log, sem métrica, sem ``await``. É o ÚNICO código
    que roda por evento. Guard estrutural em CI reprova import de
    redis/httpx/sqlalchemy aqui, exatamente como em ``inflight/matcher.py``.

``runtime``
    Tudo que toca o mundo: carga de tabela 1×/ciclo, resolução em bulk no flush do
    lote, cache, orçamento, métricas.

``contract``
    Protocolos e capabilities. Nenhum import pesado — é lido pelo applier.

``dsl``
    Compilador da política. Rejeita chave desconhecida (**não** repete o fail-open
    silencioso de ``normalize/engine._compile_single_rule``, que ignora campo
    desconhecido sem erro).

``registry``
    Catálogo self-registering, simétrico a ``output/destinations/registry.py`` e a
    ``collectors/registry.py``: adicionar um enricher toca UM lugar.

**Por que `resolve` é separado de `apply`.** O laço de coleta é um ``async for``
estritamente serial (``pipeline.py:1018``) — sem ``gather``, sem semáforo. Um lookup
de rede por evento serializa a vazão inteira; o repo já pagou por isso uma vez
(``record_in`` fazia 4 pipelines Redis síncronos por evento, ~0,8 ms/evento, ~8 s
bloqueados num ciclo de 10k — ver ``reduction/metering.InVolumeAccumulator``). Então:
o estado é resolvido **em bulk, fora do laço**, e o que roda por evento apenas LÊ um
mapa já pronto. É o mesmo modelo de *context* do Tenzir e de ``enrichment_tables`` do
Vector.

**Onde o resultado é escrito.** Exclusivamente sob ``_centralops.enrichment.*`` e
``_centralops.enrichment_tags``. Nunca em ``normalized.*``: aquele objeto já passou
pelo gate OCSF em ``pipeline.py:1255``, e escrever depois faria
``_centralops.ocsf_valid`` descrever um payload que não é o entregue.

**Consequência dura dessa escolha, que é regra e não recomendação.**
``routing/pii_redaction.ALLOWED_ROOTS`` é ``{"raw", "normalized"}`` — ``_centralops``
é blindado contra redação. Logo a saída do enriquecimento é ESTRUTURALMENTE
irredigível, e um enricher que emita PII é recusado no registro
(``EnricherCapabilities.emits_pii``). Não existe configuração de rota que torne isso
seguro.
"""

from __future__ import annotations

from .contract import (
    EnrichContext,
    EnricherCapabilities,
    EnricherRegistration,
    KEY_KINDS,
    LookupTable,
    PiiEnricherRefused,
    Resolution,
)
from .registry import all_registrations, describe_all, get, register, registered_names

__all__ = [
    "EnrichContext",
    "EnricherCapabilities",
    "EnricherRegistration",
    "KEY_KINDS",
    "LookupTable",
    "PiiEnricherRefused",
    "Resolution",
    "all_registrations",
    "describe_all",
    "get",
    "register",
    "registered_names",
]
