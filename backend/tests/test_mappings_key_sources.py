"""``GET /mappings/key-sources`` — o inventário de campos para quem escreve regra.

A correlação em voo resolve ``group_by_field`` e ``where.field`` da RAIZ do
envelope: ``source.ip`` nunca resolve, o match é contado antes da falha e a
Detection nunca nasce (``inflight/runtime.py``, razão ``group_by_root``). O
seletor que impede isso já existia no enriquecimento, atrás de
``require_admin_user``; aqui ele sai com a permissão de LEITURA de mapping,
porque quem escreve regra de correlação não é necessariamente admin.

Imports usam ``backend.app.*`` (gate compilado dual-root).
"""

from __future__ import annotations

import json
import os
from typing import Any, Generator

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
from backend.app.services import key_sources as ks


@pytest.fixture()
def client_factory() -> Generator[Any, None, None]:
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
    clients: list[TestClient] = []
    try:
        yield (lambda: clients.append(c := TestClient(app)) or c), TestingSession
    finally:
        for c in clients:
            c.close()
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)


def _bootstrap_admin(client: TestClient) -> None:
    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPassword123!", "display_name": "Admin"},
    )
    assert r.status_code in (200, 201), r.text


def _seed_org(Session, name: str) -> int:
    with Session() as db:
        org = models.Organization(name=name, slug=name.lower())
        db.add(org)
        db.commit()
        db.refresh(org)
        return org.id


def _seed_mapping(Session, vendor: str, rules: list[dict]) -> None:
    """Mapping v2 com versão vigente — só o que ``mapped_normalized_paths`` lê."""
    with Session() as db:
        defn = models.MappingDefinition(
            vendor=vendor, event_type=f"{vendor}.detection", ocsf_class_uid=2004
        )
        db.add(defn)
        db.flush()
        version = models.MappingVersion(
            definition_id=defn.id,
            version_number=1,
            rules=json.dumps({"preprocess": [], "rules": rules}),
            commit_message="seed",
        )
        db.add(version)
        db.flush()
        defn.current_version_id = version.id
        db.commit()


def _seed_integration(Session, org_id: int, platform: str, active: bool = True) -> None:
    with Session() as db:
        db.add(
            models.Integration(
                name=f"{platform}-{org_id}",
                platform=platform,
                organization_id=org_id,
                is_active=active,
            )
        )
        db.commit()


_RULES = [
    {"target": "normalized.class_uid", "const": 2004},  # constante: não é chave
    {"target": "normalized.src_endpoint.ip", "source": "src.ip"},
    {"target": "normalized.device.hostname", "source": "host"},
]


def test_org_com_integracao_ativa_recebe_o_inventario_real(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)
    org = _seed_org(Session, "Acme")
    _seed_mapping(Session, "sophos", _RULES)
    _seed_integration(Session, org, "sophos")

    r = client.get("/api/mappings/key-sources", params={"organization_id": org})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["organization_id"] == org
    assert body["from_active_mappings"] is True
    assert body["roots"] == ["_centralops", "normalized", "raw"]
    mapped = [s for s in body["suggestions"] if s["kind"] == "mapped"]
    paths = [s["path"] for s in mapped]
    # Os dois "parecem chave" (mesma relevância, mesma popularidade) ⇒ ordem
    # alfabética, a MESMA do enriquecimento; a constante do Base Event não entra.
    assert paths == ["normalized.device.hostname", "normalized.src_endpoint.ip"]
    assert "normalized.class_uid" not in paths
    assert mapped[0]["vendors"] == ["sophos"] and mapped[0]["rule_count"] == 1


def test_rotulos_do_envelope_vem_sempre_e_por_ultimo(client_factory) -> None:
    """``_centralops.*`` é o único bloco fora de ``normalized`` que uma regra
    costuma referenciar — e vem da MESMA allowlist do roteamento, não de uma
    segunda lista à mão."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)
    org = _seed_org(Session, "Acme")

    r = client.get("/api/mappings/key-sources", params={"organization_id": org})
    body = r.json()
    kinds = [s["kind"] for s in body["suggestions"]]
    assert "envelope" in kinds
    assert kinds.index("envelope") > kinds.index("catalog")
    envelope = [s["path"] for s in body["suggestions"] if s["kind"] == "envelope"]
    assert "_centralops.vendor" in envelope
    assert "_centralops.detection_matched" in envelope
    assert envelope == list(ks.ENVELOPE_LABEL_PATHS)


def test_org_sem_integracao_recebe_o_catalogo_e_diz_que_e_catalogo(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)
    org = _seed_org(Session, "Nova")
    _seed_mapping(Session, "sophos", _RULES)  # existe no catálogo, mas a org não conectou

    r = client.get("/api/mappings/key-sources", params={"organization_id": org})
    body = r.json()
    assert body["from_active_mappings"] is False
    catalog = [s["path"] for s in body["suggestions"] if s["kind"] == "catalog"]
    assert catalog == list(ks.COMMON_OCSF_KEY_PATHS)
    assert not [s for s in body["suggestions"] if s["kind"] == "mapped"]


def test_inventario_nao_vaza_entre_organizacoes(client_factory) -> None:
    """A org B conectou Okta; a org A, nada. O inventário de A não pode conter o
    que só B produz — seria oferecer campo que nunca vai existir nos dados dela."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)
    org_a = _seed_org(Session, "OrgA")
    org_b = _seed_org(Session, "OrgB")
    _seed_mapping(Session, "okta", [{"target": "normalized.actor.user.name", "source": "actor"}])
    _seed_integration(Session, org_b, "okta")

    a = client.get("/api/mappings/key-sources", params={"organization_id": org_a}).json()
    b = client.get("/api/mappings/key-sources", params={"organization_id": org_b}).json()
    assert a["from_active_mappings"] is False
    assert b["from_active_mappings"] is True
    assert [s["path"] for s in b["suggestions"] if s["kind"] == "mapped"] == [
        "normalized.actor.user.name"
    ]


def test_global_sem_org_e_422_nao_500(client_factory) -> None:
    """O inventário é por org; não existe "inventário de todas"."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    r = client.get("/api/mappings/key-sources")
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "mapping.organization_required"


def test_servico_e_a_unica_fonte_para_os_dois_routers() -> None:
    """O router de enriquecimento reexporta os nomes privados que os testes
    dele importam — mas o corpo vive no serviço. Se alguém recriar a lista no
    router, este teste fica vermelho."""
    from backend.app.routers import enrichment

    assert enrichment._COMMON_OCSF_KEY_PATHS is ks.COMMON_OCSF_KEY_PATHS
    assert enrichment._key_relevance is ks.key_relevance
    assert enrichment._rule_can_be_key is ks.rule_can_be_key
    assert enrichment._mapped_normalized_paths is ks.mapped_normalized_paths
