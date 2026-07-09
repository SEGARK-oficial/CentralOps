from __future__ import annotations

from datetime import datetime, timedelta
import logging
import threading

from sqlalchemy.orm import Session

from ..core.config import settings
from ..db import repository


logger = logging.getLogger(__name__)

_search_result_cleanup_lock = threading.Lock()
_last_search_result_cleanup_at: datetime | None = None
_SEARCH_RESULT_CLEANUP_INTERVAL = timedelta(hours=1)


class SearchResultRetentionService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = repository.SearchResultRepository(db)

    def prune_expired_entries(self, *, force: bool = False) -> int:
        global _last_search_result_cleanup_at

        retention_days = settings.SEARCH_HISTORY_CSV_RETENTION_DAYS
        if retention_days <= 0:
            return 0

        now = datetime.utcnow()
        with _search_result_cleanup_lock:
            if (
                not force
                and _last_search_result_cleanup_at
                and now - _last_search_result_cleanup_at < _SEARCH_RESULT_CLEANUP_INTERVAL
            ):
                return 0

            cutoff = now - timedelta(days=retention_days)
            deleted = self.repo.delete_older_than(cutoff)
            _last_search_result_cleanup_at = now

            if deleted:
                logger.info("Search result retention: deleted %d expired records", deleted)

            return deleted
