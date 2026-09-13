from __future__ import annotations

from typing import Any

from ._base import (
    CentralOpsClient,
    ToolSpec,
    _integer,
    _object,
)


async def _list_detections(
    client: CentralOpsClient,
    *,
    status_filter: str | None = None,
    limit: int = 100,
) -> Any:
    return await client.get(
        "/detections",
        params={
            "status_filter": status_filter,
            "limit": limit,
        },
    )


async def _get_detection(
    client: CentralOpsClient,
    *,
    detection_id: int,
) -> Any:
    return await client.get(f"/detections/{detection_id}")


def specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="list_detections",
            description=(
                "List in-pipeline detection alerts in CentralOps, newest first "
                "(ordered by created_at desc). Detections are durable, first-class "
                "alerts emitted by scheduled queries, live queries and correlation "
                "rules (the 'source' field is 'scheduled_query', 'live_query' or "
                "'correlation'). Each item contains: id, organization_id, source, "
                "source_query_id, integration_id, dialect, rule_id, rule_name, "
                "severity_id (OCSF severity 0-6 or 99), status ('open', 'ack' or "
                "'closed'), dedup_key, count (repeat matches within the suppression "
                "window bump count instead of creating a new alert), "
                "suppression_window_seconds, first_seen, last_seen, "
                "search_result_id, ocsf_ref and created_at (ISO 8601 timestamps or "
                "null). Results are scoped fail-closed to the organizations the "
                "token can access (a token with no org access gets an empty list). "
                "The server clamps limit to 1-500. Read-only: this server does not "
                "expose status transitions (triage ack/close)."
            ),
            input_schema=_object(
                properties={
                    "status_filter": {
                        "type": "string",
                        "description": (
                            "Optional triage-status filter — 'open' (new, untriaged), "
                            "'ack' (acknowledged) or 'closed'."
                        ),
                        "enum": ["open", "ack", "closed"],
                    },
                    "limit": _integer(
                        "Maximum results (server clamps to 1-500, default 100).",
                        minimum=1,
                        maximum=500,
                    ),
                },
            ),
            handler=_list_detections,
        ),
        ToolSpec(
            name="get_detection",
            description=(
                "Fetch a single detection alert by numeric id. Returns the same "
                "fields as list_detections: id, organization_id, source "
                "('scheduled_query', 'live_query' or 'correlation'), "
                "source_query_id, integration_id, dialect, rule_id, rule_name, "
                "severity_id (OCSF 0-6 or 99), status ('open', 'ack' or 'closed'), "
                "dedup_key, count, suppression_window_seconds, first_seen, "
                "last_seen, search_result_id, ocsf_ref and created_at. Org-scoped "
                "fail-closed: returns 404 (detection.not_found) if the id does not "
                "exist or belongs to an organization the token cannot access."
            ),
            input_schema=_object(
                properties={
                    "detection_id": _integer("Detection id.", minimum=1),
                },
                required=["detection_id"],
            ),
            handler=_get_detection,
        ),
    ]
