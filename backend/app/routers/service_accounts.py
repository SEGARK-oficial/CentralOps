"""Router /api/v1/service-accounts — gestão de Service Accounts.

Service Accounts representam identidades non-human (workers, IASOC, scripts).
Apenas usuários com ``USER_MANAGE`` podem criar/editar/deletar SAs e seus
tokens — alterações são audit logadas (``service_account.{created,updated,
deleted,token_created,token_revoked}``).

Endpoints (todos sob require_permission(Permission.USER_MANAGE)):

  POST   /api/v1/service-accounts           → cria SA
  GET    /api/v1/service-accounts           → lista todos
  GET    /api/v1/service-accounts/{id}      → detalhe
  PATCH  /api/v1/service-accounts/{id}      → atualiza (parcial)
  DELETE /api/v1/service-accounts/{id}      → deleta (cascade revoga tokens)

  POST   /api/v1/service-accounts/{id}/tokens          → emite token pra SA
  GET    /api/v1/service-accounts/{id}/tokens          → lista tokens do SA
  DELETE /api/v1/service-accounts/{id}/tokens/{tid}    → revoga token do SA
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.orm import Session

from ..api import schemas
from ..core import auth as app_auth
from ..core import tenant
from ..core.auth import Permission
from ..core.errors import ApiError
from ..db import database, models
from ..services.api_tokens import ApiTokenService, parse_scopes
from ..services.audit import AuditService, get_client_ip
from ..services.service_accounts import ServiceAccountService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/service-accounts", tags=["service-accounts"])


# ── Helpers ──────────────────────────────────────────────────────────────


def _serialize_sa(
    sa: models.ServiceAccount,
    sa_service: ServiceAccountService,
) -> schemas.ServiceAccountRead:
    """Converte ServiceAccount → ServiceAccountRead com active_token_count."""
    data = schemas.ServiceAccountRead.model_validate(sa)
    data.active_token_count = sa_service.count_active_tokens(sa.id)
    return data


def _serialize_token(token: models.ApiToken) -> schemas.ApiTokenRead:
    """Converte ApiToken → ApiTokenRead com scopes parseados."""
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


# ── CRUD de Service Accounts ────────────────────────────────────────────


def _assert_sa_in_scope(sa, current_user: models.AppUser, db: Session) -> None:
    """admin escopado só opera SAs da própria subárvore. SA de
    PLATAFORMA (org NULL — o shim herda escopo GLOBAL via role, auth.py) só por
    admin global. Evita escalação: escopado usando/gerindo um SA global."""
    if sa.organization_id is not None:
        tenant.require_subtree_access(current_user, sa.organization_id, db)
    else:
        tenant.require_global_scope(current_user)


@router.post(
    "",
    response_model=schemas.ServiceAccountRead,
    status_code=status.HTTP_201_CREATED,
)
def create_service_account(
    payload: schemas.ServiceAccountCreate,
    request: Request,
    db: Session = Depends(database.get_session),
    current_user: models.AppUser = Depends(
        app_auth.require_permission(Permission.USER_MANAGE)
    ),
) -> schemas.ServiceAccountRead:
    sa_service = ServiceAccountService(db)
    # anti-escalação: um SA sem org + role=admin herda escopo
    # GLOBAL (auth.py shim). Admin escopado só cria SA na própria subárvore.
    tenant.enforce_admin_delegation_scope(
        current_user,
        target_org_id=payload.organization_id,
        target_is_global=False,
        session=db,
    )
    try:
        sa = sa_service.create(
            name=payload.name,
            description=payload.description,
            role=payload.role,
            organization_id=payload.organization_id,
            # current_user pode ser shim de SA (id negativo) — preserva
            # rastreabilidade gravando created_by_user_id apenas pra users
            # reais; SA não pode criar outro SA via PAT/scope (defensivo).
            created_by_user_id=current_user.id if current_user.id > 0 else None,
        )
    except ValueError as exc:
        raise ApiError(
            "service_account.invalid",
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
        action="service_account.created",
        status_code=status.HTTP_201_CREATED,
        user=current_user,
        detail={
            "service_account_id": sa.id,
            "name": sa.name,
            "role": sa.role,
            "organization_id": sa.organization_id,
        },
    )

    return _serialize_sa(sa, sa_service)


@router.get("", response_model=list[schemas.ServiceAccountRead])
def list_service_accounts(
    db: Session = Depends(database.get_session),
    current_user: models.AppUser = Depends(
        app_auth.require_permission(Permission.USER_MANAGE)
    ),
    include_inactive: bool = True,
) -> list[schemas.ServiceAccountRead]:
    sa_service = ServiceAccountService(db)
    rows = sa_service.list_all(include_inactive=include_inactive)
    # escopado vê só SAs da própria subárvore (SA de
    # plataforma, org NULL, fica oculto). Global (None) vê todos.
    org_ids = tenant.accessible_org_ids(current_user, db)
    if org_ids is not None:
        rows = [sa for sa in rows if sa.organization_id in org_ids]
    return [_serialize_sa(sa, sa_service) for sa in rows]


@router.get(
    "/{service_account_id}",
    response_model=schemas.ServiceAccountRead,
)
def get_service_account(
    service_account_id: int,
    db: Session = Depends(database.get_session),
    current_user: models.AppUser = Depends(
        app_auth.require_permission(Permission.USER_MANAGE)
    ),
) -> schemas.ServiceAccountRead:
    sa_service = ServiceAccountService(db)
    sa = sa_service.get(service_account_id)
    if sa is None:
        raise ApiError(
            "service_account.not_found",
            status.HTTP_404_NOT_FOUND,
            messages={
                "pt": "Service account não encontrada.",
                "en": "Service account not found.",
                "es": "Cuenta de servicio no encontrada.",
            },
        )
    _assert_sa_in_scope(sa, current_user, db)
    return _serialize_sa(sa, sa_service)


@router.patch(
    "/{service_account_id}",
    response_model=schemas.ServiceAccountRead,
)
def update_service_account(
    service_account_id: int,
    payload: schemas.ServiceAccountUpdate,
    request: Request,
    db: Session = Depends(database.get_session),
    current_user: models.AppUser = Depends(
        app_auth.require_permission(Permission.USER_MANAGE)
    ),
) -> schemas.ServiceAccountRead:
    sa_service = ServiceAccountService(db)
    sa = sa_service.get(service_account_id)
    if sa is None:
        raise ApiError(
            "service_account.not_found",
            status.HTTP_404_NOT_FOUND,
            messages={
                "pt": "Service account não encontrada.",
                "en": "Service account not found.",
                "es": "Cuenta de servicio no encontrada.",
            },
        )
    _assert_sa_in_scope(sa, current_user, db)

    fields = payload.model_dump(exclude_unset=True)
    if "organization_id" in fields:
        # Reassign: a org NOVA também precisa estar no teto do actor (org None =
        # SA de plataforma ⇒ só global).
        tenant.enforce_admin_delegation_scope(
            current_user,
            target_org_id=fields.get("organization_id"),
            target_is_global=False,
            session=db,
        )
    previous = {
        "role": sa.role,
        "is_active": sa.is_active,
        "description": sa.description,
        "organization_id": sa.organization_id,
    }

    sa = sa_service.update(
        sa,
        description=fields.get("description"),
        role=fields.get("role"),
        organization_id=fields.get("organization_id"),
        is_active=fields.get("is_active"),
        _description_set="description" in fields,
        _organization_id_set="organization_id" in fields,
    )

    # Audit detalhado apenas pros campos que mudaram. Role change é
    # sensitive (afeta token ceiling) — sempre loga.
    changed = {k: v for k, v in fields.items() if previous.get(k) != v}
    if changed:
        _log_audit(
            db,
            request,
            action="service_account.updated",
            status_code=status.HTTP_200_OK,
            user=current_user,
            detail={
                "service_account_id": sa.id,
                "name": sa.name,
                "changed": changed,
                "previous": {k: previous[k] for k in changed if k in previous},
            },
        )

    return _serialize_sa(sa, sa_service)


@router.delete(
    "/{service_account_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_service_account(
    service_account_id: int,
    request: Request,
    db: Session = Depends(database.get_session),
    current_user: models.AppUser = Depends(
        app_auth.require_permission(Permission.USER_MANAGE)
    ),
):
    sa_service = ServiceAccountService(db)
    sa = sa_service.get(service_account_id)
    if sa is None:
        raise ApiError(
            "service_account.not_found",
            status.HTTP_404_NOT_FOUND,
            messages={
                "pt": "Service account não encontrada.",
                "en": "Service account not found.",
                "es": "Cuenta de servicio no encontrada.",
            },
        )
    _assert_sa_in_scope(sa, current_user, db)

    # Captura snapshot pra audit ANTES do delete (cascata destrói relacionamentos).
    snapshot = {
        "service_account_id": sa.id,
        "name": sa.name,
        "role": sa.role,
        "active_tokens_at_delete": sa_service.count_active_tokens(sa.id),
    }
    sa_service.delete(sa)

    _log_audit(
        db,
        request,
        action="service_account.deleted",
        status_code=status.HTTP_204_NO_CONTENT,
        user=current_user,
        detail=snapshot,
    )
    return None


# ── Tokens vinculados a um Service Account ──────────────────────────────


@router.post(
    "/{service_account_id}/tokens",
    response_model=schemas.ApiTokenCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_token_for_sa(
    service_account_id: int,
    payload: schemas.ApiTokenCreate,
    request: Request,
    db: Session = Depends(database.get_session),
    current_user: models.AppUser = Depends(
        app_auth.require_permission(Permission.USER_MANAGE)
    ),
) -> schemas.ApiTokenCreateResponse:
    """Emite um PAT vinculado ao SA. Caller precisa de USER_MANAGE.

    O ``service_account_id`` do payload é ignorado em favor do path —
    evita confusão. Scopes são opcionais; vazio/None = full inherit
    da role do SA.
    """
    sa_service = ServiceAccountService(db)
    sa = sa_service.get(service_account_id)
    if sa is None:
        raise ApiError(
            "service_account.not_found",
            status.HTTP_404_NOT_FOUND,
            messages={
                "pt": "Service account não encontrada.",
                "en": "Service account not found.",
                "es": "Cuenta de servicio no encontrada.",
            },
        )
    _assert_sa_in_scope(sa, current_user, db)
    if not sa.is_active:
        raise ApiError(
            "service_account.inactive_token_issue",
            status.HTTP_400_BAD_REQUEST,
            messages={
                "pt": "Não é possível emitir tokens para uma service account inativa.",
                "en": "Cannot issue tokens to an inactive service account.",
                "es": "No se pueden emitir tokens para una cuenta de servicio inactiva.",
            },
        )

    api_token_service = ApiTokenService(db)
    try:
        raw_token, token = api_token_service.create_token(
            service_account=sa,
            name=payload.name,
            expires_at=payload.expires_at,
            is_eternal=payload.is_eternal,
            scopes=payload.scopes,
        )
    except ValueError as exc:
        raise ApiError(
            "service_account.token_invalid",
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
        action="service_account.token_created",
        status_code=status.HTTP_201_CREATED,
        user=current_user,
        detail={
            "service_account_id": sa.id,
            "service_account_name": sa.name,
            "token_id": token.id,
            "token_name": token.name,
            "token_prefix": token.token_prefix,
            "is_eternal": token.is_eternal,
            "scopes": parse_scopes(token.scopes_json),
            "expires_at": token.expires_at.isoformat() if token.expires_at else None,
        },
    )

    return schemas.ApiTokenCreateResponse(token=raw_token, api_token=_serialize_token(token))


@router.get(
    "/{service_account_id}/tokens",
    response_model=list[schemas.ApiTokenRead],
)
def list_tokens_for_sa(
    service_account_id: int,
    db: Session = Depends(database.get_session),
    current_user: models.AppUser = Depends(
        app_auth.require_permission(Permission.USER_MANAGE)
    ),
    include_revoked: bool = False,
) -> list[schemas.ApiTokenRead]:
    sa_service = ServiceAccountService(db)
    sa = sa_service.get(service_account_id)
    if sa is None:
        raise ApiError(
            "service_account.not_found",
            status.HTTP_404_NOT_FOUND,
            messages={
                "pt": "Service account não encontrada.",
                "en": "Service account not found.",
                "es": "Cuenta de servicio no encontrada.",
            },
        )
    _assert_sa_in_scope(sa, current_user, db)

    api_token_service = ApiTokenService(db)
    tokens = api_token_service.list_for_service_account(
        sa.id, include_revoked=include_revoked
    )
    return [_serialize_token(t) for t in tokens]


@router.delete(
    "/{service_account_id}/tokens/{token_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def revoke_token_for_sa(
    service_account_id: int,
    token_id: int,
    request: Request,
    db: Session = Depends(database.get_session),
    current_user: models.AppUser = Depends(
        app_auth.require_permission(Permission.USER_MANAGE)
    ),
):
    sa_service = ServiceAccountService(db)
    sa = sa_service.get(service_account_id)
    if sa is None:
        raise ApiError(
            "service_account.not_found",
            status.HTTP_404_NOT_FOUND,
            messages={
                "pt": "Service account não encontrada.",
                "en": "Service account not found.",
                "es": "Cuenta de servicio no encontrada.",
            },
        )
    _assert_sa_in_scope(sa, current_user, db)

    api_token_service = ApiTokenService(db)
    token = api_token_service.revoke_sa_token(
        service_account_id=sa.id, token_id=token_id
    )
    if token is None:
        raise ApiError(
            "service_account.token_not_found",
            status.HTTP_404_NOT_FOUND,
            messages={
                "pt": "Token não encontrado para esta service account.",
                "en": "Token not found for this service account.",
                "es": "Token no encontrado para esta cuenta de servicio.",
            },
        )

    _log_audit(
        db,
        request,
        action="service_account.token_revoked",
        status_code=status.HTTP_204_NO_CONTENT,
        user=current_user,
        detail={
            "service_account_id": sa.id,
            "service_account_name": sa.name,
            "token_id": token.id,
            "token_name": token.name,
            "token_prefix": token.token_prefix,
        },
    )
    return None
