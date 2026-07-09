"""Testes de integração do router de backfill (RF2.4).

Cobertura:
- Controle de acesso (RBAC): viewer/operator → 403 no POST.
- Validações de schema: janela máxima 90 dias, from < to.
- Validação de streams existentes para o vendor.
- Isolamento multi-tenant: non-admin não acessa integration de outra org.
- Criação persistida com status="pending".
- Dispatch da task Celery com kwargs corretos.
- Listagem paginada com filtro de status.
- Isolamento multi-tenant na listagem.
- Detalhe 404 para job de outra org.
- Cancelamento: marca cancelled_at e tenta revogar task.
- Cancelamento de job completed → 400.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import models
from backend.app.db.database import Base, get_session
from backend.app.main import app

# Importa a task para que o módulo seja carregado antes do patch (garante
# que o atributo existe no namespace correto para o mock funcionar).
import backend.app.collectors.backfill_tasks as _backfill_tasks_mod  # noqa: E402


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def client_factory():
    """Fábrica de TestClient com banco SQLite in-memory isolado."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(
        autocommit=False, autoflush=False, bind=engine
    )
    Base.metadata.create_all(bind=engine)

    def override_get_session():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_session] = override_get_session
    clients: list[TestClient] = []

    def factory() -> TestClient:
        c = TestClient(app)
        clients.append(c)
        return c

    yield factory, TestingSessionLocal

    for c in clients:
        c.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


# ── Helpers ───────────────────────────────────────────────────────────


def _bootstrap_admin(client: TestClient) -> dict[str, Any]:
    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPassword123!"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _create_user(
    admin_client: TestClient, *, username: str, role: str, org_id: int | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "username": username,
        "password": "TestPassword123!",
        "role": role,
    }
    if org_id is not None:
        payload["organization_id"] = org_id
    r = admin_client.post("/api/auth/users", json=payload)
    assert r.status_code == 200, f"Falha ao criar user {username}: {r.text}"
    return r.json()


def _seed_org_and_integration(
    session, *, name_suffix: str = "", platform: str = "sophos"
) -> tuple[int, int]:
    """Cria organização + integration e retorna (org_id, integration_id)."""
    suffix = name_suffix or uuid4().hex[:6]
    org = models.Organization(
        name=f"Backfill Test Org {suffix}",
        slug=f"backfill-test-{suffix}",
        is_active=True,
    )
    session.add(org)
    session.flush()
    integration = models.Integration(
        organization_id=org.id,
        name=f"Test Integration {suffix}",
        platform=platform,
        is_active=True,
    )
    session.add(integration)
    session.commit()
    session.refresh(integration)
    return org.id, integration.id


def _valid_backfill_payload(streams: list[str] | None = None) -> dict[str, Any]:
    """Payload válido de criação: janela de 7 dias atrás até ontem."""
    now = datetime.utcnow()
    return {
        "streams": streams or ["alerts"],
        "from_ts": (now - timedelta(days=7)).isoformat(),
        "to_ts": (now - timedelta(days=1)).isoformat(),
    }


def _patch_celery_task():
    """Retorna context manager que mocka apply_async da task de backfill.

    A task é carregada via import tardio dentro do endpoint. O patch
    precisa ser aplicado no módulo fonte, não no router.
    """
    mock_result = MagicMock()
    mock_result.id = "mocked-celery-task-id"
    return patch.object(
        _backfill_tasks_mod.collect_backfill_job,
        "apply_async",
        return_value=mock_result,
    )


# ── Testes de controle de acesso (RBAC) ───────────────────────────────


@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_create_backfill_requires_integration_write_permission(
    client_factory, role: str
) -> None:
    """viewer e operator não têm integration.write → 403."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org_id, int_id = _seed_org_and_integration(db)

    uname = f"low_{role}_{uuid4().hex[:6]}"
    _create_user(client, username=uname, role=role, org_id=org_id)
    client.post("/api/auth/logout", json={})

    r = client.post(
        "/api/auth/login",
        json={"username": uname, "password": "TestPassword123!"},
    )
    assert r.status_code == 200

    with _patch_celery_task():
        r = client.post(
            f"/api/integrations/{int_id}/backfill",
            json=_valid_backfill_payload(),
        )

    assert r.status_code == 403, r.text


# ── Testes de validação de schema ─────────────────────────────────────


def test_create_backfill_validates_from_lt_to(client_factory) -> None:
    """from_ts >= to_ts deve retornar 422."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org_id, int_id = _seed_org_and_integration(db)

    now = datetime.utcnow()
    payload = {
        "streams": ["alerts"],
        "from_ts": (now - timedelta(days=1)).isoformat(),
        "to_ts": (now - timedelta(days=7)).isoformat(),  # to < from
    }

    with _patch_celery_task():
        r = client.post(
            f"/api/integrations/{int_id}/backfill",
            json=payload,
        )

    assert r.status_code == 422, r.text


def test_create_backfill_validates_window_max_90_days(client_factory) -> None:
    """Janela maior que 90 dias deve retornar 422."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org_id, int_id = _seed_org_and_integration(db)

    now = datetime.utcnow()
    payload = {
        "streams": ["alerts"],
        "from_ts": (now - timedelta(days=91)).isoformat(),
        "to_ts": now.isoformat(),
    }

    with _patch_celery_task():
        r = client.post(
            f"/api/integrations/{int_id}/backfill",
            json=payload,
        )

    assert r.status_code == 422, r.text


def test_create_backfill_validates_streams_exist_for_integration_platform(
    client_factory,
) -> None:
    """Stream não registrado para o vendor deve retornar 422."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org_id, int_id = _seed_org_and_integration(db, platform="sophos")

    # "nonexistent_stream" não existe no registry de nenhum vendor.
    payload = {
        "streams": ["nonexistent_stream_xyz"],
        "from_ts": (datetime.utcnow() - timedelta(days=7)).isoformat(),
        "to_ts": (datetime.utcnow() - timedelta(days=1)).isoformat(),
    }

    with _patch_celery_task():
        r = client.post(
            f"/api/integrations/{int_id}/backfill",
            json=payload,
        )

    assert r.status_code == 422, r.text
    body = r.json()
    assert "nonexistent_stream_xyz" in str(body)


# ── Testes de multi-tenant ────────────────────────────────────────────


def test_create_backfill_multi_tenant_isolation(client_factory) -> None:
    """Non-admin não pode criar backfill em integration de outra org."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    # Org A: onde o usuário engineer pertence.
    with Session() as db:
        org_a_id, _ = _seed_org_and_integration(db, name_suffix="orgA")

    # Org B: onde a integration alvo existe.
    with Session() as db:
        org_b_id, int_b_id = _seed_org_and_integration(db, name_suffix="orgB")

    # Cria engineer na org A.
    uname = f"eng_{uuid4().hex[:6]}"
    _create_user(client, username=uname, role="engineer", org_id=org_a_id)
    client.post("/api/auth/logout", json={})

    r = client.post(
        "/api/auth/login",
        json={"username": uname, "password": "TestPassword123!"},
    )
    assert r.status_code == 200

    with _patch_celery_task():
        r = client.post(
            f"/api/integrations/{int_b_id}/backfill",
            json=_valid_backfill_payload(),
        )

    # Deve receber 403 (multi-tenant bloqueio).
    assert r.status_code == 403, r.text


# ── Testes de criação ─────────────────────────────────────────────────


def test_create_backfill_persists_job_with_pending_status(
    client_factory,
) -> None:
    """Job criado com sucesso deve ter status='pending'."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org_id, int_id = _seed_org_and_integration(db)

    # Determina streams válidos para sophos.
    from backend.app.collectors import registry as collector_registry
    streams_available = collector_registry.supported_streams("sophos")

    if not streams_available:
        pytest.skip("Nenhum stream registrado para sophos — skipping")

    first_stream = streams_available[0]
    payload = _valid_backfill_payload(streams=[first_stream])

    mock_result = MagicMock()
    mock_result.id = "celery-task-abc"

    with patch.object(
        _backfill_tasks_mod.collect_backfill_job,
        "apply_async",
        return_value=mock_result,
    ):
        r = client.post(
            f"/api/integrations/{int_id}/backfill",
            json=payload,
        )

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "pending"
    assert body["integration_id"] == int_id
    assert first_stream in body["streams"]
    assert body["events_collected"] == 0
    assert body["progress_pct"] == 0

    # Verifica persistência no banco.
    with Session() as db:
        job = db.get(models.BackfillJob, body["id"])
        assert job is not None
        assert job.status == "pending"
        assert job.celery_task_id == "celery-task-abc"


def test_create_backfill_dispatches_celery_task(client_factory) -> None:
    """Task Celery deve ser disparada com job_id correto na fila collect.backfill."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org_id, int_id = _seed_org_and_integration(db)

    from backend.app.collectors import registry as collector_registry
    streams_available = collector_registry.supported_streams("sophos")
    if not streams_available:
        pytest.skip("Nenhum stream registrado para sophos — skipping")

    first_stream = streams_available[0]

    mock_result = MagicMock()
    mock_result.id = "celery-task-xyz-123"

    with patch.object(
        _backfill_tasks_mod.collect_backfill_job,
        "apply_async",
        return_value=mock_result,
    ) as mock_apply:
        r = client.post(
            f"/api/integrations/{int_id}/backfill",
            json=_valid_backfill_payload(streams=[first_stream]),
        )

    assert r.status_code == 201, r.text
    body = r.json()

    mock_apply.assert_called_once_with(
        kwargs={"job_id": body["id"]},
        queue="collect.backfill",
    )
    assert body["celery_task_id"] == "celery-task-xyz-123"


# ── Testes de listagem ────────────────────────────────────────────────


def test_list_backfill_jobs_paginated_filtered(client_factory) -> None:
    """Listagem retorna total, paginação e filtro por status."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org_id, int_id = _seed_org_and_integration(db)

    # Insere jobs com status variados diretamente no banco.
    with Session() as db:
        for st in ["pending", "running", "completed", "completed"]:
            db.add(
                models.BackfillJob(
                    integration_id=int_id,
                    streams='["alerts"]',
                    from_ts=datetime.utcnow() - timedelta(days=7),
                    to_ts=datetime.utcnow() - timedelta(days=1),
                    status=st,
                )
            )
        db.commit()

    r = client.get(f"/api/integrations/{int_id}/backfill-jobs")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 4
    assert len(body["items"]) == 4

    # Filtro por status=completed.
    r = client.get(
        f"/api/integrations/{int_id}/backfill-jobs",
        params={"status": "completed"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert all(j["status"] == "completed" for j in body["items"])

    # Paginação.
    r = client.get(
        f"/api/integrations/{int_id}/backfill-jobs",
        params={"limit": 2, "offset": 0},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 2
    assert body["total"] == 4


def test_list_backfill_jobs_multi_tenant_isolation(client_factory) -> None:
    """Non-admin não lista jobs de integration de outra org."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    # Org A (do usuário) e Org B (integration alvo).
    with Session() as db:
        org_a_id, _ = _seed_org_and_integration(db, name_suffix="lA")
    with Session() as db:
        org_b_id, int_b_id = _seed_org_and_integration(db, name_suffix="lB")

    uname = f"view_{uuid4().hex[:6]}"
    _create_user(client, username=uname, role="engineer", org_id=org_a_id)
    client.post("/api/auth/logout", json={})

    r = client.post(
        "/api/auth/login",
        json={"username": uname, "password": "TestPassword123!"},
    )
    assert r.status_code == 200

    r = client.get(f"/api/integrations/{int_b_id}/backfill-jobs")
    assert r.status_code == 403, r.text


# ── Testes de detalhe ─────────────────────────────────────────────────


def test_get_backfill_job_404_for_other_org(client_factory) -> None:
    """job_id de outra org deve retornar 403 para non-admin."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org_a_id, int_a_id = _seed_org_and_integration(db, name_suffix="gA")
    with Session() as db:
        org_b_id, int_b_id = _seed_org_and_integration(db, name_suffix="gB")

    # Cria job na org B.
    with Session() as db:
        job = models.BackfillJob(
            integration_id=int_b_id,
            streams='["alerts"]',
            from_ts=datetime.utcnow() - timedelta(days=7),
            to_ts=datetime.utcnow() - timedelta(days=1),
            status="pending",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id

    # Usuário da org A tenta acessar job da org B.
    uname = f"eng_{uuid4().hex[:6]}"
    _create_user(client, username=uname, role="engineer", org_id=org_a_id)
    client.post("/api/auth/logout", json={})

    r = client.post(
        "/api/auth/login",
        json={"username": uname, "password": "TestPassword123!"},
    )
    assert r.status_code == 200

    r = client.get(f"/api/backfill-jobs/{job_id}")
    assert r.status_code == 403, r.text


# ── Testes de cancelamento ────────────────────────────────────────────


def test_cancel_backfill_marks_cancelled_at_and_revokes_task(
    client_factory,
) -> None:
    """Cancelamento deve setar status=cancelled, cancelled_at e chamar revoke."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org_id, int_id = _seed_org_and_integration(db)

    with Session() as db:
        job = models.BackfillJob(
            integration_id=int_id,
            streams='["alerts"]',
            from_ts=datetime.utcnow() - timedelta(days=7),
            to_ts=datetime.utcnow() - timedelta(days=1),
            status="pending",
            celery_task_id="celery-abc-123",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id

    mock_async_result = MagicMock()

    with patch(
        "backend.app.routers.backfill.AsyncResult",
        return_value=mock_async_result,
    ):
        r = client.post(f"/api/backfill-jobs/{job_id}/cancel")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "cancelled"
    assert body["cancelled_at"] is not None

    mock_async_result.revoke.assert_called_once_with(terminate=False)

    # Verifica persistência.
    with Session() as db:
        fresh = db.get(models.BackfillJob, job_id)
        assert fresh is not None
        assert fresh.status == "cancelled"
        assert fresh.cancelled_at is not None


def test_cancel_backfill_completed_job_returns_400(client_factory) -> None:
    """Não deve ser possível cancelar job já completed."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org_id, int_id = _seed_org_and_integration(db)

    with Session() as db:
        job = models.BackfillJob(
            integration_id=int_id,
            streams='["alerts"]',
            from_ts=datetime.utcnow() - timedelta(days=7),
            to_ts=datetime.utcnow() - timedelta(days=1),
            status="completed",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id

    r = client.post(f"/api/backfill-jobs/{job_id}/cancel")
    assert r.status_code == 400, r.text
    assert "completed" in r.json()["detail"]


# ── Observabilidade: stall + diagnóstico (RF2.4) ──────────────────────


def _seed_job(Session, int_id: int, *, status: str, requested_age_s: int = 0,
              started_age_s: int | None = None) -> str:
    """Cria um BackfillJob diretamente no DB e retorna seu id."""
    now = datetime.utcnow()
    with Session() as db:
        job = models.BackfillJob(
            integration_id=int_id,
            streams='["alerts"]',
            from_ts=now - timedelta(days=7),
            to_ts=now - timedelta(days=1),
            status=status,
            requested_at=now - timedelta(seconds=requested_age_s),
            started_at=(now - timedelta(seconds=started_age_s)
                        if started_age_s is not None else None),
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job.id


def test_pending_job_stalled_after_threshold(client_factory) -> None:
    """Job 'pending' há mais que o limiar → stalled=True + dica acionável."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)
    with Session() as db:
        _org_id, int_id = _seed_org_and_integration(db)

    job_id = _seed_job(Session, int_id, status="pending", requested_age_s=300)
    r = client.get(f"/api/backfill-jobs/{job_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stalled"] is True
    assert "collect.backfill" in body["stall_reason"]


def test_fresh_pending_job_not_stalled(client_factory) -> None:
    """Job 'pending' recém-criado NÃO é stalled (dentro do limiar)."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)
    with Session() as db:
        _org_id, int_id = _seed_org_and_integration(db)

    job_id = _seed_job(Session, int_id, status="pending", requested_age_s=5)
    r = client.get(f"/api/backfill-jobs/{job_id}")
    assert r.status_code == 200, r.text
    assert r.json()["stalled"] is False
    assert r.json()["stall_reason"] is None


def test_diagnostics_no_consumer_is_unhealthy(client_factory) -> None:
    """Workers online mas nenhum consumindo collect.backfill → unhealthy + dica."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)
    with Session() as db:
        _org_id, int_id = _seed_org_and_integration(db)
    _seed_job(Session, int_id, status="pending", requested_age_s=300)

    with patch(
        "backend.app.routers.backfill._inspect_backfill_workers",
        return_value=(["worker@host"], []),  # online, mas sem consumidor da fila
    ):
        r = client.get("/api/backfill-jobs/diagnostics")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["broker_reachable"] is True
    assert body["healthy"] is False
    assert body["backfill_queue_consumers"] == []
    assert body["pending_jobs"] == 1
    assert "NENHUM consome" in body["diagnosis"]


def test_diagnostics_healthy_with_consumer(client_factory) -> None:
    """Consumidor da fila presente e sem pending velho → healthy."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)
    with Session() as db:
        _org_id, _int_id = _seed_org_and_integration(db)

    with patch(
        "backend.app.routers.backfill._inspect_backfill_workers",
        return_value=(["worker@host"], ["worker@host"]),
    ):
        r = client.get("/api/backfill-jobs/diagnostics")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["healthy"] is True
    assert body["backfill_queue_consumers"] == ["worker@host"]


def test_diagnostics_broker_unreachable(client_factory) -> None:
    """Inspeção falha (broker down) → broker_reachable=False, nunca 500."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with patch(
        "backend.app.routers.backfill._inspect_backfill_workers",
        side_effect=OSError("broker down"),
    ):
        r = client.get("/api/backfill-jobs/diagnostics")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["broker_reachable"] is False
    assert body["healthy"] is False


def test_diagnostics_high_backlog_is_unhealthy(client_factory, monkeypatch) -> None:
    """Consumidor presente + jobs frescos, mas backlog acima do watermark → unhealthy."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)
    with Session() as db:
        _org_id, int_id = _seed_org_and_integration(db)

    # Watermark baixo p/ não precisar semear centenas de jobs.
    monkeypatch.setattr(
        "backend.app.routers.backfill._PENDING_BACKLOG_WATERMARK", 2
    )
    for _ in range(3):
        _seed_job(Session, int_id, status="pending", requested_age_s=5)  # frescos

    with patch(
        "backend.app.routers.backfill._inspect_backfill_workers",
        return_value=(["worker@host"], ["worker@host"]),
    ):
        r = client.get("/api/backfill-jobs/diagnostics")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pending_jobs"] == 3
    assert body["healthy"] is False
    assert "backlog" in body["diagnosis"].lower()


def test_diagnostics_requires_admin(client_factory) -> None:
    """Endpoint de diagnóstico é admin global — viewer recebe 403."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)
    with Session() as db:
        org_id, _int_id = _seed_org_and_integration(db)

    uname = f"viewer_{uuid4().hex[:6]}"
    _create_user(client, username=uname, role="viewer", org_id=org_id)
    client.post("/api/auth/logout", json={})
    r = client.post(
        "/api/auth/login",
        json={"username": uname, "password": "TestPassword123!"},
    )
    assert r.status_code == 200

    r = client.get("/api/backfill-jobs/diagnostics")
    assert r.status_code == 403, r.text
