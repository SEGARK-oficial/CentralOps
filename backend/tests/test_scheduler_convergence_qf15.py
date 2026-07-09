"""Convergência do scheduler (resiliência + execução vendor-neutra).

Cobre o que os testes de _run_query_for_integration não pegam:
- ScheduledQueryRepository.update_run_outcome: saúde (healthy/degraded/failing).
- SearchResultRepository.has_recent_terminal_run: guarda de idempotência.
- _execute_schedule end-to-end (DB real): sucesso avança next_run + status healthy +
  SearchResult com metadados; falha → degraded; idempotência (2ª run skipa).
- run_scheduled_query roteada p/ a fila dedicada collect.query.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.collectors import scheduler_tasks
from backend.app.db import models, repository
from backend.app.db.database import Base
from backend.app.providers.base import QueryResult


@pytest.fixture()
def db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()
    eng.dispose()


def _seed(db, *, platform="wazuh"):
    org = models.Organization(name="Acme", slug="acme")
    db.add(org); db.commit(); db.refresh(org)
    integ = models.Integration(name="i", organization_id=org.id, kind="tenant", platform=platform)
    db.add(integ); db.commit(); db.refresh(integ)
    pq = models.PredefinedQuery(title="Q", statement='{"match_all":{}}', table="alerts",
                                client_ids=str(integ.id))
    db.add(pq); db.commit(); db.refresh(pq)
    sched = models.ScheduledQuery(
        query_id=pq.id, client_ids=str(integ.id), interval_minutes=60,
        lookback_value=1, lookback_unit="days", days_back=1,
        next_run=datetime.utcnow() - timedelta(minutes=1),
    )
    db.add(sched); db.commit(); db.refresh(sched)
    return org, integ, pq, sched


class _Provider:
    def __init__(self, items=None, raise_exc=None):
        self._items = items or []
        self._raise = raise_exc

    def run_query(self, statement, from_ts, to_ts, **kw):
        if self._raise:
            raise self._raise
        return QueryResult(items=list(self._items), total=len(self._items))


# ── Repo: saúde + idempotência ─────────────────────────────────────────


def test_update_run_outcome_health(db):
    _, _, _, sched = _seed(db)
    repo = repository.ScheduledQueryRepository(db)
    nxt = datetime.utcnow() + timedelta(minutes=60)

    repo.update_run_outcome(sched, next_run=nxt, last_run_at=datetime.utcnow(), success=True)
    assert sched.status == "healthy" and sched.consecutive_failures == 0

    for i in range(1, 6):
        repo.update_run_outcome(sched, next_run=nxt, last_run_at=datetime.utcnow(),
                                success=False, last_error=f"boom{i}", failing_threshold=5)
    assert sched.consecutive_failures == 5
    assert sched.status == "failing"
    assert "boom5" in sched.last_error

    repo.update_run_outcome(sched, next_run=nxt, last_run_at=datetime.utcnow(), success=True)
    assert sched.status == "healthy" and sched.consecutive_failures == 0 and sched.last_error is None


def test_has_recent_terminal_run(db):
    _, integ, _, sched = _seed(db)
    results = repository.SearchResultRepository(db)
    now = datetime.utcnow()
    assert results.has_recent_terminal_run(sched.id, now - timedelta(minutes=30)) is False
    results.add_run(integ.id, "sid1", "stmt", "alerts", "f", "t", "finished",
                    schedule_id=sched.id, organization_id=integ.organization_id)
    assert results.has_recent_terminal_run(sched.id, now - timedelta(minutes=30)) is True
    # janela antiga (futuro) não casa
    assert results.has_recent_terminal_run(sched.id, now + timedelta(minutes=30)) is False


# ── _execute_schedule end-to-end ───────────────────────────────────────


def test_execute_schedule_success(db, monkeypatch):
    org, integ, _, sched = _seed(db, platform="wazuh")
    old_next = sched.next_run
    monkeypatch.setattr(scheduler_tasks, "get_provider", lambda i: _Provider(items=[{"a": 1}, {"a": 2}]))

    scheduler_tasks._execute_schedule(db, sched)

    db.refresh(sched)
    assert sched.status == "healthy"
    assert sched.consecutive_failures == 0
    assert sched.next_run > old_next
    assert sched.organization_id == org.id  # backfill fail-closed
    sr = db.query(models.SearchResult).filter_by(schedule_id=sched.id).one()
    assert sr.status == "finished"
    assert sr.platform == "wazuh"
    assert sr.engine == "query"
    assert sr.language == "opensearch_dsl"   # dialeto da capability do wazuh
    assert sr.ocsf_mapping_version == "1"
    assert sr.organization_id == org.id
    assert sr.result_count == 2


def test_execute_schedule_failure_marks_degraded(db, monkeypatch):
    _, integ, _, sched = _seed(db, platform="wazuh")
    monkeypatch.setattr(scheduler_tasks, "get_provider",
                        lambda i: _Provider(raise_exc=RuntimeError("indexer down")))

    scheduler_tasks._execute_schedule(db, sched)

    db.refresh(sched)
    assert sched.status == "degraded"
    assert sched.consecutive_failures == 1
    sr = db.query(models.SearchResult).filter_by(schedule_id=sched.id).one()
    assert sr.status == "failed"


def test_execute_schedule_idempotent_within_cadence(db, monkeypatch):
    _, integ, _, sched = _seed(db, platform="wazuh")
    monkeypatch.setattr(scheduler_tasks, "get_provider", lambda i: _Provider(items=[{"a": 1}]))

    scheduler_tasks._execute_schedule(db, sched)
    # 2ª execução imediata (re-entrega acks_late): guarda idempotente skipa.
    scheduler_tasks._execute_schedule(db, sched)

    count = db.query(models.SearchResult).filter_by(schedule_id=sched.id).count()
    assert count == 1  # não duplicou


def test_execute_schedule_skips_source_without_query_capability(db, monkeypatch):
    # ninjaone não tem provider rico → integration_query_capability None → skip.
    org = models.Organization(name="B", slug="b")
    db.add(org); db.commit(); db.refresh(org)
    integ = models.Integration(name="n", organization_id=org.id, kind="tenant", platform="ninjaone")
    db.add(integ); db.commit(); db.refresh(integ)
    pq = models.PredefinedQuery(title="Q2", statement="x", table="t", client_ids=str(integ.id))
    db.add(pq); db.commit(); db.refresh(pq)
    sched = models.ScheduledQuery(query_id=pq.id, client_ids=str(integ.id), interval_minutes=60,
                                  lookback_value=1, lookback_unit="days", days_back=1,
                                  next_run=datetime.utcnow() - timedelta(minutes=1))
    db.add(sched); db.commit(); db.refresh(sched)

    scheduler_tasks._execute_schedule(db, sched)
    db.refresh(sched)
    assert db.query(models.SearchResult).filter_by(schedule_id=sched.id).count() == 0
    assert sched.status == "degraded"  # nenhuma fonte respondeu


# ── Roteamento da fila dedicada ────────────────────────────────────────


def test_run_scheduled_query_routed_to_query_queue():
    from backend.app.collectors.celery_app import celery_app
    from backend.app.collectors.queues import T_SCHED_RUN
    assert celery_app.conf.task_routes[T_SCHED_RUN]["queue"] == "collect.query"
