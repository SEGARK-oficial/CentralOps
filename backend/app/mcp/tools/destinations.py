"""Read-only visibility tools for CentralOps destinations (ADR-0003/0008).

Backend source of truth: ``backend/app/routers/destinations.py`` —
``router`` (prefix ``/collectors/destinations``) and ``lineage_router``
(prefix ``/collectors/lineage``), both mounted under ``/api`` in
``backend/app/main.py``. All endpoints here are GET-only; mutation
endpoints (create/update/delete/test/shadow/reprocess/rotate/revoke)
are intentionally NOT exposed.
"""

from __future__ import annotations

from typing import Any

from ._base import (
    CentralOpsClient,
    ToolSpec,
    _integer,
    _object,
    _string,
)


def _boolean(description: str, **extra: Any) -> dict[str, Any]:
    return {"type": "boolean", "description": description, **extra}


_ADMIN_NOTE = (
    "Requires an admin token (USER_MANAGE permission); returns 403 otherwise. "
    "Results are org-scoped: a non-global admin only sees their own org's "
    "destinations plus shared global rows; cross-tenant destination ids "
    "return 404 (anti-enumeration)."
)


async def _list_destinations(
    client: CentralOpsClient,
    *,
    org_id: int | None = None,
    include_disabled: bool = False,
    offset: int = 0,
    limit: int = 50,
) -> Any:
    return await client.get(
        "/collectors/destinations",
        params={
            "org_id": org_id,
            "include_disabled": include_disabled,
            "offset": offset,
            "limit": limit,
        },
    )


async def _get_destinations_health(
    client: CentralOpsClient,
    *,
    include_disabled: bool = True,
) -> Any:
    return await client.get(
        "/collectors/destinations/health",
        params={"include_disabled": include_disabled},
    )


async def _get_destination_health(
    client: CentralOpsClient,
    *,
    destination_id: str,
) -> Any:
    return await client.get(f"/collectors/destinations/{destination_id}/health")


async def _list_destination_dlq(
    client: CentralOpsClient,
    *,
    destination_id: str,
    offset: int = 0,
    limit: int = 50,
) -> Any:
    return await client.get(
        f"/collectors/destinations/{destination_id}/dlq",
        params={"offset": offset, "limit": limit},
    )


async def _get_destination_metrics(
    client: CentralOpsClient,
    *,
    destination_id: str,
    range_minutes: int = 60,
) -> Any:
    return await client.get(
        f"/collectors/destinations/{destination_id}/metrics",
        params={"range_minutes": range_minutes},
    )


async def _list_destination_audit(
    client: CentralOpsClient,
    *,
    destination_id: str,
    limit: int = 50,
) -> Any:
    return await client.get(
        f"/collectors/destinations/{destination_id}/audit",
        params={"limit": limit},
    )


async def _list_destination_lineage(
    client: CentralOpsClient,
    *,
    destination_id: str,
    event_id: str,
) -> Any:
    return await client.get(
        f"/collectors/destinations/{destination_id}/lineage",
        params={"event_id": event_id},
    )


async def _get_event_lineage(
    client: CentralOpsClient,
    *,
    event_id: str,
    org_id: int | None = None,
) -> Any:
    return await client.get(
        f"/collectors/lineage/{event_id}",
        params={"org_id": org_id},
    )


def specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="list_destinations",
            description=(
                "List event destinations (sinks) configured in CentralOps. Each item "
                "has: id, name, kind (e.g. 'splunk_hec', 'elastic', 'kafka'), enabled, "
                "config, delivery (delivery policy), config_version, organization_id "
                "(null = shared global destination), created_at, updated_at, "
                "has_secret (bool — the credential itself is NEVER returned), "
                "data_residency, and is_default (org catch-all fallback). "
                "Use this first to discover destination ids for the health/DLQ/"
                "metrics/lineage tools. " + _ADMIN_NOTE + " The org_id filter only "
                "has effect for global admins."
            ),
            input_schema=_object(
                properties={
                    "org_id": _integer(
                        "Optional — restrict to one organization's destinations "
                        "(effective for global admins only; non-global callers are "
                        "always scoped to their own org).",
                        minimum=1,
                    ),
                    "include_disabled": _boolean(
                        "Include disabled destinations (default false)."
                    ),
                    "offset": _integer("Pagination offset (default 0).", minimum=0),
                    "limit": _integer(
                        "Maximum results (1-200, default 50).",
                        minimum=1,
                        maximum=200,
                    ),
                },
            ),
            handler=_list_destinations,
        ),
        ToolSpec(
            name="get_destinations_health",
            description=(
                "Batch health snapshot for EVERY destination visible to the caller "
                "in one call (no N+1). Returns total + items, each with: "
                "destination_id, name, kind, status ('healthy' | 'degraded' — DLQ "
                "activity in last 24h | 'unhealthy' — circuit breaker open | "
                "'disabled' | 'unknown' — breaker store unreachable, fail-honest), "
                "enabled, breaker_state (closed | open | half_open | unknown), "
                "dlq_total, dlq_24h, last_dlq_at, eps (delivered events/s over the "
                "rolling window) and bytes_per_min (null until byte data exists). "
                "Best first read when triaging delivery problems. " + _ADMIN_NOTE
            ),
            input_schema=_object(
                properties={
                    "include_disabled": _boolean(
                        "Include disabled destinations so they still show a "
                        "'disabled' status (default true)."
                    ),
                },
            ),
            handler=_get_destinations_health,
        ),
        ToolSpec(
            name="get_destination_health",
            description=(
                "Health snapshot for ONE destination: destination_id, status "
                "('healthy' | 'degraded' | 'unhealthy' | 'disabled' | 'unknown'), "
                "enabled, breaker_state (closed | open | half_open | unknown), "
                "dlq_total, dlq_24h, last_dlq_at, eps (delivered events/s, computed "
                "from the native Redis observability store) and bytes_per_min. "
                "Same derivation as get_destinations_health; prefer the batch tool "
                "when checking more than one destination. " + _ADMIN_NOTE
            ),
            input_schema=_object(
                properties={
                    "destination_id": _string("Destination id (uuid)."),
                },
                required=["destination_id"],
            ),
            handler=_get_destination_health,
        ),
        ToolSpec(
            name="list_destination_dlq",
            description=(
                "Dead-letter queue drill-in for a destination: paginated entries "
                "(id, event_id, error_kind, error_detail, payload — secrets redacted "
                "on read, organization_id, created_at) plus total and a by_error_kind "
                "breakdown. Routing error kinds include 'unrouted' (no route matched "
                "and no default/catch-all destination — these land on the synthetic "
                "destination id '__unrouted__'), 'destination_missing' (route points "
                "at a deleted/unknown destination), 'cross_tenant_destination' "
                "(fail-closed org-mismatch block), 'reprocess_failed' / "
                "'reprocess_parse_error' (quarantine reprocess), and 'exhausted' "
                "(delivery retries exhausted). " + _ADMIN_NOTE
            ),
            input_schema=_object(
                properties={
                    "destination_id": _string(
                        "Destination id (uuid), or '__unrouted__' for events that "
                        "matched no route."
                    ),
                    "offset": _integer("Pagination offset (default 0).", minimum=0),
                    "limit": _integer(
                        "Maximum entries (1-200, default 50).",
                        minimum=1,
                        maximum=200,
                    ),
                },
                required=["destination_id"],
            ),
            handler=_list_destination_dlq,
        ),
        ToolSpec(
            name="get_destination_metrics",
            description=(
                "Delivery observability for a destination, served from the native "
                "store (Redis rollups — no external Prometheus needed). Returns: "
                "series (per-minute time-series for 'sent', 'rejected' and "
                "'latency_avg' over range_minutes; may be empty when no traffic), "
                "gauges (latest queue_depth and backpressure_state), and the "
                "always-available summary: dlq_total, dlq_24h, by_error_kind, "
                "breaker_state. 'available' is always true (empty series just means "
                "no data in the window). " + _ADMIN_NOTE
            ),
            input_schema=_object(
                properties={
                    "destination_id": _string("Destination id (uuid)."),
                    "range_minutes": _integer(
                        "Time window in minutes for the series (5-1440, default 60).",
                        minimum=5,
                        maximum=1440,
                    ),
                },
                required=["destination_id"],
            ),
            handler=_get_destination_metrics,
        ),
        ToolSpec(
            name="list_destination_audit",
            description=(
                "Append-only CRUD audit trail for a destination — one entry per "
                "create/update/delete, newest first. Each entry: id, destination_id, "
                "action ('create' | 'update' | 'delete'), actor, snapshot (destination "
                "state at mutation time — scrubbed: carries has_secret, never the "
                "secret in clear), created_at. This is the config-change audit, NOT "
                "the credential access log. " + _ADMIN_NOTE
            ),
            input_schema=_object(
                properties={
                    "destination_id": _string("Destination id (uuid)."),
                    "limit": _integer(
                        "Maximum entries (1-200, default 50).",
                        minimum=1,
                        maximum=200,
                    ),
                },
                required=["destination_id"],
            ),
            handler=_list_destination_audit,
        ),
        ToolSpec(
            name="list_destination_lineage",
            description=(
                "Positive delivery records for ONE event at ONE destination — "
                "answers 'was event X delivered to destination Y?'. Returns "
                "destination_id, event_id, entries (each: destination_id, kind, "
                "status='delivered', ts as UNIX epoch seconds) and a retention_note. "
                "Limitations: lineage lives in Redis with a TTL (default 7 days) — "
                "NOT a compliance archive; empty entries can mean not delivered, "
                "event went to DLQ, entry expired, or the LINEAGE_ENABLED flag is "
                "off (the endpoint returns an empty list, not an error, when the "
                "feature is disabled). " + _ADMIN_NOTE
            ),
            input_schema=_object(
                properties={
                    "destination_id": _string("Destination id (uuid)."),
                    "event_id": _string("Event id to look up.", minLength=1),
                },
                required=["destination_id", "event_id"],
            ),
            handler=_list_destination_lineage,
        ),
        ToolSpec(
            name="get_event_lineage",
            description=(
                "Org-scoped lineage for ONE event across ALL destinations — answers "
                "'where was event X delivered?'. Returns event_id, organization_id, "
                "entries (each: destination_id, kind, status='delivered', ts as UNIX "
                "epoch seconds) and a retention_note. org_id is ignored for "
                "non-global callers (always their own org); a pure global admin "
                "(no own org) MUST pass org_id or the API returns 400 — no "
                "cross-tenant query without explicit scope. Limitations: Redis TTL "
                "(default 7 days), not a compliance archive; empty entries when the "
                "event was not delivered, expired, or LINEAGE_ENABLED is off. "
                + _ADMIN_NOTE
            ),
            input_schema=_object(
                properties={
                    "event_id": _string("Event id to look up.", minLength=1),
                    "org_id": _integer(
                        "Organization to query. Required for a pure global admin "
                        "(otherwise 400); ignored for non-global callers.",
                        minimum=1,
                    ),
                },
                required=["event_id"],
            ),
            handler=_get_event_lineage,
        ),
    ]
