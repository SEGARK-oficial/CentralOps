from __future__ import annotations

from typing import Any

from ._base import (
    CentralOpsClient,
    ToolSpec,
    _integer,
    _object,
)

_NULL_REASON = (
    "Integration is not a child tenant of a Sophos Partner. "
    "Licenses only available for kind='tenant' with parent_integration_id."
)
_EMPTY_NOTE = (
    "Sophos API returned empty license list — tenant may not have any active "
    "licenses or API access is restricted."
)


async def _get_sophos_licenses(
    client: CentralOpsClient,
    *,
    integration_id: int,
) -> Any:
    if integration_id < 1:
        raise ValueError("integration_id must be >= 1")

    data = await client.get(f"/integrations/{integration_id}/overview")

    if not isinstance(data, dict):
        return data

    licensed_products = data.get("licensed_products")

    if licensed_products is None:
        return {"licensed_products": None, "reason": _NULL_REASON}

    if isinstance(licensed_products, list) and len(licensed_products) == 0:
        return {"licensed_products": [], "note": _EMPTY_NOTE}

    return {"licensed_products": licensed_products}


def specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="get_sophos_licenses",
            description=(
                "Get the list of Sophos products licensed for a child tenant integration. "
                "Use when investigating why a Sophos tenant fails to collect data "
                "(e.g. 403 on detections often means missing XDR/MDR license)."
            ),
            input_schema=_object(
                properties={
                    "integration_id": _integer("Integration id.", minimum=1),
                },
                required=["integration_id"],
            ),
            handler=_get_sophos_licenses,
        ),
    ]
