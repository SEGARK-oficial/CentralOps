"""Índice único parcial ``uq_enrich_policy_one_enabled`` contra Postgres REAL.

O SQLite dos testes unitários aceita ``WHERE enabled`` e ``SET enabled = FALSE``,
mas índice parcial e literal booleano são justamente o que diverge entre os
dialetos. Este teste prova, no Postgres:

1. ``create_all`` cria o índice parcial e ele recusa a segunda habilitada;
2. a migração de boot numa base ANTIGA (duas habilitadas, sem índice) desliga a
   sombreada, mantém a que o runtime aplicava e cria o índice — sem derrubar o
   boot, que era o risco de criar o índice direto sobre dado legado.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Iterator

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from backend.app.db import models  # noqa: F401  registra as tabelas
from backend.app.db.database import Base, _ensure_single_active_enrichment_policy

pytestmark = pytest.mark.pg


def _force_psycopg2(url: str) -> str:
    if url.startswith("postgresql+"):
        return url
    return url.replace("postgresql://", "postgresql+psycopg2://", 1)


@pytest.fixture(scope="module")
def pg_engine() -> Iterator[Engine]:
    pg_url = os.environ.get("PG_TEST_DSN")
    if pg_url:
        engine = create_engine(_force_psycopg2(pg_url), future=True)
        try:
            yield engine
        finally:
            engine.dispose()
        return
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:
        pytest.skip("precisa de PG_TEST_DSN ou `pip install 'testcontainers[postgres]'`")
    with PostgresContainer("postgres:16-alpine") as container:
        engine = create_engine(_force_psycopg2(container.get_connection_url()), future=True)
        try:
            yield engine
        finally:
            engine.dispose()


@pytest.fixture(autouse=True)
def _schema(pg_engine: Engine) -> Iterator[None]:
    with pg_engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    Base.metadata.create_all(bind=pg_engine)
    yield


def _seed(conn) -> None:
    from sqlalchemy.orm import Session

    with Session(bind=conn) as db:  # ORM preenche os defaults da org
        org = models.Organization(name="o", slug="o")
        db.add(org)
        db.flush()
        org_id = org.id
    base = datetime(2026, 1, 1)
    for i, pid in enumerate(("antiga", "nova", "outra")):
        conn.execute(
            text(
                "INSERT INTO enrichment_policies "
                "(id, organization_id, name, enabled, is_template, created_at, updated_at) "
                "VALUES (:id, :org, :id, FALSE, FALSE, :ts, :ts)"
            ),
            {"id": pid, "org": org_id, "ts": base + timedelta(minutes=i)},
        )


def test_indice_parcial_recusa_a_segunda_habilitada(pg_engine: Engine) -> None:
    with pg_engine.begin() as conn:
        _seed(conn)
        conn.execute(text("UPDATE enrichment_policies SET enabled = TRUE WHERE id = 'antiga'"))
    with pytest.raises(IntegrityError):
        with pg_engine.begin() as conn:
            conn.execute(text("UPDATE enrichment_policies SET enabled = TRUE WHERE id = 'nova'"))
    # Desligadas não contam.
    with pg_engine.begin() as conn:
        n = conn.execute(
            text("SELECT count(*) FROM enrichment_policies WHERE NOT enabled")
        ).scalar()
    assert n == 2


def test_migracao_em_base_antiga_desliga_a_sombreada_e_cria_o_indice(pg_engine: Engine) -> None:
    with pg_engine.begin() as conn:
        conn.execute(text("DROP INDEX uq_enrich_policy_one_enabled"))
        _seed(conn)
        conn.execute(
            text("UPDATE enrichment_policies SET enabled = TRUE WHERE id IN ('antiga', 'nova')")
        )

    with pg_engine.begin() as conn:
        assert _ensure_single_active_enrichment_policy(conn) == 1

    with pg_engine.begin() as conn:
        ligadas = [
            r[0]
            for r in conn.execute(text("SELECT id FROM enrichment_policies WHERE enabled"))
        ]
        indexdef = conn.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_enrich_policy_one_enabled'")
        ).scalar()
    assert ligadas == ["antiga"]
    assert indexdef is not None and "UNIQUE" in indexdef and "WHERE enabled" in indexdef

    with pg_engine.begin() as conn:  # idempotente
        assert _ensure_single_active_enrichment_policy(conn) == 0


def test_migracao_de_boot_adiciona_as_flags_de_heranca_em_base_existente(
    pg_engine: Engine, monkeypatch
) -> None:
    """``create_all`` num banco novo já cria as colunas; o ALTER só dispara numa
    base EXISTENTE — o cenário que o SQLite não pega (BOOLEAN com ``DEFAULT 0``
    derruba o boot no Postgres)."""
    from backend.app.db import database as db_module

    cols = {
        "enrichment_policies": ("template_sync", "template_enable_children"),
        "enrichment_sources": ("share_with_descendants",),
    }
    with pg_engine.begin() as conn:
        _seed(conn)
        for table, names in cols.items():
            for name in names:
                conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {name}"))

    monkeypatch.setattr(db_module, "engine", pg_engine)
    db_module._run_lightweight_migrations()

    with pg_engine.begin() as conn:
        for table, names in cols.items():
            for name in names:
                row = conn.execute(
                    text(
                        "SELECT data_type, is_nullable, column_default "
                        "FROM information_schema.columns "
                        "WHERE table_name = :t AND column_name = :c"
                    ),
                    {"t": table, "c": name},
                ).one()
                assert row[0] == "boolean" and row[1] == "NO" and "false" in str(row[2])
        # Linhas antigas ganham o default.
        assert conn.execute(
            text("SELECT count(*) FROM enrichment_policies WHERE template_sync")
        ).scalar() == 0
