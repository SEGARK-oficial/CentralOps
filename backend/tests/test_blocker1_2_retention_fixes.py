"""Testes para BLOCKER 1+2 — retention_tasks hotfixes (F5-S5).

BLOCKER 1: sessão separada para gravar status="failed" após rollback.
BLOCKER 2: scan_iter usado em vez de keys() no Redis purge.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import database, models
from backend.app.db.database import Base


# ── Fixture de banco ──────────────────────────────────────────────────


@pytest.fixture()
def db_session():
    """SQLite in-memory com SessionLocal redirecionado."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    original = database.SessionLocal
    database.SessionLocal = Session  # type: ignore[assignment]

    yield Session

    database.SessionLocal = original  # type: ignore[assignment]
    Base.metadata.drop_all(bind=engine)


# ── Helpers ────────────────────────────────────────────────────────────


def _seed_full_org_and_job(db_session) -> tuple[int, str]:
    """Cria org + job de deleção; retorna (org_id, job_id)."""
    with db_session() as db:
        org = models.Organization(
            name=f"Org Delete {uuid4().hex[:6]}",
            slug=f"org-del-{uuid4().hex[:8]}",
            is_active=True,
        )
        db.add(org)
        db.flush()

        intg = models.Integration(
            organization_id=org.id,
            name="Test Integration",
            platform="sophos",
        )
        db.add(intg)
        db.flush()

        job = models.DataDeletionJob(
            organization_id=org.id,
            organization_slug=org.slug,
            requested_by_user_id=None,
            requested_by_username="system",
            status="pending",
        )
        db.add(job)
        db.commit()
        return org.id, job.id


# ── BLOCKER 1: sessão separada para gravar "failed" ───────────────────


def test_execute_data_deletion_marks_failed_via_new_session(db_session) -> None:
    """Exceção mid-run deve persistir status='failed' via NOVA sessão.

    Simula: erro no _write_master_audit (pós-commit DB) → verifica que
    o job já estava com status correto; simula erro no DELETE → rollback
    → nova sessão grava "failed".
    """
    from backend.app.collectors.retention_tasks import execute_data_deletion

    org_id, job_id = _seed_full_org_and_job(db_session)

    # Simula RuntimeError no _purge_redis_for_integrations (que é chamado
    # após o commit DB bem-sucedido). Nesse cenário, o job fica partial/failed
    # dependendo da implementação.
    # Para testar BLOCKER 1 especificamente, precisamos que o erro ocorra
    # ANTES do commit DB (dentro do bloco try), forçando rollback.
    # Patching: force erro no db.commit() (dentro do bloco try) simulando
    # falha de constraint na transação principal.

    commit_call_count = [0]
    original_session_local = database.SessionLocal

    class _BrokenSessionFactory:
        """Sessão que falha no SEGUNDO commit (o commit principal de deletes).

        Primeiro commit: marca job como "running" → OK.
        Segundo commit: deletes principais → RuntimeError → rollback.
        Terceiro contexto (nova sessão): grava "failed" → OK.
        """

        def __call__(self):
            return _BrokenSession(original_session_local)

    class _BrokenSession:
        def __init__(self, factory):
            self._inner = factory()
            self._db = None

        def __enter__(self):
            self._db = self._inner.__enter__()
            # Intercepta commit.
            original_commit = self._db.commit
            call_count_local = [0]

            def _patched_commit():
                call_count_local[0] += 1
                if call_count_local[0] == 2:
                    # Segundo commit: simula falha mid-run.
                    self._db.rollback()
                    raise RuntimeError("Simulated mid-run DB failure")
                return original_commit()

            self._db.commit = _patched_commit
            return self._db

        def __exit__(self, *args):
            return self._inner.__exit__(*args)

        def get(self, *args, **kwargs):
            return self._db.get(*args, **kwargs) if self._db else None

    broken_factory = _BrokenSessionFactory()
    database.SessionLocal = broken_factory  # type: ignore[assignment]

    try:
        with patch(
            "backend.app.collectors.retention_tasks._write_master_audit"
        ):
            with patch(
                "backend.app.collectors.retention_tasks._purge_redis_for_integrations"
            ):
                with pytest.raises(Exception):
                    execute_data_deletion.run(job_id=job_id)
    finally:
        database.SessionLocal = original_session_local  # type: ignore[assignment]

    # Verifica: job.status deve ser "failed" (persistido via nova sessão).
    with original_session_local() as db:
        job = db.get(models.DataDeletionJob, job_id)
        assert job is not None, "Job deve existir no banco"
        assert job.status == "failed", (
            f"Job deveria ter status='failed', encontrado: {job.status!r}. "
            "Isso indica que a nova sessão não persistiu o status corretamente."
        )
        assert job.last_error is not None
        assert "Simulated mid-run DB failure" in job.last_error


# ── BLOCKER 2: scan_iter não usa keys() ──────────────────────────────


def test_purge_redis_uses_scan_iter_not_keys() -> None:
    """_purge_redis_for_integrations deve usar scan_iter, NÃO keys().

    Verifica que r.keys() nunca é chamado (seria O(N) bloqueante).
    scan_iter é incremental (cursor-based) e não bloqueia Redis.
    """
    from backend.app.collectors.retention_tasks import _purge_redis_for_integrations

    mock_redis = MagicMock()
    # scan_iter retorna iterável vazio (sem chaves a deletar).
    mock_redis.scan_iter.return_value = iter([])

    mock_redis_module = MagicMock()
    mock_redis_module.from_url.return_value = mock_redis

    with patch.dict(
        "sys.modules",
        {"redis": mock_redis_module},
    ):
        with patch(
            "backend.app.collectors.retention_tasks.database",
            database,
        ):
            # Força re-import do módulo redis dentro da função.
            import importlib
            import backend.app.collectors.retention_tasks as rt_module

            # Chama com integration_id simulado.
            original_rt = rt_module._purge_redis_for_integrations

            # Re-executa sem re-import (a função usa import local interno).
            # Precisamos injetar o mock dentro do escopo do módulo.
            with patch(
                "backend.app.core.config.settings"
            ) as mock_settings:
                mock_settings.REDIS_URL = "redis://localhost:6379/0"

                with patch("redis.from_url", mock_redis_module.from_url):
                    _purge_redis_for_integrations([1])

    # scan_iter deve ter sido chamado (pelo menos uma vez).
    assert mock_redis.scan_iter.called, (
        "scan_iter deve ser chamado (não keys())"
    )

    # keys() NÃO deve ter sido chamado.
    assert not mock_redis.keys.called, (
        "r.keys() não deve ser chamado — use scan_iter (BLOCKER 2 — F5-S5)"
    )


def test_purge_redis_scan_iter_call_structure() -> None:
    """scan_iter deve ser chamado com match=pattern e count=100."""
    from backend.app.collectors.retention_tasks import _purge_redis_for_integrations

    mock_redis = MagicMock()
    calls_recorded: list[dict] = []

    def _fake_scan_iter(**kwargs):
        calls_recorded.append(kwargs)
        return iter([])

    mock_redis.scan_iter.side_effect = _fake_scan_iter

    with patch("redis.from_url", return_value=mock_redis):
        with patch(
            "backend.app.core.config.settings"
        ) as mock_settings:
            mock_settings.REDIS_URL = "redis://localhost:6379/0"
            _purge_redis_for_integrations([42])

    # Verifica que todas as chamadas usam match= e count=100.
    assert len(calls_recorded) > 0, "scan_iter deve ter sido chamado"
    for call_kwargs in calls_recorded:
        assert "match" in call_kwargs, "scan_iter deve receber match= (pattern)"
        assert call_kwargs.get("count") == 100, (
            f"scan_iter deve usar count=100, encontrado: {call_kwargs.get('count')}"
        )
