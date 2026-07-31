"""Campos NULLABLE de ``routes`` podem ser LIMPOS via ``null`` explícito no PUT.

``update_route`` (backend/app/routers/routes.py) usava o idiom
``payload.X if payload.X is not None else repository._UNSET`` para quase todos
os campos. Para uma coluna NOT NULL isso é correto — não há estado "limpo".
Para uma coluna NULLABLE ele colapsa "campo ausente do payload" e "campo
enviado como null" no mesmo sentinel, tornando a limpeza IMPOSSÍVEL: o PUT
responde 200 e devolve o valor antigo intacto.

``suppress_key`` (ADR-0015) já tinha o tratamento correto via
``model_fields_set``; um comentário no router afirmava ser ele "o único campo
novo nullable-com-significado". A premissa estava errada — considerou apenas os
5 campos do ADR-0015 e ignorou que ``pii_redaction`` (ADR-0003),
``transform_ref`` e ``organization_id`` já eram nullable antes disso.

Sintoma reportado em produção: limpar as regras de PII de uma rota pela UI
enviava ``{"pii_redaction": null}``, recebia 200, e a resposta trazia as regras
antigas — a redação continuava ativa no pipeline.

O repositório (``RouteRepository.update``) sempre soube limpar os três campos;
o defeito estava inteiro no router.
"""
from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db.database import Base, get_session
from backend.app.main import app


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


def _bootstrap_admin(client: TestClient) -> None:
    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPassword123!", "display_name": "Admin"},
    )
    assert r.status_code == 200, r.text


def _seed_destination(client: TestClient, name: str = "Dest A") -> str:
    r = client.post(
        "/api/collectors/destinations",
        json={
            "name": name,
            "kind": "syslog_rfc3164",
            "config": {"host": "h", "port": 514},
            "auto_route": False,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


_RULES = [{"action": "drop_field", "path": "raw.rawData"}]


def _seed_route(client: TestClient, dest: str, **extra) -> str:
    payload = {"name": "Catch-all", "condition": {}, "destination_ids": [dest]}
    payload.update(extra)
    r = client.post("/api/collectors/routes", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ── pii_redaction (o bug reportado) ────────────────────────────────────────


def test_pii_redaction_can_be_explicitly_cleared(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)
    rid = _seed_route(client, dest, pii_redaction=_RULES)

    assert client.get(f"/api/collectors/routes/{rid}").json()["pii_redaction"] == _RULES

    # null EXPLÍCITO → limpa. Antes do fix voltava com _RULES e HTTP 200.
    r = client.put(f"/api/collectors/routes/{rid}", json={"pii_redaction": None})
    assert r.status_code == 200, r.text
    assert r.json()["pii_redaction"] is None
    assert client.get(f"/api/collectors/routes/{rid}").json()["pii_redaction"] is None


def test_pii_redaction_absent_from_payload_is_preserved(client_factory) -> None:
    """Regressão inversa: ausência NÃO pode virar clear indevido."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)
    rid = _seed_route(client, dest, pii_redaction=_RULES)

    r = client.put(f"/api/collectors/routes/{rid}", json={"priority": 20})
    assert r.status_code == 200, r.text
    assert r.json()["pii_redaction"] == _RULES
    assert r.json()["priority"] == 20


def test_pii_redaction_empty_list_also_clears(client_factory) -> None:
    """``[]`` já funcionava (falsy → NULL no repo); segue funcionando."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)
    rid = _seed_route(client, dest, pii_redaction=_RULES)

    r = client.put(f"/api/collectors/routes/{rid}", json={"pii_redaction": []})
    assert r.status_code == 200, r.text
    assert r.json()["pii_redaction"] is None


def test_full_form_put_clears_pii_redaction(client_factory) -> None:
    """O payload REAL da UI (form completo, não patch), como capturado em prod.

    Todos os campos presentes, ``pii_redaction: null``, ``drop_raw: true``.
    """
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)
    rid = _seed_route(client, dest, pii_redaction=_RULES)

    r = client.put(
        f"/api/collectors/routes/{rid}",
        json={
            "name": "Catch-all",
            "priority": 20,
            "action": "route",
            "condition": {},
            "destination_ids": [dest],
            "is_final": True,
            "enabled": True,
            "canary_percent": 100,
            "pii_redaction": None,
            "protect_detection": False,
            "sample_percent": 100,
            "suppress_key": None,
            "suppress_allow": 2,
            "suppress_window_s": 150,
            "drop_raw": True,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pii_redaction"] is None
    assert body["suppress_key"] is None
    # Os demais campos do mesmo PUT continuam aplicados.
    assert body["drop_raw"] is True
    assert body["suppress_window_s"] == 150
    assert body["protect_detection"] is False


# ── transform_ref ─────────────────────────────────────────────────────────


def test_transform_ref_can_be_explicitly_cleared(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)
    rid = _seed_route(client, dest, transform_ref="map-x")

    assert client.get(f"/api/collectors/routes/{rid}").json()["transform_ref"] == "map-x"

    r = client.put(f"/api/collectors/routes/{rid}", json={"transform_ref": None})
    assert r.status_code == 200, r.text
    assert r.json()["transform_ref"] is None


def test_transform_ref_absent_from_payload_is_preserved(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)
    rid = _seed_route(client, dest, transform_ref="map-x")

    r = client.put(f"/api/collectors/routes/{rid}", json={"enabled": False})
    assert r.status_code == 200, r.text
    assert r.json()["transform_ref"] == "map-x"


# ── protect_detection: fail-safe NÃO pode regredir ────────────────────────


def test_protect_detection_not_downgraded_by_absence(client_factory) -> None:
    """Coluna NOT NULL: segue no idiom ``is not None``. Ausência nunca vira
    False — o fail-safe de detecção depende disso.
    """
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)
    rid = _seed_route(client, dest)

    assert client.get(f"/api/collectors/routes/{rid}").json()["protect_detection"] is True

    r = client.put(f"/api/collectors/routes/{rid}", json={"priority": 5})
    assert r.status_code == 200, r.text
    assert r.json()["protect_detection"] is True
