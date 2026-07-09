"""Regression: Postgres api_tokens.user_id deve ser NULLABLE pós-Fase 2.

Schema original (Fase 1) tinha ``user_id NOT NULL`` porque todo PAT pertencia
a um AppUser. Fase 2 introduziu Service Accounts: tokens com
``service_account_id`` setado e ``user_id=NULL`` (XOR via CheckConstraint).

O model SQLAlchemy declara ``nullable=True``, mas DBs criados antes da Fase 2
mantêm a constraint NOT NULL no Postgres — `_run_lightweight_migrations` não
alterava o constraint. Bug em prod (2026-05-07): ``POST /service-accounts/
{id}/tokens`` → 500 com NotNullViolation. SQLite mascarava (constraint
não-enforced).

Este teste roda contra Postgres real (testcontainers ou PG_TEST_DSN). Para
SQLite, o teste é skipped — a migration não toca SQLite (model em
``create_all`` já cria nullable).
"""

from __future__ import annotations

import os
from typing import Iterator

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from backend.app.db import database as db_module
from backend.app.db import models  # noqa: F401  registers tables
from backend.app.db.database import Base, _run_lightweight_migrations


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


def _is_user_id_nullable(engine: Engine) -> bool:
    with engine.connect() as conn:
        value = conn.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'api_tokens' AND column_name = 'user_id'"
            )
        ).scalar()
    return value == "YES"


def test_migration_drops_not_null_on_user_id_when_legacy_schema(pg_engine: Engine) -> None:
    """Simula schema legacy (Fase 1) com user_id NOT NULL, roda migrations,
    verifica que vira NULLABLE — destrava insert de service-account token."""
    Base.metadata.create_all(bind=pg_engine)
    _run_lightweight_migrations()

    # Forçar de volta pra NOT NULL pra simular schema antigo.
    with pg_engine.begin() as conn:
        conn.execute(
            text("ALTER TABLE api_tokens ALTER COLUMN user_id SET NOT NULL")
        )
    assert not _is_user_id_nullable(pg_engine), "setup falhou"

    _run_lightweight_migrations()

    assert _is_user_id_nullable(pg_engine), (
        "Migration deveria ter relaxado user_id NOT NULL — "
        "regressão do bug 2026-05-07 (POST /service-accounts/{id}/tokens 500)."
    )


def test_service_account_token_insert_succeeds_after_migration(pg_engine: Engine) -> None:
    """Cenário direto do bug: inserir api_tokens row com user_id=NULL e
    service_account_id setado deve funcionar pós-migration."""
    from datetime import datetime

    Base.metadata.create_all(bind=pg_engine)
    _run_lightweight_migrations()

    now = datetime.utcnow()
    with pg_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO service_accounts "
                "(name, description, role, organization_id, is_active, created_by_user_id, "
                " created_at, updated_at) "
                "VALUES ('iasoc-test', 'test', 'viewer', NULL, TRUE, NULL, :now, :now)"
            ),
            {"now": now},
        )
        sa_id = conn.execute(
            text("SELECT id FROM service_accounts WHERE name = 'iasoc-test'")
        ).scalar()
        # O insert do bug original — deve completar sem NotNullViolation.
        conn.execute(
            text(
                "INSERT INTO api_tokens "
                "(user_id, service_account_id, name, token_prefix, token_hash, "
                " expires_at, is_eternal, scopes_json, last_used_at, last_used_ip, "
                " use_count, revoked_at, created_at) "
                "VALUES (NULL, :sa_id, 'tok-test', 'copsk_xxxx', '$argon2id$fake', "
                "        NULL, TRUE, NULL, NULL, NULL, 0, NULL, :now)"
            ),
            {"sa_id": sa_id, "now": now},
        )

    with pg_engine.connect() as conn:
        cnt = conn.execute(
            text(
                "SELECT COUNT(*) FROM api_tokens "
                "WHERE service_account_id = :sa_id AND user_id IS NULL"
            ),
            {"sa_id": sa_id},
        ).scalar()
    assert cnt == 1, "Token de SA deveria existir com user_id=NULL"


def test_idempotent_three_runs(pg_engine: Engine) -> None:
    """3 runs consecutivas não disparam ALTER toda vez (skip via is_nullable check)."""
    Base.metadata.create_all(bind=pg_engine)
    for _ in range(3):
        _run_lightweight_migrations()
    assert _is_user_id_nullable(pg_engine)
