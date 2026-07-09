"""Abstract base classes for security platform providers.

Each provider implements capabilities it supports. Not all providers
support all capabilities — callers check via ``capabilities``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ..db.models import Integration

if TYPE_CHECKING:
    from ..collectors.capabilities import QueryCapability
    from ..schemas.health import HealthMetric


@dataclass
class HealthResult:
    status: str  # "healthy" | "degraded" | "error" | "unknown"
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AlertSummary:
    alert_id: str
    title: str
    severity: str  # "critical" | "high" | "medium" | "low" | "info"
    platform: str
    timestamp: Optional[str] = None
    hostname: Optional[str] = None
    rule_id: Optional[str] = None
    rule_level: Optional[int] = None
    rule_groups: List[str] = field(default_factory=list)
    rule_firedtimes: Optional[int] = None
    mitre_ids: List[str] = field(default_factory=list)
    mitre_tactics: List[str] = field(default_factory=list)
    mitre_techniques: List[str] = field(default_factory=list)
    decoder_name: Optional[str] = None
    agent_id: Optional[str] = None
    agent_name: Optional[str] = None
    agent_ip: Optional[str] = None
    agent_group: Optional[str] = None
    agent_labels: Dict[str, Any] = field(default_factory=dict)
    manager_name: Optional[str] = None
    location: Optional[str] = None
    full_log: Optional[str] = None
    src_ip: Optional[str] = None
    dst_ip: Optional[str] = None
    src_user: Optional[str] = None
    dst_user: Optional[str] = None
    input_type: Optional[str] = None
    syscheck_path: Optional[str] = None
    data_fields: Dict[str, Any] = field(default_factory=dict)
    highlights: Dict[str, List[str]] = field(default_factory=dict)
    source_index: Optional[str] = None
    integration_id: Optional[int] = None
    integration_name: Optional[str] = None
    organization_id: Optional[int] = None
    organization_name: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AlertDetailSummary(AlertSummary):
    pass


@dataclass
class PaginatedAlertsResult:
    items: List[AlertSummary] = field(default_factory=list)
    total: int = 0
    limit: int = 0
    offset: int = 0
    has_more: bool = False


@dataclass
class QueryResult:
    items: List[Dict[str, Any]]
    total: int = 0
    status: str = "finished"
    run_id: Optional[str] = None


@dataclass
class FederatedSourceResult:
    """Resultado de UMA fonte dentro de uma query federada.

    ``status``: ``answered`` (ok) | ``partial`` (truncado/timeout parcial) |
    ``failed`` (erro) | ``skipped`` (sem capability / fora de janela / quota)."""

    integration_id: int
    status: str = "answered"
    count: int = 0
    error: Optional[str] = None
    partial: bool = False
    run_id: Optional[str] = None


@dataclass
class FederatedQueryResult:
    """Agregador tipado de "uma busca, N vendors".

    Estende a semente ``QueryResult`` para multi-fonte: itens agregados + status/
    erro/parcial POR fonte (``per_source[integration_id]``). ``allow_partial_results``
    é explícito (paridade ES|QL ``allow_partial_results``): quando ``True``, uma fonte
    com erro não derruba a busca inteira — vira um ``failed`` no ``per_source``."""

    items: List[Dict[str, Any]] = field(default_factory=list)
    total: int = 0
    per_source: Dict[int, FederatedSourceResult] = field(default_factory=dict)
    allow_partial_results: bool = False

    def add_source(self, result: FederatedSourceResult) -> None:
        """Registra (ou substitui) o resultado de uma fonte e re-soma o total."""
        self.per_source[result.integration_id] = result

    @property
    def sources_queried(self) -> int:
        return len(self.per_source)

    @property
    def sources_answered(self) -> int:
        return sum(
            1 for r in self.per_source.values() if r.status in ("answered", "partial")
        )

    @property
    def partial(self) -> bool:
        """True se alguma fonte falhou ou respondeu parcialmente (resultado incompleto)."""
        return any(
            r.partial or r.status in ("partial", "failed")
            for r in self.per_source.values()
        )


class BaseProvider(ABC):
    """Interface that all security platform providers must implement."""

    platform: str  # "sophos" | "wazuh"

    def __init__(self, integration: Integration) -> None:
        self.integration = integration

    @abstractmethod
    def capabilities(self) -> List[str]:
        """Return list of supported capability keys.

        Examples: "alerts:list", "alerts:search",
        "query:<dialect>", "health:check"
        """
        ...

    @abstractmethod
    def test_connection(self) -> HealthResult:
        """Validate credentials and connectivity."""
        ...

    @abstractmethod
    def health_check(self) -> HealthResult:
        """Return current health/status summary."""
        ...

    def list_alerts(self, **filters) -> PaginatedAlertsResult:
        raise NotImplementedError(f"{self.platform} does not support alerts:list")

    def search_alerts(self, query: str, **filters) -> PaginatedAlertsResult:
        raise NotImplementedError(f"{self.platform} does not support alerts:search")

    def get_alert(self, alert_id: str, **filters) -> Optional[AlertDetailSummary]:
        raise NotImplementedError(f"{self.platform} does not support alerts:detail")

    def get_alert_statistics(self, **filters) -> Dict[str, Any]:
        raise NotImplementedError(f"{self.platform} does not support alert statistics")

    # ── Query ──────────────────────────────────────────────────
    # ``run_query`` / ``run_query_async`` são o ÚNICO ponto de execução canônico de
    # query de um vendor. A ``QueryCapability`` declarada em
    # ``query_capability()`` APONTA para cá — não há (e é PROIBIDO) um 2º caminho.
    # Assinatura idêntica entre vendors: ``(statement, from_ts, to_ts, **kwargs)``.

    def query_capability(self) -> Optional["QueryCapability"]:
        """Capability de query aplicável a ESTA integração (``None`` ⇒ sem query).

        Default: a 1ª ``QueryCapability`` declarada no catálogo do vendor
        (``PlatformRegistration.query_capabilities``) — fonte única, evita drift
        catálogo↔runtime. Vendors com regra por kind (ex.: Sophos partner/org não
        roda query) sobrescrevem. NÃO é um 2º caminho de execução — só metadado de
        contrato (dialect/modes/max_window/rate_limit) que o ``QueryService`` usa
        para gatear e limitar."""
        from ..collectors.registry import get_platform

        reg = get_platform(self.integration.platform)
        caps = getattr(reg, "query_capabilities", ()) if reg is not None else ()
        return caps[0] if caps else None

    def _query_capability_keys(self) -> List[str]:
        """``["query:<dialect>"]`` se este integration suporta query, senão ``[]``.

        Deriva a capability-key de runtime da ``query_capability()`` — garante que o
        runtime (``capabilities()``) e o catálogo falem o MESMO ``query:<dialect>``
        (catálogo e runtime ⊆ mesmo vocabulário, sem divergência)."""
        qc = self.query_capability()
        return [qc.capability_key()] if qc is not None else []

    def run_query(self, statement: str, from_ts: str, to_ts: str, **kwargs) -> QueryResult:
        raise NotImplementedError(f"{self.platform} does not support query:<dialect>")

    async def run_query_async(self, statement: str, from_ts: str, to_ts: str, **kwargs) -> QueryResult:
        raise NotImplementedError(f"{self.platform} does not support async query:<dialect>")

    # ── Execução async worker-releasing ────────────────────
    # Para dialetos longos (ex.: Sophos XDR Data Lake 30d/~15min) o worker NÃO deve
    # bloquear em ``run_query``. O QueryService submete (``submit_query`` → run_id),
    # libera o worker e um poll-task curto chama ``poll_query`` até finalizar. Só
    # vendors com ``QueryCapability.supports_async=True`` implementam estes.

    def submit_query(self, statement: str, from_ts: str, to_ts: str, **kwargs) -> str:
        """Submete a query no vendor e devolve o ``run_id`` opaco (sem esperar).

        Levantado por padrão — só providers async (``supports_async``) sobrescrevem."""
        raise NotImplementedError(f"{self.platform} does not support async submit_query")

    def poll_query(self, run_id: str, **kwargs) -> "tuple[str, Optional[QueryResult]]":
        """Checa um run async. Devolve ``(status, result)`` onde ``status`` ∈
        ``{"running","finished","failed"}``; ``result`` (``QueryResult``) só vem
        preenchido quando ``finished``. NÃO bloqueia (1 checagem por chamada)."""
        raise NotImplementedError(f"{self.platform} does not support async poll_query")

    def get_health_metrics(self) -> List["HealthMetric"]:
        """Return a list of HealthMetric for the v2 health schema.

        Default implementation returns an empty list — providers that have not
        yet implemented v2 metrics still produce a valid HealthResponse with
        ``metrics=[]``. The UI falls back to showing only ``last_collection_at``.

        Implementors should NOT raise; return ``severity="unknown"`` for metrics
        that cannot be retrieved in the current state.
        """
        return []

    def on_created(self) -> None:
        """Hook pós-criação da integração (default no-op).

        Chamado pelo router APÓS persistir a integração. Vendors com lifecycle
        especial de criação sobrescrevem — ex.: Sophos Partner/Organization
        dispara a descoberta assíncrona de tenants. Substitui o branch
        ``if integration.kind in (...)`` que vivia no router."""
        pass

    def close(self) -> None:
        """Release resources."""
        pass
