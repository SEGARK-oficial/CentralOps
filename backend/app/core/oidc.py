"""OIDC (Authorization Code + PKCE) client for Microsoft Entra ID login.

Backend-driven *confidential client*: o ``client_secret`` nunca sai do
servidor e a sessão criada após o callback é a mesma sessão cookie do login
local (ver ``core.auth.create_user_session``). Single-tenant: id_tokens só são
aceitos quando ``tid`` == tenant configurado.

Fase 2: todas as funções operam sobre um ``IdentitySnapshot`` (``cfg``) vindo
de ``core.identity_config.load(db)`` — config no banco (UI) com fallback no
``.env``. Usa PyJWT + httpx (sem MSAL) para manter a validação explícita e
testável: assinatura via JWKS (RS256), ``iss``/``aud``/``exp``/``nbf``,
``nonce`` e ``tid``.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlencode

import httpx
import jwt
from jwt import PyJWKClient

from .identity_config import IdentitySnapshot

logger = logging.getLogger(__name__)

# Ranking de papéis para resolver múltiplos App Roles → o de maior nível.
_ROLE_RANK = {"viewer": 0, "operator": 1, "engineer": 2, "admin": 3}
_VALID_ROLES = frozenset(_ROLE_RANK)


class OidcError(RuntimeError):
    """Falha de validação/troca no fluxo OIDC (callback retorna erro ao login)."""


class OidcConfigurationError(OidcError):
    """SSO Entra desabilitado ou mal configurado."""


# ── Estado de configuração ────────────────────────────────────────────


def is_enabled(cfg: IdentitySnapshot) -> bool:
    """True somente quando o SSO está ligado E todos os campos obrigatórios
    estão presentes — usado pelo /status e como guard das rotas."""
    return bool(
        cfg.entra_enabled
        and cfg.entra_tenant_id
        and cfg.entra_client_id
        and cfg.entra_client_secret
        and cfg.entra_redirect_uri
    )


def ensure_enabled(cfg: IdentitySnapshot) -> None:
    if not is_enabled(cfg):
        raise OidcConfigurationError("Microsoft Entra SSO não está configurado")


def _authority(cfg: IdentitySnapshot) -> str:
    return f"{cfg.entra_authority.rstrip('/')}/{cfg.entra_tenant_id}"


def _expected_issuers(cfg: IdentitySnapshot) -> set[str]:
    """Emissores aceitos para o tenant (formas v2.0 e legada sts.windows.net)."""
    tid = cfg.entra_tenant_id
    base = cfg.entra_authority.rstrip("/")
    return {
        f"{base}/{tid}/v2.0",
        f"https://login.microsoftonline.com/{tid}/v2.0",
        f"https://sts.windows.net/{tid}/",
    }


# ── PKCE / state / nonce ──────────────────────────────────────────────


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def generate_pkce_pair() -> tuple[str, str]:
    """Retorna (code_verifier, code_challenge) — challenge = S256(verifier)."""
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def generate_state() -> str:
    return secrets.token_urlsafe(32)


def generate_nonce() -> str:
    return secrets.token_urlsafe(32)


# ── Discovery (cache em processo, por authority/tenant) ───────────────

_metadata_cache: dict[str, dict[str, Any]] = {}
_jwks_client_cache: dict[str, PyJWKClient] = {}


def discover(cfg: IdentitySnapshot) -> dict[str, Any]:
    """Busca (e cacheia) o OpenID configuration document do tenant."""
    url = f"{_authority(cfg)}/v2.0/.well-known/openid-configuration"
    cached = _metadata_cache.get(url)
    if cached is not None:
        return cached
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url)
            resp.raise_for_status()
            meta = resp.json()
    except httpx.HTTPError as exc:
        raise OidcError(f"OIDC discovery falhou: {exc}") from exc
    _metadata_cache[url] = meta
    return meta


def _jwks_client(cfg: IdentitySnapshot) -> PyJWKClient:
    jwks_uri = discover(cfg)["jwks_uri"]
    client = _jwks_client_cache.get(jwks_uri)
    if client is None:
        client = PyJWKClient(jwks_uri)
        _jwks_client_cache[jwks_uri] = client
    return client


def reset_caches() -> None:
    """Limpa caches de discovery/JWKS (usado por testes e troca de config)."""
    _metadata_cache.clear()
    _jwks_client_cache.clear()


# ── Fluxo ─────────────────────────────────────────────────────────────


def build_authorization_url(
    cfg: IdentitySnapshot, *, state: str, nonce: str, code_challenge: str
) -> str:
    params = {
        "client_id": cfg.entra_client_id,
        "response_type": "code",
        "redirect_uri": cfg.entra_redirect_uri,
        "response_mode": "query",
        "scope": cfg.entra_scopes,
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{discover(cfg)['authorization_endpoint']}?{urlencode(params)}"


def exchange_code(cfg: IdentitySnapshot, *, code: str, code_verifier: str) -> dict[str, Any]:
    data = {
        "client_id": cfg.entra_client_id,
        "client_secret": cfg.entra_client_secret,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": cfg.entra_redirect_uri,
        "code_verifier": code_verifier,
        "scope": cfg.entra_scopes,
    }
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(discover(cfg)["token_endpoint"], data=data)
    except httpx.HTTPError as exc:
        raise OidcError(f"troca de code falhou: {exc}") from exc
    if resp.status_code != 200:
        raise OidcError(f"token endpoint retornou {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def validate_id_token(cfg: IdentitySnapshot, id_token: str, *, nonce: str) -> dict[str, Any]:
    """Valida assinatura (JWKS/RS256), aud, exp/nbf, iss, tid e nonce."""
    if not id_token:
        raise OidcError("resposta do provedor sem id_token")
    try:
        signing_key = _jwks_client(cfg).get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=cfg.entra_client_id,
            options={
                "require": ["exp", "iat", "aud", "iss", "sub"],
                # iss validado manualmente (Entra tem múltiplas formas).
                "verify_iss": False,
            },
        )
    except jwt.PyJWTError as exc:
        raise OidcError(f"validação do id_token falhou: {exc}") from exc

    if claims.get("iss") not in _expected_issuers(cfg):
        raise OidcError(f"issuer inesperado: {claims.get('iss')!r}")
    tid = claims.get("tid")
    if tid and tid != cfg.entra_tenant_id:
        raise OidcError("tenant (tid) do token diverge do configurado")
    if claims.get("nonce") != nonce:
        raise OidcError("nonce não confere (possível replay)")
    return claims


# ── Mapeamento de identidade ──────────────────────────────────────────


@dataclass(frozen=True)
class OidcIdentity:
    subject: str               # ``oid`` (estável no tenant) ou fallback ``sub``
    email: Optional[str]
    display_name: Optional[str]
    role: str
    is_global: bool


def map_role(cfg: IdentitySnapshot, roles_claim: Any) -> str:
    """App Roles (claim ``roles``) → papel local de maior privilégio."""
    if not isinstance(roles_claim, (list, tuple)):
        roles_claim = []
    mapped = [
        cfg.entra_role_map[r]
        for r in roles_claim
        if r in cfg.entra_role_map
    ]
    mapped = [m for m in mapped if m in _VALID_ROLES]
    if mapped:
        return max(mapped, key=lambda m: _ROLE_RANK[m])
    default = cfg.entra_default_role
    return default if default in _VALID_ROLES else "viewer"


def map_identity(cfg: IdentitySnapshot, claims: dict[str, Any]) -> OidcIdentity:
    subject = claims.get("oid") or claims.get("sub")
    if not subject:
        raise OidcError("token sem 'oid'/'sub' — não é possível identificar o usuário")
    raw_email = (
        claims.get("email")
        or claims.get("preferred_username")
        or claims.get("upn")
        or ""
    )
    email = raw_email.strip().lower() or None
    role = map_role(cfg, claims.get("roles"))
    is_global = bool(cfg.entra_default_is_global) or role == "admin"
    return OidcIdentity(
        subject=str(subject),
        email=email,
        display_name=claims.get("name"),
        role=role,
        is_global=is_global,
    )


def email_domain_allowed(cfg: IdentitySnapshot, email: Optional[str]) -> bool:
    """Respeita a allowlist de domínios (vazia = sem restrição)."""
    domains = cfg.entra_allowed_email_domains
    if isinstance(domains, str):
        domains = [d.strip().lower() for d in domains.split(",") if d.strip()]
    if not domains:
        return True
    if not email or "@" not in email:
        return False
    return email.rsplit("@", 1)[-1] in domains
