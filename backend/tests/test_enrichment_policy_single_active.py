"""W4.6 — UMA política de enriquecimento em vigor por organização.

O runtime sempre aplicou só a habilitada mais antiga e ignorava as demais em
silêncio ("editei e não mudou nada"). Agora: habilitar a segunda é 409 com o
nome da que está em vigor; a leitura expõe ``is_active`` pela MESMA ordenação
do runtime; e dado legado com duas habilitadas gera aviso no log do worker.

Depois do índice único parcial (``uq_enrich_policy_one_enabled``) o BANCO recusa
a segunda habilitada — inclusive sob concorrência, que o guard check-then-act do
``enable`` não cobria. "Dado legado" aqui é uma base anterior ao índice: os
testes que o simulam removem o índice primeiro (``_como_instancia_antiga``).
"""

from __future__ import annotations

import logging
import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
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


def _como_instancia_antiga(Session) -> None:
    """Base anterior ao índice único: é o único jeito de existir duas ligadas."""
    with Session() as db:
        db.execute(text("DROP INDEX IF EXISTS uq_enrich_policy_one_enabled"))
        db.commit()


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
    _como_instancia_antiga(Session)
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
    _como_instancia_antiga(Session)
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


# ── o banco garante a regra ─────────────────────────────────────────────────


def test_o_banco_recusa_a_segunda_habilitada_mesmo_burlando_a_api(env):
    """O índice é a garantia; o guard da API é só a mensagem bonita."""
    client, Session, org = env
    a = _policy_with_version(client, Session, org, "antiga")
    b = _policy_with_version(client, Session, org, "nova")
    with Session() as db:
        db.get(models.EnrichmentPolicy, a).enabled = True
        db.commit()
    with Session() as db:
        db.get(models.EnrichmentPolicy, b).enabled = True
        with pytest.raises(IntegrityError):
            db.commit()
    # Desligadas não contam: quantas o operador quiser.
    c = _policy_with_version(client, Session, org, "terceira")
    with Session() as db:
        assert db.get(models.EnrichmentPolicy, c).enabled is False


def test_corrida_no_enable_vira_409_e_nao_500(env, monkeypatch):
    """Duas requisições passam juntas pelo SELECT; o commit da segunda bate no
    índice. Simulado fazendo o SELECT "não ver" a outra na PRIMEIRA consulta —
    exatamente a janela entre ler e gravar."""
    from backend.app.routers import enrichment as router_mod

    client, Session, org = env
    a = _policy_with_version(client, Session, org, "antiga")
    b = _policy_with_version(client, Session, org, "nova")
    assert client.post(f"{_BASE}/policies/{a}/enable", params={"enabled": True}).status_code == 200

    real = router_mod._other_enabled_policy
    calls = []

    def cego_na_primeira(db, row):
        calls.append(row.id)
        return None if len(calls) == 1 else real(db, row)

    monkeypatch.setattr(router_mod, "_other_enabled_policy", cego_na_primeira)
    r = client.post(f"{_BASE}/policies/{b}/enable", params={"enabled": True})
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "enrichment.policy_already_active"
    assert "antiga" in r.json()["detail"]
    assert len(calls) == 2  # o check (cego) e a releitura depois do IntegrityError
    with Session() as db:
        assert db.get(models.EnrichmentPolicy, b).enabled is False


def test_migracao_desliga_as_sombreadas_e_mantem_a_que_rodava(env):
    """Upgrade de base com duas ligadas: nenhuma org muda de política."""
    from backend.app.db.database import _ensure_single_active_enrichment_policy

    client, Session, org = env
    a = _policy_with_version(client, Session, org, "antiga")
    b = _policy_with_version(client, Session, org, "nova")
    c = _policy_with_version(client, Session, org, "desligada")
    _como_instancia_antiga(Session)
    with Session() as db:
        for pid in (a, b):
            db.get(models.EnrichmentPolicy, pid).enabled = True
        db.commit()

    with Session() as db:
        conn = db.connection()
        assert _ensure_single_active_enrichment_policy(conn) == 1
        db.commit()
    with Session() as db:
        assert db.get(models.EnrichmentPolicy, a).enabled is True   # a que o runtime aplicava
        assert db.get(models.EnrichmentPolicy, b).enabled is False  # a sombreada
        assert db.get(models.EnrichmentPolicy, c).enabled is False
    # Idempotente, e o índice voltou a existir.
    with Session() as db:
        assert _ensure_single_active_enrichment_policy(db.connection()) == 0
        db.commit()
    with Session() as db:
        db.get(models.EnrichmentPolicy, b).enabled = True
        with pytest.raises(IntegrityError):
            db.commit()


def test_runtime_e_ui_desempatam_do_mesmo_jeito(env, monkeypatch):
    """Mesmo ``created_at``: o worker e o ``is_active`` escolhem a mesma — a de
    menor ``id``. Os ids são escolhidos para a ordem de INSERÇÃO ser a oposta
    (``zz`` entra primeiro): no SQLite, sem desempate explícito o empate cai na
    ordem física e o teste passaria por acaso."""
    client, Session, org = env
    _como_instancia_antiga(Session)
    rules = (
        '{"version": 1, "enrichment": [{"id": "%s", "enricher": "table_exact", "table": "t", '
        '"key": {"source": "normalized.src_endpoint.ip", "kind": "ip"}, '
        '"outputs": [{"from": "site", "target": "_centralops.enrichment.src.site"}]}]}'
    )
    with Session() as db:
        ts = None
        for pid in ("zz-inserida-primeiro", "aa-inserida-depois"):
            row = models.EnrichmentPolicy(id=pid, organization_id=org, name=pid, enabled=True)
            db.add(row)
            db.flush()
            ts = ts or row.created_at
            row.created_at = ts
            v = models.EnrichmentPolicyVersion(
                policy_id=pid, version_number=1, commit_message="v1", rules=rules % pid
            )
            db.add(v)
            db.flush()
            row.current_version_id = v.id
        db.commit()
    monkeypatch.setattr(settings, "ENRICHMENT_ENABLED", True)

    rows = {p["id"]: p for p in client.get(f"{_BASE}/policies", params={"organization_id": org}).json()}
    assert rows["aa-inserida-depois"]["is_active"] is True
    assert rows["zz-inserida-primeiro"]["is_active"] is False

    compiled = runtime_mod.load_policy_for_org(org)
    assert compiled is not None
    assert [r.rule_id for r in compiled.rules] == ["aa-inserida-depois"]
