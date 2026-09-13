from __future__ import annotations

from typing import Any

from ..loopback import CentralOpsAPIError
from ._base import (
    CentralOpsClient,
    ToolSpec,
    _integer,
    _object,
    _string,
)


_MAX_BULK = 50


async def _list_quarantine(
    client: CentralOpsClient,
    *,
    vendor: str | None = None,
    event_type: str | None = None,
    error_kind: str | None = None,
    integration_id: int | None = None,
    status: str | None = None,
    integration_name: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> Any:
    return await client.get(
        "/quarantine",
        params={
            "vendor": vendor,
            "event_type": event_type,
            "error_kind": error_kind,
            "integration_id": integration_id,
            "status": status,
            "integration_name": integration_name,
            "limit": limit,
            "offset": offset,
        },
    )


async def _get_quarantine_event(
    client: CentralOpsClient,
    *,
    event_id: str,
) -> Any:
    return await client.get(f"/quarantine/{event_id}")


async def _reprocess_quarantine(
    client: CentralOpsClient,
    *,
    event_ids: list[str],
) -> Any:
    if not event_ids:
        raise ValueError("event_ids must contain at least one id")
    if len(event_ids) > _MAX_BULK:
        raise ValueError(
            f"event_ids has {len(event_ids)} entries — limit per call is {_MAX_BULK}. "
            "Split into multiple calls."
        )

    results: list[dict[str, Any]] = []
    summary = {"requested": len(event_ids), "succeeded": 0, "failed": 0}
    for event_id in event_ids:
        try:
            data = await client.post(f"/quarantine/{event_id}/reprocess")
            results.append({"event_id": event_id, "status": "ok", "data": data})
            summary["succeeded"] += 1
        except CentralOpsAPIError as exc:
            results.append(
                {
                    "event_id": event_id,
                    "status": "error",
                    "http_status": exc.status_code,
                    "message": str(exc),
                }
            )
            summary["failed"] += 1
    return {"summary": summary, "results": results}


def specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="get_quarantine_event",
            description=(
                "Fetch a single quarantined event including the full raw_payload as "
                "received from the vendor. THIS is the tool to answer 'what shape is "
                "this vendor actually sending?' or 'what specific field caused the "
                "mapping to fail?'. The raw payload is the original JSON the collector "
                "received before normalization."
            ),
            input_schema=_object(
                properties={
                    "event_id": _string("Quarantine event id (uuid)."),
                },
                required=["event_id"],
            ),
            handler=_get_quarantine_event,
        ),
        ToolSpec(
            name="list_quarantine",
            description=(
                "List quarantined events in CentralOps — raw events that failed "
                "normalization and are awaiting reprocess or discard. Filters by vendor, "
                "event_type, error_kind, integration_id."
            ),
            input_schema=_object(
                properties={
                    "vendor": _string("Optional vendor filter."),
                    "event_type": _string("Optional event type filter."),
                    "error_kind": _string(
                        "Optional error kind filter. Valid values: 'parse', 'map', "
                        "'validate', 'missing_mapping', 'missing_customer_id'. "
                        "(Routing failures like 'unrouted' live in the destination DLQ, "
                        "not here — see list_destination_dlq.)"
                    ),
                    "integration_id": _integer(
                        "Optional integration id filter.", minimum=1
                    ),
                    "status": {
                        "type": "string",
                        "description": (
                            "Lifecycle filter. Backend default is 'pending' (awaiting "
                            "reprocess); use 'reprocessed' or 'all' to see history."
                        ),
                        "enum": ["pending", "reprocessed", "all"],
                    },
                    "integration_name": _string(
                        "Optional case-insensitive substring filter on integration name."
                    ),
                    "limit": _integer(
                        "Maximum results (1-500, default 50).",
                        minimum=1,
                        maximum=500,
                    ),
                    "offset": _integer("Pagination offset (default 0).", minimum=0),
                },
            ),
            handler=_list_quarantine,
        ),
        ToolSpec(
            name="reprocess_quarantine",
            description=(
                "Reprocess one or more quarantined events: applies the current mapping "
                "to each raw payload and routes the produced envelope through the "
                "destination routing engine (first-match rules; unmatched events go to "
                "the default destination if configured, otherwise to the 'unrouted' "
                "DLQ). Idempotent at the backend level — events already reprocessed "
                "return 409; expired events return 410; mapping failures return 422. "
                f"Limit: {_MAX_BULK} events per call. Returns per-event status.\n\n"
                "DISPATCHES TO EXTERNAL SYSTEMS: a successfully reprocessed event "
                "is delivered to real destinations (syslog, SIEM, data lake) and "
                "cannot be recalled. Confirm the mapping is actually fixed — via "
                "dry_run_mapping — before reprocessing, or you will ship the same "
                "malformed events downstream. Only run on explicit human request."
            ),
            read_only=False,
            destructive=True,
            input_schema=_object(
                properties={
                    "event_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": _MAX_BULK,
                        "description": "Quarantine event ids to reprocess.",
                    },
                },
                required=["event_ids"],
            ),
            handler=_reprocess_quarantine,
        ),
    ]
