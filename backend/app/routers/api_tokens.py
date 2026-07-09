"""Router /api/v1/tokens — gestão de **Personal Access Tokens** (Fase 1, Fase 2).

PATs pessoais ficam aqui (atrelados ao ``current_user``). Tokens de
Service Account são gerenciados em ``/api/v1/service-accounts/{id}/tokens``
porque exigem perm ``USER_MANAGE`` e têm semântica diferente.

Endpoints (todos exigem ``require_authenticated_user`` — cookie session
do dono ou outro PAT do dono; o resolver Bearer é transparente):

  POST   /api/v1/tokens          → cria PAT pessoal (raw uma única vez)
  GET    /api/v1/tokens          → lista PATs pessoais
  DELETE /api/v1/tokens/{id}     → revoga (soft delete)

Auditoria: ``token.created`` e ``token.revoked`` são gravados em ``audit_logs``
via ``AuditService``. ``token.used`` é gravado pelo resolver Bearer no
``get_current_user``, fora deste router.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.orm import Session

from ..api import schemas
from ..core import auth as app_auth
from ..core.errors import ApiError
from ..db import database, models
from ..services.api_tokens import ApiTokenService, parse_scopes
from ..services.audit import AuditService, get_client_ip

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/tokens", tags=["api-tokens"])


def _serialize(token: models.ApiToken) -> schemas.ApiTokenRead:
    """Serializa ApiToken pra ApiTokenRead, decodificando scopes_json."""
    data = schemas.ApiTokenRead.model_validate(token)
    data.scopes = parse_scopes(token.scopes_json)
    return data


def _log_audit(
    db: Session,
    request: Request,
    *,
    action: str,
    status_code: int,
    user: models.AppUser,
    detail: dict | None = None,
) -> None:
    try:
        AuditService(db).log_event(
            action=action,
            endpoint=request.url.path,
            user=user,
            method=request.method,
            status_code=status_code,
            ip_address=get_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            detail=json.dumps(detail) if detail else None,
        )
    except Exception as exc:  # pragma: no cover — audit é best-effort
        logger.warning("Falha ao gravar audit %s: %s", action, exc)


@router.post(
    "",
    response_model=schemas.ApiTokenCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_token(
    payload: schemas.ApiTokenCreate,
    request: Request,
    db: Session = Depends(database.get_session),
    current_user: models.AppUser = Depends(app_auth.require_authenticated_user),
) -> schemas.ApiTokenCreateResponse:
    """Cria um PAT pessoal pro ``current_user``.

    - ``service_account_id`` no payload é **rejeitado** aqui — use
      ``POST /api/v1/service-accounts/{id}/tokens`` (Fase 2). Permite
      separação clara de perm USER_MANAGE pra emissão de SA tokens.
    - ``is_eternal=True`` exige ``expires_at=None`` e vice-versa. UI
      Fase 2 obriga checkbox explícito de "nunca expira" — backend
      ainda aceita ``expires_at=None`` legacy (Fase 1) e infere
      ``is_eternal=True`` automaticamente.
    - Scopes: subset de Permission. Vazio/None = full inherit da role.
      Fora da role do user → 400 (token nunca escala privilégio).
    """
    if payload.service_account_id is not None:
        raise ApiError(
            "api_token.service_account_id_not_allowed",
            status.HTTP_400_BAD_REQUEST,
            messages={
                "pt": (
                    "service_account_id não é permitido neste endpoint. "
                    "Use POST /api/v1/service-accounts/{id}/tokens."
                ),
                "en": (
                    "service_account_id is not allowed on this endpoint. "
                    "Use POST /api/v1/service-accounts/{id}/tokens instead."
                ),
                "es": (
                    "service_account_id no está permitido en este endpoint. "
                    "Use POST /api/v1/service-accounts/{id}/tokens."
                ),
            },
        )

    service = ApiTokenService(db)

    try:
        raw_token, token = service.create_token(
            user=current_user,
            name=payload.name,
            expires_at=payload.expires_at,
            is_eternal=payload.is_eternal,
            scopes=payload.scopes,
        )
    except ValueError as exc:
        raise ApiError(
            "api_token.invalid_request",
            status.HTTP_400_BAD_REQUEST,
            messages={
                "pt": "{error}",
                "en": "{error}",
                "es": "{error}",
            },
            params={"error": str(exc)},
        ) from exc

    _log_audit(
        db,
        request,
        action="token.created",
        status_code=status.HTTP_201_CREATED,
        user=current_user,
        detail={
            "token_id": token.id,
            "token_name": token.name,
            "token_prefix": token.token_prefix,
            "is_eternal": token.is_eternal,
            "scopes": parse_scopes(token.scopes_json),
            "expires_at": token.expires_at.isoformat() if token.expires_at else None,
        },
    )

    return schemas.ApiTokenCreateResponse(token=raw_token, api_token=_serialize(token))


@router.get("", response_model=list[schemas.ApiTokenRead])
def list_tokens(
    db: Session = Depends(database.get_session),
    current_user: models.AppUser = Depends(app_auth.require_authenticated_user),
    include_revoked: bool = False,
) -> list[schemas.ApiTokenRead]:
    """Lista PATs **pessoais** do user logado. Por default oculta revogados.

    Tokens de Service Account não aparecem aqui — use
    ``GET /api/v1/service-accounts/{id}/tokens``.
    """
    service = ApiTokenService(db)
    tokens = service.list_for_user(current_user.id, include_revoked=include_revoked)
    return [_serialize(t) for t in tokens]


@router.get("/scopes", response_model=list[str])
def list_scopes(
    _user: models.AppUser = Depends(app_auth.require_authenticated_user),
) -> list[str]:
    """Lista de scopes válidos = ``Permission`` enum no backend.

    Consumido pela UI de criação de token (TokensPage / ServiceAccountsPage)
    pra renderizar checkboxes. Mantido como endpoint próprio em vez de
    constante hardcoded no frontend pra que adicionar nova ``Permission``
    propague automaticamente sem deploy do FE.

    Não exige permissão especial — qualquer user autenticado consulta
    (saber quais scopes existem ≠ saber quais permissões cada role tem).
    """
    return [p.value for p in app_auth.Permission]


@router.delete("/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_token(
    token_id: int,
    request: Request,
    db: Session = Depends(database.get_session),
    current_user: models.AppUser = Depends(app_auth.require_authenticated_user),
):
    """Revoga (soft delete) um PAT *pessoal* do user logado.

    Tokens de Service Account não podem ser revogados aqui (404) —
    use ``DELETE /api/v1/service-accounts/{id}/tokens/{tid}``.
    """
    service = ApiTokenService(db)
    token = service.revoke_token(user=current_user, token_id=token_id)
    if not token:
        raise ApiError(
            "api_token.not_found",
            status.HTTP_404_NOT_FOUND,
            messages={
                "pt": "Token não encontrado.",
                "en": "Token not found.",
                "es": "Token no encontrado.",
            },
        )

    _log_audit(
        db,
        request,
        action="token.revoked",
        status_code=status.HTTP_204_NO_CONTENT,
        user=current_user,
        detail={
            "token_id": token.id,
            "token_name": token.name,
            "token_prefix": token.token_prefix,
        },
    )
    return None
