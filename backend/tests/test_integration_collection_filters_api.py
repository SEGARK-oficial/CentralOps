"""Testes da API de filtros de coleta.

Cobre as três superfícies:

* ``GET /api/providers/platforms`` — catálogo plugin-driven: cada stream carrega
  a declaração dos seus ``CollectionFilterField`` (a UI não hardcoda nada).
* ``GET /api/integrations/{id}/collection-filters`` — valores gravados + schema
  efetivo da plataforma daquela integração.
* ``PUT /api/integrations/{id}/collection-filters`` — validação FAIL-CLOSED,
  normalização (default some, dict vazio some, JSON vazio vira NULL), permissão,
  escopo de organização e auditoria.

Roda com SQLite in-memory + override de get_session, mesmo padrão dos demais
testes da suíte.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import database as _db_module
from backend.app.db import models  # noqa: F401  — registra tabelas
from backend.app.db.database import Base, get_session
from backend.app.main import app


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture()
def client_factory(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(_db_module, "SessionLocal", TestingSession)

    def override_get_session():
        db = TestingSession()
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

    yield factory, TestingSession

    for c in clients:
        c.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


# ── Helpers ──────────────────────────────────────────────────────────────


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


def _create_user(
    admin_client: TestClient,
    *,
    username: str,
    role: str,
    organization_id: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "username": username,
        "password": "TestPassword123!",
        "display_name": username.title(),
        "role": role,
    }
    if organization_id is not None:
        payload["organization_id"] = organization_id
    r = admin_client.post("/api/auth/users", json=payload)
    assert r.status_code == 200, f"Falha ao criar user {username}: {r.text}"
    return r.json()


def _login_as(client: TestClient, *, username: str) -> None:
    r = client.post(
        "/api/auth/login",
        json={"username": username, "password": "TestPassword123!"},
    )
    assert r.status_code == 200, f"Falha ao logar como {username}: {r.text}"


def _seed_organization(Session, *, name: str) -> int:
    with Session() as db:
        db.execute(
            text(
                "INSERT INTO organizations(name, slug, is_active, created_at, "
                "updated_at, auto_managed) "
                "VALUES (:name, :slug, 1, datetime('now'), datetime('now'), 0)"
            ),
            {"name": name, "slug": name.lower().replace(" ", "-")},
        )
        db.commit()
        return db.execute(
            text("SELECT id FROM organizations WHERE name=:n"), {"n": name}
        ).fetchone().id


def _seed_integration(
    Session,
    *,
    org_id: int,
    name: str,
    platform: str = "wazuh",
    collection_filters: str | None = None,
) -> int:
    with Session() as db:
        db.execute(
            text(
                "INSERT INTO integrations(organization_id, name, platform, "
                "is_active, kind, auth_status, created_at, updated_at, "
                "auto_managed, collection_filters) "
                "VALUES (:org, :name, :plat, 1, 'tenant', 'unknown', "
                "datetime('now'), datetime('now'), 0, :filters)"
            ),
            {"org": org_id, "name": name, "plat": platform, "filters": collection_filters},
        )
        db.commit()
        return db.execute(
            text("SELECT id FROM integrations WHERE name=:n"), {"n": name}
        ).fetchone().id


def _stored_filters(Session, integration_id: int) -> str | None:
    with Session() as db:
        return db.execute(
            text("SELECT collection_filters FROM integrations WHERE id=:i"),
            {"i": integration_id},
        ).scalar()


def _audit_details(Session, *, action: str) -> list[str]:
    with Session() as db:
        return [
            row.detail
            for row in db.execute(
                text("SELECT detail FROM audit_logs WHERE action=:a ORDER BY id"),
                {"a": action},
            ).fetchall()
        ]


# ── Catálogo: GET /providers/platforms ───────────────────────────────────


def test_catalog_exposes_declared_filters(client_factory) -> None:
    """O stream carrega a declaração do filtro — é o que torna a UI plugin-driven."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.get("/api/providers/platforms")
    assert r.status_code == 200, r.text
    wazuh = next(p for p in r.json() if p["platform"] == "wazuh")
    detections = next(s for s in wazuh["streams"] if s["stream"] == "detections")

    field = next(f for f in detections["filters"] if f["key"] == "min_rule_level")
    assert field["type"] == "int_range"
    # default é o valor que NÃO filtra nada: quem nunca abriu a tela coleta igual.
    assert field["default"] == 0
    assert (field["min"], field["max"]) == (0, 16)
    assert field["help_text"]
    # O aviso precisa chegar à tela: o filtrado na origem não entra na plataforma.
    assert field["warning_text"]


def test_catalog_streams_without_filters_are_empty(client_factory) -> None:
    """Stream que não sabe filtrar na origem devolve lista vazia (UI esconde a seção)."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.get("/api/providers/platforms")
    sophos = next(p for p in r.json() if p["platform"] == "sophos")
    assert sophos["streams"], "sophos deveria ter streams registrados"
    assert all(s["filters"] == [] for s in sophos["streams"])


# ── Leitura: GET /integrations/{id}/collection-filters ───────────────────


def test_get_returns_schema_and_empty_values(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)
    org_id = _seed_organization(Session, name="ReadOrg")
    int_id = _seed_integration(Session, org_id=org_id, name="Wazuh-Read")

    r = client.get(f"/api/integrations/{int_id}/collection-filters")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["integration_id"] == int_id
    assert body["platform"] == "wazuh"
    assert body["filters"] == {}
    assert list(body["available_filters"]) == ["detections"]
    assert [f["key"] for f in body["available_filters"]["detections"]] == ["min_rule_level"]


def test_get_omits_platforms_without_filters(client_factory) -> None:
    """Plataforma sem filtro declarado: nada a mostrar, nem schema nem valor."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)
    org_id = _seed_organization(Session, name="SophosOrg")
    int_id = _seed_integration(Session, org_id=org_id, name="Sophos-Read", platform="sophos")

    r = client.get(f"/api/integrations/{int_id}/collection-filters")
    assert r.status_code == 200, r.text
    assert r.json()["available_filters"] == {}
    assert r.json()["filters"] == {}


def test_get_hides_stored_value_that_no_longer_validates(client_factory) -> None:
    """Valor gravado fora do contrato atual do plugin NÃO é ecoado.

    O coletor é fail-open: ignora e coleta sem filtro. Devolver o valor aqui faria
    a tela anunciar uma redução de volume que não está acontecendo.
    """
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)
    org_id = _seed_organization(Session, name="StaleOrg")
    int_id = _seed_integration(
        Session,
        org_id=org_id,
        name="Wazuh-Stale",
        collection_filters=json.dumps({"detections": {"min_rule_level": 99}}),
    )

    r = client.get(f"/api/integrations/{int_id}/collection-filters")
    assert r.status_code == 200, r.text
    assert r.json()["filters"] == {}


def test_get_scoped_user_from_other_org_is_denied(client_factory) -> None:
    factory, Session = client_factory
    admin = factory()
    _bootstrap_admin(admin)
    org_a = _seed_organization(Session, name="OrgA")
    org_b = _seed_organization(Session, name="OrgB")
    int_b = _seed_integration(Session, org_id=org_b, name="Wazuh-B")
    _create_user(admin, username="viewer_a", role="viewer", organization_id=org_a)

    scoped = factory()
    _login_as(scoped, username="viewer_a")
    r = scoped.get(f"/api/integrations/{int_b}/collection-filters")
    assert r.status_code in (403, 404), r.text


# ── Escrita: PUT /integrations/{id}/collection-filters ───────────────────


def test_put_persists_and_audits(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)
    org_id = _seed_organization(Session, name="WriteOrg")
    int_id = _seed_integration(Session, org_id=org_id, name="Wazuh-Write")

    r = client.put(
        f"/api/integrations/{int_id}/collection-filters",
        json={"filters": {"detections": {"min_rule_level": 7}}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["filters"] == {"detections": {"min_rule_level": 7}}
    assert json.loads(_stored_filters(Session, int_id)) == {
        "detections": {"min_rule_level": 7}
    }

    # Auditoria com antes→depois: o que deixa de ser coletado não volta.
    details = _audit_details(Session, action="update_collection_filters")
    assert len(details) == 1
    assert "min_rule_level" in details[0]
    assert "→" in details[0]

    # Round-trip: o GET devolve exatamente o que o PUT aceita.
    assert client.get(f"/api/integrations/{int_id}/collection-filters").json()["filters"] == {
        "detections": {"min_rule_level": 7}
    }


def test_put_default_value_clears_the_column(client_factory) -> None:
    """Voltar ao default limpa: coluna NULL, não ``{}`` — nem ``{"detections": {}}``."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)
    org_id = _seed_organization(Session, name="ResetOrg")
    int_id = _seed_integration(
        Session,
        org_id=org_id,
        name="Wazuh-Reset",
        collection_filters=json.dumps({"detections": {"min_rule_level": 12}}),
    )

    r = client.put(
        f"/api/integrations/{int_id}/collection-filters",
        json={"filters": {"detections": {"min_rule_level": 0}}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["filters"] == {}
    assert _stored_filters(Session, int_id) is None


def test_put_empty_body_wipes_configuration(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)
    org_id = _seed_organization(Session, name="WipeOrg")
    int_id = _seed_integration(
        Session,
        org_id=org_id,
        name="Wazuh-Wipe",
        collection_filters=json.dumps({"detections": {"min_rule_level": 7}}),
    )

    r = client.put(f"/api/integrations/{int_id}/collection-filters", json={"filters": {}})
    assert r.status_code == 200, r.text
    assert _stored_filters(Session, int_id) is None


def test_put_unknown_stream_422(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)
    org_id = _seed_organization(Session, name="UnknownStreamOrg")
    int_id = _seed_integration(Session, org_id=org_id, name="Wazuh-UnknownStream")

    r = client.put(
        f"/api/integrations/{int_id}/collection-filters",
        json={"filters": {"nao_existe": {"min_rule_level": 7}}},
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "integration.collection_filter_stream_unknown"
    assert "nao_existe" in r.json()["detail"]
    assert _stored_filters(Session, int_id) is None


def test_put_stream_without_declared_filters_422(client_factory) -> None:
    """sophos/alerts existe, mas não sabe filtrar na origem — erro, não no-op."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)
    org_id = _seed_organization(Session, name="NoFilterOrg")
    int_id = _seed_integration(
        Session, org_id=org_id, name="Sophos-NoFilter", platform="sophos"
    )

    r = client.put(
        f"/api/integrations/{int_id}/collection-filters",
        json={"filters": {"alerts": {"min_rule_level": 7}}},
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "integration.collection_filter_stream_unsupported"
    assert "alerts" in r.json()["detail"]


def test_put_value_out_of_range_422_with_reason(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)
    org_id = _seed_organization(Session, name="RangeOrg")
    int_id = _seed_integration(Session, org_id=org_id, name="Wazuh-Range")

    r = client.put(
        f"/api/integrations/{int_id}/collection-filters",
        json={"filters": {"detections": {"min_rule_level": 99}}},
    )
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["error"]["code"] == "integration.collection_filter_invalid"
    # A mensagem do plugin chega ao operador: chave + faixa violada.
    assert "min_rule_level" in body["detail"]
    assert "[0, 16]" in body["detail"]
    assert _stored_filters(Session, int_id) is None


def test_put_unknown_key_422(client_factory) -> None:
    """Chave desconhecida é erro — descartar em silêncio faria o operador achar
    que reduziu volume sem ter reduzido nada."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)
    org_id = _seed_organization(Session, name="UnknownKeyOrg")
    int_id = _seed_integration(Session, org_id=org_id, name="Wazuh-UnknownKey")

    r = client.put(
        f"/api/integrations/{int_id}/collection-filters",
        json={"filters": {"detections": {"min_rule_levl": 7}}},
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "integration.collection_filter_invalid"
    assert "min_rule_levl" in r.json()["detail"]


def test_put_wrong_type_422(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)
    org_id = _seed_organization(Session, name="TypeOrg")
    int_id = _seed_integration(Session, org_id=org_id, name="Wazuh-Type")

    r = client.put(
        f"/api/integrations/{int_id}/collection-filters",
        json={"filters": {"detections": {"min_rule_level": "sete"}}},
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "integration.collection_filter_invalid"


def test_put_requires_integration_write(client_factory) -> None:
    """Viewer lê (INTEGRATION_READ) mas não escreve — filtrar é decisão de escrita."""
    factory, Session = client_factory
    admin = factory()
    _bootstrap_admin(admin)
    org_id = _seed_organization(Session, name="RbacOrg")
    int_id = _seed_integration(Session, org_id=org_id, name="Wazuh-Rbac")
    _create_user(admin, username="viewer_rbac", role="viewer", organization_id=org_id)

    viewer = factory()
    _login_as(viewer, username="viewer_rbac")
    assert viewer.get(f"/api/integrations/{int_id}/collection-filters").status_code == 200

    r = viewer.put(
        f"/api/integrations/{int_id}/collection-filters",
        json={"filters": {"detections": {"min_rule_level": 7}}},
    )
    assert r.status_code == 403, r.text
    assert _stored_filters(Session, int_id) is None


def test_put_requires_authentication(client_factory) -> None:
    factory, Session = client_factory
    admin = factory()
    _bootstrap_admin(admin)
    org_id = _seed_organization(Session, name="AnonOrg")
    int_id = _seed_integration(Session, org_id=org_id, name="Wazuh-Anon")

    anon = factory()
    r = anon.put(
        f"/api/integrations/{int_id}/collection-filters",
        json={"filters": {"detections": {"min_rule_level": 7}}},
    )
    assert r.status_code in (401, 403), r.text


def test_put_unknown_integration_404(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    r = client.put(
        "/api/integrations/999999/collection-filters",
        json={"filters": {"detections": {"min_rule_level": 7}}},
    )
    assert r.status_code == 404, r.text
