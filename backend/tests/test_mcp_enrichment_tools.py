"""Ferramentas MCP de enriquecimento, ponta a ponta pelo gateway ``/api/mcp``.

O teste de registro prova o CONTRATO (nomes, anotações de escrita, ack
obrigatório). Este prova que cada ferramenta chega na rota REST certa, com os
parâmetros certos, como o analista — e que o gate do ack de fato segura o
publish, que substitui TODAS as regras da política.

SQLite em ARQUIVO pelo mesmo motivo de ``test_mcp_gateway.py``: gateway,
middleware de auditoria e request abrem sessões próprias.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.db import models
from backend.app.db.database import Base, get_session
from backend.app.main import app

MCP = "/api/mcp"
ACCEPT = "application/json, text/event-stream"
_BASE = "/api/collectors/enrichment"


@pytest.fixture()
def env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'mcp-enrich.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
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
    from backend.app import main as _main
    from backend.app.db import database as _database
    from backend.app.mcp import gateway as _gateway

    original = _database.SessionLocal
    _database.SessionLocal = Session  # type: ignore[assignment]
    _gateway.SessionLocal = Session  # type: ignore[assignment]
    _main.SessionLocal = Session  # type: ignore[assignment]
    from backend.app.core.rate_limiter import token_rate_limiter

    token_rate_limiter._windows.clear()
    with TestClient(app) as admin:
        r = admin.post(
            "/api/auth/bootstrap", json={"username": "admin", "password": "AdminPassword123!"}
        )
        assert r.status_code == 200, r.text
        r = admin.put("/api/mcp/config", json={"enabled": True, "response_mode": "json"})
        assert r.status_code == 200, r.text
        r = admin.post("/api/v1/tokens", json={"name": "mcp", "expires_at": None})
        assert r.status_code == 201, r.text
        yield admin, Session, r.json()["token"]

    app.dependency_overrides.clear()
    _database.SessionLocal = original  # type: ignore[assignment]
    _gateway.SessionLocal = original  # type: ignore[assignment]
    _main.SessionLocal = original  # type: ignore[assignment]
    engine.dispose()


def _call(admin: TestClient, token: str, tool: str, /, **arguments: Any) -> tuple[bool, Any]:
    """(ok, payload) de uma ``tools/call``."""
    from backend.app.core.rate_limiter import token_rate_limiter

    token_rate_limiter._windows.clear()  # dezenas de chamadas num teste só
    r = admin.post(
        MCP,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
        headers={
            "Accept": ACCEPT,
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    payload = json.loads(result["content"][0]["text"])
    return not result.get("isError"), payload


def _org(admin: TestClient, Session, name: str, parent: int | None = None) -> int:
    r = admin.post("/api/organizations", json={"name": name, "slug": name.lower()})
    assert r.status_code in (200, 201), r.text
    org_id = int(r.json()["id"])
    if parent is not None:
        with Session() as db:
            db.get(models.Organization, org_id).parent_organization_id = parent
            db.commit()
    return org_id


def _table(admin: TestClient, org: int, name: str = "ativos") -> None:
    r = admin.post(
        f"{_BASE}/tables", json={"name": name, "organization_id": org, "match_mode": "exact"}
    )
    assert r.status_code == 201, r.text
    r = admin.post(
        f"{_BASE}/tables/{r.json()['id']}/versions",
        json={"rows": {"alice": {"dono": "ti"}}, "commit_message": "v1"},
    )
    assert r.status_code in (200, 201), r.text


def _rules(rule_id: str = "dono", table: str = "ativos") -> dict[str, Any]:
    return {
        "version": 1,
        "enrichment": [
            {
                "id": rule_id,
                "enricher": "table_exact",
                "table": table,
                "key": {"source": "normalized.actor.user.name", "kind": "user"},
                "outputs": [{"from": "dono", "target": "_centralops.enrichment.dono"}],
            }
        ],
    }


def test_ciclo_completo_de_uma_politica_pelo_mcp(env):
    admin, Session, token = env
    org = _org(admin, Session, "Cliente")
    _table(admin, org)

    ok, created = _call(admin, token, "create_enrichment_policy", name="padrao", organization_id=org)
    assert ok, created
    pid = created["id"]
    assert created["enabled"] is False

    sample = {"normalized": {"actor": {"user": {"name": "alice"}}}}
    ok, dry = _call(
        admin, token, "dry_run_enrichment",
        rules=_rules(), sample=sample, tables={"dono": {"alice": {"dono": "ti"}}}, policy_id=pid,
    )
    assert ok, dry
    assert dry["dry_run"]["hits"] == {"dono": 1}
    ack = dry["ack_token"]
    assert ack

    ok, version = _call(
        admin, token, "commit_enrichment_policy",
        policy_id=pid, rules=_rules(), commit_message="via mcp", ack_token=ack,
    )
    assert ok, version
    assert version["version_number"] == 1

    # Ack é de uso único.
    ok, again = _call(
        admin, token, "commit_enrichment_policy",
        policy_id=pid, rules=_rules(), commit_message="de novo", ack_token=ack,
    )
    assert not ok

    ok, enabled = _call(admin, token, "set_enrichment_policy_enabled", policy_id=pid, enabled=True)
    assert ok and enabled["is_active"] is True, enabled

    ok, listed = _call(admin, token, "list_enrichment_policies", organization_id=org)
    assert ok and [p["id"] for p in listed] == [pid]

    ok, full = _call(admin, token, "get_enrichment_policy", policy_id=pid)
    assert ok, full
    assert full["policy"]["id"] == pid
    assert full["current_version"]["id"] == version["id"]
    assert len(full["versions"]) == 1

    ok, ready = _call(admin, token, "get_enrichment_readiness", organization_id=org)
    assert ok and "steps" in ready, ready
    ok, tables = _call(admin, token, "list_enrichment_tables", organization_id=org)
    assert ok and [t["name"] for t in tables] == ["ativos"]
    ok, metrics = _call(admin, token, "get_enrichment_metrics", organization_id=org)
    assert ok, metrics
    ok, activity = _call(admin, token, "list_enrichment_activity", organization_id=org)
    assert ok, activity
    ok, catalog = _call(admin, token, "list_enrichers")
    assert ok and any(e["name"] == "table_exact" for e in catalog)

    # Rollback pelo MCP (para a própria versão: idempotente).
    ok, rolled = _call(
        admin, token, "rollback_enrichment_policy", policy_id=pid, version_id=version["id"]
    )
    assert ok and rolled["current_version_id"] == version["id"], rolled


def test_publicar_regras_diferentes_das_do_dry_run_e_recusado(env):
    """O ack amarra ESTAS regras a ESTA política: publicar outra coisa exige
    outro dry-run. Nada é gravado quando recusa."""
    admin, Session, token = env
    org = _org(admin, Session, "Cliente")
    _table(admin, org)
    ok, created = _call(admin, token, "create_enrichment_policy", name="p", organization_id=org)
    pid = created["id"]
    ok, dry = _call(admin, token, "dry_run_enrichment", rules=_rules("a"), policy_id=pid)
    assert ok and dry["ack_token"]

    ok, err = _call(
        admin, token, "commit_enrichment_policy",
        policy_id=pid, rules=_rules("b"), commit_message="troca", ack_token=dry["ack_token"],
    )
    assert not ok, err
    with Session() as db:
        assert db.get(models.EnrichmentPolicy, pid).current_version_id is None


def test_dry_run_sem_policy_id_nao_emite_ack(env):
    admin, _, token = env
    ok, dry = _call(admin, token, "dry_run_enrichment", rules=_rules())
    assert ok and dry["ack_token"] is None


def test_ferramenta_de_fonte_recusa_credencial(env):
    """``additionalProperties: false``: o segredo nem chega ao handler."""
    admin, Session, token = env
    ok, err = _call(
        admin, token, "update_enrichment_source_sharing", source_id="x", secret="nao"
    )
    assert not ok
    assert err["error_kind"] == "invalid_argument"


def test_modelo_da_matriz_de_ponta_a_ponta_pelo_mcp(env, monkeypatch):
    from backend.app.core import edition

    monkeypatch.setattr(edition, "feature_enabled", lambda name: name == "multi_tenant")
    admin, Session, token = env
    matriz = _org(admin, Session, "Matriz")
    filha = _org(admin, Session, "Filha", parent=matriz)
    r = admin.post(
        f"{_BASE}/sources",
        json={"name": "vt", "enricher": "virustotal", "organization_id": matriz, "secret": "k"},
    )
    assert r.status_code == 201, r.text
    sid = r.json()["id"]

    # Fonte para a subárvore — via PATCH, o verbo que o loopback ganhou.
    ok, src = _call(
        admin, token, "update_enrichment_source_sharing",
        source_id=sid, share_with_descendants=True,
    )
    assert ok, src
    assert src["share_with_descendants"] is True and src["shared_organization_ids"] == [filha]
    ok, sources = _call(admin, token, "list_enrichment_sources", organization_id=matriz)
    assert ok and sources[0].get("secret_configured") is True
    assert "secret" not in sources[0] and "secret_ref" not in sources[0]

    vt_rules = {
        "version": 1,
        "enrichment": [
            {
                "id": "vt-ip",
                "enricher": "virustotal",
                "source": "vt",
                "key": {"source": "normalized.src_endpoint.ip", "kind": "ip"},
                "outputs": [{"from": "malicious", "target": "_centralops.enrichment.vt"}],
            }
        ],
    }
    ok, created = _call(admin, token, "create_enrichment_policy", name="padrao", organization_id=matriz)
    pid = created["id"]
    ok, dry = _call(admin, token, "dry_run_enrichment", rules=vt_rules, policy_id=pid)
    ok, _ = _call(
        admin, token, "commit_enrichment_policy",
        policy_id=pid, rules=vt_rules, commit_message="v1", ack_token=dry["ack_token"],
    )
    assert ok

    ok, tpl = _call(
        admin, token, "set_enrichment_policy_template",
        policy_id=pid, is_template=True, sync=True, enable_children=True,
    )
    assert ok, tpl
    assert tpl["template_sync"] is True

    ok, pre = _call(admin, token, "preflight_enrichment_template", policy_id=pid)
    assert ok, pre
    (alvo,) = pre["targets"]
    assert alvo["organization_id"] == filha
    assert alvo["status"] == "up_to_date" and alvo["enabled"] is True

    # Reaplicar é seguro: a filha já está na versão.
    ok, applied = _call(
        admin, token, "apply_enrichment_template", policy_id=pid, organization_ids=[filha]
    )
    assert ok and [t["status"] for t in applied["skipped"]] == ["up_to_date"]
