"""DashboardSummary v2 — KPIs e buckets auto-descritos.

Payload ÚNICO do dashboard (o shape v1 via ``Accept:
application/vnd.centralops.v1+json`` foi REMOVIDO junto com a superfície de
alertas Wazuh-only). Além dos KPIs/buckets data-driven, o envelope carrega as
contagens de organizações/integrações e os itens degradados que o frontend
consome no ScopeSummary e na seção "Sources & Health".
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Literal, Optional

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


class DashboardMetricComparison(BaseModel):
    """Comparação janela atual × janela anterior de uma métrica."""

    current: int = 0
    previous: int = 0
    delta: int = 0
    trend: Literal["up", "down", "stable"] = "stable"


class DashboardIntegrationComparison(BaseModel):
    degraded_integrations: DashboardMetricComparison = Field(
        default_factory=DashboardMetricComparison
    )


class DashboardIntegrationIssue(BaseModel):
    """Integração degradada/errada exibida na seção "Sources & Health"."""

    integration_id: int
    integration_name: str
    organization_id: int
    organization_name: Optional[str] = None
    status: str
    last_error: Optional[str] = None
    last_checked_at: Optional[datetime] = None


class DashboardIntegrationHealthCounts(BaseModel):
    healthy: int = 0
    degraded: int = 0
    error: int = 0
    unknown: int = 0
    inactive: int = 0


class DashboardOrganizationsSummary(BaseModel):
    total: int = 0
    active: int = 0


class DashboardIntegrationsSummary(BaseModel):
    total: int = 0
    active: int = 0
    authenticated: int = 0
    by_platform: Dict[str, int] = Field(default_factory=dict)
    health: DashboardIntegrationHealthCounts = Field(
        default_factory=DashboardIntegrationHealthCounts
    )
    degraded_items: List[DashboardIntegrationIssue] = Field(default_factory=list)
    comparison: DashboardIntegrationComparison = Field(
        default_factory=DashboardIntegrationComparison
    )


class DashboardSummaryV2(BaseModel):
    """Response envelope v2 para GET /dashboard/summary.

    ``window`` é derivado do parâmetro ``days`` (1-6 → "24h", 7-29 → "7d",
    30+ → "30d"). ``generated_at`` permite cache invalidation no cliente.
    ``kpis`` e ``top_buckets`` são iterados pelo frontend — sem ``if field``
    no componente. ``organizations``/``integrations`` são as contagens de
    escopo + saúde que o frontend usa no ScopeSummary e em "Sources & Health"
    (aditivos — absorvem os campos não-alerts do antigo shape v1).
    """

    schema_version: Literal[2] = 2
    window: Literal["24h", "7d", "30d"]
    generated_at: datetime
    kpis: List[KpiCard]
    top_buckets: List[BucketSection]
    organizations: DashboardOrganizationsSummary = Field(
        default_factory=DashboardOrganizationsSummary
    )
    integrations: DashboardIntegrationsSummary = Field(
        default_factory=DashboardIntegrationsSummary
    )
