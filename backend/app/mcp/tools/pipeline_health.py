"""Pipeline-health tools — first stop of a triage session.

Backend source of truth: ``backend/app/routers/pipeline_health.py``
(APIRouter prefix ``/integrations``, mounted under ``/api`` in main.py):

- ``GET /api/integrations/pipeline-health``                     (bulk)
- ``GET /api/integrations/{integration_id}/pipeline-health``    (single)

Both endpoints require ``Permission.INTEGRATION_READ`` (not admin-only) and
are org-scoped: admins see every integration, non-admin tokens only see
integrations of their own organization subtree. Results are served from a
60-second Redis cache; no live vendor calls are made.
"""

from __future__ import annotations

from typing import Any

from ._base import (
    CentralOpsClient,
    ToolSpec,
    _integer,
    _object,
)


_HEALTH_FIELDS_DOC = (
    "Each health record contains: integration_id, status ('healthy' | 'degraded' "
    "| 'unhealthy' | 'unknown'), events_per_minute (approximate — derived from "
    "5-minute deltas of cumulative collection counters; null on the first call, "
    "after a counter reset, or when the window is too short), lag_seconds "
    "(seconds since the last successful collection), last_error (most recent "
    "collector error, truncated to 500 chars), last_success_at, "
    "mapped_field_ratio (1 - drift_count_24h / total mapping rules for the "
    "vendor; null when the vendor has no mapping rules with a current version), "
    "drift_count_24h (unknown fields with status 'new' seen in the last 24h, "
    "scoped to the integration's organization), quarantine_count_24h "
    "(quarantined events created in the last 24h), and cached_at. Status "
    "semantics: 'unknown' = never collected successfully; 'unhealthy' = "
    "lag > 300s or >= 3 consecutive failures; 'degraded' = last collection "
    "attempt reported an error; 'healthy' otherwise."
)


async def _get_pipeline_health(client: CentralOpsClient) -> Any:
    return await client.get("/integrations/pipeline-health")


async def _get_integration_pipeline_health(
    client: CentralOpsClient,
    *,
    integration_id: int,
) -> Any:
    return await client.get(f"/integrations/{integration_id}/pipeline-health")


def specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="get_pipeline_health",
            description=(
                "Bulk normalization-pipeline health for every integration the "
                "token can see (admins see all organizations; non-admin tokens "
                "only their own organization's integrations). Use this as the "
                "first read in a triage session to spot degraded or unhealthy "
                "integrations before drilling down. Returns {items, total, "
                "cached_at}. " + _HEALTH_FIELDS_DOC + " Results come from a "
                "60-second Redis cache (bulk view cached per user), so "
                "cached_at may be up to ~60s in the past. Metrics aggregate "
                "persisted collection state only — no live vendor calls."
            ),
            input_schema=_object(properties={}),
            handler=_get_pipeline_health,
        ),
        ToolSpec(
            name="get_integration_pipeline_health",
            description=(
                "Normalization-pipeline health for a single integration. "
                + _HEALTH_FIELDS_DOC
                + " Served from a 60-second per-integration Redis cache "
                "(cached_at marks compute time); no live vendor calls. "
                "Returns 404 if the integration does not exist and 403 if it "
                "belongs to an organization outside the token's scope "
                "(non-admin tokens)."
            ),
            input_schema=_object(
                properties={
                    "integration_id": _integer("Integration id.", minimum=1),
                },
                required=["integration_id"],
            ),
            handler=_get_integration_pipeline_health,
        ),
    ]
