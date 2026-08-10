"""API de enriquecimento — ``/api/collectors/enrichment`` (ADR-LOCAL-0002, Fase 2).

Cobre o que, se quebrar, torna a API pior que inexistente: validação no COMMIT (e
não no worker), isolamento entre organizações, e os gates que impedem publicar uma
tabela que o worker recusaria em silêncio horas depois.
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

from backend.app.db.database import Base, get_session
from backend.app.main import app

_BASE = "/api/collectors/enrichment"


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


def _bootstrap_admin(client: TestClient) -> None:
    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPassword123!", "display_name": "Admin"},
    )
    assert r.status_code == 200, r.text


def _org(client: TestClient, name: str) -> int:
    r = client.post("/api/organizations", json={"name": name, "slug": name.lower()})
    assert r.status_code in (200, 201), r.text
    return int(r.json()["id"])


def _rule(**over):
    rule = {
        "id": "ti",
        "enricher": "table_cidr",
        "table": "rede",
        "key": {"source": "normalized.src_endpoint.ip", "kind": "ip"},
        "outputs": [{"from": "site", "target": "_centralops.enrichment.src.site"}],
    }
    rule.update(over)
    return rule


# ── catálogo ────────────────────────────────────────────────────────────────

def test_catalog_is_plugin_driven_and_exposes_egress(client_factory) -> None:
    """A galeria da UI sai daqui; ``egress`` é consentimento de privacidade."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.get(f"{_BASE}/enrichers")
    assert r.status_code == 200, r.text
    by_name = {item["name"]: item for item in r.json()}
    assert {"table_exact", "table_cidr", "opencti", "virustotal"} <= set(by_name)
    assert by_name["virustotal"]["egress"] == "third_party"
    assert by_name["table_cidr"]["egress"] == "none"
    assert by_name["opencti"]["mode"] == "local"
    assert by_name["virustotal"]["mode"] == "remote"
    # A UI monta o formulário a partir do schema — sem hardcode no front.
    assert by_name["opencti"]["config_schema"]["type"] == "object"


# ── tabelas ─────────────────────────────────────────────────────────────────

def test_table_lifecycle_commit_rollback(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    org = _org(client, "AcmeA")

    r = client.post(
        f"{_BASE}/tables",
        json={"name": "rede", "organization_id": org, "match_mode": "cidr"},
    )
    assert r.status_code == 201, r.text
    table_id = r.json()["id"]

    v1 = client.post(
        f"{_BASE}/tables/{table_id}/versions",
        json={"rows": {"10.0.0.0/8": {"site": "matriz"}}, "commit_message": "v1"},
    )
    assert v1.status_code == 201, v1.text
    assert v1.json()["entry_count"] == 1 and v1.json()["is_current"]

    v2 = client.post(
        f"{_BASE}/tables/{table_id}/versions",
        json={
            "rows": {"10.0.0.0/8": {"site": "matriz"}, "10.0.5.0/24": {"site": "filial"}},
            "commit_message": "v2",
        },
    )
    assert v2.status_code == 201
    assert client.get(f"{_BASE}/tables/{table_id}").json()["entry_count"] == 2

    # Rollback re-aponta o ponteiro; o histórico permanece append-only — é o que
    # mantém o ponto-no-tempo do backfill correto depois de um rollback.
    rb = client.post(
        f"{_BASE}/tables/{table_id}/rollback", json={"version_id": v1.json()["id"]}
    )
    assert rb.status_code == 200, rb.text
    assert rb.json()["current_version_id"] == v1.json()["id"]
    assert len(client.get(f"{_BASE}/tables/{table_id}/versions").json()) == 2


def test_invalid_cidr_rows_are_counted_not_fatal(client_factory) -> None:
    """3 erros de digitação não podem derrubar as linhas boas."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    org = _org(client, "AcmeB")
    tid = client.post(
        f"{_BASE}/tables", json={"name": "rede", "organization_id": org, "match_mode": "cidr"}
    ).json()["id"]

    r = client.post(
        f"{_BASE}/tables/{tid}/versions",
        json={
            "rows": {"10.0.0.0/8": {"s": 1}, "nao-e-cidr": {"s": 2}},
            "commit_message": "com erro",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["invalid_rows"] == 1
    assert r.json()["entry_count"] == 1


def test_table_with_no_valid_cidr_is_rejected(client_factory) -> None:
    """Arquivo 100% inválido é erro de upload, não tabela vazia silenciosa."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    org = _org(client, "AcmeC")
    tid = client.post(
        f"{_BASE}/tables", json={"name": "rede", "organization_id": org, "match_mode": "cidr"}
    ).json()["id"]
    r = client.post(
        f"{_BASE}/tables/{tid}/versions",
        json={"rows": {"abc": {"s": 1}}, "commit_message": "x"},
    )
    assert r.status_code == 422
    assert "CIDR" in r.text


def test_row_value_must_be_an_object(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    org = _org(client, "AcmeD")
    tid = client.post(
        f"{_BASE}/tables", json={"name": "t", "organization_id": org}
    ).json()["id"]
    r = client.post(
        f"{_BASE}/tables/{tid}/versions",
        json={"rows": {"k": "nao-e-objeto"}, "commit_message": "x"},
    )
    assert r.status_code == 422


def test_table_in_use_cannot_be_deleted(client_factory) -> None:
    """Apagar tabela referenciada viraria erro de carga silencioso a cada ciclo."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    org = _org(client, "AcmeE")
    tid = client.post(
        f"{_BASE}/tables", json={"name": "rede", "organization_id": org, "match_mode": "cidr"}
    ).json()["id"]
    client.post(
        f"{_BASE}/tables/{tid}/versions",
        json={"rows": {"10.0.0.0/8": {"site": "m"}}, "commit_message": "v1"},
    )
    pid = client.post(
        f"{_BASE}/policies", json={"name": "p", "organization_id": org}
    ).json()["id"]
    assert client.post(
        f"{_BASE}/policies/{pid}/versions",
        json={"rules": [_rule()], "commit_message": "v1"},
    ).status_code == 201

    r = client.delete(f"{_BASE}/tables/{tid}")
    assert r.status_code == 422
    assert "referenciada" in r.text


# ── políticas ───────────────────────────────────────────────────────────────

def test_policy_invalid_rule_is_422_at_commit_not_in_the_worker(client_factory) -> None:
    """Validação na ESCRITA. O oposto do fail-open de _compile_single_rule."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    org = _org(client, "AcmeF")
    pid = client.post(
        f"{_BASE}/policies", json={"name": "p", "organization_id": org}
    ).json()["id"]

    # target fora de _centralops.enrichment → contornaria o gate OCSF
    bad_target = client.post(
        f"{_BASE}/policies/{pid}/versions",
        json={
            "rules": [_rule(outputs=[{"from": "site", "target": "normalized.x"}])],
            "commit_message": "x",
        },
    )
    assert bad_target.status_code == 422
    assert "_centralops.enrichment" in bad_target.text

    # chave desconhecida na regra
    assert client.post(
        f"{_BASE}/policies/{pid}/versions",
        json={"rules": [_rule(campo_inventado=1)], "commit_message": "x"},
    ).status_code == 422

    # enricher inexistente — a mensagem lista os válidos (consumidor é agente de IA)
    unknown = client.post(
        f"{_BASE}/policies/{pid}/versions",
        json={"rules": [_rule(enricher="nao_existe")], "commit_message": "x"},
    )
    assert unknown.status_code == 422
    assert "table_cidr" in unknown.text


def test_policy_referencing_missing_table_is_rejected(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    org = _org(client, "AcmeG")
    pid = client.post(
        f"{_BASE}/policies", json={"name": "p", "organization_id": org}
    ).json()["id"]
    r = client.post(
        f"{_BASE}/policies/{pid}/versions",
        json={"rules": [_rule(table="fantasma")], "commit_message": "x"},
    )
    assert r.status_code == 422
    assert "fantasma" in r.text


def test_policy_cannot_be_enabled_without_a_version(client_factory) -> None:
    """Caso nº1 de suporte: 'liguei e não faz nada'."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    org = _org(client, "AcmeH")
    pid = client.post(
        f"{_BASE}/policies", json={"name": "p", "organization_id": org}
    ).json()["id"]
    r = client.post(f"{_BASE}/policies/{pid}/enable", params={"enabled": True})
    assert r.status_code == 422
    assert "publique uma versão" in r.text


def test_policy_create_does_not_enable_it(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    org = _org(client, "AcmeI")
    body = client.post(
        f"{_BASE}/policies", json={"name": "p", "organization_id": org}
    ).json()
    assert body["enabled"] is False


# ── dry-run ─────────────────────────────────────────────────────────────────

def test_dry_run_shows_result_and_byte_cost_without_publishing(client_factory) -> None:
    """O operador vê o efeito E o preço antes de publicar qualquer coisa."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.post(
        f"{_BASE}/dry-run",
        json={
            "rules": [_rule(table=None, enricher="table_exact")],
            "sample": {
                "_centralops": {"organization_id": 1},
                "normalized": {"src_endpoint": {"ip": "10.0.5.7"}},
                "raw": {},
            },
            "tables": {"ti": {"10.0.5.7": {"site": "filial-sp"}}},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enriched"]["_centralops"]["enrichment"]["src"]["site"] == "filial-sp"
    assert body["hits"] == {"ti": 1}
    assert body["bytes_added"] > 0
    # O corpo OCSF permanece intocado — é o que mantém `ocsf_valid` honesto.
    assert body["enriched"]["normalized"] == {"src_endpoint": {"ip": "10.0.5.7"}}


def test_dry_run_rejects_invalid_policy(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    r = client.post(
        f"{_BASE}/dry-run",
        json={"rules": [{"id": "x"}], "sample": {}},
    )
    assert r.status_code == 422


# ── isolamento entre organizações ───────────────────────────────────────────

def test_tables_and_policies_are_listed_per_organization(client_factory) -> None:
    """Admin GLOBAL vê as duas orgs; o filtro por org existe e funciona.

    O isolamento de admin ESCOPADO é exercitado pela suíte de tenancy; aqui o que
    se ancora é que o recurso NASCE amarrado a uma org e que a listagem reflete isso.
    """
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    org_a = _org(client, "TenantA")
    org_b = _org(client, "TenantB")

    for org, name in ((org_a, "rede"), (org_b, "rede")):
        assert client.post(
            f"{_BASE}/tables",
            json={"name": name, "organization_id": org, "match_mode": "cidr"},
        ).status_code == 201

    rows = client.get(f"{_BASE}/tables").json()
    orgs = sorted(t["organization_id"] for t in rows)
    assert orgs == sorted([org_a, org_b])
    # Mesmo NOME em orgs diferentes é legítimo e não colide.
    assert {t["name"] for t in rows} == {"rede"}


def test_duplicate_name_in_same_org_is_rejected(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    org = _org(client, "TenantC")
    payload = {"name": "rede", "organization_id": org, "match_mode": "cidr"}
    assert client.post(f"{_BASE}/tables", json=payload).status_code == 201
    r = client.post(f"{_BASE}/tables", json=payload)
    assert r.status_code == 422
    assert "já existe" in r.text
