"""Testes das tasks Celery de purge por retenção.

As tasks são chamadas via .run() para execução síncrona sem broker.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import database, models
from backend.app.db.database import Base


# ── Fixture base ──────────────────────────────────────────────────────


@pytest.fixture()
def db_session():
    """SQLite in-memory com SessionLocal redirecionado para os testes."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    # Substitui o SessionLocal do módulo de tasks pelo in-memory.
    original_session_local = database.SessionLocal
    database.SessionLocal = Session  # type: ignore[assignment]

    yield Session

    database.SessionLocal = original_session_local  # type: ignore[assignment]
    Base.metadata.drop_all(bind=engine)


# ── Helpers ───────────────────────────────────────────────────────────


def _seed_org(session, *, name: str | None = None) -> models.Organization:
    slug = f"org-{uuid4().hex[:8]}"
    org = models.Organization(name=name or slug, slug=slug, is_active=True)
    session.add(org)
    session.flush()
    return org


def _seed_integration(
    session, *, org_id: int, platform: str = "sophos"
) -> models.Integration:
    intg = models.Integration(
        organization_id=org_id,
        name=f"intg-{uuid4().hex[:6]}",
        platform=platform,
    )
    session.add(intg)
    session.flush()
    return intg


def _seed_quarantine(
    session,
    *,
    integration_id: int,
    created_at: datetime,
) -> models.QuarantineEvent:
    ev = models.QuarantineEvent(
        integration_id=integration_id,
        vendor="sophos",
        event_type="sophos.alert",
        raw_payload=json.dumps({"id": str(uuid4())}),
        error_kind="map",
        created_at=created_at,
        expires_at=created_at + timedelta(days=7),
    )
    session.add(ev)
    session.flush()
    return ev


def _seed_history(
    session,
    *,
    integration_id: int,
    timestamp: datetime,
) -> models.History:
    h = models.History(
        integration_id=integration_id,
        operation="test",
        endpoint="/test",
        timestamp=timestamp,
    )
    session.add(h)
    session.flush()
    return h


def _seed_drift(
    session,
    *,
    vendor: str,
    last_seen: datetime,
    organization_id: int | None = None,
) -> models.UnknownField:
    uf = models.UnknownField(
        vendor=vendor,
        event_type="test.event",
        field_path=f"field.{uuid4().hex[:8]}",
        organization_id=organization_id,
        last_seen=last_seen,
        first_seen=last_seen,
        status="new",
    )
    session.add(uf)
    session.flush()
    return uf


def _seed_retention(
    session,
    *,
    org_id: int,
    quarantine_days: int = 7,
    drift_days: int = 90,
    history_days: int = 30,
    audit_days: int = 365,
) -> models.OrganizationRetentionConfig:
    cfg = models.OrganizationRetentionConfig(
        organization_id=org_id,
        quarantine_retention_days=quarantine_days,
        drift_retention_days=drift_days,
        history_retention_days=history_days,
        audit_log_retention_days=audit_days,
    )
    session.add(cfg)
    session.flush()
    return cfg


def _seed_audit_log(
    session,
    *,
    user_id: int | None,
    created_at: datetime,
) -> models.AuditLog:
    al = models.AuditLog(
        user_id=user_id,
        username="test" if user_id else None,
        action="test_action",
        endpoint="/test",
        created_at=created_at,
    )
    session.add(al)
    session.flush()
    return al


# ── Testes prune_expired_quarantine ──────────────────────────────────


def test_prune_quarantine_respects_org_specific_retention(db_session) -> None:
    """Org A com retention=7d e Org B com retention=30d.

    Evento criado há 10 dias: Org A deve ser purgado, Org B mantido.
    """
    from backend.app.collectors.retention_tasks import prune_expired_quarantine

    with db_session() as db:
        org_a = _seed_org(db, name="Org A Quarantine")
        org_b = _seed_org(db, name="Org B Quarantine")

        intg_a = _seed_integration(db, org_id=org_a.id)
        intg_b = _seed_integration(db, org_id=org_b.id)

        # Org A: retention 7d.
        _seed_retention(db, org_id=org_a.id, quarantine_days=7)
        # Org B: retention 30d.
        _seed_retention(db, org_id=org_b.id, quarantine_days=30)

        ten_days_ago = datetime.utcnow() - timedelta(days=10)
        ev_a = _seed_quarantine(db, integration_id=intg_a.id, created_at=ten_days_ago)
        ev_b = _seed_quarantine(db, integration_id=intg_b.id, created_at=ten_days_ago)
        db.commit()

        org_a_id = org_a.id
        org_b_id = org_b.id
        ev_a_id = ev_a.id
        ev_b_id = ev_b.id

    # Executa task.
    result = prune_expired_quarantine.run()

    # Org A purged (10d > 7d), Org B mantido (10d < 30d).
    assert str(org_a_id) in result, "Org A deveria ter eventos purgados"
    assert str(org_b_id) not in result, "Org B não deveria ter eventos purgados"

    with db_session() as db:
        assert db.get(models.QuarantineEvent, ev_a_id) is None
        assert db.get(models.QuarantineEvent, ev_b_id) is not None


def test_prune_quarantine_default_retention_when_no_config(db_session) -> None:
    """Orgs sem config usam default de 7 dias."""
    from backend.app.collectors.retention_tasks import prune_expired_quarantine

    with db_session() as db:
        org = _seed_org(db, name="Org Sem Config")
        intg = _seed_integration(db, org_id=org.id)

        # Sem OrganizationRetentionConfig — usa default 7d.
        ten_days_ago = datetime.utcnow() - timedelta(days=10)
        ev = _seed_quarantine(db, integration_id=intg.id, created_at=ten_days_ago)
        db.commit()
        org_id = org.id
        ev_id = ev.id

    result = prune_expired_quarantine.run()
    assert str(org_id) in result

    with db_session() as db:
        assert db.get(models.QuarantineEvent, ev_id) is None


def test_prune_quarantine_keeps_recent_events(db_session) -> None:
    """Eventos dentro do prazo de retenção não devem ser deletados."""
    from backend.app.collectors.retention_tasks import prune_expired_quarantine

    with db_session() as db:
        org = _seed_org(db, name="Org Recent")
        intg = _seed_integration(db, org_id=org.id)
        _seed_retention(db, org_id=org.id, quarantine_days=30)

        two_days_ago = datetime.utcnow() - timedelta(days=2)
        ev = _seed_quarantine(db, integration_id=intg.id, created_at=two_days_ago)
        db.commit()
        ev_id = ev.id

    prune_expired_quarantine.run()

    with db_session() as db:
        assert db.get(models.QuarantineEvent, ev_id) is not None


# ── Testes prune_expired_drift ────────────────────────────────────────


def test_prune_drift_filters_by_organization(db_session) -> None:
    """Drift da org A (expirado) é purgado; drift de OUTRA org
    permanece — isolamento EXATO por organization_id (não mais por vendor)."""
    from backend.app.collectors.retention_tasks import prune_expired_drift

    with db_session() as db:
        org_a = _seed_org(db, name="Org Drift A")
        org_b = _seed_org(db, name="Org Drift B")
        _seed_integration(db, org_id=org_a.id, platform="sophos")
        # org A: retenção curta (7d). org B: retenção longa (365d) — assim o
        # drift de B (100d) sobrevive ao SEU pass, e provamos que o pass da
        # org A (scoped) não toca no drift de B mesmo sendo o MESMO vendor.
        _seed_retention(db, org_id=org_a.id, drift_days=7)
        _seed_retention(db, org_id=org_b.id, drift_days=365)

        old_date = datetime.utcnow() - timedelta(days=100)
        drift_a = _seed_drift(
            db, vendor="sophos", last_seen=old_date, organization_id=org_a.id
        )
        drift_b = _seed_drift(
            db, vendor="sophos", last_seen=old_date, organization_id=org_b.id
        )
        db.commit()
        a_id = drift_a.id
        b_id = drift_b.id

    prune_expired_drift.run()

    with db_session() as db:
        # drift da org A: deletado (org dona + expirou os 7d).
        assert db.get(models.UnknownField, a_id) is None
        # drift da org B: permanece — o pass scoped da org A NÃO o tocou
        # (mesmo vendor!) e a retenção de B (365d) o mantém. Comportamento
        # antigo (por vendor) o teria deletado durante o pass da org A.
        assert db.get(models.UnknownField, b_id) is not None


# ── Testes prune_expired_history ──────────────────────────────────────


def test_prune_history_filters_by_org_integration(db_session) -> None:
    """History expirada via integration_id da org é purgada."""
    from backend.app.collectors.retention_tasks import prune_expired_history

    with db_session() as db:
        org = _seed_org(db, name="Org History")
        intg = _seed_integration(db, org_id=org.id)
        _seed_retention(db, org_id=org.id, history_days=7)

        old_date = datetime.utcnow() - timedelta(days=60)
        h = _seed_history(db, integration_id=intg.id, timestamp=old_date)
        db.commit()
        h_id = h.id

    prune_expired_history.run()

    with db_session() as db:
        assert db.get(models.History, h_id) is None


# ── Testes prune_expired_audit_logs ───────────────────────────────────


def test_prune_audit_logs_keeps_system_events(db_session) -> None:
    """Entradas com user_id=NULL (eventos de sistema) NUNCA são deletadas."""
    from backend.app.collectors.retention_tasks import prune_expired_audit_logs

    with db_session() as db:
        org = _seed_org(db, name="Org Audit")
        user = models.AppUser(
            username=f"user-{uuid4().hex[:6]}",
            password_hash="x",
            organization_id=org.id,
            role="viewer",
        )
        db.add(user)
        db.flush()

        _seed_retention(db, org_id=org.id, audit_days=7)

        old_date = datetime.utcnow() - timedelta(days=400)

        # Evento de sistema (sem user_id).
        system_log = _seed_audit_log(db, user_id=None, created_at=old_date)
        # Evento de usuário da org (deve ser deletado).
        user_log = _seed_audit_log(db, user_id=user.id, created_at=old_date)
        db.commit()
        sys_id = system_log.id
        usr_id = user_log.id

    prune_expired_audit_logs.run()

    with db_session() as db:
        # Evento de sistema deve ser preservado.
        assert db.get(models.AuditLog, sys_id) is not None, (
            "Evento de sistema (user_id=NULL) não deve ser deletado"
        )
        # Evento de usuário expirado deve ser deletado.
        assert db.get(models.AuditLog, usr_id) is None


# ── Testes prune_all ──────────────────────────────────────────────────


def test_prune_all_aggregates_results(db_session) -> None:
    """prune_all deve retornar dict com todas as chaves de sub-tasks."""
    from backend.app.collectors.retention_tasks import prune_all

    result = prune_all.run()

    assert isinstance(result, dict)
    # Deve conter as chaves das sub-tasks.
    assert "quarantine" in result
    assert "drift" in result
    assert "history" in result
    assert "audit_logs" in result
