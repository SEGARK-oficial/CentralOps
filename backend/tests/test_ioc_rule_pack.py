"""Pacote IOC → Detection (W4.7): três regras inflight embarcadas, desabilitadas.

Prova (1) que as regras COMPILAM no matcher real e casam a tag certa — e só ela;
(2) instalação idempotente sem ressurreição (apagar uma regra do pacote não a
traz de volta no próximo boot); (3) que a migração instala nas orgs existentes e
o create de org instala na nova.
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.collectors.inflight import rule_pack
from backend.app.collectors.inflight.matcher import evaluate_inflight
from backend.app.collectors.inflight.runtime import compile_rule
from backend.app.db import database as _db_module
from backend.app.db import models
from backend.app.db.database import Base, get_session
from backend.app.main import app


@pytest.fixture
def fresh_engine(monkeypatch, tmp_path):
    url = f"sqlite:///{tmp_path / 't.db'}"
    engine = create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(_db_module, "engine", engine)
    monkeypatch.setattr(_db_module, "SessionLocal", Session)
    monkeypatch.setattr(_db_module, "DATABASE_URL", url)
    yield engine, Session
    Base.metadata.drop_all(bind=engine)


def _org(db, name="acme"):
    o = models.Organization(name=name, slug=name)
    db.add(o); db.flush()
    return o


def _rules(db, org_id):
    return db.query(models.CorrelationRule).filter(models.CorrelationRule.organization_id == org_id).order_by(models.CorrelationRule.template_key).all()


# ── regras: compilam e casam só a própria tag ──────────────────────────────

def test_as_tres_regras_compilam_no_matcher_real(fresh_engine):
    _, Session = fresh_engine
    with Session() as db:
        org = _org(db)
        assert rule_pack.install_pack(db, org) == 3
        rows = _rules(db, org.id)
        assert [r.template_key for r in rows] == ["ioc.c2_domain", "ioc.ip_blocklist", "ioc.malicious_hash"]
        for r in rows:
            assert r.enabled is False and r.eval_mode == "inflight" and r.emit_event is False
            compiled, reason = compile_rule(r)
            assert compiled is not None, (r.template_key, reason)


@pytest.mark.parametrize("tag,expected", [
    ("ioc:ip", "ioc.ip_blocklist"),
    ("ioc:hash", "ioc.malicious_hash"),
    ("ioc:domain", "ioc.c2_domain"),
])
def test_cada_tag_dispara_exatamente_a_sua_regra(fresh_engine, tag, expected):
    _, Session = fresh_engine
    with Session() as db:
        org = _org(db); rule_pack.install_pack(db, org)
        compiled = [compile_rule(r)[0] for r in _rules(db, org.id)]
    envelope = {
        "_centralops": {"organization_id": 1, "enrichment_tags": ["geo:BR", tag]},
        "normalized": {"src_endpoint": {"ip": "203.0.113.7"}, "device": {"hostname": "h1"}},
        "raw": {},
    }
    hit = evaluate_inflight(envelope, compiled)
    assert [r.rule_id for r in hit] == [next(r.rule_id for r in compiled if r.name.startswith("[IOC]") and _key(r) == expected)]
    # Sem a tag, nada dispara — e sem enrichment_tags também não.
    assert evaluate_inflight({"_centralops": {"enrichment_tags": ["geo:BR"]}, "normalized": {}, "raw": {}}, compiled) == ()
    assert evaluate_inflight({"_centralops": {}, "normalized": {}, "raw": {}}, compiled) == ()


def _key(compiled_rule):
    # O matcher não carrega template_key; mapeia pelo nome do pacote.
    return {
        "[IOC] Endereço em lista de bloqueio": "ioc.ip_blocklist",
        "[IOC] Hash de arquivo malicioso": "ioc.malicious_hash",
        "[IOC] Domínio de comando e controle": "ioc.c2_domain",
    }[compiled_rule.name]


def test_tags_do_pacote_nao_sao_prefixo_umas_das_outras():
    """``contains`` é substring do repr da lista: ``ioc:ip`` casaria ``ioc:ipv6``."""
    tags = [r["where"][0]["value"] for r in rule_pack.IOC_RULES]
    for a in tags:
        for b in tags:
            assert a == b or not b.startswith(a), (a, b)


# ── instalação: idempotente, sem ressurreição ──────────────────────────────

def test_instalar_duas_vezes_nao_duplica_e_apagar_nao_ressuscita(fresh_engine):
    _, Session = fresh_engine
    with Session() as db:
        org = _org(db)
        assert rule_pack.install_pack(db, org) == 3
        assert rule_pack.install_pack(db, org) == 0
        assert rule_pack.installed_packs(org) == ["ioc-v1"]
        victim = _rules(db, org.id)[0]
        db.delete(victim); db.flush()
        assert rule_pack.install_pack(db, org) == 0
        assert len(_rules(db, org.id)) == 2
        assert rule_pack.install_pack_for_all_orgs(db) == 0


def test_pacote_desconhecido_e_erro_explicito(fresh_engine):
    _, Session = fresh_engine
    with Session() as db:
        with pytest.raises(KeyError):
            rule_pack.install_pack(db, _org(db), "nao-existe")


# ── boot e create de org ───────────────────────────────────────────────────

def test_migracao_instala_nas_orgs_existentes_e_e_idempotente(fresh_engine):
    _, Session = fresh_engine
    with Session() as db:
        a = _org(db, "a"); b = _org(db, "b"); db.commit()
        a_id, b_id = a.id, b.id
    _db_module._run_lightweight_migrations()
    _db_module._run_lightweight_migrations()
    with Session() as db:
        assert len(_rules(db, a_id)) == 3 and len(_rules(db, b_id)) == 3
        assert json.loads(db.get(models.Organization, a_id).rule_packs_installed) == ["ioc-v1"]


def test_org_nova_ja_nasce_com_o_pacote(fresh_engine):
    engine, Session = fresh_engine

    def override():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_session] = override
    try:
        client = TestClient(app)
        r = client.post("/api/auth/bootstrap", json={"username": "admin", "password": "AdminPass1!", "display_name": "Admin"})
        assert r.status_code == 200, r.text
        r = client.post("/api/organizations/", json={"name": "Nova Org"})
        assert r.status_code in (200, 201), r.text
        org_id = r.json()["id"]
        with Session() as db:
            rows = _rules(db, org_id)
            assert len(rows) == 3 and all(not x.enabled for x in rows)
        client.close()
    finally:
        app.dependency_overrides.clear()
