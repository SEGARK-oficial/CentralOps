"""Colunas do agendamento em lote e da emissão por regra (ADR-0015, W1.1/W1.4/W1.5).

O que o Core entrega aqui é SCHEMA + repositório: a regra ``batch`` ganha a
própria busca (``schedule_query_id``), a cadência (``schedule_seconds``), a
janela (``schedule_lookback_seconds``) e o rastro do último tick; o
``QueryJob`` ganha ``correlation_rule_id`` para o finish avaliar SÓ a regra dona
da busca. Quem TICA é o beat do EE — mas a consulta "quem está na hora" e o
carimbo do tick vivem no repositório do Core, testados aqui.

Também: a migração leve acorda uma instalação legada com as colunas, e é
idempotente. Imports usam ``backend.app.*`` (gate compilado dual-root).
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import database as _db_module
from backend.app.db import models, repository
from backend.app.db.database import Base

_NEW_RULE_COLUMNS = (
    "emit_event", "schedule_seconds", "schedule_query_id", "schedule_lookback_seconds",
    "schedule_next_run_at", "schedule_last_run_at", "schedule_last_job_id",
    "schedule_last_error", "max_dedup_keys",
)


# ── migração leve ─────────────────────────────────────────────────────────────


@pytest.fixture
def engine_legado(monkeypatch, tmp_path):
    """Schema real, mas ``correlation_rules`` e ``query_jobs`` SEM as colunas novas."""
    url = f"sqlite:///{tmp_path / 'legado.db'}"
    engine = create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE correlation_rules"))
        conn.execute(text(
            "CREATE TABLE correlation_rules ("
            "  id INTEGER PRIMARY KEY,"
            "  organization_id INTEGER NOT NULL,"
            "  name VARCHAR NOT NULL,"
            "  enabled BOOLEAN NOT NULL DEFAULT 1,"
            "  eval_mode VARCHAR NOT NULL DEFAULT 'batch',"
            "  eval_priority INTEGER NOT NULL DEFAULT 0"
            ")"
        ))
        conn.execute(text("INSERT INTO correlation_rules (organization_id, name) VALUES (1, 'legada')"))
        conn.execute(text("DROP TABLE query_jobs"))
        conn.execute(text(
            "CREATE TABLE query_jobs ("
            "  id INTEGER PRIMARY KEY,"
            "  job_id VARCHAR NOT NULL,"
            "  organization_id INTEGER NOT NULL,"
            "  dialect VARCHAR NOT NULL,"
            "  statement TEXT NOT NULL,"
            "  from_ts VARCHAR NOT NULL, to_ts VARCHAR NOT NULL,"
            "  integration_ids TEXT NOT NULL,"
            "  allow_partial_results BOOLEAN NOT NULL DEFAULT 0,"
            "  status VARCHAR NOT NULL DEFAULT 'submitted'"
            ")"
        ))
    monkeypatch.setattr(_db_module, "engine", engine)
    monkeypatch.setattr(_db_module, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(_db_module, "DATABASE_URL", url)
    yield engine
    engine.dispose()


def _cols(engine, table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table)}


def test_migration_adds_every_new_column_and_wakes_legacy_rows_inert(engine_legado):
    _db_module._run_lightweight_migrations()
    cols = _cols(engine_legado, "correlation_rules")
    assert set(_NEW_RULE_COLUMNS) <= cols, sorted(set(_NEW_RULE_COLUMNS) - cols)
    assert "correlation_rule_id" in _cols(engine_legado, "query_jobs")
    with engine_legado.connect() as conn:
        row = conn.execute(text(
            "SELECT emit_event, schedule_seconds, schedule_query_id, max_dedup_keys "
            "FROM correlation_rules WHERE name = 'legada'"
        )).one()
    # A regra legada acorda SEM emitir, SEM agendamento e no teto do env —
    # byte-idêntico ao comportamento anterior.
    assert (bool(row[0]), row[1], row[2], row[3]) == (False, None, None, None)
    idx = {i["name"] for i in inspect(engine_legado).get_indexes("correlation_rules")}
    assert "ix_correlation_rules_schedule_next_run_at" in idx


def test_migration_is_idempotent(engine_legado):
    _db_module._run_lightweight_migrations()
    _db_module._run_lightweight_migrations()
    assert set(_NEW_RULE_COLUMNS) <= _cols(engine_legado, "correlation_rules")


def test_ondelete_rules_are_declared_for_both_new_fks():
    """A tabela ``_EXPECTED_FK_ONDELETE_RULES`` é o que reescreve a constraint
    num Postgres criado antes desta versão — declarar só no model não basta."""
    rules = {(t, c): rule for t, c, _rt, _rc, rule in _db_module._EXPECTED_FK_ONDELETE_RULES}
    assert rules[("correlation_rules", "schedule_query_id")] == "SET NULL"
    assert rules[("query_jobs", "correlation_rule_id")] == "SET NULL"


# ── repositório: quem está na hora ───────────────────────────────────────────


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    org = models.Organization(name="acme", slug="acme")
    s.add(org)
    s.commit()
    pq = models.PredefinedQuery(title="q", statement="x", organization_id=org.id, dialect="opensearch_dsl")
    s.add(pq)
    s.commit()
    yield s, org.id, pq.id
    s.close()
    engine.dispose()


def _rule(db, org_id, pq_id, **kw) -> models.CorrelationRule:
    label = kw.pop("name", "x")
    base = dict(
        organization_id=org_id, name=f"r-{label}", enabled=True,
        eval_mode="batch", schedule_seconds=300, schedule_query_id=pq_id,
        group_by_field="host", min_count=1, window_seconds=60, where_json="[]",
    )
    base.update(kw)
    r = models.CorrelationRule(**base)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def test_list_due_picks_never_run_and_overdue_but_not_future_disabled_inflight_or_unscheduled(db):
    s, org_id, pq_id = db
    now = datetime(2026, 9, 5, 12, 0, 0)
    nunca = _rule(s, org_id, pq_id, name="nunca", schedule_next_run_at=None)
    atrasada = _rule(s, org_id, pq_id, name="atrasada", schedule_next_run_at=now - timedelta(minutes=1))
    _rule(s, org_id, pq_id, name="futura", schedule_next_run_at=now + timedelta(minutes=1))
    _rule(s, org_id, pq_id, name="off", enabled=False)
    _rule(s, org_id, pq_id, name="voo", eval_mode="inflight")
    _rule(s, org_id, pq_id, name="manual", schedule_seconds=None)
    _rule(s, org_id, pq_id, name="semquery", schedule_query_id=None)

    due = repository.CorrelationRuleRepository(s).list_due_scheduled(now)
    assert [r.id for r in due] == [nunca.id, atrasada.id], "NULL primeiro (nunca rodou), depois a mais atrasada"


def test_mark_schedule_run_always_advances_even_on_error(db):
    s, org_id, pq_id = db
    r = _rule(s, org_id, pq_id, name="r")
    repo = repository.CorrelationRuleRepository(s)
    now = datetime(2026, 9, 5, 12, 0, 0)
    repo.mark_schedule_run(r, ran_at=now, next_run_at=now + timedelta(seconds=300), error="query inválida")
    s.refresh(r)
    assert r.schedule_last_run_at == now and r.schedule_next_run_at == now + timedelta(seconds=300)
    assert r.schedule_last_error == "query inválida"
    assert repo.list_due_scheduled(now) == [], "erro NÃO vira laço apertado: o próximo tick avança"
    # Sucesso limpa o erro: o campo responde "o ÚLTIMO tick deu erro?".
    repo.mark_schedule_run(r, ran_at=now, next_run_at=now + timedelta(seconds=600), job_id="abc")
    s.refresh(r)
    assert r.schedule_last_error is None and r.schedule_last_job_id == "abc"


def test_query_job_carries_the_rule_that_scheduled_it(db):
    s, org_id, pq_id = db
    r = _rule(s, org_id, pq_id, name="r")
    job = repository.QueryJobRepository(s).create(
        job_id="j1", organization_id=org_id, dialect="opensearch_dsl", statement="x",
        from_ts="2026-09-05T00:00:00Z", to_ts="2026-09-05T01:00:00Z", integration_ids=[1],
        correlation_rule_id=r.id,
    )
    assert job.correlation_rule_id == r.id
    adhoc = repository.QueryJobRepository(s).create(
        job_id="j2", organization_id=org_id, dialect="opensearch_dsl", statement="x",
        from_ts="2026-09-05T00:00:00Z", to_ts="2026-09-05T01:00:00Z", integration_ids=[1],
    )
    assert adhoc.correlation_rule_id is None, "job humano segue sem dono"
