"""Registro das ferramentas MCP do core.

Cada ferramenta nova é código Python neste repositório, revisado como o
resto — de propósito. O registro é uma função pura: os módulos de domínio
devolvem suas ``ToolSpec`` e este arquivo só junta e recusa nome duplicado.

Extensão (EE): ``register_extension`` aceita uma fábrica adicional que é
consultada em ``build_specs``; o overlay proprietário pode acrescentar
ferramentas sem tocar no core (mesmo seam dos routers em ``activate(app)``).
"""

from __future__ import annotations

from typing import Callable, Iterable

from .ack_cache import AckCache
from .tools import (
    backfill as backfill_tools,
    collectors as collectors_tools,
    dashboard as dashboard_tools,
    destinations as destinations_tools,
    detections as detections_tools,
    drift as drift_tools,
    integrations as integrations_tools,
    mapping as mapping_tools,
    pipeline_health as pipeline_health_tools,
    quarantine as quarantine_tools,
    queries as queries_tools,
    routes as routes_tools,
    sophos_licenses as sophos_licenses_tools,
)
from .tools._base import ToolSpec

SpecFactory = Callable[[], Iterable[ToolSpec]]

_EXTENSIONS: list[SpecFactory] = []


def register_extension(factory: SpecFactory) -> None:
    """Acrescenta uma fábrica de ferramentas (overlay EE). Idempotente."""
    if factory not in _EXTENSIONS:
        _EXTENSIONS.append(factory)


def build_specs(ack_cache: AckCache | None = None) -> dict[str, ToolSpec]:
    cache = ack_cache if ack_cache is not None else AckCache()
    specs: list[ToolSpec] = [
        *integrations_tools.specs(),
        *collectors_tools.specs(),
        *drift_tools.specs(),
        *quarantine_tools.specs(),
        *mapping_tools.specs(cache),
        *backfill_tools.specs(),
        *sophos_licenses_tools.specs(),
        *pipeline_health_tools.specs(),
        *destinations_tools.specs(),
        *routes_tools.specs(),
        *detections_tools.specs(),
        *dashboard_tools.specs(),
        *queries_tools.specs(),
    ]
    for factory in _EXTENSIONS:
        specs.extend(factory())

    by_name: dict[str, ToolSpec] = {}
    for spec in specs:
        if spec.name in by_name:
            raise RuntimeError(f"Duplicate MCP tool name: {spec.name}")
        by_name[spec.name] = spec
    return by_name


__all__ = ["ToolSpec", "build_specs", "register_extension"]
