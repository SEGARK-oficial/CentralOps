"""Teste de integração do endpoint de ingestão push.

Exercita o fluxo HTTP completo: admin emite token → edge-collector faz POST com o
token → eventos vão para o buffer Redis (fakeredis). Cobre os caminhos de erro
(sem token, plataforma não-push, stream desconhecido).
"""
from __future__ import annotations

import fakeredis.aioredis
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
def setup():
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

    # Redis falso compartilhado (server único → dados persistem entre clientes).
    server = fakeredis.aioredis.FakeRedis(decode_responses=True)

    async def fake_redis_client():
        return server

    # neutraliza o aclose() do endpoint para o server seguir vivo nas asserts
    orig_aclose = server.aclose

    async def _noop_aclose(*a, **k):
        return None

    server.aclose = _noop_aclose
    ingest_router._redis_client = fake_redis_client

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

    yield client, push_id, pull_id, server

    client.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


def test_issue_token_and_ingest_flow(setup):
    client, push_id, _pull_id, server = setup

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
    assert payload["dropped"] == 0
    assert payload["buffer_depth"] == 2

    # info reflete token + profundidade
    r = client.get(f"/api/ingest/integrations/{push_id}")
    assert r.status_code == 200, r.text
    info = r.json()
    assert info["transport"] == "push" and info["has_token"] is True
    assert "traffic" in info["streams"]


def test_ingest_requires_valid_token(setup):
    client, push_id, _pull_id, _server = setup
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
    client, push_id, _pull_id, _server = setup
    token = client.post(f"/api/ingest/integrations/{push_id}/token").json()["token"]
    r = client.post(
        "/api/ingest/nonexistent",
        data='{"a":1}',
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    assert r.status_code == 404


def test_pull_platform_has_no_ingest_token(setup):
    client, _push_id, pull_id, _server = setup
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
    client, push_id, _pull_id, _server = setup
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


def test_redis_unavailable_returns_503(setup, monkeypatch):
    """Redis indisponível no buffer → 503 explícito (edge re-tenta), não 500."""
    from redis.exceptions import RedisError

    client, push_id, _pull_id, _server = setup
    token = client.post(f"/api/ingest/integrations/{push_id}/token").json()["token"]

    async def _boom(*a, **k):
        raise RedisError("redis down")

    monkeypatch.setattr(ingest_router, "push_events", _boom)
    r = client.post("/api/ingest/traffic", data='{"srcip":"1.2.3.4"}',
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    assert r.status_code == 503


def test_rate_limit_returns_429(setup, monkeypatch):
    """Excedido o rate-limit por token → 429 com Retry-After."""
    client, push_id, _pull_id, _server = setup
    token = client.post(f"/api/ingest/integrations/{push_id}/token").json()["token"]

    monkeypatch.setattr(ingest_router.ingest_rate_limiter, "check", lambda _id: 7)
    r = client.post("/api/ingest/traffic", data='{"srcip":"1.2.3.4"}',
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    assert r.status_code == 429
    assert r.headers.get("Retry-After") == "7"


def test_ndjson_partial_success_207(setup):
    """Uma linha ruim no meio do NDJSON NÃO derruba o lote (modelo Cribl/Vector): as
    linhas válidas são aceitas, a inválida é contada e o status vira 207 com contadores."""
    client, push_id, _pull_id, _server = setup
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
    assert p["buffer_depth"] == 2
    assert p["error_detail"]           # amostra do 1º erro presente


def test_ndjson_non_object_counted_as_type_error(setup):
    """Item JSON válido mas não-objeto (número/string solto) é rejeitado como type_error
    (não tem chaves p/ mapear no drift) — o resto do lote sobrevive, status 207."""
    client, push_id, _pull_id, _server = setup
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

    client, push_id, _pull_id, _server = setup
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
    client, push_id, _pull_id, _server = setup
    token = client.post(f"/api/ingest/integrations/{push_id}/token").json()["token"]
    r = client.post(
        "/api/ingest/traffic", data="{not valid json",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    assert r.status_code == 400


def test_clean_batch_stays_200(setup):
    """Sem rejeições → 200 puro, contadores zerados (backward-compat)."""
    client, push_id, _pull_id, _server = setup
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
