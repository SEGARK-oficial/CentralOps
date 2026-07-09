"""Detections router — alertas de detecção de 1ª classe.

Superfície de leitura/triagem dos ``Detection`` (org-scoped fail-closed): listar,
detalhar e mudar status (open→ack→closed). Substitui o alerta best-effort syslog
como registro consultável. Leitura exige autenticação (escopada por org); mudar
status exige ``QUERY_RUN`` (triagem é função de operator+).
"""

from __future__ import annotations

import logging
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..core import auth as app_auth
from ..core import tenant
from ..core.auth import Permission, require_permission
from ..core.errors import ApiError
from ..db import database, models, repository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/detections", tags=["detections"])


class DetectionRead(BaseModel):
    id: int
    organization_id: int
    source: str
    source_query_id: Optional[int] = None
    integration_id: Optional[int] = None
    dialect: Optional[str] = None
    rule_id: Optional[str] = None
    rule_name: Optional[str] = None
    severity_id: int
    status: str
    dedup_key: str
    count: int
    suppression_window_seconds: int
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    search_result_id: Optional[int] = None
    ocsf_ref: Optional[str] = None
    created_at: Optional[str] = None


class DetectionStatusUpdate(BaseModel):
    status: Literal["open", "ack", "closed"]


def _to_read(d: models.Detection) -> DetectionRead:
    return DetectionRead(
        id=d.id,
        organization_id=d.organization_id,
        source=d.source,
        source_query_id=d.source_query_id,
        integration_id=d.integration_id,
        dialect=d.dialect,
        rule_id=d.rule_id,
        rule_name=d.rule_name,
        severity_id=d.severity_id,
        status=d.status,
        dedup_key=d.dedup_key,
        count=d.count or 0,
        suppression_window_seconds=d.suppression_window_seconds or 0,
        first_seen=d.first_seen.isoformat() if d.first_seen else None,
        last_seen=d.last_seen.isoformat() if d.last_seen else None,
        search_result_id=d.search_result_id,
        ocsf_ref=d.ocsf_ref,
        created_at=d.created_at.isoformat() if d.created_at else None,
    )


@router.get("", response_model=List[DetectionRead])
def list_detections(
    status_filter: Optional[str] = None,
    limit: int = 100,
    db: Session = Depends(database.get_session),
    current_user: models.AppUser = Depends(app_auth.require_authenticated_user),
) -> List[DetectionRead]:
    """Lista os alertas visíveis ao chamador (org-scoped)."""
    repo = repository.DetectionRepository(db)
    org_ids = tenant.accessible_org_ids(current_user, db)  # None=global, []=nenhuma
    rows = repo.list_for_org(org_ids, status=status_filter, limit=min(max(limit, 1), 500))
    return [_to_read(d) for d in rows]


@router.get("/{detection_id}", response_model=DetectionRead)
def get_detection(
    detection_id: int,
    db: Session = Depends(database.get_session),
    current_user: models.AppUser = Depends(app_auth.require_authenticated_user),
) -> DetectionRead:
    repo = repository.DetectionRepository(db)
    org_ids = tenant.accessible_org_ids(current_user, db)
    d = repo.get(detection_id, organization_ids=org_ids)
    if d is None:
        raise ApiError(
            "detection.not_found",
            404,
            messages={
                "pt": "Detecção não encontrada.",
                "en": "Detection not found.",
                "es": "Detección no encontrada.",
            },
        )
    return _to_read(d)


@router.patch("/{detection_id}", response_model=DetectionRead)
def update_detection_status(
    detection_id: int,
    payload: DetectionStatusUpdate,
    db: Session = Depends(database.get_session),
    # Triagem de alerta = função de operator+ (mesma trilha do live-query).
    current_user: models.AppUser = Depends(require_permission(Permission.QUERY_RUN)),
) -> DetectionRead:
    repo = repository.DetectionRepository(db)
    org_ids = tenant.accessible_org_ids(current_user, db)
    d = repo.get(detection_id, organization_ids=org_ids)
    if d is None:
        raise ApiError(
            "detection.not_found",
            404,
            messages={
                "pt": "Detecção não encontrada.",
                "en": "Detection not found.",
                "es": "Detección no encontrada.",
            },
        )
    return _to_read(repo.set_status(d, payload.status))
