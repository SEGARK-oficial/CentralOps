"""Pydantic schemas for Destinations CRUD API.

These schemas live in their own module to avoid bloating schemas.py.
They are imported by the destinations router and the test suite.

Security invariants enforced here:
  - ``secret_ref`` is NEVER exposed in any Read schema.
  - ``hec_token`` is WRITE-ONLY (absent from DestinationRead).
  - ``has_secret`` is the only indication that a credential exists.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..collectors.output.destinations import registry as _registry


# ── Write schemas ─────────────────────────────────────────────────────


class DestinationCreate(BaseModel):
    """Payload for POST /collectors/destinations.

    ``hec_token`` is the plaintext secret that will be encrypted to
    ``secret_ref`` before persistence. It is WRITE-ONLY and never
    appears in any response.
    """

    name: str = Field(..., min_length=1, max_length=255)
    kind: str
    config: Dict[str, Any] = Field(default_factory=dict)
    delivery: Dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    # WRITE-ONLY — plaintext credential; encrypted before storing
    hec_token: Optional[str] = Field(default=None)
    organization_id: Optional[int] = None
    # data residency constraint (EU | US | BR | global | null = no restriction)
    data_residency: Optional[str] = Field(
        default=None,
        description="Data residency zone (EU | US | BR | global). NULL = no restriction.",
    )
    # cria automaticamente uma rota broadcast ``{} → [dest]``
    # (clone+continue) ao criar o destino — recebe todos os eventos por default,
    # editável em /routes. ``false`` → roteamento explícito puro (sem auto-rota).
    auto_route: bool = True
    # marca este destino como o FALLBACK (catch-all) p/
    # eventos que não casam NENHUMA rota. No máx. 1 por org (enforce na API).
    # Substitui o ``wazuh-default`` hardcoded — qualquer destino pode ser o default.
    is_default: bool = False

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be empty")
        return v

    @field_validator("kind")
    @classmethod
    def _validate_kind(cls, v: str) -> str:
        kinds = _registry.all_kinds()
        if v not in kinds:
            raise ValueError(
                f"kind {v!r} is not registered. Valid kinds: {kinds}"
            )
        return v

    @model_validator(mode="after")
    def _validate_config_against_schema(self) -> "DestinationCreate":
        """Validate config AND delivery against the kind's schemas.

        On error raises ValueError so FastAPI returns 422 with Pydantic detail.
        The kind validator runs first; if kind is invalid this is skipped.
        """
        try:
            reg = _registry.get(self.kind)
        except KeyError:
            # kind already failed its own validator — skip
            return self
        try:
            reg.config_schema(**self.config)
        except Exception as exc:
            raise ValueError(
                f"config is invalid for kind={self.kind!r}: {exc}"
            ) from exc
        # validate the delivery policy (breaker/concurrency/
        # backpressure/queue_ceiling/shadow). Strict: typos → 422.
        from ..collectors.output.delivery_config import parse_delivery

        try:
            parse_delivery(self.kind, self.delivery)
        except Exception as exc:
            raise ValueError(
                f"delivery is invalid for kind={self.kind!r}: {exc}"
            ) from exc
        return self


class DestinationUpdate(BaseModel):
    """Partial-update payload for PUT /collectors/destinations/{id}.

    All fields are optional. Unset fields are not mutated (``_UNSET``
    sentinel pattern). ``hec_token`` triggers re-encryption of secret_ref.
    """

    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    config: Optional[Dict[str, Any]] = None
    delivery: Optional[Dict[str, Any]] = None
    enabled: Optional[bool] = None
    # WRITE-ONLY — plaintext credential re-key
    hec_token: Optional[str] = None
    organization_id: Optional[int] = None
    # data residency update
    data_residency: Optional[str] = Field(
        default=None,
        description="Data residency zone (EU | US | BR | global). NULL = no restriction.",
    )
    # marca/desmarca este destino como o fallback (catch-all).
    is_default: Optional[bool] = None

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            raise ValueError("name must not be empty")
        return stripped

    @field_validator("delivery")
    @classmethod
    def _validate_delivery(cls, v: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Validate the delivery policy on partial update. Kind is
        unknown here, so we validate the user's blob directly against
        ``DeliveryConfig`` (catches typos/out-of-range — kind defaults are
        server-side and already valid)."""
        if v is None:
            return None
        from ..collectors.output.delivery_config import DeliveryConfig

        try:
            DeliveryConfig(**v)
        except Exception as exc:
            raise ValueError(f"delivery is invalid: {exc}") from exc
        return v


# ── Read schemas ──────────────────────────────────────────────────────


class DestinationRead(BaseModel):
    """Response shape for a Destination row.

    ``secret_ref`` is intentionally absent — callers learn only whether
    a credential exists via ``has_secret``.
    """

    id: str
    name: str
    kind: str
    enabled: bool
    config: Dict[str, Any]
    delivery: Dict[str, Any]
    config_version: str
    organization_id: Optional[int]
    created_at: datetime
    updated_at: datetime
    has_secret: bool = False
    # data residency zone (EU | US | BR | global | null = no restriction)
    data_residency: Optional[str] = None
    # este destino é o fallback (catch-all) da org?
    is_default: bool = False

    model_config = ConfigDict(from_attributes=False)


class DestinationTestResponse(BaseModel):
    """Response for POST /collectors/destinations/{id}/test."""

    ok: bool
    detail: str = ""
    latency_ms: Optional[float] = None


class DestinationShadowRequest(BaseModel):
    """Request for POST /collectors/destinations/{id}/shadow (preview).

    ``sample`` is an optional canonical envelope to format; when absent a
    synthetic minimal envelope is used. The destination is NEVER contacted.
    """

    sample: Optional[Dict[str, Any]] = None


class DestinationShadowResponse(BaseModel):
    """Response for the shadow/preview endpoint — shows the formatted wire
    WITHOUT delivering. No socket is opened; no credential is decrypted."""

    ok: bool
    detail: str = ""
    count: int = 0
    formatted_preview: Optional[str] = None
    latency_ms: Optional[float] = None


class DestinationHealthResponse(BaseModel):
    """Health snapshot for a destination (UI KPI cards).

    DB+Redis based (no Prometheus dependency). ``status`` is derived:
      - ``disabled``  — destination is not enabled.
      - ``unhealthy`` — circuit breaker OPEN.
      - ``degraded``  — DLQ activity in the last 24h.
      - ``healthy``   — enabled, breaker closed, no recent DLQ.
    Rich EPS/bytes/latency series come from the metrics-read endpoint,
    exposed here as ``null`` for now.
    """

    destination_id: str
    status: str
    enabled: bool
    breaker_state: Optional[str] = None  # closed | open | half_open | unknown
    dlq_total: int = 0
    dlq_24h: int = 0
    last_dlq_at: Optional[datetime] = None
    # COMPUTADOS do store nativo (janela 1h, padrão
    # AxoSyslog eps_last_*). eps = eventos entregues/s; bytes_per_min = bytes/min.
    eps: Optional[float] = None
    bytes_per_min: Optional[float] = None


class DestinationHealthItem(BaseModel):
    """One destination's health in the batch view.

    Same shape as ``DestinationHealthResponse`` plus ``name``/``kind`` so the UI
    can render the destinations list (health badges + EPS/bytes) in a SINGLE call
    — avoids the N+1 of fetching ``/{id}/health`` per row. ``status`` is derived
    identically to the single-destination endpoint."""

    destination_id: str
    name: str
    kind: str
    status: str  # healthy | degraded | unhealthy | disabled | unknown
    enabled: bool
    breaker_state: Optional[str] = None  # closed | open | half_open | unknown
    dlq_total: int = 0
    dlq_24h: int = 0
    last_dlq_at: Optional[datetime] = None
    eps: Optional[float] = None
    bytes_per_min: Optional[float] = None


class DestinationHealthBatchResponse(BaseModel):
    """Batch health for GET /collectors/destinations/health — every destination
    visible to the caller (same org-scope/visibility as ``list_destinations``)."""

    total: int
    items: List[DestinationHealthItem] = Field(default_factory=list)


class DestinationDlqEntry(BaseModel):
    """One dead-letter row (DLQ drill-in)."""

    id: str
    event_id: str
    error_kind: str
    error_detail: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None
    organization_id: Optional[int] = None
    created_at: datetime


class DestinationDlqResponse(BaseModel):
    """Paginated DLQ view + breakdown by error_kind for a destination."""

    destination_id: str
    total: int
    by_error_kind: Dict[str, int]
    entries: List[DestinationDlqEntry]


class DestinationTapResponse(BaseModel):
    """Live data-tap — most-recent redacted envelopes that
    flowed to a destination."""

    destination_id: str
    entries: List[Dict[str, Any]] = Field(default_factory=list)


class DestinationMetricsResponse(BaseModel):
    """Per-destination observability. DB-derived summary is
    ALWAYS present; ``series`` (EPS/bytes/latency over time) is populated only
    when a Prometheus backend is configured (``available``)."""

    destination_id: str
    available: bool
    reason: Optional[str] = None
    series: Dict[str, Any] = Field(default_factory=dict)
    # Latest point-in-time gauges (queue_depth, backpressure_state).
    gauges: Dict[str, Any] = Field(default_factory=dict)
    # Always-available DB/Redis summary.
    dlq_total: int = 0
    dlq_24h: int = 0
    by_error_kind: Dict[str, int] = Field(default_factory=dict)
    breaker_state: Optional[str] = None


class DlqReprocessRequest(BaseModel):
    """Request body for POST /{destination_id}/dlq/reprocess.

    ``event_ids``: explicit set of DLQ event_ids to reprocess.  When
    omitted (or empty), ALL entries for the destination are drained.
    Idempotent: re-posting the same ids is safe (already-cleared rows
    produce no-ops).
    """

    event_ids: Optional[List[str]] = Field(
        default=None,
        description="Specific DLQ event_ids to reprocess; null/empty → all.",
    )


class DlqReprocessResponse(BaseModel):
    """Response for POST /{destination_id}/dlq/reprocess."""

    destination_id: str
    task_id: str
    queued: int  # number of DLQ rows that will be attempted


class DestinationTypeRead(BaseModel):
    """One entry in GET /collectors/destinations/destination-types.

    Shape mirrors ``DestinationRegistration.describe()``.
    """

    kind: str
    label: str
    default_queue: str
    capabilities: List[str]
    required_secrets: List[str]
    config_schema: Dict[str, Any]
    # delivery policy schema + per-kind defaults (UI renders the
    # delivery form from this, same as config_schema for the config form).
    delivery_schema: Dict[str, Any] = Field(default_factory=dict)
    delivery_defaults: Dict[str, Any] = Field(default_factory=dict)
    # Catálogo self-describing (simetria com ProviderPlatformRead): a galeria de
    # destinos lê ícone/categoria/descrição/tier DAQUI — sem mapas hardcoded no
    # frontend. Defaults tolerantes para destinos legados que ainda não declaram.
    category: str = "Outros"
    description: str = ""
    icon_id: Optional[str] = None
    docs_url: Optional[str] = None
    tier: str = "stable"
    order: int = 100

    model_config = ConfigDict(from_attributes=False)


# ── credential lifecycle ──────────────────────────────────────────


class CredentialRotateRequest(BaseModel):
    """Payload for POST /{id}/credential/rotate.

    ``new_secret`` is the plaintext credential that will be encrypted
    before storage. WRITE-ONLY: never appears in any response.

    ``expires_at`` is an optional RFC 3339 timestamp after which the
    credential should be considered expired (informational — the server
    does not auto-revoke on expiry; use it for scheduling reminders).
    """

    new_secret: str = Field(..., min_length=1)
    expires_at: Optional[datetime] = None


class CredentialRotateResponse(BaseModel):
    """Response for POST /{id}/credential/rotate."""

    destination_id: str
    secret_version: int
    secret_rotated_at: datetime
    secret_expires_at: Optional[datetime] = None
    has_secret: bool = True


class CredentialRevokeResponse(BaseModel):
    """Response for POST /{id}/credential/revoke."""

    destination_id: str
    enabled: bool  # always False after revoke
    secret_revoked_at: datetime
    has_secret: bool  # always False after revoke


# ── credential access audit ──────────────────────────────────────


class CredentialAccessEntry(BaseModel):
    """One row in the credential access log."""

    id: str
    destination_id: str
    actor: Optional[str]
    action: str  # decrypt | test | rotate | revoke
    organization_id: Optional[int]
    detail: Optional[str]
    created_at: datetime

    model_config = ConfigDict(from_attributes=False)


class CredentialAuditResponse(BaseModel):
    """Paginated credential audit trail for GET /{id}/credential/audit."""

    destination_id: str
    total: int
    entries: List[CredentialAccessEntry]


# ── destination CRUD audit trail ────────────────────────────


class DestinationAuditEntry(BaseModel):
    """One row in the append-only destination CRUD audit trail.

    ``snapshot`` is the scrubbed destination state at mutation time — it
    NEVER contains the secret in clear (only ``has_secret: bool``)."""

    id: str
    destination_id: str
    action: str  # create | update | delete
    actor: Optional[str]
    snapshot: Dict[str, Any]
    created_at: datetime

    model_config = ConfigDict(from_attributes=False)


class DestinationAuditResponse(BaseModel):
    """Audit trail for GET /{id}/audit (newest first)."""

    destination_id: str
    total: int
    entries: List[DestinationAuditEntry]


# ── event lineage ─────────────────────────────────────────────────


class LineageEntry(BaseModel):
    """One delivery record for an event at a specific destination.

    Retention note: lineage is stored in Redis with a configurable TTL
    (default 7 days, ``LINEAGE_TTL_S``).  This is NOT a compliance archive —
    use the JSONL or Elasticsearch sink for long-term evidence.
    """

    destination_id: str
    kind: str
    status: str  # "delivered"
    ts: float  # UNIX epoch seconds


class DestinationLineageResponse(BaseModel):
    """Response for GET /collectors/destinations/{id}/lineage?event_id=...

    Lists where a specific event was delivered for the given destination.
    Empty ``entries`` means no positive lineage recorded (event may have
    gone to DLQ, was shed, or predates the LINEAGE_ENABLED flag).
    """

    destination_id: str
    event_id: str
    entries: List[LineageEntry] = Field(default_factory=list)
    retention_note: str = (
        "Lineage is recent-only (Redis TTL). "
        "Not a compliance archive — see JSONL/Elasticsearch for long-term retention."
    )


class EventLineageResponse(BaseModel):
    """Response for GET /collectors/lineage/{event_id} (admin, org-scoped).

    Shows ALL destinations that received the event within the lineage window.
    """

    event_id: str
    organization_id: int
    entries: List[LineageEntry] = Field(default_factory=list)
    retention_note: str = (
        "Lineage is recent-only (Redis TTL). "
        "Not a compliance archive — see JSONL/Elasticsearch for long-term retention."
    )
