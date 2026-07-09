"""Routers REST do subsistema de normalização.

Cobre /api/mappings, /api/quarantine, /api/drift. Todos os endpoints
exigem autenticação; mutações exigem admin.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import models
from backend.app.db.database import Base, get_session
from backend.app.main import app


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
        client = TestClient(app)
        clients.append(client)
        return client

    yield factory, TestingSessionLocal

    for client in clients:
        client.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


def _bootstrap_admin(client: TestClient) -> dict[str, Any]:
    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPassword123!", "display_name": "Admin"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _create_user(client: TestClient, *, username: str, password: str) -> dict[str, Any]:
    r = client.post(
        "/api/auth/users",
        json={
            "username": username,
            "password": password,
            "display_name": username.title(),
            "role": "user",
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _seed_definition(session, *, vendor: str, event_type: str, class_uid: int) -> str:
    defn = models.MappingDefinition(
        vendor=vendor, event_type=event_type, ocsf_class_uid=class_uid
    )
    session.add(defn)
    session.commit()
    session.refresh(defn)
    return defn.id


def _seed_version(
    session, *, definition_id: str, version_number: int, rules: list, commit: str = "seed"
) -> str:
    """Persiste uma versão com `rules` no shape v2 (dict preprocess+rules).

    Aceita uma list pura por conveniência (todos os fixtures legados usam
    esse formato) e wrapa internamente.
    """
    payload_v2 = {"preprocess": [], "rules": rules}
    v = models.MappingVersion(
        definition_id=definition_id,
        version_number=version_number,
        rules=json.dumps(payload_v2),
        commit_message=commit,
        dsl_version=2,
    )
    session.add(v)
    session.commit()
    session.refresh(v)
    return v.id


def _v2(rules: list) -> dict:
    """Wrapper para POST body — transforma list em dict v2."""
    return {"preprocess": [], "rules": rules}


# ── /api/mappings ─────────────────────────────────────────────────────


def test_list_mappings_requires_auth(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    r = client.get("/api/mappings")
    assert r.status_code == 401


def test_list_mappings_returns_seeded(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        _seed_definition(db, vendor="sophos", event_type="sophos.alert", class_uid=2004)
        _seed_definition(db, vendor="sophos", event_type="sophos.case", class_uid=2005)

    r = client.get("/api/mappings")
    assert r.status_code == 200
    data = r.json()
    assert {item["event_type"] for item in data} == {"sophos.alert", "sophos.case"}


def test_list_mappings_only_active_filters_by_integration(client_factory) -> None:
    """only_active=true mostra só mappings de vendors com integração ATIVA; o default
    (permissivo) e only_active=false mostram todos."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        _seed_definition(db, vendor="sophos", event_type="sophos.alert", class_uid=2004)
        _seed_definition(db, vendor="crowdstrike", event_type="crowdstrike.detection", class_uid=2004)
        org = models.Organization(name="ACME", slug="acme-maps")
        db.add(org)
        db.flush()
        db.add(models.Integration(organization_id=org.id, name="S", platform="sophos", is_active=True))
        # crowdstrike existe mas INATIVA → não conta como vendor ativo
        db.add(models.Integration(organization_id=org.id, name="CS", platform="crowdstrike", is_active=False))
        db.commit()

    # only_active=true → só sophos (tem integração ativa); crowdstrike (inativa) fora.
    r = client.get("/api/mappings?only_active=true")
    assert r.status_code == 200, r.text
    assert {item["vendor"] for item in r.json()} == {"sophos"}

    # only_active=false (e o default) → todos os mappings disponíveis.
    r_all = client.get("/api/mappings?only_active=false")
    assert {item["vendor"] for item in r_all.json()} == {"sophos", "crowdstrike"}
    r_default = client.get("/api/mappings")
    assert {item["vendor"] for item in r_default.json()} == {"sophos", "crowdstrike"}


def test_get_mapping_returns_versions(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        def_id = _seed_definition(
            db, vendor="sophos", event_type="sophos.alert", class_uid=2004
        )
        _seed_version(db, definition_id=def_id, version_number=1, rules=[])
        _seed_version(db, definition_id=def_id, version_number=2, rules=[])

    r = client.get(f"/api/mappings/{def_id}")
    assert r.status_code == 200
    data = r.json()
    assert len(data["versions"]) == 2
    # Ordenado desc → v2 primeiro.
    assert data["versions"][0]["version_number"] == 2
    # Schema v2: rules é sempre dict {preprocess, rules}.
    for ver in data["versions"]:
        assert isinstance(ver["rules"], dict)
        assert "preprocess" in ver["rules"]
        assert "rules" in ver["rules"]


def test_get_mapping_normalizes_legacy_v1_list_rules(client_factory) -> None:
    """Regressão do 500: linhas legadas com `rules` em formato list (v1)
    devem ser desserializadas como dict v2 pelo serializer, sem 500.
    """
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        def_id = _seed_definition(
            db, vendor="sophos", event_type="sophos.alert", class_uid=2004
        )
        # Insere bypassando o helper para preservar shape legado v1 (list).
        legacy_rules = [
            {"target": "normalized.class_uid", "const": 2004},
            {"target": "normalized.id", "source": "id"},
        ]
        v = models.MappingVersion(
            definition_id=def_id,
            version_number=1,
            rules=json.dumps(legacy_rules),
            commit_message="legacy v1 row",
            dsl_version=1,
        )
        db.add(v)
        db.commit()

    r = client.get(f"/api/mappings/{def_id}")
    assert r.status_code == 200, r.text
    data = r.json()
    rules = data["versions"][0]["rules"]
    assert isinstance(rules, dict)
    assert rules["preprocess"] == []
    assert rules["rules"] == legacy_rules


def test_create_version_requires_admin(client_factory) -> None:
    factory, Session = client_factory

    admin_client = factory()
    _bootstrap_admin(admin_client)
    _create_user(admin_client, username="bob", password="Password123!")

    with Session() as db:
        def_id = _seed_definition(
            db, vendor="sophos", event_type="sophos.alert", class_uid=2004
        )

    user_client = factory()
    user_client.post(
        "/api/auth/login",
        json={"username": "bob", "password": "Password123!"},
    )
    r = user_client.post(
        f"/api/mappings/{def_id}/versions",
        json={
            "rules": _v2([{"target": "normalized.x", "const": 1}]),
            "commit_message": "try",
        },
    )
    assert r.status_code == 403


def test_create_version_validates_dsl(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        def_id = _seed_definition(
            db, vendor="sophos", event_type="sophos.alert", class_uid=2004
        )

    # Regra sem source nem const — DSL inválida.
    r = client.post(
        f"/api/mappings/{def_id}/versions",
        json={"rules": _v2([{"target": "normalized.x"}]), "commit_message": "bad"},
    )
    assert r.status_code == 400
    assert "DSL inválida" in r.json()["detail"]


def test_create_version_persists_preprocess_and_returns_it_on_get(client_factory) -> None:
    """Regressão: ops de pré-processamento devem persistir junto com regras
    e voltar no GET /api/mappings/{id} dentro do dict v2 (`rules.preprocess`)."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        def_id = _seed_definition(
            db, vendor="sophos", event_type="sophos.alert", class_uid=2004
        )

    payload = {
        "preprocess": [
            {
                "op": "json_parse",
                "source": "details.rawData",
                "target": "_parsed",
                "tolerant": True,
            },
        ],
        "rules": [
            {"target": "normalized.class_uid", "const": 2004},
            {"target": "normalized.parsed_id", "source": "_parsed.id"},
        ],
    }

    r = client.post(
        f"/api/mappings/{def_id}/versions",
        json={"rules": payload, "commit_message": "with preprocess op"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["rules"]["preprocess"] == payload["preprocess"]
    assert body["rules"]["rules"] == payload["rules"]

    r2 = client.get(f"/api/mappings/{def_id}")
    assert r2.status_code == 200, r2.text
    versions = r2.json()["versions"]
    assert len(versions) == 1
    assert versions[0]["rules"]["preprocess"] == payload["preprocess"]
    assert versions[0]["rules"]["rules"] == payload["rules"]


def test_create_version_promotes_to_current(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        def_id = _seed_definition(
            db, vendor="sophos", event_type="sophos.alert", class_uid=2004
        )

    r = client.post(
        f"/api/mappings/{def_id}/versions",
        json={
            "rules": _v2([
                {"target": "normalized.class_uid", "const": 2004},
                {"target": "normalized.id", "source": "id"},
            ]),
            "commit_message": "first version",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version_number"] == 1

    with Session() as db:
        defn = db.get(models.MappingDefinition, def_id)
        assert defn.current_version_id == body["id"]
        # Audit log foi gravado
        log = db.query(models.MappingAuditLog).filter_by(
            mapping_definition_id=def_id, action="create_version"
        ).first()
        assert log is not None
        assert log.username == "admin"


def test_dry_run_with_explicit_raw_events(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.post(
        "/api/mappings/dry-run",
        json={
            "rules": _v2([
                {"target": "normalized.id", "source": "id", "required": True},
                {
                    "target": "normalized.severity_id",
                    "source": "severity",
                    "value_map": {"high": 4, "low": 2},
                    "default": 0,
                },
            ]),
            "raw_events": [
                {"id": "a", "severity": "high"},
                {"severity": "low"},  # falta id (required) → fail
            ],
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["sample_size"] == 2
    assert data["ok_count"] == 1
    assert data["fail_count"] == 1
    assert any(rf["target"] == "normalized.id" for rf in data["rule_failures"])


def test_dry_run_invalid_dsl_returns_400(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.post(
        "/api/mappings/dry-run",
        json={"rules": _v2([{"target": ""}]), "raw_events": []},
    )
    assert r.status_code == 400


def test_rollback_changes_current_version(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        def_id = _seed_definition(
            db, vendor="sophos", event_type="sophos.alert", class_uid=2004
        )
        v1 = _seed_version(db, definition_id=def_id, version_number=1, rules=[])
        v2 = _seed_version(db, definition_id=def_id, version_number=2, rules=[])
        defn = db.get(models.MappingDefinition, def_id)
        defn.current_version_id = v2
        db.commit()

    r = client.post(
        f"/api/mappings/{def_id}/rollback",
        json={"version_id": v1, "commit_message": "regression on v2"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["current_version_id"] == v1

    with Session() as db:
        log = db.query(models.MappingAuditLog).filter_by(
            mapping_definition_id=def_id, action="rollback"
        ).first()
        assert log is not None


def test_rollback_to_same_version_rejected(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        def_id = _seed_definition(
            db, vendor="sophos", event_type="sophos.alert", class_uid=2004
        )
        v1 = _seed_version(db, definition_id=def_id, version_number=1, rules=[])
        defn = db.get(models.MappingDefinition, def_id)
        defn.current_version_id = v1
        db.commit()

    r = client.post(
        f"/api/mappings/{def_id}/rollback",
        json={"version_id": v1, "commit_message": "noop"},
    )
    assert r.status_code == 400


# ── rules_count query param ───────────────────────────────────────────


def test_list_mappings_without_rules_count(client_factory) -> None:
    """GET /api/mappings sem ?include_rules_count → rules_count ausente ou None."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        def_id = _seed_definition(db, vendor="sophos", event_type="sophos.alert", class_uid=2004)
        v_id = _seed_version(
            db,
            definition_id=def_id,
            version_number=1,
            rules=[{"target": "normalized.class_uid", "const": 2004}],
        )
        defn = db.get(models.MappingDefinition, def_id)
        defn.current_version_id = v_id
        db.commit()

    r = client.get("/api/mappings")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    # rules_count deve ser None (não populado) ou ausente quando param não for passado.
    assert data[0].get("rules_count") is None


def test_list_mappings_with_rules_count(client_factory) -> None:
    """GET /api/mappings?include_rules_count=true → rules_count == número de regras na current_version."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    three_rules = [
        {"target": "normalized.class_uid", "const": 2004},
        {"target": "normalized.id", "source": "id"},
        {"target": "normalized.severity_id", "source": "severity", "default": 0},
    ]

    with Session() as db:
        def_id = _seed_definition(db, vendor="sophos", event_type="sophos.alert", class_uid=2004)
        v_id = _seed_version(db, definition_id=def_id, version_number=1, rules=three_rules)
        defn = db.get(models.MappingDefinition, def_id)
        defn.current_version_id = v_id
        db.commit()

    r = client.get("/api/mappings?include_rules_count=true")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]["rules_count"] == 3


def test_list_mappings_with_rules_count_no_current_version(client_factory) -> None:
    """Mapping sem current_version → rules_count == 0 quando param é true."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        # current_version_id fica NULL (nenhuma versão criada, nenhum ponteiro setado).
        _seed_definition(db, vendor="sophos", event_type="sophos.alert", class_uid=2004)

    r = client.get("/api/mappings?include_rules_count=true")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]["rules_count"] == 0


def test_list_mappings_rules_count_zero_when_empty(client_factory) -> None:
    """Mapping com current_version cujo rules lista vazia → rules_count == 0."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        def_id = _seed_definition(db, vendor="sophos", event_type="sophos.alert", class_uid=2004)
        # _seed_version wrapa em {"preprocess": [], "rules": <arg>} — lista vazia.
        v_id = _seed_version(db, definition_id=def_id, version_number=1, rules=[])
        defn = db.get(models.MappingDefinition, def_id)
        defn.current_version_id = v_id
        db.commit()

    r = client.get("/api/mappings?include_rules_count=true")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]["rules_count"] == 0


# ── /api/quarantine ───────────────────────────────────────────────────


def _seed_quarantine_event(
    session,
    *,
    vendor: str = "sophos",
    event_type: str | None = "sophos.alert",
    error_kind: str = "map",
    raw: dict | None = None,
) -> str:
    now = datetime.utcnow()
    ev = models.QuarantineEvent(
        integration_id=None,
        vendor=vendor,
        event_type=event_type,
        raw_payload=json.dumps(raw or {"id": "x"}),
        error_kind=error_kind,
        error_detail="seeded",
        expires_at=now + timedelta(days=7),
    )
    session.add(ev)
    session.commit()
    session.refresh(ev)
    return ev.id


def test_quarantine_list_filters_by_vendor(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        _seed_quarantine_event(db, vendor="sophos")
        _seed_quarantine_event(db, vendor="microsoft_defender")

    r = client.get("/api/quarantine?vendor=sophos")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 1
    assert data["items"][0]["vendor"] == "sophos"


def test_quarantine_get_returns_raw_payload(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        eid = _seed_quarantine_event(db, raw={"alert": "yes", "n": 5})

    r = client.get(f"/api/quarantine/{eid}")
    assert r.status_code == 200
    assert r.json()["raw_payload"] == {"alert": "yes", "n": 5}


def test_quarantine_discard_admin_only(client_factory) -> None:
    factory, Session = client_factory

    admin_client = factory()
    _bootstrap_admin(admin_client)
    _create_user(admin_client, username="bob", password="Password123!")

    with Session() as db:
        eid = _seed_quarantine_event(db)

    user_client = factory()
    user_client.post("/api/auth/login", json={"username": "bob", "password": "Password123!"})
    r = user_client.post(f"/api/quarantine/{eid}/discard")
    assert r.status_code == 403


def test_quarantine_discard_removes_row(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        eid = _seed_quarantine_event(db)

    r = client.post(f"/api/quarantine/{eid}/discard")
    assert r.status_code == 204

    with Session() as db:
        assert db.get(models.QuarantineEvent, eid) is None


def test_quarantine_reprocess_returns_422_without_mapping(client_factory) -> None:
    """Admin pode chamar reprocess; sem mapping ativo → 422 (não mais 501)."""
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        eid = _seed_quarantine_event(db)

    # F4-S3: 501 foi substituído por lógica real.
    # Sem mapping ativo para (sophos, sophos.alert) → 422 missing_mapping.
    r = client.post(f"/api/quarantine/{eid}/reprocess")
    assert r.status_code == 422


# ── /api/drift ────────────────────────────────────────────────────────


def _seed_unknown_field(
    session, *, vendor: str = "sophos", event_type: str = "sophos.alert", path: str = "extra.field"
) -> str:
    now = datetime.utcnow()
    uf = models.UnknownField(
        vendor=vendor,
        event_type=event_type,
        field_path=path,
        sample_value="example",
        sample_type="string",
        occurrence_count=42,
        first_seen=now - timedelta(days=2),
        last_seen=now,
        status="new",
    )
    session.add(uf)
    session.commit()
    session.refresh(uf)
    return uf.id


def test_drift_list_filters_by_status(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        _seed_unknown_field(db, path="alert.threat.hash")
        ignored = _seed_unknown_field(db, path="alert.tags")
        uf = db.get(models.UnknownField, ignored)
        uf.status = "ignored"
        db.commit()

    r = client.get("/api/drift?status=new")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 1
    assert data["items"][0]["field_path"] == "alert.threat.hash"


def test_drift_ignore_changes_status(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        fid = _seed_unknown_field(db, path="alert.threat.hash")

    r = client.post(f"/api/drift/{fid}/ignore")
    assert r.status_code == 200
    assert r.json()["status"] == "ignored"


def test_drift_mark_mapped_changes_status(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        fid = _seed_unknown_field(db, path="alert.threat.hash")

    r = client.post(f"/api/drift/{fid}/mark_mapped")
    assert r.status_code == 200
    assert r.json()["status"] == "mapped"


def test_drift_delete_removes_row(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        fid = _seed_unknown_field(db, path="alert.threat.hash")

    r = client.delete(f"/api/drift/{fid}")
    assert r.status_code == 204

    with Session() as db:
        assert db.get(models.UnknownField, fid) is None


# ── Pacote 2 — Diff endpoint ─────────────────────────────────────────


def test_diff_endpoint_returns_added_removed_modified(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    rules_v1 = [
        {"target": "class_uid", "const": 2004},
        {"target": "to_be_removed", "source": "x"},
        {"target": "to_be_modified", "const": 1},
    ]
    rules_v2 = [
        {"target": "class_uid", "const": 2004},
        {"target": "to_be_modified", "const": 2},
        {"target": "newly_added", "source": "y"},
    ]

    with Session() as db:
        def_id = _seed_definition(db, vendor="sophos", event_type="sophos.alert", class_uid=2004)
        v1_id = _seed_version(db, definition_id=def_id, version_number=1, rules=rules_v1)
        v2_id = _seed_version(db, definition_id=def_id, version_number=2, rules=rules_v2)

    r = client.get(f"/api/mappings/{def_id}/versions/{v1_id}/diff/{v2_id}")
    assert r.status_code == 200
    data = r.json()

    assert data["definition_id"] == def_id
    assert data["version_a"] == v1_id
    assert data["version_b"] == v2_id

    added_targets = {r["target"] for r in data["added"]}
    removed_targets = {r["target"] for r in data["removed"]}
    modified_targets = {r["target"] for r in data["modified"]}

    assert added_targets == {"newly_added"}
    assert removed_targets == {"to_be_removed"}
    assert modified_targets == {"to_be_modified"}
    assert data["reordered_only"] is False


def test_diff_endpoint_404_on_invalid_definition(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.get("/api/mappings/nonexistent-def/versions/va/diff/vb")
    assert r.status_code == 404


def test_diff_endpoint_404_on_version_not_in_definition(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        def1 = _seed_definition(db, vendor="sophos", event_type="sophos.alert", class_uid=2004)
        def2 = _seed_definition(db, vendor="sophos", event_type="sophos.case", class_uid=2005)
        v1_in_def1 = _seed_version(db, definition_id=def1, version_number=1, rules=[])
        v1_in_def2 = _seed_version(db, definition_id=def2, version_number=1, rules=[])

    # version de def2 usada no diff de def1 → 404
    r = client.get(f"/api/mappings/{def1}/versions/{v1_in_def1}/diff/{v1_in_def2}")
    assert r.status_code == 404


def test_diff_endpoint_reordered_only_flag(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    rules_v1 = [
        {"target": "class_uid", "const": 2004},
        {"target": "severity_id", "source": "sev"},
    ]
    rules_v2 = [
        {"target": "severity_id", "source": "sev"},
        {"target": "class_uid", "const": 2004},
    ]

    with Session() as db:
        def_id = _seed_definition(db, vendor="sophos", event_type="sophos.alert", class_uid=2004)
        v1_id = _seed_version(db, definition_id=def_id, version_number=1, rules=rules_v1)
        v2_id = _seed_version(db, definition_id=def_id, version_number=2, rules=rules_v2)

    r = client.get(f"/api/mappings/{def_id}/versions/{v1_id}/diff/{v2_id}")
    assert r.status_code == 200
    data = r.json()
    assert data["reordered_only"] is True
    assert data["added"] == []
    assert data["removed"] == []
    assert data["modified"] == []


# ── Pacote 2 — Audit endpoint ─────────────────────────────────────────


def test_audit_endpoint_paginated_and_filtered(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        def_id = _seed_definition(db, vendor="sophos", event_type="sophos.alert", class_uid=2004)

        # Insere algumas entradas de audit diretamente
        for i in range(5):
            db.add(models.MappingAuditLog(
                mapping_definition_id=def_id,
                action="create_version" if i % 2 == 0 else "rollback",
                username="admin",
                user_role="admin",
            ))
        db.commit()

    # Lista sem filtro
    r = client.get(f"/api/mappings/{def_id}/audit")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 5
    assert len(data["items"]) == 5

    # Filtra por action
    r = client.get(f"/api/mappings/{def_id}/audit?action=create_version")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 3
    for item in data["items"]:
        assert item["action"] == "create_version"

    # Paginação
    r = client.get(f"/api/mappings/{def_id}/audit?limit=2&offset=0")
    assert r.status_code == 200
    data = r.json()
    assert len(data["items"]) == 2
    assert data["total"] == 5


def test_audit_endpoint_404_on_unknown_definition(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.get("/api/mappings/nonexistent-uuid/audit")
    assert r.status_code == 404


def test_audit_endpoint_filter_by_username(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        def_id = _seed_definition(db, vendor="sophos", event_type="sophos.alert", class_uid=2004)
        db.add(models.MappingAuditLog(
            mapping_definition_id=def_id,
            action="create_version",
            username="alice",
            user_role="engineer",
        ))
        db.add(models.MappingAuditLog(
            mapping_definition_id=def_id,
            action="rollback",
            username="bob",
            user_role="admin",
        ))
        db.commit()

    r = client.get(f"/api/mappings/{def_id}/audit?username=alice")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 1
    assert data["items"][0]["username"] == "alice"


# ── Pacote 2 — Drift actions gravam MappingAuditLog ──────────────────


def test_drift_ignore_writes_audit_log(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        fid = _seed_unknown_field(db, path="alert.extra.field")

    r = client.post(f"/api/drift/{fid}/ignore")
    assert r.status_code == 200

    with Session() as db:
        logs = db.query(models.MappingAuditLog).filter(
            models.MappingAuditLog.action == "ignore_field"
        ).all()

    assert len(logs) >= 1
    assert logs[0].username == "admin"


def test_drift_mark_mapped_writes_audit_log(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        fid = _seed_unknown_field(db, path="alert.another.field")

    r = client.post(f"/api/drift/{fid}/mark_mapped")
    assert r.status_code == 200

    with Session() as db:
        logs = db.query(models.MappingAuditLog).filter(
            models.MappingAuditLog.action == "mark_mapped"
        ).all()

    assert len(logs) >= 1


def test_drift_delete_writes_audit_log(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        fid = _seed_unknown_field(db, path="alert.to_delete.field")

    r = client.delete(f"/api/drift/{fid}")
    assert r.status_code == 204

    with Session() as db:
        logs = db.query(models.MappingAuditLog).filter(
            models.MappingAuditLog.action == "delete_field"
        ).all()

    assert len(logs) >= 1


# ── Pacote 2 — Quarantine discard grava MappingAuditLog ──────────────


def test_quarantine_discard_writes_audit_log(client_factory) -> None:
    factory, Session = client_factory
    client = factory()
    _bootstrap_admin(client)

    with Session() as db:
        now = datetime.utcnow()
        ev = models.QuarantineEvent(
            vendor="sophos",
            event_type="sophos.alert",
            raw_payload=json.dumps({"id": "audit-test"}),
            error_kind="map",
            expires_at=now + timedelta(days=7),
        )
        db.add(ev)
        db.commit()
        db.refresh(ev)
        eid = ev.id

    r = client.post(f"/api/quarantine/{eid}/discard")
    assert r.status_code == 204

    with Session() as db:
        logs = db.query(models.MappingAuditLog).filter(
            models.MappingAuditLog.action == "discard_quarantine"
        ).all()

    assert len(logs) >= 1
    import json as _json
    detail = _json.loads(logs[0].detail)
    assert detail["quarantine_event_id"] == eid


# ── GET /api/mappings/normalize/type-casts ────────────────────────────


def test_list_type_casts_requires_auth(client_factory) -> None:
    """Sem autenticação, deve retornar 401."""
    factory, _ = client_factory
    client = TestClient(app)  # sem bootstrap/login
    r = client.get("/api/mappings/normalize/type-casts")
    assert r.status_code == 401


def test_list_type_casts_returns_12_items(client_factory) -> None:
    """Deve retornar exatamente 12 casts (5 originais + 7 novos), ordenados."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.get("/api/mappings/normalize/type-casts")
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data) == 12, f"Esperava 12 casts, obteve {len(data)}: {[d['name'] for d in data]}"


def test_list_type_casts_sorted_by_name(client_factory) -> None:
    """Os casts devem vir em ordem alfabética crescente."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.get("/api/mappings/normalize/type-casts")
    assert r.status_code == 200, r.text
    names = [item["name"] for item in r.json()]
    assert names == sorted(names), f"Nomes não estão ordenados: {names}"


def test_list_type_casts_schema_fields(client_factory) -> None:
    """Cada item deve ter name, description e signature."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.get("/api/mappings/normalize/type-casts")
    assert r.status_code == 200, r.text
    for item in r.json():
        assert "name" in item, f"Campo 'name' ausente em: {item}"
        assert "description" in item, f"Campo 'description' ausente em: {item}"
        assert "signature" in item, f"Campo 'signature' ausente em: {item}"
        assert item["name"], "name não pode ser vazio"
        assert item["description"], "description não pode ser vazia"
        assert item["signature"], "signature não pode ser vazia"


def test_list_type_casts_contains_expected_names(client_factory) -> None:
    """Verifica que todos os 12 casts esperados estão presentes."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.get("/api/mappings/normalize/type-casts")
    assert r.status_code == 200, r.text
    names = {item["name"] for item in r.json()}

    expected = {
        "iso_to_epoch",
        "epoch_to_iso",
        "to_str",
        "to_int",
        "to_bool",
        "score_to_percent",
        "lowercase",
        "uppercase",
        "trim",
        "to_array",
        "dedup",
        "mitre_tactic_to_ocsf",
    }
    assert expected == names, f"Diferença: {expected.symmetric_difference(names)}"


# ── Fase 4.1a — default_hit_warnings in dry-run ───────────────────────


def test_dry_run_returns_default_hit_warnings(client_factory) -> None:
    """Rule whose source is missing in ALL samples appears in default_hit_warnings."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    # Three samples: 'id' is always present, 'nonexistent_field' is always absent.
    r = client.post(
        "/api/mappings/dry-run",
        json={
            "rules": _v2([
                {"target": "normalized.id", "source": "id", "default": "unknown"},
                {
                    "target": "normalized.ghost_field",
                    "source": "nonexistent_field",
                    "default": "fallback_value",
                },
            ]),
            "raw_events": [
                {"id": "evt-1"},
                {"id": "evt-2"},
                {"id": "evt-3"},
            ],
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["sample_size"] == 3
    assert data["ok_count"] == 3

    warnings = data["default_hit_warnings"]
    assert len(warnings) == 1, f"Expected 1 warning, got: {warnings}"
    w = warnings[0]
    assert w["target"] == "normalized.ghost_field"
    assert w["hit_rate"] == 1.0
    assert w["hit_count"] == 3
    assert w["sample_size"] == 3
    assert w["expected_always_default"] is False

    # 'normalized.id' resolves every time → must NOT be in warnings
    warning_targets = {w["target"] for w in warnings}
    assert "normalized.id" not in warning_targets


def test_dry_run_excludes_expected_always_default_from_warnings(client_factory) -> None:
    """Rule with expected_always_default=True does NOT appear in warnings even at 100%."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    # Use v2 dict shape: expected_always_default is a v2-only flag (Fix 4).
    r = client.post(
        "/api/mappings/dry-run",
        json={
            "rules": {
                "rules": [
                    {
                        "target": "normalized.placeholder",
                        "source": "always_missing",
                        "default": "N/A",
                        "expected_always_default": True,
                    },
                    {
                        "target": "normalized.also_missing",
                        "source": "also_always_missing",
                        "default": "N/A",
                        # No expected_always_default → should warn
                    },
                ],
            },
            "raw_events": [
                {"unrelated_field": "value1"},
                {"unrelated_field": "value2"},
            ],
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()

    warning_targets = {w["target"] for w in data["default_hit_warnings"]}
    # The suppressed rule must NOT appear
    assert "normalized.placeholder" not in warning_targets
    # The non-suppressed rule MUST appear
    assert "normalized.also_missing" in warning_targets


def test_dry_run_partial_default_hits_not_warned(client_factory) -> None:
    """Rule with <100% default hit rate does NOT appear in default_hit_warnings."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    # 2 samples: 'sometimes_field' present in first, absent in second → 50% hit rate.
    r = client.post(
        "/api/mappings/dry-run",
        json={
            "rules": _v2([
                {
                    "target": "normalized.sometimes_field",
                    "source": "sometimes_field",
                    "default": "fallback",
                },
            ]),
            "raw_events": [
                {"sometimes_field": "present"},  # no default hit
                {},                              # default hit
            ],
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    # 50% hit rate → below 100% threshold → no warning
    assert data["default_hit_warnings"] == []


def test_dry_run_no_samples_returns_empty_warnings(client_factory) -> None:
    """Empty sample set returns default_hit_warnings=[] (no division by zero)."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.post(
        "/api/mappings/dry-run",
        json={
            "rules": _v2([
                {
                    "target": "normalized.x",
                    "source": "x",
                    "default": 0,
                }
            ]),
            "raw_events": [],
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["sample_size"] == 0
    assert data["default_hit_warnings"] == []


def test_dry_run_default_hit_warnings_present_in_schema(client_factory) -> None:
    """DryRunResult always includes default_hit_warnings key (even when empty)."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.post(
        "/api/mappings/dry-run",
        json={
            "rules": _v2([{"target": "normalized.id", "source": "id"}]),
            "raw_events": [{"id": "abc"}],
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert "default_hit_warnings" in data
    assert data["default_hit_warnings"] == []
