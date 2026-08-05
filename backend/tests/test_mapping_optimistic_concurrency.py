"""Concorrência otimista no commit de versão de mapping (``base_version_id``).

POR QUE EXISTE: a edição incremental (patch por índice) lê as regras vigentes,
calcula a mudança endereçando regras por POSIÇÃO e só então commita. Se outra
sessão promover uma versão nesse intervalo, os índices passam a apontar para
outras regras — e sem esta trava o commit sobrescreveria o trabalho alheio em
silêncio, aplicando a edição na regra errada.

``base_version_id`` é opt-in: a UI não envia e mantém o last-write-wins de
sempre. Estes testes fixam os dois caminhos.
"""

from __future__ import annotations

import json
import os
import uuid

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import models
from backend.app.db.database import Base, get_session
from backend.app.main import app


_RULES = {"preprocess": [], "rules": [{"target": "normalized.class_uid", "const": 2004}]}


@pytest.fixture()
def env():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
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
    with TestClient(app) as client:
        client.post(
            "/api/auth/bootstrap",
            json={"username": "admin", "password": "AdminPassword123!",
                  "display_name": "Admin"},
        )
        # Definição + v1 direto no banco: não há endpoint para criar definição.
        with TestingSession() as db:
            defn = models.MappingDefinition(
                id=str(uuid.uuid4()), vendor="wazuh", event_type="wazuh.detection",
                ocsf_class_uid=2004, description="teste",
            )
            db.add(defn)
            db.flush()
            v1 = models.MappingVersion(
                id=str(uuid.uuid4()), definition_id=defn.id, version_number=1,
                rules=json.dumps(_RULES), commit_message="v1", dsl_version=2,
            )
            db.add(v1)
            db.flush()
            defn.current_version_id = v1.id
            db.commit()
            ids = (defn.id, v1.id)
        yield client, ids, TestingSession
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


def _post(client, definition_id, **extra):
    body = {"rules": _RULES, "commit_message": "mudanca"}
    body.update(extra)
    return client.post(f"/api/mappings/{definition_id}/versions", json=body)


def test_commit_com_base_correta_e_aceito(env):
    client, (defn_id, v1_id), _ = env
    resp = _post(client, defn_id, base_version_id=v1_id)
    assert resp.status_code == 201, resp.text


def test_commit_com_base_obsoleta_e_recusado_com_409(env):
    client, (defn_id, v1_id), Session = env

    # Outra sessão promove uma versão nova enquanto a nossa "pensava".
    with Session() as db:
        v2 = models.MappingVersion(
            id=str(uuid.uuid4()), definition_id=defn_id, version_number=2,
            rules=json.dumps(_RULES), commit_message="v2 de outra pessoa", dsl_version=2,
        )
        db.add(v2)
        db.flush()
        db.get(models.MappingDefinition, defn_id).current_version_id = v2.id
        db.commit()

    resp = _post(client, defn_id, base_version_id=v1_id)
    assert resp.status_code == 409, resp.text
    # A mensagem precisa dizer o que fazer: repetir o commit criaria mais uma
    # versão sobre a base errada.
    assert "NÃO repita o commit" in resp.text
    assert v1_id not in resp.text or "versão atual" in resp.text


def test_409_nao_cria_versao_orfa(env):
    """A recusa tem de ser total: nada de versão pendurada sem ser promovida."""
    client, (defn_id, v1_id), Session = env
    with Session() as db:
        antes = db.query(models.MappingVersion).filter_by(definition_id=defn_id).count()

    _post(client, defn_id, base_version_id="versao-que-nao-existe")

    with Session() as db:
        depois = db.query(models.MappingVersion).filter_by(definition_id=defn_id).count()
    assert depois == antes


def test_sem_base_version_id_mantem_o_comportamento_antigo(env):
    """A UI não envia o campo — não pode passar a receber 409."""
    client, (defn_id, v1_id), Session = env
    with Session() as db:
        v2 = models.MappingVersion(
            id=str(uuid.uuid4()), definition_id=defn_id, version_number=2,
            rules=json.dumps(_RULES), commit_message="v2", dsl_version=2,
        )
        db.add(v2)
        db.flush()
        db.get(models.MappingDefinition, defn_id).current_version_id = v2.id
        db.commit()

    resp = _post(client, defn_id)  # sem base_version_id
    assert resp.status_code == 201, resp.text


def test_promocao_efetiva_aponta_para_a_versao_nova(env):
    client, (defn_id, v1_id), Session = env
    resp = _post(client, defn_id, base_version_id=v1_id)
    nova = resp.json()["id"]
    with Session() as db:
        assert db.get(models.MappingDefinition, defn_id).current_version_id == nova
