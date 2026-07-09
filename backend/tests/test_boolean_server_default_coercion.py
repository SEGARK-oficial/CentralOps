"""Regressão — server_default booleano deve usar ``_sa_text(...)``, não literal.

A própria base documenta o bug em ``models.py:115-119``: passar
``server_default="false"`` (string-literal Python) a uma coluna ``Boolean``
faz o SQLAlchemy emitir DDL ``DEFAULT 'false'`` (TEXT entre aspas) no
SQLite. Como SQLite tem tipagem dinâmica (BOOLEAN → afinidade NUMERIC), a
string ``'false'`` é gravada verbatim e relida como **string Python
truthy** ``'false'`` — não como ``False`` booleano. ``_sa_text("false")``
emite o token SQL ``DEFAULT false`` (sem aspas), que o SQLite trata como
booleano e o ORM coage corretamente.

Estes testes fixam o comportamento para as 3 colunas corrigidas
(``Organization.auto_managed``, ``Integration.auto_managed``,
``AppUser.is_global``) inserindo via **SQL cru omitindo a coluna** (única
forma de exercitar o ``server_default``) e exigindo leitura booleana.

Cobre também o caminho positivo já correto (``auto_approve_new_tenants``)
como sentinela — garante que o padrão certo continua válido.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import models as _models
from backend.app.db.database import Base


@pytest.fixture
def fresh_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


def _insert_org_raw(engine) -> int:
    """Insere uma Organization via SQL cru OMITINDO ``auto_managed`` →
    força o ``server_default`` a ser aplicado pelo banco."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO organizations (name, slug, is_active, created_at, updated_at) "
                "VALUES ('Org Raw', 'org-raw', 1, datetime('now'), datetime('now'))"
            )
        )
        row = conn.execute(
            text("SELECT id FROM organizations WHERE slug='org-raw'")
        ).fetchone()
    return int(row.id)


def test_organization_auto_managed_default_is_boolean_false(fresh_engine):
    org_id = _insert_org_raw(fresh_engine)
    with sessionmaker(bind=fresh_engine)() as db:
        org = db.get(_models.Organization, org_id)
        assert org.auto_managed is False, (
            f"auto_managed deveria ser False booleano, veio {org.auto_managed!r} "
            f"(tipo {type(org.auto_managed).__name__}) — server_default literal vira "
            f"string truthy no SQLite"
        )
        assert bool(org.auto_managed) is False
        assert not isinstance(org.auto_managed, str)


def test_integration_auto_managed_default_is_boolean_false(fresh_engine):
    # Integration exige organization_id (FK NOT NULL) — cria a org antes.
    org_id = _insert_org_raw(fresh_engine)
    with fresh_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO integrations "
                "(organization_id, name, platform, is_active, kind, auth_status, created_at, updated_at) "
                "VALUES (:oid, 'Int Raw', 'sophos', 1, 'tenant', 'unknown', datetime('now'), datetime('now'))"
            ),
            {"oid": org_id},
        )
        row = conn.execute(
            text("SELECT id FROM integrations WHERE name='Int Raw'")
        ).fetchone()
    with sessionmaker(bind=fresh_engine)() as db:
        integ = db.get(_models.Integration, int(row.id))
        assert integ.auto_managed is False, (
            f"auto_managed deveria ser False booleano, veio {integ.auto_managed!r}"
        )
        assert not isinstance(integ.auto_managed, str)


def test_appuser_is_global_default_is_boolean_false(fresh_engine):
    with fresh_engine.begin() as conn:
        # Omitimos APENAS ``is_global`` (coluna sob teste) — as demais NOT NULL
        # sem server_default precisam de valor explícito.
        conn.execute(
            text(
                "INSERT INTO app_users "
                "(uuid, username, password_hash, role, is_active, created_at, updated_at) "
                "VALUES ('raw-user-uuid', 'raw-user', 'x', 'viewer', 1, datetime('now'), datetime('now'))"
            )
        )
        row = conn.execute(
            text("SELECT id FROM app_users WHERE username='raw-user'")
        ).fetchone()
    with sessionmaker(bind=fresh_engine)() as db:
        user = db.get(_models.AppUser, int(row.id))
        assert user.is_global is False, (
            f"is_global deveria ser False booleano, veio {user.is_global!r}"
        )
        assert not isinstance(user.is_global, str)


def test_no_boolean_column_uses_string_literal_server_default():
    """Guard estrutural: nenhuma coluna ``Boolean`` mapeada pode usar um
    ``server_default`` que produza string-literal (o anti-padrão). Varre o
    metadata e falha se algum default booleano não for um token SQL.

    Pega regressões futuras (alguém reintroduzir ``server_default="false"``)
    sem depender de inspeção textual do arquivo."""
    import sqlalchemy as sa

    offenders = []
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if not isinstance(col.type, sa.Boolean):
                continue
            sd = col.server_default
            if sd is None:
                continue
            arg = getattr(sd, "arg", None)
            # Token SQL correto → arg é um TextClause (tem .text); literal
            # cru → arg é str Python.
            if isinstance(arg, str):
                offenders.append(f"{table.name}.{col.name} → server_default={arg!r}")

    assert not offenders, (
        "Colunas Boolean com server_default string-literal (use _sa_text()): "
        + "; ".join(offenders)
    )
