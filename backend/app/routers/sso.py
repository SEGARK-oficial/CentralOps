"""Login federado via Microsoft Entra (OIDC Authorization Code + PKCE).

Rotas PÚBLICAS (incluídas no main sem ``protected_api``). Backend-driven: o
browser navega para ``/api/auth/sso/login`` (302 → Microsoft) e retorna em
``/api/auth/sso/callback``, onde validamos o id_token, fazemos JIT do
``AppUser`` e criamos a MESMA sessão cookie do login local
(``core.auth.create_user_session``). Erros voltam ao frontend como
``/login?sso_error=<code>`` — sem vazar detalhe sensível na URL.

Fase 2: a configuração vem do banco (``core.identity_config.load``), editável
pela UI; o ``.env`` é apenas fallback/seed.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..core import auth as app_auth
from ..core import identity_config, oidc
from ..db import database, models, repository
from ..services.audit import AuditService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/sso", tags=["auth", "sso"])

_STATE_TTL_SECONDS = 600  # 10 min para completar o fluxo
_LOGIN_PATH = "/login"


def _error_redirect(reason: str) -> RedirectResponse:
    # 303 → o browser faz GET na tela de login; só o code do erro vai na URL.
    return RedirectResponse(
        url=f"{_LOGIN_PATH}?sso_error={reason}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _audit_safe(db: Session, **kwargs) -> None:
    try:
        AuditService(db).log_event(**kwargs)
    except Exception as exc:  # pragma: no cover — best-effort
        logger.warning("Falha ao gravar auditoria SSO: %s", exc)


def _derive_username(user_repo: repository.UserRepository, identity: oidc.OidcIdentity) -> str:
    base = identity.email or f"entra-{identity.subject}"
    if user_repo.get_by_username(base) is None:
        return base
    # Colisão improvável (o e-mail já foi checado); desambigua com o subject.
    return f"{base}#{identity.subject[:8]}"


@router.get("/login")
def sso_login(request: Request, db: Session = Depends(database.get_session)):
    cfg = identity_config.load(db)
    if not oidc.is_enabled(cfg):
        return _error_redirect("sso_disabled")

    repo = repository.OidcAuthStateRepository(db)
    try:
        repo.delete_expired()  # housekeeping best-effort
    except Exception:  # pragma: no cover
        pass

    verifier, challenge = oidc.generate_pkce_pair()
    state = oidc.generate_state()
    nonce = oidc.generate_nonce()
    repo.create(state=state, nonce=nonce, code_verifier=verifier, ttl_seconds=_STATE_TTL_SECONDS)

    try:
        url = oidc.build_authorization_url(cfg, state=state, nonce=nonce, code_challenge=challenge)
    except oidc.OidcError as exc:
        logger.warning("SSO discovery falhou: %s", exc)
        return _error_redirect("discovery_failed")

    return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)


@router.get("/callback")
def sso_callback(
    request: Request,
    db: Session = Depends(database.get_session),
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
):
    cfg = identity_config.load(db)
    if not oidc.is_enabled(cfg):
        return _error_redirect("sso_disabled")

    if error:  # erro do próprio Entra (ex: consentimento negado)
        logger.info("SSO IdP retornou erro: %s (%s)", error, error_description)
        return _error_redirect("provider_error")

    if not code or not state:
        return _error_redirect("missing_params")

    consumed = repository.OidcAuthStateRepository(db).consume(state)
    if consumed is None:
        return _error_redirect("invalid_state")
    nonce, code_verifier, _redirect_to = consumed

    try:
        token = oidc.exchange_code(cfg, code=code, code_verifier=code_verifier)
        claims = oidc.validate_id_token(cfg, token.get("id_token", ""), nonce=nonce)
        identity = oidc.map_identity(cfg, claims)
    except oidc.OidcError as exc:
        logger.warning("SSO callback inválido: %s", exc)
        return _error_redirect("invalid_token")

    if not oidc.email_domain_allowed(cfg, identity.email):
        return _error_redirect("email_not_allowed")

    user_repo = repository.UserRepository(db)
    user = user_repo.get_by_external_subject("entra", identity.subject)

    if user is None:
        # Anti account-takeover: não vincular automaticamente a um e-mail que
        # já pertence a outra conta (local ou outro subject Entra).
        if identity.email and user_repo.get_by_email(identity.email):
            logger.warning("SSO: e-mail já vinculado a outra conta — recusado")
            return _error_redirect("email_conflict")
        if not cfg.entra_jit_provisioning:
            return _error_redirect("not_provisioned")
        user = user_repo.add(
            models.AppUser(
                username=_derive_username(user_repo, identity),
                email=identity.email,
                display_name=identity.display_name,
                auth_provider="entra",
                external_subject=identity.subject,
                password_hash=None,
                role=identity.role,
                is_global=identity.is_global,
                is_active=True,
            )
        )
        _audit_safe(
            db,
            action="sso_user_provisioned",
            endpoint=request.url.path,
            method="GET",
            status_code=200,
            user=user,
            detail=json.dumps(
                {"username": user.username, "role": user.role, "email": user.email}
            ),
        )
    else:
        if not user.is_active:
            return _error_redirect("user_inactive")
        # IdP é a fonte de verdade da identidade — reconcilia a cada login.
        user_repo.update(
            user,
            email=identity.email,
            display_name=identity.display_name,
            role=identity.role,
            is_global=identity.is_global,
        )

    user_repo.mark_login(user)

    session_token, session = app_auth.create_user_session(
        user, db, user_agent=request.headers.get("user-agent")
    )
    _audit_safe(
        db,
        action="sso_login",
        endpoint=request.url.path,
        method="GET",
        status_code=200,
        user=user,
        detail=json.dumps({"username": user.username, "via": "entra"}),
    )

    redirect = RedirectResponse(
        url=cfg.entra_post_login_redirect or "/",
        status_code=status.HTTP_303_SEE_OTHER,
    )
    app_auth.set_session_cookie(redirect, session_token, session.expires_at)
    return redirect
