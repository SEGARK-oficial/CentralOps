"""Publicar política exige que a FONTE citada exista na organização.

A tabela citada já era conferida no commit (``enrichment.table_missing``). A
fonte não: publicar uma regra que cita fonte apagada, renomeada ou de outro
tenant passava, e a regra falhava a cada ciclo com ``LookupError`` — visível só
na aba de execução. A régua agora é a mesma do runtime (``_resolve_source``):
a org enxerga a fonte se é dona OU está na lista de compartilhamento.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

from backend.app.db import models
from backend.app.db.database import Base, get_session
from backend.app.main import app

_BASE = "/api/collectors/enrichment"


@pytest.fixture()
def env(monkeypatch):
    from backend.app.core import edition

    # Compartilhar fonte é Enterprise; ligado na FUNÇÃO que o router consulta.
    monkeypatch.setattr(edition, "feature_enabled", lambda name: name == "multi_tenant")
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_session] = override
    client = TestClient(app)
    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPassword123!", "display_name": "Admin"},
    )
    assert r.status_code == 200, r.text
    yield client, Session
    client.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


def _org(client, Session, name, parent=None) -> int:
    r = client.post("/api/organizations", json={"name": name, "slug": name.lower()})
    assert r.status_code in (200, 201), r.text
    org_id = int(r.json()["id"])
    if parent is not None:
        with Session() as db:
            db.get(models.Organization, org_id).parent_organization_id = parent
            db.commit()
    return org_id


def _source(client, org, name="vt-prod", shared=(), enabled=True):
    r = client.post(
        f"{_BASE}/sources",
        json={
            "name": name,
            "enricher": "virustotal",
            "organization_id": org,
            "secret": "chave",
            "enabled": enabled,
            "shared_organization_ids": list(shared),
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _rules(source="vt-prod"):
    return {
        "version": 1,
        "enrichment": [
            {
                "id": "vt-ip",
                "enricher": "virustotal",
                "source": source,
                "key": {"source": "normalized.src_endpoint.ip", "kind": "ip"},
                "outputs": [
                    {"from": "malicious", "target": "_centralops.enrichment.src.vt_malicious"}
                ],
            }
        ],
    }


def _publish(client, org, rules):
    pid = client.post(f"{_BASE}/policies", json={"name": "p", "organization_id": org}).json()["id"]
    r = client.post(
        f"{_BASE}/policies/{pid}/versions", json={"rules": rules, "commit_message": "v1"}
    )
    return pid, r


def test_fonte_inexistente_e_422_e_nao_cria_versao(env):
    client, Session = env
    org = _org(client, Session, "Solo")
    pid, r = _publish(client, org, _rules("nao-existe"))
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "enrichment.source_missing"
    assert "nao-existe" in r.json()["detail"]
    with Session() as db:
        assert db.get(models.EnrichmentPolicy, pid).current_version_id is None


def test_fonte_propria_publica(env):
    client, Session = env
    org = _org(client, Session, "Dona")
    _source(client, org)
    _, r = _publish(client, org, _rules())
    assert r.status_code == 201, r.text


def test_fonte_desabilitada_nao_impede_publicar(env):
    """Desligar a fonte por um tempo não pode travar a edição da política."""
    client, Session = env
    org = _org(client, Session, "Pausa")
    _source(client, org, enabled=False)
    _, r = _publish(client, org, _rules())
    assert r.status_code == 201, r.text


def test_fonte_compartilhada_pela_matriz_atende_a_filha(env):
    client, Session = env
    matriz = _org(client, Session, "Matriz")
    filha = _org(client, Session, "Filha", parent=matriz)
    _source(client, matriz, shared=[filha])
    _, r = _publish(client, filha, _rules())
    assert r.status_code == 201, r.text


def test_fonte_homonima_de_outro_tenant_nao_conta(env):
    """O nome é resolvido DENTRO da org: a fonte do vizinho não satisfaz."""
    client, Session = env
    vizinho = _org(client, Session, "Vizinho")
    alvo = _org(client, Session, "Alvo")
    _source(client, vizinho)
    _, r = _publish(client, alvo, _rules())
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "enrichment.source_missing"
