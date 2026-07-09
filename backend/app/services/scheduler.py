"""**DEPRECATED** — scheduler em threading substituído por Celery Beat.

A responsabilidade deste módulo foi migrada para
``backend/app/collectors/scheduler_tasks.py`` com três tasks Celery:

- ``collectors.scheduler.dispatch_due_scheduled_queries``
- ``collectors.scheduler.run_scheduled_query``
- ``collectors.scheduler.prune_search_result_retention``

O Beat (``docker compose up collector-beat``) agenda automaticamente o
tick de 60s e a retention diária. As queries do usuário continuam
sendo controladas pela tabela ``scheduled_queries`` — nenhum dado
precisa ser migrado.

Este stub é mantido apenas para **não quebrar imports existentes**
(``from .services.scheduler import start_scheduler``). Ele loga um
aviso e não inicia thread alguma.
"""

from __future__ import annotations

import logging
import os

# Re-exports para compatibilidade com callers antigos (ex: router
# ``routers/scheduled_queries.py`` que invoca ``_execute_schedule`` no
# endpoint "run now" e usa ``_convert_to_timedelta`` para serialização).
# A implementação real vive em ``backend.app.collectors.scheduler_tasks``.
from ..collectors.scheduler_tasks import (  # noqa: F401
    _convert_to_timedelta,
    _execute_schedule,
)

logger = logging.getLogger(__name__)


def start_scheduler() -> None:
    """No-op. Mantido só para compatibilidade de imports existentes.

    Se ``ENABLE_LEGACY_THREAD_SCHEDULER=1`` estiver setado, reinstala o
    scheduler antigo (emergência). Em produção, deixar desativado e
    confiar no Celery Beat — é o único pedido de um operador.
    """

    if os.environ.get("ENABLE_LEGACY_THREAD_SCHEDULER") == "1":
        logger.warning(
            "scheduler: ENABLE_LEGACY_THREAD_SCHEDULER=1 — subindo thread legada "
            "(NÃO recomendado). Desative para usar Celery Beat."
        )
        _start_legacy_thread()
        return

    logger.info(
        "scheduler: modo Celery Beat ativo — tasks em "
        "backend.app.collectors.scheduler_tasks; nenhuma thread será iniciada."
    )


def _start_legacy_thread() -> None:
    """Implementação antiga preservada para rollback de emergência."""
    import threading
    import time
    from datetime import datetime

    from ..db import database, repository
    from .search_results import SearchResultRetentionService

    def _run() -> None:  # pragma: no cover — só roda se operador forçar
        # Import tardio: evita depender da task Celery se o collector não
        # estiver disponível.
        from ..collectors.scheduler_tasks import _execute_schedule

        while True:
            try:
                db = next(database.get_session())
                now = datetime.utcnow()
                SearchResultRetentionService(db).prune_expired_entries()
                for sched in repository.ScheduledQueryRepository(db).list():
                    if sched.next_run <= now:
                        try:
                            _execute_schedule(db, sched)
                        except Exception:
                            logger.exception(
                                "legacy-scheduler: erro schedule=%d", sched.id
                            )
                db.close()
            except Exception:
                logger.exception("legacy-scheduler: erro no loop principal")
            time.sleep(60)

    thread = threading.Thread(target=_run, daemon=True, name="legacy-scheduler")
    thread.start()
