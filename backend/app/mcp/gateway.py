"""Gateway MCP em ``/api/mcp`` — Streamable HTTP, autenticado pela chave do analista.

Desenho
-------
* **Um endpoint, sem sessão.** O transporte é o Streamable HTTP do SDK oficial
  em modo *stateless*: cada POST é autossuficiente, qualquer réplica da API
  responde, nenhum ``Mcp-Session-Id`` a replicar. Resposta ``application/json``
  por padrão; ``text/event-stream`` (SSE) quando o admin escolher na tela de
  configuração. GET (canal SSE server-initiated) e DELETE (fim de sessão) não
  existem no modo stateless — o SDK responde 405, como manda o protocolo.
* **Só Bearer.** O gateway aceita exclusivamente ``Authorization: Bearer
  copsk_…`` (a PAT do analista). Cookie de sessão é ignorado de propósito: um
  endpoint que aceitasse cookie seria alvo de CSRF por qualquer página que
  o navegador visitasse.
* **Mesma cadeia de auth do REST.** A borda usa ``core.auth._resolve_bearer_user``
  (argon2, revogação, expiração, rate limit por token, ``record_usage``) e
  exige a permissão ``mcp.use`` pela interseção role × scopes do token.
* **Ferramenta = REST por loopback.** ``on_call_tool`` executa o handler com um
  ``LoopbackClient`` que entra no mesmo app FastAPI in-process, autenticado por
  um nonce de uso único ligado ao principal (``core.auth.inprocess_principal``).
  Permissão por rota, escopo de organização e auditoria são os dos routers.
* **Auditoria em nome do analista.** Cada ``tools/call`` gera uma linha
  ``mcp.tool_call`` (ferramenta, resultado, duração) além das linhas que as
  chamadas REST internas já produzem. Tentativas negadas (401/403/404) geram
  ``mcp.denied``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import Receive, Scope, Send

from ..core import auth as app_auth
from ..core.config import settings
from ..db import models
from ..db.database import SessionLocal
from ..db.repository import McpConfigRepository
from ..services.api_tokens import parse_scopes
from ..services.audit import AuditService, get_client_ip
from .ack_cache import AckTokenError
from .context import McpPrincipal, current_principal
from .instructions import SERVER_INSTRUCTIONS
from .loopback import CentralOpsAPIError, LoopbackClient
from .registry import ToolSpec, build_specs

try:  # o SDK é dependência direta; o guard só evita derrubar a API inteira
    import mcp.types as mcp_types
    from mcp.server.lowlevel import Server
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from mcp.server.transport_security import TransportSecuritySettings

    MCP_SDK_AVAILABLE = True
except ImportError:  # pragma: no cover — imagem sem o SDK
    MCP_SDK_AVAILABLE = False

logger = logging.getLogger(__name__)

MCP_ENDPOINT_PATH = "/api/mcp"
SERVER_NAME = "centralops"
#: Corpo máximo de UM POST JSON-RPC. ``dry_run_mapping`` com 100 eventos brutos
#: cabe com folga; um dump maior que isso é erro do cliente, não caso de uso.
MAX_REQUEST_BODY_BYTES = 2 * 1024 * 1024
RESPONSE_MODES = ("json", "sse")
DEFAULT_RESPONSE_MODE = "json"

AUDIT_ACTION_TOOL_CALL = "mcp.tool_call"
AUDIT_ACTION_DENIED = "mcp.denied"


# ── Runtime (ciclo de vida) ───────────────────────────────────────────


@dataclass
class McpRuntime:
    """Gerenciadores de sessão do SDK, um por modo de resposta.

    O modo vem do banco a cada requisição; os dois gerenciadores ficam
    prontos para que trocar na UI valha na requisição seguinte, sem restart.
    """

    managers: dict[str, Any]
    specs: dict[str, ToolSpec]


@contextlib.asynccontextmanager
async def mcp_runtime(fastapi_app):
    """Entra no ``run()`` dos gerenciadores durante o lifespan do FastAPI."""
    if not MCP_SDK_AVAILABLE:  # pragma: no cover
        logger.warning("SDK 'mcp' ausente — /api/mcp responderá 501.")
        yield None
        return

    specs = build_specs()
    server = build_server(fastapi_app, specs)
    security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    managers = {
        mode: StreamableHTTPSessionManager(
            app=server,
            json_response=(mode == "json"),
            stateless=True,
            security_settings=security,
            max_request_body_size=MAX_REQUEST_BODY_BYTES,
        )
        for mode in RESPONSE_MODES
    }
    async with contextlib.AsyncExitStack() as stack:
        for manager in managers.values():
            await stack.enter_async_context(manager.run())
        yield McpRuntime(managers=managers, specs=specs)


# ── Servidor MCP (protocolo) ──────────────────────────────────────────


def build_server(fastapi_app, specs: dict[str, ToolSpec]):
    async def on_list_tools(ctx, params):
        return mcp_types.ListToolsResult(
            tools=[
                mcp_types.Tool(
                    name=spec.name,
                    description=spec.description,
                    inputSchema=spec.input_schema,
                    annotations=mcp_types.ToolAnnotations(
                        readOnlyHint=spec.read_only,
                        # Only meaningful when the tool writes; the MCP default for
                        # a non-read-only tool is destructive=True, so state it
                        # explicitly rather than relying on the client's default.
                        destructiveHint=spec.destructive,
                        idempotentHint=spec.idempotent,
                    ),
                )
                for spec in specs.values()
            ]
        )

    async def on_call_tool(ctx, params):
        return await _call_tool(fastapi_app, specs, ctx, params)

    return Server(
        SERVER_NAME,
        version=settings.APP_VERSION,
        instructions=SERVER_INSTRUCTIONS,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


def _principal_from_ctx(ctx) -> McpPrincipal | None:
    request = getattr(ctx, "request", None)
    state = getattr(request, "state", None)
    principal = getattr(state, "mcp_principal", None)
    return principal if isinstance(principal, McpPrincipal) else None


def _text_result(payload: Any, *, is_error: bool = False):
    if isinstance(payload, (dict, list)) or payload is None:
        text = json.dumps(payload, indent=2, default=str, ensure_ascii=False)
    else:
        text = str(payload)
    return mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text=text)],
        isError=is_error,
    )


def _error_result(message: str, **fields: Any):
    return _text_result({"error": message, **fields}, is_error=True)


def _loopback_user_agent(principal: McpPrincipal) -> str:
    ua = f"centralops-mcp-embedded/{settings.APP_VERSION}"
    if principal.user_agent:
        # A auditoria persiste o User-Agent: é o que permite distinguir tráfego
        # MCP do da UI, e QUAL cliente MCP (Claude, Cursor, ...) originou a ação.
        ua += f" (client: {principal.user_agent[:120]})"
    return ua


async def _call_tool(fastapi_app, specs: dict[str, ToolSpec], ctx, params):
    principal = _principal_from_ctx(ctx)
    if principal is None:  # pragma: no cover — o gateway só delega autenticado
        return _error_result("Unauthenticated MCP call", error_kind="unauthenticated")

    name = params.name
    spec = specs.get(name)
    if spec is None:
        _audit_tool_call(principal, tool=name, status_code=404, error_kind="unknown_tool",
                         duration_ms=0, argument_keys=[])
        return _error_result(f"Unknown tool: {name}", error_kind="unknown_tool")

    kwargs = dict(params.arguments or {})
    started = time.monotonic()
    status_code = 200
    error_kind: str | None = None
    result: Any = None
    try:
        with app_auth.inprocess_principal(principal.token_id) as nonce:
            transport = httpx.ASGITransport(
                app=fastapi_app,
                client=(principal.client_ip or "127.0.0.1", 0),
            )
            headers = {
                "Authorization": f"Bearer {nonce}",
                "User-Agent": _loopback_user_agent(principal),
                "X-Correlation-Id": principal.correlation_id,
            }
            ctx_token = current_principal.set(principal)
            try:
                async with LoopbackClient(transport=transport, headers=headers) as client:
                    result = await spec.handler(client, **kwargs)
            finally:
                current_principal.reset(ctx_token)
        response = _text_result(result)
    except AckTokenError as exc:
        status_code, error_kind = 409, "ack_token_invalid"
        response = _error_result(str(exc), error_kind=error_kind)
    except CentralOpsAPIError as exc:
        status_code, error_kind = exc.status_code, "upstream_http_error"
        response = _error_result(
            str(exc),
            error_kind=error_kind,
            http_status=exc.status_code,
            upstream_body=exc.body,
        )
    except ValueError as exc:
        status_code, error_kind = 422, "invalid_argument"
        response = _error_result(str(exc), error_kind=error_kind)
    except TypeError as exc:
        status_code, error_kind = 422, "invalid_argument"
        response = _error_result(
            f"Invalid arguments for tool '{name}': {exc}", error_kind=error_kind
        )
    except Exception:  # noqa: BLE001 — nunca vaza stack trace para o modelo
        logger.exception("MCP tool %s crashed (user=%s)", name, principal.username)
        status_code, error_kind = 500, "internal_error"
        response = _error_result(
            f"Tool '{name}' failed unexpectedly; the error was logged server-side "
            f"under correlation id {principal.correlation_id}.",
            error_kind=error_kind,
        )
    finally:
        duration_ms = int((time.monotonic() - started) * 1000)
        await run_in_threadpool(
            _audit_tool_call,
            principal,
            tool=name,
            status_code=status_code,
            error_kind=error_kind,
            duration_ms=duration_ms,
            argument_keys=sorted(kwargs.keys()),
        )
    return response


# ── Auditoria ─────────────────────────────────────────────────────────


def _audit_actor(principal: McpPrincipal) -> models.AppUser:
    """Shim transient só para atribuição na trilha (nunca entra na sessão).

    ``AuditService.log_event`` deriva org e ``persistable_user_id`` a partir
    do objeto — o mesmo caminho do shim de service account em ``core.auth``.
    """
    return models.AppUser(
        id=principal.user_id,
        username=principal.username,
        role=principal.role,
        organization_id=principal.organization_id,
        is_global=principal.is_global,
        is_active=True,
    )


def _audit_tool_call(
    principal: McpPrincipal,
    *,
    tool: str,
    status_code: int,
    error_kind: str | None,
    duration_ms: int,
    argument_keys: list[str],
) -> None:
    """Uma linha por ``tools/call``, em nome do analista. Best-effort.

    Só os NOMES dos argumentos entram aqui: os valores já estão nas linhas das
    chamadas REST internas, redigidos pelo middleware de auditoria.
    """
    db = SessionLocal()
    try:
        AuditService(db).log_event(
            action=AUDIT_ACTION_TOOL_CALL,
            endpoint=MCP_ENDPOINT_PATH,
            user=_audit_actor(principal),
            method="POST",
            status_code=status_code,
            ip_address=principal.client_ip,
            user_agent=principal.user_agent,
            request_payload=json.dumps(
                {"tool": tool, "argument_keys": argument_keys}, ensure_ascii=False
            ),
            detail=json.dumps(
                {
                    "tool": tool,
                    "ok": error_kind is None,
                    "error_kind": error_kind,
                    "duration_ms": duration_ms,
                    "correlation_id": principal.correlation_id,
                },
                ensure_ascii=False,
            ),
        )
    except Exception as exc:  # noqa: BLE001 — auditoria nunca derruba a chamada
        logger.warning("Failed to write MCP audit log: %s", exc)
    finally:
        db.close()


def _audit_denied(
    db, request: Request, *, status_code: int, reason: str, user: models.AppUser | None
) -> None:
    try:
        AuditService(db).log_event(
            action=AUDIT_ACTION_DENIED,
            endpoint=MCP_ENDPOINT_PATH,
            user=user,
            method=request.method,
            status_code=status_code,
            ip_address=get_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            detail=json.dumps({"reason": reason}),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to write MCP audit log: %s", exc)


# ── Borda HTTP ────────────────────────────────────────────────────────


def _json_error(status_code: int, detail: str, headers: dict[str, str] | None = None) -> Response:
    return JSONResponse({"detail": detail}, status_code=status_code, headers=headers)


_WWW_AUTH = {"WWW-Authenticate": 'Bearer realm="centralops"'}


def _correlation_id(request: Request) -> str:
    try:
        from ..core.logging_config import get_correlation_id  # type: ignore[attr-defined]

        cid = get_correlation_id()
        if cid:
            return str(cid)
    except Exception:  # noqa: BLE001 — helper opcional
        pass
    return request.headers.get("X-Correlation-Id") or str(uuid.uuid4())


def authenticate(request: Request) -> Response | tuple[McpPrincipal, str]:
    """Toggle → Bearer → ``mcp.use``. Síncrono (roda no threadpool).

    Devolve a ``Response`` de erro pronta, ou ``(principal, response_mode)``.
    """
    db = SessionLocal()
    try:
        config = McpConfigRepository(db).get()
        if config is None or not config.enabled:
            _audit_denied(db, request, status_code=404, reason="mcp_disabled", user=None)
            return _json_error(404, "MCP server is disabled on this instance")

        try:
            user = app_auth._resolve_bearer_user(request, db)
        except HTTPException as exc:
            _audit_denied(db, request, status_code=exc.status_code,
                          reason="bearer_rejected", user=None)
            return _json_error(exc.status_code, str(exc.detail), exc.headers)
        if user is None:
            # Sem Bearer, Bearer de outro esquema, ou cookie: o MCP não aceita
            # sessão de navegador.
            _audit_denied(db, request, status_code=401, reason="bearer_missing", user=None)
            return _json_error(
                401, "MCP requires 'Authorization: Bearer <analyst API key>'", _WWW_AUTH
            )

        api_token: models.ApiToken | None = getattr(request.state, "authenticated_token", None)
        if api_token is None:  # pragma: no cover — invariante de _resolve_bearer_user
            return _json_error(401, "API key required", _WWW_AUTH)

        token_scopes = parse_scopes(api_token.scopes_json) or None
        allowed = app_auth.effective_scopes(user.role, token_scopes)
        if app_auth.Permission.MCP_USE not in allowed:
            _audit_denied(db, request, status_code=403, reason="permission_mcp_use", user=user)
            return _json_error(
                403,
                f"Permission '{app_auth.Permission.MCP_USE}' is required to use the MCP "
                "server (role or API key scopes exclude it)",
            )

        principal = McpPrincipal(
            user_id=int(user.id),
            username=str(user.username),
            role=str(user.role),
            token_id=int(api_token.id),
            organization_id=user.organization_id,
            is_global=bool(getattr(user, "is_global", False)),
            client_ip=get_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            correlation_id=_correlation_id(request),
        )
        mode = config.response_mode if config.response_mode in RESPONSE_MODES else DEFAULT_RESPONSE_MODE
        return principal, mode
    finally:
        db.close()


class McpGateway:
    """ASGI app montado em ``/api/mcp``."""

    def __init__(self, fastapi_app) -> None:
        self.fastapi_app = fastapi_app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # pragma: no cover — sem websocket aqui
            await Response(status_code=404)(scope, receive, send)
            return
        request = Request(scope, receive)

        # Stateless: não há sessão para encerrar (DELETE) nem canal SSE
        # iniciado pelo servidor para abrir (GET). O protocolo manda responder
        # 405 quando o endpoint não oferece o stream — e responder AQUI evita
        # que o transporte do SDK deixe um GET pendurado esperando eventos que
        # nunca virão.
        if request.method != "POST":
            await Response(status_code=405, headers={"Allow": "POST"})(scope, receive, send)
            return

        if not MCP_SDK_AVAILABLE:  # pragma: no cover
            await _json_error(501, "MCP SDK is not installed on this server")(scope, receive, send)
            return
        runtime: McpRuntime | None = getattr(self.fastapi_app.state, "mcp_runtime", None)
        if runtime is None:
            await _json_error(503, "MCP runtime is not started")(scope, receive, send)
            return

        outcome = await run_in_threadpool(authenticate, request)
        if isinstance(outcome, Response):
            await outcome(scope, receive, send)
            return

        principal, mode = outcome
        # ``Request.state`` escreve em ``scope["state"]``; o Request que o SDK
        # constrói sobre o mesmo scope enxerga o principal em
        # ``ctx.request.state.mcp_principal``.
        request.state.mcp_principal = principal
        await runtime.managers[mode].handle_request(scope, receive, send)


__all__ = [
    "MCP_ENDPOINT_PATH",
    "MCP_SDK_AVAILABLE",
    "McpGateway",
    "McpRuntime",
    "RESPONSE_MODES",
    "authenticate",
    "build_server",
    "mcp_runtime",
]
