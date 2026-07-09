"""Housekeeping task for API tokens — mark expired tokens as revoked.

Runs daily via Celery Beat. Idempotent: re-running on the same day is a no-op
because the WHERE clause filters tokens already revoked.

The task does **not** delete rows. Soft-revocation sets ``revoked_at`` to the
token's own ``expires_at`` value (so anyone querying audit can tell the
difference between "expired automatically" and "user revoked manually" —
the former lands ``revoked_at == expires_at``).

Why a periodic job vs lazy check at auth time?

The auth resolver (``ApiTokenService.resolve_bearer``) already rejects
expired tokens — clients get 401 immediately on `expires_at < now()`. The job
exists to:

1. **Reflect the truth in the UI**: tokens whose ``expires_at`` passed but
   that nobody tried to use yet still appear as "active" in
   ``/settings/tokens`` until a request is made. Job promotes them to
   "revoked" so the user sees the real state.
2. **Bound storage growth** (future): retention policy can target rows
   ``WHERE revoked_at < now() - INTERVAL '90 days'`` for hard delete. Without
   this housekeeping job, expired-but-never-used tokens stay forever
   ``revoked_at IS NULL`` and slip through retention.
3. **Metric clarity**: counters like "active tokens" become accurate without
   a complicated CASE WHEN expression at query time.

Falhas não são fatais — task loga e segue. Próxima execução pega o que ficou.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from celery import shared_task
from sqlalchemy import update

from ..db import database, models

logger = logging.getLogger(__name__)


@shared_task(bind=True, queue="maintenance", name="collectors.api_tokens_mark_expired")
def mark_expired_api_tokens(self: Any) -> dict[str, int]:
    """Marca como revogados os tokens cujo ``expires_at`` já passou.

    Returns: ``{"affected": N, "checked_at": ISO}``

    Idempotente: filtro ``revoked_at IS NULL`` garante que rodadas
    consecutivas não duplicam writes nem geram audit churn.
    """
    now = datetime.utcnow()
    affected = 0
    try:
        with database.SessionLocal() as db:
            stmt = (
                update(models.ApiToken)
                .where(
                    models.ApiToken.revoked_at.is_(None),
                    models.ApiToken.expires_at.is_not(None),
                    models.ApiToken.expires_at < now,
                )
                .values(revoked_at=models.ApiToken.expires_at)
                .execution_options(synchronize_session=False)
            )
            result = db.execute(stmt)
            db.commit()
            affected = int(result.rowcount or 0)
    except Exception:
        logger.exception("api_tokens_mark_expired: falha durante housekeeping")
        # Não relança — task é best-effort. Próxima rodada tenta de novo.
        return {"affected": 0, "checked_at": now.isoformat(), "error": True}

    if affected > 0:
        logger.info(
            "api_tokens_housekeeping: %d token(s) marcados como expirados",
            affected,
        )
    return {"affected": affected, "checked_at": now.isoformat()}
