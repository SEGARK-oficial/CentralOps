"""Modelo de política da matriz — herança por MATERIALIZAÇÃO (Enterprise).

O que estes testes protegem, em ordem de custo do defeito:

1. **O runtime não ganha um segundo caminho de resolução.** A herança acontece
   publicando uma versão própria em cada filha; ``load_policy_for_org`` segue
   lendo a política da PRÓPRIA organização pelo ponteiro. Um caminho "se não
   achar na org, procure na matriz" faria a política da matriz valer em
   qualquer tenant cuja própria política falhasse ao carregar — o vazamento
   cross-tenant que esta feature inteira existe para não ter.
2. **A subárvore é o limite.** Aplicar fora dela seria escrever política na
   organização de outra árvore de tenants.
3. **Precedência é observável.** Política própria habilitada vence o modelo, e
   a tela precisa dizer isso: sem o aviso, aplicar pareceria ter funcionado e
   nada mudaria — porque vale uma política por organização.
4. **A decisão é recalculada no apply.** Entre ver a tela e clicar, alguém pode
   ter apagado a tabela que a regra cita.
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


@pytest.fixture(autouse=True)
def _enterprise(monkeypatch):
    """O modelo é Enterprise. Sem isto todo teste aqui tomaria 403.

    Trava o gate na FUNÇÃO que o router chama, e não numa variável de ambiente:
    é o mesmo ponto que a Community exercita, então ligar aqui não esconde a
    checagem — há um teste explícito de que ela existe.
    """
    from backend.app.core import edition

    monkeypatch.setattr(edition, "feature_enabled", lambda name: name == "multi_tenant")
    yield


def _bootstrap_admin(client: TestClient) -> None:
    r = client.post(
        "/api/auth/bootstrap",
        json={
            "username": "admin",
            "password": "AdminPassword123!",
            "display_name": "Admin",
        },
    )
    assert r.status_code == 200, r.text


def _org(client: TestClient, name: str, parent: int | None = None) -> int:
    body = {"name": name, "slug": name.lower()}
    r = client.post("/api/organizations", json=body)
    assert r.status_code in (200, 201), r.text
    org_id = int(r.json()["id"])
    if parent is not None:
        # A hierarquia é materializada pelo EE; aqui basta o ponteiro, que é o
        # que ``_child_org_ids`` caminha.
        from backend.app.db import models

        return _set_parent(org_id, parent)
    return org_id


_SESSION_FACTORY = {}


def _set_parent(org_id: int, parent_id: int) -> int:
    from backend.app.db import models

    SessionLocal = _SESSION_FACTORY["s"]
    with SessionLocal() as db:
        row = db.get(models.Organization, org_id)
        row.parent_organization_id = parent_id
        db.commit()
    return org_id


def _table(client: TestClient, org: int, name: str, publish: bool = True) -> None:
    r = client.post(
        f"{_BASE}/tables",
        json={"name": name, "organization_id": org, "match_mode": "cidr"},
    )
    assert r.status_code == 201, r.text
    if publish:
        r = client.post(
            f"{_BASE}/tables/{r.json()['id']}/versions",
            json={"rows": {"10.0.5.0/24": {"site": "x"}}, "commit_message": "v1"},
        )
        assert r.status_code in (200, 201), r.text


def _rule(table: str) -> dict:
    return {
        "id": "regra-site",
        "enricher": "table_cidr",
        "table": table,
        "key": {"source": "normalized.src_endpoint.ip", "kind": "ip"},
        "outputs": [{"from": "site", "target": "_centralops.enrichment.src.site"}],
    }


def _template(client: TestClient, org: int, table: str, name: str = "padrao-soc") -> str:
    r = client.post(f"{_BASE}/policies", json={"name": name, "organization_id": org})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = client.post(
        f"{_BASE}/policies/{pid}/versions",
        json={"rules": [_rule(table)], "commit_message": "v1"},
    )
    assert r.status_code in (200, 201), r.text
    r = client.post(f"{_BASE}/policies/{pid}/template", params={"is_template": True})
    assert r.status_code == 200, r.text
    assert r.json()["is_template"] is True
    return pid


def _setup(client_factory):
    factory, SessionLocal = client_factory
    _SESSION_FACTORY["s"] = SessionLocal
    client = factory()
    _bootstrap_admin(client)
    matriz = _org(client, "Matriz")
    filha = _org(client, "FilialA")
    _set_parent(filha, matriz)
    return client, matriz, filha


# ── o caminho principal ─────────────────────────────────────────────────────


def test_aplicar_publica_versao_DERIVADA_e_desabilitada_na_filha(client_factory) -> None:
    """A filha ganha versão PRÓPRIA, com histórico e rollback próprios.

    E nasce desabilitada: colocar regra no caminho quente de outro tenant é
    decisão de quem opera aquele tenant.
    """
    client, matriz, filha = _setup(client_factory)
    _table(client, matriz, "plano-de-rede")
    _table(client, filha, "plano-de-rede")
    pid = _template(client, matriz, "plano-de-rede")

    r = client.post(f"{_BASE}/policies/{pid}/template-preflight")
    assert r.status_code == 200, r.text
    alvos = {t["organization_id"]: t for t in r.json()["targets"]}
    assert alvos[filha]["status"] == "ready"

    r = client.post(
        f"{_BASE}/policies/{pid}/apply-template",
        json={"organization_ids": [filha], "commit_message": "padrão do SOC"},
    )
    assert r.status_code == 200, r.text
    aplicadas = r.json()["applied"]
    assert len(aplicadas) == 1
    assert aplicadas[0]["organization_id"] == filha

    # A política da filha existe, é dela, e está desabilitada.
    r = client.get(f"{_BASE}/policies")
    da_filha = [p for p in r.json() if p["organization_id"] == filha]
    assert len(da_filha) == 1
    assert da_filha[0]["enabled"] is False
    assert da_filha[0]["rule_count"] == 1
    # O rastro da origem está lá — é o que permite dizer "herdada, e desta versão".
    assert da_filha[0]["derived_from_version_id"]


def test_reaplicar_a_mesma_versao_e_no_op(client_factory) -> None:
    """Sem isto, cada clique criaria uma versão idêntica e poluiria o histórico
    que serve ao rollback."""
    client, matriz, filha = _setup(client_factory)
    _table(client, matriz, "rede")
    _table(client, filha, "rede")
    pid = _template(client, matriz, "rede")

    client.post(
        f"{_BASE}/policies/{pid}/apply-template", json={"organization_ids": [filha]}
    )
    r = client.post(f"{_BASE}/policies/{pid}/template-preflight")
    alvos = {t["organization_id"]: t for t in r.json()["targets"]}
    assert alvos[filha]["status"] == "up_to_date"

    r = client.post(
        f"{_BASE}/policies/{pid}/apply-template", json={"organization_ids": [filha]}
    )
    assert r.json()["applied"] == []
    assert r.json()["skipped"][0]["status"] == "up_to_date"


# ── os guards ───────────────────────────────────────────────────────────────


def test_filha_sem_a_tabela_e_BLOQUEADA_e_nada_e_escrito(client_factory) -> None:
    """O pré-requisito ausente, sem este gate, viraria carga de tabela falhando
    a cada ciclo em N organizações ao mesmo tempo."""
    client, matriz, filha = _setup(client_factory)
    _table(client, matriz, "plano-de-rede")
    # A filha NÃO tem a tabela.
    pid = _template(client, matriz, "plano-de-rede")

    r = client.post(f"{_BASE}/policies/{pid}/template-preflight")
    alvos = {t["organization_id"]: t for t in r.json()["targets"]}
    assert alvos[filha]["status"] == "blocked"
    assert alvos[filha]["missing_tables"] == ["plano-de-rede"]

    r = client.post(
        f"{_BASE}/policies/{pid}/apply-template", json={"organization_ids": [filha]}
    )
    assert r.status_code == 200, r.text
    assert r.json()["applied"] == []
    assert r.json()["skipped"][0]["status"] == "blocked"

    # E nada foi escrito na filha.
    r = client.get(f"{_BASE}/policies")
    assert [p for p in r.json() if p["organization_id"] == filha] == []


def test_politica_propria_habilitada_vence_o_modelo(client_factory) -> None:
    """Precedência observável.

    Vale UMA política por organização, a mais antiga habilitada. Aplicar o
    modelo por cima pareceria ter funcionado e nada mudaria no runtime — e é
    justamente esse "editei e não mudou nada" que a tela precisa evitar.
    """
    client, matriz, filha = _setup(client_factory)
    _table(client, matriz, "rede")
    _table(client, filha, "rede")

    # A filha já tem política PRÓPRIA, habilitada.
    r = client.post(f"{_BASE}/policies", json={"name": "propria-da-filha", "organization_id": filha})
    own = r.json()["id"]
    client.post(
        f"{_BASE}/policies/{own}/versions",
        json={"rules": [_rule("rede")], "commit_message": "v1"},
    )
    assert client.post(f"{_BASE}/policies/{own}/enable", params={"enabled": True}).status_code == 200

    pid = _template(client, matriz, "rede")
    r = client.post(f"{_BASE}/policies/{pid}/template-preflight")
    alvos = {t["organization_id"]: t for t in r.json()["targets"]}
    assert alvos[filha]["status"] == "overridden"
    assert alvos[filha]["overriding_policy"] == "propria-da-filha"

    r = client.post(
        f"{_BASE}/policies/{pid}/apply-template", json={"organization_ids": [filha]}
    )
    assert r.json()["applied"] == []
    assert r.json()["skipped"][0]["status"] == "overridden"


def test_recusa_aplicar_fora_da_subarvore(client_factory) -> None:
    """Escrever política na organização de outra árvore é o furo cross-tenant."""
    client, matriz, filha = _setup(client_factory)
    outra = _org(client, "OutraArvore")  # sem pai
    _table(client, matriz, "rede")
    pid = _template(client, matriz, "rede")

    r = client.post(
        f"{_BASE}/policies/{pid}/apply-template", json={"organization_ids": [outra]}
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "enrichment.template_target_outside_subtree"


def test_decisao_e_recalculada_no_apply(client_factory) -> None:
    """Entre ver a tela e clicar, alguém pode apagar a tabela que a regra cita.

    Confiar no preflight anterior colocaria N organizações num estado que a
    tela acabara de dizer ser impossível.
    """
    client, matriz, filha = _setup(client_factory)
    _table(client, matriz, "rede")
    _table(client, filha, "rede")
    pid = _template(client, matriz, "rede")

    r = client.post(f"{_BASE}/policies/{pid}/template-preflight")
    alvos = {t["organization_id"]: t for t in r.json()["targets"]}
    assert alvos[filha]["status"] == "ready"

    # A tabela da filha some DEPOIS do preflight.
    from backend.app.db import models

    SessionLocal = _SESSION_FACTORY["s"]
    with SessionLocal() as db:
        db.query(models.EnrichmentTable).filter(
            models.EnrichmentTable.organization_id == filha
        ).delete()
        db.commit()

    r = client.post(
        f"{_BASE}/policies/{pid}/apply-template", json={"organization_ids": [filha]}
    )
    assert r.json()["applied"] == []
    assert r.json()["skipped"][0]["status"] == "blocked"


def test_marcar_como_modelo_exige_ter_filhas(client_factory) -> None:
    """Modelo em organização folha é um botão que nunca tem a quem aplicar."""
    factory, SessionLocal = client_factory
    _SESSION_FACTORY["s"] = SessionLocal
    client = factory()
    _bootstrap_admin(client)
    folha = _org(client, "Folha")
    _table(client, folha, "rede")

    r = client.post(f"{_BASE}/policies", json={"name": "p", "organization_id": folha})
    pid = r.json()["id"]
    client.post(
        f"{_BASE}/policies/{pid}/versions",
        json={"rules": [_rule("rede")], "commit_message": "v1"},
    )
    r = client.post(f"{_BASE}/policies/{pid}/template", params={"is_template": True})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "enrichment.template_without_children"


def test_politica_sem_a_marca_nao_pode_ser_aplicada(client_factory) -> None:
    client, matriz, filha = _setup(client_factory)
    _table(client, matriz, "rede")
    r = client.post(f"{_BASE}/policies", json={"name": "comum", "organization_id": matriz})
    pid = r.json()["id"]
    client.post(
        f"{_BASE}/policies/{pid}/versions",
        json={"rules": [_rule("rede")], "commit_message": "v1"},
    )

    r = client.post(f"{_BASE}/policies/{pid}/template-preflight")
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "enrichment.not_a_template"


# ── a propriedade que sustenta tudo ─────────────────────────────────────────


def test_o_runtime_nao_ganha_um_segundo_caminho_de_resolucao(client_factory) -> None:
    """A filha só enxerga a política DELA, e nunca a da matriz.

    Este é o teste que impede a versão "fácil" desta feature: um fallback do
    tipo "se não achar na org, procure na matriz" faria a política da matriz
    valer em qualquer tenant cuja própria política falhasse ao carregar. A
    herança é por materialização justamente para que isso não exista.
    """
    from backend.app.collectors.enrich.runtime import load_policy_for_org
    from backend.app.db import database as db_module

    client, matriz, filha = _setup(client_factory)
    _table(client, matriz, "rede")
    pid = _template(client, matriz, "rede")
    assert (
        client.post(f"{_BASE}/policies/{pid}/enable", params={"enabled": True}).status_code
        == 200
    )

    SessionLocal = _SESSION_FACTORY["s"]
    original = db_module.SessionLocal
    db_module.SessionLocal = SessionLocal
    try:
        # A matriz tem política em vigor…
        assert load_policy_for_org(matriz, enabled=True) is not None
        # …e a filha, que NÃO recebeu aplicação nenhuma, não tem nada.
        assert load_policy_for_org(filha, enabled=True) is None
    finally:
        db_module.SessionLocal = original


def test_sem_enterprise_o_modelo_e_recusado(client_factory, monkeypatch) -> None:
    """O gate existe, e a mensagem aponta o caminho da Community."""
    from backend.app.core import edition

    client, matriz, _filha = _setup(client_factory)
    _table(client, matriz, "rede")
    pid = _template(client, matriz, "rede")

    monkeypatch.setattr(edition, "feature_enabled", lambda _name: False)
    r = client.post(f"{_BASE}/policies/{pid}/template-preflight")
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "enrichment.template_requires_enterprise"
    assert "copiar" in r.json()["detail"]
