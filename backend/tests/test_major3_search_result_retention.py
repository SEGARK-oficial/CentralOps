"""Testes para MAJOR 3 — prune_expired_search_results (F5-S5).

search_result_retention_days existe no modelo OrganizationRetentionConfig
mas não havia task de purge correspondente. Crescimento ilimitado.

Cenários cobertos:
- Purge respeita retenção por organização (org A=7d, org B=30d).
- SearchResult recente não é deletado.
- Sem config: usa default _DEFAULT_SEARCH_RESULT_DAYS (7 dias).
- prune_all inclui search_results na execução.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import database, models
from backend.app.db.database import Base


# ── Fixture ────────────────────────────────────────────────────────────


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    original = database.SessionLocal
    database.SessionLocal = Session  # type: ignore[assignment]

    yield Session

    database.SessionLocal = original  # type: ignore[assignment]
    Base.metadata.drop_all(bind=engine)


# ── Helpers ────────────────────────────────────────────────────────────


def _seed_org(db, *, name: str | None = None) -> models.Organization:
    slug = f"org-{uuid4().hex[:8]}"
    org = models.Organization(name=name or slug, slug=slug, is_active=True)
    db.add(org)
    db.flush()
    return org


def _seed_integration(db, *, org_id: int) -> models.Integration:
    intg = models.Integration(
        organization_id=org_id,
        name=f"intg-{uuid4().hex[:6]}",
        platform="sophos",
    )
    db.add(intg)
    db.flush()
    return intg


def _seed_retention(db, *, org_id: int, search_days: int) -> models.OrganizationRetentionConfig:
    cfg = models.OrganizationRetentionConfig(
        organization_id=org_id,
        search_result_retention_days=search_days,
    )
    db.add(cfg)
    db.flush()
    return cfg


def _seed_search_result(
    db,
    *,
    integration_id: int,
    created_at: datetime,
) -> models.SearchResult:
    sr = models.SearchResult(
        search_id=str(uuid4()),
        integration_id=integration_id,
        platform="sophos",
        statement="SELECT * FROM xdr_data",
        table="xdr_data",
        from_ts="2026-01-01T00:00:00",
        to_ts="2026-01-02T00:00:00",
        status="completed",
        engine="query",
        language="sql",
        created_at=created_at,
    )
    db.add(sr)
    db.flush()
    return sr


# ── Testes ─────────────────────────────────────────────────────────────


def test_prune_search_results_respects_per_org_retention(db_session) -> None:
    """Org A (7d) e Org B (30d): search_result de 10 dias atrás.

    Org A deve ser purgada; Org B mantida.
    """
    from backend.app.collectors.retention_tasks import prune_expired_search_results

    with db_session() as db:
        org_a = _seed_org(db, name="Org A Search")
        org_b = _seed_org(db, name="Org B Search")

        intg_a = _seed_integration(db, org_id=org_a.id)
        intg_b = _seed_integration(db, org_id=org_b.id)

        _seed_retention(db, org_id=org_a.id, search_days=7)
        _seed_retention(db, org_id=org_b.id, search_days=30)

        ten_days_ago = datetime.utcnow() - timedelta(days=10)
        sr_a = _seed_search_result(db, integration_id=intg_a.id, created_at=ten_days_ago)
        sr_b = _seed_search_result(db, integration_id=intg_b.id, created_at=ten_days_ago)
        db.commit()

        org_a_id = org_a.id
        org_b_id = org_b.id
        sr_a_id = sr_a.id
        sr_b_id = sr_b.id

    result = prune_expired_search_results.run()

    assert str(org_a_id) in result, "Org A deveria ter search_results purgados"
    assert str(org_b_id) not in result, "Org B não deveria ter search_results purgados"

    with db_session() as db:
        assert db.get(models.SearchResult, sr_a_id) is None, "SearchResult de Org A deve ser deletado"
        assert db.get(models.SearchResult, sr_b_id) is not None, "SearchResult de Org B deve ser mantido"


def test_prune_search_results_keeps_recent(db_session) -> None:
    """SearchResult criado há 2 dias com retenção de 7 dias deve ser mantido."""
    from backend.app.collectors.retention_tasks import prune_expired_search_results

    with db_session() as db:
        org = _seed_org(db, name="Org Recent Search")
        intg = _seed_integration(db, org_id=org.id)
        _seed_retention(db, org_id=org.id, search_days=7)

        two_days_ago = datetime.utcnow() - timedelta(days=2)
        sr = _seed_search_result(db, integration_id=intg.id, created_at=two_days_ago)
        db.commit()
        sr_id = sr.id

    prune_expired_search_results.run()

    with db_session() as db:
        assert db.get(models.SearchResult, sr_id) is not None, (
            "SearchResult recente não deve ser deletado"
        )


def test_prune_search_results_uses_default_when_no_config(db_session) -> None:
    """Org sem config usa default de 7 dias."""
    from backend.app.collectors.retention_tasks import prune_expired_search_results

    with db_session() as db:
        org = _seed_org(db, name="Org Default Search")
        intg = _seed_integration(db, org_id=org.id)
        # Sem OrganizationRetentionConfig — usa default 7d.

        ten_days_ago = datetime.utcnow() - timedelta(days=10)
        sr = _seed_search_result(db, integration_id=intg.id, created_at=ten_days_ago)
        db.commit()
        org_id = org.id
        sr_id = sr.id

    result = prune_expired_search_results.run()

    assert str(org_id) in result
    with db_session() as db:
        assert db.get(models.SearchResult, sr_id) is None, (
            "SearchResult expirado deve ser deletado com retenção default"
        )


def test_prune_all_includes_search_results(db_session) -> None:
    """prune_all deve incluir 'search_results' no retorno."""
    from backend.app.collectors.retention_tasks import prune_all

    result = prune_all.run()

    assert "search_results" in result, (
        "prune_all deve incluir 'search_results' — MAJOR 3 — F5-S5"
    )
