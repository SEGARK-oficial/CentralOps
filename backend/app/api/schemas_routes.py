"""Pydantic schemas for Routes CRUD (motor de roteamento).

Conditions are validated against the routing engine's allowlist
(``routing.validate_condition``). ``destination_ids`` existence is checked in the
router (needs the DB).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RouteCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    condition: Dict[str, Any] = Field(default_factory=dict)
    destination_ids: List[str] = Field(default_factory=list)
    action: Literal["route", "drop"] = "route"
    is_final: bool = True
    priority: int = Field(default=100, ge=0, le=1_000_000)
    enabled: bool = True
    canary_percent: int = Field(default=100, ge=0, le=100)
    transform_ref: Optional[str] = None
    #: redação de PII por rota ({"version":1,"rules":[...]} ou lista).
    #: Validada na escrita (FAIL-CLOSED: spec ruim → 422, nunca armazenada).
    pii_redaction: Optional[Any] = None
    #: fail-safe de detecção — default True (PROTEGE). Espelha
    #: ``models.Route.protect_detection``: ausência de decisão NUNCA vira
    #: amostragem/agregação silenciosa. Opt-out é sempre explícito (False).
    protect_detection: bool = True
    #: amostragem determinística por event_id (0-100). 100 = byte-idêntico
    #: (sem redução). NUNCA aplicada a rotas ``protect_detection=True``.
    sample_percent: int = Field(default=100, ge=0, le=100)
    #: CSV de labels da assinatura de supressão (ex.: "src_ip,event_type").
    #: None/"" = supressão desligada.
    suppress_key: Optional[str] = None
    #: quantos eventos passam por janela antes de suprimir (0 = desligado).
    suppress_allow: int = Field(default=0, ge=0)
    #: janela de supressão em segundos (deve ser > 0).
    suppress_window_s: int = Field(default=30, gt=0)
    #: descarta o bloco ``raw`` (evento bruto) na entrega desta rota. False =
    #: byte-idêntico. Decisão POR-DESTINO: o lago recebe o bruto, o SIEM não.
    #: NUNCA aplicada a rotas ``protect_detection=True``.
    drop_raw: bool = False
    organization_id: Optional[int] = None

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be empty")
        return v

    @field_validator("condition")
    @classmethod
    def _validate_condition(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        from ..collectors.routing import validate_condition

        try:
            validate_condition(v)
        except Exception as exc:
            raise ValueError(str(exc)) from exc
        return v

    @field_validator("pii_redaction")
    @classmethod
    def _validate_pii_redaction(cls, v: Any) -> Any:
        if v is None:
            return None
        from ..collectors.routing import validate_pii_redaction

        try:
            validate_pii_redaction(v)
        except Exception as exc:
            raise ValueError(str(exc)) from exc
        return v

    @model_validator(mode="after")
    def _validate_action_destinations(self) -> "RouteCreate":
        if self.action == "route" and not self.destination_ids:
            raise ValueError("action 'route' requires at least one destination_id")
        if self.action == "drop" and self.destination_ids:
            raise ValueError("action 'drop' must not have destination_ids")
        return self


class RouteUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    condition: Optional[Dict[str, Any]] = None
    destination_ids: Optional[List[str]] = None
    action: Optional[Literal["route", "drop"]] = None
    is_final: Optional[bool] = None
    priority: Optional[int] = Field(default=None, ge=0, le=1_000_000)
    enabled: Optional[bool] = None
    canary_percent: Optional[int] = Field(default=None, ge=0, le=100)
    transform_ref: Optional[str] = None
    pii_redaction: Optional[Any] = None
    #: ausente = mantém o valor atual (fail-safe: NUNCA vira False por
    #: omissão). Explícito True/False é sempre respeitado.
    protect_detection: Optional[bool] = None
    sample_percent: Optional[int] = Field(default=None, ge=0, le=100)
    #: ausente = mantém; explícito ``null`` LIMPA a chave de supressão
    #: (ver ``model_fields_set`` no router — ``None`` aqui é ambíguo por
    #: si só, o wiring do endpoint resolve a distinção).
    suppress_key: Optional[str] = None
    suppress_allow: Optional[int] = Field(default=None, ge=0)
    suppress_window_s: Optional[int] = Field(default=None, gt=0)
    #: ausente = mantém o valor atual (mesmo fail-safe do protect_detection:
    #: nunca liga o descarte por omissão).
    drop_raw: Optional[bool] = None
    organization_id: Optional[int] = None

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        if not s:
            raise ValueError("name must not be empty")
        return s

    @field_validator("condition")
    @classmethod
    def _validate_condition(cls, v: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if v is None:
            return None
        from ..collectors.routing import validate_condition

        try:
            validate_condition(v)
        except Exception as exc:
            raise ValueError(str(exc)) from exc
        return v

    @field_validator("pii_redaction")
    @classmethod
    def _validate_pii_redaction(cls, v: Any) -> Any:
        if v is None:
            return None
        from ..collectors.routing import validate_pii_redaction

        try:
            validate_pii_redaction(v)
        except Exception as exc:
            raise ValueError(str(exc)) from exc
        return v


class RouteRead(BaseModel):
    id: str
    name: str
    priority: int
    condition: Dict[str, Any]
    action: str
    destination_ids: List[str]
    is_final: bool
    canary_percent: int
    transform_ref: Optional[str]
    pii_redaction: Optional[Any] = None
    protect_detection: bool = True
    sample_percent: int = 100
    suppress_key: Optional[str] = None
    suppress_allow: int = 0
    suppress_window_s: int = 30
    drop_raw: bool = False
    enabled: bool
    organization_id: Optional[int]
    created_at: datetime
    updated_at: datetime
    #: UX guard — True if shadowed by an earlier enabled is_final route.
    unreachable: bool = False

    model_config = ConfigDict(from_attributes=False)


class RouteAuditRead(BaseModel):
    id: str
    route_id: str
    action: str
    actor: Optional[str]
    snapshot: Dict[str, Any]
    created_at: datetime

    model_config = ConfigDict(from_attributes=False)


# ── Dry-run (preview routing BEFORE saving) ──────────────────────


class RouteDryRunRequest(BaseModel):
    """Preview what routes would do. ``routes`` = candidate rule set (null → the
    org's saved routes). ``samples`` = events/labels to test (null → pull recent
    dispatched envelopes from the org's audit buffer)."""

    routes: Optional[List[RouteCreate]] = None
    samples: Optional[List[Dict[str, Any]]] = None
    sample_size: int = Field(default=50, ge=1, le=500)


class RouteDryRunResult(BaseModel):
    labels: Dict[str, Any]
    destinations: List[str]
    dropped: bool
    fallback: bool


class RouteDryRunResponse(BaseModel):
    evaluated: int
    sample_source: str  # "provided" | "audit_buffer" | "none"
    routed: int
    dropped: int
    fallback: int
    per_destination: Dict[str, int]
    unreachable_route_ids: List[str]
    results: List[RouteDryRunResult]


class RouteRollbackRequest(BaseModel):
    """Restore a route to the state captured in a prior audit snapshot."""

    audit_id: str


class RouteMetricsResponse(BaseModel):
    """Per-route observability — events matched/routed/dropped over time,
    from the native store (Redis rollups)."""

    route_id: str
    series: Dict[str, Any]


class RouteHealthResponse(BaseModel):
    """Per-route health snapshot (paridade rota↔destino).

    Computado do store nativo (janela 1h): EPS de eventos casados, taxa de drop e
    contadores da janela. ``status`` ∈ healthy | idle | disabled."""

    route_id: str
    status: str
    enabled: bool
    matched_eps: float = 0.0  # eventos casados/s na última 1h
    matched_1h: int = 0
    routed_1h: int = 0
    dropped_1h: int = 0
    drop_rate: float = 0.0  # dropped / matched (1h)


# ── Topology (flow graph source→route→dest) ────


class TopologyDestination(BaseModel):
    """A destination node in the flow graph.

    Throughput (``eps``/``bytes_per_min``) and ``status`` are computed from the
    SAME native-store health logic as ``GET /collectors/destinations/health`` —
    no separate code path."""

    id: str
    name: str
    kind: str
    status: str  # healthy | degraded | unhealthy | disabled | unknown
    eps: Optional[float] = None
    bytes_per_min: Optional[float] = None


class TopologyRoute(BaseModel):
    """A route edge in the flow graph. ``matched/routed/drop_per_min`` are the
    per-minute averages over the last 60 min, derived from the SAME per-route
    series the ``GET /{route_id}/metrics`` endpoint reads (matched/route/drop).
    ``is_system`` flags the seeded catch-all (non-deletable, non-reorderable)."""

    id: str
    name: str
    action: str
    destination_ids: List[str]
    matched_per_min: float = 0.0
    routed_per_min: float = 0.0
    drop_per_min: float = 0.0
    enabled: bool
    is_system: bool = False


class RoutingTopologyResponse(BaseModel):
    """Flow topology for the observability UI.

    Org-scoped graph of source→route→destination with throughput, over a 60-min
    window. Same org-scope/visibility as ``list_routes``/``list_destinations``."""

    destinations: List[TopologyDestination] = Field(default_factory=list)
    routes: List[TopologyRoute] = Field(default_factory=list)


# ── Flow Graph (página /flow — sources+routes+destinations+totais) ─────


class FlowSource(BaseModel):
    """A source (integration) node in the full flow graph.

    ``events_per_minute`` comes from pipeline-health (snapshot-delta over 5 min).
    ``eps`` = events_per_minute / 60, normalised to events/second for UI parity
    with the destination metric. ``status`` is mapped from the pipeline-health
    canonical values (healthy/degraded/unhealthy/unknown).
    """

    id: str  # str(integration_id)
    name: str
    platform: str
    status: str  # healthy | degraded | unhealthy | unknown
    events_per_minute: float
    eps: float  # events_per_minute / 60


class FlowRoute(TopologyRoute):
    """Route edge in the full flow graph — identical to TopologyRoute.

    Alias kept as a named class so the frontend schema can import FlowRoute
    and TopologyRoute independently without coupling to the same symbol."""


class FlowDestination(TopologyDestination):
    """Destination node in the full flow graph — identical to TopologyDestination.

    Alias kept for the same reason as FlowRoute."""


class FlowTotals(BaseModel):
    """Aggregate throughput across the entire org pipeline (60-min window).

    Derived from the collections assembled by ``GET /collectors/routes/flow``:
    ingest_eps  = sum(sources.eps)
    routed_per_min  = sum(routes.routed_per_min)
    drop_per_min    = sum(routes.drop_per_min)
    delivered_eps   = sum(destinations.eps)
    """

    ingest_eps: float = 0.0
    routed_per_min: float = 0.0
    drop_per_min: float = 0.0
    delivered_eps: float = 0.0


class FlowGraphResponse(BaseModel):
    """Complete flow graph for the /flow page (sources → routes → destinations).

    Org-scoped, 60-min window. Each subsystem degrades independently:
    Redis down → routes/destinations show 0.0 throughput; DB down → sources
    list is empty. The overall response never returns 500 — partial data is
    always better than no data for an operational dashboard."""

    generated_at: str  # ISO UTC
    window_minutes: int = 60
    sources: List[FlowSource] = Field(default_factory=list)
    routes: List[FlowRoute] = Field(default_factory=list)
    destinations: List[FlowDestination] = Field(default_factory=list)
    totals: FlowTotals = Field(default_factory=FlowTotals)


# ── Reorder (drag-reorder bulk priority) ──────────────────────


class RouteReorderRequest(BaseModel):
    """Ordered list of route_ids → priorities are reassigned 10, 20, 30 …
    (step=10 leaves gaps for future inserts without immediate conflicts).

    Constraints enforced in the router:
      - All ids must belong to the caller's org (org-scope, anti-enum).
      - Transactional: either all priorities are reassigned or none are.
      - Each reassignment appends a ``reorder`` audit entry.
    """

    route_ids: List[str] = Field(..., min_length=1, max_length=500)


class RouteReorderResponse(BaseModel):
    """Summary of the reorder operation — the list of routes with their
    new priorities in the requested order."""

    reordered: List[RouteRead]
