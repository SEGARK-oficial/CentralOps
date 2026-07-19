"""Helpers de autenticação e dependências de rotas da aplicação."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Callable, Optional

from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from ..db import database, models, repository
from .config import settings
from .security import generate_session_token, hash_session_token


logger = logging.getLogger(__name__)


# Prefixo Bearer obrigatório no header Authorization para PATs.
_BEARER_SCHEME_PREFIX = "Bearer "


# ── AppUser shim para Service Accounts ───────────────────────────────
#
# Quando um request chega autenticado por PAT-de-SA, precisamos retornar
# um objeto que se passa por ``AppUser`` para o resto do request handler
# (routers, audit, permission checks). Em vez de refatorar 9+ routers
# pra trabalhar com um ``AuthPrincipal`` abstrato, criamos um *shim*:
#
#   - Instância **transient** de ``AppUser`` (não persistida, não
#     adicionada à session SQLAlchemy).
#   - ``id`` sintético (negativo) — evita colisão com IDs reais e fica
#     reconhecível em logs ("user_id=-12 é o shim do SA id=12").
#   - ``username = "sa:<name>"`` — diferenciável no audit log sem
#     exigir migration de schema.
#   - ``role`` = role do SA — alimenta o RBAC normalmente.
#   - ``organization_id`` herdado do SA quando aplicável.
#   - ``is_active`` herdado do SA (resolver já valida, mas double-check).
#
# Risco coberto: nenhum router atual chama ``db.merge(current_user)`` ou
# ``db.refresh(current_user)`` — tais
# chamadas quebrariam com transient instance porque SQLAlchemy tentaria
# carregar o ID negativo. Se algum router futuro fizer isso, vai
# explodir cedo (é uma assertion de tipo "este código não esperava SA").


def persistable_user_id(user_or_id) -> Optional[int]:
    """Id utilizável em colunas FK para ``app_users`` — ou ``None``.

    Service accounts autenticam como um SHIM transient de ``AppUser`` com id
    sintético NEGATIVO (``-<sa.id>``, ver ``_build_sa_appuser_shim``) que NÃO
    existe na tabela. Gravar esse id em FKs (``mapping_versions.author_user_id``,
    ``audit_logs.user_id``) viola a constraint — foi o 500 do create_version
    via MCP + a perda silenciosa de audit_logs de SA (jul/2026). Persistência:
    usuário real → id; SA/shim/ausente → ``None`` (a atribuição fica no
    ``username='sa:<name>'``, que os writers já gravam ao lado).

    Aceita a instância de ``AppUser`` OU o id cru (int/None) — cobre tanto os
    routers (têm o user) quanto o middleware de audit (têm só o id no state).
    """
    uid = getattr(user_or_id, "id", user_or_id)
    return uid if isinstance(uid, int) and not isinstance(uid, bool) and uid > 0 else None


def _build_sa_appuser_shim(sa: models.ServiceAccount) -> models.AppUser:
    """Cria um AppUser transient pra representar o SA durante o request.

    NÃO adiciona à session. NÃO commita. Vive somente em memória pelo
    ciclo do request.
    """
    shim = models.AppUser(
        id=-sa.id,  # ID sintético negativo — evita colisão com IDs reais
        uuid=f"sa:{sa.name}",  # Diferenciável em logs UUID
        username=f"sa:{sa.name}",
        display_name=sa.description or sa.name,
        password_hash="!disabled-service-account",  # Nunca usado p/ login
        auth_provider="local",
        organization_id=sa.organization_id,
        role=sa.role,
        # SA herda escopo via role (admin → global); nunca recebe is_global
        # implícito — escopo global de SA é decidido só pela role.
        is_global=False,
        is_active=sa.is_active,
    )
    # Nota: shim.id < 0 é o sinal "não persista isso". Se algum código
    # tentar fazer ``db.add(current_user)`` vai violar a PK; aceitável
    # — preferível detectar cedo do que silenciar bug.
    return shim


# ── Papéis (RBAC) ─────────────────────────────────────────────────────


class UserRole(StrEnum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    ENGINEER = "engineer"
    ADMIN = "admin"


# Aliases retrocompat — valores usados em comparações legadas
ROLE_ADMIN: str = UserRole.ADMIN
ROLE_USER: str = "user"   # papel legado migrado para viewer na DB
VALID_ROLES: set[str] = {r.value for r in UserRole}


# ── Permissões ────────────────────────────────────────────────────────


class Permission(StrEnum):
    MAPPING_READ = "mapping.read"
    MAPPING_WRITE = "mapping.write"
    MAPPING_ROLLBACK = "mapping.rollback"
    INTEGRATION_READ = "integration.read"
    INTEGRATION_WRITE = "integration.write"
    INTEGRATION_PAUSE = "integration.pause"
    QUARANTINE_READ = "quarantine.read"
    QUARANTINE_DISCARD = "quarantine.discard"
    DRIFT_READ = "drift.read"
    DRIFT_IGNORE = "drift.ignore"
    DRIFT_MARK_MAPPED = "drift.mark_mapped"
    DRIFT_DELETE = "drift.delete"
    USER_MANAGE = "user.manage"
    SECRET_READ = "secret.read"
    AUDIT_READ = "audit.read"
    ORG_MANAGE = "org.manage"
    # Service-to-service tenant resolution.
    # Service Accounts com role >= operator podem ser autorizadas a este scope.
    INTERNAL_TENANT_READ = "internal.tenant.read"
    # Capability como unidade de AUTORIZAÇÃO. Rodar query ao vivo
    # na fonte do cliente (custa $ / toca o tenant) e salvar query/agendamento são
    # permissões DISTINTAS — não mais proxy de INTEGRATION_READ (viewer rodava query)
    # / INTEGRATION_WRITE. ACTION_BLOCK removido: response actions descontinuadas.
    QUERY_RUN = "query.run"
    QUERY_SAVE = "query.save"
    # ADR-0015 Fase 3 — testar uma regra de correlação contra AMOSTRAS REAIS do
    # reservoir. Permissão PRÓPRIA, e não reuso de outra, por um motivo de
    # segurança: o preview toca payload de evento de cliente.
    #
    # Não pode herdar ``MAPPING_READ`` — VIEWER a possui, e um viewer passaria a
    # ler payload por um endpoint novo. Nem ``QUERY_RUN`` — hoje ela autoriza
    # apenas LER a configuração de regras, e ampliá-la para liberar payload seria
    # alargar em silêncio o alcance de uma permissão já concedida.
    #
    # Concedida a partir de ENGINEER (quem de fato escreve a regra).
    # Invariante travada em test_adr0015_preview_permission.py.
    CORRELATION_PREVIEW = "correlation.preview"


# Matriz papel × permissão (hardcoded — fonte da verdade)
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    UserRole.VIEWER: frozenset({
        Permission.MAPPING_READ,
        Permission.INTEGRATION_READ,
        Permission.QUARANTINE_READ,
        Permission.DRIFT_READ,
        Permission.AUDIT_READ,
    }),
    UserRole.OPERATOR: frozenset({
        Permission.MAPPING_READ,
        Permission.INTEGRATION_READ,
        Permission.INTEGRATION_PAUSE,
        Permission.QUARANTINE_READ,
        Permission.QUARANTINE_DISCARD,
        Permission.DRIFT_READ,
        Permission.DRIFT_IGNORE,
        Permission.AUDIT_READ,
        # Operator pode emitir tokens com internal.tenant.read
        # (caso típico: Service Account).
        Permission.INTERNAL_TENANT_READ,
        # Operator (responder SOC) roda query/hunt ao vivo.
        # Salvar query e block (destrutivo) ficam acima (engineer/admin).
        Permission.QUERY_RUN,
    }),
    UserRole.ENGINEER: frozenset({
        Permission.MAPPING_READ,
        Permission.MAPPING_WRITE,
        Permission.MAPPING_ROLLBACK,
        Permission.INTEGRATION_READ,
        Permission.INTEGRATION_PAUSE,
        Permission.QUARANTINE_READ,
        Permission.QUARANTINE_DISCARD,
        Permission.DRIFT_READ,
        Permission.DRIFT_IGNORE,
        Permission.DRIFT_MARK_MAPPED,
        Permission.DRIFT_DELETE,
        Permission.AUDIT_READ,
        Permission.INTERNAL_TENANT_READ,
        # ADR-0015: testar regra contra amostras reais. Quem escreve a regra.
        Permission.CORRELATION_PREVIEW,
        # Engineer roda E salva query/agendamento. Block
        # (destrutivo) segue só admin por padrão (concedível a operator via policy).
        Permission.QUERY_RUN,
        Permission.QUERY_SAVE,
    }),
    UserRole.ADMIN: frozenset({p for p in Permission}),
}


def get_user_permissions(role: str) -> list[str]:
    """Retorna lista de permissões para um papel dado.

    Roles legados ('user') são mapeados para viewer antes da consulta.
    """
    normalized = "viewer" if role == "user" else role
    return sorted(ROLE_PERMISSIONS.get(normalized, frozenset()))


def effective_scopes(
    role: str,
    token_scopes: list[str] | None,
) -> frozenset[str]:
    """Permissões efetivas de uma sessão autenticada.

    - Sem token (cookie session) ou token sem scopes_json:
      retorna todas as permissões da role.
    - Token com scopes restritos: INTERSEÇÃO entre os scopes
      do token e as permissões da role do owner. Token nunca pode
      escalar privilégio além da role.
    """
    normalized_role = "viewer" if role == "user" else role
    role_perms = ROLE_PERMISSIONS.get(normalized_role, frozenset())
    if not token_scopes:
        return role_perms
    token_set = frozenset(token_scopes)
    return frozenset(role_perms & token_set)


def require_permission(perm: str | Permission) -> Callable[..., models.AppUser]:
    """Factory de dependência FastAPI: lança 403 se o user não tem a permissão.

    Também respeita ``scopes_json`` do PAT quando o request veio
    via Bearer. O check final é INTERSEÇÃO(role.permissions, token.scopes).
    """

    perm_str = str(perm)

    def _check(
        request: Request,
        current_user: models.AppUser = Depends(get_current_user),
    ) -> models.AppUser:
        # Token associado ao request (None pra cookie sessions).
        api_token: models.ApiToken | None = getattr(
            request.state, "authenticated_token", None
        )
        token_scopes: list[str] | None = None
        if api_token is not None:
            # Import local: services.api_tokens importa core.auth — circular.
            from ..services.api_tokens import parse_scopes
            parsed = parse_scopes(api_token.scopes_json)
            # parsed = [] significa "full inherit"; só passamos lista quando
            # há restrição de fato.
            token_scopes = parsed if parsed else None

        allowed = effective_scopes(current_user.role, token_scopes)
        if perm_str not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permissão '{perm_str}' necessária",
            )
        return current_user

    return _check


# ── Repositórios ──────────────────────────────────────────────────────


def get_user_repo(db: Session = Depends(database.get_session)) -> repository.UserRepository:
    return repository.UserRepository(db)


def get_session_repo(db: Session = Depends(database.get_session)) -> repository.UserSessionRepository:
    return repository.UserSessionRepository(db)


def users_exist(db: Session) -> bool:
    return repository.UserRepository(db).count() > 0


def create_user_session(
    user: models.AppUser,
    db: Session,
    *,
    user_agent: str | None = None,
) -> tuple[str, models.UserSession]:
    token = generate_session_token()
    expires_at = datetime.utcnow() + timedelta(hours=settings.SESSION_TTL_HOURS)
    session = repository.UserSessionRepository(db).add(
        models.UserSession(
            user_id=user.id,
            token_hash=hash_session_token(token),
            user_agent=user_agent,
            expires_at=expires_at,
        )
    )
    return token, session


def revoke_session(raw_token: str | None, db: Session) -> None:
    if not raw_token:
        return

    session_repo = repository.UserSessionRepository(db)
    session = session_repo.get_active_by_token_hash(hash_session_token(raw_token))
    if session:
        session_repo.revoke(session)


def set_session_cookie(response: Response, token: str, expires_at: datetime) -> None:
    max_age = max(int((expires_at - datetime.utcnow()).total_seconds()), 0)
    response.set_cookie(
        key=settings.SESSION_COOKIE_NAME,
        value=token,
        max_age=max_age,
        httponly=True,
        secure=settings.SESSION_SECURE_COOKIE,
        samesite=settings.SESSION_SAMESITE,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.SESSION_COOKIE_NAME,
        path="/",
        secure=settings.SESSION_SECURE_COOKIE,
        samesite=settings.SESSION_SAMESITE,
    )


def _populate_authenticated_state(
    request: Request,
    user: models.AppUser,
    *,
    via: str,
    session: models.UserSession | None = None,
    api_token: models.ApiToken | None = None,
) -> None:
    """Popula ``request.state.authenticated_*`` para o resto do request.

    ``via`` é ``"cookie"`` ou ``"token"``. Quando ``via == "token"``,
    ``session`` é ``None`` e ``api_token`` carrega o ``ApiToken`` resolvido.
    """
    request.state.authenticated_user = user
    request.state.authenticated_user_id = user.id
    request.state.authenticated_user_uuid = user.uuid
    request.state.authenticated_username = user.username
    request.state.authenticated_user_role = user.role
    request.state.authenticated_session = session
    request.state.authenticated_via = via
    request.state.authenticated_token = api_token
    request.state.authenticated_token_id = api_token.id if api_token else None


def _resolve_bearer_user(
    request: Request,
    db: Session,
) -> models.AppUser | None:
    """Resolve um header ``Authorization: Bearer copsk_<...>`` em um AppUser.

    Retorna ``None`` se:
      - header ausente,
      - scheme não é Bearer,
      - token não comeca com ``copsk_`` (não é PAT — outras integrações),
      - token inválido / expirado / revogado,
      - user inativo.

    Retornar None deixa o fluxo continuar para tentativa via cookie.
    Em caso de **PAT presente porém inválido**, lança 401 imediatamente —
    é um sinal explícito de credencial errada, não fallback silencioso.
    """
    auth_header = request.headers.get("Authorization") or request.headers.get("authorization")
    if not auth_header or not auth_header.startswith(_BEARER_SCHEME_PREFIX):
        return None

    raw_token = auth_header[len(_BEARER_SCHEME_PREFIX):].strip()
    # Import local: evita ciclos backend.app.core.auth ↔ services.api_tokens.
    from ..services.api_tokens import ApiTokenService, TOKEN_RAW_PREFIX

    if not raw_token.startswith(TOKEN_RAW_PREFIX):
        # Bearer scheme mas não é PAT — pode ser outra autenticação futura.
        # deixamos passar pra cookie tentar.
        return None

    service = ApiTokenService(db)
    api_token = service.resolve_bearer(raw_token)
    if api_token is None:
        # PAT presente mas inválido — não cai pra cookie (evita confundir
        # cliente que pensa estar usando PAT mas tem cookie residual).
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired API token",
            headers={"WWW-Authenticate": 'Bearer realm="centralops"'},
        )

    # Resolver retorna ou (a) PAT pessoal — usa AppUser direto, ou
    # (b) PAT de SA — constrói AppUser shim.
    user: models.AppUser | None
    if api_token.service_account_id is not None:
        sa = api_token.service_account
        if sa is None or not sa.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Service account is inactive",
                headers={"WWW-Authenticate": 'Bearer realm="centralops"'},
            )
        user = _build_sa_appuser_shim(sa)
    else:
        user = api_token.user
        if not user or not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User is inactive",
                headers={"WWW-Authenticate": 'Bearer realm="centralops"'},
            )

    # Rate limit por token (PAT). Falha 429 antes de gravar uso.
    from .rate_limiter import token_rate_limiter
    retry_after = token_rate_limiter.check(api_token.id)
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Token rate limit exceeded",
            headers={"Retry-After": str(retry_after)},
        )

    # Best-effort: gravar last_used_at / use_count.
    try:
        # Import local: get_client_ip vive em services.audit.
        from ..services.audit import get_client_ip
        service.record_usage(api_token, ip_address=get_client_ip(request))
    except Exception as exc:  # pragma: no cover — best-effort
        logger.warning("Falha ao gravar uso de PAT id=%s: %s", api_token.id, exc)

    _populate_authenticated_state(
        request,
        user,
        via="token",
        session=None,
        api_token=api_token,
    )
    return user


def get_current_user(
    request: Request,
    response: Response,
    db: Session = Depends(database.get_session),
) -> models.AppUser:
    """Resolve o user autenticado pelo request.

    Ordem de tentativa:
      1. ``Authorization: Bearer copsk_<...>`` (PAT).
      2. ``Cookie`` da sessão browser (path tradicional).

    Se ambos estiverem presentes, **Bearer ganha** — Bearer é uma intenção
    explícita de cliente non-browser. O cookie é ignorado nesse caso e
    o ``response`` não recebe ``set-cookie`` (Bearer flow é stateless).
    """
    # 1. Bearer first — quando PAT está presente, prevalece.
    bearer_user = _resolve_bearer_user(request, db)
    if bearer_user is not None:
        return bearer_user

    # 2. Cookie fallback (path tradicional do browser).
    raw_token = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if not raw_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    session_repo = repository.UserSessionRepository(db)
    session = session_repo.get_active_by_token_hash(hash_session_token(raw_token))
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired session")

    user = session.user
    if not user or not user.is_active:
        session_repo.revoke(session)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User is inactive")

    previous_expires_at = session.expires_at
    session_repo.touch(session, settings.SESSION_TTL_HOURS)
    if session.expires_at != previous_expires_at:
        set_session_cookie(response, raw_token, session.expires_at)

    _populate_authenticated_state(
        request,
        user,
        via="cookie",
        session=session,
        api_token=None,
    )
    return user


def require_authenticated_user(
    current_user: models.AppUser = Depends(get_current_user),
) -> models.AppUser:
    return current_user


# DEPRECATED. Wrapper de require_permission(Permission.USER_MANAGE).
def require_admin_user(
    request: Request,
    current_user: models.AppUser = Depends(get_current_user),
) -> models.AppUser:
    """Mantém comportamento legado: 403 se não tem USER_MANAGE.

    Respeita scopes do token — se um PAT chegou com scope
    restrito que não inclui ``user.manage``, o request é negado mesmo
    se a role do owner permitiria.
    """
    api_token: models.ApiToken | None = getattr(
        request.state, "authenticated_token", None
    )
    token_scopes: list[str] | None = None
    if api_token is not None:
        from ..services.api_tokens import parse_scopes
        parsed = parse_scopes(api_token.scopes_json)
        token_scopes = parsed if parsed else None

    allowed = effective_scopes(current_user.role, token_scopes)
    if Permission.USER_MANAGE not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return current_user
