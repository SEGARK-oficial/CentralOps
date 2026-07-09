"""Regression: migração lightweight de scheduled_queries usa TIMESTAMP (Postgres).

Bug de produção (2026-06-23): o boot da
API abortava no Postgres com ``type "datetime" does not exist`` ao adicionar
``scheduled_queries.last_error_at`` — porque ``_run_lightweight_migrations``
emitia ``ALTER TABLE ... ADD COLUMN ... DATETIME``. ``DATETIME`` é tipo de
SQLite/MySQL; o PostgreSQL não o reconhece (usa ``TIMESTAMP``).

Escapou da suíte porque os testes unitários rodam em **SQLite**, onde ``DATETIME``
é aceito. E ``create_all`` num DB novo já cria a coluna com o tipo certo, então
o ALTER só dispara num DB **existente** que ganha a coluna nova — exatamente o
cenário de produção.

Este teste reproduz esse cenário contra **Postgres real** (mesmo padrão de
``test_lightweight_migrations_api_tokens_user_id.py``): cria o schema, simula um
DB antigo droppando as colunas de data/hora, re-roda a migração e exige que ela
(a) NÃO levante e (b) re-adicione as colunas como ``timestamp``.
"""

from __future__ import annotations

import os
from typing import Iterator

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from backend.app.db import database as db_module
from backend.app.db import models  # noqa: F401  registra as tabelas
from backend.app.db.database import Base, _run_lightweight_migrations


pytestmark = pytest.mark.pg

# As colunas de data/hora de scheduled_queries adicionadas via ALTER lightweight
# (eram DATETIME — incompatível com Postgres; corrigidas para TIMESTAMP).
_DATETIME_COLS = ("last_error_at", "last_run_at", "created_at", "updated_at")


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
        pytest.skip(
            "Postgres migration test needs PG_TEST_DSN or "
            "`pip install 'testcontainers[postgres]'`"
        )

    with PostgresContainer("postgres:16-alpine") as container:
        engine = create_engine(_force_psycopg2(container.get_connection_url()), future=True)
        try:
            yield engine
        finally:
            engine.dispose()


@pytest.fixture(autouse=True)
def _reset_schema_and_patch_engine(pg_engine: Engine, monkeypatch) -> Iterator[Engine]:
    with pg_engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    monkeypatch.setattr(db_module, "engine", pg_engine)
    yield pg_engine


def _datetime_column_types(engine: Engine) -> dict[str, str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = 'scheduled_queries' AND column_name = ANY(:cols)"
            ),
            {"cols": list(_DATETIME_COLS)},
        ).all()
    return {name: dtype for name, dtype in rows}


def test_scheduled_queries_datetime_alter_uses_timestamp_on_postgres(pg_engine: Engine) -> None:
    """Reproduz o cenário de produção: DB existente SEM as colunas de data/hora.

    Antes do fix, o ALTER ... DATETIME levantava ``ProgrammingError: type
    "datetime" does not exist`` e abortava o boot. Agora usa TIMESTAMP.
    """
    Base.metadata.create_all(bind=pg_engine)
    _run_lightweight_migrations()

    # Simula um DB antigo: remove as colunas de data/hora
    # para forçar o ramo ADD COLUMN da migração lightweight.
    with pg_engine.begin() as conn:
        for col in _DATETIME_COLS:
            conn.execute(text(f"ALTER TABLE scheduled_queries DROP COLUMN IF EXISTS {col}"))
    assert _datetime_column_types(pg_engine) == {}, "setup falhou — colunas não removidas"

    # Não deve levantar (era aqui que o boot quebrava em produção).
    _run_lightweight_migrations()

    types = _datetime_column_types(pg_engine)
    assert set(types) == set(_DATETIME_COLS), (
        f"migração não re-adicionou as colunas de data/hora: {types}"
    )
    for col, dtype in types.items():
        assert dtype.startswith("timestamp"), (
            f"{col} deveria ser TIMESTAMP no Postgres (não {dtype!r}) — "
            "regressão do bug DATETIME de 2026-06-23 (boot Postgres)."
        )


def test_scheduled_queries_migration_idempotent_on_postgres(pg_engine: Engine) -> None:
    """3 runs consecutivas não quebram nem duplicam (skip via checagem de coluna)."""
    Base.metadata.create_all(bind=pg_engine)
    for _ in range(3):
        _run_lightweight_migrations()
    types = _datetime_column_types(pg_engine)
    assert set(types) == set(_DATETIME_COLS)
    assert all(dtype.startswith("timestamp") for dtype in types.values())
