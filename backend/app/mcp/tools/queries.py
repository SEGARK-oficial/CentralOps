from __future__ import annotations

from typing import Any

from ._base import (
    CentralOpsClient,
    ToolSpec,
    _integer,
    _object,
    _string,
)


async def _list_scheduled_queries(client: CentralOpsClient) -> Any:
    # NB: trailing slash is significant — the backend registers GET /schedules/
    # and httpx does not follow 307 redirects.
    return await client.get("/schedules/")


async def _get_scheduled_query_history(
    client: CentralOpsClient,
    *,
    schedule_id: int,
) -> Any:
    return await client.get(f"/schedules/{schedule_id}/history")


async def _list_search_history(
    client: CentralOpsClient,
    *,
    client_id: int | None = None,
    schedule_id: int | None = None,
) -> Any:
    return await client.get(
        "/search/history",
        params={"client_id": client_id, "schedule_id": schedule_id},
    )


async def _get_search_result(
    client: CentralOpsClient,
    *,
    search_id: str,
) -> Any:
    return await client.get(f"/search/history/result/{search_id}")


async def _list_audit_log(
    client: CentralOpsClient,
    *,
    username: str | None = None,
    ip_address: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> Any:
    return await client.get(
        "/history/audit",
        params={
            "username": username,
            "ip_address": ip_address,
            "date_from": date_from,
            "date_to": date_to,
        },
    )


async def _get_query_capabilities(client: CentralOpsClient) -> Any:
    return await client.get("/providers/query-capabilities")


def specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="list_scheduled_queries",
            description=(
                "List scheduled queries (recurring saved-query executions). Each item "
                "has: id, organization_id, query_id, query_title, client_ids (the "
                "integration ids the schedule runs against), interval_value/"
                "interval_unit, lookback_value/lookback_unit, notify_on_results, "
                "days_back, next_run, last_run_at, created_at, updated_at. No filters "
                "or pagination — returns every schedule in scope. Requires the "
                "mapping.read permission; org-scoped users only see schedules of "
                "their own organization."
            ),
            input_schema=_object(properties={}),
            handler=_list_scheduled_queries,
        ),
        ToolSpec(
            name="get_scheduled_query_history",
            description=(
                "List past runs of one scheduled query, newest first. Each run is a "
                "search result with: id, search_id, client_id (integration id), "
                "schedule_id, status, statement, table, from_ts/to_ts (queried "
                "window), engine, language, result_count, error_message, result_json "
                "(raw results payload, may be large or null after retention pruning), "
                "created_at. Requires the mapping.read permission; returns 404 if the "
                "schedule does not exist or belongs to an organization outside your "
                "scope. Non-admin users additionally only see runs they executed."
            ),
            input_schema=_object(
                properties={
                    "schedule_id": _integer("Scheduled query id.", minimum=1),
                },
                required=["schedule_id"],
            ),
            handler=_get_scheduled_query_history,
        ),
        ToolSpec(
            name="list_search_history",
            description=(
                "List the search/query run history visible to the caller (both ad-hoc "
                "and scheduled runs), newest first. Each item has: id, search_id, "
                "client_id (integration id), schedule_id (null for ad-hoc runs), "
                "status, statement, table, from_ts/to_ts, engine, language, "
                "result_count, error_message, result_json, created_at. Non-admin "
                "users only see runs they executed; admin/global users see all runs "
                "within their organization scope. Expired entries are pruned "
                "server-side by the retention policy before listing; no pagination."
            ),
            input_schema=_object(
                properties={
                    "client_id": _integer(
                        "Optional — filter by integration id the search ran against.",
                        minimum=1,
                    ),
                    "schedule_id": _integer(
                        "Optional — filter to runs of a single scheduled query.",
                        minimum=1,
                    ),
                },
            ),
            handler=_list_search_history,
        ),
        ToolSpec(
            name="get_search_result",
            description=(
                "Fetch a single search run by its search_id (the string identifier, "
                "not the numeric row id). Returns the same fields as "
                "list_search_history — including the full result_json payload — for "
                "one run. Returns 404 if the search does not exist or is not visible "
                "to the caller (other user's run, or outside the organization scope)."
            ),
            input_schema=_object(
                properties={
                    "search_id": _string(
                        "Search id string as returned in the 'search_id' field of "
                        "list_search_history or get_scheduled_query_history."
                    ),
                },
                required=["search_id"],
            ),
            handler=_get_search_result,
        ),
        ToolSpec(
            name="list_audit_log",
            description=(
                "List API audit-log entries, newest first. Each entry has: id, "
                "user_id, username, user_role, action, endpoint, method, status_code, "
                "ip_address, user_agent, request_payload, detail, created_at. "
                "Filters: username (case-insensitive substring), ip_address "
                "(substring), date_from/date_to accepting 'YYYY-MM-DD' or an ISO 8601 "
                "datetime (a date-only date_to includes the whole day; unparseable "
                "values are silently ignored, not rejected). Returns at most the 500 "
                "most recent matching entries and there is no pagination — narrow "
                "with filters to avoid truncation. Requires an admin token "
                "(user.manage permission); returns 403 otherwise."
            ),
            input_schema=_object(
                properties={
                    "username": _string(
                        "Optional — case-insensitive substring match on the acting "
                        "username."
                    ),
                    "ip_address": _string(
                        "Optional — substring match on the client IP address."
                    ),
                    "date_from": _string(
                        "Optional — lower bound, 'YYYY-MM-DD' or ISO 8601 datetime "
                        "(e.g. '2026-07-01' or '2026-07-01T12:00:00Z')."
                    ),
                    "date_to": _string(
                        "Optional — upper bound, 'YYYY-MM-DD' (inclusive of the whole "
                        "day) or ISO 8601 datetime."
                    ),
                },
            ),
            handler=_list_audit_log,
        ),
        ToolSpec(
            name="get_query_capabilities",
            description=(
                "Catalog of query dialects supported by the installed source plugins, "
                "aggregated per dialect. Each item has: dialect, capability (e.g. "
                "'query:opensearch_dsl'), modes, supports_async, max_window_seconds, "
                "rate_limit, required_secrets, ocsf_mapping_version, spec_kinds, and "
                "supported_by (the platforms offering that dialect). Use this to know "
                "which sources can be queried and with which limits. This is static "
                "catalog metadata — whether a specific integration can actually run a "
                "query is still gated server-side by RBAC and organization scope. Any "
                "authenticated token can call it."
            ),
            input_schema=_object(properties={}),
            handler=_get_query_capabilities,
        ),
    ]
