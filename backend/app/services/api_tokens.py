"""Personal Access Tokens (Fase 1, expandido na Fase 2).

A criação retorna ``(raw_token, ApiToken)`` — o ``raw_token`` plaintext
**nunca** é persistido. A UI exibe-o uma única vez no momento da criação.

Lookup performante:
  1. Header ``Authorization: Bearer copsk_<...>``
  2. Extrai ``token_prefix`` (12 chars) e procura linha única no DB.
  3. ``argon2.verify(stored_hash, raw)`` — único custo Argon2 por request.
  4. Atualiza ``last_used_at`` / ``last_used_ip`` / ``use_count`` (best-effort).

Diferenças intencionais vs ``ThreatIntelToken``:
  - Argon2id (vs sha256) — PATs têm acesso ao app inteiro, alvo de alto valor.
  - Prefixo bem definido ``copsk_`` — facilita scanners (TruffleHog, gitleaks).
  - Expiração opcional (UI exibe warning quando is_eternal=True).

**Fase 2 — owners XOR + scopes:**
- Token pode pertencer a um ``AppUser`` *ou* a um ``ServiceAccount`` —
  XOR enforced em duas camadas (CheckConstraint na DB + ``create_token``
  aqui no service). DDL XOR sozinho não basta: SQLite < 3.39 valida
  CHECK em INSERT mas não em UPDATE com expressões boolean booleanas
  encadeadas — service-layer validation é o filtro de fato.
- Scopes (subset de ``Permission`` enum) limitam o que o token pode
  fazer dentro do que a role do owner permite. NULL/[] = full inherit
  (legacy Fase 1).
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime
from typing import Iterable

from ..core.datetime_utils import ensure_naive_utc

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError
from sqlalchemy.orm import Session

from ..db import models

logger = logging.getLogger(__name__)


# Argon2 PasswordHasher singleton — reutiliza configurações entre requests.
# Defaults da lib argon2-cffi (~50ms): time_cost=2, memory_cost=64MB,
# parallelism=8 — suficiente para PATs (não é hot-path de password login).
_PASSWORD_HASHER = PasswordHasher()


# Prefixo público de PATs — ajuda secret scanners a identificar um leak
# (TruffleHog, gitleaks, GitHub secret scanning custom patterns).
TOKEN_RAW_PREFIX = "copsk_"

# Tamanho do segmento aleatório (depois do prefixo). 32 bytes urlsafe ~ 43 chars.
_TOKEN_RANDOM_BYTES = 32

# Quantos chars do raw token vão para ``token_prefix`` na DB. Inclui ``copsk_``
# (6 chars) + 6 chars do random — o suficiente pra UI mostrar "copsk_aB3xK7..."
# e para fazer lookup determinístico antes do Argon2.verify.
_TOKEN_PREFIX_LENGTH = 12


# ── Geração / verificação de tokens raw ──────────────────────────────────


def _generate_raw_token() -> str:
    """Gera um raw token no formato ``copsk_<43 chars urlsafe>``."""
    random_part = secrets.token_urlsafe(_TOKEN_RANDOM_BYTES)
    return f"{TOKEN_RAW_PREFIX}{random_part}"


def _extract_prefix(raw_token: str) -> str:
    """Retorna os primeiros ``_TOKEN_PREFIX_LENGTH`` chars do raw token."""
    return raw_token[:_TOKEN_PREFIX_LENGTH]


def _hash_token(raw_token: str) -> str:
    """Aplica Argon2id ao raw token — retorna a string self-described."""
    return _PASSWORD_HASHER.hash(raw_token)


def _verify_token_hash(stored_hash: str, raw_token: str) -> bool:
    """``argon2.verify`` com tratamento explícito dos exceptions."""
    try:
        return _PASSWORD_HASHER.verify(stored_hash, raw_token)
    except (VerifyMismatchError, InvalidHashError):
        return False
    except Exception as exc:  # pragma: no cover — defesa em profundidade
        logger.warning("Erro inesperado ao verificar token: %s", exc)
        return False


# ── Helpers de scope (Fase 2) ────────────────────────────────────────────


def parse_scopes(scopes_json: str | None) -> list[str]:
    """Decodifica ``ApiToken.scopes_json`` em lista. ``None``/inválido → []."""
    if not scopes_json:
        return []
    try:
        value = json.loads(scopes_json)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str)]


def serialize_scopes(scopes: list[str] | None) -> str | None:
    """Encoda lista de scopes em JSON pra ``ApiToken.scopes_json``.

    ``None``/`[]` → ``None`` (semantica "full inherit").
    """
    if not scopes:
        return None
    return json.dumps(sorted(set(scopes)), separators=(",", ":"))


def validate_scopes(scopes: Iterable[str]) -> list[str]:
    """Valida que cada scope é um membro de ``Permission`` enum.

    Retorna a lista deduplicada e ordenada. Lança ``ValueError`` se
    qualquer scope é inválido — caller decide o status code.
    """
    # Import local evita ciclo backend.app.core.auth ↔ services.api_tokens.
    from ..core.auth import Permission

    valid = {p.value for p in Permission}
    requested = sorted(set(str(s) for s in scopes))
    invalid = [s for s in requested if s not in valid]
    if invalid:
        raise ValueError(
            f"invalid scope(s): {sorted(invalid)}. Must be subset of Permission enum."
        )
    return requested


# ── Repositório ──────────────────────────────────────────────────────────


class ApiTokenRepository:
    """CRUD de ``ApiToken``. Mantém-se separado do repositório de users
    para que ``UserRepository`` continue isolado de side-effects de PAT.
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    def add(self, token: models.ApiToken) -> models.ApiToken:
        self.db.add(token)
        self.db.commit()
        self.db.refresh(token)
        return token

    def get(self, token_id: int) -> models.ApiToken | None:
        return self.db.query(models.ApiToken).filter(models.ApiToken.id == token_id).first()

    def get_by_prefix(self, prefix: str) -> models.ApiToken | None:
        """Lookup determinístico para o resolver Bearer (1 linha no Postgres)."""
        return (
            self.db.query(models.ApiToken)
            .filter(models.ApiToken.token_prefix == prefix)
            .first()
        )

    def list_for_user(self, user_id: int, *, include_revoked: bool = False) -> list[models.ApiToken]:
        """Lista PATs **pessoais** (user_id=user_id, service_account_id IS NULL)."""
        query = (
            self.db.query(models.ApiToken)
            .filter(models.ApiToken.user_id == user_id)
            .filter(models.ApiToken.service_account_id.is_(None))
            .order_by(models.ApiToken.created_at.desc())
        )
        if not include_revoked:
            query = query.filter(models.ApiToken.revoked_at.is_(None))
        return query.all()

    def list_for_service_account(
        self, service_account_id: int, *, include_revoked: bool = False
    ) -> list[models.ApiToken]:
        """Lista PATs vinculados a um Service Account específico."""
        query = (
            self.db.query(models.ApiToken)
            .filter(models.ApiToken.service_account_id == service_account_id)
            .order_by(models.ApiToken.created_at.desc())
        )
        if not include_revoked:
            query = query.filter(models.ApiToken.revoked_at.is_(None))
        return query.all()

    def get_by_user_and_name(
        self, user_id: int, name: str, *, include_revoked: bool = True
    ) -> models.ApiToken | None:
        query = (
            self.db.query(models.ApiToken)
            .filter(models.ApiToken.user_id == user_id)
            .filter(models.ApiToken.service_account_id.is_(None))
            .filter(models.ApiToken.name == name)
        )
        if not include_revoked:
            query = query.filter(models.ApiToken.revoked_at.is_(None))
        return query.first()

    def get_by_sa_and_name(
        self, service_account_id: int, name: str, *, include_revoked: bool = True
    ) -> models.ApiToken | None:
        query = (
            self.db.query(models.ApiToken)
            .filter(models.ApiToken.service_account_id == service_account_id)
            .filter(models.ApiToken.name == name)
        )
        if not include_revoked:
            query = query.filter(models.ApiToken.revoked_at.is_(None))
        return query.first()

    def revoke(self, token: models.ApiToken) -> models.ApiToken:
        if token.revoked_at is None:
            token.revoked_at = datetime.utcnow()
            self.db.commit()
            self.db.refresh(token)
        return token

    def revoke_all_for_user(self, user_id: int) -> int:
        """Revoga todos os PATs pessoais ativos de um usuário (offboarding).

        Fecha o gap em que tokens sobreviviam à desativação da conta. Retorna
        a quantidade revogada. UPDATE em massa (idempotente — só toca ativos).
        """
        from sqlalchemy import update as _sa_update

        stmt = (
            _sa_update(models.ApiToken)
            .where(
                models.ApiToken.user_id == user_id,
                models.ApiToken.service_account_id.is_(None),
                models.ApiToken.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.utcnow())
            .execution_options(synchronize_session=False)
        )
        result = self.db.execute(stmt)
        self.db.commit()
        return int(result.rowcount or 0)

    def record_usage(
        self,
        token: models.ApiToken,
        *,
        ip_address: str | None = None,
        when: datetime | None = None,
    ) -> models.ApiToken:
        """Atualiza last_used_* / use_count. Best-effort no resolver Bearer.

        Faz commit isolado para não conflitar com transações do request handler.
        """
        token.last_used_at = when or datetime.utcnow()
        token.last_used_ip = ip_address
        token.use_count = (token.use_count or 0) + 1
        self.db.commit()
        self.db.refresh(token)
        return token


# ── Service de alto nível ────────────────────────────────────────────────


class ApiTokenService:
    """Camada de aplicação: regras de negócio + composição com repos.

    Os métodos de **leitura** podem ser chamados de qualquer caller. Os de
    **mutação** (``create_token``, ``revoke_token``) devem ser chamados
    apenas pelo router após require_authenticated_user.
    """

    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = ApiTokenRepository(db)

    def revoke_all_for_user(self, user_id: int) -> int:
        """Revoga todos os PATs pessoais ativos do usuário (offboarding)."""
        return self.repo.revoke_all_for_user(user_id)

    # -- Criação ---------------------------------------------------------

    def create_token(
        self,
        *,
        user: models.AppUser | None = None,
        service_account: models.ServiceAccount | None = None,
        name: str,
        expires_at: datetime | None,
        is_eternal: bool = False,
        scopes: list[str] | None = None,
    ) -> tuple[str, models.ApiToken]:
        """Gera, persiste e devolve ``(raw_token, ApiToken)``.

        ``raw_token`` plaintext sai daqui uma única vez — o caller deve
        retornar pra UI e descartar imediatamente. Nunca é re-emitido.

        Owner XOR (Fase 2):
          - Exatamente um de ``user`` ou ``service_account`` deve ser
            fornecido. Nem nenhum, nem ambos.

        Validações:
          - name não-vazio + único por owner.
          - expires_at no futuro OU is_eternal=True (não os dois).
          - scopes válidos (subset de Permission).
        """
        # Owner XOR — defesa em profundidade junto com CheckConstraint da DB.
        if (user is None) == (service_account is None):
            raise ValueError(
                "exactly one of `user` or `service_account` must be provided"
            )

        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("name must not be empty")

        # Validação expires_at vs is_eternal.
        # Backwards-compat (Fase 1): se ambos default (expires_at=None,
        # is_eternal=False), tratamos como is_eternal=True automaticamente.
        # O router (Fase 2) é o gate que exige opt-in explícito do operador
        # quando expires_at chega como None.
        if is_eternal and expires_at is not None:
            raise ValueError(
                "is_eternal=True is mutually exclusive with expires_at"
            )
        if expires_at is None and not is_eternal:
            # Compat path: Fase 1 + clientes antigos não conhecem is_eternal.
            is_eternal = True
        # Normaliza para naive-UTC antes de comparar com datetime.utcnow() (naive).
        # Clientes podem enviar ISO com timezone (ex: "2030-01-01T00:00:00Z") e o
        # Pydantic cria datetime aware — comparar aware vs naive levantaria TypeError.
        expires_at = ensure_naive_utc(expires_at)
        if expires_at is not None and expires_at <= datetime.utcnow():
            raise ValueError("expires_at must be in the future")

        # Validação de scopes — falha cedo com erro humano.
        validated_scopes: list[str] = []
        if scopes:
            validated_scopes = validate_scopes(scopes)

        # Unicidade de nome por owner.
        if user is not None:
            existing = self.repo.get_by_user_and_name(
                user.id, normalized_name, include_revoked=False
            )
            if existing:
                raise ValueError("token name already in use for this user")
        else:
            assert service_account is not None  # XOR garantido acima
            existing = self.repo.get_by_sa_and_name(
                service_account.id, normalized_name, include_revoked=False
            )
            if existing:
                raise ValueError(
                    "token name already in use for this service account"
                )

        raw_token = _generate_raw_token()
        token = models.ApiToken(
            user_id=user.id if user is not None else None,
            service_account_id=(
                service_account.id if service_account is not None else None
            ),
            name=normalized_name,
            token_prefix=_extract_prefix(raw_token),
            token_hash=_hash_token(raw_token),
            expires_at=expires_at,
            is_eternal=is_eternal,
            scopes_json=serialize_scopes(validated_scopes),
        )
        self.repo.add(token)
        return raw_token, token

    # -- Listagem / leitura ---------------------------------------------

    def list_for_user(
        self, user_id: int, *, include_revoked: bool = False
    ) -> list[models.ApiToken]:
        return self.repo.list_for_user(user_id, include_revoked=include_revoked)

    def list_for_service_account(
        self, service_account_id: int, *, include_revoked: bool = False
    ) -> list[models.ApiToken]:
        return self.repo.list_for_service_account(
            service_account_id, include_revoked=include_revoked
        )

    # -- Revogação -------------------------------------------------------

    def revoke_token(
        self, *, user: models.AppUser, token_id: int
    ) -> models.ApiToken | None:
        """Revoga um PAT *pessoal* do user. Retorna o token ou None se 404.

        Não permite revogar token de SA por aqui — use revoke_sa_token.
        """
        token = self.repo.get(token_id)
        if not token or token.user_id != user.id or token.service_account_id is not None:
            return None
        return self.repo.revoke(token)

    def revoke_sa_token(
        self, *, service_account_id: int, token_id: int
    ) -> models.ApiToken | None:
        """Revoga um token de SA. Caller já validou perm USER_MANAGE."""
        token = self.repo.get(token_id)
        if not token or token.service_account_id != service_account_id:
            return None
        return self.repo.revoke(token)

    # -- Resolver (path crítico do Bearer) -------------------------------

    def resolve_bearer(self, raw_token: str) -> models.ApiToken | None:
        """Resolve um raw token vindo de ``Authorization: Bearer copsk_<...>``.

        Retorna o ``ApiToken`` válido (ativo, não expirado, não revogado) ou
        ``None`` se inválido. **Não** atualiza ``last_used_at`` aqui — esse
        side effect é responsabilidade do caller via ``record_usage`` para
        que a verificação seja idempotente em testes.

        Fase 2: tokens de SA também precisam que o SA esteja ``is_active``.
        """
        if not raw_token or not raw_token.startswith(TOKEN_RAW_PREFIX):
            return None

        prefix = _extract_prefix(raw_token)
        candidate = self.repo.get_by_prefix(prefix)
        if not candidate:
            return None

        if candidate.revoked_at is not None:
            return None

        if candidate.expires_at is not None and candidate.expires_at <= datetime.utcnow():
            return None

        # Fase 2: token de SA exige SA ativo.
        if candidate.service_account_id is not None:
            sa = candidate.service_account
            if sa is None or not sa.is_active:
                return None

        if not _verify_token_hash(candidate.token_hash, raw_token):
            return None

        return candidate

    def record_usage(
        self,
        token: models.ApiToken,
        *,
        ip_address: str | None = None,
    ) -> None:
        try:
            self.repo.record_usage(token, ip_address=ip_address)
        except Exception as exc:  # pragma: no cover — best-effort
            logger.warning("Falha ao gravar uso de PAT id=%s: %s", token.id, exc)


__all__: Iterable[str] = (
    "ApiTokenRepository",
    "ApiTokenService",
    "TOKEN_RAW_PREFIX",
    "parse_scopes",
    "serialize_scopes",
    "validate_scopes",
)
