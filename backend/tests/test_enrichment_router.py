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


# ── fontes configuradas: o segredo nunca sai, e nunca cruza a org ───────────

def test_source_secret_is_never_returned_by_the_api(client_factory) -> None:
    """``secret_ref`` é o CIPHERTEXT, e o cofre não conhece organização.

    ``core.secrets.backend.decrypt(ciphertext)`` não recebe org: qualquer blob
    válido decifra. Se a API devolvesse a referência, um admin da Org A copiaria
    a da Org B e passaria a USAR a credencial dela — a cota, a identidade e o
    egresso do vizinho — sem nunca ver o texto claro. Por isso a resposta expõe
    só ``secret_configured``.
    """
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    org = _org(client, "SecretOrg")

    r = client.post(
        f"{_BASE}/sources",
        json={
            "name": "vt-prod",
            "enricher": "virustotal",
            "organization_id": org,
            "secret": "chave-super-secreta-123",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["secret_configured"] is True
    assert "secret_ref" not in body
    assert "secret" not in body
    assert "chave-super-secreta-123" not in r.text

    listed = client.get(f"{_BASE}/sources").text
    assert "chave-super-secreta-123" not in listed
    assert "secret_ref" not in listed


def test_source_is_scoped_to_its_organization(client_factory) -> None:
    """Mesmo nome em orgs distintas é legítimo; a resolução é sempre (org, nome)."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    org_a = _org(client, "SrcOrgA")
    org_b = _org(client, "SrcOrgB")

    for org in (org_a, org_b):
        assert client.post(
            f"{_BASE}/sources",
            json={
                "name": "vt",
                "enricher": "virustotal",
                "organization_id": org,
                "secret": f"chave-da-org-{org}",
            },
        ).status_code == 201

    rows = client.get(f"{_BASE}/sources").json()
    assert sorted(s["organization_id"] for s in rows) == sorted([org_a, org_b])
    assert {s["name"] for s in rows} == {"vt"}


def test_source_config_is_validated_against_the_enricher_schema(client_factory) -> None:
    """Config inválida é 422 no commit, não erro de rede no meio do ciclo."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    org = _org(client, "CfgOrg")

    r = client.post(
        f"{_BASE}/sources",
        json={
            "name": "octi",
            "enricher": "opencti",
            "organization_id": org,
            "config": {"url": "https://octi.interno", "page_size": 999_999},
            "secret": "tok",
        },
    )
    assert r.status_code == 422
    assert "page_size" in r.text


def test_source_url_goes_through_the_egress_guard(client_factory) -> None:
    """SSRF: a `url` da fonte vira `aiohttp.post` COM o token no Authorization.

    Sem o guard, `file://`, credencial embutida ou um host de metadados de nuvem
    viraria exfiltração de credencial. O projeto já tem `normalize_service_url`
    protegendo okta/crowdstrike/veeam/wazuh; enrichment passou a usar o mesmo.
    """
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    org = _org(client, "SsrfOrg")

    for bad in ("file:///etc/passwd", "https://user:senha@octi.interno"):
        r = client.post(
            f"{_BASE}/sources",
            json={
                "name": f"octi-{abs(hash(bad)) % 9999}",
                "enricher": "opencti",
                "organization_id": org,
                "config": {"url": bad},
                "secret": "tok",
            },
        )
        assert r.status_code == 422, f"{bad} deveria ser recusada, veio {r.status_code}"


def test_enricher_requiring_secret_needs_a_source_in_the_rule(client_factory) -> None:
    """Regra que precisa de credencial e não cita fonte nunca poderia rodar.

    422 no commit em vez de no-op silencioso no ciclo — a lição do rótulo
    `producer_unsupported`: falha invisível é pior que falha barulhenta.
    """
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    org = _org(client, "NeedSrcOrg")
    pol = client.post(
        f"{_BASE}/policies", json={"name": "p", "organization_id": org}
    ).json()["id"]

    r = client.post(
        f"{_BASE}/policies/{pol}/versions",
        json={
            "rules": [
                {
                    "id": "vt",
                    "enricher": "virustotal",
                    "key": {"source": "normalized.src_endpoint.ip", "kind": "ip"},
                    "outputs": [
                        {"from": "malicious", "target": "_centralops.enrichment.vt.m"}
                    ],
                }
            ],
            "commit_message": "sem fonte",
        },
    )
    assert r.status_code == 422
    assert "source" in r.text


def test_source_in_use_cannot_be_deleted(client_factory) -> None:
    """Apagar fonte em uso quebraria a regra em SILÊNCIO a cada ciclo."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    org = _org(client, "InUseOrg")
    client.post(
        f"{_BASE}/sources",
        json={
            "name": "vt", "enricher": "virustotal",
            "organization_id": org, "secret": "tok",
        },
    )
    src_id = client.get(f"{_BASE}/sources").json()[0]["id"]
    pol = client.post(
        f"{_BASE}/policies", json={"name": "p", "organization_id": org}
    ).json()["id"]
    ver = client.post(
        f"{_BASE}/policies/{pol}/versions",
        json={
            "rules": [
                {
                    "id": "vt", "enricher": "virustotal", "source": "vt",
                    "key": {"source": "normalized.src_endpoint.ip", "kind": "ip"},
                    "outputs": [
                        {"from": "malicious", "target": "_centralops.enrichment.vt.m"}
                    ],
                }
            ],
            "commit_message": "usa a fonte",
        },
    )
    assert ver.status_code == 201, ver.text

    r = client.delete(f"{_BASE}/sources/{src_id}")
    assert r.status_code == 422
    assert "é usada" in r.text


def test_config_cannot_carry_a_secret_reference(client_factory) -> None:
    """A credencial entra SÓ pelo campo ``secret``, cifrado pelo servidor.

    ``core.secrets.backend.decrypt(ciphertext)`` não recebe organização: qualquer
    blob válido decifra. Enquanto os schemas tinham ``api_key_secret_ref`` /
    ``token_secret_ref``, a config — escrita por um admin de org via API — vencia a
    referência do servidor (``ref = secret_ref or ctx.secret_ref``), então colar o
    ciphertext da Org B dava acesso à credencial dela. Os campos saíram do schema e
    a rota recusa qualquer chave com "secret".
    """
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    org = _org(client, "NoSecretInCfgOrg")

    r = client.post(
        f"{_BASE}/sources",
        json={
            "name": "vt-hack",
            "enricher": "virustotal",
            "organization_id": org,
            "config": {"api_key_secret_ref": "ciphertext-roubado-de-outra-org"},
            "secret": "minha-propria-chave",
        },
    )
    assert r.status_code == 422, r.text
    assert "secret" in r.text


# ── fonte multi-org (MSP) e gate de edição ──────────────────────────────────

def test_sharing_a_source_requires_enterprise(client_factory, monkeypatch) -> None:
    """Compartilhar entre orgs é EE. Na Community a fonte atende uma org.

    Não é trava artificial: o escopo de subárvore que faz a matriz enxergar as
    filhas já é EE (`core/tenant.py` usa resolver FLAT no Core), então em CE nem
    existe como escolher a filha.
    """
    from backend.app.core import edition

    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    matriz = _org(client, "MatrizCE")
    filha = _org(client, "FilhaCE")

    monkeypatch.setattr(edition, "feature_enabled", lambda name: False)
    r = client.post(
        f"{_BASE}/sources",
        json={
            "name": "vt-msp", "enricher": "virustotal",
            "organization_id": matriz, "secret": "tok",
            "shared_organization_ids": [filha],
        },
    )
    assert r.status_code == 403, r.text
    assert "Enterprise" in r.text


def test_source_shared_with_child_orgs_and_editable_afterwards(
    client_factory, monkeypatch
) -> None:
    """Com EE a matriz escolhe as filhas, e a lista muda depois da criação."""
    from backend.app.core import edition

    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    matriz = _org(client, "MatrizEE")
    filha_a = _org(client, "FilhaA")
    filha_b = _org(client, "FilhaB")

    monkeypatch.setattr(edition, "feature_enabled", lambda name: True)
    created = client.post(
        f"{_BASE}/sources",
        json={
            "name": "vt-msp", "enricher": "virustotal",
            "organization_id": matriz, "secret": "tok",
            "shared_organization_ids": [filha_a],
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["shared_organization_ids"] == [filha_a]
    src_id = created.json()["id"]

    # A lista é editável: era a filha A, passa a ser A e B.
    upd = client.patch(
        f"{_BASE}/sources/{src_id}",
        json={"shared_organization_ids": [filha_a, filha_b]},
    )
    assert upd.status_code == 200, upd.text
    assert upd.json()["shared_organization_ids"] == sorted([filha_a, filha_b])

    # E dá para tirar todas, voltando a atender só a dona.
    upd2 = client.patch(f"{_BASE}/sources/{src_id}", json={"shared_organization_ids": []})
    assert upd2.json()["shared_organization_ids"] == []
