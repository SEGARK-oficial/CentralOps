"""REST endpoints para o Drift Explorer (RF5.2).

Lista campos do raw que nenhum mapping consome — coletados pelo
detector ``normalize/drift.py`` durante a coleta. A UI expõe
filtros, agregação por frequência e ações ("ignorar", "criar regra").

Endpoints:

- ``GET /api/drift`` — lista paginada com filtros vendor, event_type,
  status, ordenação por ``last_seen``.
- ``POST /api/drift/{id}/ignore`` — marca como ``ignored`` (campo
  conhecido, não vai virar regra).
- ``POST /api/drift/{id}/mark_mapped`` — marca como ``mapped``
  (engenheiro criou mapping para o campo).
- ``DELETE /api/drift/{id}`` — descarta entry. Útil quando o campo
  foi falso-positivo de um sample antigo.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..core import auth as app_auth, tenant
from ..core.errors import ApiError
from ..db import database, models

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/drift", tags=["drift"])


class UnknownFieldRead(BaseModel):
    id: str
    vendor: str
    event_type: str
    field_path: str
    sample_value: Optional[str]
    sample_type: Optional[str]
    occurrence_count: int
    first_seen: datetime
    last_seen: datetime
    status: str


class UnknownFieldList(BaseModel):
    total: int
    items: List[UnknownFieldRead]
    limit: int
    offset: int


def _to_read(uf: models.UnknownField) -> UnknownFieldRead:
    return UnknownFieldRead(
        id=uf.id,
        vendor=uf.vendor,
        event_type=uf.event_type,
        field_path=uf.field_path,
        sample_value=uf.sample_value,
        sample_type=uf.sample_type,
        occurrence_count=uf.occurrence_count,
        first_seen=uf.first_seen,
        last_seen=uf.last_seen,
        status=uf.status,
    )


@router.get("", response_model=UnknownFieldList)
def list_drift(
    vendor: Optional[str] = Query(None),
    event_type: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(database.get_session),
    user: models.AppUser = Depends(app_auth.require_permission(app_auth.Permission.DRIFT_READ)),
) -> UnknownFieldList:
    """Lista campos com drift de normalização.

    Isolamento multi-tenant: non-global vê APENAS o drift da
    própria organização — filtro EXATO por ``organization_id`` (substitui a
    antiga aproximação por vendor, que vazava campos entre clientes do mesmo
    vendor). Global scope (admin/SOC interno) vê tudo.
    """
    q = db.query(models.UnknownField)

    # Isolamento exato por tenant
    if not tenant.has_global_scope(user):
        if user.organization_id is None:
            # Sem org e sem global scope → nada a ver (fail-closed).
            return UnknownFieldList(total=0, items=[], limit=limit, offset=offset)
        q = q.filter(models.UnknownField.organization_id == user.organization_id)

    if vendor:
        q = q.filter(models.UnknownField.vendor == vendor)
    if event_type:
        q = q.filter(models.UnknownField.event_type == event_type)
    if status_filter:
        q = q.filter(models.UnknownField.status == status_filter)
    total = q.count()
    rows = (
        q.order_by(
            models.UnknownField.last_seen.desc(),
            models.UnknownField.occurrence_count.desc(),
        )
        .offset(offset)
        .limit(limit)
        .all()
    )
    return UnknownFieldList(
        total=total,
        items=[_to_read(r) for r in rows],
        limit=limit,
        offset=offset,
    )


def _resolve_definition_id(db: Session, *, vendor: str, event_type: str) -> str | None:
    """Resolve o definition_id pelo par (vendor, event_type), ou None se não existir."""
    row = (
        db.query(models.MappingDefinition)
        .filter(
            models.MappingDefinition.vendor == vendor,
            models.MappingDefinition.event_type == event_type,
        )
        .first()
    )
    return row.id if row else None


def _audit_drift(
    db: Session,
    *,
    uf: models.UnknownField,
    action: str,
    user: models.AppUser,
    detail: str | None = None,
) -> None:
    """Grava entrada em MappingAuditLog para ações no Drift Explorer."""
    definition_id = _resolve_definition_id(db, vendor=uf.vendor, event_type=uf.event_type)
    try:
        db.add(
            models.MappingAuditLog(
                mapping_definition_id=definition_id,
                mapping_version_id=None,
                action=action,
                user_id=user.id,
                username=user.username,
                user_role=user.role,
                detail=detail or json.dumps({
                    "field_path": uf.field_path,
                    "vendor": uf.vendor,
                    "event_type": uf.event_type,
                }),
            )
        )
        db.commit()
    except Exception as exc:
        logger.warning("Falha ao gravar audit de drift (%s): %s", action, exc)
        db.rollback()


def _check_drift_vendor_access(
    uf: models.UnknownField,
    db: Session,
    user: models.AppUser,
) -> None:
    """Valida acesso multi-tenant ao UnknownField por ``organization_id``
    (antes era aproximação por vendor).

    Lança HTTP 404 (não 403) para evitar enumeração entre tenants.
    Global scope (admin/SOC interno) tem acesso irrestrito.
    """
    if tenant.has_global_scope(user):
        return
    if user.organization_id is None:
        # Sem org e sem global scope → nada a ver (fail-closed). Simétrico ao
        # caminho de leitura: sem isto, ``None != None`` == False deixaria um
        # usuário escopado sem org escrever em UnknownField legado de org NULL.
        raise ApiError(
            "drift.field_not_found",
            404,
            messages={
                "pt": "Entrada de campo desconhecido não encontrada",
                "en": "Unknown field entry not found",
                "es": "Entrada de campo desconocido no encontrada",
            },
        )
    if uf.organization_id != user.organization_id:
        raise ApiError(
            "drift.field_not_found",
            404,
            messages={
                "pt": "Entrada de campo desconhecido não encontrada",
                "en": "Unknown field entry not found",
                "es": "Entrada de campo desconocido no encontrada",
            },
        )


_BULK_LIMIT = 500


class BulkActionRequest(BaseModel):
    field_ids: List[str]


class BulkActionResultItem(BaseModel):
    id: str
    success: bool
    error: Optional[str] = None


class BulkActionResult(BaseModel):
    updated: int
    failed: int
    items: List[BulkActionResultItem]


def _apply_bulk_status(
    db: Session,
    user: models.AppUser,
    field_ids: List[str],
    new_status: str,
    audit_action: str,
) -> BulkActionResult:
    if len(field_ids) == 0:
        raise ApiError(
            "drift.bulk.empty_field_ids",
            422,
            messages={
                "pt": "field_ids não pode ser vazio",
                "en": "field_ids must not be empty",
                "es": "field_ids no puede estar vacío",
            },
        )
    if len(field_ids) > _BULK_LIMIT:
        raise ApiError(
            "drift.bulk.limit_exceeded",
            422,
            messages={
                "pt": "field_ids excede o limite em lote ({limit})",
                "en": "field_ids exceeds bulk limit ({limit})",
                "es": "field_ids excede el límite por lote ({limit})",
            },
            params={"limit": _BULK_LIMIT},
        )
    # Dedupe preservando ordem para audit log determinístico.
    seen: set[str] = set()
    unique_ids = [fid for fid in field_ids if not (fid in seen or seen.add(fid))]  # type: ignore[func-returns-value]

    items: List[BulkActionResultItem] = []
    updated_count = 0

    for field_id in unique_ids:
        try:
            uf = db.get(models.UnknownField, field_id)
            if uf is None:
                items.append(BulkActionResultItem(
                    id=field_id, success=False, error="not_found",
                ))
                continue
            try:
                _check_drift_vendor_access(uf, db, user)
            except ApiError:
                # Não enumera tenants — mesmo erro de "not found"
                items.append(BulkActionResultItem(
                    id=field_id, success=False, error="not_found",
                ))
                continue
            uf.status = new_status
            db.commit()
            db.refresh(uf)
            _audit_drift(db, uf=uf, action=audit_action, user=user)
            items.append(BulkActionResultItem(id=field_id, success=True))
            updated_count += 1
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            items.append(BulkActionResultItem(
                id=field_id, success=False, error=str(exc),
            ))

    return BulkActionResult(
        updated=updated_count,
        failed=len(items) - updated_count,
        items=items,
    )


def _patch_status(db: Session, field_id: str, new_status: str) -> models.UnknownField:
    uf = db.get(models.UnknownField, field_id)
    if uf is None:
        raise ApiError(
            "drift.field_not_found",
            404,
            messages={
                "pt": "Entrada de campo desconhecido não encontrada",
                "en": "Unknown field entry not found",
                "es": "Entrada de campo desconocido no encontrada",
            },
        )
    uf.status = new_status
    db.commit()
    db.refresh(uf)
    return uf


# Bulk endpoints are registered BEFORE /{field_id}/* so that literal
# path segment "bulk" takes priority over the wildcard path parameter.
@router.post("/bulk/ignore", response_model=BulkActionResult)
def bulk_ignore_fields(
    body: BulkActionRequest,
    db: Session = Depends(database.get_session),
    user: models.AppUser = Depends(app_auth.require_permission(app_auth.Permission.DRIFT_IGNORE)),
) -> BulkActionResult:
    return _apply_bulk_status(db, user, body.field_ids, "ignored", "ignore_field")


@router.post("/bulk/mark_mapped", response_model=BulkActionResult)
def bulk_mark_mapped(
    body: BulkActionRequest,
    db: Session = Depends(database.get_session),
    user: models.AppUser = Depends(app_auth.require_permission(app_auth.Permission.DRIFT_MARK_MAPPED)),
) -> BulkActionResult:
    return _apply_bulk_status(db, user, body.field_ids, "mapped", "mark_mapped")


@router.post("/{field_id}/ignore", response_model=UnknownFieldRead)
def ignore_field(
    field_id: str,
    db: Session = Depends(database.get_session),
    user: models.AppUser = Depends(app_auth.require_permission(app_auth.Permission.DRIFT_IGNORE)),
) -> UnknownFieldRead:
    uf = db.get(models.UnknownField, field_id)
    if uf is None:
        raise ApiError(
            "drift.field_not_found",
            404,
            messages={
                "pt": "Entrada de campo desconhecido não encontrada",
                "en": "Unknown field entry not found",
                "es": "Entrada de campo desconocido no encontrada",
            },
        )
    _check_drift_vendor_access(uf, db, user)
    uf = _patch_status(db, field_id, "ignored")
    _audit_drift(db, uf=uf, action="ignore_field", user=user)
    return _to_read(uf)


@router.post("/{field_id}/mark_mapped", response_model=UnknownFieldRead)
def mark_mapped(
    field_id: str,
    db: Session = Depends(database.get_session),
    user: models.AppUser = Depends(app_auth.require_permission(app_auth.Permission.DRIFT_MARK_MAPPED)),
) -> UnknownFieldRead:
    uf = db.get(models.UnknownField, field_id)
    if uf is None:
        raise ApiError(
            "drift.field_not_found",
            404,
            messages={
                "pt": "Entrada de campo desconhecido não encontrada",
                "en": "Unknown field entry not found",
                "es": "Entrada de campo desconocido no encontrada",
            },
        )
    _check_drift_vendor_access(uf, db, user)
    uf = _patch_status(db, field_id, "mapped")
    _audit_drift(db, uf=uf, action="mark_mapped", user=user)
    return _to_read(uf)


@router.delete("/{field_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_field(
    field_id: str,
    db: Session = Depends(database.get_session),
    user: models.AppUser = Depends(app_auth.require_permission(app_auth.Permission.DRIFT_DELETE)),
) -> None:
    uf = db.get(models.UnknownField, field_id)
    if uf is None:
        raise ApiError(
            "drift.field_not_found",
            404,
            messages={
                "pt": "Entrada de campo desconhecido não encontrada",
                "en": "Unknown field entry not found",
                "es": "Entrada de campo desconocido no encontrada",
            },
        )
    _check_drift_vendor_access(uf, db, user)
    _audit_drift(db, uf=uf, action="delete_field", user=user)
    db.delete(uf)
    db.commit()
