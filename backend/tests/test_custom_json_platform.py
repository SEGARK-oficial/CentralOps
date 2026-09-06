"""Fonte genérica ``custom_json`` (W3.1): streams dinâmicos definidos em banco.

Cobre as três camadas: (1) o módulo (nome, esqueleto, fábrica de collector),
(2) o registry resolvendo streams por resolver — inclusive com banco fora,
(3) o fluxo HTTP: criar o stream → token → ``POST /api/ingest/<stream>`` →
o pipeline resolve o mapping v1 da definição recém-criada.
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.collectors import registry
from backend.app.collectors import scheduler as scheduler_mod
from backend.app.collectors.normalize.engine import compile_rules
from backend.app.collectors.vendors import custom_json
from backend.app.db import database as _db_module
from backend.app.db import models
from backend.app.db.database import Base, get_session
from backend.app.main import app
from backend.app.routers import ingest as ingest_router


# ── 1. módulo ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["fw", "fw-logs", "app_01", "a" * 63, "  fw  "])
def test_nome_valido(name):
    assert custom_json.validate_stream_name(name) == name.strip()


@pytest.mark.parametrize("name", ["", "Fw", "fw logs", "-fw", "a" * 64, "fw/x", "fw.logs", "integrations", None])
def test_nome_invalido_ou_reservado(name):
    with pytest.raises(custom_json.InvalidStreamName):
        custom_json.validate_stream_name(name)


def test_event_type_ida_e_volta():
    assert custom_json.event_type_for("fw-logs") == "custom_json.fw-logs"
    assert custom_json.stream_name("custom_json.fw-logs") == "fw-logs"
    assert custom_json.stream_name("fortinet_fortigate.traffic") is None
    assert custom_json.stream_name("custom_json.Bad Name") is None


@pytest.mark.parametrize("class_uid", [0, 1006, 2004, 3002, 4001, 6003])
def test_esqueleto_compila_e_emite_a_classe(class_uid):
    rules = custom_json.skeleton_rules("fw", class_uid)
    compile_rules(rules)
    consts = {r["target"]: r.get("const") for r in rules["rules"] if "const" in r}
    assert consts["normalized.class_uid"] == class_uid
    assert consts["normalized.category_uid"] == class_uid // 1000
    assert consts["normalized.metadata.product.name"] == "fw"
    # time obrigatório vem do carimbo de recepção: nada cai na quarentena por
    # required ausente antes de o operador apontar o campo do produto.
    time_rule = next(r for r in rules["rules"] if r["target"] == "normalized.time")
    assert time_rule["source"] == "_ingest.received_at" and time_rule["required"] is True


def test_fabrica_de_collector_por_stream_e_estavel():
    a = custom_json.collector_class_for("fw")
    b = custom_json.collector_class_for("fw")
    assert a is b
    assert (a.platform, a.stream, a.event_type) == ("custom_json", "fw", "custom_json.fw")
    assert custom_json.collector_class_for("other") is not a


# ── 2. registry + resolver ────────────────────────────────────────────────────

def test_registry_resolve_stream_dinamico_pelo_banco(monkeypatch):
    monkeypatch.setattr(custom_json, "streams_from_db", lambda db=None: ["dyn-a", "dyn-b"])
    custom_json.invalidate()
    assert registry.has("custom_json", "dyn-a")
    assert registry.get("custom_json", "dyn-b").collector_cls.event_type == "custom_json.dyn-b"
    assert {"dyn-a", "dyn-b"} <= set(registry.supported_streams("custom_json"))
    assert not registry.has("custom_json", "nunca-criado")
    with pytest.raises(KeyError):
        registry.get("custom_json", "nunca-criado")


def test_banco_fora_mantem_o_registrado_e_nao_levanta(monkeypatch):
    monkeypatch.setattr(custom_json, "streams_from_db", lambda db=None: ["dyn-keep"])
    custom_json.invalidate()
    assert registry.has("custom_json", "dyn-keep")

    def _boom(db=None):
        raise RuntimeError("db down")

    monkeypatch.setattr(custom_json, "streams_from_db", _boom)
    custom_json.invalidate()
    assert registry.has("custom_json", "dyn-keep")          # já registrado: segue drenando
    assert not registry.has("custom_json", "dyn-nunca")      # desconhecido: False, sem exceção


def test_plataforma_no_catalogo_e_push():
    reg = registry.get_platform("custom_json")
    assert reg is not None and reg.transport == "push" and reg.auth_fields == ()


def test_resolver_e_cacheado(monkeypatch):
    calls = []

    def _list(db=None):
        calls.append(1)
        return ["dyn-cache"]

    monkeypatch.setattr(custom_json, "streams_from_db", _list)
    custom_json.invalidate()
    registry.has("custom_json", "dyn-cache")
    registry.has("custom_json", "dyn-cache-nope")
    registry.supported_streams("custom_json")
    assert len(calls) == 1


# ── 3. fluxo HTTP ─────────────────────────────────────────────────────────────

@pytest.fixture()
def setup(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_session():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_session] = override_get_session
    # O resolver e o pipeline abrem sessão própria: apontam para o mesmo banco.
    monkeypatch.setattr(_db_module, "SessionLocal", TestingSession)
    monkeypatch.setattr(_db_module, "engine", engine)

    server = fakeredis.aioredis.FakeRedis(decode_responses=True)

    async def fake_redis_client():
        return server

    async def _noop_aclose(*a, **k):
        return None

    server.aclose = _noop_aclose
    monkeypatch.setattr(ingest_router, "_redis_client", fake_redis_client)

    rescheduled: list = []
    monkeypatch.setattr(scheduler_mod, "register_integration_in_beat", lambda iid: rescheduled.append(iid))

    with TestingSession() as db:
        org = models.Organization(name="ACME", slug="acme")
        db.add(org)
        db.flush()
        integ = models.Integration(organization_id=org.id, name="Generic JSON", platform="custom_json", is_active=True)
        db.add(integ)
        db.commit()
        integ_id = integ.id

    client = TestClient(app)
    r = client.post("/api/auth/bootstrap", json={"username": "admin", "password": "AdminPass1!", "display_name": "Admin"})
    assert r.status_code == 200, r.text
    r = client.post("/api/auth/login", json={"username": "admin", "password": "AdminPass1!"})
    assert r.status_code == 200, r.text

    custom_json.invalidate()
    yield client, integ_id, server, rescheduled, TestingSession

    client.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)
    custom_json.invalidate()


def test_fluxo_criar_stream_ingerir_e_resolver_mapping(setup):
    from backend.app.collectors.pipeline import _load_current_mapping

    client, integ_id, server, rescheduled, Session = setup

    # Antes do stream: 404 no ingest e vazio na lista.
    token = client.post(f"/api/ingest/integrations/{integ_id}/token").json()["token"]
    r = client.post("/api/ingest/http-fw", data='{"a":1}', headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    assert r.status_code == 404
    assert client.get("/api/mappings/custom-streams").json() == []

    r = client.post("/api/mappings/custom-streams", json={"stream": "http-fw", "ocsf_class_uid": 4001, "description": "Firewall X"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["event_type"] == "custom_json.http-fw"
    assert body["ocsf_class_name"] == "Network Activity"
    assert body["endpoint"] == "/api/ingest/http-fw"
    assert body["current_version_id"]
    assert rescheduled == [integ_id]

    # Lista + definição aparecem onde os mappings sempre estiveram.
    assert [s["stream"] for s in client.get("/api/mappings/custom-streams").json()] == ["http-fw"]
    defs = client.get("/api/mappings").json()
    assert any(d["vendor"] == "custom_json" and d["event_type"] == "custom_json.http-fw" for d in defs)

    # Ingest passa a aceitar o stream e o info o lista.
    body = '{"srcip":"10.0.0.1"}\n{"srcip":"10.0.0.2"}'
    r = client.post("/api/ingest/http-fw", data=body, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/x-ndjson"})
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] == 2
    info = client.get(f"/api/ingest/integrations/{integ_id}").json()
    assert "http-fw" in info["streams"] and info["buffer_depth"] == 2

    # O pipeline resolve o mapping v1 pelo (platform, event_type) do collector.
    reg = registry.get("custom_json", "http-fw")
    resolved = _load_current_mapping(reg.platform, reg.collector_cls.event_type)
    assert resolved is not None
    _version_id, rules, dsl_version = resolved
    assert dsl_version == 2
    assert any(r.get("target") == "normalized.class_uid" and r.get("const") == 4001 for r in rules["rules"])

    # Auditoria registrou a criação.
    with Session() as db:
        rows = db.query(models.MappingAuditLog).filter(models.MappingAuditLog.detail.like("custom_json:%")).all()
        assert len(rows) == 1 and rows[0].username == "admin"


def test_erros_de_criacao(setup):
    client, *_ = setup
    assert client.post("/api/mappings/custom-streams", json={"stream": "dup", "ocsf_class_uid": 0}).status_code == 201
    r = client.post("/api/mappings/custom-streams", json={"stream": "dup", "ocsf_class_uid": 0})
    assert r.status_code == 409 and r.json()["error"]["code"] == "mapping.stream_exists"
    r = client.post("/api/mappings/custom-streams", json={"stream": "Bad Name", "ocsf_class_uid": 0})
    assert r.status_code == 422 and r.json()["error"]["code"] == "mapping.invalid_stream_name"
    r = client.post("/api/mappings/custom-streams", json={"stream": "integrations", "ocsf_class_uid": 0})
    assert r.status_code == 422 and r.json()["error"]["code"] == "mapping.invalid_stream_name"
    r = client.post("/api/mappings/custom-streams", json={"stream": "ok-name", "ocsf_class_uid": 9999})
    assert r.status_code == 422 and r.json()["error"]["code"] == "mapping.invalid_class_uid"
    # Nada disso vazou para o registry.
    assert not registry.has("custom_json", "ok-name")


def test_stream_de_outro_processo_aparece_pelo_resolver(setup):
    """Simula o worker: o stream foi criado por OUTRO processo (só está no banco)."""
    client, integ_id, server, rescheduled, Session = setup
    with Session() as db:
        custom_json.create_stream(db, stream="from-db", ocsf_class_uid=2004)
        db.commit()
    # Some do registry deste processo (como se fosse um worker que nunca o viu).
    registry._REGISTRY.pop(("custom_json", "from-db"), None)
    custom_json.invalidate()
    assert registry.has("custom_json", "from-db")
    assert "from-db" in registry.supported_streams("custom_json")
