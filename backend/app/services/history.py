from typing import Optional

from sqlalchemy.orm import Session

from ..db import models


class HistoryService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def add_entry(
        self,
        integration_id: int,
        operation: str,
        endpoint: str,
        payload: Optional[str],
        response_summary: Optional[str],
        user_id: int | None = None,
    ) -> models.History:
        entry = models.History(
            integration_id=integration_id,
            user_id=user_id,
            operation=operation,
            endpoint=endpoint,
            payload=payload,
            response_summary=response_summary,
        )
        self.db.add(entry)
        self.db.commit()
        self.db.refresh(entry)
        return entry

    def list_entries(self, viewer: models.AppUser) -> list[models.History]:
        query = self.db.query(models.History)
        if getattr(viewer, "role", None) != "admin":
            query = query.filter(models.History.user_id == viewer.id)
        return query.order_by(models.History.timestamp.desc(), models.History.id.desc()).all()
