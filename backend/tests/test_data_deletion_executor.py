"""Testes do executor Celery de right-to-delete."""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, call, mock_open, patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import database, models
from backend.app.db.database import Base


# ── Fixture base ──────────────────────────────────────────────────────


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


# ── Helpers ───────────────────────────────────────────────────────────


def _seed_full_org(db_session):
    """Cria org + integration + dados dependentes para testar purge completo."""
    with db_session() as db:
        org = models.Organization(
            name=f"Test Org {uuid4().hex[:6]}",
            slug=f"test-{uuid4().hex[:8]}",
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

        user = models.AppUser(
            username=f"user-{uuid4().hex[:6]}",
            password_hash="x",
            organization_id=org.id,
            role="viewer",
        )
        db.add(user)
        db.flush()

        # 3 quarantine events.
        for _ in range(3):
            ev = models.QuarantineEvent(
                integration_id=intg.id,
                vendor="sophos",
                event_type="sophos.alert",
                raw_payload=json.dumps({"id": str(uuid4())}),
                error_kind="map",
                created_at=datetime.utcnow() - timedelta(days=1),
                expires_at=datetime.utcnow() + timedelta(days=6),
            )
            db.add(ev)

        # 5 history entries.
        for _ in range(5):
            h = models.History(
                integration_id=intg.id,
                operation="test",
                endpoint="/test",
                timestamp=datetime.utcnow() - timedelta(hours=1),
            )
            db.add(h)

        db.commit()
        org_id = org.id
        intg_id = intg.id

    # Cria job de deleção.
    with db_session() as db:
        job = models.DataDeletionJob(
            organization_id=org_id,
            organization_slug=f"test-{uuid4().hex[:8]}",
            requested_by_username="admin",
            status="pending",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id

    return {"org_id": org_id, "intg_id": intg_id, "job_id": job_id}


# ── Testes ────────────────────────────────────────────────────────────


def test_execute_deletion_purges_all_dependent_tables(db_session) -> None:
    """Após execução, org + integration + quarantine + history devem ser deletados."""
    from backend.app.collectors.retention_tasks import execute_data_deletion

    data = _seed_full_org(db_session)
    org_id = data["org_id"]
    intg_id = data["intg_id"]
    job_id = data["job_id"]

    # Mock do audit de arquivo para não criar diretório em /var/log.
    with patch("builtins.open", mock_open()):
        with patch("pathlib.Path.mkdir"):
            execute_data_deletion.run(job_id)

    # Verifica que tudo foi deletado.
    with db_session() as db:
        assert db.get(models.Organization, org_id) is None, "Org deve ser deletada"
        assert db.get(models.Integration, intg_id) is None, "Integration deve ser deletada"

        q_count = (
            db.query(models.QuarantineEvent)
            .filter(models.QuarantineEvent.integration_id == intg_id)
            .count()
        )
        assert q_count == 0, f"QuarantineEvents devem ser 0, got {q_count}"

        h_count = (
            db.query(models.History)
            .filter(models.History.integration_id == intg_id)
            .count()
        )
        assert h_count == 0, f"History entries devem ser 0, got {h_count}"


def test_execute_deletion_job_status_updated(db_session) -> None:
    """Job deve ser atualizado para 'completed' ou 'partial' após execução."""
    from backend.app.collectors.retention_tasks import execute_data_deletion

    data = _seed_full_org(db_session)
    job_id = data["job_id"]

    with patch("builtins.open", mock_open()):
        with patch("pathlib.Path.mkdir"):
            result = execute_data_deletion.run(job_id)

    assert result["job_id"] == job_id
    assert result["status"] in ("completed", "partial")


def test_execute_deletion_purges_redis_cursors(db_session) -> None:
    """_purge_redis_for_integrations deve ser chamado com os IDs das integrações."""
    from backend.app.collectors.retention_tasks import execute_data_deletion

    data = _seed_full_org(db_session)
    job_id = data["job_id"]

    with patch(
        "backend.app.collectors.retention_tasks._purge_redis_for_integrations"
    ) as mock_redis:
        with patch("builtins.open", mock_open()):
            with patch("pathlib.Path.mkdir"):
                execute_data_deletion.run(job_id)

    # Redis purge deve ter sido chamado (com lista de integration IDs).
    mock_redis.assert_called_once()
    called_ids = mock_redis.call_args[0][0]
    assert isinstance(called_ids, list)


def test_execute_deletion_erases_org_scoped_unknown_fields(db_session) -> None:
    """O right-to-erasure APAGA o drift inferido DA ORG
    (organization_id == org). Drift de OUTRA org (ou legado NULL-org) é
    preservado — escopo exato por tenant."""
    from backend.app.collectors.retention_tasks import execute_data_deletion

    data = _seed_full_org(db_session)
    job_id = data["job_id"]
    org_id = data["org_id"]

    with db_session() as db:
        # Drift DESTA org → deve ser apagado.
        uf_own = models.UnknownField(
            vendor="sophos", event_type="sophos.alert",
            field_path=f"own.{uuid4().hex[:8]}", organization_id=org_id,
            last_seen=datetime.utcnow(), first_seen=datetime.utcnow(), status="new",
        )
        # Drift de OUTRA org → deve ser preservado (isolamento).
        uf_other = models.UnknownField(
            vendor="sophos", event_type="sophos.alert",
            field_path=f"other.{uuid4().hex[:8]}", organization_id=org_id + 9999,
            last_seen=datetime.utcnow(), first_seen=datetime.utcnow(), status="new",
        )
        # Drift legado SEM org (NULL) → preservado (gap conhecido: não
        # atribuível a tenant; nunca é servido por leitura — fail-closed).
        uf_null = models.UnknownField(
            vendor="orphanvendor", event_type="x.y",
            field_path=f"null.{uuid4().hex[:8]}", organization_id=None,
            last_seen=datetime.utcnow(), first_seen=datetime.utcnow(), status="new",
        )
        db.add_all([uf_own, uf_other, uf_null])
        db.commit()
        own_id, other_id, null_id = uf_own.id, uf_other.id, uf_null.id

    with patch("builtins.open", mock_open()):
        with patch("pathlib.Path.mkdir"):
            result = execute_data_deletion.run(job_id)

    with db_session() as db:
        assert db.get(models.UnknownField, own_id) is None, "drift da org deve ser apagado"
        assert db.get(models.UnknownField, other_id) is not None, "drift de outra org preservado"
        assert db.get(models.UnknownField, null_id) is not None, "drift NULL-org preservado"

    assert result["rows_deleted"].get("unknown_fields") == 1


def test_execute_deletion_writes_master_audit_to_file(db_session) -> None:
    """Audit master deve ser gravado como JSON no diretório configurado."""
    from backend.app.collectors.retention_tasks import execute_data_deletion

    data = _seed_full_org(db_session)
    job_id = data["job_id"]

    written_content: list[str] = []

    def capture_write(content: str) -> None:
        written_content.append(content)

    m = mock_open()
    m.return_value.write.side_effect = capture_write

    with patch("pathlib.Path.mkdir"):
        with patch("builtins.open", m):
            execute_data_deletion.run(job_id)

    # Verifica que open() foi chamado.
    assert m.called, "open() deve ser chamado para gravar audit master"

    # Verifica que o conteúdo é JSON válido com os campos esperados.
    # json.dump escreve em múltiplas chamadas; pegamos todos os writes.
    full_written = "".join(written_content)
    if full_written:
        payload = json.loads(full_written)
        assert "job_id" in payload
        assert "organization_id" in payload
        assert "rows_deleted" in payload
        assert "status" in payload


def test_execute_deletion_handles_missing_job(db_session) -> None:
    """Job inexistente → retorna erro sem explodir."""
    from backend.app.collectors.retention_tasks import execute_data_deletion

    result = execute_data_deletion.run("id-que-nao-existe")
    assert "error" in result


def test_execute_deletion_rows_deleted_includes_expected_tables(db_session) -> None:
    """O retorno rows_deleted deve incluir as tabelas do purge."""
    from backend.app.collectors.retention_tasks import execute_data_deletion

    data = _seed_full_org(db_session)
    job_id = data["job_id"]

    with patch("builtins.open", mock_open()):
        with patch("pathlib.Path.mkdir"):
            result = execute_data_deletion.run(job_id)

    rows = result["rows_deleted"]
    for table in ("quarantine_events", "history", "integrations", "organizations"):
        assert table in rows, f"Tabela '{table}' deve estar em rows_deleted"
