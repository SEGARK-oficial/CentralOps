"""Regressão — a etapa standalone ``python -m app.db.migrate`` deve
criar o schema COMPLETO sozinha.

Bug que isto guarda (quebrava o boot em Postgres fresh): a etapa
``python -m app.db.migrate`` NÃO importa ``app.main`` — e ``_run_schema_init``
NÃO importava os models. Logo ``Base.metadata`` ficava VAZIO no ``create_all``
→ ZERO tabela → a migração leve do ``destination_dlq`` referenciava
``organizations`` (inexistente) → ``UndefinedTable`` no Postgres (FK estrita). O
SQLite tolerava (FK lazy), mas o schema saía quase vazio (sem ``organizations``).

Por que os outros testes não pegavam: todos importam ``backend.app.db.models`` no
topo do módulo, populando o metadata no processo de teste. Este teste roda o
entrypoint REAL num SUBPROCESSO limpo (sem esse import), exercitando a condição
de produção.
"""

from __future__ import annotations

import os
import subprocess
import sys
import sqlite3
import tempfile
from pathlib import Path

# Raiz importável do runtime: o entrypoint é ``app.db.migrate`` com ``app`` no
# topo (WORKDIR /app em prod). Aqui ``app`` vive sob ``backend/``.
_BACKEND_DIR = Path(__file__).resolve().parent.parent


def test_standalone_migrate_creates_full_schema():
    db_path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{db_path}",
        "APP_ENV": "test",
        "APP_MASTER_KEY": "test-master-key-for-centralops-suite-12345",
        "SESSION_SECURE_COOKIE": "false",
        # Garante interpretador limpo: nada de sitecustomize importando o app.
        "PYTHONPATH": str(_BACKEND_DIR),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "app.db.migrate"],
        cwd=str(_BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, (
        f"migrate falhou (rc={proc.returncode}).\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )

    con = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        con.close()
        os.unlink(db_path)

    # Sem o import dos models, ``create_all`` criaria 0 tabela e estas faltariam.
    for required in ("organizations", "destinations", "app_users", "destination_dlq"):
        assert required in tables, (
            f"tabela {required!r} ausente — Base.metadata não foi populado no "
            f"caminho de migrate (regressão do boot-em-Postgres). Tabelas: "
            f"{sorted(tables)}"
        )
    # Sanidade: o schema é amplo (não um punhado de tabelas residuais).
    assert len(tables) > 30, f"schema incompleto ({len(tables)} tabelas): {sorted(tables)}"
