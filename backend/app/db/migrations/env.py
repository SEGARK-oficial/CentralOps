"""Alembic environment — adoção do Alembic.

Migrations FUTURAS são geradas com ``alembic revision --autogenerate`` — os
models (``Base.metadata``) são a fonte de verdade. O **baseline (0001)** é uma
ÂNCORA de adoção: o schema legado é criado/curado por
``database._run_schema_init`` (create_all + migrações idempotentes + seeds) e
``initialize_database`` CARIMBA (stamp) a versão — ver ``database.py``.

Por isso 6 índices legados (alguns PARCIAIS / sem declaração no model) são
EXCLUÍDOS do autogenerate aqui: sem isso, o diff proporia DROPá-los (não estão
no metadata). Eles seguem geridos por ``_run_schema_init``.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Import root-agnostic. Preferimos ``backend.app`` PRIMEIRO: no sweep de teste
# compilado o PYTHONPATH inclui /build E /build/backend (ambos os roots
# resolvem) e os testes usam ``backend.app`` — se o env.py importasse ``app.*``
# teríamos DOIS módulos ``database`` distintos (dual-root) e o monkeypatch dos
# testes não pegaria o que o env.py usa. No runtime (container WORKDIR /app, sem
# pacote ``backend``) cai no fallback ``app.*``, consistente com ``app.db.migrate``.
try:  # pragma: no cover - depende do root de execução
    from backend.app.db.database import Base, DATABASE_URL
    import backend.app.db.models  # noqa: F401  (popula o metadata)
except ModuleNotFoundError:  # pragma: no cover
    from app.db.database import Base, DATABASE_URL
    import app.db.models  # noqa: F401

config = context.config
if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except Exception:  # pragma: no cover - logging do alembic.ini é opcional
        pass

target_metadata = Base.metadata

# Índices legados curados por ``_run_schema_init`` (criados pelas migrações
# lightweight, NÃO declarados nos models). Alguns são PARCIAIS
# (``uq_organization_provider_external`` tem WHERE ... IS NOT NULL); outros
# duplicam, com NOME custom, um índice já declarado no model (``ix_tenant_
# selection_parent_id`` etc.). Excluídos do autogenerate p/ o diff não propor
# dropá-los. Lista FECHADA (as migrações lightweight estão congeladas — o
# futuro é Alembic); ``alembic check`` no CI pega qualquer drift.
_LEGACY_INDEXES = frozenset(
    {
        "uq_dest_dlq_dest_event",
        "uq_app_users_provider_subject",
        "uq_organization_provider_external",
        "uq_tenant_selection_parent_external",
        "idx_mapping_audit_def_ts",
        "idx_unknown_fields_lookup",
        "ix_tenant_selection_decided_by",
        "ix_tenant_selection_parent_id",
    }
)


def _include_object(obj, name, type_, reflected, compare_to):  # noqa: ANN001
    if type_ == "index" and name in _LEGACY_INDEXES:
        return False
    return True


def _url() -> str:
    # Precedência: url do Config (setada por database._alembic_config a partir do
    # DATABASE_URL corrente, inclusive sob monkeypatch nos testes) > env > import.
    return (
        config.get_main_option("sqlalchemy.url")
        or os.environ.get("DATABASE_URL")
        or DATABASE_URL
    )


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        include_object=_include_object,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _url()
    connectable = engine_from_config(
        section, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=_include_object,
            compare_type=True,
            # batch p/ SQLite (ALTER limitado) nas revisions futuras.
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
