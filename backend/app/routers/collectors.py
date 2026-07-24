"""Collector Multi-Tenant — REST endpoints.

Expõe ao frontend o estado de coleta (``collection_state``), o registry
de vendors suportados e ações de manutenção (trigger manual, reset de
cursor). **Não** expõe credenciais ou cursor hot do Redis diretamente —
para isso o operador usa a CLI (``python -m app.collectors.cli``).

Scope multi-tenant: segue o mesmo padrão de outros routers —
``tenant.accessible_org_ids(current_user, db)`` decide o que o usuário
vê. Admins veem tudo; usuários veem apenas as integrações da própria
organização.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..api import schemas
from ..core import auth as app_auth
from ..core import ee_hooks
from ..core import tenant
from ..core.errors import ApiError
from ..db import database, models, repository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/collectors", tags=["collectors"])


# ── Dependencies ──────────────────────────────────────────────────────


def get_state_repo(
    db: Session = Depends(database.get_session),
) -> repository.CollectionStateRepository:
    return repository.CollectionStateRepository(db)


def get_integration_repo(
    db: Session = Depends(database.get_session),
) -> repository.IntegrationRepository:
    return repository.IntegrationRepository(db)


# ── Helpers ───────────────────────────────────────────────────────────


def _load_registry():
    """Import tardio do registry.

    Evita carregar Celery/aiohttp quando o collector não está habilitado
    (ex: testes do FastAPI puro). Se o subsistema não estiver disponível,
    devolvemos um registry vazio para manter os endpoints funcionais.
    """
    try:
        from ..collectors.registry import all_registrations

        return list(all_registrations())
    except Exception as exc:  # pragma: no cover — defensivo
        logger.warning("collectors: registry indisponível: %s", exc)
        return []


def _serialize_state(
    row: models.CollectionState,
    integration_by_id: Dict[int, models.Integration],
) -> schemas.CollectionStateRead:
    integration = integration_by_id.get(row.integration_id)

    cursor_parsed: Optional[Dict[str, Any]] = None
    if row.cursor:
        try:
            parsed = json.loads(row.cursor)
            if isinstance(parsed, dict):
                cursor_parsed = parsed
        except json.JSONDecodeError:
            cursor_parsed = {"_raw": row.cursor[:200]}

    return schemas.CollectionStateRead(
        integration_id=row.integration_id,
        integration_name=integration.name if integration else None,
        organization_id=integration.organization_id if integration else None,
        organization_name=(
            integration.organization.name
            if integration and integration.organization
            else None
        ),
        platform=integration.platform if integration else None,
        stream=row.stream,
        cursor=cursor_parsed,
        last_success_at=row.last_success_at,
        last_attempt_at=row.last_attempt_at,
        # Precisa ser copiado à mão: este serializador monta o schema por kwargs,
        # então `from_attributes` não alcança as colunas novas — omiti-las aqui
        # entregaria `null`/`false` fixos e a coluna de atraso real da tela de
        # Coletores nasceria morta.
        watermark_at=row.watermark_at,
        last_run_capped=bool(row.last_run_capped),
        last_error=row.last_error,
        consecutive_failures=row.consecutive_failures or 0,
        events_collected_total=row.events_collected_total or 0,
        updated_at=row.updated_at,
    )


# ── Vendors registrados ──────────────────────────────────────────────


@router.get("/vendors", response_model=List[schemas.CollectorVendorRead])
def list_vendors(
    _: models.AppUser = Depends(app_auth.require_authenticated_user),
) -> List[schemas.CollectorVendorRead]:
    """Lista ``(platform, stream)`` atualmente registrados no collector.

    Útil para a UI mostrar quais vendors podem ser coletados e com que
    cadência — dado essencialmente estático (define-se no código), mas
    exposto via API para evitar divergência frontend/backend.
    """
    registrations = _load_registry()
    return [
        schemas.CollectorVendorRead(
            platform=reg.platform,
            stream=reg.stream,
            queue=reg.queue,
            task_name=reg.task_name,
            schedule_seconds=int(reg.schedule.total_seconds()),
        )
        for reg in registrations
    ]


@router.get(
    "/platforms-streams",
    response_model=schemas.PlatformStreamsResponse,
    summary="Auto-discovery de platforms e streams disponíveis",
)
def get_platforms_streams(
    _: models.AppUser = Depends(app_auth.require_authenticated_user),
) -> schemas.PlatformStreamsResponse:
    """Retorna map agregado ``platform → [streams]`` para o frontend.

    Substitui o hardcode ``PLATFORM_STREAMS`` em ``BackfillForm`` e em
    qualquer outro lugar que precise listar streams disponíveis por
    vendor. Adicionar vendor novo via :func:`registry.register` faz
    com que ele apareça aqui automaticamente — sem edição no frontend.
    """
    registrations = _load_registry()
    platforms: Dict[str, List[str]] = {}
    for reg in registrations:
        platforms.setdefault(reg.platform, []).append(reg.stream)
    # Ordena streams alfabeticamente para UX consistente.
    return schemas.PlatformStreamsResponse(
        platforms={p: sorted(streams) for p, streams in platforms.items()}
    )


# ── Lista de estados de coleta ────────────────────────────────────────


@router.get("/state", response_model=List[schemas.CollectionStateRead])
def list_collection_state(
    integration_id: Optional[int] = Query(None, description="Filtra por integração"),
    include_inactive: bool = Query(
        False,
        description=(
            "Inclui estados de Integrations/Organizations desativadas. "
            "Default ``false`` — desativadas somem da listagem porque "
            "``last_success_at`` ficaria congelado e poluiria o KPI de lag."
        ),
    ),
    db: Session = Depends(database.get_session),
    current_user: models.AppUser = Depends(app_auth.require_authenticated_user),
) -> List[schemas.CollectionStateRead]:
    """Lista ``collection_state`` com scope por tenant.

    Admin → todas as linhas (ativas).
    User  → apenas integrações da própria organização (ativas).

    Default oculta linhas de Integrations ``is_active=false`` ou de
    Organizations ``is_active=false`` (cascade do Partner soft-delete).
    Use ``?include_inactive=true`` pra debug ou histórico.
    """
    query = db.query(models.CollectionState)

    # Scope por integração (se solicitado)
    if integration_id is not None:
        query = query.filter(models.CollectionState.integration_id == integration_id)

    rows = query.order_by(
        models.CollectionState.integration_id.asc(),
        models.CollectionState.stream.asc(),
    ).all()

    if not rows:
        return []

    # Carrega integrações em lote para preencher nome/organization.
    integration_ids = {r.integration_id for r in rows}
    integrations = (
        db.query(models.Integration)
        .filter(models.Integration.id.in_(integration_ids))
        .all()
    )
    integration_by_id = {i.id: i for i in integrations}

    # Filtro de atividade — esconde Integration desativada e Org desativada
    # (cascade Partner soft-delete). Sem isso, ``last_success_at`` congela
    # e o card "Lag máximo" infla com integrações zumbis.
    if not include_inactive:
        org_active_ids: set[int] = set()
        if integration_by_id:
            org_ids_seen = {i.organization_id for i in integration_by_id.values()}
            org_active_ids = {
                row.id
                for row in db.query(models.Organization.id, models.Organization.is_active)
                .filter(
                    models.Organization.id.in_(org_ids_seen),
                    models.Organization.is_active.is_(True),
                )
                .all()
            }
        rows = [
            r
            for r in rows
            if (
                (integ := integration_by_id.get(r.integration_id)) is not None
                and integ.is_active
                and integ.organization_id in org_active_ids
            )
        ]

    # Aplica scope multi-tenant — filtra por organization_id visível.
    allowed_org_ids = tenant.accessible_org_ids(current_user, db)
    if allowed_org_ids is not None:
        rows = [
            r
            for r in rows
            if integration_by_id.get(r.integration_id) is not None
            and integration_by_id[r.integration_id].organization_id in allowed_org_ids
        ]

    return [_serialize_state(r, integration_by_id) for r in rows]


# ── Summary / KPIs ────────────────────────────────────────────────────


# ── cost/volume summary (Community = volume + razão; US$ é EE) ──

_COST_WINDOW_MINUTES = 180  # = observability_store TTL (3h)


class CostUsd(BaseModel):
    usd: float
    currency: str


class CostSummaryRow(BaseModel):
    organization_id: int
    # Volume BRUTO coletado (raw, lógico/pré-compressão) que entrou no pipeline.
    bytes_in: int
    # Volume ENTREGUE aos destinos (envelope {_centralops,normalized,raw} = wire-proxy):
    # é a BASE DE CUSTO do SIEM. Inclui overhead de envelope + fan-out (1 evento → N
    # destinos), por isso é normalmente MAIOR que bytes_in — NÃO é "amplificação" indevida.
    bytes_out: int
    events_in: int
    events_out: int
    # bytes_out/bytes_in. NÃO é razão de "economia": sem alavanca é ≥1 por
    # construção (envelope + fan-out). Cai quando alavancas de redução (drop/trim/sample)
    # entrarem em fases seguintes. None quando bytes_in==0 na janela.
    out_in_byte_ratio: Optional[float] = None
    # True quando uma alavanca de redução (trim/sample/suppress/aggregate) economizou
    # volume p/ a org na janela (bytes_saved>0) — só então a redução é "economia".
    reduction_active: bool = False
    # bytes LÓGICOS evitados pelas alavancas na janela (soma de todos os
    # reasons; ver collector_bytes_saved_total). 0 = nenhuma alavanca ativa/efetiva.
    bytes_saved: int = 0
    # Decomposição de bytes_saved por CAUSA (trim/sample/suppress/drop/aggregate/
    # redaction). Existe porque as causas NÃO compartilham base de medição — ver
    # o bloco `units` do envelope — e um total único esconde essa mistura.
    bytes_saved_by_reason: Dict[str, int] = {}
    # % de volume evitado = bytes_saved / (bytes_out + bytes_saved). None sem base.
    # ATENÇÃO: o denominador é CONTRAFACTUAL ("o que seria entregue"), não o
    # volume coletado — por isso nunca passa de 100% mesmo com as unidades
    # misturadas, e nunca denuncia a incoerência. Ver `unit_mismatch`.
    reduction_pct: Optional[float] = None
    # True quando o funil está no estado aritmeticamente impossível
    # (bytes_saved > bytes_in). NÃO é erro de contagem nem dupla contagem: é
    # consequência direta de bytes_in medir o evento CRU enquanto bytes_out e a
    # maior parte de bytes_saved medem o ENVELOPE, por ENTREGA. Exposto para a UI
    # poder rotular em vez de exibir um número impossível em silêncio.
    unit_mismatch: bool = False
    # Economia extrapolada em US$/DIA (janela → dia). Só com o pricer EE + cost_per_gb
    # setado no destino; None no core Community puro (sem tradução em $).
    savings_usd_per_day: Optional[float] = None
    # Bloco de custo em US$ (volume ENTREGUE) — só presente quando o pricer EE registrado.
    cost: Optional[CostUsd] = None


class CostSummary(BaseModel):
    window_minutes: int
    enabled: bool
    pricing_available: bool
    # Estado REAL das flags de redução no processo que respondeu. Existe porque
    # até aqui nada expunha isso e a UI de rotas afirmava (errado) que as
    # alavancas nasciam desligadas — o operador não tinha como conferir.
    levers: Dict[str, bool] = {}
    # Base de medição de cada métrica, para a UI rotular em vez de somar unidades
    # incomparáveis. Chaves espelham os campos de CostSummaryRow.
    units: Dict[str, str] = {}
    rows: List[CostSummaryRow]
    note: str


@router.get("/cost-summary", response_model=CostSummary)
def get_cost_summary(
    db: Session = Depends(database.get_session),
    current_user: models.AppUser = Depends(app_auth.require_authenticated_user),
) -> CostSummary:
    """Volume lógico (eventos/bytes) que ENTRA vs SAI por organização, na janela de
    metering. Read-only, org-scoped (fail-closed). Custo em US$ só
    aparece quando o pacote Enterprise registra um ``cost_pricer`` (seam ee_hooks);
    o core Community devolve apenas volume + razão adimensional.

    HONESTIDADE DE UNIDADE (o texto anterior aqui estava obsoleto: afirmava que
    nenhuma alavanca estava ativa, o que deixou de ser verdade na ADR-0015). As
    três métricas de bytes NÃO compartilham base de medição:

      * ``bytes_in``  = evento CRU do vendor, medido 1× por evento, pré-envelope;
      * ``bytes_out`` = ENVELOPE ``{_centralops, normalized, raw}``, medido por
        ENTREGA — logo multiplicado pelo fan-out de destinos;
      * ``bytes_saved`` = MISTURA: ``trim`` credita delta em unidade RAW (1× por
        evento, pré-fan-out), enquanto ``drop``/``sample`` creditam envelope por
        par evento×destino e ``suppress`` credita envelope 1× por evento.

    Consequência algébrica: ``(bytes_out + bytes_saved) / bytes_in ≈ (e/r)·D``,
    com ``e/r`` = overhead do envelope (sempre > 1, pois o envelope CONTÉM o raw)
    e ``D`` = destinos por evento. Ou seja, ``bytes_saved`` PODE legitimamente
    superar ``bytes_in`` sem que nenhum evento tenha sido contado duas vezes.
    Isso é sinalizado por ``unit_mismatch`` na linha, e a unificação das bases é
    trabalho de fase seguinte — aqui apenas não escondemos mais o problema."""
    from ..collectors import observability_store as obs
    from ..collectors.reduction import metering

    allowed_org_ids = tenant.accessible_org_ids(current_user, db)
    if allowed_org_ids is None:  # escopo global → todas as orgs
        org_ids = [r.id for r in db.query(models.Organization.id).all()]
    else:
        org_ids = sorted(allowed_org_ids)

    pricer = ee_hooks.get_cost_pricer()
    rows: List[CostSummaryRow] = []
    for oid in org_ids:
        key = str(oid)
        bytes_in = int(obs.read_window_total("org", key, "bytes_in", minutes=_COST_WINDOW_MINUTES))
        bytes_out = int(obs.read_window_total("org", key, "bytes_out", minutes=_COST_WINDOW_MINUTES))
        events_in = int(obs.read_window_total("org", key, "events_in", minutes=_COST_WINDOW_MINUTES))
        events_out = int(obs.read_window_total("org", key, "events_out", minutes=_COST_WINDOW_MINUTES))
        bytes_saved = int(obs.read_window_total("org", key, "bytes_saved", minutes=_COST_WINDOW_MINUTES))
        # Decomposição por causa: séries escritas por metering.record_saving sob
        # o vocabulário fechado SAVING_REASONS. Zeros são omitidos para não
        # poluir a UI com causas que nunca dispararam nesta org.
        by_reason = {
            reason: total
            for reason in metering.SAVING_REASONS
            if (
                total := int(
                    obs.read_window_total(
                        "org", key, f"bytes_saved:{reason}", minutes=_COST_WINDOW_MINUTES
                    )
                )
            )
        }
        if not (bytes_in or bytes_out or events_in or events_out or bytes_saved):
            continue  # sem dado na janela → omite a org
        out_in_byte_ratio = (bytes_out / bytes_in) if bytes_in else None
        # % de volume evitado pelas alavancas: bytes_saved sobre o que
        # SERIA entregue (entregue + evitado). None quando não há base.
        _reduction_base = bytes_out + bytes_saved
        reduction_pct = round(bytes_saved / _reduction_base, 4) if _reduction_base else None

        cost: Optional[CostUsd] = None
        savings_usd_per_day: Optional[float] = None
        if pricer is not None:
            try:
                priced = pricer(oid, None, bytes_out / 1e9)
                cost = CostUsd(usd=float(priced["usd"]), currency=str(priced.get("currency", "USD")))
                if bytes_saved > 0 and _COST_WINDOW_MINUTES > 0:
                    # US$ economizado na JANELA (pricer sobre bytes_saved) extrapolado p/ dia.
                    priced_sav = pricer(oid, None, bytes_saved / 1e9)
                    savings_usd_per_day = round(
                        float(priced_sav["usd"]) * (1440.0 / _COST_WINDOW_MINUTES), 4
                    )
            except Exception:  # noqa: BLE001 — pricing é best-effort; nunca derruba o endpoint
                logger.debug("cost_pricer falhou para org=%s", oid, exc_info=True)
        rows.append(
            CostSummaryRow(
                organization_id=oid,
                bytes_in=bytes_in,
                bytes_out=bytes_out,
                events_in=events_in,
                events_out=events_out,
                out_in_byte_ratio=out_in_byte_ratio,
                reduction_active=bytes_saved > 0,
                bytes_saved=bytes_saved,
                bytes_saved_by_reason=by_reason,
                unit_mismatch=bool(bytes_in and bytes_saved > bytes_in),
                reduction_pct=reduction_pct,
                savings_usd_per_day=savings_usd_per_day,
                cost=cost,
            )
        )

    from ..core.config import settings as _settings

    return CostSummary(
        window_minutes=_COST_WINDOW_MINUTES,
        enabled=metering.enabled(),
        pricing_available=pricer is not None,
        levers={
            "trim": bool(getattr(_settings, "REDUCTION_TRIM_ENABLED", False)),
            "sample": bool(getattr(_settings, "REDUCTION_SAMPLE_ENABLED", False)),
            "suppress": bool(getattr(_settings, "REDUCTION_SUPPRESS_ENABLED", False)),
            "aggregate": bool(getattr(_settings, "REDUCTION_AGGREGATE_ENABLED", False)),
            # drop é CONFIG DE ROTA, não alavanca opcional: não existe flag para
            # desligá-la, então é sempre efetiva (ver metering.record_drop_saving).
            "drop": True,
        },
        units={
            "bytes_in": "raw_event",
            "bytes_out": "envelope_per_delivery",
            "bytes_saved": "mixed",
            "bytes_saved:trim": "raw_delta_per_event",
            "bytes_saved:sample": "envelope_per_delivery",
            "bytes_saved:drop": "envelope_per_delivery",
            "bytes_saved:suppress": "envelope_per_event",
            "bytes_saved:aggregate": "envelope_per_delivery",
            "bytes_saved:redaction": "envelope_per_delivery",
        },
        rows=rows,
        note=(
            "bytes_in = evento CRU coletado (1× por evento). bytes_out = ENVELOPE "
            "{_centralops,normalized,raw} por ENTREGA — inclui overhead de envelope e "
            "fan-out, por isso out_in_byte_ratio é ≥1 por construção e NÃO é amplificação "
            "indevida. bytes_saved MISTURA bases: trim credita delta em unidade raw, "
            "drop/sample creditam envelope por par evento×destino, suppress credita "
            "envelope por evento — ver o bloco `units` e a decomposição "
            "bytes_saved_by_reason. Como consequência, bytes_saved PODE superar bytes_in "
            "sem dupla contagem (unit_mismatch=true sinaliza a linha). reduction_pct usa "
            "denominador CONTRAFACTUAL (bytes_out + bytes_saved), não bytes_in. IN e OUT "
            "são alinhados pela janela de tempo (por record-time, não por evento), então "
            "a razão é aproximação de regime estacionário. Custo em US$ é Enterprise "
            "(ee_hooks.cost_pricer)."
        ),
    )


@router.get("/summary", response_model=schemas.CollectorSummary)
def get_summary(
    db: Session = Depends(database.get_session),
    current_user: models.AppUser = Depends(app_auth.require_authenticated_user),
) -> schemas.CollectorSummary:
    """KPIs agregados — usado pela UI no topo da página do collector."""

    registrations = _load_registry()
    allowed_org_ids = tenant.accessible_org_ids(current_user, db)

    # Estados visíveis para este user. Filtra por Integration.is_active e
    # Organization.is_active — sem isso, integrações desativadas (manual ou
    # cascade Partner soft-delete) congelam ``last_success_at`` e poluem o
    # KPI ``stale_minutes_max`` com tempo desde o último sucesso de algo
    # que nunca mais vai coletar.
    rows = db.query(models.CollectionState).all()
    integration_ids = {r.integration_id for r in rows}
    if integration_ids:
        # Org-active filter pra cobrir cascade do Partner.
        active_org_ids = {
            row.id
            for row in db.query(models.Organization.id)
            .filter(models.Organization.is_active.is_(True))
            .all()
        }
        active_integrations = (
            db.query(models.Integration)
            .filter(
                models.Integration.id.in_(integration_ids),
                models.Integration.is_active.is_(True),
                models.Integration.organization_id.in_(active_org_ids),
            )
            .all()
        )
        if allowed_org_ids is not None:
            integrations_in_scope = {
                i.id: i
                for i in active_integrations
                if i.organization_id in allowed_org_ids
            }
        else:
            integrations_in_scope = {i.id: i for i in active_integrations}
    else:
        integrations_in_scope = {}
    rows = [r for r in rows if r.integration_id in integrations_in_scope]

    events_total = sum((r.events_collected_total or 0) for r in rows)
    errors = sum(1 for r in rows if (r.consecutive_failures or 0) > 0)

    stale_minutes_max: Optional[int] = None
    now = datetime.utcnow()
    for r in rows:
        if r.last_success_at:
            delta = now - r.last_success_at
            minutes = int(delta.total_seconds() // 60)
            if stale_minutes_max is None or minutes > stale_minutes_max:
                stale_minutes_max = minutes

    # Agrupamento por platform
    per_platform_map: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        integ = integrations_in_scope.get(r.integration_id)
        if integ is None:
            continue
        bucket = per_platform_map.setdefault(
            integ.platform,
            {
                "platform": integ.platform,
                "integrations": 0,
                "events_collected_total": 0,
                "errors": 0,
            },
        )
        bucket["integrations"] += 1
        bucket["events_collected_total"] += r.events_collected_total or 0
        if (r.consecutive_failures or 0) > 0:
            bucket["errors"] += 1

    return schemas.CollectorSummary(
        integrations_tracked=len({r.integration_id for r in rows}),
        vendors_registered=len(registrations),
        events_collected_total=events_total,
        integrations_with_errors=errors,
        stale_minutes_max=stale_minutes_max,
        per_platform=sorted(per_platform_map.values(), key=lambda b: b["platform"]),
    )


# ── Ações ────────────────────────────────────────────────────────────


@router.post(
    "/state/{integration_id}/{stream}/trigger",
    response_model=schemas.CollectorTriggerResponse,
    status_code=202,
)
def trigger_collection(
    integration_id: int,
    stream: str,
    db: Session = Depends(database.get_session),
    integration_repo: repository.IntegrationRepository = Depends(get_integration_repo),
    current_user: models.AppUser = Depends(app_auth.require_authenticated_user),
) -> schemas.CollectorTriggerResponse:
    """Enfileira imediatamente uma task Celery de coleta para
    ``(integration_id, stream)``.

    Útil para validar credenciais novas sem esperar o próximo tick do
    Beat. Retorna ``202 Accepted`` com o ``task_id`` — consulta ao
    status fica por conta do Flower (fora do escopo deste endpoint).
    """
    integration = integration_repo.get(integration_id)
    if integration is None:
        raise ApiError(
            "collector.integration_not_found",
            404,
            messages={
                "pt": "Integração não encontrada.",
                "en": "Integration not found.",
                "es": "Integración no encontrada.",
            },
        )

    if not tenant.can_access_organization(current_user, integration.organization_id):
        raise ApiError(
            "collector.access_denied",
            403,
            messages={
                "pt": "Acesso negado à integração.",
                "en": "Access denied to the integration.",
                "es": "Acceso denegado a la integración.",
            },
        )

    # Resolve via registry qual task/queue usar.
    try:
        from ..collectors.registry import get as registry_get
    except Exception as exc:  # pragma: no cover
        raise ApiError(
            "collector.subsystem_unavailable",
            503,
            messages={
                "pt": "Subsistema collector indisponível: {error}",
                "en": "Collector subsystem unavailable: {error}",
                "es": "Subsistema de recolección no disponible: {error}",
            },
            params={"error": str(exc)},
        )

    try:
        reg = registry_get(integration.platform, stream)
    except KeyError as exc:
        raise ApiError(
            "collector.not_registered",
            404,
            messages={
                "pt": (
                    "Nenhum collector registrado para platform={platform!r} "
                    "stream={stream!r}"
                ),
                "en": (
                    "No collector registered for platform={platform!r} "
                    "stream={stream!r}"
                ),
                "es": (
                    "Ningún recolector registrado para platform={platform!r} "
                    "stream={stream!r}"
                ),
            },
            params={"platform": integration.platform, "stream": stream},
        ) from exc

    # Import tardio das tasks para não quebrar quando o broker está offline.
    try:
        from ..collectors import tasks as collector_tasks
    except Exception as exc:  # pragma: no cover
        raise ApiError(
            "collector.tasks_unavailable",
            503,
            messages={
                "pt": "Tasks do collector indisponíveis: {error}",
                "en": "Collector tasks unavailable: {error}",
                "es": "Tareas del recolector no disponibles: {error}",
            },
            params={"error": str(exc)},
        )

    task_fn = getattr(collector_tasks, _task_name_to_callable(reg.task_name), None)
    if task_fn is None:
        raise ApiError(
            "collector.task_not_mapped",
            500,
            messages={
                "pt": "Task Celery não mapeada: {task_name}",
                "en": "Celery task not mapped: {task_name}",
                "es": "Tarea Celery no mapeada: {task_name}",
            },
            params={"task_name": reg.task_name},
        )

    async_result = task_fn.apply_async(
        args=(integration.id, stream),
        queue=reg.queue,
    )

    logger.info(
        "collectors: trigger manual integration=%s stream=%s user=%s task=%s",
        integration.id, stream, current_user.username, async_result.id,
    )

    return schemas.CollectorTriggerResponse(
        task_id=async_result.id,
        queue=reg.queue,
        integration_id=integration.id,
        stream=stream,
    )


@router.delete(
    "/state/{integration_id}/{stream}/cursor",
    status_code=204,
    dependencies=[Depends(app_auth.require_admin_user)],
)
def reset_cursor(
    integration_id: int,
    stream: str,
    db: Session = Depends(database.get_session),
    integration_repo: repository.IntegrationRepository = Depends(get_integration_repo),
    state_repo: repository.CollectionStateRepository = Depends(get_state_repo),
) -> None:
    """Zera o cursor persistido **e** o cursor hot em Redis (se disponível).

    Admin-only — reset faz re-coletar a partir do lookback padrão do
    vendor (geralmente 1 hora) e pode causar duplicidade temporária até
    o TTL de dedupe expirar.
    """
    integration = integration_repo.get(integration_id)
    if integration is None:
        raise ApiError(
            "collector.integration_not_found",
            404,
            messages={
                "pt": "Integração não encontrada.",
                "en": "Integration not found.",
                "es": "Integración no encontrada.",
            },
        )

    row = state_repo.get(integration_id, stream)
    if row is not None:
        db.delete(row)
        db.commit()

    # Best-effort no Redis — sem bloquear em caso de falha.
    try:
        import redis as _redis_sync

        from ..core.config import settings

        redis_url = settings.REDIS_URL or "redis://localhost:6379/0"
        client = _redis_sync.Redis.from_url(redis_url, decode_responses=True)
        client.delete(f"collection:cursor:{integration_id}:{stream}")
        client.close()
    except Exception as exc:  # pragma: no cover
        logger.warning("reset_cursor: Redis indisponível — %s", exc)

    logger.info(
        "collectors: cursor resetado integration=%s stream=%s",
        integration_id, stream,
    )


# ── Util interno ──────────────────────────────────────────────────────


def _task_name_to_callable(task_name: str) -> str:
    """Mapeia ``collectors.collect_vendor_logs_priority`` →
    ``collect_vendor_logs_priority`` (nome da função no módulo ``tasks``).
    """
    if "." in task_name:
        return task_name.rsplit(".", 1)[-1]
    return task_name
