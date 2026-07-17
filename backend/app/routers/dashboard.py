"""Dashboard router with scoped operational summaries (pipeline-first)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional, Sequence

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session, selectinload

from ..core import auth as app_auth
from ..core import tenant
from ..core.errors import ApiError
from ..db import database, models, repository
from ..schemas.dashboard import BucketItem, BucketSection, DashboardSummaryV2, KpiCard

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

_DEGRADED_STATUSES = {"degraded", "error"}
_DEGRADED_STATUS_RANK = {"error": 0, "degraded": 1, "unknown": 2}


def _metric_comparison(current: int, previous: int) -> dict[str, Any]:
    delta = current - previous
    if delta > 0:
        trend = "up"
    elif delta < 0:
        trend = "down"
    else:
        trend = "stable"
    return {
        "current": current,
        "previous": previous,
        "delta": delta,
        "trend": trend,
    }


def _resolve_dashboard_scope(
    *,
    organization_id: int | None,
    integration_id: int | None,
    platform: str | None,
    repo: repository.IntegrationRepository,
    current_user: models.AppUser,
) -> tuple[int | None, models.Integration | None, str | None]:
    effective_organization_id = organization_id
    effective_platform = platform
    target_integration: models.Integration | None = None

    if organization_id is not None:
        tenant.require_subtree_access(current_user, organization_id)

    # Platform filter: no up-front rejection — unknown platforms silently return
    # empty results via the integration query filter below. The collector registry
    # is not a closed list: platforms like "wazuh" have integrations in the DB
    # but no CollectorRegistration (manager-side). Rejecting them here would break
    # backwards-compat and valid tenants.
    _ = platform  # referenced below in effective_platform; no validation needed

    if integration_id is not None:
        target_integration = repo.get(integration_id)
        if not target_integration:
            raise ApiError(
                "integration.not_found",
                404,
                messages={
                    "pt": "Integração não encontrada.",
                    "en": "Integration not found.",
                    "es": "Integración no encontrada.",
                },
            )
        tenant.require_subtree_access(current_user, target_integration.organization_id)
        if organization_id is not None and target_integration.organization_id != organization_id:
            raise ApiError(
                "dashboard.integration_org_mismatch",
                409,
                messages={
                    "pt": "A integração não pertence à organização selecionada.",
                    "en": "Integration does not belong to the selected organization.",
                    "es": "La integración no pertenece a la organización seleccionada.",
                },
            )
        if platform is not None and target_integration.platform != platform:
            raise ApiError(
                "dashboard.integration_platform_mismatch",
                409,
                messages={
                    "pt": "A integração não corresponde à plataforma selecionada.",
                    "en": "Integration does not match the selected platform.",
                    "es": "La integración no coincide con la plataforma seleccionada.",
                },
            )
        effective_organization_id = target_integration.organization_id
        effective_platform = target_integration.platform

    return effective_organization_id, target_integration, effective_platform


def _collect_integration_health(
    integrations: Sequence[models.Integration],
    *,
    health_repo: repository.IntegrationHealthRepository,
    comparison_anchor: datetime,
) -> dict[str, Any]:
    platform_counts: dict[str, int] = {}
    healthy_count = 0
    degraded_count = 0
    error_count = 0
    unknown_count = 0
    inactive_count = 0
    previous_degraded_count = 0
    degraded_items: list[dict[str, Any]] = []

    if not integrations:
        return {
            "platform_counts": platform_counts,
            "healthy_count": 0,
            "degraded_count": 0,
            "error_count": 0,
            "unknown_count": 0,
            "inactive_count": 0,
            "degraded_items": [],
            "previous_degraded_count": 0,
        }

    integration_ids = [integration.id for integration in integrations]
    latest_map = health_repo.get_latest_bulk(integration_ids)
    previous_map = health_repo.get_latest_before_bulk(integration_ids, comparison_anchor)

    for integration in integrations:
        platform_counts[integration.platform] = platform_counts.get(integration.platform, 0) + 1

        # Integrações inativas saem das contagens de saúde — vão para o bucket próprio.
        if not integration.is_active:
            inactive_count += 1
            continue

        latest = latest_map.get(integration.id)
        if latest is None:
            unknown_count += 1
        elif latest.status == "healthy":
            healthy_count += 1
        elif latest.status == "degraded":
            degraded_count += 1
            degraded_items.append(
                {
                    "integration_id": integration.id,
                    "integration_name": integration.name,
                    "organization_id": integration.organization_id,
                    "organization_name": integration.organization.name if integration.organization else None,
                    "status": latest.status,
                    "last_error": integration.last_error,
                    "last_checked_at": integration.last_checked_at,
                }
            )
        elif latest.status == "error":
            error_count += 1
            degraded_items.append(
                {
                    "integration_id": integration.id,
                    "integration_name": integration.name,
                    "organization_id": integration.organization_id,
                    "organization_name": integration.organization.name if integration.organization else None,
                    "status": latest.status,
                    "last_error": integration.last_error,
                    "last_checked_at": integration.last_checked_at,
                }
            )
        else:
            unknown_count += 1

        previous = previous_map.get(integration.id)
        if previous and previous.status in _DEGRADED_STATUSES:
            previous_degraded_count += 1

    degraded_items.sort(
        key=lambda item: (
            _DEGRADED_STATUS_RANK.get(str(item.get("status", "")).lower(), 99),
            -(item.get("last_checked_at").timestamp() if item.get("last_checked_at") else 0),
            str(item.get("integration_name") or ""),
        )
    )

    return {
        "platform_counts": platform_counts,
        "healthy_count": healthy_count,
        "degraded_count": degraded_count,
        "error_count": error_count,
        "unknown_count": unknown_count,
        "inactive_count": inactive_count,
        "degraded_items": degraded_items,
        "previous_degraded_count": previous_degraded_count,
    }


def _days_to_window(days: int) -> str:
    """Map the ``days`` query param to a v2 window label."""
    if days <= 1:
        return "24h"
    if days <= 7:
        return "7d"
    return "30d"


# ── Pipeline-funnel data collection ──────────────────────────────────────────
# Two-phase: DB phase (before db.close) + Redis phase (after db.close).


def _collect_funnel_db(
    integrations: Sequence[models.Integration],
    db: Session,
    *,
    scope_org_id: int | None,
    global_scope: bool,
) -> Dict[str, Any]:
    """Phase 1 — DB queries for pipeline funnel KPIs.

    ``scope_org_id``/``global_scope`` vêm do usuário corrente: usuário escopado
    (``global_scope=False``) só agrega destinos/rotas/DLQ da própria org — fecha
    o leak cross-tenant de nomes de destino/rota e métricas no funil.

    Must be called BEFORE ``db.expunge_all() / db.close()``.
    Returns a dict with:
    - ``ph_items``: list of per-integration pipeline-health snapshots (dicts)
    - ``dest_rows``: list of (id, name, kind, enabled, org_id) tuples for destinations
    - ``dest_dlq``: dict[dest_id → {dlq_24h, dlq_total}]
    - ``route_rows``: list of (id, name, action, dest_ids_json) tuples
    """
    from ..routers.pipeline_health import compute_pipeline_health

    ph_items: List[Dict[str, Any]] = []
    for intg in integrations:
        if not intg.is_active:
            continue
        try:
            health = compute_pipeline_health(db, int(intg.id))
            ph_items.append({
                "integration_id": int(intg.id),
                "integration_name": intg.name,
                "organization_name": intg.organization.name if intg.organization else None,
                "events_per_minute": health.events_per_minute,
                "mapped_field_ratio": health.mapped_field_ratio,
                "quarantine_count_24h": health.quarantine_count_24h,
                "drift_count_24h": health.drift_count_24h,
            })
        except Exception as exc:
            logger.warning(
                "dashboard funnel: falha ao coletar pipeline_health para integration=%s: %s",
                intg.id,
                exc,
            )

    # Destinations — ID, name, kind, enabled (para health/EPS pós-close)
    try:
        dest_repo = repository.DestinationRepository(db)
        raw_dests = dest_repo.list(scope_org_id, include_disabled=True, offset=0, limit=200, global_scope=global_scope)
        dest_rows = [
            {
                "id": str(d.id),
                "name": str(d.name),
                "kind": str(d.kind),
                "enabled": bool(d.enabled),
            }
            for d in raw_dests
        ]
        dest_dlq: Dict[str, Dict[str, Any]] = {}
        for d in raw_dests:
            try:
                stats = dest_repo.dlq_stats(str(d.id), org_id=scope_org_id, global_scope=global_scope)
                dest_dlq[str(d.id)] = stats
            except Exception:
                dest_dlq[str(d.id)] = {"dlq_total": 0, "dlq_24h": 0, "last_dlq_at": None}
    except Exception as exc:
        logger.warning("dashboard funnel: falha ao coletar destinations: %s", exc)
        dest_rows = []
        dest_dlq = {}

    # Routes — lê do DB para ter nomes e actions
    try:
        route_repo = repository.RouteRepository(db)
        raw_routes = route_repo.list(scope_org_id, include_disabled=True, global_scope=global_scope, limit=500)
        route_rows = [
            {
                "id": str(r.id),
                "name": str(r.name),
                "action": str(r.action),
                "destination_ids": json.loads(str(r.destination_ids or "[]")),
            }
            for r in raw_routes
        ]
    except Exception as exc:
        logger.warning("dashboard funnel: falha ao coletar routes: %s", exc)
        route_rows = []

    return {
        "ph_items": ph_items,
        "dest_rows": dest_rows,
        "dest_dlq": dest_dlq,
        "route_rows": route_rows,
    }


def _collect_funnel_redis(db_data: Dict[str, Any]) -> Dict[str, Any]:
    """Phase 2 — sync Redis reads from observability_store (safe after db.close()).

    Augments ``db_data`` with EPS/bytes per destination and
    matched/routed/dropped per route (60-min window from native store).
    Returns a combined dict ready for ``build_dashboard_summary_v2``.
    """
    try:
        from ..collectors import observability_store as obs
    except Exception as exc:
        logger.warning("dashboard funnel: observability_store indisponível: %s", exc)
        return {**db_data, "dest_eps": {}, "route_metrics": {}}

    # EPS per destination (sync obs read)
    dest_eps: Dict[str, float] = {}
    for d in db_data.get("dest_rows", []):
        did = d["id"]
        try:
            eps = obs.read_window_rate("dest", did, "sent", minutes=60)
            dest_eps[did] = round(float(eps or 0), 4)
        except Exception:
            dest_eps[did] = 0.0

    # Route metrics (matched/routed/dropped) per route (sync obs read)
    route_metrics: Dict[str, Dict[str, float]] = {}
    for r in db_data.get("route_rows", []):
        rid = r["id"]
        try:
            matched = obs.read_window_total("route", rid, "matched", minutes=60)
            routed = obs.read_window_total("route", rid, "route", minutes=60)
            dropped = obs.read_window_total("route", rid, "drop", minutes=60)
            route_metrics[rid] = {
                "matched_per_min": round((float(matched or 0)) / 60.0, 4),
                "routed_per_min": round((float(routed or 0)) / 60.0, 4),
                "drop_per_min": round((float(dropped or 0)) / 60.0, 4),
            }
        except Exception:
            route_metrics[rid] = {"matched_per_min": 0.0, "routed_per_min": 0.0, "drop_per_min": 0.0}

    return {**db_data, "dest_eps": dest_eps, "route_metrics": route_metrics}


def _days_to_window_lit(days: int) -> Literal["24h", "7d", "30d"]:
    """Same as ``_days_to_window`` but with a Literal return type for Pydantic."""
    if days <= 1:
        return "24h"
    if days <= 7:
        return "7d"
    return "30d"


def build_dashboard_summary_v2(
    *,
    summary: Dict[str, Any],
    days: int,
    generated_at: datetime,
    funnel_data: Optional[Dict[str, Any]] = None,
) -> DashboardSummaryV2:
    """Build the (single) DashboardSummaryV2 payload.

    ``summary`` carries the org/integration counts + health collected by the
    handler (``organizations`` / ``integrations`` keys). ``funnel_data``
    (optional) carries the pipeline-funnel metrics collected by
    ``_collect_funnel_db`` + ``_collect_funnel_redis``.  When absent (e.g. old
    tests that pre-date the funnel) the KPIs degrade gracefully to 0/"—".
    """
    ints = summary.get("integrations", {})

    fd: Dict[str, Any] = funnel_data or {}
    ph_items: List[Dict[str, Any]] = fd.get("ph_items", [])
    dest_rows: List[Dict[str, Any]] = fd.get("dest_rows", [])
    dest_dlq: Dict[str, Dict[str, Any]] = fd.get("dest_dlq", {})
    dest_eps: Dict[str, float] = fd.get("dest_eps", {})
    route_rows: List[Dict[str, Any]] = fd.get("route_rows", [])
    route_metrics: Dict[str, Dict[str, float]] = fd.get("route_metrics", {})

    # ── Funnel KPI computations ────────────────────────────────────────────
    # 1. Ingest EPS
    total_epm = sum(
        float(item.get("events_per_minute") or 0) for item in ph_items
    )
    ingest_eps = round(total_epm / 60.0, 1)

    # 2. Mapping coverage (avg ratio, only for integrations that have a ratio)
    ratios = [
        float(item["mapped_field_ratio"])
        for item in ph_items
        if item.get("mapped_field_ratio") is not None
    ]
    _Sev = Optional[Literal["ok", "warn", "critical", "info"]]

    if ratios:
        avg_ratio = sum(ratios) / len(ratios)
        mapping_pct_val = f"{avg_ratio * 100:.0f}%"
        mapping_severity: _Sev = "ok" if avg_ratio >= 0.80 else "warn"
    else:
        avg_ratio = 1.0
        mapping_pct_val = "—"
        mapping_severity = None

    # 3. Quarantine rate (24h)
    total_quarantine_24h = sum(
        int(item.get("quarantine_count_24h") or 0) for item in ph_items
    )
    # EPS * 60 * 60 * 24 → events_24h estimate; fallback to absolute count
    events_24h_est = total_epm * 60 * 24  # events in 24h from per-minute rate
    if events_24h_est > 0:
        qrate = 100.0 * total_quarantine_24h / events_24h_est
        quarantine_val: str | int = f"{qrate:.1f}%"
        quarantine_sub = "taxa 24h"
        quarantine_severity: _Sev = (
            "critical" if qrate > 5 else "warn" if qrate > 1 else "ok"
        )
    else:
        quarantine_val = total_quarantine_24h
        quarantine_sub = "eventos 24h"
        quarantine_severity = (
            "critical" if total_quarantine_24h > 100
            else "warn" if total_quarantine_24h > 0
            else "ok"
        )

    # 4. Routed events per min / drop rate
    total_matched = sum(
        m.get("matched_per_min", 0.0)
        for m in route_metrics.values()
    )
    total_routed = sum(
        m.get("routed_per_min", 0.0)
        for m in route_metrics.values()
    )
    total_dropped = sum(
        m.get("drop_per_min", 0.0)
        for m in route_metrics.values()
    )
    if total_matched > 0:
        drop_rate = 100.0 * total_dropped / total_matched
    else:
        drop_rate = 0.0
    routed_val = round(total_routed)
    routed_sub = f"{drop_rate:.1f}% drop"
    routed_severity: _Sev = "warn" if drop_rate > 5 else "ok"

    # 5. Destinations health
    total_dests = len(dest_rows)
    healthy_dests = 0
    for d in dest_rows:
        did = d["id"]
        dlq_24h = int((dest_dlq.get(did) or {}).get("dlq_24h", 0) or 0)
        # Healthy: enabled + no DLQ in 24h (breaker unknown tolerated for dashboard)
        if d.get("enabled") and dlq_24h == 0:
            healthy_dests += 1
    dests_unhealthy = total_dests - healthy_dests
    dests_severity: _Sev = "critical" if dests_unhealthy > 0 else "ok"
    dests_val = f"{healthy_dests}/{total_dests}"

    # 6. Active collector sources (from collector summary via integrations list in v1)
    active_sources_total = int(ints.get("active", 0) or 0)
    # "with_errors" proxied from v1 degraded+error health count
    health_v1 = ints.get("health", {})
    sources_with_errors = (
        int(health_v1.get("degraded", 0) or 0)
        + int(health_v1.get("error", 0) or 0)
    )
    sources_severity: _Sev = "critical" if sources_with_errors > 0 else "ok"

    # ── KPIs (funnel-first, vendor-neutral) ───────────────────────────────
    kpis: list[KpiCard] = [
        KpiCard(
            id="ingest_eps",
            label="Ingestão (EPS)",
            value=ingest_eps,
            sub="eventos/s",
            icon_id="activity",
            severity="ok" if ingest_eps > 0 else None,
        ),
        KpiCard(
            id="mapping_coverage",
            label="Cobertura de mapping",
            value=mapping_pct_val,
            icon_id="check",
            severity=mapping_severity,
        ),
        KpiCard(
            id="quarantine_rate",
            label="Quarentena 24h",
            value=quarantine_val,
            sub=quarantine_sub,
            icon_id="shield-alert",
            severity=quarantine_severity,
        ),
        KpiCard(
            id="routed_events",
            label="Roteados (/min)",
            value=routed_val,
            sub=routed_sub,
            icon_id="network",
            severity=routed_severity,
        ),
        KpiCard(
            id="destinations_healthy",
            label="Destinos",
            value=dests_val,
            sub="saudáveis",
            icon_id="cloud",
            severity=dests_severity,
        ),
        KpiCard(
            id="active_sources",
            label="Fontes ativas",
            value=active_sources_total,
            sub=f"{sources_with_errors} com erro",
            icon_id="server",
            severity=sources_severity,
        ),
    ]

    # ── Top Buckets (vendor-neutral) ──────────────────────────────────────
    # 1. Top sources by volume (integrations ordered by epm desc)
    top_sources_items: list[BucketItem] = [
        BucketItem(
            id=str(item["integration_id"]),
            label=str(item["integration_name"]),
            value=round(float(item.get("events_per_minute") or 0), 1),
            sub=item.get("organization_name"),
        )
        for item in sorted(
            ph_items,
            key=lambda x: -(float(x.get("events_per_minute") or 0)),
        )[:5]
        if (float(item.get("events_per_minute") or 0)) > 0
    ]

    # 2. Top destinations by volume (by EPS desc)
    top_dest_sorted = sorted(
        dest_rows, key=lambda d: -(dest_eps.get(d["id"], 0.0))
    )[:5]
    top_dests_items: list[BucketItem] = [
        BucketItem(
            id=d["id"],
            label=d["name"],
            value=round(dest_eps.get(d["id"], 0.0), 2),
            sub=d["kind"],
        )
        for d in top_dest_sorted
        if dest_eps.get(d["id"], 0.0) > 0
    ]

    # 3. Top quarantine by integration (24h desc)
    top_quarantine_items: list[BucketItem] = [
        BucketItem(
            id=str(item["integration_id"]),
            label=str(item["integration_name"]),
            value=int(item.get("quarantine_count_24h") or 0),
            sub=item.get("organization_name"),
        )
        for item in sorted(
            ph_items,
            key=lambda x: -(int(x.get("quarantine_count_24h") or 0)),
        )[:5]
        if int(item.get("quarantine_count_24h") or 0) > 0
    ]

    # 4. Top routes by drop_per_min desc
    route_drop_sorted = sorted(
        [
            (r, route_metrics.get(r["id"], {}).get("drop_per_min", 0.0))
            for r in route_rows
        ],
        key=lambda t: -t[1],
    )[:5]
    top_route_drops_items: list[BucketItem] = [
        BucketItem(
            id=r["id"],
            label=r["name"],
            value=round(drop, 2),
        )
        for r, drop in route_drop_sorted
        if drop > 0
    ]

    top_buckets: list[BucketSection] = [
        BucketSection(
            id="top_sources_volume",
            label="Top fontes por volume",
            items=top_sources_items,
            icon_id="activity",
            empty_hint="Sem dados de ingestão na janela atual.",
        ),
        BucketSection(
            id="top_destinations_volume",
            label="Top destinos por volume",
            items=top_dests_items,
            icon_id="cloud",
            # Empty-state ACIONÁVEL: zero aqui não é bug — indica ausência de tráfego
            # entregue na janela. Aponta o que o operador deve verificar.
            empty_hint=(
                "Sem eventos entregues a destinos na janela — verifique rotas ativas "
                "e destinos configurados."
            ),
        ),
        BucketSection(
            id="top_quarantine",
            label="Maiores quarentenas (24h)",
            items=top_quarantine_items,
            icon_id="shield-alert",
            empty_hint="Sem eventos em quarentena nas últimas 24h.",
        ),
        BucketSection(
            id="top_route_drops",
            label="Rotas com maior drop",
            items=top_route_drops_items,
            icon_id="network",
            empty_hint="Sem eventos descartados na janela atual.",
        ),
    ]

    return DashboardSummaryV2(
        window=_days_to_window_lit(days),
        generated_at=generated_at,
        kpis=kpis,
        top_buckets=top_buckets,
        organizations=summary.get("organizations") or {},
        integrations=ints or {},
    )


@router.get("/summary", response_model=DashboardSummaryV2)
def get_dashboard_summary(
    organization_id: int | None = None,
    integration_id: int | None = None,
    platform: str | None = None,
    days: int = Query(default=7, ge=1, le=90),
    db: Session = Depends(database.get_session),
    current_user: models.AppUser = Depends(app_auth.require_authenticated_user),
):
    """Sumário do dashboard: funil do pipeline + saúde das integrações.

    Retorna SEMPRE ``DashboardSummaryV2`` (payload único). O shape v1 legado
    (``Accept: application/vnd.centralops.v1+json``) e a agregação de alertas
    Wazuh-only foram removidos.
    """
    org_repo = repository.OrganizationRepository(db)
    int_repo = repository.IntegrationRepository(db)

    effective_organization_id, target_integration, effective_platform = _resolve_dashboard_scope(
        organization_id=organization_id,
        integration_id=integration_id,
        platform=platform,
        repo=int_repo,
        current_user=current_user,
    )

    scoped_org_ids = tenant.accessible_org_ids(current_user, db)
    include_inactive = tenant.is_admin(current_user)

    _int_q = (
        db.query(models.Integration)
        .options(selectinload(models.Integration.organization))
    )
    if not include_inactive:
        _int_q = _int_q.filter(models.Integration.is_active == True)  # noqa: E712
    if scoped_org_ids is not None:
        _int_q = _int_q.filter(models.Integration.organization_id.in_(scoped_org_ids))
    if effective_organization_id is not None:
        _int_q = _int_q.filter(models.Integration.organization_id == effective_organization_id)
    if effective_platform is not None:
        _int_q = _int_q.filter(models.Integration.platform == effective_platform)
    integrations = _int_q.order_by(models.Integration.name.asc()).all()
    if target_integration is not None:
        integrations = [integration for integration in integrations if integration.id == target_integration.id]

    orgs = org_repo.list(include_inactive=include_inactive, organization_ids=scoped_org_ids)
    if effective_organization_id is not None:
        orgs = [organization for organization in orgs if organization.id == effective_organization_id]
    elif effective_platform is not None or target_integration is not None:
        visible_org_ids = {integration.organization_id for integration in integrations}
        orgs = [organization for organization in orgs if organization.id in visible_org_ids]

    now_aware = datetime.now(timezone.utc)
    now_naive = datetime.utcnow()

    health_repo = repository.IntegrationHealthRepository(db)
    health_summary = _collect_integration_health(
        integrations,
        health_repo=health_repo,
        comparison_anchor=now_naive - timedelta(days=days),
    )

    # ── Phase 1: DB-only funnel data (before session close) ───────────────
    try:
        funnel_db = _collect_funnel_db(
            integrations,
            db,
            scope_org_id=current_user.organization_id,
            global_scope=tenant.has_global_scope(current_user),
        )
    except Exception as exc:
        logger.warning("dashboard funnel DB phase failed: %s", exc)
        funnel_db = {"ph_items": [], "dest_rows": [], "dest_dlq": {}, "route_rows": []}

    db.expunge_all()
    db.close()

    # ── Phase 2: Redis funnel data (after session close) ──────────────────
    try:
        funnel_data = _collect_funnel_redis(funnel_db)
    except Exception as exc:
        logger.warning("dashboard funnel Redis phase failed: %s", exc)
        funnel_data = {**funnel_db, "dest_eps": {}, "route_metrics": {}}

    summary: Dict[str, Any] = {
        "organizations": {
            "total": len(orgs),
            "active": sum(1 for organization in orgs if organization.is_active),
        },
        "integrations": {
            "total": len(integrations),
            "active": sum(1 for integration in integrations if integration.is_active),
            "authenticated": sum(1 for integration in integrations if integration.is_authenticated),
            "by_platform": health_summary["platform_counts"],
            "health": {
                "healthy": health_summary["healthy_count"],
                "degraded": health_summary["degraded_count"],
                "error": health_summary["error_count"],
                "unknown": health_summary["unknown_count"],
                "inactive": health_summary["inactive_count"],
            },
            "degraded_items": health_summary["degraded_items"],
            "comparison": {
                "degraded_integrations": _metric_comparison(
                    health_summary["degraded_count"] + health_summary["error_count"],
                    health_summary["previous_degraded_count"],
                )
            },
        },
    }

    return build_dashboard_summary_v2(
        summary=summary,
        days=days,
        generated_at=now_aware,
        funnel_data=funnel_data,
    )
