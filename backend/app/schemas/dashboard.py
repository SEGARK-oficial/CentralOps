"""DashboardSummary v2 — KPIs e buckets auto-descritos.

O shape v1 (DashboardSummaryRead em api/schemas.py) continua
servido quando o cliente envia Accept: application/vnd.centralops.v1+json.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class KpiCard(BaseModel):
    """Cartão de KPI para o topo do Dashboard.

    ``id`` é estável e usado como chave de render no frontend.
    ``trend`` e ``trend_value`` são opcionais — só incluir quando o dado
    de comparação estiver disponível. ``severity`` controla a cor do valor.
    """

    id: str
    label: str
    value: str | int | float
    sub: Optional[str] = None
    icon_id: Optional[str] = None
    trend: Optional[Literal["up", "down", "flat"]] = None
    trend_value: Optional[str] = None
    severity: Optional[Literal["ok", "warn", "critical", "info"]] = None


class BucketItem(BaseModel):
    """Item dentro de um BucketSection.

    ``id`` é a chave estável (hostname, rule_id, mitre_id, etc.).
    ``href`` é um deep-link relativo — se presente, o item é clicável.
    """

    id: str
    label: str
    value: int | float
    sub: Optional[str] = None
    severity: Optional[Literal["ok", "warn", "critical", "info"]] = None
    href: Optional[str] = None


class BucketSection(BaseModel):
    """Seção de bucket para rankings (top hosts, top rules, etc.).

    ``empty_hint`` é exibido quando ``items`` está vazio.
    ``icon_id`` é o ícone do header da seção.
    """

    id: str
    label: str
    items: List[BucketItem]
    icon_id: Optional[str] = None
    empty_hint: Optional[str] = None


class DashboardSummaryV2(BaseModel):
    """Response envelope v2 para GET /dashboard/summary.

    ``window`` é derivado do parâmetro ``days`` (1-6 → "24h", 7-29 → "7d",
    30+ → "30d"). ``generated_at`` permite cache invalidation no cliente.
    ``kpis`` e ``top_buckets`` são iterados pelo frontend — sem ``if field``
    no componente.
    """

    schema_version: Literal[2] = 2
    window: Literal["24h", "7d", "30d"]
    generated_at: datetime
    kpis: List[KpiCard]
    top_buckets: List[BucketSection]
