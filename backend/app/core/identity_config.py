"""Snapshot de configuração de identidade/SSO (Fase 2).

Fonte de verdade: tabela singleton ``identity_config`` (editada pela UI em
/config → Identidade & SSO). Fallback: variáveis ``ENTRA_*`` do ``.env``
(seed/bootstrap). É lido **por request** — login SSO não é hot path como o
collector, então não há cache Redis; mudanças na UI valem no próximo login
sem restart. O ``client_secret`` chega aqui já **decifrado** (uso interno).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from .config import settings
from .crypto import decrypt

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IdentitySnapshot:
    entra_enabled: bool = False
    entra_tenant_id: Optional[str] = None
    entra_client_id: Optional[str] = None
    entra_client_secret: Optional[str] = None  # decifrado — uso interno
    entra_redirect_uri: Optional[str] = None
    entra_authority: str = "https://login.microsoftonline.com"
    entra_scopes: str = "openid profile email"
    entra_role_map: dict = field(default_factory=dict)
    entra_default_role: str = "viewer"
    entra_default_is_global: bool = False
    entra_jit_provisioning: bool = True
    entra_allowed_email_domains: list = field(default_factory=list)
    entra_button_label: str = "Entrar com Microsoft"
    entra_post_login_redirect: str = "/"
    is_persisted: bool = False  # True quando veio do banco

    # Fase 2B: campos de controle do Graph-sync
    entra_sync_enabled: bool = False
    entra_sync_deprovision: bool = True
    entra_last_sync_at: Optional[datetime] = None
    entra_last_sync_status: Optional[str] = None
    entra_last_sync_summary: Optional[str] = None  # JSON bruto; a task parseia quando necessario


def _domains_from_settings() -> list:
    val = settings.ENTRA_ALLOWED_EMAIL_DOMAINS
    if isinstance(val, str):
        return [d.strip().lower() for d in val.split(",") if d.strip()]
    return list(val or [])


def from_settings() -> IdentitySnapshot:
    """Snapshot a partir do ``.env`` — fallback/seed quando não há linha no DB."""
    return IdentitySnapshot(
        entra_enabled=bool(settings.ENTRA_ENABLED),
        entra_tenant_id=settings.ENTRA_TENANT_ID,
        entra_client_id=settings.ENTRA_CLIENT_ID,
        entra_client_secret=settings.ENTRA_CLIENT_SECRET,
        entra_redirect_uri=settings.ENTRA_REDIRECT_URI,
        entra_authority=settings.ENTRA_AUTHORITY,
        entra_scopes=settings.ENTRA_SCOPES,
        entra_role_map=dict(settings.ENTRA_ROLE_MAP or {}),
        entra_default_role=settings.ENTRA_DEFAULT_ROLE,
        entra_default_is_global=bool(settings.ENTRA_DEFAULT_IS_GLOBAL),
        entra_jit_provisioning=bool(settings.ENTRA_JIT_PROVISIONING),
        entra_allowed_email_domains=_domains_from_settings(),
        entra_button_label=settings.ENTRA_BUTTON_LABEL,
        entra_post_login_redirect=settings.ENTRA_POST_LOGIN_REDIRECT,
        is_persisted=False,
        # Fase 2B: defaults para snapshot construido a partir do .env
        entra_sync_enabled=False,
        entra_sync_deprovision=True,
        entra_last_sync_at=None,
        entra_last_sync_status=None,
        entra_last_sync_summary=None,
    )


def _parse_json(value, fallback):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return fallback


def from_row(row) -> IdentitySnapshot:
    """Snapshot a partir da linha do banco — decifra o secret, parseia JSON."""
    secret = None
    if row.entra_client_secret:
        try:
            secret = decrypt(row.entra_client_secret)
        except Exception as exc:  # pragma: no cover — secret corrompido
            logger.error("identity_config: falha ao decifrar client_secret: %s", exc)
    return IdentitySnapshot(
        entra_enabled=bool(row.entra_enabled),
        entra_tenant_id=row.entra_tenant_id,
        entra_client_id=row.entra_client_id,
        entra_client_secret=secret,
        entra_redirect_uri=row.entra_redirect_uri,
        entra_authority=row.entra_authority or "https://login.microsoftonline.com",
        entra_scopes=row.entra_scopes or "openid profile email",
        entra_role_map=_parse_json(row.entra_role_map, {}),
        entra_default_role=row.entra_default_role or "viewer",
        entra_default_is_global=bool(row.entra_default_is_global),
        entra_jit_provisioning=bool(row.entra_jit_provisioning),
        entra_allowed_email_domains=_parse_json(row.entra_allowed_email_domains, []),
        entra_button_label=row.entra_button_label or "Entrar com Microsoft",
        entra_post_login_redirect=row.entra_post_login_redirect or "/",
        is_persisted=True,
        # Fase 2B: lidos diretamente da linha; sem decifrar nem parsear JSON aqui
        entra_sync_enabled=bool(row.entra_sync_enabled),
        entra_sync_deprovision=bool(row.entra_sync_deprovision),
        entra_last_sync_at=row.entra_last_sync_at,
        entra_last_sync_status=row.entra_last_sync_status,
        entra_last_sync_summary=row.entra_last_sync_summary,
    )


def load(db: Session) -> IdentitySnapshot:
    """Carrega o snapshot ativo: banco (se houver linha) senão ``.env``."""
    from ..db import repository

    row = repository.IdentityConfigRepository(db).get()
    return from_settings() if row is None else from_row(row)
