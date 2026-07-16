"""License activation API (open-core) — admin-operated, mirrors /config.

Lets an admin ACTIVATE an Enterprise license from the UI: the signed EdDSA token is
validated offline against the bundled public keyring, then persisted **encrypted** in
the DB (``license_config`` singleton) so the running deploy reads it DB-first (no env
editing, survives restarts, audit trail). Fail-closed: an invalid/expired token is
rejected (400) and never stored; the edition stays Community.

Routes (registered under ``/api`` with the authenticated dependency):
  GET    /api/licenses/status    — current edition + activation metadata (any user)
  POST   /api/licenses/activate  — validate + persist + refresh (admin)
  DELETE /api/licenses           — deactivate + refresh (admin)
"""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from ..core import auth as app_auth
from ..core import edition as edition_core
from ..core import tenant
from ..core import license_store
from ..core.errors import ApiError
from ..core.licensing import LicenseError, verify_license
from ..db import models

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/licenses", tags=["licenses"])


class LicenseStatus(BaseModel):
    edition: str
    features: List[str]
    plan: Optional[str] = None
    seats: Optional[int] = None
    max_organizations: Optional[int] = None
    expires_at: Optional[str] = None          # ISO-8601, or null
    # Where the active token came from: 'database' (UI-activated), 'environment'
    # (env/file fallback), or 'none'. Lets the UI explain the source of truth.
    source: str = "none"
    activated_by: Optional[str] = None
    activated_at: Optional[str] = None
    # True = venceu mas está na janela de carência (renove); depois → Community.
    expired_in_grace: bool = False


class ActivateLicenseRequest(BaseModel):
    token: str = Field(min_length=16, description="The signed EdDSA license token (JWT).")


def _actor(user: models.AppUser) -> Optional[str]:
    return (
        getattr(user, "email", None)
        or getattr(user, "username", None)
        or (str(user.id) if getattr(user, "id", None) is not None else None)
    )


def _status() -> LicenseStatus:
    fs = edition_core.current()
    info = license_store.activation_info()
    source = info["source"]
    if source is None:
        # No DB token. If we still resolved Enterprise, it came from env/file.
        source = "environment" if fs.is_enterprise else "none"
    return LicenseStatus(
        edition=fs.edition,
        features=sorted(fs.features),
        plan=fs.plan,
        seats=fs.seats,
        max_organizations=fs.max_organizations,
        expires_at=fs.expires_at.isoformat() if fs.expires_at else None,
        source=source,
        activated_by=info["activated_by"],
        activated_at=info["activated_at"],
        expired_in_grace=fs.expired_in_grace,
    )


@router.get("/status", response_model=LicenseStatus)
def get_status(
    _: models.AppUser = Depends(app_auth.require_authenticated_user),
) -> LicenseStatus:
    return _status()


@router.post("/activate", response_model=LicenseStatus)
def activate_license(
    payload: ActivateLicenseRequest,
    user: models.AppUser = Depends(app_auth.require_admin_user),
) -> LicenseStatus:
    # Licença é da PLATAFORMA (flip de edição afeta todos os tenants) — só admin
    # global.
    tenant.require_global_scope(user)
    token = payload.token.strip()
    actor = _actor(user)
    # Validate offline BEFORE persisting — fail-closed: never store an invalid token.
    keyring = edition_core.load_keyring()
    try:
        verify_license(token, keyring)
    except LicenseError as exc:
        # Diagnóstico server-side da rejeição (antes só o 400 chegava ao operador).
        # NUNCA logar o token (nem prefixo) — só metadados: erro, contagem e dir.
        logger.warning(
            "license activation rejected for %s: %s (keyring: %d key(s) from %s)",
            actor, exc, len(keyring), edition_core._keys_dir(),
        )
        raise ApiError(
            "license.invalid_or_expired",
            status.HTTP_400_BAD_REQUEST,
            messages={
                "pt": "Licença inválida ou expirada: {error}",
                "en": "Invalid or expired license: {error}",
                "es": "Licencia inválida o expirada: {error}",
            },
            params={"error": str(exc)},
        )
    except Exception as exc:  # noqa: BLE001 — bad keyring/library error → 400, never 500
        logger.error("unexpected error verifying license on activate: %r", exc)
        raise ApiError(
            "license.verification_failed",
            status.HTTP_400_BAD_REQUEST,
            messages={
                "pt": "Não foi possível verificar a licença.",
                "en": "Could not verify the license.",
                "es": "No fue posible verificar la licencia.",
            },
        )
    license_store.save_token(token, actor=actor)
    fs = edition_core.refresh()  # re-resolve from the new DB token, update the cache
    logger.info("license activated by %s → edition=%s plan=%s", actor, fs.edition, fs.plan)
    return _status()


@router.delete("", response_model=LicenseStatus)
def deactivate_license(
    user: models.AppUser = Depends(app_auth.require_admin_user),
) -> LicenseStatus:
    # Desativar a licença rebaixa a PLATAFORMA inteira p/ Community — só global.
    tenant.require_global_scope(user)
    actor = _actor(user)
    license_store.clear_token(actor=actor)
    edition_core.refresh()
    logger.info("license deactivated by %s", actor)
    return _status()
