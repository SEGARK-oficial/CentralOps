"""W4.6 — UMA política de enriquecimento em vigor por organização.

O runtime sempre aplicou só a habilitada mais antiga e ignorava as demais em
silêncio ("editei e não mudou nada"). Agora: habilitar a segunda é 409 com o
nome da que está em vigor; a leitura expõe ``is_active`` pela MESMA ordenação
do runtime; e dado legado com duas habilitadas gera aviso no log do worker.
"""

from __future__ import annotations

import logging
import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.collectors.enrich import runtime as runtime_mod
from backend.app.core.config import settings
from backend.app.db import database as _db_module
from backend.app.db import models
from backend.app.db.database import Base, get_session
from backend.app.main import app

_BASE = "/api/collectors/enrichment"


@pytest.fixture()
def env(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_session] = override
    monkeypatch.setattr(_db_module, "SessionLocal", Session)
    client = TestClient(app)
    r = client.post("/api/auth/bootstrap", json={"username": "admin", "password": "AdminPassword123!", "display_name": "Admin"})
    assert r.status_code == 200, r.text
    org = client.post("/api/organizations", json={"name": "AcmeP", "slug": "acmep"}).json()["id"]
    yield client, Session, org
    client.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


def _policy_with_version(client, Session, org, name):
    pid = client.post(f"{_BASE}/policies", json={"name": name, "organization_id": org}).json()["id"]
    with Session() as db:
        v = models.EnrichmentPolicyVersion(
            policy_id=pid, version_number=1, commit_message="v1",
            rules='{"version": 1, "enrichment": [{"id": "r", "enricher": "table_exact", "table": "t", '
                  '"key": {"source": "normalized.src_endpoint.ip", "kind": "ip"}, '
                  '"outputs": [{"from": "site", "target": "_centralops.enrichment.src.site"}]}]}',
        )
        db.add(v); db.flush()
        db.get(models.EnrichmentPolicy, pid).current_version_id = v.id
        db.commit()
    return pid


def test_segunda_politica_habilitada_e_409_com_o_nome_da_vigente(env):
    client, Session, org = env
    a = _policy_with_version(client, Session, org, "antiga")
    b = _policy_with_version(client, Session, org, "nova")
    assert client.post(f"{_BASE}/policies/{a}/enable", params={"enabled": True}).status_code == 200
    r = client.post(f"{_BASE}/policies/{b}/enable", params={"enabled": True})
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "enrichment.policy_already_active"
    assert "antiga" in r.json()["detail"]
    # Re-habilitar a que já está ligada não é conflito (idempotente).
    assert client.post(f"{_BASE}/policies/{a}/enable", params={"enabled": True}).status_code == 200
    # Desligar a vigente libera a outra.
    assert client.post(f"{_BASE}/policies/{a}/enable", params={"enabled": False}).status_code == 200
    assert client.post(f"{_BASE}/policies/{b}/enable", params={"enabled": True}).status_code == 200


def test_is_active_segue_a_ordenacao_do_runtime(env):
    """Dado legado: duas habilitadas. A UI tem que apontar a mesma que o worker."""
    client, Session, org = env
    a = _policy_with_version(client, Session, org, "antiga")
    b = _policy_with_version(client, Session, org, "nova")
    with Session() as db:  # burla o guard, como uma instância anterior a ele
        for pid in (a, b):
            db.get(models.EnrichmentPolicy, pid).enabled = True
        db.commit()
    rows = {p["name"]: p for p in client.get(f"{_BASE}/policies", params={"organization_id": org}).json()}
    assert rows["antiga"]["enabled"] and rows["antiga"]["is_active"] is True
    assert rows["nova"]["enabled"] and rows["nova"]["is_active"] is False


def test_runtime_avisa_quando_ha_politica_sombreada(env, caplog, monkeypatch):
    client, Session, org = env
    a = _policy_with_version(client, Session, org, "antiga")
    b = _policy_with_version(client, Session, org, "nova")
    with Session() as db:
        for pid in (a, b):
            db.get(models.EnrichmentPolicy, pid).enabled = True
        db.commit()
    monkeypatch.setattr(settings, "ENRICHMENT_ENABLED", True)
    with caplog.at_level(logging.WARNING):
        policy = runtime_mod.load_policy_for_org(org)
    assert policy is not None
    assert any(getattr(r, "event", "") == "enrich.policy_shadowed" for r in caplog.records)

    # Com uma só habilitada, silêncio.
    with Session() as db:
        db.get(models.EnrichmentPolicy, b).enabled = False
        db.commit()
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        runtime_mod.load_policy_for_org(org)
    assert not any(getattr(r, "event", "") == "enrich.policy_shadowed" for r in caplog.records)
