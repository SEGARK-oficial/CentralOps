"""Kind ``jsonl`` — destino de arquivo NDJSON local.

Subconjunto do ``wazuh_syslog`` (modo jsonl), exposto como ``kind``
standalone: útil como sink de baixo custo / buffer durável independente
de um host Wazuh. Wire byte-idêntico ao ``JSONLWriter`` atual.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from pydantic import BaseModel, Field

from ..base import Destination, LegacyTargetDestination, TestResult
from ..formatters import format_jsonl
from ..jsonl_writer import JSONLWriter
from .registry import DestinationConfig, DestinationRegistration, register

KIND = "jsonl"
_DEFAULT_JSONL_DIR = "/var/log/centralops/collectors"

# FONTE ÚNICA do wire JSONL: o ``Destination.format()`` e o
# ``JSONLWriter.send_batch`` consomem a MESMA ``format_jsonl``. Mantemos o nome
# histórico ``_jsonl_format`` como alias (re-export) para o wire-contract test
# existente — é o MESMO objeto, não uma segunda implementação.
_jsonl_format = format_jsonl


class JsonlConfig(BaseModel):
    """Schema de config do destino JSONL local."""

    jsonl_dir: str = Field(default=_DEFAULT_JSONL_DIR)


def _factory(config: DestinationConfig, secrets: Optional[Any] = None) -> Destination:
    cfg = JsonlConfig(**dict(config.config or {}))

    async def _probe() -> TestResult:
        try:
            os.makedirs(cfg.jsonl_dir, exist_ok=True)
            if not os.access(cfg.jsonl_dir, os.W_OK):
                return TestResult.failed(f"diretório não gravável: {cfg.jsonl_dir}")
            return TestResult.passed(f"diretório gravável: {cfg.jsonl_dir}")
        except OSError as exc:
            return TestResult.failed(f"erro ao acessar diretório: {exc}")

    return LegacyTargetDestination(
        KIND,
        JSONLWriter(cfg.jsonl_dir or _DEFAULT_JSONL_DIR),
        formatter=_jsonl_format,
        probe=_probe,
    )


register(
    DestinationRegistration(
        kind=KIND,
        factory=_factory,
        config_schema=JsonlConfig,
        default_queue="dispatch.jsonl",
        capabilities=frozenset({"batch", "persistent_queue", "test"}),
        required_secrets=(),
        label="Arquivo JSONL (local)",
        # Writer de arquivo único — 1 por destino evita linhas interleavadas.
        delivery_defaults={"concurrency": 1},
        # Campos de catálogo self-describing (galeria de destinos).
        category="Arquivo",
        icon_id="jsonl",
        tier="stable",
        order=130,
        description="JSON Lines em arquivo local — debug, auditoria e arquivamento.",
    )
)
