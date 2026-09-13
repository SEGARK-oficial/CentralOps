"""Configuração e status do servidor MCP embutido.

* ``GET/PUT /api/mcp/config`` — admin de PLATAFORMA (singleton, sem org): liga
  ou desliga o endpoint ``/api/mcp`` e escolhe o modo de resposta. Vale na
  requisição seguinte, em todas as réplicas (o gateway lê o banco a cada
  chamada; não há cache a invalidar).
* ``GET /api/mcp/status`` — qualquer usuário autenticado: o que ELE precisa
  para configurar um cliente (endpoint, se está ligado, se tem ``mcp.use``).

O endpoint do protocolo em si é ``/api/mcp`` (ver ``backend.app.mcp.gateway``)
e não passa por aqui.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..api import schemas
from ..core import auth as app_auth
from ..core import tenant
from ..core.config import settings
from ..db import database, models, repository
from ..mcp.gateway import DEFAULT_RESPONSE_MODE, MCP_ENDPOINT_PATH, RESPONSE_MODES, SERVER_NAME
from ..services.api_tokens import parse_scopes
from ..services.audit import AuditService, get_client_ip

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mcp", tags=["mcp"])


def _require_platform_admin(
    user: models.AppUser = Depends(app_auth.require_admin_user),
) -> models.AppUser:
    """Expor uma superfície de automação é decisão de PLATAFORMA (singleton,
    sem org) — um admin-de-org não pode ler nem alterar."""
    tenant.require_global_scope(user)
    return user


def _tools_count(request: Request) -> int:
    runtime = getattr(request.app.state, "mcp_runtime", None)
    specs = getattr(runtime, "specs", None)
    if specs is not None:
        return len(specs)
    # Fora do lifespan (testes sem contexto) o registro ainda responde.
    try:
        from ..mcp.registry import build_specs

        return len(build_specs())
    except Exception:  # noqa: BLE001 — contagem é informativa
        return 0


def _to_read(row: models.McpConfig | None, request: Request) -> schemas.McpConfigRead:
    if row is None:
        return schemas.McpConfigRead(
            enabled=False,
            response_mode=DEFAULT_RESPONSE_MODE,
            endpoint_path=MCP_ENDPOINT_PATH,
            tools_count=_tools_count(request),
            is_persisted=False,
        )
    mode = row.response_mode if row.response_mode in RESPONSE_MODES else DEFAULT_RESPONSE_MODE
    return schemas.McpConfigRead(
        enabled=bool(row.enabled),
        response_mode=mode,
        endpoint_path=MCP_ENDPOINT_PATH,
        tools_count=_tools_count(request),
        is_persisted=True,
        updated_at=row.updated_at,
    )


@router.get("/config", response_model=schemas.McpConfigRead)
def get_mcp_config(
    request: Request,
    _: models.AppUser = Depends(_require_platform_admin),
    db: Session = Depends(database.get_session),
):
    return _to_read(repository.McpConfigRepository(db).get(), request)


@router.put("/config", response_model=schemas.McpConfigRead)
def update_mcp_config(
    payload: schemas.McpConfigUpdate,
    request: Request,
    user: models.AppUser = Depends(_require_platform_admin),
    db: Session = Depends(database.get_session),
):
    data = payload.model_dump(exclude_unset=True)
    repo = repository.McpConfigRepository(db)
    before = repo.get()
    was_enabled = bool(before.enabled) if before is not None else False
    row = repo.update(**data)

    # Ligar/desligar uma superfície de automação merece uma linha NOMEADA na
    # trilha (o middleware grava a genérica com o payload).
    if "enabled" in data and bool(row.enabled) != was_enabled:
        try:
            AuditService(db).log_event(
                action="mcp.enabled" if row.enabled else "mcp.disabled",
                endpoint=request.url.path,
                user=user,
                method=request.method,
                status_code=200,
                ip_address=get_client_ip(request),
                user_agent=request.headers.get("user-agent"),
                detail=json.dumps({"response_mode": row.response_mode}),
            )
        except Exception as exc:  # noqa: BLE001 — auditoria best-effort
            logger.warning("Failed to write MCP config audit log: %s", exc)
    return _to_read(row, request)


@router.get("/status", response_model=schemas.McpStatusRead)
def get_mcp_status(
    request: Request,
    user: models.AppUser = Depends(app_auth.require_authenticated_user),
    db: Session = Depends(database.get_session),
):
    row = repository.McpConfigRepository(db).get()
    api_token: models.ApiToken | None = getattr(request.state, "authenticated_token", None)
    token_scopes = parse_scopes(api_token.scopes_json) or None if api_token is not None else None
    allowed = app_auth.effective_scopes(user.role, token_scopes)
    mode = (
        row.response_mode
        if row is not None and row.response_mode in RESPONSE_MODES
        else DEFAULT_RESPONSE_MODE
    )
    return schemas.McpStatusRead(
        enabled=bool(row.enabled) if row is not None else False,
        response_mode=mode,
        endpoint_path=MCP_ENDPOINT_PATH,
        has_permission=app_auth.Permission.MCP_USE in allowed,
        required_permission=str(app_auth.Permission.MCP_USE),
        tools_count=_tools_count(request),
        server_name=SERVER_NAME,
        server_version=settings.APP_VERSION,
    )
