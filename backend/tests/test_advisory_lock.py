"""O init de schema é serializado por um advisory lock no Postgres.

Fecha o gap de auditoria: o lock em ``initialize_database()`` NUNCA era exercido
(todos os outros testes chamam ``_run_lightweight_migrations()`` direto,
bypassando o wrapper). Aqui provamos o invariante de concorrência:

1. No caminho Postgres, ``initialize_database`` adquire ``pg_advisory_lock``
   ANTES do DDL e o libera (``pg_advisory_unlock``) DEPOIS — sob AUTOCOMMIT.
2. O unlock roda mesmo se o DDL levantar (``finally``) e a exceção re-propaga.
3. No SQLite (instância única) o lock é dispensado.
4. [gated em Postgres real] o ``pg_advisory_lock`` REALMENTE serializa: enquanto
   uma sessão o detém, outra não consegue ``pg_try_advisory_lock``.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest

from backend.app.db import database


# ── Fakes que registram a sequência de chamadas no caminho Postgres ──────


class _FakeLockConn:
    def __init__(self, log: list) -> None:
        self._log = log

    def execute(self, stmt, params=None):  # noqa: ANN001
        self._log.append(("execute", str(stmt), dict(params or {})))

    def __enter__(self) -> "_FakeLockConn":
        return self

    def __exit__(self, *exc) -> bool:  # noqa: ANN002
        self._log.append(("exit", None, None))
        return False


class _FakeConn:
    def __init__(self, log: list) -> None:
        self._log = log

    def execution_options(self, **kw):  # noqa: ANN003
        self._log.append(("execution_options", kw, None))
        return _FakeLockConn(self._log)


class _FakeEngine:
    def __init__(self, log: list) -> None:
        self._log = log

    def connect(self) -> _FakeConn:
        return _FakeConn(self._log)


def _patch_pg(monkeypatch, log, *, schema_init_raises=False):
    monkeypatch.setattr(database, "DATABASE_URL", "postgresql+psycopg2://x/y")
    monkeypatch.setattr(database, "engine", _FakeEngine(log))
    monkeypatch.setattr(database, "_wait_for_db", lambda *a, **k: None)

    # initialize_database envolve _do_init (schema + sync Alembic)
    # sob o lock. Espiamos _do_init como a unidade de trabalho protegida.
    def _do_init():
        log.append(("do_init", None, None))
        if schema_init_raises:
            raise RuntimeError("DDL boom")

    monkeypatch.setattr(database, "_do_init", _do_init)


def _kinds(log):
    """Sequência de eventos-chave (lock / do_init / unlock)."""
    out = []
    for kind, payload, _ in log:
        if kind == "execute" and "pg_advisory_lock" in payload:
            out.append("lock")
        elif kind == "execute" and "pg_advisory_unlock" in payload:
            out.append("unlock")
        elif kind == "do_init":
            out.append("do_init")
    return out


def test_advisory_lock_wraps_schema_init(monkeypatch):
    log: list = []
    _patch_pg(monkeypatch, log)

    database.initialize_database()

    # Ordem: lock ANTES do trabalho, unlock DEPOIS — exatamente 1 de cada.
    assert _kinds(log) == ["lock", "do_init", "unlock"]
    # A chave do lock é fixa, nos dois execute.
    lock_calls = [p for k, s, p in log if k == "execute"]
    assert all(p == {"k": database._MIGRATION_ADVISORY_LOCK_KEY} for p in lock_calls)
    # E o lock-conn roda em AUTOCOMMIT (sessão, não preso a transação).
    assert any(
        k == "execution_options" and s.get("isolation_level") == "AUTOCOMMIT"
        for k, s, _ in log
    )


def test_advisory_lock_released_on_schema_init_failure(monkeypatch):
    log: list = []
    _patch_pg(monkeypatch, log, schema_init_raises=True)

    with pytest.raises(RuntimeError, match="DDL boom"):
        database.initialize_database()

    # Mesmo com o trabalho falhando, o unlock roda (finally) — não vaza o lock.
    assert _kinds(log) == ["lock", "do_init", "unlock"]


def test_sqlite_path_skips_advisory_lock(monkeypatch):
    log: list = []
    monkeypatch.setattr(database, "DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setattr(database, "_wait_for_db", lambda *a, **k: None)
    called = {"do_init": False}
    monkeypatch.setattr(
        database, "engine", _FakeEngine(log)
    )  # não deve ser tocado no path sqlite

    def _do_init():
        called["do_init"] = True

    monkeypatch.setattr(database, "_do_init", _do_init)

    database.initialize_database()

    assert called["do_init"] is True
    assert log == [], "SQLite não deve abrir conexão de advisory lock"


# ── Real Postgres (gated): prova que o lock SERIALIZA de fato ────────────
# Roda quando CENTRALOPS_TEST_PG_DSN aponta p/ um Postgres (ex.: o serviço do
# docker-compose.e2e.yml ou um PG de CI). Sem ele, skip — o ambiente unit é SQLite.


@pytest.mark.pg
@pytest.mark.skipif(
    not os.environ.get("CENTRALOPS_TEST_PG_DSN"),
    reason="CENTRALOPS_TEST_PG_DSN não definido (sem Postgres real)",
)
def test_advisory_lock_serializes_on_real_postgres():
    from sqlalchemy import create_engine, text

    dsn = os.environ["CENTRALOPS_TEST_PG_DSN"]
    key = database._MIGRATION_ADVISORY_LOCK_KEY
    eng = create_engine(dsn)
    try:
        c1 = eng.connect().execution_options(isolation_level="AUTOCOMMIT")
        c2 = eng.connect().execution_options(isolation_level="AUTOCOMMIT")
        try:
            c1.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})
            # Enquanto c1 detém o lock, c2 NÃO consegue adquiri-lo.
            held = c2.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": key}
            ).scalar()
            assert held is False, "lock deveria estar retido por c1"
            # Após c1 liberar, c2 consegue.
            c1.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
            now = c2.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": key}
            ).scalar()
            assert now is True
            c2.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
        finally:
            c1.close()
            c2.close()
    finally:
        eng.dispose()
