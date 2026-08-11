"""Enrichers registrados (ADR-LOCAL-0002).

Este módulo existe SÓ para disparar o side-effect de registro — mesmo contrato de
``output/destinations/__init__.py`` e ``collectors/vendors/__init__.py``. Adicionar
uma fonte de enriquecimento = criar o módulo e acrescentar uma linha aqui. Nenhuma
edição em pipeline, beat, API ou frontend.

O import é tolerante a falha por enricher: uma dependência opcional ausente derruba
UM enricher, não o catálogo inteiro nem o boot do worker.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_MODULES = ("customer_table", "opencti", "taxii", "virustotal")

for _name in _MODULES:
    try:
        __import__(f"{__name__}.{_name}")
    except Exception:  # noqa: BLE001 — um enricher quebrado não derruba o worker
        logger.warning(
            "enricher %r não pôde ser registrado", _name,
            extra={"event": "enrich.registry.import_failed", "enricher": _name},
            exc_info=True,
        )

del _name
