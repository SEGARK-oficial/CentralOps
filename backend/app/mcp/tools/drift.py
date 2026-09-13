from __future__ import annotations

from typing import Any

from ._base import (
    CentralOpsClient,
    ToolSpec,
    _integer,
    _object,
    _string,
)


async def _list_drift_fields(
    client: CentralOpsClient,
    *,
    vendor: str | None = None,
    event_type: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> Any:
    return await client.get(
        "/drift",
        params={
            "vendor": vendor,
            "event_type": event_type,
            "status": status,
            "limit": limit,
            "offset": offset,
        },
    )


def specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="list_drift_fields",
            description=(
                "List unknown fields detected by the drift sampler in CentralOps. "
                "Use this to triage which raw vendor fields are not yet captured by any "
                "mapping rule. Filters apply to the same scope a non-admin user already "
                "has access to (their organization's integrations)."
            ),
            input_schema=_object(
                properties={
                    "vendor": _string(
                        "Optional vendor filter (e.g. 'sophos', 'wazuh', 'microsoft_defender')."
                    ),
                    "event_type": _string(
                        "Optional event type filter (e.g. 'sophos.alert')."
                    ),
                    "status": {
                        "type": "string",
                        "description": (
                            "Status filter — 'new' (observed but not yet triaged), "
                            "'ignored', or 'mapped'."
                        ),
                        "enum": ["new", "ignored", "mapped"],
                    },
                    "limit": _integer(
                        "Maximum results (1-500, default 50).",
                        minimum=1,
                        maximum=500,
                    ),
                    "offset": _integer(
                        "Pagination offset (default 0).", minimum=0
                    ),
                },
            ),
            handler=_list_drift_fields,
        ),
    ]
