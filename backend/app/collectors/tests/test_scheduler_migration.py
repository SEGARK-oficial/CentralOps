"""Testes da migração ``services/scheduler.py`` (threading) → Celery Beat.

Cobertura:

- ``dispatch_due_scheduled_queries`` enfileira apenas schedules vencidos.
- ``run_scheduled_query`` com sched inexistente não explode.
- ``services.scheduler.start_scheduler`` virou no-op (não sobe thread).
- Tasks estão registradas no Celery app.
- Entries estáticas (tick + retention) estão no ``beat_schedule``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest


def test_scheduler_tasks_are_registered_on_celery_app() -> None:
    from ..celery_app import celery_app
    from .. import scheduler_tasks  # noqa: F401  força descoberta

    registered = set(celery_app.tasks)
    assert "collectors.scheduler.dispatch_due_scheduled_queries" in registered
    assert "collectors.scheduler.run_scheduled_query" in registered
    assert "collectors.scheduler.prune_search_result_retention" in registered


def test_beat_schedule_has_static_scheduler_entries() -> None:
    from ..beat_schedule import _static_entries

    static = _static_entries()
    assert "scheduler-tick" in static
    assert "scheduler-retention" in static
    assert static["scheduler-tick"]["task"] == (
        "collectors.scheduler.dispatch_due_scheduled_queries"
    )
    assert static["scheduler-retention"]["task"] == (
        "collectors.scheduler.prune_search_result_retention"
    )
    # Tick é 60s, retention é diária.
    assert static["scheduler-tick"]["schedule"] == timedelta(seconds=60)
    assert static["scheduler-retention"]["schedule"] == timedelta(hours=24)


def test_legacy_start_scheduler_is_noop_by_default() -> None:
    """Sem a flag de emergência, ``start_scheduler`` não sobe thread nenhuma."""
    import os
    import threading as _threading

    from ...services.scheduler import start_scheduler

    # Garante que a flag de emergência está desligada.
    os.environ.pop("ENABLE_LEGACY_THREAD_SCHEDULER", None)

    before = {t.name for t in _threading.enumerate()}
    start_scheduler()
    after = {t.name for t in _threading.enumerate()}

    assert "legacy-scheduler" not in (after - before)


def test_dispatch_due_filters_by_next_run() -> None:
    """Só schedules com ``next_run <= now`` viram ``apply_async``."""
    from .. import scheduler_tasks

    now = datetime.utcnow()

    class _FakeSched:
        def __init__(self, id: int, next_run: datetime) -> None:
            self.id = id
            self.next_run = next_run

    due1 = _FakeSched(1, now - timedelta(seconds=30))
    due2 = _FakeSched(2, now - timedelta(minutes=5))
    future = _FakeSched(3, now + timedelta(minutes=10))

    class _FakeRepo:
        def list(self):
            return [due1, due2, future]

    class _FakeDb:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    enqueued: list[int] = []

    def _fake_apply_async(kwargs, queue):
        enqueued.append(kwargs["sched_id"])

    with patch.object(scheduler_tasks.database, "SessionLocal", return_value=_FakeDb()), \
         patch.object(
             scheduler_tasks.repository, "ScheduledQueryRepository", return_value=_FakeRepo()
         ), \
         patch.object(
             scheduler_tasks.run_scheduled_query, "apply_async", side_effect=_fake_apply_async
         ):
        # Chama a função subjacente (não ``.delay``) p/ executar inline.
        result = scheduler_tasks.dispatch_due_scheduled_queries.run()

    assert sorted(enqueued) == [1, 2]
    assert result == {"dispatched": 2, "skipped": 1}


def test_run_scheduled_query_handles_missing_sched_id() -> None:
    """Task tolera sched_id inexistente sem explodir."""
    from .. import scheduler_tasks

    class _FakeRepo:
        def get(self, sched_id):
            return None

    class _FakeDb:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    with patch.object(scheduler_tasks.database, "SessionLocal", return_value=_FakeDb()), \
         patch.object(
             scheduler_tasks.repository, "ScheduledQueryRepository", return_value=_FakeRepo()
         ):
        # Não deve lançar.
        scheduler_tasks.run_scheduled_query.run(sched_id=99999)
