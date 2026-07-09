"""Testes do fix 2026-05-06: ON DELETE rules em FKs de
``integrations``/``organizations``/``app_users``.

Bug original: ``DELETE /api/organizations/{id}`` levantava
``ForeignKeyViolation`` porque ``mapping_audit_log.integration_id``
apontava pra ``integrations.id`` SEM ``ondelete=`` definido. Cascata do
relationship ``Organization.integrations`` (``cascade='all, delete-orphan'``)
removia as integrations da sessão SQLAlchemy, mas o FK do audit-log no
banco bloqueava no flush.

Cobertura:
1. Regressão direta — DELETE Organization com Integration filha referenciada
   por ``mapping_audit_log`` completa sem erro; row do audit-log preserva
   ``integration_id=NULL``.
2. Por-FK — pra cada FK afetada, valida o comportamento esperado:
     - SET NULL: parent some, child preserva com FK NULL.
     - CASCADE:  parent some, child some junto.
     - RESTRICT: parent não pode sumir enquanto child existir.
3. Sanity de metadata — ``Base.metadata`` reflete os ``ondelete=`` esperados.

Estratégia: SQLite temp file (não ``:memory:``) com ``PRAGMA
foreign_keys=ON`` via event listener — replica a config de produção
(database.py:_enable_sqlite_wal). Para Postgres, a migration
``_heal_fk_ondelete_rules`` reescreve as constraints em runtime; aqui
testamos apenas o lado do model porque CI não tem Postgres.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta
from typing import Generator
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event as sa_event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.db import models
from backend.app.db.database import (
    Base,
    _EXPECTED_FK_ONDELETE_RULES,
)


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture()
def sqlite_engine():
    """Engine SQLite em arquivo temp com ``PRAGMA foreign_keys=ON``.

    Replica o listener de produção (database.py) para garantir que
    cascades sejam efetivamente aplicados. ``:memory:`` não serve
    porque cada conexão recria o schema; arquivo persiste entre
    conexões dentro do teste.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @sa_event.listens_for(engine, "connect")
    def _enable_fk(dbapi_conn: object, _conn_record: object) -> None:
        cursor = dbapi_conn.cursor()  # type: ignore[union-attr]
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    Base.metadata.create_all(bind=engine)

    try:
        yield engine
    finally:
        engine.dispose()
        try:
            os.unlink(db_path)
        except OSError:
            pass


@pytest.fixture()
def session(sqlite_engine) -> Generator[Session, None, None]:
    SessionLocal = sessionmaker(bind=sqlite_engine, autoflush=False)
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _make_org(session: Session, slug: str = "acme") -> models.Organization:
    org = models.Organization(name=f"Org-{slug}", slug=slug)
    session.add(org)
    session.commit()
    session.refresh(org)
    return org


def _make_integration(
    session: Session, org_id: int, name: str = "sophos-test"
) -> models.Integration:
    integ = models.Integration(
        organization_id=org_id,
        name=name,
        platform="sophos",
        kind="tenant",
    )
    session.add(integ)
    session.commit()
    session.refresh(integ)
    return integ


def _make_user(
    session: Session, username: str = "alice", org_id: int | None = None
) -> models.AppUser:
    user = models.AppUser(
        username=username,
        password_hash="x",
        organization_id=org_id,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


# ── Caso 1: Regressão direta do bug 2026-05-06 ──────────────────────────


def test_delete_organization_with_mapping_audit_log_does_not_raise(
    session: Session, sqlite_engine
) -> None:
    """Reproduz o crash de produção e valida o fix.

    Pipeline:
      1) Cria Org + Integration filha + MappingAuditLog referenciando a
         integration (mesmo cenário do incidente).
      2) DELETE Org via ORM (mesma rota que ``DELETE /organizations/{id}``).
      3) Esperado:
            - DELETE completa sem ``IntegrityError``.
            - Integration removida (CASCADE da org → integrations).
            - MappingAuditLog preservado, ``integration_id=NULL``.
    """
    org = _make_org(session, slug="bug-repro")
    integ = _make_integration(session, org.id, name="sophos-bug")

    audit = models.MappingAuditLog(
        id=str(uuid4()),
        integration_id=integ.id,
        action="discard_quarantine",
        username="ops",
        user_role="operator",
        detail="repro do bug 2026-05-06",
    )
    session.add(audit)
    session.commit()

    # Sanity check: row foi escrita com FK preenchido.
    assert audit.integration_id == integ.id

    # Ato — esperar que NÃO levante.
    session.delete(org)
    session.commit()

    # Validações pós-delete:
    assert (
        session.query(models.Organization).filter_by(id=org.id).count() == 0
    ), "Organization deve sumir após delete"
    assert (
        session.query(models.Integration).filter_by(id=integ.id).count() == 0
    ), "Integration deve cascatear via FK ondelete=CASCADE"

    surviving = session.query(models.MappingAuditLog).filter_by(id=audit.id).one()
    assert surviving.integration_id is None, (
        "mapping_audit_log deve preservar com integration_id=NULL "
        "(SET NULL é a política definida)"
    )


# ── Caso 2: SET NULL — child preserva quando parent some ────────────────


def test_history_integration_id_set_null_on_integration_delete(
    session: Session,
) -> None:
    """``history.integration_id`` é audit; SET NULL preserva forense."""
    org = _make_org(session, slug="hist")
    integ = _make_integration(session, org.id)
    user = _make_user(session, username="historian")

    h = models.History(
        integration_id=integ.id,
        user_id=user.id,
        operation="query",
        endpoint="/api/search",
        payload="{}",
    )
    session.add(h)
    session.commit()
    h_id = h.id

    session.delete(integ)
    session.commit()

    surviving = session.query(models.History).filter_by(id=h_id).one()
    assert surviving.integration_id is None
    assert surviving.user_id == user.id  # user ainda existe


def test_audit_logs_user_id_set_null_on_user_delete(session: Session) -> None:
    """``audit_logs.user_id`` SET NULL — RNF4.4 audit imutável."""
    user = _make_user(session, username="auditee")
    al = models.AuditLog(
        user_id=user.id,
        username="auditee",
        action="login",
        endpoint="/login",
    )
    session.add(al)
    session.commit()
    al_id = al.id

    session.delete(user)
    session.commit()

    survivor = session.query(models.AuditLog).filter_by(id=al_id).one()
    assert survivor.user_id is None
    assert survivor.username == "auditee"


def test_search_results_integration_id_set_null(session: Session) -> None:
    org = _make_org(session, slug="sr")
    integ = _make_integration(session, org.id)

    sr = models.SearchResult(
        search_id=f"s-{uuid4()}",
        integration_id=integ.id,
        statement="SELECT 1",
        table="xdr_data",
        from_ts="t1",
        to_ts="t2",
        status="completed",
    )
    session.add(sr)
    session.commit()
    sr_id = sr.id

    session.delete(integ)
    session.commit()

    survivor = session.query(models.SearchResult).filter_by(id=sr_id).one()
    assert survivor.integration_id is None


def test_app_users_organization_id_set_null(session: Session) -> None:
    """``app_users.organization_id`` SET NULL — preserva user mesmo se org sumir."""
    org = _make_org(session, slug="userorg")
    user = _make_user(session, username="orphan", org_id=org.id)
    user_id = user.id

    session.delete(org)
    session.commit()

    survivor = session.query(models.AppUser).filter_by(id=user_id).one()
    assert survivor.organization_id is None


# ── Caso 3: CASCADE — child some junto ──────────────────────────────────


def test_integrations_cascade_on_organization_delete(session: Session) -> None:
    """Integration filha some quando org pai é deletada (operacional)."""
    org = _make_org(session, slug="cascade-org")
    integ = _make_integration(session, org.id)
    integ_id = integ.id

    session.delete(org)
    session.commit()

    assert session.query(models.Integration).filter_by(id=integ_id).count() == 0


def test_health_check_cascade_on_integration_delete(session: Session) -> None:
    """``integration_health_checks`` é snapshot operacional — CASCADE."""
    org = _make_org(session, slug="hc")
    integ = _make_integration(session, org.id)

    hc = models.IntegrationHealthCheck(
        integration_id=integ.id,
        status="healthy",
    )
    session.add(hc)
    session.commit()
    hc_id = hc.id

    session.delete(integ)
    session.commit()

    assert (
        session.query(models.IntegrationHealthCheck).filter_by(id=hc_id).count() == 0
    )


def test_user_sessions_cascade_on_user_delete(session: Session) -> None:
    user = _make_user(session, username="sess-user")

    s = models.UserSession(
        user_id=user.id,
        token_hash=f"h-{uuid4()}",
        expires_at=datetime.utcnow() + timedelta(hours=1),
    )
    session.add(s)
    session.commit()
    s_id = s.id

    session.delete(user)
    session.commit()

    assert session.query(models.UserSession).filter_by(id=s_id).count() == 0


# ── Caso 4: RESTRICT — parent não pode sumir enquanto child existir ─────


def test_data_deletion_jobs_restrict_organization_delete(session: Session) -> None:
    """LGPD/GDPR job em andamento bloqueia deleção da org (RESTRICT)."""
    org = _make_org(session, slug="ddj")

    job = models.DataDeletionJob(
        id=str(uuid4()),
        organization_id=org.id,
        organization_slug="ddj",
        status="running",
    )
    session.add(job)
    session.commit()

    with pytest.raises(IntegrityError):
        session.delete(org)
        session.commit()
    session.rollback()

    # Org ainda lá após rollback.
    assert session.query(models.Organization).filter_by(id=org.id).count() == 1


# ── Caso 5: Sanity de metadata ──────────────────────────────────────────


def test_metadata_reflects_expected_ondelete_rules() -> None:
    """``Base.metadata`` reflete os ``ondelete=`` esperados pra cada FK.

    Sentinel pra evitar drift entre ``_EXPECTED_FK_ONDELETE_RULES``
    (tabela usada pela migration) e a declaração no model.
    """
    rules_by_target: dict[tuple[str, str], str] = {}
    for table, column, ref_table, ref_column, expected in _EXPECTED_FK_ONDELETE_RULES:
        rules_by_target[(table, column)] = expected

    for table_name, table in Base.metadata.tables.items():
        for col in table.columns:
            for fk in col.foreign_keys:
                key = (table_name, col.name)
                if key not in rules_by_target:
                    continue
                expected = rules_by_target[key]
                actual = (fk.ondelete or "").upper()
                assert actual == expected, (
                    f"FK {table_name}.{col.name} → {fk.target_fullname}: "
                    f"esperava ondelete={expected!r}, obtido {actual!r}. "
                    "Atualize models.py OU _EXPECTED_FK_ONDELETE_RULES."
                )


def test_mapping_audit_log_integration_id_is_set_null_in_sqlite(
    sqlite_engine,
) -> None:
    """``inspect(engine).get_foreign_keys`` retorna SET NULL na FK do bug.

    Garantia: a constraint efetiva no banco (não só metadata) tem a regra
    correta após ``create_all``. Em SQLite isso é o caminho normal —
    em Postgres prod, ``_heal_fk_ondelete_rules`` reescreve constraints
    legadas; aqui validamos só o lado do create_all.
    """
    inspector = inspect(sqlite_engine)
    fks = inspector.get_foreign_keys("mapping_audit_log")
    by_col = {tuple(fk["constrained_columns"]): fk for fk in fks}

    target_fk = by_col.get(("integration_id",))
    assert target_fk is not None, (
        "FK em mapping_audit_log.integration_id sumiu — verifique models.py"
    )

    options = target_fk.get("options") or {}
    on_delete = (options.get("ondelete") or "").upper()
    assert on_delete == "SET NULL", (
        f"Esperado 'SET NULL' em mapping_audit_log.integration_id, "
        f"obtido {on_delete!r}"
    )
