"""Etapa de migração EXPLÍCITA (sai do import do app).

Antes, ``initialize_database()`` rodava no import de ``app.main`` — schema-no-
import impedia ``api`` em ``replicas>1`` (cada import dispararia DDL) e acoplava
o boot. Agora a migração é uma ETAPA própria: os entrypoints
(``start-api.sh``/``start-collector.sh``) executam ``python -m app.db.migrate``
UMA vez no boot, ANTES de subir uvicorn/celery.

Idempotente + serializado por advisory lock no Postgres (ver
``database.initialize_database``) → seguro rodar em todas as réplicas no boot:
a primeira aplica, as demais veem o schema já conciliado e seguem.
"""

from __future__ import annotations

import logging
import sys

from .database import initialize_database

logger = logging.getLogger(__name__)


def main() -> int:
    try:
        initialize_database()
    except Exception:  # pragma: no cover — falha de migração deve abortar o boot
        logger.exception("migrate: falha ao inicializar o schema")
        return 1
    logger.info("migrate: schema garantido + ponteiro Alembic conciliado")
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s"
    )
    sys.exit(main())
