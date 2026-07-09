"""Celery Beat schedule — entries estáticas + boot-time sync das dinâmicas.

Com RedBeat, as entries dinâmicas por integração vivem no Redis
e são gerenciadas via ``backend.app.collectors.scheduler``. Este módulo só
precisa:

1. Registrar as **entries estáticas** (scheduler-tick, retention) via
   ``celery_app.conf.beat_schedule`` — estas não mudam em runtime e não
   precisam do Redis do RedBeat.

2. Disparar o **boot-time sync** das integrações ativas, que chama
   ``sync_all_active_integrations()`` para popular/reconciliar as entries
   dinâmicas no Redis.

Integrações criadas **depois** do boot são registradas via hook on-create
em ``routers/integrations.py`` — sem necessidade de reiniciar o Beat.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Dict

from celery.schedules import crontab

from ..core import ee_hooks
from .celery_app import celery_app
from .queues import (
    Q_BULK,
    T_SCHED_DISPATCH_DUE,
    T_SCHED_PRUNE_RESULTS,
)
from .registry import all_registrations

logger = logging.getLogger(__name__)


def _parse_cron(expression: str) -> crontab | None:
    """Parse a five-field cron expression into a celery ``crontab``.

    Returns ``None`` if the expression is empty or malformed — the caller
    skips registering the entry in that case.
    """
    parts = (expression or "").strip().split()
    if len(parts) != 5:
        return None
    minute, hour, day_of_month, month_of_year, day_of_week = parts
    try:
        return crontab(
            minute=minute,
            hour=hour,
            day_of_month=day_of_month,
            month_of_year=month_of_year,
            day_of_week=day_of_week,
        )
    except Exception:  # noqa: BLE001
        return None


def _static_entries() -> Dict[str, Any]:
    """Entries fixas: tick do scheduler legado + retention diária.

    Essas entries são registradas via ``conf.beat_schedule`` (dict in-memory)
    e não passam pelo RedBeat. Isso é intencional: elas são estáticas e não
    precisam de atualização em runtime.
    """
    entries: Dict[str, Any] = {
        # Substitui o loop `while True: sleep(60)` do services/scheduler.py.
        "scheduler-tick": {
            "task": T_SCHED_DISPATCH_DUE,
            "schedule": timedelta(seconds=60),
            "options": {"queue": Q_BULK, "expires": 55},
        },
        # SearchResultRetentionService.prune_expired_entries() — 1×/dia às 03:00 UTC.
        "scheduler-retention": {
            "task": T_SCHED_PRUNE_RESULTS,
            "schedule": timedelta(hours=24),
            "options": {"queue": Q_BULK, "expires": 3600},
        },
        # Purge de dados expirados por política de retenção.
        # Roda diariamente às 3am UTC.
        "prune-expired-data": {
            "task": f"{__package__}.retention_tasks.prune_all",
            "schedule": crontab(hour=3, minute=0),
            "options": {"queue": "maintenance", "expires": 3600},
        },
        # Marca PATs/SA tokens cujo ``expires_at`` passou como revogados
        # (``revoked_reason="expired"``). Roda diariamente às 03:30 UTC,
        # 30 min depois da retention pra não congestionar a fila.
        "api-tokens-mark-expired": {
            "task": "collectors.api_tokens_mark_expired",
            "schedule": crontab(hour=3, minute=30),
            "options": {"queue": "maintenance", "expires": 3600},
        },
    }

    # The Sophos partner-sync beat entry is an Enterprise feature, registered by the
    # Enterprise edition via ee_hooks.register_beat_entries (build_schedule() merges
    # it). Keep the settings import — the Entra block below reuses ``_settings``.
    from ..core.config import settings as _settings

    # Entra Graph user sync — schedule from settings; empty cron disables.
    try:
        entra_sync_cron = _parse_cron(_settings.ENTRA_SYNC_CRON)
        if entra_sync_cron is not None:
            entries["entra-user-sync"] = {
                "task": f"{__package__}.entra_sync_tasks.sync_entra_users",
                "schedule": entra_sync_cron,
                "options": {"queue": "maintenance", "expires": 3600},
            }
        else:
            logger.info(
                "beat_schedule: ENTRA_SYNC_CRON vazio/invalido — "
                "sync periodico de usuarios Entra desabilitado"
            )
    except Exception:  # noqa: BLE001
        logger.warning(
            "beat_schedule: falha ao registrar entra-user-sync entry",
            exc_info=True,
        )

    return entries


def build_schedule() -> Dict[str, Any]:
    """Monta o dict de entries estáticas para ``conf.beat_schedule``.

    As entries dinâmicas (por integração) são gerenciadas pelo RedBeat via
    ``sync_all_active_integrations()`` — chamada abaixo como side-effect
    deste módulo quando importado pelo processo Beat.
    """
    entries = _static_entries()

    # Merge any EE-contributed beat entries over the static ones.
    # Empty in Community (behavior-preserving). The EE registers these in its beat
    # bootstrap before this module is imported.
    ee_entries = ee_hooks.get_beat_entries()
    if ee_entries:
        entries.update(ee_entries)

    logger.info(
        "beat_schedule: %d entries estáticas (%d EE); %d vendors registrados no registry",
        len(entries),
        len(ee_entries),
        len(all_registrations()),
    )
    return entries


celery_app.conf.beat_schedule = build_schedule()

# Boot-time sync: popula/reconcilia entries dinâmicas no RedBeat.
# Tolerante a falhas — se DB/Redis estiver indisponível, loga e segue.
# Workers que já estão em loop continuarão funcionando com as entries
# que já existiam no Redis de boots anteriores.
try:
    from .scheduler import sync_all_active_integrations
    sync_all_active_integrations()
except Exception:  # pragma: no cover — guard de import inesperado
    logger.error(
        "beat_schedule: sync_all_active_integrations falhou no boot — "
        "entries dinâmicas podem estar desatualizadas",
        exc_info=True,
    )
