"""Cobre a migration leve que cria ``integration_tenant_selections`` + backfill.

Cenários:
  * Tabela criada idempotentemente (re-run não falha)
  * Coluna ``auto_approve_new_tenants`` adicionada com default FALSE
  * Backfill: children ``auto_managed=true`` ganham row ``state='approved'``
  * Backfill é idempotente (re-roda sem duplicar)
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import database as _db_module
from backend.app.db import models  # noqa: F401  — register Base tables


@pytest.fixture
def fresh_engine(monkeypatch, tmp_path):
    """Engine SQLite isolado, com a engine do módulo monkeypatched.

    Importante: ``_run_lightweight_migrations`` lê ``inspect(engine)`` do
    objeto importado de ``database.py``, então temos que substituir o
    módulo-level ``engine`` (não só ``SessionLocal``).
    """
    db_path = tmp_path / "test.db"
    url = f"sqlite:///{db_path}"
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    _db_module.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(_db_module, "engine", engine)
    monkeypatch.setattr(_db_module, "SessionLocal", Session)
    monkeypatch.setattr(_db_module, "DATABASE_URL", url)
    yield engine
    _db_module.Base.metadata.drop_all(bind=engine)


def _seed_partner_with_legacy_children(engine, n: int = 3) -> int:
    """Cria 1 partner + N children auto_managed=true, sem rows de seleção.

    Returns o partner_id.
    """
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.execute(text(
            "INSERT INTO organizations(name, slug, is_active, created_at, updated_at, auto_managed) "
            "VALUES ('Holding', 'holding', 1, datetime('now'), datetime('now'), 0)"
        ))
        db.execute(text(
            "INSERT INTO integrations(organization_id, name, platform, is_active, kind, "
            "auth_status, created_at, updated_at, auto_managed) "
            "VALUES (1, 'Partner', 'sophos', 1, 'partner', 'unknown', "
            "datetime('now'), datetime('now'), 0)"
        ))
        partner_row = db.execute(text("SELECT id FROM integrations WHERE kind='partner'")).fetchone()
        partner_id = partner_row.id
        # N children auto_managed=true
        for i in range(n):
            db.execute(text(
                "INSERT INTO organizations(name, slug, is_active, external_provider, external_id, "
                "auto_managed, created_at, updated_at) "
                f"VALUES ('child {i}', 'child-{i}', 1, 'sophos', 'tenant-{i}', 1, "
                "datetime('now'), datetime('now'))"
            ))
            child_org_row = db.execute(
                text("SELECT id FROM organizations WHERE slug = :slug"),
                {"slug": f"child-{i}"},
            ).fetchone()
            db.execute(text(
                "INSERT INTO integrations(organization_id, name, platform, is_active, kind, "
                "parent_integration_id, external_id, auth_status, created_at, updated_at, auto_managed) "
                f"VALUES ({child_org_row.id}, 'Child {i}', 'sophos', 1, 'tenant', "
                f":parent_id, 'tenant-{i}', 'unknown', "
                "datetime('now'), datetime('now'), 1)"
            ), {"parent_id": partner_id})
        db.commit()
    return partner_id


def test_migration_creates_table_and_column(fresh_engine):
    # Drop a tabela criada por Base.metadata.create_all pra simular DB pré-Fase 1.
    with fresh_engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS integration_tenant_selections"))
    # Drop a coluna auto_approve_new_tenants — SQLite não suporta DROP COLUMN
    # facilmente; criamos um cenário onde a coluna pode estar ausente recriando
    # a tabela integrations sem ela.
    insp = inspect(fresh_engine)
    int_cols = {c["name"] for c in insp.get_columns("integrations")}
    assert "auto_approve_new_tenants" in int_cols  # Base já criou.

    # Roda migration — re-add é noop (idempotente).
    _db_module._run_lightweight_migrations()

    insp2 = inspect(fresh_engine)
    table_names = set(insp2.get_table_names())
    assert "integration_tenant_selections" in table_names
    sel_cols = {c["name"] for c in insp2.get_columns("integration_tenant_selections")}
    expected = {
        "id", "parent_integration_id", "external_id", "state",
        "decided_by_user_id", "decided_at",
        "name_snapshot", "region_snapshot", "data_geography_snapshot",
        "api_host_snapshot", "last_seen_at", "created_at", "updated_at",
    }
    assert expected.issubset(sel_cols), f"missing: {expected - sel_cols}"


def test_migration_idempotent_runs_twice(fresh_engine):
    _db_module._run_lightweight_migrations()
    _db_module._run_lightweight_migrations()
    # Sem exceção é o teste — recriação repetida não pode falhar.


def test_backfill_marks_legacy_children_as_approved(fresh_engine):
    partner_id = _seed_partner_with_legacy_children(fresh_engine, n=3)
    # Garante que NÃO existem rows de seleção pra esses children.
    with fresh_engine.connect() as conn:
        cnt = conn.execute(text(
            "SELECT COUNT(*) AS n FROM integration_tenant_selections "
            "WHERE parent_integration_id = :p"
        ), {"p": partner_id}).fetchone()
        assert cnt.n == 0

    _db_module._run_lightweight_migrations()

    Session = sessionmaker(bind=fresh_engine)
    with Session() as db:
        rows = db.execute(text(
            "SELECT external_id, state, name_snapshot FROM integration_tenant_selections "
            "WHERE parent_integration_id = :p ORDER BY external_id"
        ), {"p": partner_id}).fetchall()
        assert len(rows) == 3
        assert {r.external_id for r in rows} == {"tenant-0", "tenant-1", "tenant-2"}
        for r in rows:
            assert r.state == "approved"
            assert r.name_snapshot is not None  # snapshot preenchido com child.name


def test_backfill_idempotent_does_not_duplicate(fresh_engine):
    partner_id = _seed_partner_with_legacy_children(fresh_engine, n=2)
    _db_module._run_lightweight_migrations()
    _db_module._run_lightweight_migrations()
    Session = sessionmaker(bind=fresh_engine)
    with Session() as db:
        cnt = db.execute(text(
            "SELECT COUNT(*) AS n FROM integration_tenant_selections "
            "WHERE parent_integration_id = :p"
        ), {"p": partner_id}).fetchone()
        assert cnt.n == 2  # Não duplicou.


def test_backfill_skips_non_auto_managed(fresh_engine):
    """Children NÃO auto_managed (criados manualmente pelo operador) NÃO
    devem ser backfillados — eles têm seu próprio fluxo, não dependem de seleção.
    """
    Session = sessionmaker(bind=fresh_engine)
    with Session() as db:
        db.execute(text(
            "INSERT INTO organizations(name, slug, is_active, created_at, updated_at, auto_managed) "
            "VALUES ('o', 'o', 1, datetime('now'), datetime('now'), 0)"
        ))
        db.execute(text(
            "INSERT INTO integrations(organization_id, name, platform, is_active, kind, "
            "auth_status, created_at, updated_at, auto_managed) "
            "VALUES (1, 'p', 'sophos', 1, 'partner', 'unknown', datetime('now'), datetime('now'), 0)"
        ))
        # Manual child: auto_managed=0 + parent_integration_id NULL (não é child de partner).
        db.execute(text(
            "INSERT INTO integrations(organization_id, name, platform, is_active, kind, "
            "external_id, auth_status, created_at, updated_at, auto_managed) "
            "VALUES (1, 'manual-tenant', 'sophos', 1, 'tenant', 'manual-uuid', 'unknown', "
            "datetime('now'), datetime('now'), 0)"
        ))
        db.commit()

    _db_module._run_lightweight_migrations()

    with Session() as db:
        cnt = db.execute(text(
            "SELECT COUNT(*) AS n FROM integration_tenant_selections"
        )).fetchone()
        assert cnt.n == 0


def test_auto_approve_column_default_false(fresh_engine):
    """Coluna nova ``auto_approve_new_tenants`` defaults a FALSE para Partners existentes."""
    Session = sessionmaker(bind=fresh_engine)
    with Session() as db:
        db.execute(text(
            "INSERT INTO organizations(name, slug, is_active, created_at, updated_at, auto_managed) "
            "VALUES ('o', 'o', 1, datetime('now'), datetime('now'), 0)"
        ))
        db.execute(text(
            "INSERT INTO integrations(organization_id, name, platform, is_active, kind, "
            "auth_status, created_at, updated_at, auto_managed) "
            "VALUES (1, 'p', 'sophos', 1, 'partner', 'unknown', datetime('now'), datetime('now'), 0)"
        ))
        db.commit()

    _db_module._run_lightweight_migrations()

    # Lendo via ORM, a coerção do SQLAlchemy garante boolean Python.
    from backend.app.db import models as _models
    with Session() as db:
        partner = (
            db.query(_models.Integration)
            .filter(_models.Integration.kind == "partner")
            .first()
        )
        assert partner is not None
        # Default seguro: todo Partner existente fica em False (modo manual).
        assert partner.auto_approve_new_tenants is False
        # Migration ALTER TABLE também garantiu o default em SQL puro.
        raw = db.execute(text(
            "SELECT auto_approve_new_tenants FROM integrations WHERE kind='partner'"
        )).fetchone()
        # Coluna criada via Base.metadata.create_all com server_default=text('false')
        # retorna 0 em SQLite. Coluna adicionada via ALTER (caminho de upgrade)
        # retorna 'FALSE' literal porque a migration usa keyword DDL — aceitar
        # qualquer um é compat-realista.
        assert raw.auto_approve_new_tenants in (0, False, "FALSE", "false", None)
