"""Destino HTTP endpoint do Amazon Data Firehose — ``POST /api/ingest/firehose/<stream>``.

O contrato do Firehose (docs da AWS) é o que se prova aqui: o token vem no
``X-Amz-Firehose-Access-Key``; toda resposta, inclusive de erro, é JSON com o
``requestId`` do pedido e ``timestamp``; e nenhuma recusa sai como 413, porque o
Firehose trata 413 como falha permanente e não grava o lote no backup S3.
"""
from __future__ import annotations

import base64
import gzip
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import models
from backend.app.db.database import Base, get_session
from backend.app.main import app
from backend.app.routers import ingest as ingest_router

_ACCT = "111122223333"
_WAF = {
    "timestamp": 1762682400000,
    "formatVersion": 1,
    "webaclId": f"arn:aws:wafv2:us-east-1:{_ACCT}:regional/webacl/prod/abc",
    "terminatingRuleId": "AWS-AWSManagedRulesSQLiRuleSet",
    "action": "BLOCK",
    "httpRequest": {"clientIp": "198.51.100.7", "country": "BR", "uri": "/login",
                    "httpMethod": "POST", "requestId": "req-123", "headers": []},
}
_SOURCE_ARN = f"arn:aws:firehose:us-east-1:{_ACCT}:deliverystream/aws-waf-logs-centralops"


@pytest.fixture()
def setup(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_session():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_session] = override_get_session

    with TestingSession() as db:
        org = models.Organization(name="ACME", slug="acme")
        db.add(org)
        db.flush()
        fh = models.Integration(organization_id=org.id, name="AWS Firehose", platform="aws_firehose", is_active=True)
        fg = models.Integration(organization_id=org.id, name="FG edge", platform="fortinet_fortigate", is_active=True)
        db.add_all([fh, fg])
        db.commit()
        fh_id, fg_id = fh.id, fg.id

    client = TestClient(app)
    assert client.post("/api/auth/bootstrap", json={"username": "admin", "password": "AdminPass1!", "display_name": "Admin"}).status_code == 200
    assert client.post("/api/auth/login", json={"username": "admin", "password": "AdminPass1!"}).status_code == 200
    token = client.post(f"/api/ingest/integrations/{fh_id}/token").json()["token"]
    fg_token = client.post(f"/api/ingest/integrations/{fg_id}/token").json()["token"]
    yield client, token, fg_token

    client.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def produced(monkeypatch):
    """Tópico raw em memória: o que o endpoint gravou, na ordem."""
    out: list = []

    async def _produce(integration_id, stream, events, ids=None, historical=False):
        out.extend(events)
        return len(events)

    monkeypatch.setattr(ingest_router, "produce_raw_async", _produce)
    return out


def _body(*records: bytes, request_id: str = "ed4acda5-034f-9f42-bba1-f29aea6d7d8f") -> bytes:
    return json.dumps({
        "requestId": request_id,
        "timestamp": 1578090901599,
        "records": [{"data": base64.b64encode(r).decode()} for r in records],
    }).encode()


def _headers(token: str, request_id: str = "ed4acda5-034f-9f42-bba1-f29aea6d7d8f", **extra) -> dict:
    h = {
        "X-Amz-Firehose-Protocol-Version": "1.0",
        "X-Amz-Firehose-Request-Id": request_id,
        "X-Amz-Firehose-Source-Arn": _SOURCE_ARN,
        "X-Amz-Firehose-Access-Key": token,
        "Content-Type": "application/json",
    }
    h.update(extra)
    return h


def _assert_firehose_shape(r, request_id: str = "ed4acda5-034f-9f42-bba1-f29aea6d7d8f") -> dict:
    assert r.headers["content-type"].startswith("application/json")
    payload = r.json()
    assert payload["requestId"] == request_id
    assert isinstance(payload["timestamp"], int)
    return payload


def test_waf_batch_with_gzip_content_encoding_is_stored(setup, produced):
    client, token, _ = setup
    raw = _body(json.dumps(_WAF).encode() + b"\n", json.dumps(dict(_WAF, timestamp=2)).encode())
    r = client.post(
        "/api/ingest/firehose/waf",
        content=gzip.compress(raw),
        headers=_headers(token, **{"Content-Encoding": "gzip"}),
    )
    assert r.status_code == 200, r.text
    payload = _assert_firehose_shape(r)
    assert "errorMessage" not in payload
    assert len(produced) == 2
    first = produced[0]
    assert first["centralops"]["source"] == "firehose"
    assert first["centralops"]["deliveryStream"] == "aws-waf-logs-centralops"
    assert first["_ingest"]["stream"] == "waf"


def test_redelivery_carries_the_same_content_id(setup, produced):
    client, token, _ = setup
    body = _body(json.dumps(_WAF).encode())
    for _ in range(2):
        assert client.post("/api/ingest/firehose/waf", content=body, headers=_headers(token)).status_code == 200
    assert produced[0]["_ingest"]["id"] == produced[1]["_ingest"]["id"]


def test_wrong_access_key_is_401_in_firehose_shape(setup, produced):
    client, _, _ = setup
    r = client.post("/api/ingest/firehose/waf", content=_body(b"{}"), headers=_headers("coi_1_errado"))
    assert r.status_code == 401
    assert _assert_firehose_shape(r)["errorMessage"]
    assert produced == []


def test_unknown_stream_is_404_in_firehose_shape(setup, produced):
    client, token, _ = setup
    r = client.post("/api/ingest/firehose/vpc_flow", content=_body(b"{}"), headers=_headers(token))
    assert r.status_code == 404
    _assert_firehose_shape(r)


def test_body_that_is_not_a_firehose_request_is_400(setup, produced):
    client, token, _ = setup
    r = client.post("/api/ingest/firehose/waf", content=b'{"foo": 1}', headers=_headers(token))
    assert r.status_code == 400
    assert "records" in _assert_firehose_shape(r)["errorMessage"]


def test_bad_records_do_not_fail_the_batch(setup, produced):
    client, token, _ = setup
    body = json.dumps({
        "requestId": "r-2",
        "records": [{"data": "!!!"}, {"data": base64.b64encode(json.dumps(_WAF).encode()).decode()}],
    }).encode()
    r = client.post("/api/ingest/firehose/waf", content=body, headers=_headers(token, request_id="r-2"))
    assert r.status_code == 200, r.text
    _assert_firehose_shape(r, "r-2")
    assert len(produced) == 1


def test_request_id_falls_back_to_the_body(setup, produced):
    client, token, _ = setup
    h = _headers(token)
    del h["X-Amz-Firehose-Request-Id"]
    r = client.post("/api/ingest/firehose/waf", content=_body(json.dumps(_WAF).encode(), request_id="so-no-corpo"), headers=h)
    assert r.status_code == 200
    _assert_firehose_shape(r, "so-no-corpo")


def test_too_large_is_never_413(setup, produced, monkeypatch):
    """413 = falha permanente no Firehose e o lote NÃO vai para o backup S3."""
    client, token, _ = setup
    monkeypatch.setattr(ingest_router, "_FIREHOSE_MAX_EVENTS", 1)
    body = _body(json.dumps(_WAF).encode(), json.dumps(dict(_WAF, timestamp=2)).encode())
    r = client.post("/api/ingest/firehose/waf", content=body, headers=_headers(token))
    assert r.status_code == 500
    assert "buffer size" in _assert_firehose_shape(r)["errorMessage"]
    assert produced == []

    monkeypatch.setattr(ingest_router, "_MAX_BODY_BYTES", 10)
    r = client.post("/api/ingest/firehose/waf", content=body, headers=_headers(token))
    assert r.status_code == 500
    assert "buffer size" in _assert_firehose_shape(r)["errorMessage"]


def test_dataplane_down_is_retryable_503(setup, monkeypatch):
    client, token, _ = setup

    async def _down(*a, **k):
        raise ConnectionError("broker fora")

    monkeypatch.setattr(ingest_router, "produce_raw_async", _down)
    r = client.post("/api/ingest/firehose/waf", content=_body(json.dumps(_WAF).encode()), headers=_headers(token))
    assert r.status_code == 503
    assert r.headers.get("Retry-After")
    _assert_firehose_shape(r)


def test_other_push_platform_receives_generic_firehose_records(setup, produced):
    client, _, fg_token = setup
    r = client.post(
        "/api/ingest/firehose/traffic",
        content=_body(b'{"srcip": "10.0.0.1"}', b"linha de syslog"),
        headers=_headers(fg_token),
    )
    assert r.status_code == 200, r.text
    assert [e.get("srcip") or e.get("message") for e in produced] == ["10.0.0.1", "linha de syslog"]
