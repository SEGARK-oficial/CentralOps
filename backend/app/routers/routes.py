"""REST endpoints for Routes CRUD (motor de roteamento).

All admin-only, org-scoped (anti-enumeration). Every mutation appends a
RouteAuditLog row (governance + rollback). O roteamento é GA (modelo único, sem
flag): cada rota criada/editada aqui passa a valer no próximo ciclo de despacho
(vendor-neutral: wazuh-default é um Destination real, sem lane
especial; fallback resolve via is_default do org).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from collections import defaultdict

from ..api.schemas_routes import (
    FlowDestination,
    FlowGraphResponse,
    FlowRoute,
    FlowSource,
    FlowTotals,
    RouteAuditRead,
    RouteCreate,
    RouteDryRunRequest,
    RouteDryRunResponse,
    RouteDryRunResult,
    RouteHealthResponse,
    RouteMetricsResponse,
    RouteRead,
    RouteReorderRequest,
    RouteReorderResponse,
    RouteRollbackRequest,
    RouteUpdate,
    RoutingTopologyResponse,
    TopologyDestination,
    TopologyRoute,
)
from ..collectors.pipeline import _load_fallback_destination_id
from ..collectors.routing import (
    CompiledRoute,
    evaluate_event,
    event_labels,
    find_unreachable,
    order_routes,
)
from ..core import auth as app_auth
from ..core.errors import ApiError
from ..core import tenant
from ..core.tenant import has_global_scope
from ..db import database, models, repository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/collectors/routes", tags=["routes"])

# Vendor-neutral: o catch-all wazuh-default-catchall não é mais
# seedado automaticamente e não tem proteção especial — é um route normal, pode
# ser deletado ou reordenado pelo operador como qualquer outra rota.
_SYSTEM_ROUTE_IDS: frozenset[str] = frozenset()

# Janela (minutos) da média móvel de taxa em /flow (EPS de fontes/destinos,
# *_per_min de rotas). Configurável via OBS_RATE_WINDOW_MINUTES (default 5 = mais
# "tempo real"). NÃO se aplica ao endpoint de saúde de rota (matched_1h fixo em 1h).
from ..core.config import settings as _cfg  # noqa: E402

_RWIN = int(getattr(_cfg, "OBS_RATE_WINDOW_MINUTES", 5) or 5)


def _get_repo(db: Session = Depends(database.get_session)) -> repository.RouteRepository:
    return repository.RouteRepository(db)


def _get_dest_repo(db: Session = Depends(database.get_session)) -> repository.DestinationRepository:
    return repository.DestinationRepository(db)


def _resolve_scope(user: models.AppUser) -> tuple[bool, Optional[int]]:
    is_global = has_global_scope(user)
    raw_org = user.organization_id
    org_id: Optional[int] = int(raw_org) if raw_org is not None else None  # type: ignore[arg-type]
    return is_global, (org_id if not is_global else None)


def _assert_visible(row: models.Route | None, user: models.AppUser) -> models.Route:
    if row is None:
        raise ApiError(
            "route.not_found",
            status.HTTP_404_NOT_FOUND,
            messages={
                "pt": "Rota não encontrada.",
                "en": "Route not found.",
                "es": "Ruta no encontrada.",
            },
        )
    is_global, _ = _resolve_scope(user)
    if is_global:
        return row
    # Rota GLOBAL (org NULL) vale para todas as orgs — sempre visível.
    # Rota de org: usa o gate SUBTREE-AWARE, não igualdade exata. Com igualdade,
    # um admin de org PAI recebia 404 numa rota da FILHA mesmo com a hierarquia
    # materializada — divergindo de integrações, que já usavam require_subtree_access.
    # Em Community o resolver é FLAT e o resultado é idêntico ao de antes; sob
    # Enterprise a subárvore passa a valer, que é o contrato prometido.
    if row.organization_id is not None and not tenant.can_access_subtree(
        user, int(row.organization_id)
    ):
        raise ApiError(
            "route.not_found",
            status.HTTP_404_NOT_FOUND,
            messages={
                "pt": "Rota não encontrada.",
                "en": "Route not found.",
                "es": "Ruta no encontrada.",
            },
        )
    return row


def _assert_mutable(row: models.Route | None, user: models.AppUser) -> models.Route:
    """``_assert_visible`` + trava de ESCRITA: rota GLOBAL
    (``organization_id`` NULL) vale para TODAS as orgs — um admin-de-org a enxerga
    (participa do roteamento dele), mas editar/deletar/rollback afetaria todos os
    tenants; só admin de PLATAFORMA (escopo global) pode."""
    row = _assert_visible(row, user)
    is_global, _ = _resolve_scope(user)
    if not is_global and row.organization_id is None:
        raise ApiError(
            "route.global_requires_platform_admin",
            status.HTTP_403_FORBIDDEN,
            messages={
                "pt": "Rota global (compartilhada) só pode ser alterada por um administrador de plataforma.",
                "en": "A global (shared) route can only be changed by a platform administrator.",
                "es": "Una ruta global (compartida) solo puede ser modificada por un administrador de plataforma.",
            },
        )
    return row


def _row_to_read(row: models.Route, *, unreachable: bool = False) -> RouteRead:
    return RouteRead(
        id=str(row.id),
        name=str(row.name),
        priority=int(row.priority),
        condition=json.loads(str(row.condition or "{}")),
        action=str(row.action),
        destination_ids=json.loads(str(row.destination_ids or "[]")),
        is_final=bool(row.is_final),
        canary_percent=int(row.canary_percent),
        transform_ref=row.transform_ref,  # type: ignore[arg-type]
        pii_redaction=(
            json.loads(str(row.pii_redaction))
            if getattr(row, "pii_redaction", None)
            else None
        ),
        protect_detection=bool(row.protect_detection),
        sample_percent=int(row.sample_percent),
        suppress_key=row.suppress_key,  # type: ignore[arg-type]
        suppress_allow=int(row.suppress_allow),
        suppress_window_s=int(row.suppress_window_s),
        drop_raw=bool(getattr(row, "drop_raw", False) or False),
        enabled=bool(row.enabled),
        organization_id=int(row.organization_id) if row.organization_id is not None else None,
        created_at=row.created_at,  # type: ignore[arg-type]
        updated_at=row.updated_at,  # type: ignore[arg-type]
        unreachable=unreachable,
    )


def _compile(row: models.Route) -> CompiledRoute:
    return CompiledRoute(
        id=str(row.id),
        name=str(row.name),
        priority=int(row.priority),
        condition=json.loads(str(row.condition or "{}")),
        action=str(row.action),
        destination_ids=tuple(json.loads(str(row.destination_ids or "[]"))),
        is_final=bool(row.is_final),
        enabled=bool(row.enabled),
        canary_percent=int(row.canary_percent),
    )


def _validate_destinations_exist(
    dest_ids: List[str],
    dest_repo: repository.DestinationRepository,
    *,
    caller_org: Optional[int] = None,
    is_global: bool = True,
) -> None:
    """422 if any destination_id does not resolve to a destination VISÍVEL ao
    caller. wazuh-default é agora uma row real (org=NULL global)
    e passa pela mesma checagem que qualquer outro destino.

    ``DestinationRepository.get`` filtra só por id (sem org), ao contrário de
    ``.list`` (org-scoped). Sem o escopo aqui, um caller não-global poderia
    referenciar — e, pelo eco de ``destination_ids`` em ``GET /routes/topology``,
    ENUMERAR — destinos de OUTRA org. Defesa-em-profundidade: hoje os endpoints
    de mutação exigem admin (sempre global, ``has_global_scope``), mas papéis
    org-scoped com permissão de rota tornariam o gap explorável. Um destino
    cross-org responde o MESMO 422 "not found" de um inexistente — não revela
    a existência (fecha o vetor de enumeração)."""
    for did in dest_ids:
        dest = dest_repo.get(did)
        # Visível = existe E (caller global, OU destino global, OU mesma org).
        visible = dest is not None and (
            is_global
            or dest.organization_id is None
            or dest.organization_id == caller_org
        )
        if not visible:
            raise ApiError(
                "route.destination_not_found",
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                messages={
                    "pt": "destino {destination_id!r} não encontrado",
                    "en": "destination {destination_id!r} not found",
                    "es": "destino {destination_id!r} no encontrado",
                },
                params={"destination_id": did},
            )


# ── GET "" (list, with unreachable flags) ─────────────────────────────


@router.get("", response_model=List[RouteRead])
def list_routes(
    include_disabled: bool = Query(default=True),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=500),
    user: models.AppUser = Depends(app_auth.require_admin_user),
    repo: repository.RouteRepository = Depends(_get_repo),
) -> List[RouteRead]:
    is_global, caller_org = _resolve_scope(user)
    # Escopo SUBTREE-AWARE (reusa a sessão do repo): um admin de org PAI passa a
    # enxergar as rotas das FILHAS. Antes o filtro era igualdade exata e a tela
    # de Rotas era estruturalmente incapaz de mostrá-las, mesmo com o resolver
    # Enterprise registrado — divergindo do /flow, que já lista as FONTES da
    # subárvore. Em Community o resolver é FLAT e o resultado não muda.
    _org_ids = None if is_global else tenant.accessible_org_ids(user, repo.db)
    rows = repo.list(
        caller_org,
        include_disabled=include_disabled,
        global_scope=is_global,
        offset=offset,
        limit=limit,
        org_ids=_org_ids,
    )
    # Compute unreachable over the FULL ordered visible set (not just this page's
    # slice) for an accurate UX guard — re-list without pagination bound.
    full = repo.list(
        caller_org, include_disabled=include_disabled, global_scope=is_global,
        limit=500, org_ids=_org_ids,
    )
    unreachable_ids = set(find_unreachable(order_routes([_compile(r) for r in full])))
    return [_row_to_read(r, unreachable=str(r.id) in unreachable_ids) for r in rows]


# ── POST "" (create) ──────────────────────────────────────────────────


@router.post("", response_model=RouteRead, status_code=status.HTTP_201_CREATED)
def create_route(
    payload: RouteCreate,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    repo: repository.RouteRepository = Depends(_get_repo),
    dest_repo: repository.DestinationRepository = Depends(_get_dest_repo),
) -> RouteRead:
    is_global, caller_org = _resolve_scope(user)
    org_id = payload.organization_id
    if not is_global and org_id is not None and org_id != caller_org:
        raise ApiError(
            "route.cross_org_create_denied",
            status.HTTP_403_FORBIDDEN,
            messages={
                "pt": "Não é possível criar rota para outra organização",
                "en": "Cannot create route for another organization",
                "es": "No es posible crear una ruta para otra organización",
            },
        )
    if not is_global and org_id is None and caller_org is not None:
        org_id = caller_org

    _validate_destinations_exist(
        payload.destination_ids, dest_repo, caller_org=caller_org, is_global=is_global
    )

    row = repo.add(
        name=payload.name,
        condition=payload.condition,
        destination_ids=payload.destination_ids,
        action=payload.action,
        is_final=payload.is_final,
        priority=payload.priority,
        enabled=payload.enabled,
        canary_percent=payload.canary_percent,
        transform_ref=payload.transform_ref,
        pii_redaction=payload.pii_redaction,
        protect_detection=payload.protect_detection,
        sample_percent=payload.sample_percent,
        suppress_key=payload.suppress_key,
        suppress_allow=payload.suppress_allow,
        suppress_window_s=payload.suppress_window_s,
        drop_raw=payload.drop_raw,
        organization_id=org_id,
        actor=user.username,
    )
    logger.info("routes: created id=%s name=%r by=%s", row.id, row.name, user.username)
    return _row_to_read(row)


# ── GET /topology (flow graph w/ throughput) ──
# NOTE: declared BEFORE GET /{route_id} so the literal "topology" segment is
# matched by THIS handler and not captured as a route_id. FastAPI resolves by
# declaration order; ``test_topology_route_does_not_collide_with_id`` guards it.
# Final URL: GET /api/collectors/routes/topology.


@router.get("/topology", response_model=RoutingTopologyResponse)
async def routing_topology(
    user: models.AppUser = Depends(app_auth.require_admin_user),
    repo: repository.RouteRepository = Depends(_get_repo),
    dest_repo: repository.DestinationRepository = Depends(_get_dest_repo),
) -> RoutingTopologyResponse:
    """Flow topology source→route→destination for the observability UI,
    org-scoped, 60-min window.

    - Routes: per-minute averages of matched/routed/dropped over the last 60 min,
      derived from the SAME native-store per-route series as ``/{id}/metrics``
      (matched/route/drop). ``is_system`` flags the seeded catch-all.
    - Destinations: ``status``/``eps``/``bytes_per_min`` reuse the destination
      health helper (same logic as ``GET /collectors/destinations/health``).

    Org-scope/visibility is identical to ``list_routes``/``list_destinations``:
    non-global callers see only their org's routes/destinations + global rows.
    """
    from ..collectors import observability_store as obs
    from .destinations import _compute_destination_health

    is_global, caller_org = _resolve_scope(user)

    # ── Routes (org-scoped, includes disabled so the UI can grey them out) ──
    route_rows = repo.list(
        caller_org, include_disabled=True, global_scope=is_global, limit=500
    )
    async def _route_node(r: models.Route) -> TopologyRoute:
        rid = str(r.id)
        # As 3 leituras por rota correm em paralelo; falha degrada o throughput
        # dessa rota a 0 sem derrubar a resposta (Redis indisponível).
        try:
            matched, routed, dropped = await asyncio.gather(
                asyncio.to_thread(obs.read_window_total, "route", rid, "matched", minutes=_RWIN),
                asyncio.to_thread(obs.read_window_total, "route", rid, "route", minutes=_RWIN),
                asyncio.to_thread(obs.read_window_total, "route", rid, "drop", minutes=_RWIN),
            )
        except Exception:  # pragma: no cover — caminho defensivo
            matched = routed = dropped = 0.0
        return TopologyRoute(
            id=rid,
            name=str(r.name),
            action=str(r.action),
            destination_ids=json.loads(str(r.destination_ids or "[]")),
            matched_per_min=round((matched or 0) / float(_RWIN), 4),
            routed_per_min=round((routed or 0) / float(_RWIN), 4),
            drop_per_min=round((dropped or 0) / float(_RWIN), 4),
            enabled=bool(r.enabled),
            is_system=rid in _SYSTEM_ROUTE_IDS,
        )

    # Paraleliza por rota (cada uma faz 3 leituras Redis): evita o N+1 serial.
    topo_routes: List[TopologyRoute] = list(
        await asyncio.gather(*[_route_node(r) for r in route_rows])
    )

    # ── Destinations (same org-scope as list_destinations; include disabled) ──
    dest_rows = dest_repo.list(
        caller_org,
        include_disabled=True,
        offset=0,
        limit=200,
        global_scope=is_global,
    )

    async def _dest_node(d: models.Destination) -> TopologyDestination:
        try:
            health = await _compute_destination_health(
                d, org_id=caller_org, global_scope=is_global, repo=dest_repo
            )
            return TopologyDestination(
                id=health["destination_id"],
                name=health["name"],
                kind=health["kind"],
                status=health["status"],
                eps=health["eps"],
                bytes_per_min=health["bytes_per_min"],
            )
        except Exception:  # pragma: no cover — caminho defensivo
            return TopologyDestination(
                id=str(d.id), name=d.name, kind=d.kind, status="unknown"
            )

    topo_dests: List[TopologyDestination] = list(
        await asyncio.gather(*[_dest_node(d) for d in dest_rows])
    )

    return RoutingTopologyResponse(destinations=topo_dests, routes=topo_routes)


# ── GET /flow (full flow graph: sources + routes + destinations + totals) ─
# NOTE: declared BEFORE GET /{route_id} so the literal "flow" segment is
# matched by THIS handler and not captured as a route_id path parameter.
# Final URL: GET /api/collectors/routes/flow.


def _pipeline_status_to_flow(status: str) -> str:
    """Map pipeline-health status values to FlowSource canonical set.

    pipeline-health already uses the same four values, so this is a pass-through
    with a fallback for any unexpected string (defensive, forward-compat).
    """
    if status in {"healthy", "degraded", "unhealthy", "unknown"}:
        return status
    return "unknown"


@router.get("/flow", response_model=FlowGraphResponse)
async def flow_graph(
    user: models.AppUser = Depends(app_auth.require_admin_user),
    repo: repository.RouteRepository = Depends(_get_repo),
    dest_repo: repository.DestinationRepository = Depends(_get_dest_repo),
    db: Session = Depends(database.get_session),
) -> FlowGraphResponse:
    """Full flow graph for the /flow page: SOURCES → ROUTES → DESTINATIONS with
    live volume and health.  Org-scoped, 60-min window, admin-only.

    Sources (integrations visible to the caller) use pipeline-health metrics
    (events_per_minute from the 5-min Redis snapshot, status from DB indicators).
    Routes and destinations reuse the same helpers as ``GET /topology``.

    Each subsystem is collected in try/except: Redis down → 0.0 throughput;
    DB error on sources → empty list.  The response never returns 500.

    Session-safety note: ``compute_pipeline_health`` is synchronous and runs in
    ``asyncio.to_thread``.  Each source node opens its OWN ``SessionLocal``
    context so there is no concurrent access to the shared injected ``db`` from
    worker threads (SQLAlchemy Session is not thread-safe).  The injected ``db``
    is used only for the integration-id enumeration query, which completes before
    any thread is spawned.
    """
    from datetime import datetime, timezone

    from ..collectors import observability_store as obs
    from ..routers.pipeline_health import (
        IntegrationPipelineHealth,
        _get_events_per_minute,
        compute_pipeline_health,
    )
    from ..routers.destinations import _compute_destination_health
    from sqlalchemy import select
    from ..core import tenant as _tenant

    import redis.asyncio as redis_async
    from ..core.config import settings as _settings

    is_global, caller_org = _resolve_scope(user)
    generated_at = datetime.now(timezone.utc).isoformat()

    # ── SOURCES (integrations org-scoped) ─────────────────────────────
    flow_sources: List[FlowSource] = []
    try:
        integration_query = select(models.Integration).where(
            models.Integration.is_active.is_(True)
        )
        org_ids = _tenant.accessible_org_ids(user, db)
        if org_ids is not None:
            integration_query = integration_query.where(
                models.Integration.organization_id.in_(org_ids)
            )
        # Collect id+name+platform only (primitive scalars) from the shared db
        # session BEFORE spawning threads — no Session handed to worker threads.
        integrations_raw: List[tuple[int, str, str]] = [
            (int(row.id), str(row.name), str(row.platform))
            for row in db.execute(integration_query).scalars().all()
        ]

        # Open a single async Redis client for all EPM lookups (shared, closed below).
        _redis_source: Optional[Any] = None
        try:
            _redis_source = redis_async.from_url(
                _settings.REDIS_URL or "redis://localhost:6379/0",
                decode_responses=True,
            )
        except Exception:
            pass

        async def _source_node(
            integ_id: int, integ_name: str, integ_platform: str
        ) -> Optional[FlowSource]:
            try:
                epm: Optional[float] = None
                if _redis_source is not None:
                    try:
                        # _get_events_per_minute needs a DB session — open ephemeral.
                        with database.SessionLocal() as _epm_db:
                            epm = await _get_events_per_minute(
                                _redis_source, _epm_db, integ_id
                            )
                    except Exception:
                        pass

                def _compute_sync() -> IntegrationPipelineHealth:
                    with database.SessionLocal() as _ph_db:
                        return compute_pipeline_health(
                            _ph_db, integ_id, events_per_minute=epm
                        )

                health: IntegrationPipelineHealth = await asyncio.to_thread(_compute_sync)
                # EPS de ingestão: PREFERE o counter nativo de source
                # (obs:source:{id}:ingested, gravado na ingestão do pipeline —
                # real-time, independente do path de coleta). Cai no snapshot
                # 5-min do pipeline-health só quando o counter está zerado (sem
                # tráfego na janela, ou imagem sem a instrumentação). Isto evita
                # o /flow mostrar 0 EPS enquanto o snapshot ainda "esquenta".
                src_eps = await asyncio.to_thread(
                    obs.read_window_rate, "source", str(integ_id), "ingested", minutes=_RWIN
                )
                if src_eps and src_eps > 0:
                    eps_val = round(float(src_eps), 6)
                    epm_out = round(float(src_eps) * 60.0, 4)
                else:
                    epm_val = float(health.events_per_minute or 0.0)
                    eps_val = round(epm_val / 60.0, 6)
                    epm_out = round(epm_val, 4)
                return FlowSource(
                    id=str(integ_id),
                    name=integ_name,
                    platform=integ_platform,
                    status=_pipeline_status_to_flow(health.status),
                    events_per_minute=epm_out,
                    eps=eps_val,
                )
            except Exception:
                logger.debug(
                    "flow: falha ao calcular source node integration_id=%s — ignorado",
                    integ_id,
                    exc_info=True,
                )
                return None

        source_results = await asyncio.gather(
            *[_source_node(iid, iname, iplatform) for iid, iname, iplatform in integrations_raw]
        )
        flow_sources = [s for s in source_results if s is not None]

        if _redis_source is not None:
            try:
                await _redis_source.aclose()
            except Exception:
                pass
    except Exception:
        logger.warning("flow: falha ao coletar sources — degradando para []", exc_info=True)

    # ── ROUTES (reusa lógica idêntica ao /topology) ────────────────────
    flow_routes: List[FlowRoute] = []
    try:
        route_rows = repo.list(
            caller_org, include_disabled=True, global_scope=is_global, limit=500
        )

        async def _flow_route_node(r: models.Route) -> FlowRoute:
            rid = str(r.id)
            try:
                matched, routed, dropped = await asyncio.gather(
                    asyncio.to_thread(obs.read_window_total, "route", rid, "matched", minutes=_RWIN),
                    asyncio.to_thread(obs.read_window_total, "route", rid, "route", minutes=_RWIN),
                    asyncio.to_thread(obs.read_window_total, "route", rid, "drop", minutes=_RWIN),
                )
            except Exception:
                matched = routed = dropped = 0.0
            return FlowRoute(
                id=rid,
                name=str(r.name),
                action=str(r.action),
                destination_ids=json.loads(str(r.destination_ids or "[]")),
                matched_per_min=round((matched or 0) / float(_RWIN), 4),
                routed_per_min=round((routed or 0) / float(_RWIN), 4),
                drop_per_min=round((dropped or 0) / float(_RWIN), 4),
                enabled=bool(r.enabled),
                is_system=rid in _SYSTEM_ROUTE_IDS,
            )

        flow_routes = list(await asyncio.gather(*[_flow_route_node(r) for r in route_rows]))
    except Exception:
        logger.warning("flow: falha ao coletar routes — degradando para []", exc_info=True)

    # ── DESTINATIONS (reusa _compute_destination_health) ───────────────
    flow_dests: List[FlowDestination] = []
    try:
        dest_rows = dest_repo.list(
            caller_org,
            include_disabled=True,
            offset=0,
            limit=200,
            global_scope=is_global,
        )

        async def _flow_dest_node(d: models.Destination) -> FlowDestination:
            try:
                health = await _compute_destination_health(
                    d, org_id=caller_org, global_scope=is_global, repo=dest_repo
                )
                return FlowDestination(
                    id=health["destination_id"],
                    name=health["name"],
                    kind=health["kind"],
                    status=health["status"],
                    eps=health["eps"],
                    bytes_per_min=health["bytes_per_min"],
                )
            except Exception:
                return FlowDestination(
                    id=str(d.id), name=str(d.name), kind=str(d.kind), status="unknown"
                )

        flow_dests = list(await asyncio.gather(*[_flow_dest_node(d) for d in dest_rows]))
    except Exception:
        logger.warning("flow: falha ao coletar destinations — degradando para []", exc_info=True)

    # ── TOTALS ─────────────────────────────────────────────────────────
    totals = FlowTotals(
        ingest_eps=round(sum(s.eps for s in flow_sources), 6),
        routed_per_min=round(sum(r.routed_per_min for r in flow_routes), 4),
        drop_per_min=round(sum(r.drop_per_min for r in flow_routes), 4),
        delivered_eps=round(sum(d.eps or 0.0 for d in flow_dests), 6),
    )

    return FlowGraphResponse(
        generated_at=generated_at,
        window_minutes=_RWIN,
        sources=flow_sources,
        routes=flow_routes,
        destinations=flow_dests,
        totals=totals,
    )


# ── GET /{id} ─────────────────────────────────────────────────────────


@router.get("/{route_id}", response_model=RouteRead)
def get_route(
    route_id: str,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    repo: repository.RouteRepository = Depends(_get_repo),
) -> RouteRead:
    row = _assert_visible(repo.get(route_id), user)
    return _row_to_read(row)


# ── PUT /{id} ─────────────────────────────────────────────────────────


@router.put("/{route_id}", response_model=RouteRead)
def update_route(
    route_id: str,
    payload: RouteUpdate,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    repo: repository.RouteRepository = Depends(_get_repo),
    dest_repo: repository.DestinationRepository = Depends(_get_dest_repo),
) -> RouteRead:
    existing = _assert_mutable(repo.get(route_id), user)
    is_global, caller_org = _resolve_scope(user)

    # Reassign de org: admin escopado não pode mover a rota para outra org
    # (mesma regra do create — cross_org_create_denied).
    if (
        payload.organization_id is not None
        and not is_global
        and payload.organization_id != caller_org
    ):
        raise ApiError(
            "route.cross_org_update_denied",
            status.HTTP_403_FORBIDDEN,
            messages={
                "pt": "Não é possível mover a rota para outra organização",
                "en": "Cannot move the route to another organization",
                "es": "No es posible mover la ruta a otra organización",
            },
        )

    # Resolve the effective (action, destination_ids) post-update for validation.
    eff_action = payload.action if payload.action is not None else str(existing.action)
    eff_dests = (
        payload.destination_ids
        if payload.destination_ids is not None
        else json.loads(str(existing.destination_ids or "[]"))
    )
    if eff_action == "route" and not eff_dests:
        raise ApiError(
            "route.action_route_requires_destination",
            422,
            messages={
                "pt": "a ação 'route' exige ao menos um destination_id",
                "en": "action 'route' requires at least one destination_id",
                "es": "la acción 'route' requiere al menos un destination_id",
            },
        )
    if eff_action == "drop" and eff_dests:
        raise ApiError(
            "route.action_drop_forbids_destination",
            422,
            messages={
                "pt": "a ação 'drop' não deve ter destination_ids",
                "en": "action 'drop' must not have destination_ids",
                "es": "la acción 'drop' no debe tener destination_ids",
            },
        )
    if payload.destination_ids is not None:
        _validate_destinations_exist(
            payload.destination_ids, dest_repo, caller_org=caller_org, is_global=is_global
        )

    # ``suppress_key`` é o único campo novo nullable-com-significado: ausente
    # (não enviado) = mantém; ``null`` EXPLÍCITO = limpa a chave de supressão.
    # ``model_fields_set`` (Pydantic v2) distingue "não enviado" de "enviado
    # como null" — sem ele, ``payload.suppress_key is not None`` colapsaria os
    # dois casos em _UNSET e um clear explícito nunca aplicaria (bug conhecido:
    # ver CorrelationRuleRepository.update). Os outros 4 campos novos são
    # colunas NOT NULL (sem estado "limpo"), então o idiom padrão
    # ``is not None`` já usado por ``is_final``/``enabled``/``canary_percent``
    # é suficiente — e preserva o fail-safe: ausência de ``protect_detection``
    # nunca vira False, só um True/False EXPLÍCITO é aplicado.
    _suppress_key_update = (
        payload.suppress_key
        if "suppress_key" in payload.model_fields_set
        else repository._UNSET
    )
    updated = repo.update(
        route_id,
        name=payload.name if payload.name is not None else repository._UNSET,
        priority=payload.priority if payload.priority is not None else repository._UNSET,
        condition=payload.condition if payload.condition is not None else repository._UNSET,
        action=payload.action if payload.action is not None else repository._UNSET,
        destination_ids=payload.destination_ids if payload.destination_ids is not None else repository._UNSET,
        is_final=payload.is_final if payload.is_final is not None else repository._UNSET,
        canary_percent=payload.canary_percent if payload.canary_percent is not None else repository._UNSET,
        transform_ref=payload.transform_ref if payload.transform_ref is not None else repository._UNSET,
        pii_redaction=payload.pii_redaction if payload.pii_redaction is not None else repository._UNSET,
        protect_detection=payload.protect_detection if payload.protect_detection is not None else repository._UNSET,
        sample_percent=payload.sample_percent if payload.sample_percent is not None else repository._UNSET,
        suppress_key=_suppress_key_update,
        suppress_allow=payload.suppress_allow if payload.suppress_allow is not None else repository._UNSET,
        suppress_window_s=payload.suppress_window_s if payload.suppress_window_s is not None else repository._UNSET,
        drop_raw=payload.drop_raw if payload.drop_raw is not None else repository._UNSET,
        enabled=payload.enabled if payload.enabled is not None else repository._UNSET,
        organization_id=payload.organization_id if payload.organization_id is not None else repository._UNSET,
        actor=user.username,
    )
    if updated is None:
        raise ApiError(
            "route.not_found",
            status.HTTP_404_NOT_FOUND,
            messages={
                "pt": "Rota não encontrada.",
                "en": "Route not found.",
                "es": "Ruta no encontrada.",
            },
        )
    logger.info("routes: updated id=%s by=%s", route_id, user.username)
    return _row_to_read(updated)


# ── DELETE /{id} ──────────────────────────────────────────────────────


@router.delete("/{route_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_route(
    route_id: str,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    repo: repository.RouteRepository = Depends(_get_repo),
) -> None:
    # _SYSTEM_ROUTE_IDS is empty — guard is a no-op kept for
    # forward-compatibility if a new system route is ever added.
    if route_id in _SYSTEM_ROUTE_IDS:
        raise ApiError(
            "route.system_route_immutable",
            status.HTTP_409_CONFLICT,
            messages={
                "pt": "Esta rota de sistema não pode ser removida.",
                "en": "This system route cannot be removed.",
                "es": "Esta ruta de sistema no puede eliminarse.",
            },
        )
    _assert_mutable(repo.get(route_id), user)
    repo.delete(route_id, actor=user.username)
    logger.info("routes: deleted id=%s by=%s", route_id, user.username)


# ── POST /dry-run (preview routing before saving) ────────────────
# NOTE: declared before /{route_id}/... to avoid 'dry-run' being read as an id.


async def _resolve_samples(
    payload: RouteDryRunRequest, caller_org: Optional[int]
) -> tuple[list[dict], str]:
    """Return (sample envelopes, source). Caller-provided wins; else pull recent
    dispatched envelopes from the org audit buffer (best-effort)."""
    if payload.samples is not None:
        return list(payload.samples), "provided"
    if caller_org is None:
        return [], "none"  # global admin must provide samples (no single org)
    try:
        from ..collectors import audit_buffer
        from ..collectors.celery_app import get_worker_redis

        redis = get_worker_redis()
        try:
            recent = await audit_buffer.read_recent(redis, caller_org, limit=payload.sample_size)
        finally:
            try:
                await redis.aclose()
            except Exception:  # pragma: no cover
                pass
        # read_recent entries are {event, envelope, syslog_format} wrappers; the
        # canonical envelope (with _centralops labels) is under "event". Unwrap it
        # so the routing engine sees real labels (otherwise every
        # recent event reads _centralops off the wrapper → {} → all fallback).
        return [e.get("event", e) for e in (recent or [])], "audit_buffer"
    except Exception:
        logger.warning("routes/dry-run: falha ao ler audit buffer (org=%s)", caller_org, exc_info=True)
        return [], "none"


@router.post("/dry-run", response_model=RouteDryRunResponse)
async def dry_run_routes(
    payload: RouteDryRunRequest,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    repo: repository.RouteRepository = Depends(_get_repo),
) -> RouteDryRunResponse:
    """Evaluate a candidate (or the saved) route set against sample events WITHOUT
    enqueuing anything — shows, per event, where it would land + which routes are
    unreachable. The safe-ops preview before a cutover."""
    is_global, caller_org = _resolve_scope(user)

    if payload.routes is not None:
        compiled = [
            CompiledRoute(
                id=f"candidate-{i}",
                name=r.name,
                priority=r.priority,
                condition=r.condition,
                action=r.action,
                destination_ids=tuple(r.destination_ids),
                is_final=r.is_final,
                enabled=r.enabled,
                canary_percent=r.canary_percent,
            )
            for i, r in enumerate(payload.routes)
        ]
    else:
        compiled = [_compile(r) for r in repo.list_enabled_for_org(caller_org)]

    ordered = order_routes(compiled)
    unreachable = find_unreachable(ordered)

    samples, source = await _resolve_samples(payload, caller_org)

    results: list[RouteDryRunResult] = []
    routed = dropped = fallback = 0
    per_dest: dict[str, int] = defaultdict(int)
    for env in samples:
        labels = event_labels(env)
        decision = evaluate_event(labels, ordered)
        if decision.dropped:
            dropped += 1
            results.append(RouteDryRunResult(labels=labels, destinations=[], dropped=True, fallback=False))
            continue
        if not decision.destinations:
            fb_dest_id = _load_fallback_destination_id(caller_org)
            if fb_dest_id is not None:
                fallback += 1
                per_dest[fb_dest_id] += 1
                results.append(RouteDryRunResult(labels=labels, destinations=[fb_dest_id], dropped=False, fallback=True))
            else:
                fallback += 1
                results.append(RouteDryRunResult(labels=labels, destinations=[], dropped=False, fallback=True))
            continue
        routed += 1
        for d in decision.destinations:
            per_dest[d] += 1
        results.append(
            RouteDryRunResult(labels=labels, destinations=sorted(decision.destinations), dropped=False, fallback=False)
        )

    return RouteDryRunResponse(
        evaluated=len(samples),
        sample_source=source,
        routed=routed,
        dropped=dropped,
        fallback=fallback,
        per_destination=dict(per_dest),
        unreachable_route_ids=unreachable,
        results=results,
    )


# ── POST /reorder (drag-reorder priorities in bulk) ─────────
# NOTE: declared before /{route_id}/... to avoid 'reorder' being treated
# as a route_id path parameter.


@router.post("/reorder", response_model=RouteReorderResponse, status_code=status.HTTP_200_OK)
def reorder_routes(
    payload: RouteReorderRequest,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    repo: repository.RouteRepository = Depends(_get_repo),
) -> RouteReorderResponse:
    """Atomically reassign priorities to the provided ordered list of route_ids.

    The caller supplies the full ordered list (e.g. from a drag-drop UI).
    Priorities are reassigned as multiples of 10 (10, 20, 30 …) in that order,
    leaving gaps for future inserts without immediate conflicts.

    Constraints:
      - All route_ids must belong to the caller's org (anti-enumeration).
        Any id outside the caller's scope returns 403.
      - If any route_id does not exist, 404 is returned and NO priorities are
        changed (the commit is rolled back).
      - Each reassignment appends a ``reorder`` audit row (same audit trail as
        ``updated``/``rolled_back``).

    Org-scoped + admin-only (same RBAC as all other route endpoints).
    """
    # _SYSTEM_ROUTE_IDS is empty — guard is a no-op kept for
    # forward-compatibility if a new system route is ever added.
    if _SYSTEM_ROUTE_IDS.intersection(payload.route_ids):
        raise ApiError(
            "route.system_route_immutable",
            status.HTTP_409_CONFLICT,
            messages={
                "pt": "Uma rota de sistema está incluída — não pode ser reordenada.",
                "en": "A system route is included — it cannot be reordered.",
                "es": "Se incluyó una ruta de sistema — no puede reordenarse.",
            },
        )
    is_global, caller_org = _resolve_scope(user)

    try:
        updated_rows = repo.reorder_routes(
            payload.route_ids,
            org_id=caller_org,
            global_scope=is_global,
            actor=user.username,
        )
    except ValueError as exc:
        raise ApiError(
            "route.not_found",
            status.HTTP_404_NOT_FOUND,
            messages={
                "pt": "Rota não encontrada: {error}",
                "en": "Route not found: {error}",
                "es": "Ruta no encontrada: {error}",
            },
            params={"error": str(exc)},
        ) from exc
    except PermissionError as exc:
        raise ApiError(
            "route.cross_org_reorder_denied",
            status.HTTP_403_FORBIDDEN,
            messages={
                "pt": "Rota fora do escopo da organização do solicitante: {error}",
                "en": "Route outside the caller's organization scope: {error}",
                "es": "Ruta fuera del alcance de la organización del solicitante: {error}",
            },
            params={"error": str(exc)},
        ) from exc

    logger.info(
        "routes: reorder %d routes by=%s org_id=%s",
        len(updated_rows),
        user.username,
        caller_org,
    )
    return RouteReorderResponse(reordered=[_row_to_read(r) for r in updated_rows])


# ── POST /{id}/rollback (restore a prior audit snapshot) ──────────────


@router.post("/{route_id}/rollback", response_model=RouteRead)
def rollback_route(
    route_id: str,
    payload: RouteRollbackRequest,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    repo: repository.RouteRepository = Depends(_get_repo),
    dest_repo: repository.DestinationRepository = Depends(_get_dest_repo),
) -> RouteRead:
    """Restore a route to a prior snapshot from its audit trail. Records a
    'rolled_back' audit entry (the trail stays append-only)."""
    _assert_mutable(repo.get(route_id), user)
    is_global, caller_org = _resolve_scope(user)

    audit = repo.get_audit(payload.audit_id)
    if audit is None or str(audit.route_id) != route_id:
        raise ApiError(
            "route.audit_not_found",
            status.HTTP_404_NOT_FOUND,
            messages={
                "pt": "Entrada de auditoria não encontrada para esta rota",
                "en": "Audit entry not found for this route",
                "es": "Entrada de auditoría no encontrada para esta ruta",
            },
        )

    snap = json.loads(str(audit.snapshot or "{}"))

    # Re-validate the restored destinations: a snapshot may point at
    # a since-deleted destination. The dispatcher's zero-loss net would DLQ those
    # events, but failing the rollback with 422 is the clearer signal.
    if snap.get("action") == "route":
        _validate_destinations_exist(
            snap.get("destination_ids", []), dest_repo, caller_org=caller_org, is_global=is_global
        )
    # Re-valida a spec de redação do snapshot: create/update
    # validam via Pydantic, mas o rollback escrevia o snapshot verbatim — fecha o
    # gap de defesa-em-profundidade (FAIL-CLOSED na escrita).
    _snap_red = snap.get("pii_redaction")
    if _snap_red:
        from ..collectors.routing import validate_pii_redaction

        try:
            validate_pii_redaction(_snap_red)
        except Exception as exc:
            raise ApiError(
                "route.snapshot_pii_redaction_invalid",
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                messages={
                    "pt": "Snapshot pii_redaction inválida: {error}",
                    "en": "Invalid snapshot pii_redaction: {error}",
                    "es": "pii_redaction del snapshot no válida: {error}",
                },
                params={"error": str(exc)},
            ) from exc
    updated = repo.update(
        route_id,
        name=snap.get("name", repository._UNSET),
        priority=snap.get("priority", repository._UNSET),
        condition=snap.get("condition", repository._UNSET),
        action=snap.get("action", repository._UNSET),
        destination_ids=snap.get("destination_ids", repository._UNSET),
        is_final=snap.get("is_final", repository._UNSET),
        canary_percent=snap.get("canary_percent", repository._UNSET),
        transform_ref=snap.get("transform_ref", repository._UNSET),
        pii_redaction=snap.get("pii_redaction", repository._UNSET),
        protect_detection=snap.get("protect_detection", repository._UNSET),
        sample_percent=snap.get("sample_percent", repository._UNSET),
        suppress_key=snap.get("suppress_key", repository._UNSET),
        suppress_allow=snap.get("suppress_allow", repository._UNSET),
        suppress_window_s=snap.get("suppress_window_s", repository._UNSET),
        drop_raw=snap.get("drop_raw", repository._UNSET),
        enabled=snap.get("enabled", repository._UNSET),
        actor=user.username,
        audit_action="rolled_back",
    )
    if updated is None:
        raise ApiError(
            "route.not_found",
            status.HTTP_404_NOT_FOUND,
            messages={
                "pt": "Rota não encontrada.",
                "en": "Route not found.",
                "es": "Ruta no encontrada.",
            },
        )
    logger.info("routes: rolled back id=%s to audit=%s by=%s", route_id, payload.audit_id, user.username)
    return _row_to_read(updated)


# ── GET /{id}/metrics (per-route observability) ──────────────


@router.get("/{route_id}/metrics", response_model=RouteMetricsResponse)
async def route_metrics(
    route_id: str,
    range_minutes: int = Query(default=60, ge=5, le=1440),
    user: models.AppUser = Depends(app_auth.require_admin_user),
    repo: repository.RouteRepository = Depends(_get_repo),
) -> RouteMetricsResponse:
    """Per-route time-series (matched/route/drop events per minute) from the
    native store — the routing observability the UI shows without Prometheus."""
    _assert_visible(repo.get(route_id), user)

    from ..collectors import observability_store as obs

    series = await asyncio.to_thread(
        obs.read_series, "route", route_id, ["matched", "route", "drop"], minutes=range_minutes
    )
    return RouteMetricsResponse(route_id=route_id, series=series)


@router.get("/{route_id}/health", response_model=RouteHealthResponse)
async def route_health(
    route_id: str,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    repo: repository.RouteRepository = Depends(_get_repo),
) -> RouteHealthResponse:
    """Per-route health (paridade rota↔destino): EPS de eventos
    casados + taxa de drop na última 1h, do store nativo (sem Prometheus)."""
    row = _assert_visible(repo.get(route_id), user)

    from ..collectors import observability_store as obs

    matched = await asyncio.to_thread(
        obs.read_window_total, "route", route_id, "matched", minutes=60
    )
    routed = await asyncio.to_thread(
        obs.read_window_total, "route", route_id, "route", minutes=60
    )
    dropped = await asyncio.to_thread(
        obs.read_window_total, "route", route_id, "drop", minutes=60
    )
    enabled = bool(row.enabled)
    status_str = "disabled" if not enabled else ("healthy" if matched > 0 else "idle")
    return RouteHealthResponse(
        route_id=route_id,
        status=status_str,
        enabled=enabled,
        matched_eps=round(matched / 3600.0, 4),
        matched_1h=int(matched),
        routed_1h=int(routed),
        dropped_1h=int(dropped),
        drop_rate=round(dropped / matched, 4) if matched else 0.0,
    )


# ── GET /{id}/audit ───────────────────────────────────────────────────


@router.get("/{route_id}/audit", response_model=List[RouteAuditRead])
def route_audit(
    route_id: str,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    repo: repository.RouteRepository = Depends(_get_repo),
) -> List[RouteAuditRead]:
    _assert_visible(repo.get(route_id), user)
    return [
        RouteAuditRead(
            id=str(a.id),
            route_id=str(a.route_id),
            action=str(a.action),
            actor=a.actor,  # type: ignore[arg-type]
            snapshot=json.loads(str(a.snapshot or "{}")),
            created_at=a.created_at,  # type: ignore[arg-type]
        )
        for a in repo.audit_trail(route_id)
    ]
