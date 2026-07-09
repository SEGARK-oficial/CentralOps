"""Testes de integração do endpoint POST /api/quarantine/{id}/reprocess.

Cobertura:

- 404 evento inexistente.
- 404 (disfarçado de multi-tenant) — outro tenant não vê o evento.
- 409 evento já reprocessado.
- 410 evento expirado.
- 422 sem MappingDefinition para (vendor, event_type).
- 422 sem current_version_id na definição.
- 200 sucesso — reprocessed_at preenchido + audit gravado.
- 200 sucesso — _enqueue_reprocess_dispatch chamado com envelope correto.
- 422 falha de mapping — error_kind/detail atualizado + audit gravado.
- 200 dedupe: event_id já visto → não re-enfileira mas marca reprocessed_at.
- 403 viewer não tem permissão (QUARANTINE_DISCARD requerida).
- attempt_reprocess como função pura (sem DB writes).
"""

from __future__ import annotations

import json
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


# ── Helpers de seed ───────────────────────────────────────────────────


def _bootstrap_admin(client: TestClient) -> dict[str, Any]:
    r = client.post(
        "/api/auth/bootstrap",
        json={
            "username": "admin",
            "password": "AdminPassword123!",
            "display_name": "Admin",
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _login(client: TestClient, username: str, password: str) -> None:
    r = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert r.status_code == 200, f"login falhou: {r.text}"


def _create_org(session, *, name: str = "org-a") -> int:
    """Cria Organization já com ``iris_customer_id`` preenchido.

    ``settings.ENVELOPE_USE_IRIS_CUSTOMER_ID`` defaulta para True, e o
    pipeline de reprocess resolve ``envelope.customer_id`` via
    ``Organization.iris_customer_id`` — não via id interno. Sem o
    preenchimento o reprocess termina em ``error_kind=missing_customer_id``.
    Usar o mesmo valor do id interno mantém os asserts dos testes
    (``customer_id == org_id``) válidos sem precisar mexer no settings.
    """
    org = models.Organization(name=name, slug=name)
    session.add(org)
    session.commit()
    session.refresh(org)
    org.iris_customer_id = org.id
    session.commit()
    return org.id


def _create_integration(session, *, org_id: int, platform: str = "sophos") -> int:
    integration = models.Integration(
        organization_id=org_id,
        name=f"{platform}-integration",
        platform=platform,
    )
    session.add(integration)
    session.commit()
    session.refresh(integration)
    return integration.id


def _create_user_with_role(
    client: TestClient,
    *,
    username: str,
    password: str = "Password123!",
    role: str = "viewer",
) -> dict[str, Any]:
    r = client.post(
        "/api/auth/users",
        json={
            "username": username,
            "password": password,
            "display_name": username.title(),
            "role": role,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _seed_quarantine_event(
    session,
    *,
    vendor: str = "sophos",
    event_type: str | None = "sophos.alert",
    error_kind: str = "map",
    raw: dict | None = None,
    integration_id: int | None = None,
    expires_delta: timedelta = timedelta(days=7),
    reprocessed_at: datetime | None = None,
) -> str:
    now = datetime.utcnow()
    ev = models.QuarantineEvent(
        integration_id=integration_id,
        vendor=vendor,
        event_type=event_type,
        raw_payload=json.dumps(raw or {"id": "alert-x", "severity": "high"}),
        error_kind=error_kind,
        error_detail="original error",
        expires_at=now + expires_delta,
        reprocessed_at=reprocessed_at,
    )
    session.add(ev)
    session.commit()
    session.refresh(ev)
    return ev.id


def _seed_mapping(
    session,
    *,
    vendor: str = "sophos",
    event_type: str = "sophos.alert",
    rules: list | None = None,
    set_current: bool = True,
) -> tuple[str, str]:
    """Cria MappingDefinition + MappingVersion e aponta current.

    O ``rules`` é uma lista simples (formato amigável para o teste). O JSON
    persistido é envelopado no formato DSL v2 — ``{"rules": [...]}`` —
    porque ``MappingVersion.dsl_version`` defaulta para 2 e o engine
    de normalização rejeita listas nuas com a mensagem
    ``"DSL v2 espera um dict com 'rules' e 'preprocess'"``.
    """
    defn = models.MappingDefinition(
        vendor=vendor,
        event_type=event_type,
        ocsf_class_uid=2004,
    )
    session.add(defn)
    session.flush()

    rules_list = rules or [
        {"target": "normalized.class_uid", "const": 2004},
        {"target": "normalized.id", "source": "id", "required": True},
    ]
    version = models.MappingVersion(
        definition_id=defn.id,
        version_number=1,
        rules=json.dumps({"rules": rules_list}),
        commit_message="seed",
    )
    session.add(version)
    session.flush()

    if set_current:
        defn.current_version_id = version.id

    session.commit()
    session.refresh(defn)
    session.refresh(version)
    return defn.id, version.id


# ── Testes ────────────────────────────────────────────────────────────


def test_reprocess_returns_404_for_nonexistent_event(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.post(f"/api/quarantine/{uuid4()}/reprocess")
    assert r.status_code == 404


def test_reprocess_returns_403_for_other_org_event(client_factory) -> None:
    """Non-admin recebe 404 (enumeração prevenida) ao tentar acessar evento de outra org."""
    factory, Session = client_factory

    # Admin cria tudo.
    admin_client = factory()
    _bootstrap_admin(admin_client)

    with Session() as db:
        org_a = _create_org(db, name="org-a")
        org_b = _create_org(db, name="org-b")
        int_a = _create_integration(db, org_id=org_a)
        eid = _seed_quarantine_event(db, integration_id=int_a)

    # Cria usuário operator na org B — não deve ver eventos da org A.
    _create_user_with_role(admin_client, username="op-b", role="operator")
    with Session() as db:
        user_b = db.query(models.AppUser).filter_by(username="op-b").first()
        assert user_b is not None
        user_b.organization_id = org_b
        db.commit()

    op_client = factory()
    _login(op_client, "op-b", "Password123!")

    r = op_client.post(f"/api/quarantine/{eid}/reprocess")
    # 404 por segurança (prevenção de enumeração de IDs entre tenants).
    assert r.status_code == 404


def test_reprocess_returns_409_for_already_reprocessed(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    already = datetime.utcnow() - timedelta(hours=1)
    with Session() as db:
        eid = _seed_quarantine_event(db, reprocessed_at=already)

    r = client.post(f"/api/quarantine/{eid}/reprocess")
    assert r.status_code == 409
    assert "reprocessado" in r.json()["detail"].lower()


def test_reprocess_returns_410_for_expired_event(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        eid = _seed_quarantine_event(db, expires_delta=timedelta(days=-1))

    r = client.post(f"/api/quarantine/{eid}/reprocess")
    assert r.status_code == 410
    assert "expirou" in r.json()["detail"].lower()


def test_reprocess_returns_422_when_mapping_definition_missing(client_factory) -> None:
    """Sem MappingDefinition para (vendor, event_type) → 422."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org_id = _create_org(db, name="nomap-org")
        int_id = _create_integration(db, org_id=org_id)
        # Nenhum mapping seedado para sophos/sophos.alert.
        eid = _seed_quarantine_event(db, integration_id=int_id)

    r = client.post(f"/api/quarantine/{eid}/reprocess")
    assert r.status_code == 422
    error = r.json()["error"]
    assert error["code"] == "quarantine.reprocess_failed"
    assert error["details"]["error_kind"] == "missing_mapping"


def test_reprocess_returns_422_when_no_current_version(client_factory) -> None:
    """MappingDefinition existe mas current_version_id é null → 422."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org_id = _create_org(db, name="nocurver-org")
        int_id = _create_integration(db, org_id=org_id)
        eid = _seed_quarantine_event(db, integration_id=int_id)
        # Cria definição sem current_version_id.
        _seed_mapping(db, set_current=False)

    r = client.post(f"/api/quarantine/{eid}/reprocess")
    assert r.status_code == 422
    error = r.json()["error"]
    assert error["code"] == "quarantine.reprocess_failed"
    assert error["details"]["error_kind"] == "missing_mapping"


def test_reprocess_success_marks_reprocessed_at_and_audit(client_factory) -> None:
    """Sucesso completo: reprocessed_at preenchido + audit gravado."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org_id = _create_org(db, name="acme")
        int_id = _create_integration(db, org_id=org_id)
        eid = _seed_quarantine_event(
            db,
            integration_id=int_id,
            raw={"id": "alert-success", "severity": "high"},
        )
        _seed_mapping(
            db,
            rules=[
                {"target": "normalized.class_uid", "const": 2004},
                {"target": "normalized.id", "source": "id", "required": True},
            ],
        )

    with patch(
        "backend.app.routers.quarantine._enqueue_reprocess_dispatch"
    ) as mock_dispatch:
        r = client.post(f"/api/quarantine/{eid}/reprocess")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reprocessed_at"] is not None
    assert body["id"] == eid

    # Dispatch foi chamado uma vez.
    mock_dispatch.assert_called_once()

    # Audit de sucesso gravado.
    with Session() as db:
        logs = (
            db.query(models.MappingAuditLog)
            .filter_by(action="reprocess_quarantine_success")
            .all()
        )
    assert len(logs) == 1
    detail = json.loads(logs[0].detail)
    assert detail["quarantine_event_id"] == eid
    assert detail["error_kind_after"] is None


def test_reprocess_success_dispatches_via_enqueue(client_factory) -> None:
    """Verifica que _enqueue_reprocess_dispatch é chamado com envelope correto.

    O reprocess não enfileira mais ``dispatch_to_wazuh``
    diretamente. Roteia por ``_enqueue_reprocess_dispatch`` →
    ``pipeline._enqueue_dispatch``, que faz fan-out via
    ``dispatch_to_destination`` (incl. ``wazuh-default`` como destino normal).
    """
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org_id = _create_org(db, name="dispatch-org")
        int_id = _create_integration(db, org_id=org_id)
        eid = _seed_quarantine_event(
            db,
            integration_id=int_id,
            raw={"id": "alert-dispatch", "severity": "high"},
        )
        _seed_mapping(
            db,
            rules=[
                {"target": "normalized.class_uid", "const": 2004},
                {"target": "normalized.id", "source": "id", "required": True},
            ],
        )

    mock_task = MagicMock()
    mock_task.apply_async = MagicMock()

    with patch(
        "backend.app.routers.quarantine._enqueue_reprocess_dispatch"
    ) as mock_enqueue:
        r = client.post(f"/api/quarantine/{eid}/reprocess")

    assert r.status_code == 200, r.text
    # _enqueue_reprocess_dispatch foi chamado com o envelope.
    mock_enqueue.assert_called_once()
    envelope = mock_enqueue.call_args[0][0]
    # Envelope tem os blocos esperados.
    assert "_centralops" in envelope
    assert "normalized" in envelope
    assert "raw" in envelope
    assert envelope["_centralops"]["vendor"] == "sophos"


def test_reprocess_failure_updates_error_detail_and_audit(client_factory) -> None:
    """Falha de mapping: error_kind/detail atualizado; reprocessed_at = null."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org_id = _create_org(db, name="fail-org")
        int_id = _create_integration(db, org_id=org_id)
        # Evento sem o campo "id" que a regra exige com required=True.
        eid = _seed_quarantine_event(
            db,
            integration_id=int_id,
            raw={"severity": "high"},  # falta "id"
        )
        _seed_mapping(
            db,
            rules=[
                {"target": "normalized.class_uid", "const": 2004},
                # "id" required mas não existe no raw → MappingRequiredFieldError.
                {"target": "normalized.id", "source": "id", "required": True},
            ],
        )

    r = client.post(f"/api/quarantine/{eid}/reprocess")
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "quarantine.reprocess_failed"
    assert body["error"]["details"]["error_kind"] == "map"
    assert "normalized.id" in body["error"]["details"]["error_detail"]

    # Linha do banco: reprocessed_at ainda null; error_detail atualizado.
    with Session() as db:
        ev = db.get(models.QuarantineEvent, eid)
    assert ev is not None
    assert ev.reprocessed_at is None
    assert ev.error_kind == "map"

    # Audit de falha gravado.
    with Session() as db:
        logs = (
            db.query(models.MappingAuditLog)
            .filter_by(action="reprocess_quarantine_failed")
            .all()
        )
    assert len(logs) == 1
    detail = json.loads(logs[0].detail)
    assert detail["quarantine_event_id"] == eid


def test_reprocess_requires_quarantine_discard_permission(client_factory) -> None:
    """Viewer não tem QUARANTINE_DISCARD → 403."""
    factory, Session = client_factory

    admin_client = factory()
    _bootstrap_admin(admin_client)
    _create_user_with_role(admin_client, username="alice-viewer", role="viewer")

    with Session() as db:
        eid = _seed_quarantine_event(db)

    viewer_client = factory()
    _login(viewer_client, "alice-viewer", "Password123!")

    r = viewer_client.post(f"/api/quarantine/{eid}/reprocess")
    assert r.status_code == 403


def test_reprocess_operator_has_permission(client_factory) -> None:
    """Operator tem QUARANTINE_DISCARD → pode reprocessar."""
    factory, Session = client_factory

    admin_client = factory()
    _bootstrap_admin(admin_client)
    _create_user_with_role(admin_client, username="op-user", role="operator")

    with Session() as db:
        org_id = _create_org(db, name="op-org")
        int_id = _create_integration(db, org_id=org_id)
        eid = _seed_quarantine_event(
            db,
            integration_id=int_id,
            raw={"id": "op-event"},
        )
        _seed_mapping(
            db,
            rules=[
                {"target": "normalized.class_uid", "const": 2004},
                {"target": "normalized.id", "source": "id", "required": True},
            ],
        )

    # Associa operator à mesma org da integration.
    with Session() as db:
        u = db.query(models.AppUser).filter_by(username="op-user").first()
        assert u is not None
        u.organization_id = org_id
        db.commit()

    op_client = factory()
    _login(op_client, "op-user", "Password123!")

    with patch("backend.app.routers.quarantine._enqueue_reprocess_dispatch"):
        r = op_client.post(f"/api/quarantine/{eid}/reprocess")

    assert r.status_code == 200, r.text


def test_reprocess_attempt_helper_pure_function(client_factory) -> None:
    """attempt_reprocess é puro — não altera DB, não faz dispatch."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org_id = _create_org(db, name="pure-org")
        int_id = _create_integration(db, org_id=org_id)
        _seed_mapping(
            db,
            rules=[
                {"target": "normalized.class_uid", "const": 2004},
                {"target": "normalized.id", "source": "id", "required": True},
            ],
        )

    from backend.app.collectors.normalize.reprocess import attempt_reprocess

    raw_payload = json.dumps({"id": "pure-test-event", "severity": "low"})

    with Session() as db:
        result = attempt_reprocess(
            raw_payload=raw_payload,
            vendor="sophos",
            event_type="sophos.alert",
            integration_id=int_id,
            organization_id=org_id,
            db=db,
        )

    assert result.success is True
    assert result.envelope is not None
    assert result.error_kind is None
    assert "_centralops" in result.envelope
    assert result.envelope["_centralops"]["vendor"] == "sophos"
    assert result.envelope["_centralops"]["customer_id"] == org_id

    # Nada foi persistido (sem QuarantineEvent, sem MappingAuditLog extras).
    with Session() as db:
        assert db.query(models.MappingAuditLog).count() == 0


def test_reprocess_attempt_helper_returns_error_for_invalid_json(client_factory) -> None:
    """attempt_reprocess retorna error_kind=parse para raw_payload malformado."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    from backend.app.collectors.normalize.reprocess import attempt_reprocess

    with Session() as db:
        result = attempt_reprocess(
            raw_payload="not-json{{{",
            vendor="sophos",
            event_type="sophos.alert",
            integration_id=1,
            organization_id=1,
            db=db,
        )

    assert result.success is False
    assert result.error_kind == "parse"
    assert result.envelope is None


def test_reprocess_attempt_helper_rejects_truncated_payload(client_factory) -> None:
    """Evento truncado no armazenamento não reprocessa o wrapper silenciosamente.

    Um raw_payload {"_truncated": true, ...} significa que o original foi podado
    para caber no limite da quarentena. Reprocessar normalizaria o wrapper e
    re-quarentenaria com erro enganoso; o helper deve falhar explicitamente.
    """
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    from backend.app.collectors.normalize.reprocess import attempt_reprocess

    truncated = json.dumps({"_truncated": True, "id": "evt-1", "_reduced": "scalars_only"})
    with Session() as db:
        result = attempt_reprocess(
            raw_payload=truncated,
            vendor="sophos",
            event_type="sophos.detection",
            integration_id=1,
            organization_id=1,
            db=db,
        )

    assert result.success is False
    assert result.error_kind == "map"
    assert result.envelope is None
    assert "truncado" in (result.error_detail or "")


def test_reprocess_attempt_helper_missing_mapping(client_factory) -> None:
    """attempt_reprocess retorna missing_mapping quando sem definição."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    from backend.app.collectors.normalize.reprocess import attempt_reprocess

    with Session() as db:
        result = attempt_reprocess(
            raw_payload=json.dumps({"id": "x"}),
            vendor="unknown-vendor",
            event_type="unknown.event",
            integration_id=1,
            organization_id=1,
            db=db,
        )

    assert result.success is False
    assert result.error_kind == "missing_mapping"


@pytest.mark.parametrize(
    "raw,error_kind_expected",
    [
        # Campo required faltando → map error.
        ({"severity": "high"}, "map"),
        # Raw sem o campo obrigatório (id ausente).
        ({"unrelated": "field"}, "map"),
    ],
)
def test_reprocess_attempt_helper_parametrized_failures(
    raw: dict,
    error_kind_expected: str,
    client_factory,
) -> None:
    """attempt_reprocess categoriza falhas de mapping corretamente."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    from backend.app.collectors.normalize.reprocess import attempt_reprocess

    with Session() as db:
        org_id = _create_org(db, name=f"param-org-{uuid4().hex[:6]}")
        int_id = _create_integration(db, org_id=org_id)
        _seed_mapping(
            db,
            vendor="sophos",
            event_type="sophos.alert",
            rules=[
                {"target": "normalized.class_uid", "const": 2004},
                {"target": "normalized.id", "source": "id", "required": True},
            ],
        )

    with Session() as db:
        result = attempt_reprocess(
            raw_payload=json.dumps(raw),
            vendor="sophos",
            event_type="sophos.alert",
            integration_id=int_id,
            organization_id=org_id,
            db=db,
        )

    assert result.success is False
    assert result.error_kind == error_kind_expected


def test_reprocess_no_duplicate_when_called_twice(client_factory) -> None:
    """Segundo chamada retorna 409 (idempotência via reprocessed_at)."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        org_id = _create_org(db, name="dedup-org")
        int_id = _create_integration(db, org_id=org_id)
        eid = _seed_quarantine_event(
            db,
            integration_id=int_id,
            raw={"id": "dedup-event"},
        )
        _seed_mapping(
            db,
            rules=[
                {"target": "normalized.class_uid", "const": 2004},
                {"target": "normalized.id", "source": "id", "required": True},
            ],
        )

    with patch("backend.app.routers.quarantine._enqueue_reprocess_dispatch"):
        r1 = client.post(f"/api/quarantine/{eid}/reprocess")
    assert r1.status_code == 200

    # Segunda chamada: evento já reprocessado → 409.
    r2 = client.post(f"/api/quarantine/{eid}/reprocess")
    assert r2.status_code == 409
