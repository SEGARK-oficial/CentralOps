"""RBAC de LEITURA de destinos e rotas — ``destination.read`` / ``route.read``.

Antes, TODO GET de destino/rota exigia ``require_admin_user`` (= ``user.manage``)
por falta de permissão fina: ler contagem de DLQ ou estado de circuit breaker
exigia um token capaz de CRIAR E REMOVER USUÁRIOS. Um alvo desproporcional para
o caso de uso real — Zabbix, script de health check, dashboard externo.

Invariantes travadas aqui:
  1. As duas permissões existem e são concedidas a partir de VIEWER — é o que a
     matriz de docs-site/docs/concepts/rbac.md já afirmava; o código divergia.
  2. Um token com APENAS ``destination.read`` lê os GETs de destino e recebe 403
     em toda ESCRITA — e em ``/api/auth/users`` (o ponto da mudança).
  3. ``destination.read`` e ``route.read`` são DISTINTAS: uma não abre a outra.
  4. Os GETs que expõem outra classe de dado (``/{id}/tap`` — tap ao vivo do que
     está trafegando — e o audit de credencial) seguem exigindo admin.
"""

from __future__ import annotations

import os
from typing import Any, Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

from backend.app.core.auth import (
    ROLE_PERMISSIONS,
    Permission,
    UserRole,
    effective_scopes,
)
from backend.app.db.database import Base, get_session
from backend.app.main import app


# ── Matriz papel × permissão (unit, sem TestClient) ───────────────────


@pytest.mark.parametrize(
    "role",
    [UserRole.VIEWER, UserRole.OPERATOR, UserRole.ENGINEER, UserRole.ADMIN],
)
def test_todo_papel_le_destino_e_rota(role: str) -> None:
    """Paridade com a matriz documentada: "Ver destinos" / "Ver regras de
    roteamento" = ✓ para os quatro papéis."""
    perms = ROLE_PERMISSIONS[role]
    assert Permission.DESTINATION_READ in perms, f"{role} deveria ler destino"
    assert Permission.ROUTE_READ in perms, f"{role} deveria ler rota"


@pytest.mark.parametrize(
    "role", [UserRole.VIEWER, UserRole.OPERATOR, UserRole.ENGINEER]
)
def test_ler_destino_e_rota_nao_arrasta_user_manage(role: str) -> None:
    """A permissão fina NÃO pode vir acompanhada da permissão de gerenciar
    usuários — é exatamente a over-permissão que esta mudança desfaz."""
    perms = ROLE_PERMISSIONS[role]
    assert Permission.USER_MANAGE not in perms


def test_permissoes_novas_nao_escalam_via_token() -> None:
    """Token de um admin pedindo só ``destination.read`` fica com ISSO e nada mais
    — em especial, sem ``user.manage``."""
    perms = effective_scopes(UserRole.ADMIN, [Permission.DESTINATION_READ])
    assert perms == frozenset({Permission.DESTINATION_READ})
    assert Permission.USER_MANAGE not in perms
    assert Permission.ROUTE_READ not in perms


def test_viewer_com_token_de_leitura_mantem_as_duas_permissoes() -> None:
    perms = effective_scopes(
        UserRole.VIEWER, [Permission.DESTINATION_READ, Permission.ROUTE_READ]
    )
    assert perms == frozenset(
        {Permission.DESTINATION_READ, Permission.ROUTE_READ}
    )


# ── Fixture E2E ───────────────────────────────────────────────────────


@pytest.fixture()
def setup() -> Generator[Any, None, None]:
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
    client = TestClient(app)

    # Reset do rate limiter — evita contaminação cruzada entre módulos.
    from backend.app.core.rate_limiter import token_rate_limiter

    token_rate_limiter._windows.clear()

    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPassword123!"},
    )
    assert r.status_code == 200, r.text

    yield client, TestingSession

    client.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


def _seed_destination(client: TestClient, name: str = "Zabbix Dest") -> str:
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


def _seed_route(client: TestClient, dest_id: str, name: str = "Zabbix Route") -> str:
    r = client.post(
        "/api/collectors/routes",
        json={"name": name, "condition": {}, "destination_ids": [dest_id]},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _mint_token(client: TestClient, name: str, scopes: list[str]) -> str:
    """Cria um PAT com scopes restritos e faz logout (força caminho Bearer)."""
    r = client.post(
        "/api/v1/tokens",
        json={"name": name, "is_eternal": True, "scopes": scopes},
    )
    assert r.status_code == 201, r.text
    raw = r.json()["token"]
    client.post("/api/auth/logout")
    return raw


# ── O caso de uso do enunciado: token de monitoramento ────────────────


def test_token_so_com_destination_read_le_health_dlq_e_metrics(setup) -> None:
    """O cenário Zabbix: um token de LEITURA lê health/DLQ/breaker sem carregar
    poder de gerenciar usuários."""
    client, _ = setup
    dest_id = _seed_destination(client)
    token = _mint_token(client, "zabbix-ro", [Permission.DESTINATION_READ.value])
    h = {"Authorization": f"Bearer {token}"}

    for path in (
        "/api/collectors/destinations",
        "/api/collectors/destinations/health",
        "/api/collectors/destinations/destination-types",
        f"/api/collectors/destinations/{dest_id}",
        f"/api/collectors/destinations/{dest_id}/health",
        f"/api/collectors/destinations/{dest_id}/dlq",
        f"/api/collectors/destinations/{dest_id}/metrics",
    ):
        r = client.get(path, headers=h)
        assert r.status_code == 200, f"{path} deveria ser 200 — {r.status_code} {r.text[:200]}"

    # O payload de health de fato traz o que o monitoramento consome.
    body = client.get(f"/api/collectors/destinations/{dest_id}/health", headers=h).json()
    for field in ("status", "breaker_state", "dlq_total", "dlq_24h"):
        assert field in body, f"health sem campo {field}"


def test_token_de_leitura_recebe_403_em_escrita_de_destino(setup) -> None:
    """Contraparte obrigatória: leitura liberada NÃO libera escrita."""
    client, _ = setup
    dest_id = _seed_destination(client)
    token = _mint_token(client, "zabbix-ro", [Permission.DESTINATION_READ.value])
    h = {"Authorization": f"Bearer {token}"}

    r_create = client.post(
        "/api/collectors/destinations",
        json={
            "name": "should-fail",
            "kind": "syslog_rfc3164",
            "config": {"host": "h", "port": 514},
            "auto_route": False,
        },
        headers=h,
    )
    assert r_create.status_code == 403, r_create.text

    r_update = client.put(
        f"/api/collectors/destinations/{dest_id}",
        json={"name": "renamed"},
        headers=h,
    )
    assert r_update.status_code == 403, r_update.text

    r_delete = client.delete(f"/api/collectors/destinations/{dest_id}", headers=h)
    assert r_delete.status_code == 403, r_delete.text

    r_reprocess = client.post(
        f"/api/collectors/destinations/{dest_id}/dlq/reprocess",
        json={"limit": 1},
        headers=h,
    )
    assert r_reprocess.status_code == 403, r_reprocess.text


def test_token_de_leitura_nao_gerencia_usuarios(setup) -> None:
    """O ponto da mudança: ler DLQ deixou de exigir poder sobre usuários."""
    client, _ = setup
    _seed_destination(client)
    token = _mint_token(client, "zabbix-ro", [Permission.DESTINATION_READ.value])
    h = {"Authorization": f"Bearer {token}"}

    assert client.get("/api/auth/users", headers=h).status_code == 403
    r = client.post(
        "/api/auth/users",
        json={"username": "intruder", "password": "IntruderPwd123!", "role": "admin"},
        headers=h,
    )
    assert r.status_code == 403, r.text


def test_tap_ao_vivo_segue_exigindo_admin(setup) -> None:
    """``/{id}/tap`` devolve o que está TRAFEGANDO (payload ao vivo) — outra
    classe de dado, deliberadamente fora de ``destination.read``."""
    client, _ = setup
    dest_id = _seed_destination(client)
    token = _mint_token(client, "zabbix-ro", [Permission.DESTINATION_READ.value])
    h = {"Authorization": f"Bearer {token}"}

    assert client.get(f"/api/collectors/destinations/{dest_id}/tap", headers=h).status_code == 403
    assert (
        client.get(
            f"/api/collectors/destinations/{dest_id}/credential/audit", headers=h
        ).status_code
        == 403
    )


# ── route.read ────────────────────────────────────────────────────────


def test_token_so_com_route_read_le_topologia_e_flow(setup) -> None:
    client, _ = setup
    dest_id = _seed_destination(client)
    route_id = _seed_route(client, dest_id)
    token = _mint_token(client, "zabbix-routes", [Permission.ROUTE_READ.value])
    h = {"Authorization": f"Bearer {token}"}

    for path in (
        "/api/collectors/routes",
        "/api/collectors/routes/topology",
        "/api/collectors/routes/flow",
        f"/api/collectors/routes/{route_id}",
        f"/api/collectors/routes/{route_id}/health",
        f"/api/collectors/routes/{route_id}/metrics",
    ):
        r = client.get(path, headers=h)
        assert r.status_code == 200, f"{path} deveria ser 200 — {r.status_code} {r.text[:200]}"


def test_token_de_leitura_recebe_403_em_escrita_de_rota(setup) -> None:
    client, _ = setup
    dest_id = _seed_destination(client)
    route_id = _seed_route(client, dest_id)
    token = _mint_token(client, "zabbix-routes", [Permission.ROUTE_READ.value])
    h = {"Authorization": f"Bearer {token}"}

    r_create = client.post(
        "/api/collectors/routes",
        json={"name": "should-fail", "condition": {}, "destination_ids": [dest_id]},
        headers=h,
    )
    assert r_create.status_code == 403, r_create.text

    r_update = client.put(
        f"/api/collectors/routes/{route_id}",
        json={"name": "renamed"},
        headers=h,
    )
    assert r_update.status_code == 403, r_update.text

    assert client.delete(f"/api/collectors/routes/{route_id}", headers=h).status_code == 403


def test_as_duas_permissoes_sao_distintas(setup) -> None:
    """``destination.read`` não abre rota, e ``route.read`` não abre destino —
    senão a granularidade seria decorativa."""
    client, _ = setup
    dest_id = _seed_destination(client)
    route_id = _seed_route(client, dest_id)

    dest_only = _mint_token(client, "dest-only", [Permission.DESTINATION_READ.value])
    hd = {"Authorization": f"Bearer {dest_only}"}
    assert client.get("/api/collectors/routes/topology", headers=hd).status_code == 403
    assert client.get(f"/api/collectors/routes/{route_id}", headers=hd).status_code == 403

    # Novo login pra emitir o segundo token (o logout de _mint_token encerrou a sessão).
    client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "AdminPassword123!"},
    )
    route_only = _mint_token(client, "route-only", [Permission.ROUTE_READ.value])
    hr = {"Authorization": f"Bearer {route_only}"}
    assert client.get("/api/collectors/destinations/health", headers=hr).status_code == 403
    assert (
        client.get(f"/api/collectors/destinations/{dest_id}/dlq", headers=hr).status_code == 403
    )


# ── Sessão de cookie: papel não-admin lendo pela UI ───────────────────


def test_viewer_le_destinos_e_rotas_pela_sessao(setup) -> None:
    """Fecha a divergência código↔documentação: a matriz publicada diz que Viewer
    vê destinos e regras de roteamento. Antes, a UI de um viewer levava 403."""
    client, _ = setup
    dest_id = _seed_destination(client)
    route_id = _seed_route(client, dest_id)

    r = client.post(
        "/api/auth/users",
        json={"username": "viewer1", "password": "ViewerPwd123!", "role": "viewer"},
    )
    assert r.status_code == 200, r.text

    viewer = TestClient(app)
    try:
        r_login = viewer.post(
            "/api/auth/login",
            json={"username": "viewer1", "password": "ViewerPwd123!"},
        )
        assert r_login.status_code == 200, r_login.text

        for path in (
            "/api/collectors/destinations",
            "/api/collectors/destinations/health",
            f"/api/collectors/destinations/{dest_id}/health",
            "/api/collectors/routes",
            "/api/collectors/routes/topology",
            f"/api/collectors/routes/{route_id}/health",
        ):
            assert viewer.get(path).status_code == 200, f"{path} deveria ser 200 p/ viewer"

        # ...e continua sem poder escrever.
        assert (
            viewer.post(
                "/api/collectors/destinations",
                json={
                    "name": "nope",
                    "kind": "syslog_rfc3164",
                    "config": {"host": "h", "port": 514},
                    "auto_route": False,
                },
            ).status_code
            == 403
        )
        assert viewer.delete(f"/api/collectors/destinations/{dest_id}").status_code == 403
        assert viewer.get("/api/auth/users").status_code == 403
    finally:
        viewer.close()


def test_scopes_endpoint_expoe_as_permissoes_novas(setup) -> None:
    """A UI de PAT lista scopes a partir do enum — as duas precisam aparecer,
    senão não há como emitir o token de monitoramento pela interface."""
    client, _ = setup
    r = client.get("/api/v1/tokens/scopes")
    assert r.status_code == 200, r.text
    scopes = r.json()
    assert "destination.read" in scopes
    assert "route.read" in scopes
