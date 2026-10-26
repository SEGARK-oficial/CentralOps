"""Contrato do Core para desvincular/vincular uma org filha de parceiro (ADR-0017).

As operações moram no EE; o Core carrega o que elas pressupõem e as guardas que
fecham o caminho que gerou o estado quebrado visto em produção: a filha excluída
no parceiro, reativada à mão, com chave própria gravada e ignorada.

  * estado ``detached`` na seleção — fora do aprovar/excluir em lote;
  * listagem do parceiro com filtro ``detached`` e a org do tenant desvinculado;
  * ``PUT /integrations/{id}``: filha recusa credencial própria, aceita edição sem
    ela, e reativação recusa org inativa e tenant excluído;
  * ``db.hierarchy``: virar raiz, revogar a fonte herdada, ``link_existing_organization``
    sem commit.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import database as _db_module
from backend.app.db import hierarchy, models, repository
from backend.app.db.database import Base, get_session
from backend.app.main import app

TENANT = "11111111-2222-3333-4444-555555555555"


@pytest.fixture()
def engine_and_client(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(_db_module, "SessionLocal", TestingSession)

    def override_get_session():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_session] = override_get_session
    # O PUT mexe no RedBeat ao reativar; aqui só interessa o banco.
    monkeypatch.setattr(
        "backend.app.routers.integrations._register_in_beat", lambda _id: None
    )
    monkeypatch.setattr(
        "backend.app.routers.integrations._deregister_from_beat", lambda _id: None
    )
    client = TestClient(app)
    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPassword123!", "display_name": "Admin"},
    )
    assert r.status_code == 200, r.text
    r = client.post("/api/auth/login", json={"username": "admin", "password": "AdminPassword123!"})
    assert r.status_code == 200, r.text
    yield engine, client
    client.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


def _seed_real_case(engine, *, selection_state: str = "excluded") -> dict[str, int]:
    """O estado visto em produção: org e filha desativadas pela exclusão no parceiro."""
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.execute(text(
            "INSERT INTO organizations(name, slug, is_active, created_at, updated_at, auto_managed) "
            "VALUES ('Reseller Exemplo', 'reseller-exemplo', 1, datetime('now'), datetime('now'), 0)"
        ))
        db.execute(text(
            "INSERT INTO integrations(organization_id, name, platform, is_active, kind, "
            "client_id, external_id, id_type, auth_status, created_at, updated_at, "
            "auto_managed, auto_approve_new_tenants) "
            "VALUES (1, 'Sophos Partner', 'sophos', 1, 'partner', 'partner-cid', "
            "'partner-uuid', 'partner', 'healthy', datetime('now'), datetime('now'), 0, 0)"
        ))
        db.execute(text(
            "INSERT INTO organizations(name, slug, is_active, created_at, updated_at, "
            "auto_managed, external_provider, external_id, partner_integration_id) "
            "VALUES ('Cliente Exemplo', 'cliente-exemplo', 0, "
            "datetime('now'), datetime('now'), 1, 'sophos', :t, 1)"
        ), {"t": TENANT})
        db.execute(text(
            "INSERT INTO integrations(organization_id, name, platform, is_active, kind, "
            "parent_integration_id, external_id, tenant_id, id_type, api_host, region, "
            "auth_status, created_at, updated_at, auto_managed) "
            "VALUES (2, 'Cliente Exemplo (Sophos)', 'sophos', 0, 'tenant', 1, "
            ":t, :t, 'tenant', 'api-us03.central.sophos.com', 'us03', 'unknown', "
            "datetime('now'), datetime('now'), 1)"
        ), {"t": TENANT})
        db.execute(text(
            "INSERT INTO integration_tenant_selections (parent_integration_id, external_id, "
            "state, name_snapshot, last_seen_at, created_at, updated_at) "
            "VALUES (1, :t, :s, 'Cliente Exemplo', datetime('now'), "
            "datetime('now'), datetime('now'))"
        ), {"t": TENANT, "s": selection_state})
        db.commit()
    return {"partner": 1, "child": 2, "org": 2, "reseller_org": 1}


def _error_code(r) -> str:
    return r.json()["error"]["code"]


# ── PUT /integrations/{id} ────────────────────────────────────────────


def test_put_partner_child_with_credentials_is_refused(engine_and_client):
    engine, client = engine_and_client
    ids = _seed_real_case(engine)
    r = client.put(
        f"/api/integrations/{ids['child']}",
        json={"client_id": "own-cid", "client_secret": "own-secret"},
    )
    assert r.status_code == 409, r.text
    assert _error_code(r) == "integration.partner_child_credentials"
    Session = sessionmaker(bind=engine)
    with Session() as db:
        child = db.get(models.Integration, ids["child"])
        assert child.client_id is None
        assert not child.credentials


def test_put_partner_child_without_credentials_is_accepted(engine_and_client):
    """Antes, toda edição de filha Sophos exigia client_id/client_secret (400) —
    o caminho que levou o operador a gravar a chave própria na filha."""
    engine, client = engine_and_client
    ids = _seed_real_case(engine, selection_state="approved")
    r = client.put(f"/api/integrations/{ids['child']}", json={"name": "Cliente (Sophos)"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Cliente (Sophos)"


def test_put_reactivate_in_inactive_org_is_refused(engine_and_client):
    engine, client = engine_and_client
    ids = _seed_real_case(engine, selection_state="approved")
    r = client.put(f"/api/integrations/{ids['child']}", json={"is_active": True})
    assert r.status_code == 409, r.text
    assert _error_code(r) == "organization.inactive"


def test_put_reactivate_partner_child_with_excluded_selection_is_refused(engine_and_client):
    engine, client = engine_and_client
    ids = _seed_real_case(engine, selection_state="excluded")
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.execute(text("UPDATE organizations SET is_active = 1 WHERE id = :o"), {"o": ids["org"]})
        db.commit()
    r = client.put(f"/api/integrations/{ids['child']}", json={"is_active": True})
    assert r.status_code == 409, r.text
    assert _error_code(r) == "integration.partner_tenant_excluded"


def test_put_reactivate_partner_child_approved_in_active_org(engine_and_client):
    engine, client = engine_and_client
    ids = _seed_real_case(engine, selection_state="approved")
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.execute(text("UPDATE organizations SET is_active = 1 WHERE id = :o"), {"o": ids["org"]})
        db.commit()
    r = client.put(f"/api/integrations/{ids['child']}", json={"is_active": True})
    assert r.status_code == 200, r.text
    assert r.json()["is_active"] is True


def test_put_keeps_already_active_integration_editable_in_inactive_org(engine_and_client):
    """A guarda é da TRANSIÇÃO para ativo; não trava um PUT que só mantém o estado."""
    engine, client = engine_and_client
    ids = _seed_real_case(engine, selection_state="approved")
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.execute(text("UPDATE integrations SET is_active = 1 WHERE id = :i"), {"i": ids["child"]})
        db.commit()
    r = client.put(f"/api/integrations/{ids['child']}", json={"is_active": True})
    assert r.status_code == 200, r.text


# ── seleção: detached ─────────────────────────────────────────────────


def test_select_tenants_refuses_detached_tenant(engine_and_client, monkeypatch):
    engine, client = engine_and_client
    ids = _seed_real_case(engine, selection_state="detached")
    calls: list[Any] = []

    def _applier(db, integration, selections, state):
        calls.append([s.external_id for s in selections])
        return {"materialized": 0, "deactivated": 0, "pending": 0, "errors": []}

    from backend.app.core import ee_hooks

    ee_hooks.reset_tenant_selection_applier()
    ee_hooks.register_tenant_selection_applier(_applier)
    try:
        r = client.post(
            f"/api/integrations/{ids['partner']}/tenants/select",
            json={"external_ids": [TENANT], "state": "approved"},
        )
    finally:
        ee_hooks.reset_tenant_selection_applier()
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["processed"] == 0
    assert [e["external_id"] for e in body["errors"]] == [TENANT]
    assert "detached" in body["errors"][0]["reason"]
    assert calls == []
    Session = sessionmaker(bind=engine)
    with Session() as db:
        state = db.execute(text("SELECT state FROM integration_tenant_selections")).scalar()
        assert state == "detached"


def test_sophos_tenants_lists_detached_with_its_organization(engine_and_client):
    engine, client = engine_and_client
    ids = _seed_real_case(engine, selection_state="detached")
    Session = sessionmaker(bind=engine)
    with Session() as db:
        # Desvinculada: a integração deixou de ser filha.
        db.execute(text(
            "UPDATE integrations SET parent_integration_id = NULL, auto_managed = 0 "
            "WHERE id = :i"
        ), {"i": ids["child"]})
        db.commit()
    r = client.get(f"/api/integrations/{ids['partner']}/sophos-tenants?state=detached")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["selection_state"] == "detached"
    assert item["child_integration_id"] is None
    assert item["organization_id"] == ids["org"]


def test_repository_set_state_never_touches_detached_rows(engine_and_client):
    engine, _client = engine_and_client
    ids = _seed_real_case(engine, selection_state="detached")
    Session = sessionmaker(bind=engine)
    with Session() as db:
        repo = repository.IntegrationTenantSelectionRepository(db)
        assert "detached" in repo.VALID_STATES
        updated = repo.set_state(
            parent_id=ids["partner"], external_ids=[TENANT], state="excluded",
            decided_by_user_id=None,
        )
        assert updated == []
        with pytest.raises(ValueError):
            repo.set_state(
                parent_id=ids["partner"], external_ids=[TENANT], state="detached",
                decided_by_user_id=None,
            )
        # O sync preserva o estado de quem já tem linha.
        row, created = repo.upsert_snapshot(
            parent_id=ids["partner"], external_id=TENANT, name_snapshot="Cliente",
            default_state="approved",
        )
        assert created is False
        assert row.state == "detached"


# ── hierarquia ────────────────────────────────────────────────────────


def test_detach_to_root_and_release_inherited_source(engine_and_client):
    engine, _client = engine_and_client
    ids = _seed_real_case(engine, selection_state="approved")
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.execute(text(
            "UPDATE organizations SET parent_organization_id = 1, root_id = 1, depth = 1 "
            "WHERE id = :o"
        ), {"o": ids["org"]})
        db.add(models.EnrichmentSource(
            id="src-reseller", organization_id=ids["reseller_org"], name="virustotal",
            enricher="virustotal", share_with_descendants=True,
        ))
        db.add(models.EnrichmentSource(
            id="src-own", organization_id=ids["org"], name="own", enricher="virustotal",
        ))
        db.flush()
        db.add(models.EnrichmentSourceOrg(source_id="src-reseller", organization_id=ids["reseller_org"]))
        db.add(models.EnrichmentSourceOrg(source_id="src-reseller", organization_id=ids["org"]))
        db.add(models.EnrichmentSourceOrg(source_id="src-own", organization_id=ids["org"]))
        db.commit()

    with Session() as db:
        org = db.get(models.Organization, ids["org"])
        assert hierarchy.has_child_orgs(db, ids["reseller_org"]) is True
        assert hierarchy.has_child_orgs(db, ids["org"]) is False
        former = hierarchy.detach_to_root(db, org)
        assert former == [ids["reseller_org"]]
        revoked = hierarchy.release_inherited_enrichment(db, org.id, former)
        assert revoked == ["virustotal"]
        db.commit()

    with Session() as db:
        org = db.get(models.Organization, ids["org"])
        assert org.parent_organization_id is None
        assert org.partner_integration_id is None
        assert org.root_id == org.id
        assert org.depth == 0
        shares = {
            (r.source_id, r.organization_id)
            for r in db.query(models.EnrichmentSourceOrg).all()
        }
        # A matriz e a fonte própria ficam; só o compartilhamento herdado sai.
        assert shares == {("src-reseller", ids["reseller_org"]), ("src-own", ids["org"])}


def test_link_existing_organization_without_commit_rolls_back(engine_and_client):
    engine, _client = engine_and_client
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.execute(text(
            "INSERT INTO organizations(name, slug, is_active, created_at, updated_at, auto_managed) "
            "VALUES ('Reseller', 'reseller', 1, datetime('now'), datetime('now'), 0)"
        ))
        db.execute(text(
            "INSERT INTO integrations(organization_id, name, platform, is_active, kind, "
            "auth_status, created_at, updated_at, auto_managed) "
            "VALUES (1, 'P', 'sophos', 1, 'partner', 'unknown', datetime('now'), datetime('now'), 0)"
        ))
        db.execute(text(
            "INSERT INTO organizations(name, slug, is_active, created_at, updated_at, auto_managed) "
            "VALUES ('Standalone', 'standalone', 1, datetime('now'), datetime('now'), 0)"
        ))
        db.commit()

    with Session() as db:
        org = repository.OrganizationRepository(db).link_existing_organization(
            2, {"external_id": TENANT}, 1, commit=False
        )
        assert org.partner_integration_id == 1
        db.rollback()

    with Session() as db:
        org = db.get(models.Organization, 2)
        assert org.partner_integration_id is None
        assert org.external_id is None


def test_audit_log_event_without_commit_rides_the_caller_transaction(engine_and_client):
    """O vincular/desvincular grava a trilha na MESMA transação da mudança."""
    from backend.app.services.audit import AuditService

    engine, _client = engine_and_client
    Session = sessionmaker(bind=engine)
    with Session() as db:
        before = db.query(models.AuditLog).count()
        AuditService(db).log_event(
            action="organization.partner_detach", endpoint="/x", username="cli",
            commit=False,
        )
        db.rollback()
    with Session() as db:
        assert db.query(models.AuditLog).count() == before
