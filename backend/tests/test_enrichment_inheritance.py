"""Herança de enriquecimento para as filhas: sincronizada, ligada, e para as futuras.

O "aplicar às filhas" manual já existia. O que faltava, e fazia o MSP criar
uma política por cliente:

1. **Ligar nas filhas.** A cópia nascia desligada e cada filha tinha que ser
   ligada à mão. ``enable=true`` no apply (e ``enable_children`` no modelo
   sincronizado) liga — nunca por cima de política própria ligada.
2. **Modelo sincronizado.** Editar o modelo exigia reaplicar. Agora publicar
   (e fazer rollback) reaplica.
3. **Filhas futuras.** Fonte e modelo alcançavam só as filhas que existiam no
   clique. Agora a filha que entra na subárvore herda na hora — pelo gancho de
   criação, em sessão própria, sem nunca desfazer a criação da org.

Tudo continua sendo materialização: o runtime não ganha caminho novo (há teste
disso em ``test_enrichment_template.py``).

SQLite em ARQUIVO: o gancho abre a própria sessão, e com ``StaticPool`` as duas
dividiriam uma conexão — o que produção nunca faz.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

from backend.app.core import ee_hooks
from backend.app.db import database as _db_module
from backend.app.db import hierarchy, models
from backend.app.db.database import Base, get_session
from backend.app.main import app
from backend.app.services import enrichment_inheritance as inheritance

_BASE = "/api/collectors/enrichment"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from backend.app.core import edition

    monkeypatch.setattr(edition, "feature_enabled", lambda name: name == "multi_tenant")
    engine = create_engine(
        f"sqlite:///{tmp_path / 'inherit.db'}", connect_args={"check_same_thread": False}
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
    monkeypatch.setattr(_db_module, "SessionLocal", Session)
    client = TestClient(app)
    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPassword123!", "display_name": "Admin"},
    )
    assert r.status_code == 200, r.text
    yield client, Session, engine
    client.close()
    app.dependency_overrides.clear()
    engine.dispose()


def _org(client, Session, name, parent=None) -> int:
    r = client.post("/api/organizations", json={"name": name, "slug": name.lower()})
    assert r.status_code in (200, 201), r.text
    org_id = int(r.json()["id"])
    if parent is not None:
        with Session() as db:
            db.get(models.Organization, org_id).parent_organization_id = parent
            db.commit()
    return org_id


def _source(client, org, *, descendants=False, shared=()):
    r = client.post(
        f"{_BASE}/sources",
        json={
            "name": "vt",
            "enricher": "virustotal",
            "organization_id": org,
            "secret": "chave",
            "shared_organization_ids": list(shared),
            "share_with_descendants": descendants,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _vt_rules(rule_id="vt-ip"):
    return {
        "version": 1,
        "enrichment": [
            {
                "id": rule_id,
                "enricher": "virustotal",
                "source": "vt",
                "key": {"source": "normalized.src_endpoint.ip", "kind": "ip"},
                "outputs": [
                    {"from": "malicious", "target": "_centralops.enrichment.src.vt_malicious"}
                ],
            }
        ],
    }


def _template(client, org, rules=None, name="padrao"):
    pid = client.post(f"{_BASE}/policies", json={"name": name, "organization_id": org}).json()["id"]
    r = client.post(
        f"{_BASE}/policies/{pid}/versions",
        json={"rules": rules or _vt_rules(), "commit_message": "v1"},
    )
    assert r.status_code == 201, r.text
    r = client.post(f"{_BASE}/policies/{pid}/template", params={"is_template": True})
    assert r.status_code == 200, r.text
    return pid


def _child_policy(Session, org_id, name="padrao"):
    with Session() as db:
        row = (
            db.query(models.EnrichmentPolicy)
            .filter_by(organization_id=org_id, name=name)
            .first()
        )
        if row is None:
            return None
        v = db.get(models.EnrichmentPolicyVersion, row.current_version_id)
        return {
            "enabled": bool(row.enabled),
            "rules": v.rules if v else None,
            "derived_from": v.derived_from_version_id if v else None,
            "versions": db.query(models.EnrichmentPolicyVersion).filter_by(policy_id=row.id).count(),
        }


# ── fonte para toda a subárvore ─────────────────────────────────────────────


def test_fonte_para_a_subarvore_alcanca_netas_e_se_declara(env):
    client, Session, _ = env
    matriz = _org(client, Session, "Matriz")
    filha = _org(client, Session, "Filha", parent=matriz)
    neta = _org(client, Session, "Neta", parent=filha)
    body = _source(client, matriz, descendants=True)
    assert body["share_with_descendants"] is True
    assert sorted(body["shared_organization_ids"]) == sorted([filha, neta])


def test_reescrever_a_lista_com_o_flag_ligado_nao_derruba_as_filhas(env):
    client, Session, _ = env
    matriz = _org(client, Session, "Matriz")
    a = _org(client, Session, "A", parent=matriz)
    b = _org(client, Session, "B", parent=matriz)
    sid = _source(client, matriz, descendants=True)["id"]
    r = client.patch(f"{_BASE}/sources/{sid}", json={"shared_organization_ids": [a]})
    assert r.status_code == 200, r.text
    assert sorted(r.json()["shared_organization_ids"]) == sorted([a, b])


def test_desligar_o_flag_mantem_as_linhas_como_lista_explicita(env):
    """Tirar a credencial de N clientes é gesto explícito, não efeito de checkbox."""
    client, Session, _ = env
    matriz = _org(client, Session, "Matriz")
    a = _org(client, Session, "A", parent=matriz)
    sid = _source(client, matriz, descendants=True)["id"]
    r = client.patch(f"{_BASE}/sources/{sid}", json={"share_with_descendants": False})
    assert r.status_code == 200, r.text
    assert r.json()["share_with_descendants"] is False
    assert r.json()["shared_organization_ids"] == [a]


def test_fonte_para_a_subarvore_na_community_e_403(env, monkeypatch):
    from backend.app.core import edition

    client, Session, _ = env
    matriz = _org(client, Session, "Matriz")
    monkeypatch.setattr(edition, "feature_enabled", lambda name: False)
    r = client.post(
        f"{_BASE}/sources",
        json={
            "name": "vt", "enricher": "virustotal", "organization_id": matriz,
            "secret": "k", "share_with_descendants": True,
        },
    )
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "enrichment.source_sharing_requires_enterprise"


# ── ligar nas filhas ────────────────────────────────────────────────────────


def test_apply_com_enable_liga_a_copia_mas_nao_por_cima_da_propria(env):
    client, Session, _ = env
    matriz = _org(client, Session, "Matriz")
    livre = _org(client, Session, "Livre", parent=matriz)
    ocupada = _org(client, Session, "Ocupada", parent=matriz)
    _source(client, matriz, descendants=True)
    # A "ocupada" tem política PRÓPRIA ligada: vence o modelo.
    propria = client.post(f"{_BASE}/policies", json={"name": "minha", "organization_id": ocupada}).json()["id"]
    client.post(f"{_BASE}/policies/{propria}/versions", json={"rules": _vt_rules("x"), "commit_message": "v1"})
    assert client.post(f"{_BASE}/policies/{propria}/enable", params={"enabled": True}).status_code == 200
    pid = _template(client, matriz)

    r = client.post(
        f"{_BASE}/policies/{pid}/apply-template",
        json={"organization_ids": [livre, ocupada], "enable": True},
    )
    assert r.status_code == 200, r.text
    applied = {t["organization_id"]: t for t in r.json()["applied"]}
    skipped = {t["organization_id"]: t for t in r.json()["skipped"]}
    assert applied[livre]["enabled"] is True
    assert skipped[ocupada]["status"] == "overridden"
    assert _child_policy(Session, livre)["enabled"] is True
    assert _child_policy(Session, ocupada) is None


def test_apply_sem_enable_preserva_o_comportamento_original(env):
    client, Session, _ = env
    matriz = _org(client, Session, "Matriz")
    filha = _org(client, Session, "Filha", parent=matriz)
    _source(client, matriz, descendants=True)
    pid = _template(client, matriz)
    r = client.post(f"{_BASE}/policies/{pid}/apply-template", json={"organization_ids": [filha]})
    assert r.status_code == 200, r.text
    assert _child_policy(Session, filha)["enabled"] is False


def test_enable_em_filha_ja_atualizada_so_liga(env):
    client, Session, _ = env
    matriz = _org(client, Session, "Matriz")
    filha = _org(client, Session, "Filha", parent=matriz)
    _source(client, matriz, descendants=True)
    pid = _template(client, matriz)
    client.post(f"{_BASE}/policies/{pid}/apply-template", json={"organization_ids": [filha]})
    r = client.post(
        f"{_BASE}/policies/{pid}/apply-template",
        json={"organization_ids": [filha], "enable": True},
    )
    assert [t["status"] for t in r.json()["applied"]] == ["applied"]
    estado = _child_policy(Session, filha)
    assert estado["enabled"] is True
    assert estado["versions"] == 1  # não criou versão nova só para ligar


# ── modelo sincronizado ─────────────────────────────────────────────────────


def test_ligar_sync_aplica_agora_e_publicar_propaga(env):
    client, Session, _ = env
    matriz = _org(client, Session, "Matriz")
    a = _org(client, Session, "A", parent=matriz)
    b = _org(client, Session, "B", parent=matriz)
    _source(client, matriz, descendants=True)
    pid = _template(client, matriz)

    r = client.post(
        f"{_BASE}/policies/{pid}/template",
        params={"is_template": True, "sync": True, "enable_children": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["template_sync"] is True and r.json()["template_enable_children"] is True
    for org in (a, b):
        estado = _child_policy(Session, org)
        assert estado is not None and estado["enabled"] is True

    novas = _vt_rules("vt-ip-v2")
    r = client.post(f"{_BASE}/policies/{pid}/versions", json={"rules": novas, "commit_message": "v2"})
    assert r.status_code == 201, r.text
    assert sorted(r.json()["template_sync_applied"]) == sorted([a, b])
    for org in (a, b):
        assert "vt-ip-v2" in _child_policy(Session, org)["rules"]
        assert _child_policy(Session, org)["versions"] == 2


def test_modelo_nao_sincronizado_nao_propaga_ao_publicar(env):
    client, Session, _ = env
    matriz = _org(client, Session, "Matriz")
    filha = _org(client, Session, "Filha", parent=matriz)
    _source(client, matriz, descendants=True)
    pid = _template(client, matriz)
    r = client.post(f"{_BASE}/policies/{pid}/versions", json={"rules": _vt_rules("v2"), "commit_message": "v2"})
    assert r.status_code == 201, r.text
    assert r.json()["template_sync_applied"] == []
    assert _child_policy(Session, filha) is None


def test_rollback_do_modelo_sincronizado_volta_as_filhas(env):
    client, Session, _ = env
    matriz = _org(client, Session, "Matriz")
    filha = _org(client, Session, "Filha", parent=matriz)
    _source(client, matriz, descendants=True)
    pid = _template(client, matriz)
    client.post(f"{_BASE}/policies/{pid}/template", params={"is_template": True, "sync": True})
    v1 = client.get(f"{_BASE}/policies/{pid}/versions").json()
    v1_id = [v for v in v1 if v["version_number"] == 1][0]["id"]
    client.post(f"{_BASE}/policies/{pid}/versions", json={"rules": _vt_rules("v2"), "commit_message": "v2"})
    assert '"v2"' in _child_policy(Session, filha)["rules"]

    r = client.post(f"{_BASE}/policies/{pid}/rollback", json={"version_id": v1_id})
    assert r.status_code == 200, r.text
    estado = _child_policy(Session, filha)
    assert '"vt-ip"' in estado["rules"]
    assert estado["derived_from"] == v1_id
    assert estado["versions"] == 3  # append-only: o rollback é versão nova na filha


def test_desmarcar_modelo_desliga_a_sincronizacao(env):
    client, Session, _ = env
    matriz = _org(client, Session, "Matriz")
    _org(client, Session, "Filha", parent=matriz)
    _source(client, matriz, descendants=True)
    pid = _template(client, matriz)
    client.post(f"{_BASE}/policies/{pid}/template", params={"is_template": True, "sync": True, "enable_children": True})
    r = client.post(f"{_BASE}/policies/{pid}/template", params={"is_template": False})
    assert r.json()["template_sync"] is False and r.json()["template_enable_children"] is False


# ── filha futura ────────────────────────────────────────────────────────────


def _modelo_sincronizado(client, Session, *, enable):
    matriz = _org(client, Session, "Matriz")
    _org(client, Session, "Existente", parent=matriz)  # exigência de ter filha
    _source(client, matriz, descendants=True)
    pid = _template(client, matriz)
    client.post(
        f"{_BASE}/policies/{pid}/template",
        params={"is_template": True, "sync": True, "enable_children": enable},
    )
    return matriz, pid


def test_filha_nova_herda_fonte_e_modelo_na_hora(env):
    client, Session, engine = env
    matriz, _ = _modelo_sincronizado(client, Session, enable=True)
    nova = _org(client, Session, "Nova", parent=matriz)
    assert _child_policy(Session, nova) is None  # ainda não passou pelo gancho

    with Session() as db:
        hierarchy.inherit_enrichment(db, nova)

    with Session() as db:
        linhas = db.query(models.EnrichmentSourceOrg).filter_by(organization_id=nova).count()
    assert linhas == 1
    estado = _child_policy(Session, nova)
    assert estado is not None and estado["enabled"] is True


def test_neta_nova_herda_o_modelo_do_avo(env):
    client, Session, _ = env
    matriz, _ = _modelo_sincronizado(client, Session, enable=False)
    filha = _org(client, Session, "FilhaIntermediaria", parent=matriz)
    neta = _org(client, Session, "NetaNova", parent=filha)
    with Session() as db:
        hierarchy.inherit_enrichment(db, neta)
    estado = _child_policy(Session, neta)
    assert estado is not None and estado["enabled"] is False


def test_sync_de_parceiro_dispara_o_gancho_pelo_caminho_real(env, monkeypatch):
    """Não basta a função existir: a criação de org do sync de parceiro tem que
    chamá-la. Materializador mínimo no lugar do EE, que grava o pai."""
    from backend.app.db.repository import OrganizationRepository

    client, Session, _ = env
    matriz, _ = _modelo_sincronizado(client, Session, enable=True)
    with Session() as db:
        integ = models.Integration(organization_id=matriz, name="parceiro", platform="sophos")
        db.add(integ)
        db.commit()
        integ_id = integ.id

    def materializer(session, org, parent_id):
        org.parent_organization_id = parent_id
        org.root_id = parent_id or org.id
        org.depth = 1 if parent_id else 0

    ee_hooks.reset_hierarchy_materializer()
    ee_hooks.register_hierarchy_materializer(materializer)
    try:
        with Session() as db:
            org = OrganizationRepository(db).create_from_sophos_tenant(
                {"id": "tenant-1", "name": "Cliente Novo"}, partner_integration_id=integ_id
            )
            nova = org.id
            assert org.parent_organization_id == matriz
    finally:
        ee_hooks.reset_hierarchy_materializer()

    estado = _child_policy(Session, nova)
    assert estado is not None and estado["enabled"] is True


def test_falha_na_heranca_nunca_desfaz_a_org(env, monkeypatch):
    client, Session, _ = env
    matriz, _ = _modelo_sincronizado(client, Session, enable=True)
    nova = _org(client, Session, "Nova", parent=matriz)
    calls = []

    def explode(db, org_id):
        calls.append(org_id)
        raise RuntimeError("boom")

    monkeypatch.setattr(inheritance, "on_org_attached", explode)
    with Session() as db:
        hierarchy.inherit_enrichment(db, nova)  # não levanta
    assert calls == [nova]
    with Session() as db:
        assert db.get(models.Organization, nova) is not None


def test_community_nao_herda_nada(env, monkeypatch):
    from backend.app.core import edition

    client, Session, _ = env
    matriz, _ = _modelo_sincronizado(client, Session, enable=True)
    nova = _org(client, Session, "Nova", parent=matriz)
    monkeypatch.setattr(edition, "feature_enabled", lambda name: False)
    with Session() as db:
        hierarchy.inherit_enrichment(db, nova)
    assert _child_policy(Session, nova) is None
    with Session() as db:
        assert db.query(models.EnrichmentSourceOrg).filter_by(organization_id=nova).count() == 0
