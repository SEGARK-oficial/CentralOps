"""Testes do router /api/collectors/config (GET/PUT/test/audit)."""

from __future__ import annotations

import os
from typing import Any

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


def test_put_persists_dedupe_ttl_seconds_round_trip(client_factory) -> None:
    """Regressão: o campo existia em Read/Base mas não em Update, então o PUT
    descartava o valor em silêncio (HTTP 200) e o TTL ficava preso no derivado
    de ``dedupe_ttl_days`` (24h). Cobre o meio da cadeia, não só as pontas.
    """
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    # 4h — o piso permitido (state/dedupe.MIN_TTL_SECONDS).
    r = client.put("/api/collectors/config", json={"dedupe_ttl_seconds": 14_400})
    assert r.status_code == 200, r.text
    assert r.json()["dedupe_ttl_seconds"] == 14_400

    # Releitura independente: prova que foi ao banco, não só ecoado.
    r2 = client.get("/api/collectors/config")
    assert r2.status_code == 200
    assert r2.json()["dedupe_ttl_seconds"] == 14_400


def test_put_rejects_dedupe_ttl_seconds_below_floor(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.put("/api/collectors/config", json={"dedupe_ttl_seconds": 3_600})
    assert r.status_code == 422


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


# ── /test REMOVIDO (ago/2026) ─────────────────────────────────────────
#
# ``POST /collectors/config/test`` sondava ``wazuh_syslog_host`` e
# ``wazuh_dispatch_mode``: campos que NENHUM formulário expõe e que NENHUM
# despachante lê desde que a saída passou a ser o sistema de Destinos
# (``collectors/output/destinations/``). Numa instalação nova esses campos são
# NULL, então o botão respondia "wazuh_syslog_host não configurado" em 100% dos
# cliques, para sempre. Quem testa o caminho de saída de verdade é
# ``POST /api/destinations/{id}/test``.


def test_test_endpoint_no_longer_exists(client_factory) -> None:
    """404, não 405: a rota sumiu inteira, não só o método.

    Trava a remoção. Se alguém reintroduzir o endpoint sem reintroduzir também
    um caminho de saída que ele de fato exercite, este teste avisa.
    """
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.post("/api/collectors/config/test")
    assert r.status_code == 404, r.text


def test_dispatch_probe_helpers_are_gone() -> None:
    """Os helpers de sondagem saíram junto — sem código morto de rede."""
    from backend.app.routers import collector_config as mod

    for name in ("_test_syslog", "_test_jsonl", "_build_probe_message"):
        assert not hasattr(mod, name), f"{name} deveria ter sido removido"
