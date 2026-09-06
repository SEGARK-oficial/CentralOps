"""``PredefinedQuery.severity_id`` e ``finding_shape`` existem de ponta a ponta.

O repo já pagou duas vezes o defeito "coluna + UI existem, schema não declara,
Pydantic descarta em silêncio com HTTP 200" (``dialect``/``spec_kind``,
``dedupe_ttl_seconds``). Estes testes fixam as três pontas de uma vez: schema
de escrita (com validação do enum), persistência via repositório, e a
migração leve que cria as colunas num banco antigo.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.api import schemas
from backend.app.db import models, repository
from backend.app.db.models import Base


# ── schema ────────────────────────────────────────────────────────────


def test_create_accepts_severity_and_shape() -> None:
    body = schemas.PredefinedQueryCreate(
        title="Hunt", statement="SELECT 1", severity_id=3, finding_shape="per_row"
    )
    assert body.severity_id == 3
    assert body.finding_shape == "per_row"


def test_create_defaults_are_none_so_the_runtime_decides() -> None:
    body = schemas.PredefinedQueryCreate(title="Hunt", statement="SELECT 1")
    assert body.severity_id is None
    assert body.finding_shape is None


@pytest.mark.parametrize("bad", [42, -1, 7])
def test_severity_outside_the_ocsf_enum_is_422(bad: int) -> None:
    with pytest.raises(ValidationError):
        schemas.PredefinedQueryCreate(title="Hunt", statement="SELECT 1", severity_id=bad)
    with pytest.raises(ValidationError):
        schemas.PredefinedQueryUpdate(severity_id=bad)


@pytest.mark.parametrize("ok", [0, 1, 2, 3, 4, 5, 6, 99])
def test_every_enum_value_is_accepted(ok: int) -> None:
    assert schemas.PredefinedQueryUpdate(severity_id=ok).severity_id == ok


def test_unknown_shape_is_422() -> None:
    with pytest.raises(ValidationError):
        schemas.PredefinedQueryCreate(title="Hunt", statement="SELECT 1", finding_shape="rows")
    with pytest.raises(ValidationError):
        schemas.PredefinedQueryUpdate(finding_shape="rows")


def test_update_declares_both_fields_so_nothing_is_dropped() -> None:
    # ``StrictUpdateModel`` recusa campo desconhecido; se um dos dois não
    # estivesse declarado, este construtor levantaria.
    body = schemas.PredefinedQueryUpdate(severity_id=5, finding_shape="summary")
    assert body.model_dump(exclude_unset=True) == {"severity_id": 5, "finding_shape": "summary"}


def test_read_exposes_both_fields() -> None:
    read = schemas.PredefinedQueryRead(
        id=1, title="Hunt", statement="SELECT 1", severity_id=2, finding_shape="both"
    )
    assert read.model_dump()["severity_id"] == 2
    assert read.model_dump()["finding_shape"] == "both"


# ── persistência ──────────────────────────────────────────────────────


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_repository_persists_both_fields(db) -> None:
    repo = repository.PredefinedQueryRepository(db)
    q = repo.add(models.PredefinedQuery(title="Hunt", statement="SELECT 1", table="t"))
    assert q.severity_id is None
    assert q.finding_shape == "both"

    updated = repo.update(q, severity_id=5, finding_shape="per_row")
    db.expire_all()
    again = repo.get(updated.id)
    assert again.severity_id == 5
    assert again.finding_shape == "per_row"

    # ``None`` no update significa "não mexa", como nos outros campos.
    repo.update(again, title="Hunt 2")
    db.expire_all()
    assert repo.get(again.id).severity_id == 5


# ── migração leve ─────────────────────────────────────────────────────


def test_lightweight_migration_adds_the_columns_to_an_old_table(monkeypatch) -> None:
    """Um banco criado ANTES destas colunas ganha as duas no boot, sem perder
    linha; o default da forma é ``both`` para a linha antiga."""
    from backend.app.db import database

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE predefined_queries ("
                "id INTEGER PRIMARY KEY, title VARCHAR NOT NULL, description TEXT, "
                "statement TEXT NOT NULL, \"table\" VARCHAR NOT NULL, client_ids TEXT)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO predefined_queries (id, title, statement, \"table\") "
                "VALUES (1, 'Hunt', 'SELECT 1', 't')"
            )
        )
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "DATABASE_URL", "sqlite:///:memory:")

    database._run_lightweight_migrations()

    columns = {c["name"] for c in inspect(engine).get_columns("predefined_queries")}
    assert {"severity_id", "finding_shape"} <= columns
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT severity_id, finding_shape FROM predefined_queries WHERE id = 1")
        ).one()
    assert row[0] is None
    assert row[1] == "both"
