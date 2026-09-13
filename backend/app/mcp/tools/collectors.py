from __future__ import annotations

from typing import Any

from ._base import (
    CentralOpsClient,
    ToolSpec,
    _integer,
    _object,
    _string,
)


async def _list_collector_vendors(client: CentralOpsClient) -> Any:
    return await client.get("/collectors/vendors")


async def _list_collection_state(
    client: CentralOpsClient,
    *,
    integration_id: int | None = None,
    include_inactive: bool = False,
) -> Any:
    return await client.get(
        "/collectors/state",
        params={
            "integration_id": integration_id,
            "include_inactive": "true" if include_inactive else None,
        },
    )


async def _get_collector_summary(client: CentralOpsClient) -> Any:
    return await client.get("/collectors/summary")


async def _get_collector_cost_summary(client: CentralOpsClient) -> Any:
    return await client.get("/collectors/cost-summary")


def specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="list_collector_vendors",
            description=(
                "List vendor/stream pairs registered in the collector registry — the "
                "static set of (platform, stream) combinations the backend knows how to "
                "poll (e.g. (sophos, alerts), (wazuh, detections)). Use to know which "
                "streams a vendor exposes for backfill or coverage analysis."
            ),
            input_schema=_object(properties={}),
            handler=_list_collector_vendors,
        ),
        ToolSpec(
            name="list_collection_state",
            description=(
                "List per-(integration, stream) collection state: current cursor, "
                "last_success_at, last_attempt_at, last_error, consecutive_failures, "
                "events_collected_total. This answers 'is this stream up to date and "
                "what was the last error from the vendor?'. NOTE: rows belonging to "
                "deactivated integrations/organizations are omitted unless "
                "include_inactive=true."
            ),
            input_schema=_object(
                properties={
                    "integration_id": _integer(
                        "Optional — filter to a single integration.", minimum=1
                    ),
                    "include_inactive": {
                        "type": "boolean",
                        "description": (
                            "Include state rows of deactivated integrations/"
                            "organizations (default false)."
                        ),
                    },
                },
            ),
            handler=_list_collection_state,
        ),
        ToolSpec(
            name="get_collector_summary",
            description=(
                "Aggregated view across all integrations the user can see: counters "
                "(events_collected_total, integrations_tracked, vendors_registered), "
                "failing streams (integrations_with_errors = rows with "
                "consecutive_failures > 0), max staleness in minutes since last "
                "successful collection (stale_minutes_max) and a per_platform "
                "breakdown. Useful as the first read in a triage session."
            ),
            input_schema=_object(properties={}),
            handler=_get_collector_summary,
        ),
        ToolSpec(
            name="get_collector_cost_summary",
            description=(
                "ADR-0011 volume metering: logical volume (events/bytes) entering vs "
                "leaving the pipeline per organization within the metering window — "
                "rows of {bytes_in, bytes_out, events_in, events_out, "
                "out_in_byte_ratio, reduction_pct}. Read-only, org-scoped "
                "(fail-closed). USD cost fields only appear when the Enterprise "
                "cost_pricer is active; on Community the ratio reflects envelope/"
                "fan-out overhead, not deliberate savings."
            ),
            input_schema=_object(properties={}),
            handler=_get_collector_cost_summary,
        ),
    ]
