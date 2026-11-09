"""Teste de integração do endpoint de ingestão push.

Exercita o fluxo HTTP completo: admin emite token → edge-collector faz POST com o
token → eventos vão para o tópico ``raw`` do data-plane Kafka (``produce_raw_async``
trocado por um gravador). O buffer Redis de antes saiu na ADR-0018 Onda 5: o 200
só sai depois do ack do raw, e broker fora é 503 com Retry-After. Cobre também os
caminhos de erro (sem token, plataforma não-push, stream desconhecido).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db.database import Base, get_session
from backend.app.db import models
from backend.app.main import app
from backend.app.routers import ingest as ingest_router


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

    # Tópico raw em memória: cada produce grava (integration_id, stream, eventos).
    # O router importa ``produce_raw_async`` pelo nome, então o patch vale no
    # módulo do router (o que ele chama) e na origem (quem importar depois).
    produced: list = []

    async def _produce(integration_id, stream, events, ids=None, historical=False):
        produced.append((integration_id, stream, list(events)))
        return len(events)

    monkeypatch.setattr(ingest_router, "produce_raw_async", _produce)
    monkeypatch.setattr(
        "backend.app.collectors.dataplane.raw.produce_raw_async", _produce
    )

    # Seed org + integração push (FortiGate) + uma pull (sophos) para o caminho 422.
    with TestingSession() as db:
        org = models.Organization(name="ACME", slug="acme")
        db.add(org)
        db.flush()
        push_integ = models.Integration(organization_id=org.id, name="FG edge", platform="fortinet_fortigate", is_active=True)
        pull_integ = models.Integration(organization_id=org.id, name="Sophos", platform="sophos", is_active=True)
        db.add_all([push_integ, pull_integ])
        db.commit()
        push_id, pull_id = push_integ.id, pull_integ.id

    client = TestClient(app)
    # bootstrap admin + login (cookie session)
    r = client.post("/api/auth/bootstrap", json={"username": "admin", "password": "AdminPass1!", "display_name": "Admin"})
    assert r.status_code == 200, r.text
    r = client.post("/api/auth/login", json={"username": "admin", "password": "AdminPass1!"})
    assert r.status_code == 200, r.text

    yield client, push_id, pull_id, produced

    client.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


def test_issue_token_and_ingest_flow(setup):
    client, push_id, _pull_id, produced = setup

    # admin emite token
    r = client.post(f"/api/ingest/integrations/{push_id}/token")
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    assert token.startswith(f"coi_{push_id}_")
    assert r.json()["endpoint"] == "/api/ingest"

    # edge-collector envia NDJSON com o token (Bearer)
    body = '{"srcip":"10.0.0.1","action":"deny"}\n{"srcip":"10.0.0.2","action":"accept"}'
    r = client.post(
        "/api/ingest/traffic",
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/x-ndjson"},
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["accepted"] == 2
    # Sem buffer Redis: a resposta não tem mais ``dropped``/``buffer_depth``.
    assert "dropped" not in payload and "buffer_depth" not in payload
    # Os dois eventos foram ao tópico raw, na ordem, para a integração/stream certos.
    assert len(produced) == 1
    iid, stream, events = produced[0]
    assert (iid, stream) == (push_id, "traffic")
    assert [e["srcip"] for e in events] == ["10.0.0.1", "10.0.0.2"]

    # info reflete o token
    r = client.get(f"/api/ingest/integrations/{push_id}")
    assert r.status_code == 200, r.text
    info = r.json()
    assert info["transport"] == "push" and info["has_token"] is True
    assert "traffic" in info["streams"]
    assert "buffer_depth" not in info


def test_ingest_requires_valid_token(setup):
    client, push_id, _pull_id, _raw = setup
    # sem token → 401
    r = client.post("/api/ingest/traffic", data="{}", headers={"Content-Type": "application/json"})
    assert r.status_code == 401
    # token inválido → 401
    r = client.post(
        "/api/ingest/traffic", data="{}",
        headers={"Authorization": "Bearer coi_999_deadbeef", "Content-Type": "application/json"},
    )
    assert r.status_code == 401


def test_unknown_stream_rejected(setup):
    client, push_id, _pull_id, _raw = setup
    token = client.post(f"/api/ingest/integrations/{push_id}/token").json()["token"]
    r = client.post(
        "/api/ingest/nonexistent",
        data='{"a":1}',
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    assert r.status_code == 404


def test_pull_platform_has_no_ingest_token(setup):
    client, _push_id, pull_id, _raw = setup
    # emitir token para integração pull (sophos) → 422
    r = client.post(f"/api/ingest/integrations/{pull_id}/token")
    assert r.status_code == 422


def test_load_push_integration_enforces_org_scope():
    """Hardening multi-tenant (review): um usuário escopado a OUTRA org não
    consegue carregar a integração (403), mesmo push. Admin global passa. Mesmo
    padrão de integrations.py/destinations.py."""
    from fastapi import HTTPException
    from backend.app.routers.ingest import _load_push_integration

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    with TestingSession() as db:
        org = models.Organization(name="OrgA", slug="org-a")
        db.add(org)
        db.flush()
        integ = models.Integration(organization_id=org.id, name="FG", platform="fortinet_fortigate", is_active=True)
        db.add(integ)
        db.commit()
        iid, oid = integ.id, org.id

        scoped = models.AppUser(username="u", role="viewer", organization_id=oid + 999)
        scoped.is_global = False
        with pytest.raises(HTTPException) as exc:
            _load_push_integration(db, iid, scoped)
        assert exc.value.status_code == 403

        admin = models.AppUser(username="a", role="admin", organization_id=None)
        assert _load_push_integration(db, iid, admin).id == iid
    Base.metadata.drop_all(bind=engine)


def test_revoke_token_invalidates_ingest(setup):
    """Revogar o token mata um token vazado SEM rotacionar: o POST passa a 401."""
    client, push_id, _pull_id, _raw = setup
    token = client.post(f"/api/ingest/integrations/{push_id}/token").json()["token"]
    r = client.post("/api/ingest/traffic", data='{"srcip":"1.2.3.4"}',
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    assert r.status_code == 200, r.text
    assert client.delete(f"/api/ingest/integrations/{push_id}/token").status_code == 204
    r = client.post("/api/ingest/traffic", data='{"srcip":"1.2.3.4"}',
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    assert r.status_code == 401
    # segunda revogação → 404 (nada ativo)
    assert client.delete(f"/api/ingest/integrations/{push_id}/token").status_code == 404


def test_rate_limit_returns_429(setup, monkeypatch):
    """Excedido o rate-limit por token → 429 com Retry-After."""
    client, push_id, _pull_id, _raw = setup
    token = client.post(f"/api/ingest/integrations/{push_id}/token").json()["token"]

    monkeypatch.setattr(ingest_router.ingest_rate_limiter, "check", lambda _id: 7)
    r = client.post("/api/ingest/traffic", data='{"srcip":"1.2.3.4"}',
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    assert r.status_code == 429
    assert r.headers.get("Retry-After") == "7"


def test_ndjson_partial_success_207(setup):
    """Uma linha ruim no meio do NDJSON NÃO derruba o lote (modelo Cribl/Vector): as
    linhas válidas são aceitas, a inválida é contada e o status vira 207 com contadores."""
    client, push_id, _pull_id, produced = setup
    token = client.post(f"/api/ingest/integrations/{push_id}/token").json()["token"]
    body = (
        '{"srcip":"10.0.0.1","action":"deny"}\n'
        "isto não é json {{{\n"                       # linha malformada → parse_error
        '{"srcip":"10.0.0.2","action":"accept"}'
    )
    r = client.post(
        "/api/ingest/traffic", data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/x-ndjson"},
    )
    assert r.status_code == 207, r.text
    p = r.json()
    assert p["accepted"] == 2          # as duas linhas boas passaram
    assert p["parse_errors"] == 1      # a linha ruim foi contada, não abortou o lote
    # Só as duas linhas boas chegaram ao tópico raw.
    assert [e["srcip"] for _iid, _st, evs in produced for e in evs] == ["10.0.0.1", "10.0.0.2"]
    assert p["error_detail"]           # amostra do 1º erro presente


def test_ndjson_non_object_counted_as_type_error(setup):
    """Item JSON válido mas não-objeto (número/string solto) é rejeitado como type_error
    (não tem chaves p/ mapear no drift) — o resto do lote sobrevive, status 207."""
    client, push_id, _pull_id, _raw = setup
    token = client.post(f"/api/ingest/integrations/{push_id}/token").json()["token"]
    body = '{"srcip":"10.0.0.1"}\n42\n"apenas uma string"'
    r = client.post(
        "/api/ingest/traffic", data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/x-ndjson"},
    )
    assert r.status_code == 207, r.text
    p = r.json()
    assert p["accepted"] == 1
    assert p["type_errors"] == 2


def test_oversized_event_dropped_batch_survives(setup):
    """Um evento individual acima do teto por-evento é dropado+contado; o lote segue."""
    from backend.app.routers import ingest as ing

    client, push_id, _pull_id, _raw = setup
    token = client.post(f"/api/ingest/integrations/{push_id}/token").json()["token"]
    big = "x" * (ing._MAX_EVENT_BYTES + 10)
    body = '{"srcip":"10.0.0.1"}\n' + f'{{"blob":"{big}"}}'
    r = client.post(
        "/api/ingest/traffic", data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/x-ndjson"},
    )
    assert r.status_code == 207, r.text
    p = r.json()
    assert p["accepted"] == 1
    assert p["oversized"] == 1


def test_single_json_malformed_still_400(setup):
    """JSON ÚNICO (não-NDJSON) irrecuperável continua 400 — não há sucesso parcial possível."""
    client, push_id, _pull_id, _raw = setup
    token = client.post(f"/api/ingest/integrations/{push_id}/token").json()["token"]
    r = client.post(
        "/api/ingest/traffic", data="{not valid json",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    assert r.status_code == 400


def test_clean_batch_stays_200(setup):
    """Sem rejeições → 200 puro, contadores zerados (backward-compat)."""
    client, push_id, _pull_id, _raw = setup
    token = client.post(f"/api/ingest/integrations/{push_id}/token").json()["token"]
    r = client.post(
        "/api/ingest/traffic",
        data='{"srcip":"10.0.0.1"}\n{"srcip":"10.0.0.2"}',
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/x-ndjson"},
    )
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["accepted"] == 2
    assert p["parse_errors"] == 0 and p["type_errors"] == 0 and p["oversized"] == 0
    assert p["error_detail"] is None


# ── ADR-0018 Onda 2: o 200 só sai depois do ack do tópico raw ─────────────────


def _post_ndjson(client, token, body='{"a":1}\n{"a":2}'):
    return client.post(
        "/api/ingest/traffic",
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/x-ndjson"},
    )


def test_ingest_acks_after_durable_write(setup, monkeypatch):
    """O 200 sai DEPOIS do produce no raw (o ack do broker é o retorno de
    ``produce_raw_async``) e devolve o que o data-plane aceitou."""
    client, push_id, _pull_id, _produced = setup
    token = client.post(f"/api/ingest/integrations/{push_id}/token").json()["token"]
    produced: list = []

    async def _produce(iid, stream, events, ids=None, historical=False):
        produced.append((iid, stream, [e["a"] for e in events], historical))
        return len(events)

    monkeypatch.setattr(ingest_router, "produce_raw_async", _produce)

    r = _post_ndjson(client, token)
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] == 2
    # Push ao vivo nunca é histórico (backfill tem caminho próprio).
    assert produced == [(push_id, "traffic", [1, 2], False)]


def test_ingest_kafka_down_returns_503(setup, monkeypatch):
    """Broker fora → 503 explícito com Retry-After (a borda re-tenta com o buffer
    de disco dela), nunca 500 nem um 200 de evento não gravado."""
    client, push_id, _pull_id, _produced = setup
    token = client.post(f"/api/ingest/integrations/{push_id}/token").json()["token"]

    async def _down(*a, **k):
        raise ConnectionError("KafkaConnectionError: broker fora")

    monkeypatch.setattr(ingest_router, "produce_raw_async", _down)
    r = _post_ndjson(client, token)
    assert r.status_code == 503
    assert r.headers.get("Retry-After")


# ── ADR-0018 Onda 2b: Splunk HEC e OTLP/HTTP no mesmo caminho durável ─────────


def _raw_events(produced):
    """Achata o que o gravador do fixture recebeu em uma lista de eventos."""
    return [e for _iid, _stream, events in produced for e in events]


def test_hec_event_concatenated_with_splunk_auth(setup, monkeypatch):
    client, push_id, _pull_id, raw = setup
    token = client.post(f"/api/ingest/integrations/{push_id}/token").json()["token"]

    body = (
        '{"time": 1700000000, "host": "fw01", "sourcetype": "fortigate", "event": {"srcip": "10.0.0.1"}}'
        '{"event": "linha de texto"}\n'
        '{"event": {"srcip": "10.0.0.2"}, "fields": {"site": "a"}}'
    )
    r = client.post(
        "/api/ingest/hec/traffic/services/collector/event",
        data=body, headers={"Authorization": f"Splunk {token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"text": "Success", "code": 0}
    produced = _raw_events(raw)
    assert [e.get("srcip") or e.get("message") for e in produced] == ["10.0.0.1", "linha de texto", "10.0.0.2"]
    assert produced[0]["_hec"]["host"] == "fw01"
    assert produced[2]["_hec"]["fields"] == {"site": "a"}
    assert all("_ingest" in e for e in produced)


def test_hec_raw_and_health(setup, monkeypatch):
    client, push_id, _pull_id, raw = setup
    token = client.post(f"/api/ingest/integrations/{push_id}/token").json()["token"]

    r = client.post(
        "/api/ingest/hec/traffic/services/collector/raw",
        data="linha 1\nlinha 2\n", headers={"Authorization": f"Splunk {token}"},
    )
    assert r.status_code == 200, r.text
    produced = _raw_events(raw)
    assert [e["message"] for e in produced] == ["linha 1", "linha 2"]
    assert client.get("/api/ingest/hec/traffic/services/collector/health/1.0").json()["code"] == 17


def test_hec_requires_token_and_returns_503_when_not_stored(setup, monkeypatch):
    client, push_id, _pull_id, _raw = setup
    token = client.post(f"/api/ingest/integrations/{push_id}/token").json()["token"]
    assert client.post("/api/ingest/hec/traffic/services/collector/event", data='{"event":1}').status_code == 401

    async def _down(*a, **k):
        raise ConnectionError("broker fora")

    monkeypatch.setattr(ingest_router, "produce_raw_async", _down)
    r = client.post(
        "/api/ingest/hec/traffic/services/collector/event",
        data='{"event":{"a":1}}', headers={"Authorization": f"Splunk {token}"},
    )
    assert r.status_code == 503 and r.headers.get("Retry-After")


def _otlp_doc():
    return {
        "resourceLogs": [{
            "resource": {"attributes": [{"key": "host.name", "value": {"stringValue": "srv1"}}]},
            "scopeLogs": [{
                "scope": {"name": "fluentbit"},
                "logRecords": [
                    {
                        "timeUnixNano": "1700000000000000000",
                        "severityText": "WARN",
                        "body": {"kvlistValue": {"values": [
                            {"key": "EventID", "value": {"intValue": "4625"}},
                            {"key": "user", "value": {"stringValue": "alice"}},
                        ]}},
                        "attributes": [{"key": "channel", "value": {"stringValue": "Security"}}],
                    },
                    {"body": {"stringValue": "texto livre"}},
                ],
            }],
        }]
    }


def test_otlp_json_gzip(setup, monkeypatch):
    import gzip as _gzip
    import json as _json

    client, push_id, _pull_id, raw = setup
    token = client.post(f"/api/ingest/integrations/{push_id}/token").json()["token"]

    r = client.post(
        "/api/ingest/otlp/traffic/v1/logs",
        content=_gzip.compress(_json.dumps(_otlp_doc()).encode()),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json() == {}
    produced = _raw_events(raw)
    first, second = produced
    assert first["EventID"] == 4625 and first["user"] == "alice"
    assert first["_otlp"]["resource"] == {"host.name": "srv1"}
    assert first["_otlp"]["attributes"] == {"channel": "Security"}
    assert first["_otlp"]["severity_text"] == "WARN"
    assert second["message"] == "texto livre"


def test_otlp_protobuf(setup, monkeypatch):
    import importlib.util

    client, push_id, _pull_id, raw = setup
    token = client.post(f"/api/ingest/integrations/{push_id}/token").json()["token"]
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/x-protobuf"}

    if importlib.util.find_spec("opentelemetry.proto") is None:
        # Imagem sem o opentelemetry-proto: recusa clara, nunca um 200 vazio.
        r = client.post("/api/ingest/otlp/traffic/v1/logs", content=b"\x0a\x00", headers=headers)
        assert r.status_code == 415
        return

    from google.protobuf import json_format
    from opentelemetry.proto.collector.logs.v1 import logs_service_pb2

    msg = json_format.ParseDict(_otlp_doc(), logs_service_pb2.ExportLogsServiceRequest())
    r = client.post("/api/ingest/otlp/traffic/v1/logs", content=msg.SerializeToString(), headers=headers)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/x-protobuf")
    produced = _raw_events(raw)
    assert [e.get("user") or e.get("message") for e in produced] == ["alice", "texto livre"]


def test_gzip_bomb_is_refused(setup, monkeypatch):
    import gzip as _gzip

    client, push_id, _pull_id, raw = setup
    token = client.post(f"/api/ingest/integrations/{push_id}/token").json()["token"]

    bomb = _gzip.compress(b"{" + b" " * (17 * 1024 * 1024) + b"}")  # 17 MiB descompactado
    r = client.post(
        "/api/ingest/traffic", content=bomb,
        headers={"Authorization": f"Bearer {token}", "Content-Encoding": "gzip", "Content-Type": "application/json"},
    )
    assert r.status_code == 413
    assert raw == []
