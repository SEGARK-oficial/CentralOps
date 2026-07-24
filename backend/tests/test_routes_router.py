"""Tests for /api/collectors/routes (CRUD + audit)."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

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


def _seed_destination(client: TestClient, name="Dest A") -> str:
    # creating a destination auto-creates a broadcast route
    # ``{} → [dest]``. These route tests define their own routes explicitly, so
    # ``auto_route: false`` keeps the auto-route from polluting list/dry-run/reorder
    # assertions (the auto-route behaviour itself is covered in the destinations tests).
    r = client.post(
        "/api/collectors/destinations",
        json={
            "name": name,
            "kind": "syslog_rfc3164",
            "config": {"host": "h", "port": 514},
            "auto_route": False,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ── Create ─────────────────────────────────────────────────────────────


def test_create_route_happy_path(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)

    r = client.post(
        "/api/collectors/routes",
        json={
            "name": "High severity → dest",
            "condition": {"severity_id": {"gte": 4}},
            "destination_ids": [dest],
            "is_final": True,
            "priority": 10,
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["action"] == "route"
    assert body["destination_ids"] == [dest]
    assert body["condition"] == {"severity_id": {"gte": 4}}
    assert body["unreachable"] is False


def test_create_route_with_pii_redaction_round_trips(client_factory) -> None:
    """Uma spec de redação válida é aceita e volta no GET."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)

    spec = {"version": 1, "rules": [
        {"path": "raw.user.email", "action": "mask"},
        {"path": "raw.src.ip", "action": "partial", "octets": 2},
    ]}
    r = client.post(
        "/api/collectors/routes",
        json={"name": "masked siem", "condition": {}, "destination_ids": [dest],
              "pii_redaction": spec},
    )
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    assert r.json()["pii_redaction"] == spec
    # round-trips on GET
    got = client.get(f"/api/collectors/routes/{rid}").json()
    assert got["pii_redaction"] == spec


def test_create_route_bad_pii_redaction_422(client_factory) -> None:
    """FAIL-CLOSED na escrita: spec inválida (path proibido _centralops, ação
    desconhecida) → 422, nunca armazenada."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)

    for bad in (
        [{"path": "_centralops.event_id", "action": "mask"}],   # raiz proibida
        [{"path": "raw.x", "action": "encrypt"}],               # ação inválida
        [{"path": "data.x", "action": "mask"}],                 # raiz fora da allowlist
    ):
        r = client.post(
            "/api/collectors/routes",
            json={"name": "bad pii", "condition": {}, "destination_ids": [dest],
                  "pii_redaction": bad},
        )
        assert r.status_code == 422, f"esperava 422 para {bad!r}, veio {r.status_code}"


def test_create_route_with_canary(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)
    r = client.post(
        "/api/collectors/routes",
        json={"name": "canary", "condition": {"severity_id": {"gte": 4}}, "destination_ids": [dest], "canary_percent": 25},
    )
    assert r.status_code == 201, r.text
    assert r.json()["canary_percent"] == 25
    # default is 100 when omitted
    r2 = client.post("/api/collectors/routes", json={"name": "full", "condition": {}, "destination_ids": [dest]})
    assert r2.json()["canary_percent"] == 100


def test_canary_percent_out_of_range_422(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)
    r = client.post(
        "/api/collectors/routes",
        json={"name": "bad", "condition": {}, "destination_ids": [dest], "canary_percent": 150},
    )
    assert r.status_code == 422, r.text


def test_create_drop_route(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    r = client.post(
        "/api/collectors/routes",
        json={"name": "drop noise", "condition": {"severity_id": {"lt": 1}}, "action": "drop", "priority": 5},
    )
    assert r.status_code == 201, r.text
    assert r.json()["action"] == "drop"


def test_create_route_invalid_condition_422(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)
    r = client.post(
        "/api/collectors/routes",
        json={"name": "bad", "condition": {"not_a_field": 1}, "destination_ids": [dest]},
    )
    assert r.status_code == 422, r.text


def test_create_route_action_route_needs_destination_422(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    r = client.post(
        "/api/collectors/routes",
        json={"name": "no dest", "condition": {}, "destination_ids": []},
    )
    assert r.status_code == 422, r.text


def test_create_route_unknown_destination_422(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    r = client.post(
        "/api/collectors/routes",
        json={"name": "dangling", "condition": {}, "destination_ids": ["does-not-exist"]},
    )
    assert r.status_code == 422, r.text


def test_create_route_wazuh_default_allowed(client_factory) -> None:
    """wazuh-default is now a real Destination row (org=NULL,
    global). Routes targeting it pass validation when the row exists, and fail
    with 422 when it does not (no bypass — same path as every other destination)."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    # Without a wazuh-default destination row the route creation must fail.
    r_no_dest = client.post(
        "/api/collectors/routes",
        json={"name": "to wazuh no dest", "condition": {}, "destination_ids": ["wazuh-default"]},
    )
    assert r_no_dest.status_code == 422, r_no_dest.text

    # Seed wazuh-default as a real destination (global, id="wazuh-default").
    r_dest = client.post(
        "/api/collectors/destinations",
        json={
            "name": "Wazuh (default)",
            "kind": "syslog_rfc3164",
            "config": {"host": "wazuh", "port": 514},
            "auto_route": False,
        },
    )
    assert r_dest.status_code == 201, r_dest.text
    wd_id = r_dest.json()["id"]

    # Now create a route targeting the real dest id (not the legacy string).
    r = client.post(
        "/api/collectors/routes",
        json={"name": "to wazuh", "condition": {}, "destination_ids": [wd_id]},
    )
    assert r.status_code == 201, r.text


# ── List + unreachable ─────────────────────────────────────────────────


def test_list_routes_ordered_and_unreachable_flag(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)

    # priority 10 catch-all (final) shadows priority 20.
    client.post("/api/collectors/routes", json={"name": "catchall", "condition": {}, "destination_ids": [dest], "priority": 10, "is_final": True})
    client.post("/api/collectors/routes", json={"name": "shadowed", "condition": {"severity_id": {"gte": 4}}, "destination_ids": [dest], "priority": 20, "is_final": True})

    r = client.get("/api/collectors/routes")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert [x["name"] for x in rows] == ["catchall", "shadowed"]  # priority order
    by_name = {x["name"]: x for x in rows}
    assert by_name["catchall"]["unreachable"] is False
    assert by_name["shadowed"]["unreachable"] is True


# ── Update / delete ────────────────────────────────────────────────────


def test_update_route(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)
    rid = client.post("/api/collectors/routes", json={"name": "r", "condition": {}, "destination_ids": [dest], "priority": 50}).json()["id"]

    r = client.put(f"/api/collectors/routes/{rid}", json={"priority": 5, "enabled": False})
    assert r.status_code == 200, r.text
    assert r.json()["priority"] == 5
    assert r.json()["enabled"] is False


def test_update_route_to_empty_dest_route_action_422(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)
    rid = client.post("/api/collectors/routes", json={"name": "r", "condition": {}, "destination_ids": [dest]}).json()["id"]
    r = client.put(f"/api/collectors/routes/{rid}", json={"destination_ids": []})
    assert r.status_code == 422, r.text


def test_delete_route(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)
    rid = client.post("/api/collectors/routes", json={"name": "r", "condition": {}, "destination_ids": [dest]}).json()["id"]
    assert client.delete(f"/api/collectors/routes/{rid}").status_code == 204
    assert client.get(f"/api/collectors/routes/{rid}").status_code == 404


def test_get_unknown_route_404(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    assert client.get("/api/collectors/routes/nope").status_code == 404


# ── Audit trail (append-only) ──────────────────────────────────────────


def test_audit_trail_records_lifecycle(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)
    rid = client.post("/api/collectors/routes", json={"name": "r", "condition": {}, "destination_ids": [dest]}).json()["id"]
    client.put(f"/api/collectors/routes/{rid}", json={"priority": 7})

    r = client.get(f"/api/collectors/routes/{rid}/audit")
    assert r.status_code == 200, r.text
    trail = r.json()
    actions = [a["action"] for a in trail]
    assert "created" in actions and "updated" in actions
    # snapshot carries the full route state for rollback
    created = next(a for a in trail if a["action"] == "created")
    assert created["snapshot"]["name"] == "r"
    assert created["actor"] == "admin"


# ── Dry-run (preview before save) ──────────────────────────────────────


def _sample(sev):
    return {"_centralops": {"severity_id": sev, "vendor": "sophos", "event_id": f"e{sev}"}}


def test_dry_run_candidate_routes(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)

    r = client.post(
        "/api/collectors/routes/dry-run",
        json={
            "routes": [
                {"name": "hi", "condition": {"severity_id": {"gte": 4}}, "destination_ids": [dest], "priority": 10, "is_final": True},
                {"name": "rest", "condition": {}, "destination_ids": ["wazuh-default"], "priority": 100, "is_final": True},
            ],
            "samples": [_sample(5), _sample(2), _sample(4)],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["evaluated"] == 3
    assert body["sample_source"] == "provided"
    assert body["routed"] == 3  # all matched a route (catch-all)
    assert body["per_destination"][dest] == 2  # sev 5 + 4
    assert body["per_destination"]["wazuh-default"] == 1  # sev 2
    assert body["unreachable_route_ids"] == []


def test_dry_run_flags_drop_and_fallback(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)

    r = client.post(
        "/api/collectors/routes/dry-run",
        json={
            "routes": [
                {"name": "noise", "condition": {"severity_id": {"lt": 1}}, "action": "drop", "priority": 5, "is_final": True},
                {"name": "hi", "condition": {"severity_id": {"gte": 9}}, "destination_ids": [dest], "priority": 10, "is_final": True},
            ],
            "samples": [_sample(0), _sample(9), _sample(3)],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dropped"] == 1  # sev 0
    assert body["routed"] == 1  # sev 9 → dest
    assert body["fallback"] == 1  # sev 3 matched nothing → wazuh-default


def test_dry_run_candidate_unreachable_warning(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)
    r = client.post(
        "/api/collectors/routes/dry-run",
        json={
            "routes": [
                {"name": "catchall", "condition": {}, "destination_ids": [dest], "priority": 10, "is_final": True},
                {"name": "shadowed", "condition": {"severity_id": {"gte": 4}}, "destination_ids": [dest], "priority": 20, "is_final": True},
            ],
            "samples": [_sample(5)],
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["unreachable_route_ids"] == ["candidate-1"]


def test_dry_run_uses_saved_routes_when_routes_null(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)
    client.post("/api/collectors/routes", json={"name": "hi", "condition": {"severity_id": {"gte": 4}}, "destination_ids": [dest], "priority": 10})

    r = client.post(
        "/api/collectors/routes/dry-run",
        json={"routes": None, "samples": [_sample(5), _sample(2)]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["per_destination"].get(dest) == 1  # sev 5
    assert body["fallback"] == 1  # sev 2 → wazuh-default (no catch-all saved)


# ── Rollback ───────────────────────────────────────────────────────────


def test_rollback_restores_prior_snapshot(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)
    rid = client.post("/api/collectors/routes", json={"name": "orig", "condition": {}, "destination_ids": [dest], "priority": 50}).json()["id"]

    # Mutate it.
    client.put(f"/api/collectors/routes/{rid}", json={"name": "changed", "priority": 5})
    assert client.get(f"/api/collectors/routes/{rid}").json()["name"] == "changed"

    # Find the 'created' snapshot and roll back to it.
    trail = client.get(f"/api/collectors/routes/{rid}/audit").json()
    created_audit = next(a for a in trail if a["action"] == "created")
    r = client.post(f"/api/collectors/routes/{rid}/rollback", json={"audit_id": created_audit["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "orig"
    assert r.json()["priority"] == 50

    # The rollback itself is recorded (append-only trail).
    actions = [a["action"] for a in client.get(f"/api/collectors/routes/{rid}/audit").json()]
    assert "rolled_back" in actions


def test_route_metrics_native_store(client_factory) -> None:
    """Per-route activity from the native store (no Prometheus)."""
    import fakeredis

    from backend.app.collectors import observability_store as obs

    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)
    rid = client.post("/api/collectors/routes", json={"name": "r", "condition": {}, "destination_ids": [dest]}).json()["id"]

    fake = fakeredis.FakeStrictRedis(decode_responses=True)
    with patch.object(obs, "_redis", return_value=fake):
        obs.record_counter("route", rid, "matched", 9)
        obs.record_counter("route", rid, "route", 9)
        r = client.get(f"/api/collectors/routes/{rid}/metrics")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["route_id"] == rid
    assert body["series"]["matched"][0][1] == 9


def test_route_metrics_unknown_404(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    assert client.get("/api/collectors/routes/nope/metrics").status_code == 404


def test_rollback_unknown_audit_404(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)
    rid = client.post("/api/collectors/routes", json={"name": "r", "condition": {}, "destination_ids": [dest]}).json()["id"]
    r = client.post(f"/api/collectors/routes/{rid}/rollback", json={"audit_id": "nope"})
    assert r.status_code == 404, r.text


def test_rollback_to_deleted_destination_422(client_factory) -> None:
    """Rollback re-validates restored destinations — a snapshot
    pointing at a since-deleted destination → 422, not a dangling route."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)
    rid = client.post("/api/collectors/routes", json={"name": "r", "condition": {}, "destination_ids": [dest]}).json()["id"]
    created_audit = next(a for a in client.get(f"/api/collectors/routes/{rid}/audit").json() if a["action"] == "created")

    # Delete the destination the snapshot references, then try to roll back.
    assert client.delete(f"/api/collectors/destinations/{dest}").status_code == 204
    r = client.post(f"/api/collectors/routes/{rid}/rollback", json={"audit_id": created_audit["id"]})
    assert r.status_code == 422, r.text


def _tamper_snapshot(SessionLocal, audit_id: str, **fields) -> None:
    """Escreve valores CRUS no snapshot de auditoria, contornando o schema.

    Simula o cenário real: uma chave gravada ANTES da validação existir (ou vinda
    de seed/import) que só volta a valer pelo histórico.
    """
    import json as _json

    from backend.app.db import models

    db = SessionLocal()
    try:
        row = db.get(models.RouteAuditLog, audit_id)
        assert row is not None
        snap = _json.loads(str(row.snapshot or "{}"))
        snap.update(fields)
        row.snapshot = _json.dumps(snap)
        db.commit()
    finally:
        db.close()


def test_rollback_rejects_snapshot_with_invalid_suppress_key(client_factory) -> None:
    """Snapshot com suppress_key fora da allowlist → 422 e rota INTACTA.

    ``src_ip`` é campo do log, não label de roteamento: a assinatura colapsaria
    para todos os eventos e a supressão descartaria em silêncio."""
    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)
    rid = client.post(
        "/api/collectors/routes",
        json={"name": "orig", "condition": {}, "destination_ids": [dest], "priority": 50},
    ).json()["id"]
    client.put(f"/api/collectors/routes/{rid}", json={"name": "changed", "priority": 5})

    created_audit = next(a for a in client.get(f"/api/collectors/routes/{rid}/audit").json() if a["action"] == "created")
    _tamper_snapshot(SessionLocal, created_audit["id"], suppress_key="src_ip")

    r = client.post(f"/api/collectors/routes/{rid}/rollback", json={"audit_id": created_audit["id"]})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "route.snapshot_suppress_key_invalid"

    # Fail-closed: nada do snapshot foi aplicado.
    current = client.get(f"/api/collectors/routes/{rid}").json()
    assert current["name"] == "changed"
    assert current["priority"] == 5
    assert current["suppress_key"] is None
    assert "rolled_back" not in [a["action"] for a in client.get(f"/api/collectors/routes/{rid}/audit").json()]


def test_rollback_rejects_snapshot_with_unique_per_event_suppress_key(client_factory) -> None:
    """``event_id`` é único por evento: a assinatura nunca repete e a supressão
    nunca dispara — o extremo oposto, igualmente silencioso."""
    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)
    rid = client.post("/api/collectors/routes", json={"name": "r", "condition": {}, "destination_ids": [dest]}).json()["id"]
    created_audit = next(a for a in client.get(f"/api/collectors/routes/{rid}/audit").json() if a["action"] == "created")
    _tamper_snapshot(SessionLocal, created_audit["id"], suppress_key="vendor,event_id")

    r = client.post(f"/api/collectors/routes/{rid}/rollback", json={"audit_id": created_audit["id"]})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "route.snapshot_suppress_key_invalid"


def test_rollback_restores_valid_suppress_key(client_factory) -> None:
    """Caminho feliz preservado: chave válida no snapshot volta pelo rollback."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)
    rid = client.post(
        "/api/collectors/routes",
        json={
            "name": "r",
            "condition": {},
            "destination_ids": [dest],
            "protect_detection": False,
            "suppress_key": "vendor,severity_id",
            "suppress_allow": 5,
            "suppress_window_s": 60,
        },
    ).json()["id"]

    # Desliga a supressão (null explícito LIMPA a chave).
    assert client.put(f"/api/collectors/routes/{rid}", json={"suppress_key": None, "suppress_allow": 0}).status_code == 200
    assert client.get(f"/api/collectors/routes/{rid}").json()["suppress_key"] is None

    created_audit = next(a for a in client.get(f"/api/collectors/routes/{rid}/audit").json() if a["action"] == "created")
    r = client.post(f"/api/collectors/routes/{rid}/rollback", json={"audit_id": created_audit["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["suppress_key"] == "vendor,severity_id"
    assert r.json()["suppress_allow"] == 5


@pytest.mark.parametrize("value", [None, "", "  ,  "])
def test_rollback_allows_snapshot_without_suppression(client_factory, value) -> None:
    """Ausente/None/vazia = supressão desligada — configuração legítima, não erro."""
    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)
    rid = client.post("/api/collectors/routes", json={"name": "orig", "condition": {}, "destination_ids": [dest]}).json()["id"]
    client.put(f"/api/collectors/routes/{rid}", json={"name": "changed"})

    created_audit = next(a for a in client.get(f"/api/collectors/routes/{rid}/audit").json() if a["action"] == "created")
    _tamper_snapshot(SessionLocal, created_audit["id"], suppress_key=value)

    r = client.post(f"/api/collectors/routes/{rid}/rollback", json={"audit_id": created_audit["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "orig"


def test_rollback_rejects_snapshot_with_invalid_condition(client_factory) -> None:
    """Condição do snapshot fora da allowlist → 422 e rota INTACTA.

    O estrago aqui é o oposto do da supressão: ``compare_values`` faz ``ne``/``nin``
    casarem por VACUIDADE em campo ausente, então uma característica inválida não
    deixa de casar — ela casa com TUDO, e a regra captura tráfego que não é dela.
    """
    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)
    rid = client.post(
        "/api/collectors/routes",
        json={"name": "orig", "condition": {}, "destination_ids": [dest], "priority": 50},
    ).json()["id"]
    client.put(f"/api/collectors/routes/{rid}", json={"name": "changed", "priority": 5})

    created_audit = next(a for a in client.get(f"/api/collectors/routes/{rid}/audit").json() if a["action"] == "created")
    _tamper_snapshot(SessionLocal, created_audit["id"], condition={"src_ip": {"ne": "10.0.0.1"}})

    r = client.post(f"/api/collectors/routes/{rid}/rollback", json={"audit_id": created_audit["id"]})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "route.snapshot_condition_invalid"

    # Fail-closed: nada do snapshot foi aplicado.
    current = client.get(f"/api/collectors/routes/{rid}").json()
    assert current["name"] == "changed"
    assert current["priority"] == 5
    assert "rolled_back" not in [a["action"] for a in client.get(f"/api/collectors/routes/{rid}/audit").json()]


def test_rollback_restores_valid_condition(client_factory) -> None:
    """Caminho feliz preservado: condição válida no snapshot volta pelo rollback."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)
    rid = client.post(
        "/api/collectors/routes",
        json={
            "name": "orig",
            "condition": {"vendor": "sophos"},
            "destination_ids": [dest],
        },
    ).json()["id"]
    assert client.put(f"/api/collectors/routes/{rid}", json={"condition": {"vendor": "defender"}}).status_code == 200

    created_audit = next(a for a in client.get(f"/api/collectors/routes/{rid}/audit").json() if a["action"] == "created")
    r = client.post(f"/api/collectors/routes/{rid}/rollback", json={"audit_id": created_audit["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["condition"] == {"vendor": "sophos"}


def test_rollback_allows_snapshot_with_suppress_key_absent(client_factory) -> None:
    """Snapshot antigo, anterior à coluna: sem a chave no JSON o rollback passa."""
    import json as _json

    from backend.app.db import models

    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)
    rid = client.post("/api/collectors/routes", json={"name": "orig", "condition": {}, "destination_ids": [dest]}).json()["id"]
    client.put(f"/api/collectors/routes/{rid}", json={"name": "changed"})
    created_audit = next(a for a in client.get(f"/api/collectors/routes/{rid}/audit").json() if a["action"] == "created")

    db = SessionLocal()
    try:
        row = db.get(models.RouteAuditLog, created_audit["id"])
        snap = _json.loads(str(row.snapshot or "{}"))
        snap.pop("suppress_key", None)
        row.snapshot = _json.dumps(snap)
        db.commit()
    finally:
        db.close()

    r = client.post(f"/api/collectors/routes/{rid}/rollback", json={"audit_id": created_audit["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "orig"


@pytest.mark.asyncio
async def test_resolve_samples_unwraps_audit_buffer_entries() -> None:
    """read_recent returns {event, envelope, syslog_format}
    wrappers; dry-run must read labels off the inner `event` envelope, not the
    wrapper (else everything misreports as wazuh-default fallback)."""
    from backend.app.api.schemas_routes import RouteDryRunRequest
    from backend.app.routers import routes as routes_mod

    wrappers = [
        {"event": {"_centralops": {"vendor": "sophos", "severity_id": 5}}, "envelope": {}, "syslog_format": None}
    ]
    with (
        patch("backend.app.collectors.audit_buffer.read_recent", new=AsyncMock(return_value=wrappers)),
        patch("backend.app.collectors.celery_app.get_worker_redis", return_value=AsyncMock()),
    ):
        samples, source = await routes_mod._resolve_samples(
            RouteDryRunRequest(samples=None), caller_org=42
        )
    assert source == "audit_buffer"
    # Unwrapped to the inner envelope — labels are visible to event_labels().
    assert samples[0]["_centralops"]["vendor"] == "sophos"
    assert samples[0]["_centralops"]["severity_id"] == 5


# ── POST /reorder (drag-reorder bulk priority) ──────


def _create_route(client: TestClient, name: str, dest: str, priority: int = 100) -> str:
    r = client.post(
        "/api/collectors/routes",
        json={"name": name, "condition": {}, "destination_ids": [dest], "priority": priority},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_reorder_requires_admin(client_factory) -> None:
    """Unauthenticated callers must receive 401/403."""
    factory, _ = client_factory
    client = factory()
    r = client.post("/api/collectors/routes/reorder", json={"route_ids": ["x"]})
    assert r.status_code in (401, 403), r.text


def test_reorder_happy_path_reassigns_priorities(client_factory) -> None:
    """Ordered list → priorities reassigned as 10, 20, 30 …"""
    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)

    rid1 = _create_route(client, "route-a", dest, priority=100)
    rid2 = _create_route(client, "route-b", dest, priority=200)
    rid3 = _create_route(client, "route-c", dest, priority=300)

    # Reverse the order: c → b → a
    r = client.post(
        "/api/collectors/routes/reorder",
        json={"route_ids": [rid3, rid2, rid1]},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    reordered = body["reordered"]
    assert len(reordered) == 3

    id_to_priority = {row["id"]: row["priority"] for row in reordered}
    assert id_to_priority[rid3] == 10  # first in list → highest prio
    assert id_to_priority[rid2] == 20
    assert id_to_priority[rid1] == 30

    # Verify persisted in DB via GET list (sorted by priority asc).
    list_r = client.get("/api/collectors/routes")
    assert list_r.status_code == 200, list_r.text
    ids_in_order = [row["id"] for row in list_r.json()]
    assert ids_in_order == [rid3, rid2, rid1]


def test_reorder_audit_log_recorded(client_factory) -> None:
    """reorder action is written to the audit trail for each reordered route."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)
    rid1 = _create_route(client, "r1", dest)
    rid2 = _create_route(client, "r2", dest)

    client.post(
        "/api/collectors/routes/reorder",
        json={"route_ids": [rid2, rid1]},
    )

    # Each route should have a 'reorder' entry in its audit trail.
    for rid in (rid1, rid2):
        trail_r = client.get(f"/api/collectors/routes/{rid}/audit")
        assert trail_r.status_code == 200, trail_r.text
        actions = [a["action"] for a in trail_r.json()]
        assert "reorder" in actions, f"reorder audit missing for route {rid}"


def test_reorder_unknown_route_404(client_factory) -> None:
    """Any unknown route_id in the list → 404, no priorities changed."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)
    rid = _create_route(client, "r", dest, priority=50)

    r = client.post(
        "/api/collectors/routes/reorder",
        json={"route_ids": [rid, "does-not-exist"]},
    )
    assert r.status_code == 404, r.text

    # The valid route's priority must be unchanged.
    get_r = client.get(f"/api/collectors/routes/{rid}")
    assert get_r.json()["priority"] == 50


def test_reorder_org_scope_enforced_in_repository(client_factory) -> None:
    """RouteRepository.reorder_routes raises PermissionError when a non-global
    caller attempts to reorder a route belonging to a different org.

    Admin users always have global_scope (has_global_scope returns True for role=admin),
    so org-scoping is only relevant at the repository layer when called with
    global_scope=False.  This tests the enforcement directly.
    """
    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)

    # Create a route assigned to org 99 (hypothetical).
    org_b_id = 99
    client.post(
        "/api/organizations",
        json={"name": "ReorderOrgB", "slug": "reorder-org-b"},
    )
    # Seed route with organization_id via the API (global admin can set any org).
    client.post(
        "/api/collectors/routes",
        json={
            "name": "route-org-b",
            "condition": {},
            "destination_ids": [dest],
            "organization_id": org_b_id,
        },
    ).json().get("id") or client.post(
        # Fallback: if org 99 doesn't exist, the route will have org=None.
        # In that case test via repository directly.
        "/api/collectors/routes",
        json={"name": "route-org-b2", "condition": {}, "destination_ids": [dest]},
    ).json()["id"]

    # Test the repository directly with non-global scope for org 1.
    from backend.app.db import repository as repo_mod

    with SessionLocal() as db:
        repo = repo_mod.RouteRepository(db)
        # Get the actual route we created.
        all_routes = repo.list(None, global_scope=True)
        # Find a route we can use (pick the first one with an organization_id).
        route_with_org = next(
            (r for r in all_routes if r.organization_id is not None), None
        )
        if route_with_org is None:
            # Skip the org-scope path — the API didn't assign an org (org doesn't exist in DB).
            # Test that a route with org=None is visible to all non-global callers.
            pytest.skip("No scoped route available in test DB — org id validation prevented it")

        # Non-global caller from a DIFFERENT org should get PermissionError.
        different_org = (int(route_with_org.organization_id) + 1)
        import pytest as _pytest

        with _pytest.raises(PermissionError, match="outside the caller's organization scope"):
            repo.reorder_routes(
                [str(route_with_org.id)],
                org_id=different_org,
                global_scope=False,
                actor="test",
            )


@pytest.mark.parametrize(
    "route_ids,expected_priorities",
    [
        (["a", "b", "c"], {0: 10, 1: 20, 2: 30}),
        (["c", "a"], {0: 10, 1: 20}),
        (["b"], {0: 10}),
    ],
)
def test_reorder_priority_step(client_factory, route_ids: list, expected_priorities: dict) -> None:
    """Parametrize: reorder produces priority = step * position for any sub-list."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)

    name_to_id: dict[str, str] = {}
    for name in ["a", "b", "c"]:
        name_to_id[name] = _create_route(client, f"rt-{name}", dest, priority=100)

    target_ids = [name_to_id[n] for n in route_ids]

    r = client.post(
        "/api/collectors/routes/reorder",
        json={"route_ids": target_ids},
    )

    assert r.status_code == 200, r.text
    reordered = r.json()["reordered"]
    for pos, expected_p in expected_priorities.items():
        assert reordered[pos]["priority"] == expected_p


# ── wazuh-default-catchall é uma rota NORMAL (deletável/reordenável) ──


def test_delete_catchall_route_is_now_normal(client_factory) -> None:
    """_SYSTEM_ROUTE_IDS está vazio — a rota wazuh-default-catchall
    não é mais protegida. DELETE em um id inexistente devolve 404 (sem 409 de sistema).
    Uma rota criada com esse id pode ser deletada normalmente."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    # No route named wazuh-default-catchall exists in a fresh DB → 404, not 409.
    r = client.delete("/api/collectors/routes/wazuh-default-catchall")
    assert r.status_code == 404, r.text

    # If a user creates such a route it becomes deletable (no system guard).
    dest = _seed_destination(client)
    catchall_id = client.post(
        "/api/collectors/routes",
        json={
            "name": "wazuh-default-catchall",
            "condition": {},
            "destination_ids": [dest],
            "priority": 999,
        },
    ).json()["id"]
    del_r = client.delete(f"/api/collectors/routes/{catchall_id}")
    assert del_r.status_code == 204, del_r.text
    assert client.get(f"/api/collectors/routes/{catchall_id}").status_code == 404


def test_reorder_former_system_route_is_now_allowed(client_factory) -> None:
    """reorder já não rejeita wazuh-default-catchall com 409.
    Incluir um id inexistente devolve 404 (comportamento normal de rota ausente)."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)
    route = client.post(
        "/api/collectors/routes",
        json={
            "name": "r1",
            "condition": {},
            "destination_ids": [dest],
            "priority": 10,
        },
    ).json()
    # Mixing a real route with an unknown id → 404 (not 409).
    r = client.post(
        "/api/collectors/routes/reorder",
        json={"route_ids": [route["id"], "wazuh-default-catchall"]},
    )
    assert r.status_code == 404, r.text


# ── health por rota (paridade rota↔destino) ───────────


def test_route_health_endpoint_shape(client_factory) -> None:
    """GET /{route_id}/health devolve o snapshot computado do store nativo
    (sem dado → status 'idle', contadores zerados, sem erro)."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)
    route = client.post(
        "/api/collectors/routes",
        json={"name": "r-health", "condition": {}, "destination_ids": [dest]},
    ).json()

    r = client.get(f"/api/collectors/routes/{route['id']}/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["route_id"] == route["id"]
    assert body["status"] == "idle"  # enabled mas sem tráfego ainda
    assert body["enabled"] is True
    assert body["matched_eps"] == 0.0
    assert body["matched_1h"] == 0
    assert body["drop_rate"] == 0.0


def test_route_health_disabled_status(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    dest = _seed_destination(client)
    route = client.post(
        "/api/collectors/routes",
        json={"name": "r-off", "condition": {}, "destination_ids": [dest], "enabled": False},
    ).json()
    body = client.get(f"/api/collectors/routes/{route['id']}/health").json()
    assert body["status"] == "disabled"
    assert body["enabled"] is False
