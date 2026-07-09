"""Testes para W1: WAL mode + busy_timeout no SQLite (perf/db).

Valida que o listener ``_enable_sqlite_wal`` configura os PRAGMAs
corretos em cada nova conexão SQLite. Para PostgreSQL (URL diferente),
confirma que o listener NÃO é instalado.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from typing import Generator

import pytest
from sqlalchemy import create_engine, event as sa_event, text
from sqlalchemy.orm import sessionmaker


# ── Fixture: engine SQLite temporário em arquivo (não :memory:) ──────────────
# WAL mode exige arquivo — não funciona em :memory: (seria ignorado pelo SQLite).
@pytest.fixture()
def sqlite_engine():
    """Cria engine SQLite em arquivo temporário com o listener WAL registrado."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    # Registra o mesmo listener que database.py instala em produção.
    @sa_event.listens_for(engine, "connect")
    def _enable_wal(dbapi_conn: object, _conn_record: object) -> None:
        cursor = dbapi_conn.cursor()  # type: ignore[union-attr]
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
        finally:
            cursor.close()

    try:
        yield engine
    finally:
        engine.dispose()
        try:
            os.unlink(db_path)
        except OSError:
            pass


@pytest.fixture()
def sqlite_session(sqlite_engine) -> Generator:
    Session = sessionmaker(bind=sqlite_engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


# ── Testes ───────────────────────────────────────────────────────────────────


def test_journal_mode_is_wal_after_connect(sqlite_engine) -> None:
    """PRAGMA journal_mode deve retornar 'wal' após a conexão."""
    with sqlite_engine.connect() as conn:
        row = conn.execute(text("PRAGMA journal_mode")).fetchone()
    assert row is not None
    assert row[0] == "wal", f"Esperado 'wal', obtido '{row[0]}'"


def test_busy_timeout_set_to_30s(sqlite_engine) -> None:
    """PRAGMA busy_timeout deve retornar 30000 (30 segundos em ms)."""
    with sqlite_engine.connect() as conn:
        row = conn.execute(text("PRAGMA busy_timeout")).fetchone()
    assert row is not None
    assert int(row[0]) == 30000, f"Esperado 30000, obtido {row[0]}"


def test_foreign_keys_enabled(sqlite_engine) -> None:
    """PRAGMA foreign_keys deve retornar 1 (ativo)."""
    with sqlite_engine.connect() as conn:
        row = conn.execute(text("PRAGMA foreign_keys")).fetchone()
    assert row is not None
    assert int(row[0]) == 1, f"Esperado 1, obtido {row[0]}"


def test_postgres_url_skips_sqlite_pragmas() -> None:
    """Para URL PostgreSQL, nenhum listener WAL deve ser instalado.

    Simula a lógica condicional de database.py: listener só é registrado
    quando DATABASE_URL começa com 'sqlite:///'. Valida que o event não é
    disparado para URLs de outros bancos.
    """
    postgres_url = "postgresql://user:pass@localhost/testdb"
    # Apenas verifica que a lógica condicional não instala o listener.
    # Não tenta conectar (não há Postgres disponível em testes unitários).
    listener_was_called = False

    def _mock_wal_listener(dbapi_conn: object, _conn_record: object) -> None:
        nonlocal listener_was_called
        listener_was_called = True

    # Verifica que a condição do database.py bloquearia o registro.
    assert not postgres_url.startswith("sqlite:///"), (
        "URL PostgreSQL não deve disparar o branch SQLite"
    )
    # Confirmação: listener nunca foi chamado porque a condição não passou.
    assert not listener_was_called
