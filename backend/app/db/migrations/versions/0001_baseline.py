"""baseline — adoção do Alembic

Revision ID: 0001_baseline
Revises:
Create Date: 2026-06-19

ÂNCORA de adoção. O ``upgrade`` é NO-OP de PROPÓSITO: o bootstrap do schema é
feito por ``database.initialize_database`` → ``_run_schema_init`` (create_all +
migrações idempotentes + seeds), que então CARIMBA esta revisão. Esta é a
fronteira: tudo DEPOIS dela são revisions Alembic normais (``op.*``) geradas do
model via ``alembic revision --autogenerate``.

NÃO rode ``alembic upgrade head`` num DB vazio esperando o schema — o bootstrap
suportado é ``python -m app.db.migrate`` (que chama ``initialize_database``).
"""

# revision identifiers, used by Alembic.
revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No-op: schema legado criado por _run_schema_init; esta revisão é só a
    # âncora de versão (stamp). Ver docstring.
    pass


def downgrade() -> None:
    raise NotImplementedError("baseline (0001) não tem downgrade")
