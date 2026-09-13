from __future__ import annotations

from typing import Any

from ._base import (
    CentralOpsClient,
    ToolSpec,
    _integer,
    _object,
    _string,
)


async def _list_integrations(
    client: CentralOpsClient,
    *,
    organization_id: int | None = None,
    platform: str | None = None,
    include_inactive: bool = False,
    name: str | None = None,
    page: int | None = None,
    size: int | None = None,
) -> Any:
    return await client.get(
        "/integrations/",
        params={
            "organization_id": organization_id,
            "platform": platform,
            "include_inactive": "true" if include_inactive else None,
            "name": name,
            "page": page,
            "size": size,
        },
    )


async def _get_integration(client: CentralOpsClient, *, integration_id: int) -> Any:
    return await client.get(f"/integrations/{integration_id}")


async def _get_integration_health(
    client: CentralOpsClient, *, integration_id: int
) -> Any:
    return await client.get(f"/integrations/{integration_id}/health")


async def _get_integration_overview(
    client: CentralOpsClient, *, integration_id: int
) -> Any:
    return await client.get(f"/integrations/{integration_id}/overview")


async def _list_supported_platforms(client: CentralOpsClient) -> Any:
    return await client.get("/integrations/platforms")


def specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="list_integrations",
            description=(
                "List CentralOps integrations (a vendor connection per row). Each item "
                "has a plugin-driven platform identifier (use list_supported_platforms "
                "for the current registry), is_active, auth_status, last_checked_at, "
                "last_successful_check_at, last_error and the capabilities the provider "
                "exposes. Use this to find integration_id values for the other tools. "
                "Unfiltered and unpaginated by default — pass page/size on large "
                "deployments."
            ),
            input_schema=_object(
                properties={
                    "organization_id": _integer(
                        "Optional organization filter.", minimum=1
                    ),
                    "platform": _string("Optional platform filter (registry id)."),
                    "include_inactive": {
                        "type": "boolean",
                        "description": "Include deactivated integrations (default false).",
                    },
                    "name": _string(
                        "Optional case-insensitive substring filter on integration name."
                    ),
                    "page": _integer("1-based page number.", minimum=1),
                    "size": _integer(
                        "Page size (1-200). Omitted = no pagination (all rows).",
                        minimum=1,
                        maximum=200,
                    ),
                },
            ),
            handler=_list_integrations,
        ),
        ToolSpec(
            name="get_integration",
            description=(
                "Get a single integration with its configuration. Config fields "
                "(client_id, tenant_id, URLs, usernames, verify_ssl) come back null "
                "unless the token has secret.read permission. Actual secrets "
                "(passwords, client_secret) are NEVER returned — for Wazuh, the "
                "manager_api_password_configured / indexer_password_configured "
                "booleans indicate presence."
            ),
            input_schema=_object(
                properties={
                    "integration_id": _integer("Integration id.", minimum=1),
                },
                required=["integration_id"],
            ),
            handler=_get_integration,
        ),
        ToolSpec(
            name="get_integration_health",
            description=(
                "Run the live health check for an integration. Returns the v2 "
                "envelope: {schema_version: 2, platform, last_collection_at, "
                "last_success_at, metrics[]} where each metric is data-driven "
                "{id, label, value, severity: ok|warn|critical|unknown} — vendor "
                "sub-components (e.g. Wazuh manager/indexer) appear as metric "
                "entries such as id='manager_status'. Use this to answer 'is this "
                "vendor reachable and healthy?'."
            ),
            input_schema=_object(
                properties={
                    "integration_id": _integer("Integration id.", minimum=1),
                },
                required=["integration_id"],
            ),
            handler=_get_integration_health,
        ),
        ToolSpec(
            name="get_integration_overview",
            description=(
                "Aggregated view of an integration: the serialized integration, live "
                "health (or an 'error' entry when the health check fails), and "
                "licensed_products for Sophos child tenants (null otherwise). Best "
                "single call to answer 'how is vendor X behaving right now?'. For "
                "per-stream collection state, use list_collection_state. To search "
                "the vendor's own event store, use the federated query tools "
                "(query jobs) instead — the legacy per-integration alerts listing "
                "was removed from the backend."
            ),
            input_schema=_object(
                properties={
                    "integration_id": _integer("Integration id.", minimum=1),
                },
                required=["integration_id"],
            ),
            handler=_get_integration_overview,
        ),
        ToolSpec(
            name="list_supported_platforms",
            description=(
                "List the platform identifiers the CentralOps backend can connect to "
                "(static — defined in the provider registry). Use to validate vendor "
                "names before creating an integration."
            ),
            input_schema=_object(properties={}),
            handler=_list_supported_platforms,
        ),
    ]
