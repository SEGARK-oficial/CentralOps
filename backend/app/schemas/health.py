"""HealthSchema v2 — métricas data-driven por provider.

O shape v1 (IntegrationHealthRead em api/schemas.py) continua
servido quando o cliente envia Accept: application/vnd.centralops.v1+json.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class HealthMetric(BaseModel):
    """Unidade atômica de saúde de um provider.

    ``id`` é estável (snake_case, único por provider) e pode ser usado como
    chave de lookup no frontend. ``severity`` controla a cor do indicador.
    ``group`` permite agrupar métricas relacionadas (ex: "cluster", "agents").
    """

    id: str
    label: str
    value: str | int | float | bool
    unit: Optional[str] = None
    severity: Literal["ok", "warn", "critical", "unknown"] = "unknown"
    icon_id: Optional[str] = None
    hint: Optional[str] = None
    group: Optional[str] = None


class HealthResponse(BaseModel):
    """Response envelope v2 para GET /integrations/{id}/health.

    ``last_collection_at`` é o max(last_attempt_at) entre todos os streams
    da integração; ``last_success_at`` é o max(last_success_at).
    Presentes mesmo quando ``metrics`` está vazio — a UI usa esses campos
    para renderizar "Última coleta há Xm" sem depender das métricas.
    """

    schema_version: Literal[2] = 2
    platform: str
    last_collection_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    metrics: List[HealthMetric] = Field(default_factory=list)
