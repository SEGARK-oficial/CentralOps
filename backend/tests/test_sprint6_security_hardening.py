"""Testes de hardening de segurança.

Cobre os blockers e majors identificados:
- BLOCKER 1: isolamento multi-tenant em /api/quarantine
- BLOCKER 2: isolamento multi-tenant em /api/drift
- MAJOR 1: dry-run exige mapping.read
- MAJOR 2: list/get definitions exige mapping.read
- MAJOR 4: create_user grava audit log
- MAJOR 5: delete_user grava audit log
- MAJOR 7: discard quarantine é atômico com audit
- MEDIUM: UnicodeDecodeError em main.py não crasha o middleware
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import models
from backend.app.db.database import Base, get_session
from backend.app.main import _serialize_request_payload, app


# ── Fixture compartilhada ─────────────────────────────────────────────


@pytest.fixture()
def client_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
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


# ── Helpers de setup ──────────────────────────────────────────────────


def _bootstrap_admin(client: TestClient) -> dict[str, Any]:
    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPassword123!", "display_name": "Admin"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _create_org(session) -> int:
    """Cria uma organização de teste, retorna org.id."""
    org = models.Organization(
        name=f"Test Org {uuid4().hex[:6]}",
        slug=f"test-org-{uuid4().hex[:6]}",
        is_active=True,
    )
    session.add(org)
    session.commit()
    session.refresh(org)
    return org.id


def _create_integration(session, *, org_id: int, platform: str = "sophos") -> int:
    """Cria uma integração para a org, retorna integration.id."""
    integ = models.Integration(
        organization_id=org_id,
        name=f"Integration {uuid4().hex[:6]}",
        platform=platform,
    )
    session.add(integ)
    session.commit()
    session.refresh(integ)
    return integ.id


def _create_non_admin_user(
    admin_client: TestClient,
    *,
    username: str,
    role: str = "operator",
    org_id: int | None = None,
) -> None:
    """Cria usuário non-admin via API (requer session de admin ativa)."""
    r = admin_client.post(
        "/api/auth/users",
        json={
            "username": username,
            "password": "UserPassword123!",
            "display_name": username.title(),
            "role": role,
            "organization_id": org_id,
        },
    )
    assert r.status_code == 200, f"Falha ao criar user {username}: {r.text}"


def _login(client: TestClient, username: str, password: str = "UserPassword123!") -> None:
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, f"Falha ao logar como {username}: {r.text}"


def _seed_quarantine(
    session,
    *,
    vendor: str = "sophos",
    event_type: str = "sophos.alert",
    integration_id: int | None = None,
) -> str:
    """Insere QuarantineEvent e retorna seu id."""
    ev = models.QuarantineEvent(
        vendor=vendor,
        event_type=event_type,
        raw_payload=json.dumps({"id": uuid4().hex}),
        error_kind="map",
        integration_id=integration_id,
        expires_at=datetime.utcnow() + timedelta(days=7),
    )
    session.add(ev)
    session.commit()
    session.refresh(ev)
    return ev.id


def _seed_unknown_field(
    session, *, vendor: str = "sophos", organization_id: int | None = None
) -> str:
    """Insere UnknownField e retorna seu id."""
    uf = models.UnknownField(
        vendor=vendor,
        event_type="sophos.alert",
        field_path=f"extra.field.{uuid4().hex[:8]}",
        organization_id=organization_id,
        occurrence_count=1,
        first_seen=datetime.utcnow(),
        last_seen=datetime.utcnow(),
        status="new",
    )
    session.add(uf)
    session.commit()
    session.refresh(uf)
    return uf.id


# ── BLOCKER 1: multi-tenant em /api/quarantine ────────────────────────


def test_quarantine_list_filters_by_org_for_non_admin(client_factory) -> None:
    """Non-admin só enxerga eventos de quarentena da própria org."""
    factory, Session = client_factory

    admin_client = factory()
    _bootstrap_admin(admin_client)

    # Configura duas orgs com integrations diferentes
    with Session() as db:
        org_a = _create_org(db)
        org_b = _create_org(db)
        integ_a = _create_integration(db, org_id=org_a)
        integ_b = _create_integration(db, org_id=org_b)
        # Evento da org A
        _seed_quarantine(db, integration_id=integ_a)
        # Evento da org B
        _seed_quarantine(db, integration_id=integ_b)
        # Evento sem integration (admin-only)
        _seed_quarantine(db, integration_id=None)

    _create_non_admin_user(admin_client, username="alice_q", role="operator", org_id=org_a)
    alice_client = factory()
    _login(alice_client, "alice_q")

    r = alice_client.get("/api/quarantine")
    assert r.status_code == 200, r.text
    data = r.json()
    # Alice vê apenas os eventos da org A (1 evento)
    assert data["total"] == 1
    assert data["items"][0]["integration_id"] == integ_a

    # Admin vê todos (3 eventos)
    r_admin = admin_client.get("/api/quarantine")
    assert r_admin.status_code == 200, r_admin.text
    assert r_admin.json()["total"] == 3


def test_quarantine_get_returns_404_for_non_admin_other_org(client_factory) -> None:
    """Non-admin que tenta acessar evento de outra org recebe 404 (não 403)."""
    factory, Session = client_factory

    admin_client = factory()
    _bootstrap_admin(admin_client)

    with Session() as db:
        org_a = _create_org(db)
        org_b = _create_org(db)
        integ_b = _create_integration(db, org_id=org_b)
        # Evento pertence à org B
        event_id = _seed_quarantine(db, integration_id=integ_b)

    # Usuário da org A tenta acessar evento da org B
    _create_non_admin_user(admin_client, username="bob_q", role="viewer", org_id=org_a)
    bob_client = factory()
    _login(bob_client, "bob_q")

    r = bob_client.get(f"/api/quarantine/{event_id}")
    # 404 em vez de 403 para não vazar existência do recurso (user enum)
    assert r.status_code == 404, r.text


def test_quarantine_discard_validates_org_access(client_factory) -> None:
    """Non-admin não pode descartar eventos de outra org."""
    factory, Session = client_factory

    admin_client = factory()
    _bootstrap_admin(admin_client)

    with Session() as db:
        org_a = _create_org(db)
        org_b = _create_org(db)
        integ_a = _create_integration(db, org_id=org_a)
        integ_b = _create_integration(db, org_id=org_b)
        # Evento da org B
        event_other_org = _seed_quarantine(db, integration_id=integ_b)
        # Evento da org A (deve poder descartar)
        event_own_org = _seed_quarantine(db, integration_id=integ_a)

    _create_non_admin_user(admin_client, username="carol_q", role="operator", org_id=org_a)
    carol_client = factory()
    _login(carol_client, "carol_q")

    # Tenta descartar evento de outra org → 404
    r = carol_client.post(f"/api/quarantine/{event_other_org}/discard")
    assert r.status_code == 404, r.text

    # Evento da própria org → 204
    r = carol_client.post(f"/api/quarantine/{event_own_org}/discard")
    assert r.status_code == 204, r.text


# ── BLOCKER 2: multi-tenant em /api/drift ────────────────────────────


def test_drift_list_filters_by_vendor_for_non_admin(client_factory) -> None:
    """Non-admin só enxerga drift da PRÓPRIA org (isolamento
    EXATO por organization_id — antes era aproximação por vendor, que vazava
    campos entre clientes do mesmo vendor)."""
    factory, Session = client_factory

    admin_client = factory()
    _bootstrap_admin(admin_client)

    with Session() as db:
        org_a = _create_org(db)
        org_b = _create_org(db)
        _create_integration(db, org_id=org_a, platform="sophos")
        # MESMO vendor 'sophos', orgs diferentes — o vetor de vazamento.
        _seed_unknown_field(db, vendor="sophos", organization_id=org_a)
        _seed_unknown_field(db, vendor="sophos", organization_id=org_b)

    _create_non_admin_user(admin_client, username="dave_d", role="viewer", org_id=org_a)
    dave_client = factory()
    _login(dave_client, "dave_d")

    r = dave_client.get("/api/drift")
    assert r.status_code == 200, r.text
    data = r.json()
    # Dave (org A) vê APENAS o campo da própria org — não o da org B (mesmo vendor).
    assert data["total"] == 1

    # Admin (global scope) vê ambos.
    r_admin = admin_client.get("/api/drift")
    assert r_admin.status_code == 200
    assert r_admin.json()["total"] == 2


def test_drift_list_returns_empty_for_user_without_org_integrations(client_factory) -> None:
    """Non-admin sem integrations na org recebe lista vazia (fail-closed)."""
    factory, Session = client_factory

    admin_client = factory()
    _bootstrap_admin(admin_client)

    with Session() as db:
        org_a = _create_org(db)
        # Org sem integrations
        _seed_unknown_field(db, vendor="sophos")

    _create_non_admin_user(admin_client, username="eve_d", role="viewer", org_id=org_a)
    eve_client = factory()
    _login(eve_client, "eve_d")

    r = eve_client.get("/api/drift")
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 0


def test_drift_ignore_blocked_for_non_admin_wrong_vendor(client_factory) -> None:
    """Non-admin não pode ignorar campo de vendor fora da sua org."""
    factory, Session = client_factory

    admin_client = factory()
    _bootstrap_admin(admin_client)

    with Session() as db:
        org_a = _create_org(db)
        _create_integration(db, org_id=org_a, platform="sophos")
        # Campo de wazuh — fora do escopo da org A
        field_id = _seed_unknown_field(db, vendor="wazuh")

    _create_non_admin_user(admin_client, username="frank_d", role="operator", org_id=org_a)
    frank_client = factory()
    _login(frank_client, "frank_d")

    r = frank_client.post(f"/api/drift/{field_id}/ignore")
    assert r.status_code == 404, r.text


# ── MAJOR 1: dry-run exige mapping.read ──────────────────────────────


@pytest.mark.parametrize("role,expected", [
    ("viewer", 200),
    ("operator", 200),
    ("engineer", 200),
    ("admin", 200),
])
def test_dry_run_requires_mapping_read_permission(
    client_factory, role: str, expected: int
) -> None:
    """Todos os papéis com mapping.read podem acessar /api/mappings/dry-run."""
    factory, _ = client_factory

    admin_client = factory()
    _bootstrap_admin(admin_client)

    if role != "admin":
        _create_non_admin_user(admin_client, username=f"dry_{role}", role=role)

    target = factory()
    if role == "admin":
        target = admin_client
    else:
        _login(target, f"dry_{role}")

    r = target.post(
        "/api/mappings/dry-run",
        json={
            "rules": {"preprocess": [], "rules": [{"target": "class_uid", "const": 2004}]},
            "raw_events": [{"id": "x"}],
        },
    )
    assert r.status_code == expected, f"role={role}: {r.text}"


def test_dry_run_unauthenticated_returns_401(client_factory) -> None:
    """Sem autenticação, dry-run retorna 401."""
    factory, _ = client_factory
    # client sem bootstrap
    client = TestClient(app)
    r = client.post(
        "/api/mappings/dry-run",
        json={
            "rules": {"preprocess": [], "rules": [{"target": "x", "const": 1}]},
            "raw_events": [],
        },
    )
    assert r.status_code == 401


# ── MAJOR 2: list/get definitions exige mapping.read ─────────────────


def test_list_mappings_requires_mapping_read_permission(client_factory) -> None:
    """Viewer tem mapping.read → 200. Unauthenticated → 401."""
    factory, _ = client_factory

    admin_client = factory()
    _bootstrap_admin(admin_client)

    # Viewer pode listar
    _create_non_admin_user(admin_client, username="viewer_m", role="viewer")
    viewer_client = factory()
    _login(viewer_client, "viewer_m")

    r = viewer_client.get("/api/mappings")
    assert r.status_code == 200, r.text

    # Sem sessão → 401
    anon = TestClient(app)
    r_anon = anon.get("/api/mappings")
    assert r_anon.status_code == 401


def test_get_definition_requires_mapping_read_permission(client_factory) -> None:
    """Non-admin com mapping.read pode acessar definição por id."""
    factory, Session = client_factory

    admin_client = factory()
    _bootstrap_admin(admin_client)

    with Session() as db:
        defn = models.MappingDefinition(
            vendor="sophos",
            event_type="sophos.alert",
            ocsf_class_uid=2004,
        )
        db.add(defn)
        db.commit()
        db.refresh(defn)
        def_id = defn.id

    _create_non_admin_user(admin_client, username="viewer_g", role="viewer")
    viewer_client = factory()
    _login(viewer_client, "viewer_g")

    r = viewer_client.get(f"/api/mappings/{def_id}")
    assert r.status_code == 200, r.text


# ── MAJOR 4: create_user grava audit log ─────────────────────────────


def test_create_user_writes_audit_log(client_factory) -> None:
    """POST /api/auth/users deve gravar entrada em AuditLog com action=user_created."""
    factory, Session = client_factory

    admin_client = factory()
    _bootstrap_admin(admin_client)

    r = admin_client.post(
        "/api/auth/users",
        json={
            "username": "new_user_audit",
            "password": "AuditPassword123!",
            "display_name": "Audit User",
            "role": "viewer",
        },
    )
    assert r.status_code == 200, r.text

    with Session() as db:
        log = (
            db.query(models.AuditLog)
            .filter(models.AuditLog.action == "user_created")
            .order_by(models.AuditLog.created_at.desc())
            .first()
        )

    assert log is not None, "Audit log de user_created não foi gravado"
    assert log.username == "admin", "Actor deve ser o admin que criou"
    detail = json.loads(log.detail)
    assert detail["target_username"] == "new_user_audit"
    assert detail["role"] == "viewer"
    assert "target_user_id" in detail


# ── MAJOR 5: delete_user grava audit log ─────────────────────────────


def test_delete_user_writes_audit_log(client_factory) -> None:
    """DELETE /api/auth/users/{id} deve gravar audit log ANTES do delete."""
    factory, Session = client_factory

    admin_client = factory()
    _bootstrap_admin(admin_client)

    # Cria um segundo admin para poder deletar o primeiro sem violar "último admin"
    r_create = admin_client.post(
        "/api/auth/users",
        json={
            "username": "user_to_delete",
            "password": "DeleteMe123!",
            "display_name": "Delete Me",
            "role": "viewer",
        },
    )
    assert r_create.status_code == 200, r_create.text
    user_data = r_create.json()
    user_public_id = user_data["id"]

    r_delete = admin_client.delete(f"/api/auth/users/{user_public_id}")
    assert r_delete.status_code == 204, r_delete.text

    with Session() as db:
        log = (
            db.query(models.AuditLog)
            .filter(models.AuditLog.action == "user_deleted")
            .order_by(models.AuditLog.created_at.desc())
            .first()
        )

    assert log is not None, "Audit log de user_deleted não foi gravado"
    assert log.username == "admin"
    detail = json.loads(log.detail)
    assert detail["target_username"] == "user_to_delete"
    assert detail["role_at_deletion"] == "viewer"
    assert "target_user_id" in detail


# ── MEDIUM: UnicodeDecodeError em main.py:104 ────────────────────────


def test_unicode_decode_error_does_not_crash_audit_middleware() -> None:
    """Payload com bytes inválidos UTF-8 não deve crashar o middleware de auditoria.

    Verifica que main._serialize_request_payload trata UnicodeDecodeError
    corretamente (o bug original era o typo ÚnicodeDecodeError → NameError
    em runtime quando a exceção ocorria).
    """
    from unittest.mock import MagicMock

    invalid_utf8_body = b"\xff\xfe invalid utf8"

    # Constrói mock compatível com a interface de Request que
    # _serialize_request_payload usa: .query_params.multi_items() e .headers.get()
    mock_qp = MagicMock()
    mock_qp.multi_items.return_value = []
    mock_qp.__bool__ = lambda s: False  # falsy para o `if request.query_params:` check

    mock_request = MagicMock()
    mock_request.query_params = mock_qp
    mock_request.headers.get.return_value = "application/json"

    # A função deve retornar None ou string sem levantar NameError/UnicodeDecodeError
    result = _serialize_request_payload(mock_request, invalid_utf8_body)
    # Sem exceção = bug corrigido. Resultado pode ser None ou string com omissão.
    assert result is None or isinstance(result, str)


def test_audit_middleware_handles_binary_body_gracefully(client_factory) -> None:
    """Requisição com body binário inválido não deve resultar em 500."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    # Envia body com bytes inválidos UTF-8 para endpoint autenticado
    r = client.post(
        "/api/mappings/dry-run",
        content=b"\xff\xfe not utf8",
        headers={"content-type": "application/json"},
    )
    # Deve retornar erro de validação (400/422), não 500 (crash do middleware)
    assert r.status_code in (400, 422), f"Esperava 400/422, got {r.status_code}: {r.text}"


# ── MAJOR 7: discard quarantine atômico com audit ────────────────────


def test_quarantine_discard_atomic_with_audit(client_factory) -> None:
    """Discard deve gravar audit e deletar evento na mesma transação.

    Verifica que: após discard bem-sucedido, o evento foi removido E
    um MappingAuditLog foi gravado com os metadados corretos.
    """
    factory, Session = client_factory

    admin_client = factory()
    _bootstrap_admin(admin_client)

    with Session() as db:
        ev = models.QuarantineEvent(
            vendor="sophos",
            event_type="sophos.detect",
            raw_payload=json.dumps({"event_id": "atomic-test"}),
            error_kind="map",
            expires_at=datetime.utcnow() + timedelta(days=7),
        )
        db.add(ev)
        db.commit()
        db.refresh(ev)
        event_id = ev.id

    r = admin_client.post(f"/api/quarantine/{event_id}/discard")
    assert r.status_code == 204, r.text

    with Session() as db:
        # Evento deve ter sido deletado
        assert db.get(models.QuarantineEvent, event_id) is None

        # Audit log deve ter sido gravado na mesma transação
        log = (
            db.query(models.MappingAuditLog)
            .filter(
                models.MappingAuditLog.action == "discard_quarantine",
            )
            .order_by(models.MappingAuditLog.created_at.desc())
            .first()
        )
        assert log is not None, "MappingAuditLog de discard_quarantine não foi gravado"
        assert log.username == "admin"
        detail = json.loads(log.detail)
        assert detail["quarantine_event_id"] == event_id
        assert detail["vendor"] == "sophos"


def test_quarantine_discard_404_does_not_create_audit(client_factory) -> None:
    """Discard de evento inexistente retorna 404 sem criar audit log."""
    factory, Session = client_factory

    admin_client = factory()
    _bootstrap_admin(admin_client)

    r = admin_client.post(f"/api/quarantine/{uuid4()}/discard")
    assert r.status_code == 404, r.text

    with Session() as db:
        count_before = db.query(models.MappingAuditLog).count()

    # Nenhum audit log deve ter sido criado
    assert count_before == 0
