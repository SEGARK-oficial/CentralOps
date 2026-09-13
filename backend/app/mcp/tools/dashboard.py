from __future__ import annotations

from typing import Any

from ._base import (
    CentralOpsClient,
    ToolSpec,
    _integer,
    _object,
    _string,
)


async def _get_dashboard_summary(
    client: CentralOpsClient,
    *,
    organization_id: int | None = None,
    integration_id: int | None = None,
    platform: str | None = None,
    days: int = 7,
) -> Any:
    return await client.get(
        "/dashboard/summary",
        params={
            "organization_id": organization_id,
            "integration_id": integration_id,
            "platform": platform,
            "days": days,
        },
    )


def specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="get_dashboard_summary",
            description=(
                "Situational-awareness summary — the same payload the CentralOps "
                "dashboard loads first (DashboardSummaryV2). Returns: schema_version "
                "(2), window ('24h' for days=1, '7d' for days 2-7, '30d' above), "
                "generated_at, kpis, top_buckets, organizations and integrations. "
                "kpis is a list of cards "
                "{id, label, value, sub?, icon_id?, trend?, trend_value?, severity?} "
                "with stable ids: ingest_eps, mapping_coverage, quarantine_rate, "
                "routed_events (with drop-rate sub), destinations_healthy "
                "('healthy/total'), active_sources. top_buckets is a list of ranking "
                "sections {id, label, items, icon_id?, empty_hint?} where items are "
                "{id, label, value, sub?, severity?, href?}; section ids: "
                "top_sources_volume, top_destinations_volume, top_quarantine, "
                "top_route_drops. organizations carries {total, active}; "
                "integrations carries {total, active, authenticated, by_platform, "
                "health: {healthy, degraded, error, unknown, inactive}, "
                "degraded_items[], comparison} — the scope/health counts formerly "
                "served by the removed v1 shape (the Wazuh-only alert analytics "
                "were removed entirely). Labels are localized (pt-BR). Results are "
                "scoped to organizations the token's user can access; passing "
                "organization_id outside that subtree returns 403, an unknown "
                "integration_id returns 404, and an integration_id that mismatches "
                "organization_id or platform returns 409. An unknown platform is "
                "not rejected — it just yields empty data. Use this as the first "
                "read in a triage session before drilling into collectors, "
                "quarantine, or destinations."
            ),
            input_schema=_object(
                properties={
                    "organization_id": _integer(
                        "Optional — restrict the summary to a single organization "
                        "(must be within the caller's accessible subtree, else 403).",
                        minimum=1,
                    ),
                    "integration_id": _integer(
                        "Optional — restrict to a single integration; also narrows "
                        "the effective organization and platform to that "
                        "integration's own.",
                        minimum=1,
                    ),
                    "platform": _string(
                        "Optional platform filter (e.g. 'sophos', 'wazuh'). Unknown "
                        "values return empty data rather than an error."
                    ),
                    "days": _integer(
                        "Lookback window in days (1-90, default 7). Mapped to the "
                        "response 'window' label: 1 -> '24h', 2-7 -> '7d', "
                        "8+ -> '30d'.",
                        minimum=1,
                        maximum=90,
                    ),
                },
            ),
            handler=_get_dashboard_summary,
        ),
    ]
