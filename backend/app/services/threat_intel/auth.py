"""Autenticação via bearer token para o endpoint público do Threat Intel."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime
from typing import Optional

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from ...db import database, models


TOKEN_BYTES = 36  # ~48 chars base64
PREFIX_LEN = 8


def hash_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.strip().encode("utf-8")).hexdigest()


def generate_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_prefix(plaintext: str) -> str:
    cleaned = plaintext.strip()
    return cleaned[:PREFIX_LEN]


def _extract_bearer(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    parts = value.strip().split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    if len(parts) == 1:
        return parts[0].strip()
    return None


def validate_bearer_token(
    authorization: Optional[str] = Header(default=None),
    db: Session = Depends(database.get_session),
) -> models.ThreatIntelToken:
    raw = _extract_bearer(authorization)
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token ausente",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token_record = (
        db.query(models.ThreatIntelToken)
        .filter(models.ThreatIntelToken.token_hash == hash_token(raw))
        .first()
    )
    if not token_record or not token_record.is_active or token_record.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido ou revogado",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token_record.last_used_at = datetime.utcnow()
    token_record.requests_count = (token_record.requests_count or 0) + 1
    db.add(token_record)
    db.commit()
    db.refresh(token_record)
    return token_record


__all__ = [
    "validate_bearer_token",
    "generate_token",
    "hash_token",
    "token_prefix",
]
