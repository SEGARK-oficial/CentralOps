"""Captura ao vivo — escopo hierárquico, catálogo de vendors e contadores.

Cobre as três correções do endpoint de capture-sessions:

B1  Escopo: quem é autorizado na SUBÁRVORE também LÊ a subárvore. Uma sessão
    aberta na org PAI captura o tráfego das FILHAS (ring único), e o escopo é
    sempre intersectado com ``tenant.accessible_org_ids`` — nunca alcança uma org
    que o usuário já não podia ver.
B2  Catálogo de vendors derivado do registry, incluindo transporte PUSH
    (``fortinet_fortigate`` / ``windows_event_log`` via ``/api/ingest``).
B3  Contadores (``total_captured`` / ``outcome_counts``) para a UI distinguir
    "sessão ativa e nada aconteceu" de "houve tráfego".

Imports usam ``backend.app.*`` (gotcha .so dual-root). Redis é fakeredis.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Generator, List

import fakeredis.aioredis as fakeredis_aio
import pytest
from fakeredis import FakeServer
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.collectors import capture_session as cs
from backend.app.core import ee_hooks
from backend.app.db import models
from backend.app.db.database import Base, get_session
from backend.app.main import app
from backend.app.routers import collector_config

BASE = "/api/collectors/config"


@pytest.fixture()
def env(monkeypatch) -> Generator[Dict[str, Any], None, None]:
    """Admin global + admin escopado da org A + orgs A, B e C (C = filha de A).

    A hierarquia é escrita DIRETO no banco: em Community o materializador é FLAT
    (``parent=None``), e o que queremos exercitar aqui é a leitura da árvore.
    """
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

    # Redis compartilhado entre o router e as chamadas diretas de ``record``:
    # mesmo FakeServer, clientes distintos (o router fecha o seu no finally).
    server = FakeServer()

    async def _fake_client():
        return fakeredis_aio.FakeRedis(server=server, decode_responses=True)

    monkeypatch.setattr(collector_config, "_redis_client", _fake_client)

    clients: List[TestClient] = []

    def factory() -> TestClient:
        c = TestClient(app)
        clients.append(c)
        return c

    ga = factory()
    r = ga.post(
        "/api/auth/bootstrap",
        json={"username": "root", "password": "AdminPassword123!", "display_name": "Root"},
    )
    assert r.status_code in (200, 201), r.text

    def create_org(name: str) -> int:
        r = ga.post(
            "/api/organizations",
            json={"name": name, "slug": name.lower().replace(" ", "-")},
        )
        assert r.status_code in (200, 201), r.text
        return r.json()["id"]

    org_a, org_b, org_c = create_org("Org A"), create_org("Org B"), create_org("Org C")

    # C é filha de A (a materialização FLAT do Core zera o parent no create).
    with TestingSession() as db:
        child = db.get(models.Organization, org_c)
        child.parent_organization_id = org_a
        db.commit()

    r = ga.post(
        "/api/auth/users",
        json={
            "username": "orgadmin",
            "password": "OrgAdminPassword123!",
            "role": "admin",
            "organization_id": org_a,
        },
    )
    assert r.status_code in (200, 201), r.text
    scoped = factory()
    r = scoped.post(
        "/api/auth/login",
        json={"username": "orgadmin", "password": "OrgAdminPassword123!"},
    )
    assert r.status_code == 200, r.text

    yield {
        "ga": ga,
        "scoped": scoped,
        "org_a": org_a,
        "org_b": org_b,
        "org_c": org_c,
        "server": server,
    }

    ee_hooks.reset_scope_resolver()
    for c in clients:
        c.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


def _event(org_id: int, vendor: str = "sophos") -> Dict[str, Any]:
    return {
        "_centralops": {"vendor": vendor, "organization_id": org_id, "event_id": "x"},
        "raw": {"msg": "hello"},
    }


def _record(
    server: FakeServer, batch: List[Dict[str, Any]], org_id: int, **kwargs: Any
) -> None:
    """Simula o tap do hot path gravando um lote (com desfecho) na org indicada."""

    async def _run() -> None:
        redis = fakeredis_aio.FakeRedis(server=server, decode_responses=True)
        try:
            await cs.record(redis, batch, org_id, **kwargs)
        finally:
            await redis.aclose()

    # O tap memoiza "org sem sessão" por ~2s (cache de processo) — os testes gravam
    # imediatamente após o start, então invalidamos para não ler cache velho.
    cs.reset_session_cache()
    asyncio.run(_run())


def _start(client: TestClient, org_id: int, **body: Any) -> Dict[str, Any]:
    payload = {"duration_seconds": 300}
    payload.update(body)
    r = client.post(f"{BASE}/capture-sessions?org_id={org_id}", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


# ── B1: escopo hierárquico ───────────────────────────────────────────────────


def test_session_scope_covers_authorized_subtree(env) -> None:
    """Admin global abrindo captura no PAI cobre a FILHA (e só ela)."""
    session = _start(env["ga"], env["org_a"])
    assert sorted(session["scope_org_ids"]) == sorted([env["org_a"], env["org_c"]])
    assert env["org_b"] not in session["scope_org_ids"]


def test_parent_session_captures_child_traffic(env) -> None:
    session = _start(env["ga"], env["org_a"])
    _record(env["server"], [_event(env["org_c"])], env["org_c"])

    r = env["ga"].get(
        f"{BASE}/capture-sessions/{session['id']}/events?org_id={env['org_a']}"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1
    assert body["events"][0]["organization_id"] == env["org_c"]
    assert body["total_captured"] == 1


def test_unrelated_org_traffic_is_not_captured(env) -> None:
    """Org fora da subárvore (B) não entra no ring — sem vazamento cross-tenant."""
    session = _start(env["ga"], env["org_a"])
    _record(env["server"], [_event(env["org_b"])], env["org_b"])

    r = env["ga"].get(
        f"{BASE}/capture-sessions/{session['id']}/events?org_id={env['org_a']}"
    )
    assert r.json()["count"] == 0
    assert r.json()["total_captured"] == 0


def test_scope_is_intersected_with_user_access(env) -> None:
    """Escopo = subárvore ∩ orgs acessíveis. Uma org acessível FORA da subárvore
    (B) não é coberta, e a subárvore só é coberta se o usuário a alcança."""
    org_a, org_b, org_c = env["org_a"], env["org_b"], env["org_c"]

    # Community FLAT (sem resolver EE): o admin escopado só alcança a própria org
    # ⇒ nada de filha no escopo (fail-closed).
    session = _start(env["scoped"], org_a)
    assert session["scope_org_ids"] == [org_a]

    # Com resolver subtree-aware (Enterprise) o usuário alcança A, B e C — mas só
    # A e C estão na subárvore de A.
    ee_hooks.reset_scope_resolver()
    ee_hooks.register_scope_resolver(lambda user, session_: {org_a, org_b, org_c})
    try:
        session = _start(env["scoped"], org_a)
        assert sorted(session["scope_org_ids"]) == sorted([org_a, org_c])
    finally:
        ee_hooks.reset_scope_resolver()


def test_child_org_does_not_see_parent_session(env) -> None:
    """O fan-out do índice não expõe a sessão do PAI para a org FILHA."""
    session = _start(env["ga"], env["org_a"])

    r = env["ga"].get(f"{BASE}/capture-sessions?org_id={env['org_c']}")
    assert r.status_code == 200, r.text
    assert [s["id"] for s in r.json()["sessions"]] == []

    # E o dono continua listando a própria sessão, com o escopo explícito.
    r = env["ga"].get(f"{BASE}/capture-sessions?org_id={env['org_a']}")
    listed = r.json()["sessions"]
    assert [s["id"] for s in listed] == [session["id"]]
    assert sorted(listed[0]["scope_org_ids"]) == sorted([env["org_a"], env["org_c"]])


def test_scoped_admin_cannot_capture_foreign_org(env) -> None:
    r = env["scoped"].post(
        f"{BASE}/capture-sessions?org_id={env['org_b']}", json={"duration_seconds": 60}
    )
    assert r.status_code == 403, r.text


def test_delete_clears_the_whole_scope_index(env) -> None:
    session = _start(env["ga"], env["org_a"])
    r = env["ga"].delete(
        f"{BASE}/capture-sessions/{session['id']}?org_id={env['org_a']}"
    )
    assert r.status_code == 204, r.text

    async def _members() -> set:
        redis = fakeredis_aio.FakeRedis(server=env["server"], decode_responses=True)
        try:
            return await redis.smembers(cs._org_index_key(env["org_c"]))
        finally:
            await redis.aclose()

    assert asyncio.run(_members()) == set()


# ── B2: catálogo de vendors (pull + push) ────────────────────────────────────


def test_capture_vendor_catalog_includes_push_sources(env) -> None:
    r = env["ga"].get(f"{BASE}/capture-vendors")
    assert r.status_code == 200, r.text
    body = r.json()
    by_vendor = {v["vendor"]: v for v in body["vendors"]}
    assert body["count"] == len(body["vendors"]) > 0

    # PUSH (/api/ingest) — o buraco que a lista antiga tinha.
    assert by_vendor["fortinet_fortigate"]["transport"] == "push"
    assert by_vendor["windows_event_log"]["transport"] == "push"
    # PULL segue presente.
    assert by_vendor["sophos"]["transport"] == "pull"
    # Streams vêm do registry (sem hardcode).
    assert "traffic" in by_vendor["fortinet_fortigate"]["streams"]
    assert by_vendor["fortinet_fortigate"]["display_name"]


def test_capture_vendor_catalog_matches_registry(env) -> None:
    from backend.app.collectors import registry

    expected = {r.platform for r in registry.all_registrations()} | {
        p.platform for p in registry.all_platforms()
    }
    got = {v["vendor"] for v in env["ga"].get(f"{BASE}/capture-vendors").json()["vendors"]}
    assert got == expected


# ── B3: contadores ───────────────────────────────────────────────────────────


def test_active_session_without_traffic_is_honest(env) -> None:
    session = _start(env["ga"], env["org_a"])
    body = env["ga"].get(
        f"{BASE}/capture-sessions/{session['id']}/events?org_id={env['org_a']}"
    ).json()
    assert body["count"] == 0
    assert body["total_captured"] == 0
    assert body["outcome_counts"] == {}
    assert body["session_status"] == "active"


def test_outcome_counts_breakdown(env) -> None:
    """O que NÃO foi entregue tem que aparecer separado do que foi."""
    org_a = env["org_a"]
    session = _start(env["ga"], org_a)
    _record(
        env["server"],
        [_event(org_a)],
        org_a,
        outcome=cs.OUTCOME_DELIVERED,
        destination_id="7",
    )
    _record(
        env["server"],
        [_event(org_a), _event(org_a)],
        org_a,
        outcome=cs.OUTCOME_DROPPED,
        detail="rota noise-filter",
    )
    _record(env["server"], [_event(org_a)], org_a, outcome=cs.OUTCOME_QUARANTINED)

    body = env["ga"].get(
        f"{BASE}/capture-sessions/{session['id']}/events?org_id={org_a}"
    ).json()
    assert body["count"] == 4
    assert body["total_captured"] == 4
    assert body["outcome_counts"] == {"delivered": 1, "dropped": 2, "quarantined": 1}

    by_outcome = {e["outcome"]: e for e in body["events"]}
    assert by_outcome["delivered"]["destination_id"] == "7"
    assert by_outcome["dropped"]["detail"] == "rota noise-filter"
    assert by_outcome["quarantined"]["destination_id"] is None


def test_event_without_outcome_is_reported_as_unknown(env) -> None:
    """Entrada legada (tap antigo, sem desfecho) não vira "delivered" por chute."""
    org_a = env["org_a"]
    session = _start(env["ga"], org_a)

    async def _push_legacy() -> None:
        import json

        redis = fakeredis_aio.FakeRedis(server=env["server"], decode_responses=True)
        try:
            await redis.lpush(
                cs._events_key(session["id"]),
                json.dumps({"event": _event(org_a), "vendor": "sophos"}),
            )
        finally:
            await redis.aclose()

    asyncio.run(_push_legacy())
    body = env["ga"].get(
        f"{BASE}/capture-sessions/{session['id']}/events?org_id={org_a}"
    ).json()
    assert body["outcome_counts"] == {"unknown": 1}


def test_total_captured_survives_ring_trim(env) -> None:
    """``total_captured`` conta a sessão inteira, mesmo com o ring podado."""
    session = _start(env["ga"], env["org_a"], ring_size=2)
    _record(env["server"], [_event(env["org_a"]) for _ in range(5)], env["org_a"])
    body = env["ga"].get(
        f"{BASE}/capture-sessions/{session['id']}/events?org_id={env['org_a']}"
    ).json()
    assert body["count"] == 2
    assert body["total_captured"] == 5


def test_session_level_outcome_counters_are_exposed_when_present(env) -> None:
    """Contadores por desfecho no meta (``outcome:<nome>``) sobem para a sessão.

    Enquanto o engine não os mantiver o campo sai vazio (nunca um palpite) — a UI
    trata como opcional."""
    session = _start(env["ga"], env["org_a"])
    listed = env["ga"].get(f"{BASE}/capture-sessions?org_id={env['org_a']}").json()
    assert listed["sessions"][0]["outcome_counts"] == {}

    async def _seed_counters() -> None:
        redis = fakeredis_aio.FakeRedis(server=env["server"], decode_responses=True)
        try:
            await redis.hset(
                cs._meta_key(session["id"]),
                mapping={"outcome:delivered": "9", "outcome:unrouted": "3"},
            )
        finally:
            await redis.aclose()

    asyncio.run(_seed_counters())
    listed = env["ga"].get(f"{BASE}/capture-sessions?org_id={env['org_a']}").json()
    assert listed["sessions"][0]["outcome_counts"] == {"delivered": 9, "unrouted": 3}


def test_stopped_session_status_is_reported(env) -> None:
    session = _start(env["ga"], env["org_a"])
    r = env["ga"].post(
        f"{BASE}/capture-sessions/{session['id']}/stop?org_id={env['org_a']}"
    )
    assert r.status_code == 204, r.text
    body = env["ga"].get(
        f"{BASE}/capture-sessions/{session['id']}/events?org_id={env['org_a']}"
    ).json()
    assert body["session_status"] == "stopped"
