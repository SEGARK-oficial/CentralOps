"""ADR-0015 — expõe as 5 alavancas de redução de volume da tabela ``routes``
(``protect_detection``, ``sample_percent``, ``suppress_key``, ``suppress_allow``,
``suppress_window_s``) de ponta a ponta na API (``POST``/``PUT``/``GET``
``/collectors/routes``).

Antes deste fix, ``RouteCreate``/``RouteUpdate``/``RouteRead``
(backend/app/api/schemas_routes.py) e ``RouteRepository.add``/``update``
(backend/app/db/repository.py) não conheciam esses campos: Pydantic
``extra="ignore"`` (default) os descartava em silêncio no create/update, e o
GET nunca os devolvia — as alavancas eram inconfiguráveis por qualquer
interface, mesmo já sendo lidas pelo pipeline de dispatch
(``backend/app/collectors/pipeline.py:_compile_route_row``).

Cobre:
  * create com os 5 campos → persistidos e devolvidos no GET;
  * create SEM os campos → defaults do modelo (``protect_detection=True``);
  * update parcial de UM campo não zera os outros 4;
  * ``suppress_key`` pode ser LIMPO explicitamente (``null`` no PUT) — bug
    conhecido no repo (``CorrelationRuleRepository.update``,
    repository.py:1947-1952): ``if value is not None: setattr(...)`` descarta
    um null explícito do cliente e devolve 200 no-op. Aqui NÃO reproduzimos
    o bug: ausência (campo fora do payload) mantém o valor atual; ``null``
    explícito limpa;
  * ``sample_percent`` fora de 0-100 → 422 (não persiste nada);
  * ``protect_detection`` NUNCA vira False por omissão (fail-safe de
    detecção) — nem no create nem no update.
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


# ── create: os 5 campos persistem e voltam no read ─────────────────────────


def test_create_with_reduction_fields_round_trips(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)

    r = client.post(
        "/api/collectors/routes",
        json={
            "name": "low-noise route",
            "condition": {},
            "destination_ids": [dest],
            "protect_detection": False,
            "sample_percent": 25,
            "suppress_key": "vendor,severity_id",
            "suppress_allow": 3,
            "suppress_window_s": 45,
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["protect_detection"] is False
    assert body["sample_percent"] == 25
    assert body["suppress_key"] == "vendor,severity_id"
    assert body["suppress_allow"] == 3
    assert body["suppress_window_s"] == 45

    got = client.get(f"/api/collectors/routes/{body['id']}").json()
    assert got["protect_detection"] is False
    assert got["sample_percent"] == 25
    assert got["suppress_key"] == "vendor,severity_id"
    assert got["suppress_allow"] == 3
    assert got["suppress_window_s"] == 45


# ── create sem os campos → defaults do modelo, protect_detection=True ──────


def test_create_without_reduction_fields_uses_model_defaults(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)

    r = client.post(
        "/api/collectors/routes",
        json={"name": "default route", "condition": {}, "destination_ids": [dest]},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["protect_detection"] is True  # fail-safe: protege por default
    assert body["sample_percent"] == 100  # sem amostragem (byte-idêntico)
    assert body["suppress_key"] is None
    assert body["suppress_allow"] == 0  # supressão desligada
    assert body["suppress_window_s"] == 30

    got = client.get(f"/api/collectors/routes/{body['id']}").json()
    assert got["protect_detection"] is True
    assert got["sample_percent"] == 100
    assert got["suppress_key"] is None
    assert got["suppress_allow"] == 0
    assert got["suppress_window_s"] == 30


# ── update parcial de um campo não zera os outros ───────────────────────────


def test_partial_update_of_one_field_preserves_others(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)

    rid = client.post(
        "/api/collectors/routes",
        json={
            "name": "r",
            "condition": {},
            "destination_ids": [dest],
            "protect_detection": False,
            "sample_percent": 40,
            "suppress_key": "vendor",
            "suppress_allow": 2,
            "suppress_window_s": 20,
        },
    ).json()["id"]

    # Só mexe em sample_percent — os outros 4 devem sobreviver intactos.
    r = client.put(f"/api/collectors/routes/{rid}", json={"sample_percent": 80})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sample_percent"] == 80
    assert body["protect_detection"] is False
    assert body["suppress_key"] == "vendor"
    assert body["suppress_allow"] == 2
    assert body["suppress_window_s"] == 20

    got = client.get(f"/api/collectors/routes/{rid}").json()
    assert got["sample_percent"] == 80
    assert got["protect_detection"] is False
    assert got["suppress_key"] == "vendor"
    assert got["suppress_allow"] == 2
    assert got["suppress_window_s"] == 20


# ── suppress_key pode ser LIMPO explicitamente (o bug do item 3) ───────────


def test_suppress_key_can_be_explicitly_cleared(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)

    rid = client.post(
        "/api/collectors/routes",
        json={
            "name": "r",
            "condition": {},
            "destination_ids": [dest],
            "suppress_key": "vendor",
            "suppress_allow": 5,
        },
    ).json()["id"]
    assert client.get(f"/api/collectors/routes/{rid}").json()["suppress_key"] == "vendor"

    # null EXPLÍCITO no payload → limpa (não é "campo ausente").
    r = client.put(f"/api/collectors/routes/{rid}", json={"suppress_key": None})
    assert r.status_code == 200, r.text
    assert r.json()["suppress_key"] is None
    # suppress_allow NÃO deveria ter sido tocado por esse PUT.
    assert r.json()["suppress_allow"] == 5

    got = client.get(f"/api/collectors/routes/{rid}").json()
    assert got["suppress_key"] is None
    assert got["suppress_allow"] == 5

    # Round 2: PUT que NÃO menciona suppress_key (campo ausente do payload,
    # não null) não deve alterar o que já está lá — regressão contra o
    # padrão inverso (ausência virando clear indevido).
    r2 = client.put(f"/api/collectors/routes/{rid}", json={"suppress_allow": 9})
    assert r2.status_code == 200, r2.text
    assert r2.json()["suppress_key"] is None  # continua None (já estava)
    assert r2.json()["suppress_allow"] == 9

    r3 = client.put(f"/api/collectors/routes/{rid}", json={"suppress_key": "platform"})
    assert r3.status_code == 200, r3.text
    assert r3.json()["suppress_key"] == "platform"
    r4 = client.put(f"/api/collectors/routes/{rid}", json={"suppress_allow": 1})
    assert r4.status_code == 200, r4.text
    # campo ausente do payload (não null) → suppress_key NÃO é zerado.
    assert r4.json()["suppress_key"] == "platform"


# ── faixa inválida → 422, nada persistido ───────────────────────────────────


@pytest.mark.parametrize("bad_sample_percent", [-1, 101, 1000])
def test_sample_percent_out_of_range_rejected_on_create(client_factory, bad_sample_percent) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)

    r = client.post(
        "/api/collectors/routes",
        json={
            "name": "bad",
            "condition": {},
            "destination_ids": [dest],
            "sample_percent": bad_sample_percent,
        },
    )
    assert r.status_code == 422, r.text
    assert client.get("/api/collectors/routes").json() == []


def test_sample_percent_out_of_range_rejected_on_update(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)
    rid = client.post(
        "/api/collectors/routes",
        json={"name": "r", "condition": {}, "destination_ids": [dest], "sample_percent": 50},
    ).json()["id"]

    r = client.put(f"/api/collectors/routes/{rid}", json={"sample_percent": 150})
    assert r.status_code == 422, r.text
    # não persistiu o valor inválido — segue com o anterior.
    assert client.get(f"/api/collectors/routes/{rid}").json()["sample_percent"] == 50


@pytest.mark.parametrize(
    "field,value",
    [
        ("suppress_allow", -1),
        ("suppress_window_s", 0),
        ("suppress_window_s", -5),
    ],
)
def test_other_range_constraints_rejected(client_factory, field, value) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)

    r = client.post(
        "/api/collectors/routes",
        json={"name": "bad", "condition": {}, "destination_ids": [dest], field: value},
    )
    assert r.status_code == 422, r.text


# ── protect_detection nunca vira False por omissão ──────────────────────────


def test_protect_detection_never_false_by_omission_on_create(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)

    r = client.post(
        "/api/collectors/routes",
        json={"name": "r", "condition": {}, "destination_ids": [dest]},
    )
    assert r.json()["protect_detection"] is True


def test_protect_detection_preserved_across_unrelated_update(client_factory) -> None:
    """Uma rota criada com protect_detection=True (default) permanece True
    depois de um PUT que não menciona o campo — omissão nunca rebaixa o
    fail-safe."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)
    rid = client.post(
        "/api/collectors/routes",
        json={"name": "r", "condition": {}, "destination_ids": [dest]},
    ).json()["id"]
    assert client.get(f"/api/collectors/routes/{rid}").json()["protect_detection"] is True

    r = client.put(f"/api/collectors/routes/{rid}", json={"priority": 5})
    assert r.status_code == 200, r.text
    assert r.json()["protect_detection"] is True

    got = client.get(f"/api/collectors/routes/{rid}").json()
    assert got["protect_detection"] is True


def test_protect_detection_explicit_false_is_respected_and_stays_after_unrelated_update(
    client_factory,
) -> None:
    """Opt-out explícito (False) é uma decisão consciente do operador — deve
    ser respeitado no create E sobreviver a updates que não o mencionam
    (mas SEM nunca poder ser rebaixado por omissão a partir de True)."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)
    rid = client.post(
        "/api/collectors/routes",
        json={"name": "r", "condition": {}, "destination_ids": [dest], "protect_detection": False},
    ).json()["id"]
    assert client.get(f"/api/collectors/routes/{rid}").json()["protect_detection"] is False

    r = client.put(f"/api/collectors/routes/{rid}", json={"priority": 7})
    assert r.status_code == 200, r.text
    assert r.json()["protect_detection"] is False  # omissão preserva o valor atual (False)

    # Reversão exige decisão EXPLÍCITA (True) — não acontece sozinha.
    r2 = client.put(f"/api/collectors/routes/{rid}", json={"protect_detection": True})
    assert r2.status_code == 200, r2.text
    assert r2.json()["protect_detection"] is True
