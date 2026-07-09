"""Testes do router /api/collectors/config (GET/PUT/test/audit)."""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import models
from backend.app.db.database import Base, get_session
from backend.app.main import app

try:
    import fakeredis.aioredis as _fakeredis_aio  # noqa: F401
    _FAKEREDIS_AVAILABLE = True
except ImportError:
    _FAKEREDIS_AVAILABLE = False


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


def _bootstrap_admin(client: TestClient) -> dict[str, Any]:
    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPassword123!", "display_name": "Admin"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _create_user(client: TestClient, *, username: str, password: str) -> dict[str, Any]:
    r = client.post(
        "/api/auth/users",
        json={
            "username": username,
            "password": password,
            "display_name": username.title(),
            "role": "user",
            "organization_id": None,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _login(client: TestClient, username: str, password: str) -> None:
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text


# ── GET ───────────────────────────────────────────────────────────────


def test_get_requires_admin(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    assert client.get("/api/collectors/config").status_code == 401

    _bootstrap_admin(client)
    _create_user(client, username="basic", password="BasicPass123!")

    user_client = factory()
    _login(user_client, "basic", "BasicPass123!")
    r = user_client.get("/api/collectors/config")
    assert r.status_code == 403


def test_get_returns_fallback_snapshot_when_table_empty(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.get("/api/collectors/config")
    assert r.status_code == 200, r.text
    body = r.json()
    # Fallback do env: is_persisted=False quando tabela está vazia
    assert body["is_persisted"] is False
    assert body["config_version"]
    # Sanity: shape do snapshot (valores exatos dependem do .env local)
    assert 1 <= body["wazuh_syslog_port"] <= 65535
    assert body["wazuh_dispatch_mode"] in {"syslog", "jsonl", "both"}
    assert isinstance(body["wazuh_syslog_use_tls"], bool)
    assert body["collector_batch_size"] > 0


# ── PUT ───────────────────────────────────────────────────────────────


def test_put_persists_and_flips_is_persisted(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.put(
        "/api/collectors/config",
        json={
            "wazuh_syslog_host": "wazuh.interno",
            "wazuh_syslog_port": 6514,
            "wazuh_syslog_use_tls": True,
            "collector_batch_size": 500,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_persisted"] is True
    assert body["wazuh_syslog_host"] == "wazuh.interno"
    assert body["wazuh_syslog_port"] == 6514
    assert body["wazuh_syslog_use_tls"] is True
    assert body["collector_batch_size"] == 500
    v1 = body["config_version"]
    assert v1

    # Mudança de dispatch_mode deve mudar a versão (é um campo versionado)
    r2 = client.put(
        "/api/collectors/config",
        json={"wazuh_dispatch_mode": "both"},
    )
    assert r2.status_code == 200
    assert r2.json()["config_version"] != v1


def test_put_validation_rejects_bad_rate_limits(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.put(
        "/api/collectors/config",
        json={"rate_limits_by_vendor": {"sophos": {"per_second": -5}}},
    )
    assert r.status_code == 422  # Pydantic validation


def test_put_validation_rejects_bad_domain_concurrency(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.put(
        "/api/collectors/config",
        json={"domain_concurrency_limits": {"sophos": 0}},
    )
    assert r.status_code == 422


def test_put_accepts_valid_maps(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.put(
        "/api/collectors/config",
        json={
            "rate_limits_by_vendor": {
                "sophos": {"per_second": 10, "per_minute": 400, "per_hour": 20000}
            },
            "domain_concurrency_limits": {"sophos": 20, "microsoft_defender": 30},
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["rate_limits_by_vendor"]["sophos"]["per_minute"] == 400
    assert body["domain_concurrency_limits"]["microsoft_defender"] == 30


# ── /test ─────────────────────────────────────────────────────────────


def test_test_endpoint_jsonl_only_when_mode_excludes_syslog(
    client_factory, tmp_path
) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    # Configura modo jsonl + dir temp writable
    r = client.put(
        "/api/collectors/config",
        json={
            "wazuh_dispatch_mode": "jsonl",
            "collector_jsonl_dir": str(tmp_path),
        },
    )
    assert r.status_code == 200

    r = client.post("/api/collectors/config/test")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "jsonl"
    components = [x["component"] for x in body["results"]]
    assert components == ["jsonl"]  # só jsonl — syslog não testado
    assert body["results"][0]["status"] == "healthy"


def test_test_endpoint_syslog_error_when_host_unreachable(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    # Configura host inválido + modo syslog
    r = client.put(
        "/api/collectors/config",
        json={
            "wazuh_dispatch_mode": "syslog",
            "wazuh_syslog_host": "host-que-nao-existe.invalido",
            "wazuh_syslog_port": 6514,
            "wazuh_syslog_use_tls": False,
        },
    )
    assert r.status_code == 200

    r = client.post("/api/collectors/config/test")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "syslog"
    syslog_result = next(x for x in body["results"] if x["component"] == "syslog")
    assert syslog_result["status"] == "error"
    assert "reason" in syslog_result["details"]


def test_test_endpoint_jsonl_error_on_nonwritable_dir(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    # /root/nao-existe: impossível criar
    r = client.put(
        "/api/collectors/config",
        json={
            "wazuh_dispatch_mode": "jsonl",
            "collector_jsonl_dir": "/dev/null/impossivel",
        },
    )
    assert r.status_code == 200
    r = client.post("/api/collectors/config/test")
    assert r.status_code == 200
    body = r.json()
    jsonl_result = next(x for x in body["results"] if x["component"] == "jsonl")
    assert jsonl_result["status"] == "error"


# ── GET /audit/recent (syslog_format) ─────────────────────────


def _make_audit_redis_patcher():
    """Substitui redis_async.from_url do router por FakeRedis."""
    if not _FAKEREDIS_AVAILABLE:
        pytest.skip("fakeredis[lua] não instalado")

    import fakeredis.aioredis as fakeredis_aio

    fake = fakeredis_aio.FakeRedis(decode_responses=True)
    mock_from_url = MagicMock(return_value=fake)
    patcher = patch(
        "backend.app.routers.collector_config.redis_async.from_url",
        mock_from_url,
    )
    return patcher, fake


@pytest.mark.asyncio
async def test_audit_recent_returns_syslog_format_rfc3164(client_factory) -> None:
    """Evento gravado com syslog_format='rfc3164' deve aparecer no response."""
    if not _FAKEREDIS_AVAILABLE:
        pytest.skip("fakeredis[lua] não instalado")

    from backend.app.collectors.audit_buffer import record_batch

    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    patcher, fake_redis = _make_audit_redis_patcher()

    event = {
        "id": "ev-rfc3164",
        "_centralops": {
            "integration_id": 1,
            "customer_id": 10,
            "vendor": "sophos",
            "platform": "sophos",
            "stream": "alerts",
            "collected_at": "2026-04-26T00:00:00Z",
        },
    }
    await record_batch(fake_redis, [event], 10, syslog_format="rfc3164")

    with patcher:
        # ring por tenant; admin nomeia o tenant via ?org_id.
        r = client.get("/api/collectors/config/audit/recent?limit=10&org_id=10")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1
    assert body["events"][0]["syslog_format"] == "rfc3164"


@pytest.mark.asyncio
async def test_audit_recent_legacy_entry_returns_none_syslog_format(
    client_factory,
) -> None:
    """Entrada legada (sem syslog_format no ring) → None no response."""
    if not _FAKEREDIS_AVAILABLE:
        pytest.skip("fakeredis[lua] não instalado")

    import json
    from backend.app.collectors.audit_buffer import _audit_key

    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    patcher, fake_redis = _make_audit_redis_patcher()

    # Simula entrada legada: envelope+event mas sem syslog_format
    legacy_item = json.dumps(
        {
            "envelope": {"hostname": "legacy-host", "pri": 134},
            "event": {
                "id": "legacy-ev",
                "_centralops": {
                    "integration_id": 1,
                    "customer_id": 10,
                    "vendor": "sophos",
                    "platform": "sophos",
                    "stream": "alerts",
                    "collected_at": "2026-04-26T00:00:00Z",
                },
            },
        },
        separators=(",", ":"),
    )
    await fake_redis.lpush(_audit_key(10), legacy_item)

    with patcher:
        r = client.get("/api/collectors/config/audit/recent?limit=10&org_id=10")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1
    assert body["events"][0]["syslog_format"] is None
