"""API de fontes syslog (W3.2/W3.3): a validação que o receptor não pode fazer
a tempo acontece na ESCRITA — CIDR aberto, stream inexistente, JMESPath
inválido, integração pull — e o isolamento entre organizações vale como nas
demais rotas de integração.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import models
from backend.app.db.database import Base, get_session
from backend.app.main import app

_BASE = "/api/syslog"


@pytest.fixture()
def setup():
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
    with Session() as db:
        org = models.Organization(name="ACME", slug="acme"); db.add(org); db.flush()
        push = models.Integration(organization_id=org.id, name="FG", platform="fortinet_fortigate", is_active=True)
        pull = models.Integration(organization_id=org.id, name="Sophos", platform="sophos", is_active=True)
        db.add_all([push, pull]); db.commit()
        push_id, pull_id = push.id, pull.id
    client = TestClient(app)
    r = client.post("/api/auth/bootstrap", json={"username": "admin", "password": "AdminPass1!", "display_name": "Admin"})
    assert r.status_code == 200, r.text
    yield client, push_id, pull_id, Session
    client.close(); app.dependency_overrides.clear(); Base.metadata.drop_all(bind=engine)


def _body(push_id, **over):
    b = {"integration_id": push_id, "name": "fw-edge", "source_cidr": "10.0.5.7/32", "default_stream": "traffic"}
    b.update(over)
    return b


def test_crud_completo(setup):
    client, push_id, _, _ = setup
    r = client.post(f"{_BASE}/sources", json=_body(push_id, classifier={"rules": [{"when": "@fortigate", "stream": "traffic"}]}))
    assert r.status_code == 201, r.text
    src = r.json()
    assert src["platform"] == "fortinet_fortigate" and src["source_cidr"] == "10.0.5.7/32" and src["transport"] == "any"
    assert src["classifier"]["rules"][0]["when"] == "@fortigate"

    assert [s["id"] for s in client.get(f"{_BASE}/sources", params={"integration_id": push_id}).json()] == [src["id"]]

    r = client.patch(f"{_BASE}/sources/{src['id']}", json={"source_cidr": "10.0.5.0/24", "listen_port": 1514, "transport": "tcp", "enabled": False})
    assert r.status_code == 200, r.text
    assert (r.json()["source_cidr"], r.json()["listen_port"], r.json()["transport"], r.json()["enabled"]) == ("10.0.5.0/24", 1514, "tcp", False)
    r = client.patch(f"{_BASE}/sources/{src['id']}", json={"listen_port": 0})
    assert r.json()["listen_port"] is None  # 0 = qualquer porta

    assert client.delete(f"{_BASE}/sources/{src['id']}").status_code == 204
    assert client.get(f"{_BASE}/sources", params={"integration_id": push_id}).json() == []
    assert client.delete(f"{_BASE}/sources/{src['id']}").status_code == 404


@pytest.mark.parametrize("over,code", [
    ({"source_cidr": "0.0.0.0/0"}, "syslog.open_cidr"),
    ({"source_cidr": "::/0"}, "syslog.open_cidr"),
    ({"source_cidr": "10.0.5.7/99"}, "syslog.invalid_cidr"),
    ({"source_cidr": "banana"}, "syslog.invalid_cidr"),
    ({"transport": "sctp"}, "syslog.invalid_transport"),
    ({"default_stream": "nao-existe"}, "syslog.unknown_stream"),
    ({"classifier": {"rules": [{"when": "@fortigate", "stream": "outro"}]}}, "syslog.unknown_stream"),
    ({"classifier": {"rules": [{"when": "msg ==", "stream": "traffic"}]}}, "syslog.invalid_classifier"),
    ({"classifier": {"rules": [{"when": "@xpto", "stream": "traffic"}]}}, "syslog.invalid_classifier"),
])
def test_validacao_na_escrita(setup, over, code):
    client, push_id, _, _ = setup
    r = client.post(f"{_BASE}/sources", json=_body(push_id, **over))
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == code


def test_integracao_pull_nao_recebe_syslog(setup):
    client, _, pull_id, _ = setup
    r = client.post(f"{_BASE}/sources", json=_body(pull_id))
    assert r.status_code == 422 and r.json()["error"]["code"] == "syslog.not_push_integration"
    assert client.post(f"{_BASE}/sources", json=_body(99999)).status_code == 404


def test_catalogo_de_detectores_e_teste_de_linha(setup):
    client, *_ = setup
    names = {d["name"] for d in client.get(f"{_BASE}/classifiers").json()}
    assert {"fortigate", "paloalto", "cisco_asa", "pfsense", "linux_auth", "windows_vector"} <= names
    r = client.post(f"{_BASE}/classify-test", json={
        "line": "<166>Sep  6 01:02:03 asa-01 %ASA-6-302013: Built inbound TCP connection",
        "classifier": {"rules": [{"when": "@fortigate", "stream": "traffic"}, {"when": "@cisco_asa", "stream": "asa"}]},
        "default_stream": "other",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stream"] == "asa" and body["matched_rule"] is True
    assert body["parsed"]["format"] == "rfc3164" and body["parsed"]["host"] == "asa-01"
    assert [t["matched"] for t in body["trace"]] == [False, True]
    r = client.post(f"{_BASE}/classify-test", json={"line": "x", "classifier": {"rules": [{"when": "msg ==", "stream": "a"}]}})
    assert r.status_code == 422


def test_isolamento_entre_organizacoes(setup):
    """Admin escopado a outra org não vê nem edita a fonte."""
    client, push_id, _, Session = setup
    src = client.post(f"{_BASE}/sources", json=_body(push_id)).json()
    with Session() as db:
        other = models.Organization(name="Outra", slug="outra"); db.add(other); db.commit(); other_id = other.id
    r = client.post("/api/auth/users", json={"username": "adm2", "password": "AdminPass2!", "display_name": "A2", "role": "admin", "organization_id": other_id})
    if r.status_code not in (200, 201):
        pytest.skip(f"criação de admin escopado indisponível nesta edição: {r.status_code}")
    c2 = TestClient(app)
    assert c2.post("/api/auth/login", json={"username": "adm2", "password": "AdminPass2!"}).status_code == 200
    assert [s["id"] for s in c2.get(f"{_BASE}/sources").json()] == []
    assert c2.delete(f"{_BASE}/sources/{src['id']}").status_code in (403, 404)
    c2.close()
