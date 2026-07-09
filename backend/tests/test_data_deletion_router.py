"""Testes do endpoint right-to-delete DELETE /api/organizations/{id}/data (RNF7.3).

Commit 3 — F5-S1.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sqlalchemy import create_engine as _create_engine

from backend.app.db import database as _db_module
from backend.app.db import models
from backend.app.db.database import Base, get_session
from backend.app.main import app


def _delete_json(client: TestClient, url: str, body: dict) -> object:
    """Helper: DELETE com corpo JSON via client.request() (Starlette 1.x compat)."""
    return client.request("DELETE", url, json=body)


# ── Fixture base ──────────────────────────────────────────────────────


@pytest.fixture()
def env():
    """SQLite in-memory + TestClient admin logado."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_session():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_session] = override_session
    client = TestClient(app)

    # Bootstrap admin.
    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPass123!"},
    )
    assert r.status_code == 200, r.text

    yield client, Session

    client.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


# ── Helpers ───────────────────────────────────────────────────────────


def _seed_org(session_factory, *, slug: str | None = None) -> tuple[int, str]:
    """Cria org e retorna (org_id, slug)."""
    _slug = slug or f"test-org-{uuid4().hex[:8]}"
    with session_factory() as db:
        org = models.Organization(
            name=f"Org {_slug}",
            slug=_slug,
            is_active=True,
        )
        db.add(org)
        db.commit()
        db.refresh(org)
        return org.id, org.slug


def _create_user(admin_client, *, username: str, role: str, org_id: int | None = None) -> None:
    payload = {"username": username, "password": "TestPass123!", "role": role}
    if org_id:
        payload["organization_id"] = org_id
    r = admin_client.post("/api/auth/users", json=payload)
    assert r.status_code == 200, r.text


def _login(client, username: str, password: str = "TestPass123!") -> None:
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text


# ── Testes ────────────────────────────────────────────────────────────


def test_data_deletion_requires_org_manage_permission(env) -> None:
    """engineer e operator → 403 no DELETE /organizations/{id}/data."""
    client, Session = env
    org_id, slug = _seed_org(Session)

    for role in ("engineer", "operator", "viewer"):
        username = f"{role}_del_{uuid4().hex[:4]}"
        _create_user(client, username=username, role=role, org_id=org_id)

        target = TestClient(app)
        _login(target, username)

        r = _delete_json(
            target,
            f"/api/organizations/{org_id}/data",
            {"confirmation_text": f"DELETAR {slug}", "reason": "teste"},
        )
        assert r.status_code == 403, (
            f"role={role} deveria receber 403 mas got {r.status_code}: {r.text}"
        )
        target.close()


def test_data_deletion_requires_confirmation_text_match(env) -> None:
    """Texto de confirmação errado → 400."""
    client, Session = env
    org_id, slug = _seed_org(Session)

    # Mock Celery para não tentar conectar ao broker.
    with patch(
        "backend.app.collectors.retention_tasks.execute_data_deletion"
    ) as mock_task:
        mock_task.apply_async.return_value = MagicMock(id="fake-task-id")

        # Texto totalmente errado.
        r = _delete_json(
            client,
            f"/api/organizations/{org_id}/data",
            {"confirmation_text": "APAGAR TUDO", "reason": "teste"},
        )
        assert r.status_code == 400, r.text
        assert "DELETAR" in r.json()["detail"]

        # Texto correto mas slug diferente.
        r2 = _delete_json(
            client,
            f"/api/organizations/{org_id}/data",
            {"confirmation_text": "DELETAR wrong-slug", "reason": "teste"},
        )
        assert r2.status_code == 400, r2.text


def test_data_deletion_creates_job_and_dispatches_task(env) -> None:
    """DELETE bem-sucedido → 202 + DataDeletionJob criado + Celery despachado."""
    client, Session = env
    org_id, slug = _seed_org(Session)

    with patch(
        "backend.app.collectors.retention_tasks.execute_data_deletion"
    ) as mock_task:
        mock_async = MagicMock()
        mock_async.id = "fake-celery-task-id"
        mock_task.apply_async.return_value = mock_async

        r = _delete_json(
            client,
            f"/api/organizations/{org_id}/data",
            {
                "confirmation_text": f"DELETAR {slug}",
                "reason": "Solicitação cliente cancelado contrato",
            },
        )

    assert r.status_code == 202, r.text
    data = r.json()

    assert data["organization_id"] == org_id
    assert data["organization_slug"] == slug
    assert data["status"] == "pending"
    assert data["reason"] == "Solicitação cliente cancelado contrato"
    assert "id" in data

    # Verifica que o job foi salvo no banco.
    with Session() as db:
        job = db.get(models.DataDeletionJob, data["id"])
    assert job is not None
    assert job.organization_id == org_id
    assert job.status == "pending"


def test_data_deletion_audit_log_recorded(env) -> None:
    """DELETE deve criar AuditLog com action='request_data_deletion'."""
    client, Session = env
    org_id, slug = _seed_org(Session)

    with patch(
        "backend.app.collectors.retention_tasks.execute_data_deletion"
    ) as mock_task:
        mock_task.apply_async.return_value = MagicMock(id="fake-task")

        r = _delete_json(
            client,
            f"/api/organizations/{org_id}/data",
            {
                "confirmation_text": f"DELETAR {slug}",
                "reason": "LGPD request",
            },
        )
    assert r.status_code == 202, r.text

    with Session() as db:
        log = (
            db.query(models.AuditLog)
            .filter(models.AuditLog.action == "request_data_deletion")
            .first()
        )
    assert log is not None, "AuditLog de request_data_deletion não encontrado"
    assert str(org_id) in (log.detail or "")
    assert slug in (log.detail or "")


def test_data_deletion_returns_404_for_missing_org(env) -> None:
    """Org inexistente → 404."""
    client, Session = env

    r = _delete_json(
        client,
        "/api/organizations/99999/data",
        {"confirmation_text": "DELETAR ghost-org", "reason": "teste"},
    )
    assert r.status_code == 404, r.text


# ── S3: destination erasure integration ───────────────────────────────────────


@pytest.fixture()
def db_erasure():
    """Isolated in-memory SQLite that patches database.SessionLocal — needed
    for testing _run_destination_erasure which calls database.SessionLocal directly
    (not via FastAPI get_session dependency)."""
    engine = _create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    original = _db_module.SessionLocal
    _db_module.SessionLocal = Session  # type: ignore[assignment]

    yield Session

    _db_module.SessionLocal = original  # type: ignore[assignment]
    Base.metadata.drop_all(bind=engine)


def test_run_destination_erasure_no_erasure_dests(db_erasure) -> None:
    """_run_destination_erasure returns empty outcomes when no erasure-capable destinations exist."""
    from backend.app.collectors.retention_tasks import _run_destination_erasure

    Session = db_erasure
    with Session() as db:
        org = models.Organization(name="Org1", slug="org-1", is_active=True)
        db.add(org)
        db.commit()
        db.refresh(org)
        org_id = org.id

    outcomes, partial = _run_destination_erasure(org_id, "test-job-no-dests")
    assert outcomes == []
    assert partial is False


def test_run_destination_erasure_with_erasure_capable_dest_and_no_dlq(db_erasure) -> None:
    """elastic_bulk com erasure_by_query: mesmo sem DLQ, chama erase com filter de org.

    Dados ENTREGUES com sucesso ao cluster não aparecem na DLQ; o erasure_by_query
    via _delete_by_query garante purge LGPD completo. Portanto, com destino
    elastic_bulk e sem DLQ events, erase() DEVE ser chamado com filter={"organization_id"}.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from backend.app.collectors.output.base import ErasureResult
    from backend.app.collectors.retention_tasks import _run_destination_erasure

    Session = db_erasure
    dest_id = f"dest-elastic-{uuid4().hex[:8]}"
    with Session() as db:
        org = models.Organization(name="Org2", slug="org-2", is_active=True)
        db.add(org)
        db.commit()
        db.refresh(org)
        org_id = org.id

        dest = models.Destination(
            id=dest_id,
            name=f"test-elastic-{uuid4().hex[:8]}",
            kind="elastic_bulk",
            config='{"url": "https://es:9200", "index": "test"}',
            delivery="{}",
            config_version="test",
            organization_id=org_id,
            enabled=True,
        )
        db.add(dest)
        db.commit()

    # elastic_bulk tem erasure_by_query → mesmo sem DLQ, erase() é chamado com filter.
    mock_connector = MagicMock()
    mock_connector.erase = AsyncMock(
        return_value=ErasureResult.success(
            [f"org:{org_id}:deleted:0"],
            detail="delete_by_query: 0 docs apagados",
        )
    )
    mock_connector.close = AsyncMock()

    with patch(
        "backend.app.collectors.output.destinations.registry.has",
        return_value=True,
    ), patch(
        "backend.app.collectors.output.destinations.registry.get",
        return_value=MagicMock(
            capabilities=frozenset({"erasure", "erasure_by_query"})
        ),
    ), patch(
        "backend.app.collectors.output.destinations.registry.build",
        return_value=mock_connector,
    ):
        outcomes, partial = _run_destination_erasure(org_id, "test-job-no-dlq")

    assert partial is False
    assert len(outcomes) == 1
    assert outcomes[0]["destination_id"] == dest_id
    # erase foi chamado com event_ids=[] e filter={"organization_id": org_id}
    mock_connector.erase.assert_called_once_with([], filter={"organization_id": org_id})


def test_run_destination_erasure_calls_erase_on_dlq_events(db_erasure) -> None:
    """With DLQ events, _run_destination_erasure calls erase() on erasure-capable dests."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from backend.app.collectors.output.base import ErasureResult
    from backend.app.collectors.retention_tasks import _run_destination_erasure

    Session = db_erasure
    dest_id = f"dest-elastic-{uuid4().hex[:8]}"

    with Session() as db:
        org = models.Organization(name="Org3", slug="org-3", is_active=True)
        db.add(org)
        db.commit()
        db.refresh(org)
        org_id = org.id

        dest = models.Destination(
            id=dest_id,
            name=f"test-elastic-{uuid4().hex[:8]}",
            kind="elastic_bulk",
            config='{"url": "https://es:9200", "index": "test"}',
            delivery="{}",
            config_version="test",
            organization_id=org_id,
            enabled=True,
        )
        db.add(dest)

        dlq1 = models.DestinationDeadLetter(
            id=str(uuid4()),
            destination_id=dest_id,
            event_id="evt-to-erase-1",
            organization_id=org_id,
            error_kind="schema_rejected",
        )
        dlq2 = models.DestinationDeadLetter(
            id=str(uuid4()),
            destination_id=dest_id,
            event_id="evt-to-erase-2",
            organization_id=org_id,
            error_kind="schema_rejected",
        )
        db.add(dlq1)
        db.add(dlq2)
        db.commit()

    # Mock the connector's erase() to return success without network.
    mock_connector = MagicMock()
    mock_connector.erase = AsyncMock(
        return_value=ErasureResult.success(["evt-to-erase-1", "evt-to-erase-2"])
    )
    mock_connector.close = AsyncMock()

    with patch(
        "backend.app.collectors.output.destinations.registry.has",
        return_value=True,
    ), patch(
        "backend.app.collectors.output.destinations.registry.get",
        return_value=MagicMock(capabilities=frozenset({"erasure"})),
    ), patch(
        "backend.app.collectors.output.destinations.registry.build",
        return_value=mock_connector,
    ):
        outcomes, partial = _run_destination_erasure(org_id, "test-job-with-dlq")

    assert partial is False
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome["destination_id"] == dest_id
    assert set(outcome["erased"]) == {"evt-to-erase-1", "evt-to-erase-2"}
    assert outcome["failed"] == []
    mock_connector.erase.assert_called_once()
    called_ids = set(mock_connector.erase.call_args[0][0])
    assert called_ids == {"evt-to-erase-1", "evt-to-erase-2"}


def test_run_destination_erasure_elastic_calls_erase_with_filter(db_erasure) -> None:
    """elastic_bulk com erasure_by_query: retention chama erase com filter={"organization_id"}
    ALÉM dos event_ids do DLQ, cobrindo dados ENTREGUES no cluster (purge LGPD completo).
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from backend.app.collectors.output.base import ErasureResult
    from backend.app.collectors.retention_tasks import _run_destination_erasure

    Session = db_erasure
    dest_id = f"dest-elastic-{uuid4().hex[:8]}"

    with Session() as db:
        org = models.Organization(name="Org4", slug="org-4", is_active=True)
        db.add(org)
        db.commit()
        db.refresh(org)
        org_id = org.id

        dest = models.Destination(
            id=dest_id,
            name=f"test-elastic-{uuid4().hex[:8]}",
            kind="elastic_bulk",
            config='{"url": "https://es:9200", "index": "test"}',
            delivery="{}",
            config_version="test",
            organization_id=org_id,
            enabled=True,
        )
        db.add(dest)

        dlq = models.DestinationDeadLetter(
            id=str(uuid4()),
            destination_id=dest_id,
            event_id="dlq-evt-1",
            organization_id=org_id,
            error_kind="schema_rejected",
        )
        db.add(dlq)
        db.commit()

    mock_connector = MagicMock()
    mock_connector.erase = AsyncMock(
        return_value=ErasureResult.success(
            ["dlq-evt-1", f"org:{org_id}:deleted:7"],
            detail="1 eventos apagados; delete_by_query: 7 docs apagados",
        )
    )
    mock_connector.close = AsyncMock()

    with patch(
        "backend.app.collectors.output.destinations.registry.has",
        return_value=True,
    ), patch(
        "backend.app.collectors.output.destinations.registry.get",
        return_value=MagicMock(
            capabilities=frozenset({"erasure", "erasure_by_query"})
        ),
    ), patch(
        "backend.app.collectors.output.destinations.registry.build",
        return_value=mock_connector,
    ):
        outcomes, partial = _run_destination_erasure(org_id, "test-job-filter")

    assert partial is False
    assert len(outcomes) == 1
    assert outcomes[0]["destination_id"] == dest_id

    # Verifica que erase foi chamado com event_ids E filter de org (dupla cobertura).
    mock_connector.erase.assert_called_once()
    erase_call = mock_connector.erase.call_args
    called_ids = erase_call[0][0]  # primeiro arg posicional
    called_filter = erase_call.kwargs.get("filter")
    assert "dlq-evt-1" in called_ids, "event_ids do DLQ devem ser passados"
    assert called_filter == {"organization_id": org_id}, (
        f"filter de org deve ser passado para cobrir dados entregues, got: {called_filter}"
    )


def test_run_destination_erasure_non_query_dest_uses_only_ids(db_erasure) -> None:
    """Destino sem erasure_by_query: erase() é chamado APENAS com event_ids (sem filter).

    Conectores como splunk_hec, jsonl, syslog, otlp não suportam delete_by_query —
    apenas recebem os IDs da DLQ. O contrato é preservado: sem filter kwarg.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from backend.app.collectors.output.base import ErasureResult
    from backend.app.collectors.retention_tasks import _run_destination_erasure

    Session = db_erasure
    dest_id = f"dest-custom-{uuid4().hex[:8]}"

    with Session() as db:
        org = models.Organization(name="Org5", slug="org-5", is_active=True)
        db.add(org)
        db.commit()
        db.refresh(org)
        org_id = org.id

        dest = models.Destination(
            id=dest_id,
            name=f"test-custom-{uuid4().hex[:8]}",
            kind="custom_erasure_dest",  # kind hipotético com "erasure" mas sem "erasure_by_query"
            config="{}",
            delivery="{}",
            config_version="test",
            organization_id=org_id,
            enabled=True,
        )
        db.add(dest)

        dlq = models.DestinationDeadLetter(
            id=str(uuid4()),
            destination_id=dest_id,
            event_id="id-only-evt",
            organization_id=org_id,
            error_kind="schema_rejected",
        )
        db.add(dlq)
        db.commit()

    mock_connector = MagicMock()
    mock_connector.erase = AsyncMock(
        return_value=ErasureResult.success(["id-only-evt"])
    )
    mock_connector.close = AsyncMock()

    with patch(
        "backend.app.collectors.output.destinations.registry.has",
        return_value=True,
    ), patch(
        "backend.app.collectors.output.destinations.registry.get",
        return_value=MagicMock(
            # Apenas "erasure", sem "erasure_by_query".
            capabilities=frozenset({"erasure"})
        ),
    ), patch(
        "backend.app.collectors.output.destinations.registry.build",
        return_value=mock_connector,
    ):
        outcomes, partial = _run_destination_erasure(org_id, "test-job-ids-only")

    assert partial is False
    assert len(outcomes) == 1

    # erase chamado SÓ com event_ids, sem filter kwarg.
    mock_connector.erase.assert_called_once()
    erase_call = mock_connector.erase.call_args
    called_filter = erase_call.kwargs.get("filter")
    assert called_filter is None, (
        f"destino sem erasure_by_query não deve receber filter, got: {called_filter}"
    )
    assert "id-only-evt" in erase_call[0][0]


def test_run_destination_erasure_non_query_dest_without_dlq_is_skipped(db_erasure) -> None:
    """Destino sem erasure_by_query e sem DLQ events: pulado (vacuous ok, outcomes=[]).

    Conectores sem erasure_by_query só apagam via _id; sem IDs rastreados,
    não há nada a fazer — sem chamada a erase().
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from backend.app.collectors.retention_tasks import _run_destination_erasure

    Session = db_erasure
    dest_id = f"dest-nq-{uuid4().hex[:8]}"

    with Session() as db:
        org = models.Organization(name="Org6", slug="org-6", is_active=True)
        db.add(org)
        db.commit()
        db.refresh(org)
        org_id = org.id

        dest = models.Destination(
            id=dest_id,
            name=f"test-nq-{uuid4().hex[:8]}",
            kind="nq_erasure_dest",
            config="{}",
            delivery="{}",
            config_version="test",
            organization_id=org_id,
            enabled=True,
        )
        db.add(dest)
        db.commit()
        # Sem DLQ rows.

    mock_connector = MagicMock()
    mock_connector.erase = AsyncMock()
    mock_connector.close = AsyncMock()

    with patch(
        "backend.app.collectors.output.destinations.registry.has",
        return_value=True,
    ), patch(
        "backend.app.collectors.output.destinations.registry.get",
        return_value=MagicMock(capabilities=frozenset({"erasure"})),
    ), patch(
        "backend.app.collectors.output.destinations.registry.build",
        return_value=mock_connector,
    ):
        outcomes, partial = _run_destination_erasure(org_id, "test-job-nq-no-dlq")

    # Pulou: sem DLQ e sem erasure_by_query → outcomes vazio, erase não chamado.
    assert outcomes == []
    assert partial is False
    mock_connector.erase.assert_not_called()
