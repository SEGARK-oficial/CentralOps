"""Probes liveness/readiness."""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.routers import health


client = TestClient(app)


def test_livez_is_dependency_free_200():
    """Liveness nunca toca DB/Redis e sempre responde 200 com o processo vivo."""
    resp = client.get("/livez")
    assert resp.status_code == 200
    assert resp.json() == {"status": "alive"}


def test_livez_is_public_and_unaudited():
    """Probe é pública (sem sessão) e fora de /api (não passa pelo audit)."""
    resp = client.get("/livez")
    assert resp.status_code == 200


def test_readyz_ok_when_db_reachable_redis_unset():
    """Sem REDIS_URL no ambiente de teste, readiness depende só do DB (ok)."""
    resp = client.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["checks"]["db"] == "ok"
    assert body["checks"]["redis"] == "ok"  # skip = ok quando REDIS_URL vazio


def test_readyz_503_when_db_down(monkeypatch):
    """DB inalcançável → 503 (tira a réplica do LB), sem derrubar o processo."""

    def _boom() -> None:
        raise RuntimeError("db unreachable")

    monkeypatch.setattr(health, "_ping_db", _boom)
    resp = client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["db"].startswith("error:")


def test_readyz_503_when_redis_down(monkeypatch):
    """Redis configurado mas inalcançável → 503."""

    async def _boom() -> None:
        raise RuntimeError("redis unreachable")

    monkeypatch.setattr(health, "_ping_redis", _boom)
    resp = client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["checks"]["redis"].startswith("error:")


def test_readyz_503_when_edition_misconfigured(monkeypatch):
    """Fail-closed: licença paga concede multi_tenant/reseller mas o pacote
    centralops_ee não ativou (scope resolver ausente) → /readyz 503, para o pod NÃO
    receber tráfego e servir multi-tenant em silêncio como FLAT."""
    from backend.app.core import edition

    monkeypatch.setattr(
        edition, "enterprise_integrity_problem",
        lambda: "licença concede multi_tenant, mas o scope resolver do EE não está registrado",
    )
    resp = client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["edition"].startswith("misconfigured:")


def test_readyz_ok_when_edition_healthy(monkeypatch):
    """Community (ou EE bem-configurado) → sem problema de integridade → 200."""
    from backend.app.core import edition

    monkeypatch.setattr(edition, "enterprise_integrity_problem", lambda: None)
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert "edition" not in resp.json()["checks"]  # só aparece quando mal-configurado
