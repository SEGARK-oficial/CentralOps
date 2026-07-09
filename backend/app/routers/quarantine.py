"""REST endpoints para inspeção da fila de quarentena.

Eventos com erro de normalização vivem em ``quarantine_events`` por
``retention_days`` (default 7). UI lista, filtra, reprocessa
ou descarta.

Endpoints:

- ``GET /api/quarantine`` — lista paginada com filtros vendor,
  event_type, error_kind, integration_id.
- ``GET /api/quarantine/{id}`` — payload completo + erro.
- ``POST /api/quarantine/{id}/discard`` — deleta evento (hard delete,
  operação irreversível). Requer permissão QUARANTINE_DISCARD.
- ``POST /api/quarantine/{id}/reprocess`` — reprocessamento
  real. Aplica mapping atual sobre raw_payload, despacha para Wazuh
  via fila ``dispatch.wazuh`` (async) e marca ``reprocessed_at``.
  Falhas de mapping atualizam error_kind/error_detail sem marcar como
  reprocessado. Requer permissão QUARANTINE_DISCARD (mesma de discard;
  permissão dedicada QUARANTINE_REPROCESS pode ser adicionada
  se necessário de granularidade).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import auth as app_auth, tenant
from ..core.errors import ApiError
from ..db import database, models

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/quarantine", tags=["quarantine"])


class QuarantineListItem(BaseModel):
    id: str
    integration_id: Optional[int]
    vendor: str
    event_type: Optional[str]
    error_kind: str
    error_detail: Optional[str]
    mapping_version_id: Optional[str]
    created_at: datetime
    expires_at: datetime
    reprocessed_at: Optional[datetime]


class QuarantineDetail(QuarantineListItem):
    raw_payload: Any


class QuarantineListResponse(BaseModel):
    total: int
    items: List[QuarantineListItem]
    limit: int
    offset: int


# ── Schemas bulk ─────────────────────────────────────────────


class BulkDiscardRequest(BaseModel):
    """Body do POST /bulk/discard. Cap operacional: 500 IDs/request."""

    ids: List[str] = Field(..., min_length=1, max_length=500)


class BulkErrorItem(BaseModel):
    id: str
    reason: str


class BulkDiscardResponse(BaseModel):
    """processed = IDs únicos vistos; discarded = efetivamente deletados;
    errors = (id, reason) p/ IDs ausentes ou de outro tenant."""

    processed: int
    discarded: int
    errors: List[BulkErrorItem]


class BulkReprocessRequest(BaseModel):
    ids: List[str] = Field(..., min_length=1, max_length=500)


class BulkReprocessResponse(BaseModel):
    """Reprocess é assíncrono — endpoint enfileira N tasks Celery."""

    accepted: int
    expired: int
    already_reprocessed: int
    errors: List[BulkErrorItem]


class QuarantineBulkIdsResponse(BaseModel):
    total: int
    ids: List[str]
    capped: bool


_BULK_IDS_MAX_CAP = 2000


def _to_list_item(ev: models.QuarantineEvent) -> QuarantineListItem:
    return QuarantineListItem(
        id=ev.id,
        integration_id=ev.integration_id,
        vendor=ev.vendor,
        event_type=ev.event_type,
        error_kind=ev.error_kind,
        error_detail=ev.error_detail,
        mapping_version_id=ev.mapping_version_id,
        created_at=ev.created_at,
        expires_at=ev.expires_at,
        reprocessed_at=ev.reprocessed_at,
    )


def _apply_org_scope_quarantine(
    q,
    db: Session,
    user: models.AppUser,
):
    """Filtra QuerySet de QuarantineEvent pela organização do usuário.

    Non-admin: restringe aos eventos cujo integration_id pertença a uma
    integração da organização do usuário (ou eventos sem integration_id,
    que permanecem visíveis apenas para admin).
    Admin: sem filtro adicional — vê tudo.
    """
    if tenant.has_global_scope(user):
        return q

    # Subquery: IDs de integrations que pertencem à org do usuário.
    # Usa .select() explicitamente para evitar SAWarning de coerção de Subquery.
    from sqlalchemy import select

    scoped_ids_select = (
        select(models.Integration.id)
        .where(models.Integration.organization_id == user.organization_id)
    )
    # Non-admin vê apenas eventos vinculados à sua org; eventos sem
    # integration_id são excluídos (unknown tenant → só admin acessa)
    q = q.filter(
        models.QuarantineEvent.integration_id.in_(scoped_ids_select)
    )
    return q


def _check_quarantine_org_access(
    ev: models.QuarantineEvent,
    db: Session,
    user: models.AppUser,
) -> None:
    """Valida acesso multi-tenant ao evento de quarentena.

    Lança HTTP 404 (não 403) para evitar enumeração de IDs entre tenants.
    Admin tem acesso irrestrito.
    """
    if tenant.has_global_scope(user):
        return
    if ev.integration_id is None:
        # Evento sem integração vinculada: apenas admin pode acessar
        raise ApiError(
            "quarantine.event_not_found",
            404,
            messages={
                "pt": "Evento de quarentena não encontrado.",
                "en": "Quarantine event not found.",
                "es": "Evento de cuarentena no encontrado.",
            },
        )
    integration = db.get(models.Integration, ev.integration_id)
    if integration is None or integration.organization_id != user.organization_id:
        raise ApiError(
            "quarantine.event_not_found",
            404,
            messages={
                "pt": "Evento de quarentena não encontrado.",
                "en": "Quarantine event not found.",
                "es": "Evento de cuarentena no encontrado.",
            },
        )


def _build_filtered_query(
    db: Session,
    user: models.AppUser,
    *,
    vendor: Optional[str],
    event_type: Optional[str],
    error_kind: Optional[str],
    integration_id: Optional[int],
    integration_name: Optional[str],
    status_filter: str,
):
    """Query base com todos os filtros + multi-tenant.

    ``status_filter`` aceita pending/reprocessed/all. O modelo NÃO tem
    flag 'discarded' — discard é hard delete.
    """
    q = db.query(models.QuarantineEvent)
    q = _apply_org_scope_quarantine(q, db, user)

    if vendor:
        q = q.filter(models.QuarantineEvent.vendor == vendor)
    if event_type:
        q = q.filter(models.QuarantineEvent.event_type == event_type)
    if error_kind:
        q = q.filter(models.QuarantineEvent.error_kind == error_kind)
    if integration_id is not None:
        q = q.filter(models.QuarantineEvent.integration_id == integration_id)

    if integration_name:
        like_pattern = f"%{integration_name}%"
        matching_int_ids = select(models.Integration.id).where(
            models.Integration.name.ilike(like_pattern)
        )
        q = q.filter(
            models.QuarantineEvent.integration_id.in_(matching_int_ids)
        )

    if status_filter == "pending":
        q = q.filter(models.QuarantineEvent.reprocessed_at.is_(None))
    elif status_filter == "reprocessed":
        q = q.filter(models.QuarantineEvent.reprocessed_at.isnot(None))
    # status_filter == "all" → sem filtro

    return q


@router.get("", response_model=QuarantineListResponse)
def list_quarantine(
    vendor: Optional[str] = Query(None),
    event_type: Optional[str] = Query(None),
    error_kind: Optional[str] = Query(None),
    integration_id: Optional[int] = Query(None),
    integration_name: Optional[str] = Query(
        None, description="Substring (case-insensitive) sobre Integration.name"
    ),
    status_filter: str = Query(
        "pending",
        alias="status",
        pattern="^(pending|reprocessed|all)$",
    ),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(database.get_session),
    user: models.AppUser = Depends(app_auth.require_permission(app_auth.Permission.QUARANTINE_READ)),
) -> QuarantineListResponse:
    q = _build_filtered_query(
        db,
        user,
        vendor=vendor,
        event_type=event_type,
        error_kind=error_kind,
        integration_id=integration_id,
        integration_name=integration_name,
        status_filter=status_filter,
    )

    total = q.count()
    rows = (
        q.order_by(models.QuarantineEvent.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return QuarantineListResponse(
        total=total,
        items=[_to_list_item(r) for r in rows],
        limit=limit,
        offset=offset,
    )


# IMPORTANTE: rotas /bulk/* declaradas ANTES de /{event_id} para o
# FastAPI não tratar "bulk" como um event_id.


@router.get("/bulk/ids", response_model=QuarantineBulkIdsResponse)
def list_quarantine_ids(
    vendor: Optional[str] = Query(None),
    event_type: Optional[str] = Query(None),
    error_kind: Optional[str] = Query(None),
    integration_id: Optional[int] = Query(None),
    integration_name: Optional[str] = Query(None),
    status_filter: str = Query(
        "pending",
        alias="status",
        pattern="^(pending|reprocessed|all)$",
    ),
    max_ids: int = Query(
        _BULK_IDS_MAX_CAP, alias="max", ge=1, le=_BULK_IDS_MAX_CAP
    ),
    db: Session = Depends(database.get_session),
    user: models.AppUser = Depends(app_auth.require_permission(app_auth.Permission.QUARANTINE_READ)),
) -> QuarantineBulkIdsResponse:
    """IDs casados pelos filtros (cap ``max`` ≤ 2000). Usado pelo
    "Selecionar tudo do filtro" — payload mais leve do que paginar
    a lista completa."""
    q = _build_filtered_query(
        db,
        user,
        vendor=vendor,
        event_type=event_type,
        error_kind=error_kind,
        integration_id=integration_id,
        integration_name=integration_name,
        status_filter=status_filter,
    )

    total = q.count()
    rows = (
        q.with_entities(models.QuarantineEvent.id)
        .order_by(models.QuarantineEvent.created_at.desc())
        .limit(max_ids)
        .all()
    )
    ids = [r[0] for r in rows]
    return QuarantineBulkIdsResponse(
        total=total,
        ids=ids,
        capped=total > len(ids),
    )


def _is_event_visible(
    ev: models.QuarantineEvent,
    db: Session,
    user: models.AppUser,
) -> bool:
    """Versão non-raise de _check_quarantine_org_access. Bulk endpoints
    skipam IDs invisíveis sem abortar o request."""
    if tenant.has_global_scope(user):
        return True
    if ev.integration_id is None:
        return False
    integration = db.get(models.Integration, ev.integration_id)
    if integration is None or integration.organization_id != user.organization_id:
        return False
    return True


@router.post(
    "/bulk/discard",
    response_model=BulkDiscardResponse,
    status_code=status.HTTP_200_OK,
)
def bulk_discard(
    payload: BulkDiscardRequest,
    db: Session = Depends(database.get_session),
    user: models.AppUser = Depends(
        app_auth.require_permission(app_auth.Permission.QUARANTINE_DISCARD)
    ),
) -> BulkDiscardResponse:
    """Discard idempotente em lote (até 500 IDs).

    IDs ausentes/de outro tenant viram entradas em ``errors`` com motivo
    ``not_found``. Discard é hard delete + audit log por ID.
    """
    seen_ids = list(dict.fromkeys(payload.ids))  # preserva ordem, dedupe
    errors: List[BulkErrorItem] = []
    discarded_count = 0

    for event_id in seen_ids:
        ev = db.get(models.QuarantineEvent, event_id)
        if ev is None or not _is_event_visible(ev, db, user):
            errors.append(BulkErrorItem(id=event_id, reason="not_found"))
            continue

        db.add(
            models.MappingAuditLog(
                mapping_definition_id=None,
                mapping_version_id=ev.mapping_version_id,
                action="discard_quarantine",
                user_id=user.id,
                username=user.username,
                user_role=user.role,
                detail=json.dumps({
                    "quarantine_event_id": ev.id,
                    "vendor": ev.vendor,
                    "event_type": ev.event_type,
                    "integration_id": ev.integration_id,
                    "bulk": True,
                }),
            )
        )
        db.delete(ev)
        discarded_count += 1

    db.commit()

    return BulkDiscardResponse(
        processed=len(seen_ids),
        discarded=discarded_count,
        errors=errors,
    )


@router.post(
    "/bulk/reprocess",
    response_model=BulkReprocessResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def bulk_reprocess(
    payload: BulkReprocessRequest,
    db: Session = Depends(database.get_session),
    user: models.AppUser = Depends(
        app_auth.require_permission(app_auth.Permission.QUARANTINE_DISCARD)
    ),
) -> BulkReprocessResponse:
    """Reprocess assíncrono em lote. Enfileira tasks Celery
    ``collectors.reprocess_quarantine_event`` na queue ``maintenance``.

    Buckets do response:
    - accepted: tasks enfileiradas com sucesso
    - expired: ID com expires_at < now
    - already_reprocessed: reprocessed_at já preenchido
    - errors: ausentes/invisíveis (not_found) ou falha de enqueue
    """
    from ..collectors.tasks import reprocess_quarantine_event

    seen_ids = list(dict.fromkeys(payload.ids))
    errors: List[BulkErrorItem] = []
    accepted = 0
    expired = 0
    already = 0
    now = datetime.utcnow()

    for event_id in seen_ids:
        ev = db.get(models.QuarantineEvent, event_id)
        if ev is None or not _is_event_visible(ev, db, user):
            errors.append(BulkErrorItem(id=event_id, reason="not_found"))
            continue
        if ev.reprocessed_at is not None:
            already += 1
            continue
        if ev.expires_at < now:
            expired += 1
            continue

        try:
            reprocess_quarantine_event.apply_async(
                kwargs={"event_id": ev.id, "actor_user_id": user.id},
                queue="maintenance",
            )
            accepted += 1
        except Exception as exc:  # pragma: no cover - depende de broker
            logger.exception(
                "bulk_reprocess: enqueue falhou event_id=%s", ev.id
            )
            errors.append(
                BulkErrorItem(id=ev.id, reason=f"enqueue_failed: {exc}"[:200])
            )

    # Audit único da operação bulk (não 1 por ID — ruído).
    db.add(
        models.MappingAuditLog(
            mapping_definition_id=None,
            mapping_version_id=None,
            action="bulk_reprocess_quarantine",
            user_id=user.id,
            username=user.username,
            user_role=user.role,
            detail=json.dumps({
                "requested": len(seen_ids),
                "accepted": accepted,
                "expired": expired,
                "already_reprocessed": already,
                "errors": [e.model_dump() for e in errors],
            }),
        )
    )
    db.commit()

    return BulkReprocessResponse(
        accepted=accepted,
        expired=expired,
        already_reprocessed=already,
        errors=errors,
    )


@router.get("/{event_id}", response_model=QuarantineDetail)
def get_quarantine(
    event_id: str,
    db: Session = Depends(database.get_session),
    user: models.AppUser = Depends(app_auth.require_permission(app_auth.Permission.QUARANTINE_READ)),
) -> QuarantineDetail:
    ev = db.get(models.QuarantineEvent, event_id)
    if ev is None:
        raise ApiError(
            "quarantine.event_not_found",
            404,
            messages={
                "pt": "Evento de quarentena não encontrado.",
                "en": "Quarantine event not found.",
                "es": "Evento de cuarentena no encontrado.",
            },
        )
    # Validação multi-tenant: non-admin só vê eventos da sua org
    _check_quarantine_org_access(ev, db, user)
    try:
        raw = json.loads(ev.raw_payload)
    except (TypeError, ValueError):
        raw = ev.raw_payload  # devolve string crua se falhar parse
    base = _to_list_item(ev).model_dump()
    base["raw_payload"] = raw
    return QuarantineDetail(**base)


@router.post("/{event_id}/discard", status_code=status.HTTP_204_NO_CONTENT)
def discard(
    event_id: str,
    db: Session = Depends(database.get_session),
    user: models.AppUser = Depends(app_auth.require_permission(app_auth.Permission.QUARANTINE_DISCARD)),
) -> None:
    ev = db.get(models.QuarantineEvent, event_id)
    if ev is None:
        raise ApiError(
            "quarantine.event_not_found",
            404,
            messages={
                "pt": "Evento de quarentena não encontrado.",
                "en": "Quarantine event not found.",
                "es": "Evento de cuarentena no encontrado.",
            },
        )

    # Validação multi-tenant: non-admin só descarta eventos da sua org
    _check_quarantine_org_access(ev, db, user)

    # Audit + delete na mesma transação.
    # Se o audit falhar, o delete também não acontece — consistência garantida.
    db.add(
        models.MappingAuditLog(
            mapping_definition_id=None,
            mapping_version_id=ev.mapping_version_id,
            action="discard_quarantine",
            user_id=user.id,
            username=user.username,
            user_role=user.role,
            detail=json.dumps({
                "quarantine_event_id": ev.id,
                "vendor": ev.vendor,
                "event_type": ev.event_type,
                "integration_id": ev.integration_id,
            }),
        )
    )
    db.delete(ev)
    db.commit()


@router.post("/{event_id}/reprocess", response_model=QuarantineListItem)
def reprocess(
    event_id: str,
    db: Session = Depends(database.get_session),
    user: models.AppUser = Depends(app_auth.require_permission(app_auth.Permission.QUARANTINE_DISCARD)),
) -> QuarantineListItem:
    """Reprocessa um evento de quarentena.

    Aplica o mapping atual da definição (vendor, event_type) sobre o
    raw_payload armazenado. Se a aplicação for bem-sucedida, o envelope
    produzido é despachado para a fila ``dispatch.wazuh`` (assíncrono)
    e ``reprocessed_at`` é marcado na linha do banco.

    Em caso de falha de mapping/validação, os campos ``error_kind`` e
    ``error_detail`` são atualizados para refletir o resultado da
    tentativa mais recente, mas ``reprocessed_at`` permanece null
    (o evento continua na quarentena para nova tentativa futura).

    Lógica de dedupe:
        Eventos reprocessados NÃO passam pelo claim() de dedupe do
        collector (vêm do banco direto). Para evitar duplicação no
        Wazuh, verificamos se o ``_centralops.event_id`` já tem entrada
        Redis ativa antes de enfileirar. Se sim, apenas marcamos
        ``reprocessed_at`` sem reenviar (o Wazuh já recebeu o evento).

    Permissão:
        QUARANTINE_DISCARD (operator+). Quem pode descartar pode
        reprocessar — reprocess é menos destrutivo (reversível via novo
        discard se o evento foi enviado errado). Permissão dedicada
        QUARANTINE_REPROCESS pode ser adicionada se houver
        demanda de granularidade entre descartar/reprocessar.

    Status HTTP:
        200 — sucesso (reprocessed_at preenchido).
        409 — evento já foi reprocessado anteriormente.
        410 — evento expirou (expires_at < now).
        422 — falha de mapping/parse/validação (error_kind atualizado).

    Returns:
        QuarantineListItem atualizado.
    """
    # ── 1. Carrega evento ─────────────────────────────────────────────
    ev = db.get(models.QuarantineEvent, event_id)
    if ev is None:
        raise ApiError(
            "quarantine.event_not_found",
            404,
            messages={
                "pt": "Evento de quarentena não encontrado.",
                "en": "Quarantine event not found.",
                "es": "Evento de cuarentena no encontrado.",
            },
        )

    # ── 2. Validação multi-tenant (igual a discard) ───────────────────
    _check_quarantine_org_access(ev, db, user)

    now = datetime.utcnow()

    # ── 3. Idempotência — já foi reprocessado? ────────────────────────
    if ev.reprocessed_at is not None:
        raise ApiError(
            "quarantine.already_reprocessed",
            409,
            messages={
                "pt": "Evento já foi reprocessado em {timestamp}Z.",
                "en": "Event was already reprocessed at {timestamp}Z.",
                "es": "El evento ya fue reprocesado en {timestamp}Z.",
            },
            params={"timestamp": ev.reprocessed_at.isoformat()},
        )

    # ── 4. Evento expirou? ────────────────────────────────────────────
    if ev.expires_at < now:
        raise ApiError(
            "quarantine.expired",
            410,
            messages={
                "pt": "Evento expirou em {timestamp}Z; não pode mais ser reprocessado.",
                "en": "Event expired at {timestamp}Z; it can no longer be reprocessed.",
                "es": "El evento expiró en {timestamp}Z; ya no puede reprocesarse.",
            },
            params={"timestamp": ev.expires_at.isoformat()},
        )

    # ── 5. Resolve organization_id da integration ─────────────────────
    # Necessário para preencher customer_id no EnvelopeContext.
    organization_id: Optional[int] = None
    if ev.integration_id is not None:
        integration = db.scalar(
            select(models.Integration).where(
                models.Integration.id == ev.integration_id
            )
        )
        if integration is not None:
            organization_id = integration.organization_id

    if organization_id is None:
        # Fallback: usa org do usuário logado (cobre casos onde a
        # integration foi deletada mas o evento ainda está na fila).
        organization_id = user.organization_id

    if organization_id is None:
        # Não há como preencher customer_id — falha com contexto claro.
        raise ApiError(
            "quarantine.organization_unresolved",
            422,
            messages={
                "pt": "Não foi possível resolver organization_id para o evento; a integração pode ter sido removida.",
                "en": "Could not resolve organization_id for the event; the integration may have been removed.",
                "es": "No fue posible resolver organization_id para el evento; la integración puede haber sido eliminada.",
            },
        )

    # ── 6. Tenta aplicar mapping ──────────────────────────────────────
    from ..collectors.normalize.reprocess import attempt_reprocess

    result = attempt_reprocess(
        raw_payload=ev.raw_payload,
        vendor=ev.vendor,
        event_type=ev.event_type,
        integration_id=ev.integration_id or 0,
        organization_id=organization_id,
        db=db,
    )

    error_kind_before = ev.error_kind

    if not result.success:
        # Atualiza o contexto do erro para a tentativa mais recente.
        ev.error_kind = result.error_kind or ev.error_kind
        ev.error_detail = result.error_detail
        if result.mapping_version_id:
            ev.mapping_version_id = result.mapping_version_id

        # Audit da tentativa falha.
        db.add(
            models.MappingAuditLog(
                mapping_definition_id=None,
                mapping_version_id=result.mapping_version_id,
                integration_id=ev.integration_id,
                action="reprocess_quarantine_failed",
                user_id=user.id,
                username=user.username,
                user_role=user.role,
                detail=json.dumps({
                    "quarantine_event_id": ev.id,
                    "vendor": ev.vendor,
                    "event_type": ev.event_type,
                    "error_kind_before": error_kind_before,
                    "error_kind_after": ev.error_kind,
                    "error_detail": result.error_detail,
                }),
            )
        )
        db.commit()
        db.refresh(ev)

        raise ApiError(
            "quarantine.reprocess_failed",
            422,
            messages={
                "pt": "Falha ao reprocessar evento: {error_kind} — {error_detail}",
                "en": "Failed to reprocess event: {error_kind} — {error_detail}",
                "es": "Error al reprocesar el evento: {error_kind} — {error_detail}",
            },
            params={
                "error_kind": ev.error_kind,
                "error_detail": result.error_detail,
            },
        )

    # ── 7. Sucesso — dedupe check antes de enfileirar ─────────────────
    # Eventos reprocessados saem direto do banco, sem passar pelo
    # collector que aplica dedupe. Verificamos o event_id do envelope
    # produzido: se já existe no Redis, o Wazuh já recebeu antes
    # (TTL de dedupe expirou na quarentena original, por isso está aqui).
    # Nesse caso marcamos reprocessed_at sem re-enfileirar.

    # Assert silenciado com -O. Substituído por validação explícita.
    if result.envelope is None:
        raise ApiError(
            "quarantine.reprocess_internal_error",
            500,
            messages={
                "pt": "Envelope nulo em reprocessamento bem-sucedido (bug interno) — result.success=True mas envelope=None.",
                "en": "Null envelope on a successful reprocess (internal bug) — result.success=True but envelope=None.",
                "es": "Envelope nulo en un reprocesamiento exitoso (bug interno) — result.success=True pero envelope=None.",
            },
        )

    centralops_meta = result.envelope.get("_centralops", {})
    event_id_from_envelope = centralops_meta.get("event_id", "")

    # ── 8. Enqueue ANTES de marcar reprocessed_at ───────────
    # Ordem correta: enqueue primeiro, só marca reprocessed_at se enqueue
    # retornar sem exceção. Se enqueue falhar, o evento permanece na
    # quarentena com reprocessed_at=null para nova tentativa futura.
    # _enqueue_reprocess_dispatch NÃO suprime exceções de enqueue —
    # qualquer falha real de broker é propagada aqui.
    _enqueue_reprocess_dispatch(result.envelope)

    # Marca reprocessed_at SOMENTE após enqueue bem-sucedido.
    ev.reprocessed_at = now
    if result.mapping_version_id:
        ev.mapping_version_id = result.mapping_version_id

    # Audit de sucesso.
    db.add(
        models.MappingAuditLog(
            mapping_definition_id=None,
            mapping_version_id=result.mapping_version_id,
            integration_id=ev.integration_id,
            action="reprocess_quarantine_success",
            user_id=user.id,
            username=user.username,
            user_role=user.role,
            detail=json.dumps({
                "quarantine_event_id": ev.id,
                "vendor": ev.vendor,
                "event_type": ev.event_type,
                "error_kind_before": error_kind_before,
                "error_kind_after": None,
                "event_id_dispatched": event_id_from_envelope,
            }),
        )
    )
    db.commit()
    db.refresh(ev)
    return _to_list_item(ev)


def _enqueue_reprocess_dispatch(envelope: dict) -> None:
    """Enfileira o envelope reprocessado pelo helper ÚNICO de dispatch.

    Roteia por ``pipeline._enqueue_dispatch`` (não mais direto a
    ``dispatch.wazuh``), de modo que o evento reprocessado siga o MESMO caminho
    de qualquer evento coletado: lane dedicada do Wazuh (byte-idêntico) +
    roteamento por regra (GA, first-match + catch-all -> wazuh-default).
    (Import tardio evita ciclo tasks↔pipeline.)

    Exceções de enqueue são PROPAGADAS para o caller. O
    ``_enqueue_dispatch`` propaga falha de ``apply_async`` (broker offline) →
    ``reprocessed_at`` só é marcado no sucesso (evita "reprocessado mas nunca
    enviado"). Broker offline → endpoint 500, operador re-tenta.
    """
    from ..collectors.pipeline import _enqueue_dispatch

    _enqueue_dispatch([envelope])
