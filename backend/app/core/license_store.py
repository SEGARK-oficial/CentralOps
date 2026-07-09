"""DB-backed, encrypted persistence for the activated license token.

The operator activates a license from the UI (``/config`` → Licenciamento); the signed
EdDSA token is stored **encrypted** in the ``license_config`` singleton (id=1) and
becomes the source of truth read by :mod:`app.core.edition` (DB-first, with env/file as
fallback for bootstrap / air-gapped / compose deploys).

The PRIVATE signing key never lives in the product — only the public keyring verifies
the token. Encryption-at-rest uses the pluggable secrets backend (Fernet default,
KMS/Vault-Transit optional) via :mod:`app.core.crypto`.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from .crypto import decrypt, encrypt

logger = logging.getLogger(__name__)

# Singleton row id — the license is per-DEPLOY (not per-org), like IdentityConfig.
_SINGLETON_ID = 1


def load_active_token() -> Optional[str]:
    """Return the decrypted activated token, or ``None`` when none is stored.

    Callers (the edition resolver) treat any exception as "no DB token" and fall back
    to env/file — but we also guard here so a missing table during very early boot never
    propagates."""
    from ..db.database import SessionLocal  # noqa: PLC0415 — lazy: no DB import at module load
    from ..db.models import LicenseConfig  # noqa: PLC0415

    with SessionLocal() as db:
        row = db.get(LicenseConfig, _SINGLETON_ID)
        if row is None or not row.license_token:
            return None
        return decrypt(row.license_token)


def save_token(token: str, *, actor: Optional[str] = None) -> None:
    """Encrypt + persist the token in the singleton (upsert id=1)."""
    from ..db.database import SessionLocal  # noqa: PLC0415
    from ..db.models import LicenseConfig  # noqa: PLC0415

    now = datetime.utcnow()
    with SessionLocal() as db:
        row = db.get(LicenseConfig, _SINGLETON_ID)
        if row is None:
            row = LicenseConfig(id=_SINGLETON_ID)
            db.add(row)
        row.license_token = encrypt(token)
        row.activated_by = actor
        row.activated_at = now
        row.updated_at = now
        db.commit()


def clear_token(*, actor: Optional[str] = None) -> None:
    """Remove the stored token (deactivate). Idempotent — no-op if none stored."""
    from ..db.database import SessionLocal  # noqa: PLC0415
    from ..db.models import LicenseConfig  # noqa: PLC0415

    with SessionLocal() as db:
        row = db.get(LicenseConfig, _SINGLETON_ID)
        if row is None or not row.license_token:
            return
        row.license_token = None
        row.activated_by = actor
        row.activated_at = None
        row.updated_at = datetime.utcnow()
        db.commit()


def activation_info() -> dict:
    """Metadata for the UI: whether a DB token is set + who/when activated it.

    Fail-safe — returns the "none" shape on any error (e.g. table not yet created)."""
    from ..db.database import SessionLocal  # noqa: PLC0415
    from ..db.models import LicenseConfig  # noqa: PLC0415

    try:
        with SessionLocal() as db:
            row = db.get(LicenseConfig, _SINGLETON_ID)
            if row and row.license_token:
                return {
                    "source": "database",
                    "activated_by": row.activated_by,
                    "activated_at": row.activated_at.isoformat() if row.activated_at else None,
                }
    except Exception as exc:  # noqa: BLE001 — never break the status endpoint
        logger.warning("license activation_info lookup failed: %r", exc)
    return {"source": None, "activated_by": None, "activated_at": None}
