import csv
import io

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from ..api.schemas import AuditLogRead, HistoryRead
from ..core import auth as app_auth
from ..db import database, models
from ..services.audit import AuditService
from ..services.history import HistoryService

router = APIRouter(prefix="/history", tags=["history"])


def get_history(db: Session = Depends(database.get_session)) -> HistoryService:
    return HistoryService(db)


def get_audit(db: Session = Depends(database.get_session)) -> AuditService:
    return AuditService(db)


@router.get("/", response_model=list[HistoryRead])
def list_history(
    service: HistoryService = Depends(get_history),
    current_user: models.AppUser = Depends(app_auth.require_authenticated_user),
):
    return service.list_entries(current_user)


@router.get("/audit", response_model=list[AuditLogRead])
def list_audit_history(
    username: str | None = None,
    ip_address: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    service: AuditService = Depends(get_audit),
    current_user: models.AppUser = Depends(app_auth.require_admin_user),
):
    return service.list_entries(
        username=username,
        ip_address=ip_address,
        date_from=date_from,
        date_to=date_to,
        viewer=current_user,
        include_all=True,
    )


@router.get("/audit/csv")
def export_audit_history_csv(
    username: str | None = None,
    ip_address: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    service: AuditService = Depends(get_audit),
    current_user: models.AppUser = Depends(app_auth.require_admin_user),
):
    entries = service.list_entries(
        username=username,
        ip_address=ip_address,
        date_from=date_from,
        date_to=date_to,
        limit=0,
        viewer=current_user,
        include_all=True,
    )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "created_at", "username", "user_role", "action", "method", "endpoint", "status_code", "ip_address", "detail"])

    for entry in entries:
        writer.writerow(
            [
                entry.id,
                entry.created_at.isoformat() if entry.created_at else "",
                entry.username or "",
                entry.user_role or "",
                entry.action,
                entry.method or "",
                entry.endpoint,
                entry.status_code or "",
                entry.ip_address or "",
                entry.detail or "",
            ]
        )

    return Response(
        output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="audit-history.csv"'},
    )
