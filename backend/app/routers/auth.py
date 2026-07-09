from __future__ import annotations

import hashlib
from datetime import datetime
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from ..api import schemas
from ..core import auth as app_auth
from ..core import identity_config, oidc, tenant
from ..core.config import settings
from ..core.errors import ApiError
from ..core.rate_limiter import auth_attempt_limiter
from ..core.security import hash_password, hash_session_token, verify_password
from ..db import database, models, repository
from ..services.audit import AuditService, get_client_ip
from ..services.emailer import send_email


MIN_PASSWORD_LENGTH = 10
AUTH_FAILURE_ALERT_THRESHOLD = 2
AUTH_RATE_LIMIT_ERROR = "Too many authentication attempts. Please try again later."
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def get_user_repo(db: Session = Depends(database.get_session)) -> repository.UserRepository:
    return repository.UserRepository(db)


def _validate_password_strength(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ApiError(
            "auth.weak_password",
            status.HTTP_400_BAD_REQUEST,
            messages={
                "pt": "A senha não atende aos requisitos de segurança.",
                "en": "Password does not meet security requirements.",
                "es": "La contraseña no cumple los requisitos de seguridad.",
            },
        )


def _serialize_session_user(user: models.AppUser) -> schemas.SessionUserRead:
    return schemas.SessionUserRead(
        id=user.uuid,
        username=user.username,
        email=user.email,
        display_name=user.display_name,
        auth_provider=user.auth_provider or "local",
        is_global=bool(user.is_global),
        organization_id=user.organization_id,
        organization_name=user.organization.name if user.organization else None,
        role=user.role,
        is_active=user.is_active,
        permissions=app_auth.get_user_permissions(user.role),
        locale=getattr(user, "locale", None),
    )


def _serialize_account_profile(user: models.AppUser) -> schemas.AccountProfileRead:
    """Perfil próprio para a página de conta (self-service).

    Superset seguro de ``SessionUserRead``: adiciona ``created_at``/
    ``last_login_at`` (úteis ao dono) e NUNCA expõe ``password_hash`` nem
    ``external_subject``."""
    return schemas.AccountProfileRead(
        id=user.uuid,
        username=user.username,
        email=user.email,
        display_name=user.display_name,
        auth_provider=user.auth_provider or "local",
        is_global=bool(user.is_global),
        organization_id=user.organization_id,
        organization_name=user.organization.name if user.organization else None,
        role=user.role,
        is_active=user.is_active,
        permissions=app_auth.get_user_permissions(user.role),
        locale=getattr(user, "locale", None),
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


def _current_session_id(request: Request, db: Session) -> int | None:
    """Id da sessão de browser ATUAL (via cookie), ou None.

    Usado pelas ações self-service que preservam o dispositivo atual ("sair das
    outras sessões", troca de senha). Requisições via PAT (Bearer, sem cookie)
    retornam None — não há sessão de browser a preservar."""
    raw_token = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if not raw_token:
        return None
    session = repository.UserSessionRepository(db).get_active_by_token_hash(
        hash_session_token(raw_token)
    )
    return session.id if session else None


def _serialize_user(user: models.AppUser) -> schemas.UserRead:
    return schemas.UserRead(
        id=user.uuid,
        username=user.username,
        email=user.email,
        display_name=user.display_name,
        auth_provider=user.auth_provider or "local",
        is_global=bool(user.is_global),
        organization_id=user.organization_id,
        organization_name=user.organization.name if user.organization else None,
        role=user.role,
        is_active=user.is_active,
        created_at=user.created_at,
        updated_at=user.updated_at,
        last_login_at=user.last_login_at,
    )


def _get_user_by_public_id(
    user_repo: repository.UserRepository,
    user_id: str,
) -> models.AppUser | None:
    user = user_repo.get_by_uuid(user_id)
    if user:
        return user
    if user_id.isdigit():
        return user_repo.get(int(user_id))
    return None


def _active_admin_count(repo: repository.UserRepository) -> int:
    return sum(1 for user in repo.list() if user.role == app_auth.ROLE_ADMIN and user.is_active)


def _log_auth_event(
    db: Session,
    request: Request,
    *,
    action: str,
    status_code: int,
    user: models.AppUser | None = None,
    username: str | None = None,
    user_role: str | None = None,
    detail: str | None = None,
) -> None:
    try:
        AuditService(db).log_event(
            action=action,
            endpoint=request.url.path,
            user=user,
            username=username,
            user_role=user_role,
            method=request.method,
            status_code=status_code,
            ip_address=get_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            detail=detail,
        )
    except Exception as exc:
        logger.warning("Failed to write auth audit log: %s", exc)


def _hash_username(username: str) -> str:
    """Retorna os primeiros 16 hex do SHA-256 do username normalizado.

    Evita armazenar usernames em plaintext no Redis (enumeração de usuários).
    Truncado em 16 chars para reduzir footprint de memória mantendo entropia
    suficiente para distinguir usuários distintos.
    """
    return hashlib.sha256(username.encode()).hexdigest()[:16]


def _build_auth_attempt_keys(request: Request, username: str | None) -> list[str]:
    ip_address = get_client_ip(request) or "unknown"
    keys = [f"auth:ip:{ip_address}"]

    normalized_username = username.strip().lower() if username else ""
    if normalized_username:
        # Usa hash do username em vez de plaintext — previne enumeração de usuários
        # via chaves Redis e limita cardinalidade de chave.
        username_hash = _hash_username(normalized_username)
        keys.append(f"auth:ip-user:{ip_address}:{username_hash}")

    return keys


def _get_auth_failure_count(request: Request, username: str | None) -> int:
    return max(
        (auth_attempt_limiter.failure_count(key) for key in _build_auth_attempt_keys(request, username)),
        default=0,
    )


def _send_auth_security_alert(
    db: Session,
    request: Request,
    *,
    action: str,
    status_code: int,
    user: models.AppUser | None = None,
    username: str | None = None,
    user_role: str | None = None,
    detail: str | None = None,
    failure_count: int | None = None,
    retry_after: int | None = None,
) -> None:
    try:
        recipients = [entry.email for entry in repository.EmailRepository(db).list()]
        if not recipients:
            return

        actor_username = username if username is not None else getattr(user, "username", None)
        normalized_username = actor_username.strip() if actor_username else ""
        actor_role = user_role if user_role is not None else getattr(user, "role", None)
        timestamp = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        client_ip = get_client_ip(request) or "unknown"
        user_agent = request.headers.get("user-agent") or "unknown"

        body_lines = [
            "Foi detectada atividade suspeita de autenticação.",
            "",
            f"Ação: {action}",
            f"Status HTTP: {status_code}",
            f"Usuário informado: {normalized_username or 'não informado'}",
            f"Perfil: {actor_role or 'desconhecido'}",
            f"Horario (UTC): {timestamp}",
            f"IP de origem: {client_ip}",
            f"Endpoint: {request.method} {request.url.path}",
            f"User-Agent: {user_agent}",
        ]

        if failure_count is not None:
            body_lines.append(f"Falhas consecutivas observadas: {failure_count}")

        if retry_after is not None:
            body_lines.append(f"Bloqueio ativo / retry after: {retry_after}s")

        if detail:
            body_lines.append(f"Detalhe: {detail}")

        send_email(
            recipients,
            f"Alerta de segurança: autenticação suspeita ({action})",
            "\n".join(body_lines),
        )
    except Exception as exc:
        logger.warning("Failed to send auth alert email: %s", exc)


def _handle_auth_failure_signal(
    db: Session,
    request: Request,
    *,
    action: str,
    status_code: int,
    lockout_action: str,
    user: models.AppUser | None = None,
    username: str | None = None,
    user_role: str | None = None,
    detail: str | None = None,
    failure_count: int,
    retry_after: int | None,
) -> None:
    if retry_after is not None:
        _log_auth_event(
            db,
            request,
            action=lockout_action,
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            user=user,
            username=username,
            user_role=user_role,
            detail=f"Authentication lockout triggered after {failure_count} consecutive failures. Retry after {retry_after}s",
        )

    if failure_count >= AUTH_FAILURE_ALERT_THRESHOLD or retry_after is not None:
        _send_auth_security_alert(
            db,
            request,
            action=action,
            status_code=status_code,
            user=user,
            username=username,
            user_role=user_role,
            detail=detail,
            failure_count=failure_count,
            retry_after=retry_after,
        )


def _enforce_auth_rate_limit(
    db: Session,
    request: Request,
    *,
    action: str,
    username: str | None,
    user_role: str | None = None,
) -> None:
    retry_after = 0
    ip_address = get_client_ip(request) or "unknown"

    # Verifica lockout de IP por DoS de cardinalidade.
    ip_lockout = auth_attempt_limiter.retry_after_ip_lockout(ip_address)
    if ip_lockout:
        retry_after = max(retry_after, ip_lockout)

    for key in _build_auth_attempt_keys(request, username):
        current_retry = auth_attempt_limiter.retry_after(key)
        if current_retry:
            retry_after = max(retry_after, current_retry)

    if retry_after <= 0:
        return

    _log_auth_event(
        db,
        request,
        action=action,
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        username=username,
        user_role=user_role,
        detail=f"Rate limit exceeded. Retry after {retry_after}s",
    )
    _send_auth_security_alert(
        db,
        request,
        action=action,
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        username=username,
        user_role=user_role,
        detail="Authentication request blocked by rate limiter",
        failure_count=_get_auth_failure_count(request, username),
        retry_after=retry_after,
    )
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=AUTH_RATE_LIMIT_ERROR,
        headers={"Retry-After": str(retry_after)},
    )


def _register_auth_failure(request: Request, username: str | None) -> tuple[int, int | None]:
    max_failure_count = 0
    retry_after = 0
    ip_address = get_client_ip(request) or "unknown"
    normalized_username = username.strip().lower() if username else ""

    for key in _build_auth_attempt_keys(request, username):
        # Para chaves que incluem username, usa registro com cardinality cap
        # (rastreia usernames distintos por IP para anti-DoS).
        if normalized_username and key.startswith(f"auth:ip-user:{ip_address}:"):
            username_hash = _hash_username(normalized_username)
            current_failure_count, current_retry_after = (
                auth_attempt_limiter.register_failure_with_cardinality(
                    key, ip_address, username_hash
                )
            )
        else:
            current_failure_count, current_retry_after = auth_attempt_limiter.register_failure(key)
        max_failure_count = max(max_failure_count, current_failure_count)
        if current_retry_after:
            retry_after = max(retry_after, current_retry_after)

    return max_failure_count, retry_after or None


def _reset_auth_failures(request: Request, username: str | None) -> None:
    for key in _build_auth_attempt_keys(request, username):
        auth_attempt_limiter.reset(key)


@router.get("/status", response_model=schemas.AuthStatusRead)
def auth_status(db: Session = Depends(database.get_session)):
    cfg = identity_config.load(db)
    sso_on = oidc.is_enabled(cfg)
    return schemas.AuthStatusRead(
        setup_required=not app_auth.users_exist(db),
        company_name=settings.APP_COMPANY_NAME,
        company_portal_name=settings.APP_COMPANY_PORTAL_NAME,
        sso_enabled=sso_on,
        sso_button_label=cfg.entra_button_label if sso_on else None,
    )


@router.post("/bootstrap", response_model=schemas.LoginResponse)
def bootstrap_admin(
    payload: schemas.BootstrapAdminRequest,
    request: Request,
    response: Response,
    db: Session = Depends(database.get_session),
    user_repo: repository.UserRepository = Depends(get_user_repo),
):
    _enforce_auth_rate_limit(
        db,
        request,
        action="bootstrap_admin_rate_limited",
        username=payload.username,
        user_role=app_auth.ROLE_ADMIN,
    )

    if app_auth.users_exist(db):
        failure_count, retry_after = _register_auth_failure(request, payload.username)
        _log_auth_event(
            db,
            request,
            action="bootstrap_admin_rejected",
            status_code=status.HTTP_409_CONFLICT,
            username=payload.username,
            user_role=app_auth.ROLE_ADMIN,
            detail="Initial admin already configured",
        )
        _handle_auth_failure_signal(
            db,
            request,
            action="bootstrap_admin_rejected",
            status_code=status.HTTP_409_CONFLICT,
            lockout_action="bootstrap_admin_lockout",
            username=payload.username,
            user_role=app_auth.ROLE_ADMIN,
            detail="Initial admin already configured",
            failure_count=failure_count,
            retry_after=retry_after,
        )
        raise ApiError(
            "auth.admin_already_configured",
            status.HTTP_409_CONFLICT,
            messages={
                "pt": "O administrador inicial já foi configurado.",
                "en": "Initial admin already configured.",
                "es": "El administrador inicial ya está configurado.",
            },
        )

    try:
        _validate_password_strength(payload.password)
    except ApiError:
        failure_count, retry_after = _register_auth_failure(request, payload.username)
        _log_auth_event(
            db,
            request,
            action="bootstrap_admin_failed",
            status_code=status.HTTP_400_BAD_REQUEST,
            username=payload.username,
            user_role=app_auth.ROLE_ADMIN,
            detail="Weak password",
        )
        _handle_auth_failure_signal(
            db,
            request,
            action="bootstrap_admin_failed",
            status_code=status.HTTP_400_BAD_REQUEST,
            lockout_action="bootstrap_admin_lockout",
            username=payload.username,
            user_role=app_auth.ROLE_ADMIN,
            detail="Weak password",
            failure_count=failure_count,
            retry_after=retry_after,
        )
        raise

    if user_repo.get_by_username(payload.username):
        failure_count, retry_after = _register_auth_failure(request, payload.username)
        _log_auth_event(
            db,
            request,
            action="bootstrap_admin_failed",
            status_code=status.HTTP_409_CONFLICT,
            username=payload.username,
            user_role=app_auth.ROLE_ADMIN,
            detail="Username already exists",
        )
        _handle_auth_failure_signal(
            db,
            request,
            action="bootstrap_admin_failed",
            status_code=status.HTTP_409_CONFLICT,
            lockout_action="bootstrap_admin_lockout",
            username=payload.username,
            user_role=app_auth.ROLE_ADMIN,
            detail="Username already exists",
            failure_count=failure_count,
            retry_after=retry_after,
        )
        raise ApiError(
            "auth.username_exists",
            status.HTTP_409_CONFLICT,
            messages={
                "pt": "Nome de usuário já existe.",
                "en": "Username already exists.",
                "es": "El nombre de usuario ya existe.",
            },
        )

    user = user_repo.add(
        models.AppUser(
            username=payload.username,
            display_name=payload.display_name,
            password_hash=hash_password(payload.password),
            organization_id=None,
            role=app_auth.ROLE_ADMIN,
            is_active=True,
        )
    )
    token, session = app_auth.create_user_session(
        user,
        db,
        user_agent=request.headers.get("user-agent"),
    )
    user_repo.mark_login(user)
    app_auth.set_session_cookie(response, token, session.expires_at)
    _reset_auth_failures(request, payload.username)
    _log_auth_event(
        db,
        request,
        action="bootstrap_admin_success",
        status_code=status.HTTP_200_OK,
        user=user,
        detail="Initial admin created",
    )
    return schemas.LoginResponse(user=_serialize_session_user(user), expires_at=session.expires_at)


@router.post("/login", response_model=schemas.LoginResponse)
def login(
    payload: schemas.LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(database.get_session),
    user_repo: repository.UserRepository = Depends(get_user_repo),
):
    _enforce_auth_rate_limit(
        db,
        request,
        action="login_rate_limited",
        username=payload.username,
    )

    if not app_auth.users_exist(db):
        failure_count, retry_after = _register_auth_failure(request, payload.username)
        _log_auth_event(
            db,
            request,
            action="login_rejected",
            status_code=status.HTTP_400_BAD_REQUEST,
            username=payload.username,
            detail="Initial admin setup is required",
        )
        _handle_auth_failure_signal(
            db,
            request,
            action="login_rejected",
            status_code=status.HTTP_400_BAD_REQUEST,
            lockout_action="login_lockout",
            username=payload.username,
            detail="Initial admin setup is required",
            failure_count=failure_count,
            retry_after=retry_after,
        )
        raise ApiError(
            "auth.setup_required",
            status.HTTP_400_BAD_REQUEST,
            messages={
                "pt": "É necessário configurar o administrador inicial.",
                "en": "Initial admin setup is required.",
                "es": "Es necesario configurar el administrador inicial.",
            },
        )

    user = user_repo.get_by_username(payload.username)
    if not user or not verify_password(payload.password, user.password_hash):
        failure_count, retry_after = _register_auth_failure(request, payload.username)
        logger.warning(
            "tentativa de login com credenciais inválidas",
            extra={
                "event": "auth.login_failed",
                "username": payload.username,  # não é campo sensível
                "ip": str(request.client.host) if request.client else None,
                "failure_count": failure_count,
            },
        )
        _log_auth_event(
            db,
            request,
            action="login_failed",
            status_code=status.HTTP_401_UNAUTHORIZED,
            username=payload.username,
            detail="Invalid credentials",
        )
        _handle_auth_failure_signal(
            db,
            request,
            action="login_failed",
            status_code=status.HTTP_401_UNAUTHORIZED,
            lockout_action="login_lockout",
            username=payload.username,
            detail="Invalid credentials",
            failure_count=failure_count,
            retry_after=retry_after,
        )
        raise ApiError(
            "auth.invalid_credentials",
            status.HTTP_401_UNAUTHORIZED,
            messages={
                "pt": "Credenciais inválidas.",
                "en": "Invalid credentials.",
                "es": "Credenciales inválidas.",
            },
        )

    if not user.is_active:
        failure_count, retry_after = _register_auth_failure(request, payload.username)
        logger.warning(
            "tentativa de login com conta inativa",
            extra={
                "event": "auth.login_inactive_user",
                "user_id": user.id,
                "ip": str(request.client.host) if request.client else None,
            },
        )
        _log_auth_event(
            db,
            request,
            action="login_failed",
            status_code=status.HTTP_401_UNAUTHORIZED,
            user=user,
            detail="User is inactive",
        )
        _handle_auth_failure_signal(
            db,
            request,
            action="login_failed",
            status_code=status.HTTP_401_UNAUTHORIZED,
            lockout_action="login_lockout",
            user=user,
            detail="User is inactive",
            failure_count=failure_count,
            retry_after=retry_after,
        )
        raise ApiError(
            "auth.invalid_credentials",
            status.HTTP_401_UNAUTHORIZED,
            messages={
                "pt": "Credenciais inválidas.",
                "en": "Invalid credentials.",
                "es": "Credenciales inválidas.",
            },
        )

    repository.UserSessionRepository(db).delete_expired()
    token, session = app_auth.create_user_session(
        user,
        db,
        user_agent=request.headers.get("user-agent"),
    )
    user_repo.mark_login(user)
    app_auth.set_session_cookie(response, token, session.expires_at)
    _reset_auth_failures(request, payload.username)
    logger.info(
        "login bem-sucedido",
        extra={
            "event": "auth.login_success",
            "user_id": user.id,
            "role": user.role,
            "org_id": user.organization_id,
        },
    )
    _log_auth_event(
        db,
        request,
        action="login_success",
        status_code=status.HTTP_200_OK,
        user=user,
    )
    return schemas.LoginResponse(user=_serialize_session_user(user), expires_at=session.expires_at)


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    current_user: models.AppUser = Depends(app_auth.require_authenticated_user),
    db: Session = Depends(database.get_session),
):
    app_auth.revoke_session(request.cookies.get(settings.SESSION_COOKIE_NAME), db)
    app_auth.clear_session_cookie(response)
    _log_auth_event(
        db,
        request,
        action="logout",
        status_code=status.HTTP_200_OK,
        user=current_user,
    )
    return {"detail": "Logged out"}


@router.get("/me", response_model=schemas.SessionUserRead)
def get_me(current_user: models.AppUser = Depends(app_auth.require_authenticated_user)):
    return _serialize_session_user(current_user)


@router.put("/me/locale", response_model=schemas.SessionUserRead)
def set_my_locale(
    body: schemas.LocaleUpdate,
    db: Session = Depends(database.get_session),
    current_user: models.AppUser = Depends(app_auth.require_authenticated_user),
):
    """Persiste a preferência de idioma da UI do usuário.

    O seletor de idioma do SPA chama isto para que a escolha siga o usuário entre
    dispositivos (o backend passa a devolver ``locale`` em ``/auth/me``, que o SPA
    usa como prioridade máxima de detecção)."""
    current_user.locale = body.locale
    db.add(current_user)
    db.commit()
    db.refresh(current_user)
    return _serialize_session_user(current_user)


@router.get("/me/profile", response_model=schemas.AccountProfileRead)
def get_my_profile(
    current_user: models.AppUser = Depends(app_auth.require_authenticated_user),
):
    """Perfil completo do próprio usuário para a página de conta (self-service).

    Só lê a identidade do CALLER — nenhum escopo de org é consultado (o dono
    sempre pode ver a si mesmo). Campos administrativos (role/org/is_global) vêm
    read-only; a mutação deles continua exclusiva do endpoint de admin."""
    return _serialize_account_profile(current_user)


@router.patch("/me", response_model=schemas.AccountProfileRead)
def update_my_profile(
    body: schemas.SelfProfileUpdate,
    request: Request,
    db: Session = Depends(database.get_session),
    user_repo: repository.UserRepository = Depends(get_user_repo),
    current_user: models.AppUser = Depends(app_auth.require_authenticated_user),
):
    """Atualiza os campos que o usuário pode alterar em SI MESMO.

    Lista de permissão explícita (``SelfProfileUpdate`` = display_name/email/
    locale): não há como tocar em role, organização, escopo global ou status —
    esses campos nem existem no schema (defesa contra mass-assignment) e seguem
    exclusivos do endpoint de admin. Trocar o e-mail é sensível: exige a senha
    atual (reautenticação) e é recusado para contas federadas (e-mail vem do
    IdP)."""
    import json as _json

    is_local = (current_user.auth_provider or "local") == "local"
    changed_fields: list[str] = []

    if "display_name" in body.model_fields_set:
        if current_user.display_name != body.display_name:
            current_user.display_name = body.display_name
            changed_fields.append("display_name")

    if "locale" in body.model_fields_set:
        if getattr(current_user, "locale", None) != body.locale:
            current_user.locale = body.locale
            changed_fields.append("locale")

    if "email" in body.model_fields_set and body.email != current_user.email:
        # Contas federadas: e-mail é derivado do IdP — imutável no produto.
        if not is_local:
            raise ApiError(
                "auth.email_managed_by_idp",
                status.HTTP_403_FORBIDDEN,
                messages={
                    "pt": "Seu e-mail é gerenciado pelo provedor de identidade.",
                    "en": "Your email is managed by your identity provider.",
                    "es": "Tu correo es gestionado por tu proveedor de identidad.",
                },
            )
        # Mudança sensível → reautenticação com a senha atual (anti-sequestro
        # de sessão). Rate-limit + auditoria como nas rotas de login.
        _enforce_auth_rate_limit(
            db,
            request,
            action="profile_email_change_rate_limited",
            username=current_user.username,
            user_role=current_user.role,
        )
        if not body.current_password or not verify_password(
            body.current_password, current_user.password_hash or ""
        ):
            failure_count, retry_after = _register_auth_failure(request, current_user.username)
            _log_auth_event(
                db,
                request,
                action="profile_email_change_failed",
                status_code=status.HTTP_401_UNAUTHORIZED,
                user=current_user,
                detail="Invalid current password on email change",
            )
            _handle_auth_failure_signal(
                db,
                request,
                action="profile_email_change_failed",
                status_code=status.HTTP_401_UNAUTHORIZED,
                lockout_action="profile_email_change_lockout",
                user=current_user,
                detail="Invalid current password on email change",
                failure_count=failure_count,
                retry_after=retry_after,
            )
            raise ApiError(
                "auth.invalid_credentials",
                status.HTTP_401_UNAUTHORIZED,
                messages={
                    "pt": "Credenciais inválidas.",
                    "en": "Invalid credentials.",
                    "es": "Credenciales inválidas.",
                },
            )
        if body.email:
            existing_email = user_repo.get_by_email(body.email)
            if existing_email and existing_email.id != current_user.id:
                raise ApiError(
                    "auth.email_exists",
                    status.HTTP_409_CONFLICT,
                    messages={
                        "pt": "E-mail já cadastrado.",
                        "en": "Email already exists.",
                        "es": "El correo electrónico ya existe.",
                    },
                )
        _reset_auth_failures(request, current_user.username)
        current_user.email = body.email
        changed_fields.append("email")

    if changed_fields:
        db.add(current_user)
        db.commit()
        db.refresh(current_user)
        try:
            AuditService(db).log_event(
                action="profile_self_update",
                endpoint=request.url.path,
                method=request.method,
                status_code=status.HTTP_200_OK,
                user=current_user,
                detail=_json.dumps({"fields": sorted(changed_fields)}),
            )
        except Exception as exc:
            logger.warning("Falha ao gravar auditoria de profile_self_update: %s", exc)

    return _serialize_account_profile(current_user)


@router.post("/me/password")
def change_my_password(
    body: schemas.PasswordChange,
    request: Request,
    db: Session = Depends(database.get_session),
    current_user: models.AppUser = Depends(app_auth.require_authenticated_user),
):
    """Troca a própria senha (contas locais).

    Exige a senha atual (reautenticação), aplica a MESMA política de força do
    cadastro e, ao concluir, revoga todas as OUTRAS sessões do usuário — o
    dispositivo atual permanece logado. Contas federadas (Entra/OIDC) não têm
    senha local: 403 com orientação para usar o IdP. Rate-limit + auditoria
    espelham o fluxo de login."""
    import json as _json

    _enforce_auth_rate_limit(
        db,
        request,
        action="password_change_rate_limited",
        username=current_user.username,
        user_role=current_user.role,
    )

    is_local = (current_user.auth_provider or "local") == "local"
    if not is_local or not current_user.password_hash:
        _log_auth_event(
            db,
            request,
            action="password_change_rejected",
            status_code=status.HTTP_403_FORBIDDEN,
            user=current_user,
            detail="Federated account has no local password",
        )
        raise ApiError(
            "auth.password_managed_by_idp",
            status.HTTP_403_FORBIDDEN,
            messages={
                "pt": "Sua senha é gerenciada pelo provedor de identidade.",
                "en": "Your password is managed by your identity provider.",
                "es": "Tu contraseña es gestionada por tu proveedor de identidad.",
            },
        )

    if not verify_password(body.current_password, current_user.password_hash):
        failure_count, retry_after = _register_auth_failure(request, current_user.username)
        _log_auth_event(
            db,
            request,
            action="password_change_failed",
            status_code=status.HTTP_401_UNAUTHORIZED,
            user=current_user,
            detail="Invalid current password",
        )
        _handle_auth_failure_signal(
            db,
            request,
            action="password_change_failed",
            status_code=status.HTTP_401_UNAUTHORIZED,
            lockout_action="password_change_lockout",
            user=current_user,
            detail="Invalid current password",
            failure_count=failure_count,
            retry_after=retry_after,
        )
        raise ApiError(
            "auth.invalid_credentials",
            status.HTTP_401_UNAUTHORIZED,
            messages={
                "pt": "Credenciais inválidas.",
                "en": "Invalid credentials.",
                "es": "Credenciales inválidas.",
            },
        )

    _validate_password_strength(body.new_password)

    if verify_password(body.new_password, current_user.password_hash):
        raise ApiError(
            "auth.password_reuse",
            status.HTTP_400_BAD_REQUEST,
            messages={
                "pt": "A nova senha deve ser diferente da senha atual.",
                "en": "The new password must be different from the current one.",
                "es": "La nueva contraseña debe ser distinta de la actual.",
            },
        )

    keep_session_id = _current_session_id(request, db)
    current_user.password_hash = hash_password(body.new_password)
    db.add(current_user)

    # Atomicidade: a troca de senha e a revogação das OUTRAS sessões precisam
    # cair (ou falhar) juntas. Não damos commit da senha aqui — o commit interno
    # de revoke_all_for_user_except descarrega AMBAS as mudanças numa única
    # transação. Se a revogação falhar, a senha também reverte (fail-safe: o
    # usuário tenta de novo), em vez de "senha trocada mas sessão comprometida
    # ainda viva".
    revoked = repository.UserSessionRepository(db).revoke_all_for_user_except(
        current_user.id, keep_session_id
    )
    _reset_auth_failures(request, current_user.username)
    _log_auth_event(
        db,
        request,
        action="password_change_success",
        status_code=status.HTTP_200_OK,
        user=current_user,
        detail=_json.dumps({"revoked_other_sessions": revoked}),
    )
    return {"detail": "password_changed", "revoked_other_sessions": revoked}


@router.post("/me/sessions/revoke-others")
def revoke_my_other_sessions(
    request: Request,
    db: Session = Depends(database.get_session),
    current_user: models.AppUser = Depends(app_auth.require_authenticated_user),
):
    """Encerra todas as OUTRAS sessões do usuário, mantendo a atual.

    Botão "sair de todos os outros dispositivos" da página de conta. Opera só
    sobre o próprio ``user.id``; não toca em PATs (esses são revogados
    individualmente na página de tokens)."""
    import json as _json

    keep_session_id = _current_session_id(request, db)
    revoked = repository.UserSessionRepository(db).revoke_all_for_user_except(
        current_user.id, keep_session_id
    )
    _log_auth_event(
        db,
        request,
        action="sessions_revoke_others",
        status_code=status.HTTP_200_OK,
        user=current_user,
        detail=_json.dumps({"revoked": revoked}),
    )
    return {"revoked": revoked}


@router.get("/permissions")
def list_permissions(
    _: models.AppUser = Depends(app_auth.require_authenticated_user),
) -> dict:
    """Retorna a matriz completa de papel × permissão para o frontend."""
    return {
        role: sorted(perms)
        for role, perms in app_auth.ROLE_PERMISSIONS.items()
    }


@router.get("/admin-access")
def verify_admin_access(_: models.AppUser = Depends(app_auth.require_admin_user)):
    return {"allowed": True}


@router.get("/users", response_model=list[schemas.UserRead])
def list_users(
    user_repo: repository.UserRepository = Depends(get_user_repo),
    current_user: models.AppUser = Depends(app_auth.require_permission(app_auth.Permission.USER_MANAGE)),
):
    # admin-de-org (escopado) só enxerga usuários da própria
    # subárvore — nem usuários de outras orgs, nem usuários de PLATAFORMA
    # (organization_id NULL / is_global). Admin global (org_ids=None) vê todos.
    users = user_repo.list()
    org_ids = tenant.accessible_org_ids(current_user, user_repo.db)
    if org_ids is not None:
        users = [
            u
            for u in users
            if u.organization_id in org_ids
            and not bool(getattr(u, "is_global", False))
        ]
    return [_serialize_user(user) for user in users]


@router.post("/users", response_model=schemas.UserRead)
def create_user(
    payload: schemas.UserCreate,
    request: Request,
    user_repo: repository.UserRepository = Depends(get_user_repo),
    db: Session = Depends(database.get_session),
    current_user: models.AppUser = Depends(app_auth.require_permission(app_auth.Permission.USER_MANAGE)),
):
    import json as _json

    _validate_password_strength(payload.password)

    if user_repo.get_by_username(payload.username):
        raise ApiError(
            "auth.username_exists",
            status.HTTP_409_CONFLICT,
            messages={
                "pt": "Nome de usuário já existe.",
                "en": "Username already exists.",
                "es": "El nombre de usuario ya existe.",
            },
        )

    if payload.email and user_repo.get_by_email(payload.email):
        raise ApiError(
            "auth.email_exists",
            status.HTTP_409_CONFLICT,
            messages={
                "pt": "E-mail já cadastrado.",
                "en": "Email already exists.",
                "es": "El correo electrónico ya existe.",
            },
        )

    if payload.organization_id is not None:
        org = repository.OrganizationRepository(user_repo.db).get(payload.organization_id)
        if not org:
            raise ApiError(
                "org.not_found",
                status.HTTP_404_NOT_FOUND,
                messages={
                    "pt": "Organização não encontrada.",
                    "en": "Organization not found.",
                    "es": "Organización no encontrada.",
                },
            )
        if not org.is_active:
            raise ApiError(
                "org.inactive",
                status.HTTP_409_CONFLICT,
                messages={
                    "pt": "A organização está inativa.",
                    "en": "Organization is inactive.",
                    "es": "La organización está inactiva.",
                },
            )

    # anti-escalonamento: um admin ESCOPADO não pode conceder
    # escopo global, não pode criar usuário sem org, e a org alvo precisa estar
    # na sua subárvore. No-op para admin global (flag OFF ⇒ todo admin é global).
    tenant.enforce_admin_delegation_scope(
        current_user,
        target_org_id=payload.organization_id,
        target_is_global=payload.is_global,
        session=db,
    )

    new_user = user_repo.add(
        models.AppUser(
            username=payload.username,
            email=payload.email,
            display_name=payload.display_name,
            password_hash=hash_password(payload.password),
            auth_provider="local",
            organization_id=payload.organization_id,
            role=payload.role,
            is_global=payload.is_global,
            is_active=True,
        )
    )

    # Auditoria de criação de usuário
    try:
        AuditService(db).log_event(
            action="user_created",
            endpoint=request.url.path,
            method="POST",
            status_code=200,
            user=current_user,
            detail=_json.dumps({
                "target_user_id": new_user.uuid,
                "target_username": new_user.username,
                "role": new_user.role,
            }),
        )
    except Exception as exc:
        logger.warning("Falha ao gravar auditoria de user_created: %s", exc)

    return _serialize_user(new_user)


@router.put("/users/{user_id}", response_model=schemas.UserRead)
def update_user(
    user_id: str,
    payload: schemas.UserUpdate,
    request: Request,
    response: Response,
    user_repo: repository.UserRepository = Depends(get_user_repo),
    db: Session = Depends(database.get_session),
    current_user: models.AppUser = Depends(app_auth.require_permission(app_auth.Permission.USER_MANAGE)),
):
    user = _get_user_by_public_id(user_repo, user_id)
    if not user:
        raise ApiError(
            "auth.user_not_found",
            status.HTTP_404_NOT_FOUND,
            messages={
                "pt": "Usuário não encontrado.",
                "en": "User not found.",
                "es": "Usuario no encontrado.",
            },
        )

    if payload.username and payload.username != user.username:
        existing_user = user_repo.get_by_username(payload.username)
        if existing_user and existing_user.id != user.id:
            raise ApiError(
                "auth.username_exists",
                status.HTTP_409_CONFLICT,
                messages={
                    "pt": "Nome de usuário já existe.",
                    "en": "Username already exists.",
                    "es": "El nombre de usuario ya existe.",
                },
            )

    if (
        "email" in payload.model_fields_set
        and payload.email
        and payload.email != user.email
    ):
        existing_email = user_repo.get_by_email(payload.email)
        if existing_email and existing_email.id != user.id:
            raise ApiError(
                "auth.email_exists",
                status.HTTP_409_CONFLICT,
                messages={
                    "pt": "E-mail já cadastrado.",
                    "en": "Email already exists.",
                    "es": "El correo electrónico ya existe.",
                },
            )

    next_role = payload.role if payload.role is not None else user.role
    next_is_active = payload.is_active if payload.is_active is not None else user.is_active

    if user.role == app_auth.ROLE_ADMIN and (next_role != app_auth.ROLE_ADMIN or not next_is_active):
        if _active_admin_count(user_repo) <= 1:
            raise ApiError(
                "auth.last_admin_required",
                status.HTTP_400_BAD_REQUEST,
                messages={
                    "pt": "É necessário manter ao menos um administrador ativo.",
                    "en": "At least one active admin must remain.",
                    "es": "Debe permanecer al menos un administrador activo.",
                },
            )

    if current_user.id == user.id and not next_is_active:
        raise ApiError(
            "auth.cannot_deactivate_self",
            status.HTTP_400_BAD_REQUEST,
            messages={
                "pt": "Você não pode desativar a própria conta.",
                "en": "You cannot deactivate your own account.",
                "es": "No puedes desactivar tu propia cuenta.",
            },
        )

    if payload.organization_id is not None:
        org = repository.OrganizationRepository(user_repo.db).get(payload.organization_id)
        if not org:
            raise ApiError(
                "org.not_found",
                status.HTTP_404_NOT_FOUND,
                messages={
                    "pt": "Organização não encontrada.",
                    "en": "Organization not found.",
                    "es": "Organización no encontrada.",
                },
            )
        if not org.is_active:
            raise ApiError(
                "org.inactive",
                status.HTTP_409_CONFLICT,
                messages={
                    "pt": "A organização está inativa.",
                    "en": "Organization is inactive.",
                    "es": "La organización está inactiva.",
                },
            )

    # anti-escalonamento (mesmas regras do create), sobre os
    # valores EFETIVOS pós-update (campo omitido herda o do usuário atual).
    _next_is_global = payload.is_global if payload.is_global is not None else user.is_global
    _next_org_id = (
        payload.organization_id
        if "organization_id" in payload.model_fields_set
        else user.organization_id
    )
    tenant.enforce_admin_delegation_scope(
        current_user,
        target_org_id=_next_org_id,
        target_is_global=_next_is_global,
        session=db,
    )

    password_hash = None
    if payload.password:
        _validate_password_strength(payload.password)
        password_hash = hash_password(payload.password)

    previous_role = user.role

    update_kwargs = {
        "username": payload.username,
        "display_name": payload.display_name,
        "password_hash": password_hash,
        "role": payload.role,
        "is_global": payload.is_global,
        "is_active": payload.is_active,
    }
    if "organization_id" in payload.model_fields_set:
        update_kwargs["organization_id"] = payload.organization_id
    if "email" in payload.model_fields_set:
        update_kwargs["email"] = payload.email

    updated = user_repo.update(user, **update_kwargs)

    # Auditoria de mudança de papel
    if payload.role is not None and payload.role != previous_role:
        import json as _json
        try:
            AuditService(db).log_event(
                action="role_change",
                endpoint=request.url.path,
                user=current_user,
                method=request.method,
                status_code=status.HTTP_200_OK,
                detail=_json.dumps({
                    "target_user_id": updated.id,
                    "target_username": updated.username,
                    "previous_role": previous_role,
                    "new_role": updated.role,
                }),
            )
        except Exception as exc:
            logger.warning("Falha ao gravar auditoria de role_change: %s", exc)

    if password_hash is not None or not updated.is_active:
        repository.UserSessionRepository(user_repo.db).revoke_all_for_user(updated.id)
        if current_user.id == updated.id:
            app_auth.clear_session_cookie(response)

    # Offboarding: desativar a conta revoga também os PATs (sessões já foram
    # revogadas acima). Fecha o gap de tokens sobreviverem à desativação.
    if not updated.is_active:
        from ..services.api_tokens import ApiTokenService

        revoked_pats = ApiTokenService(user_repo.db).revoke_all_for_user(updated.id)
        if revoked_pats:
            logger.info(
                "user %s desativado: %d PAT(s) revogado(s)", updated.uuid, revoked_pats
            )

    return _serialize_user(updated)


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: str,
    request: Request,
    user_repo: repository.UserRepository = Depends(get_user_repo),
    db: Session = Depends(database.get_session),
    current_user: models.AppUser = Depends(app_auth.require_permission(app_auth.Permission.USER_MANAGE)),
):
    import json as _json

    user = _get_user_by_public_id(user_repo, user_id)
    if not user:
        raise ApiError(
            "auth.user_not_found",
            status.HTTP_404_NOT_FOUND,
            messages={
                "pt": "Usuário não encontrado.",
                "en": "User not found.",
                "es": "Usuario no encontrado.",
            },
        )

    if current_user.id == user.id:
        raise ApiError(
            "auth.cannot_delete_self",
            status.HTTP_400_BAD_REQUEST,
            messages={
                "pt": "Você não pode excluir a própria conta.",
                "en": "You cannot delete your own account.",
                "es": "No puedes eliminar tu propia cuenta.",
            },
        )

    # admin escopado nunca deleta ACIMA do próprio teto —
    # alvo global (is_global), de plataforma (org NULL) ou fora da subárvore → 403.
    tenant.enforce_admin_delegation_scope(
        current_user,
        target_org_id=user.organization_id,
        target_is_global=bool(getattr(user, "is_global", False)),
        session=db,
    )

    if user.role == app_auth.ROLE_ADMIN and user.is_active and _active_admin_count(user_repo) <= 1:
        raise ApiError(
            "auth.last_admin_required",
            status.HTTP_400_BAD_REQUEST,
            messages={
                "pt": "É necessário manter ao menos um administrador ativo.",
                "en": "At least one active admin must remain.",
                "es": "Debe permanecer al menos un administrador activo.",
            },
        )

    # Auditoria ANTES do delete — captura dados do usuário enquanto existe
    try:
        AuditService(db).log_event(
            action="user_deleted",
            endpoint=request.url.path,
            method="DELETE",
            status_code=204,
            user=current_user,
            detail=_json.dumps({
                "target_user_id": user.uuid,
                "target_username": user.username,
                "role_at_deletion": user.role,
            }),
        )
    except Exception as exc:
        logger.warning("Falha ao gravar auditoria de user_deleted: %s", exc)

    repository.UserSessionRepository(user_repo.db).revoke_all_for_user(user.id)
    user_repo.delete(user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
