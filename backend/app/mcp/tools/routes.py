"""Read-only MCP tools for the routing surface (ADR-0008).

Answers triage questions like "why did this event end up in the unrouted DLQ?"
and "which destination should have received it?".

Backend source of truth: backend/app/routers/routes.py
(APIRouter prefix="/collectors/routes", mounted under /api in main.py).
All endpoints below require an admin token (require_admin_user) and are
org-scoped: an org-scoped admin only sees their organization's routes plus
global (shared, organization_id=null) rows.

Only GET endpoints are exposed — no create/update/delete/reorder/rollback/dry-run.
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

_ADMIN_NOTE = (
    "Requires an admin token (USER_MANAGE permission); returns 403 otherwise. "
    "Org-scoped: an org-scoped admin sees only their organization's rows plus "
    "global (shared) ones."
)


async def _list_routes(
    client: CentralOpsClient,
    *,
    include_disabled: bool | None = None,
    offset: int | None = None,
    limit: int | None = None,
) -> Any:
    return await client.get(
        "/collectors/routes",
        params={
            "include_disabled": include_disabled,
            "offset": offset,
            "limit": limit,
        },
    )


async def _get_routes_topology(client: CentralOpsClient) -> Any:
    return await client.get("/collectors/routes/topology")


async def _get_routes_flow(client: CentralOpsClient) -> Any:
    return await client.get("/collectors/routes/flow")


async def _get_route_health(
    client: CentralOpsClient,
    *,
    route_id: str,
) -> Any:
    return await client.get(f"/collectors/routes/{route_id}/health")


async def _get_route_metrics(
    client: CentralOpsClient,
    *,
    route_id: str,
    range_minutes: int | None = None,
) -> Any:
    return await client.get(
        f"/collectors/routes/{route_id}/metrics",
        params={"range_minutes": range_minutes},
    )


def specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="list_routes",
            description=(
                "List routing rules visible to the caller. Each route has: id, "
                "name, priority (lower evaluates first), condition (structured "
                "match), action ('route' or 'drop'), destination_ids, is_final, "
                "canary_percent, transform_ref, pii_redaction, enabled, "
                "organization_id (null = global/shared route), created_at, "
                "updated_at, and unreachable (true when the route is shadowed by "
                "an earlier enabled is_final route and can never match — computed "
                "over the full visible set, not just this page). Use this to "
                "understand why an event was delivered to a destination, dropped, "
                "or fell through to the unrouted DLQ (no route matched and the org "
                "has no default destination). " + _ADMIN_NOTE
            ),
            input_schema=_object(
                properties={
                    "include_disabled": {
                        "type": "boolean",
                        "description": (
                            "Include disabled routes (server default true)."
                        ),
                    },
                    "offset": _integer(
                        "Pagination offset (server default 0).", minimum=0
                    ),
                    "limit": _integer(
                        "Maximum results (1-500, server default 200).",
                        minimum=1,
                        maximum=500,
                    ),
                },
            ),
            handler=_list_routes,
        ),
        ToolSpec(
            name="get_routes_topology",
            description=(
                "Routing topology graph (routes + destinations) with live "
                "throughput for observability. Response has 'routes' (id, name, "
                "action, destination_ids, matched_per_min / routed_per_min / "
                "drop_per_min averaged over the server's configured rate window, "
                "enabled, is_system) and 'destinations' (id, name, kind, status "
                "in healthy|degraded|unhealthy|disabled|unknown, eps, "
                "bytes_per_min — same health logic as destination health). "
                "Sources/integrations are NOT included — use get_routes_flow for "
                "the full source→route→destination graph. Throughput degrades to "
                "0.0 if the native metrics store (Redis) is unavailable. "
                + _ADMIN_NOTE
            ),
            input_schema=_object(properties={}),
            handler=_get_routes_topology,
        ),
        ToolSpec(
            name="get_routes_flow",
            description=(
                "Full pipeline flow graph: sources → routes → destinations, with "
                "live volume and health. Response fields: generated_at (ISO UTC), "
                "window_minutes (server rate window), sources (per integration: "
                "id, name, platform, status healthy|degraded|unhealthy|unknown, "
                "events_per_minute, eps), routes (same shape as topology routes), "
                "destinations (same shape as topology destinations), and totals "
                "(ingest_eps, routed_per_min, drop_per_min, delivered_eps). Best "
                "first read for 'where is my traffic going / where is it being "
                "dropped?'. Each subsystem degrades independently (Redis down -> "
                "0.0 throughput; DB error -> empty sources); the endpoint never "
                "returns 500, so partial data is possible. " + _ADMIN_NOTE
            ),
            input_schema=_object(properties={}),
            handler=_get_routes_flow,
        ),
        ToolSpec(
            name="get_route_health",
            description=(
                "Health snapshot for a single route over a fixed 1-hour window: "
                "status ('disabled' if the route is off, 'healthy' if it matched "
                "events in the last hour, otherwise 'idle'), enabled, matched_eps "
                "(matched events/second), matched_1h, routed_1h, dropped_1h, and "
                "drop_rate (dropped/matched). Counters come from the native "
                "metrics store. Routes outside the caller's scope return 404 "
                "(anti-enumeration), not 403. " + _ADMIN_NOTE
            ),
            input_schema=_object(
                properties={
                    "route_id": _string("Route id (uuid)."),
                },
                required=["route_id"],
            ),
            handler=_get_route_health,
        ),
        ToolSpec(
            name="get_route_metrics",
            description=(
                "Per-route time series from the native metrics store: response is "
                "{route_id, series} where series holds the 'matched', 'route' "
                "(routed) and 'drop' counters per minute over the requested "
                "range. Use to see when a route started/stopped matching or "
                "dropping traffic. Routes outside the caller's scope return 404 "
                "(anti-enumeration). " + _ADMIN_NOTE
            ),
            input_schema=_object(
                properties={
                    "route_id": _string("Route id (uuid)."),
                    "range_minutes": _integer(
                        "Time range in minutes (5-1440, server default 60).",
                        minimum=5,
                        maximum=1440,
                    ),
                },
                required=["route_id"],
            ),
            handler=_get_route_metrics,
        ),
    ]
