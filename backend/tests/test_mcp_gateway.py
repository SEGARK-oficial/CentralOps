"""Gateway MCP em ``/api/mcp`` — Streamable HTTP autenticado pela PAT do analista.

O que estes testes provam, ponta a ponta (TestClient + SQLite):

* o toggle do banco governa a exposição (404 quando desligado);
* só Bearer autentica — cookie de sessão é recusado, PAT inválida é 401;
* ``mcp.use`` é exigida pela interseção papel × scopes do token;
* uma ferramenta atravessa o REST por loopback COMO o analista: a linha de
  auditoria da rota interna sai com o username dele e o User-Agent do MCP;
* o rate limit do token é consumido UMA vez por chamada MCP (a chamada
  interna não paga de novo);
* uma ferramenta cujo REST nega (403) devolve erro estruturado, não elevação;
* o modo SSE responde ``text/event-stream``.
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
ADMIN_PASSWORD = "AdminPassword123!"
USER_PASSWORD = "AnalystPassword123!"


@pytest.fixture()
def env(tmp_path):
    # SQLite em ARQUIVO, não ``:memory:`` + ``StaticPool``.
    #
    # ``:memory:`` exige StaticPool para que todas as sessões enxerguem o mesmo
    # banco — e StaticPool serve UMA única conexão a todas elas. Aqui isso não
    # é detalhe: além da sessão do request (injetada), o middleware de auditoria
    # abre a PRÓPRIA ``SessionLocal()`` para gravar a linha, e o gateway abre
    # mais uma. Duas Sessions sobre a mesma conexão disputam a transação: o
    # ``commit()`` de uma descarta a linha pendente da outra, e o ``refresh()``
    # que ``AuditLogRepository.add`` faz em seguida estoura com "Could not
    # refresh instance". Passava localmente por sorte de escalonamento e
    # reprovava na imagem compilada, onde o tempo é outro.
    #
    # Com arquivo cada Session tem conexão própria e transação isolada, que é o
    # que produção faz (Postgres, pool de verdade) e o que o dev já usa
    # (``backend/data/sophos.db``).
    engine = create_engine(
        f"sqlite:///{tmp_path / 'mcp-gateway-test.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
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

    # O gateway, a auditoria do MCP e o middleware de auditoria abrem sessão
    # fora do DI (``SessionLocal`` importado por nome em cada módulo).
    from backend.app import main as _main
    from backend.app.db import database as _database
    from backend.app.mcp import gateway as _gateway

    original_session_local = _database.SessionLocal
    _database.SessionLocal = TestingSession  # type: ignore[assignment]
    _gateway.SessionLocal = TestingSession  # type: ignore[assignment]
    _main.SessionLocal = TestingSession  # type: ignore[assignment]

    from backend.app.core.rate_limiter import token_rate_limiter
    token_rate_limiter._windows.clear()

    # ``with`` roda o lifespan: é lá que os gerenciadores do SDK sobem.
    with TestClient(app) as admin:
        r = admin.post("/api/auth/bootstrap", json={"username": "admin", "password": ADMIN_PASSWORD})
        assert r.status_code == 200, r.text
        yield admin, TestingSession

    app.dependency_overrides.clear()
    _database.SessionLocal = original_session_local  # type: ignore[assignment]
    _gateway.SessionLocal = original_session_local  # type: ignore[assignment]
    _main.SessionLocal = original_session_local  # type: ignore[assignment]
    Base.metadata.drop_all(bind=engine)


def _enable(admin: TestClient, *, mode: str = "json") -> None:
    r = admin.put("/api/mcp/config", json={"enabled": True, "response_mode": mode})
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is True


def _pat(client: TestClient, name: str = "mcp", scopes: list[str] | None = None) -> str:
    body: dict[str, Any] = {"name": name, "expires_at": None}
    if scopes is not None:
        body["scopes"] = scopes
    r = client.post("/api/v1/tokens", json=body)
    assert r.status_code == 201, r.text
    return r.json()["token"]


def _login_as(admin: TestClient, username: str, role: str) -> TestClient:
    r = admin.post(
        "/api/auth/users",
        json={"username": username, "password": USER_PASSWORD, "role": role},
    )
    assert r.status_code in (200, 201), r.text
    other = TestClient(app)
    r = other.post("/api/auth/login", json={"username": username, "password": USER_PASSWORD})
    assert r.status_code == 200, r.text
    return other


def _rpc(method: str, params: dict[str, Any] | None = None, *, id: int = 1) -> dict[str, Any]:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": id, "method": method}
    if params is not None:
        body["params"] = params
    return body


def _post(client: TestClient, token: str | None, body: dict[str, Any], **headers: str):
    h = {"Accept": ACCEPT, "Content-Type": "application/json", **headers}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return client.post(MCP, json=body, headers=h)


def _initialize_params() -> dict[str, Any]:
    return {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "0"},
    }


def _tool_text(payload: dict[str, Any]) -> Any:
    result = payload["result"]
    text = result["content"][0]["text"]
    return result, json.loads(text)


# ── Exposição e autenticação ─────────────────────────────────────────


def test_disabled_by_default_returns_404_and_audits(env):
    admin, Session = env
    token = _pat(admin)
    r = _post(admin, token, _rpc("initialize", _initialize_params()))
    assert r.status_code == 404
    assert "disabled" in r.json()["detail"]
    with Session() as db:
        denied = db.query(models.AuditLog).filter(models.AuditLog.action == "mcp.denied").all()
        assert [json.loads(d.detail)["reason"] for d in denied] == ["mcp_disabled"]


def test_session_cookie_is_not_accepted(env):
    admin, _ = env
    _enable(admin)
    # ``admin`` carrega o cookie de sessão do bootstrap — e nada de Bearer.
    r = _post(admin, None, _rpc("initialize", _initialize_params()))
    assert r.status_code == 401
    assert r.headers.get("WWW-Authenticate", "").startswith("Bearer")


def test_invalid_pat_is_401(env):
    admin, _ = env
    _enable(admin)
    r = _post(admin, "copsk_nao_existe_000000", _rpc("initialize", _initialize_params()))
    assert r.status_code == 401


def test_viewer_lacks_mcp_use(env):
    admin, Session = env
    _enable(admin)
    viewer = _login_as(admin, "viewer1", "viewer")
    token = _pat(viewer)
    r = _post(viewer, token, _rpc("initialize", _initialize_params()))
    assert r.status_code == 403
    assert "mcp.use" in r.json()["detail"]
    with Session() as db:
        denied = db.query(models.AuditLog).filter(models.AuditLog.action == "mcp.denied").all()
        assert denied[-1].username == "viewer1"
        assert json.loads(denied[-1].detail)["reason"] == "permission_mcp_use"


def test_token_scopes_can_exclude_mcp_use(env):
    """Um admin com PAT restrita a ``integration.read`` não entra no MCP: o
    token só estreita, nunca alarga."""
    admin, _ = env
    _enable(admin)
    narrow = _pat(admin, "narrow", scopes=["integration.read"])
    r = _post(admin, narrow, _rpc("initialize", _initialize_params()))
    assert r.status_code == 403
    wide = _pat(admin, "wide", scopes=["integration.read", "mcp.use"])
    r = _post(admin, wide, _rpc("initialize", _initialize_params()))
    assert r.status_code == 200, r.text


def test_get_and_delete_are_405_in_stateless_mode(env):
    """Sem sessão não há stream server-initiated (GET) nem sessão a encerrar
    (DELETE): 405 com ``Allow: POST``, como o protocolo manda."""
    admin, _ = env
    _enable(admin)
    token = _pat(admin)
    headers = {"Authorization": f"Bearer {token}", "Accept": ACCEPT}
    r = admin.get(MCP, headers=headers)
    assert r.status_code == 405 and r.headers["Allow"] == "POST"
    r = admin.delete(MCP, headers=headers)
    assert r.status_code == 405


# ── Protocolo ────────────────────────────────────────────────────────


def test_initialize_and_list_tools(env):
    admin, _ = env
    _enable(admin)
    token = _pat(admin)

    r = _post(admin, token, _rpc("initialize", _initialize_params()))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json")
    result = r.json()["result"]
    assert result["serverInfo"]["name"] == "centralops"
    assert "analyst" in result["instructions"]
    assert "tools" in result["capabilities"]

    r = _post(admin, token, _rpc("tools/list", {}, id=2))
    assert r.status_code == 200, r.text
    tools = {t["name"]: t for t in r.json()["result"]["tools"]}
    assert len(tools) == 57
    assert tools["commit_mapping"]["annotations"]["readOnlyHint"] is False
    assert tools["commit_mapping"]["annotations"]["destructiveHint"] is True
    assert tools["list_integrations"]["annotations"]["readOnlyHint"] is True
    assert tools["list_integrations"]["inputSchema"]["additionalProperties"] is False


def test_tool_call_runs_as_the_analyst_and_is_audited(env):
    admin, Session = env
    _enable(admin)
    token = _pat(admin)
    from backend.app.core.rate_limiter import token_rate_limiter

    r = _post(
        admin, token,
        _rpc("tools/call", {"name": "list_supported_platforms", "arguments": {}}),
        **{"User-Agent": "claude-desktop/1.0"},
    )
    assert r.status_code == 200, r.text
    result, payload = _tool_text(r.json())
    assert result.get("isError") in (None, False)
    assert isinstance(payload, (list, dict))

    with Session() as db:
        # 1) A linha do MCP, em nome do analista, com a ferramenta e o resultado.
        calls = db.query(models.AuditLog).filter(models.AuditLog.action == "mcp.tool_call").all()
        assert len(calls) == 1
        call = calls[0]
        assert call.username == "admin"
        assert call.user_role == "admin"
        assert call.status_code == 200
        assert call.user_agent == "claude-desktop/1.0"
        detail = json.loads(call.detail)
        assert detail["tool"] == "list_supported_platforms"
        assert detail["ok"] is True
        assert json.loads(call.request_payload) == {
            "tool": "list_supported_platforms", "argument_keys": [],
        }
        # 2) A linha da rota REST interna: o mesmo analista, marcado como MCP.
        inner = (
            db.query(models.AuditLog)
            .filter(models.AuditLog.endpoint == "/api/integrations/platforms")
            .all()
        )
        assert len(inner) == 1
        assert inner[0].username == "admin"
        assert inner[0].user_agent.startswith("centralops-mcp-embedded/")
        assert "claude-desktop/1.0" in inner[0].user_agent
        assert inner[0].status_code == 200
        # 3) Nenhuma linha genérica do middleware para o POST em /api/mcp.
        assert db.query(models.AuditLog).filter(models.AuditLog.endpoint == MCP,
                                                 models.AuditLog.action != "mcp.tool_call").count() == 0
        # 4) O uso da PAT foi registrado na borda (record_usage).
        pat = db.query(models.ApiToken).filter(models.ApiToken.name == "mcp").one()
        assert pat.use_count == 1

    # 5) Rate limit por token: UMA unidade por chamada MCP, não duas.
    assert len(token_rate_limiter._windows) == 1
    (window,) = token_rate_limiter._windows.values()
    assert len(window.timestamps) == 1


def test_tool_call_cannot_exceed_the_analyst_rest_permissions(env):
    """Operator tem ``mcp.use`` mas não ``mapping.write``: o REST nega e a
    ferramenta devolve o 403 estruturado em vez de fazer o commit."""
    admin, _ = env
    _enable(admin)
    operator = _login_as(admin, "op1", "operator")
    token = _pat(operator)

    r = _post(
        operator, token,
        _rpc("tools/call", {
            "name": "request_backfill",
            "arguments": {
                "integration_id": 1, "streams": ["alerts"],
                "from_ts": "2026-09-01T00:00:00Z", "to_ts": "2026-09-02T00:00:00Z",
            },
        }),
    )
    assert r.status_code == 200, r.text
    result, payload = _tool_text(r.json())
    assert result["isError"] is True
    assert payload["error_kind"] == "upstream_http_error"
    assert payload["http_status"] == 403
    assert "grants nothing beyond" in payload["error"]


def test_unknown_tool_and_bad_arguments_are_structured_errors(env):
    admin, _ = env
    _enable(admin)
    token = _pat(admin)

    r = _post(admin, token, _rpc("tools/call", {"name": "nope", "arguments": {}}))
    result, payload = _tool_text(r.json())
    assert result["isError"] is True and payload["error_kind"] == "unknown_tool"

    r = _post(admin, token, _rpc("tools/call", {
        "name": "get_integration", "arguments": {"integration_id": 1, "bogus": True},
    }))
    result, payload = _tool_text(r.json())
    assert result["isError"] is True and payload["error_kind"] == "invalid_argument"


def test_sse_mode_streams_the_response(env):
    admin, _ = env
    _enable(admin, mode="sse")
    token = _pat(admin)
    r = _post(admin, token, _rpc("tools/list", {}))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    assert '"tools"' in r.text
    assert "list_integrations" in r.text


def test_disabling_takes_effect_on_the_next_request(env):
    admin, Session = env
    _enable(admin)
    token = _pat(admin)
    assert _post(admin, token, _rpc("initialize", _initialize_params())).status_code == 200
    r = admin.put("/api/mcp/config", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert _post(admin, token, _rpc("initialize", _initialize_params())).status_code == 404
    with Session() as db:
        actions = [a.action for a in db.query(models.AuditLog).all()]
        assert "mcp.enabled" in actions and "mcp.disabled" in actions


# ── Config e status ──────────────────────────────────────────────────


def test_config_is_platform_admin_only_and_strict(env):
    admin, _ = env
    r = admin.get("/api/mcp/config")
    assert r.status_code == 200
    assert r.json() == {
        "enabled": False, "response_mode": "json", "endpoint_path": MCP,
        "tools_count": 57, "is_persisted": False, "updated_at": None,
    }
    # Campo desconhecido é 422, não descarte silencioso (StrictUpdateModel).
    assert admin.put("/api/mcp/config", json={"enabled": True, "nope": 1}).status_code == 422
    assert admin.put("/api/mcp/config", json={"response_mode": "grpc"}).status_code == 422

    operator = _login_as(admin, "op2", "operator")
    assert operator.get("/api/mcp/config").status_code == 403
    assert operator.put("/api/mcp/config", json={"enabled": True}).status_code == 403


def test_status_tells_the_analyst_what_they_need(env):
    admin, _ = env
    viewer = _login_as(admin, "viewer2", "viewer")
    r = viewer.get("/api/mcp/status")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["has_permission"] is False
    assert body["required_permission"] == "mcp.use"
    assert body["endpoint_path"] == MCP
    assert body["tools_count"] == 57

    _enable(admin)
    r = admin.get("/api/mcp/status")
    assert r.json()["enabled"] is True and r.json()["has_permission"] is True
