"""Pool de chaves de API com rotação least-recently-used e estado persistido."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from ...core.crypto import decrypt
from ...db import database, models

logger = logging.getLogger(__name__)


PROVIDER_ABUSEIPDB = "abuseipdb"
PROVIDER_OTX = "otx"
SUPPORTED_PROVIDERS = {PROVIDER_ABUSEIPDB, PROVIDER_OTX}


class NoApiKeyAvailableError(RuntimeError):
    """Lançada quando não existe chave ativa/disponível para o provedor."""


class ApiKeyManager:
    """Gerencia o pool de chaves persistido em ``ThreatIntelApiKey``.

    Estratégia de rotação: ``last_used_at NULLS FIRST`` ⇒ chave menos
    recentemente usada vai primeiro (round-robin natural). Concorrência
    leve: corridas resultam, no pior caso, em duas requisições usando a
    mesma chave — aceitável para o caso de uso.
    """

    def get_next_key(self, db: Session, provider: str) -> tuple[models.ThreatIntelApiKey, str]:
        if provider not in SUPPORTED_PROVIDERS:
            raise ValueError(f"Provedor desconhecido: {provider}")

        now = datetime.utcnow()
        key = (
            db.query(models.ThreatIntelApiKey)
            .filter(models.ThreatIntelApiKey.provider == provider)
            .filter(models.ThreatIntelApiKey.is_active.is_(True))
            .filter(
                (models.ThreatIntelApiKey.exhausted_until.is_(None))
                | (models.ThreatIntelApiKey.exhausted_until < now)
            )
            .order_by(
                models.ThreatIntelApiKey.last_used_at.is_(None).desc(),
                models.ThreatIntelApiKey.last_used_at.asc(),
            )
            .first()
        )
        if not key:
            raise NoApiKeyAvailableError(
                f"Nenhuma chave ativa disponível para o provedor '{provider}'."
            )

        key.last_used_at = now
        db.add(key)
        db.commit()
        db.refresh(key)

        try:
            plaintext = decrypt(key.api_key)
        except Exception as exc:
            logger.error("Falha descriptografando chave %s (id=%s): %s", provider, key.id, exc)
            raise NoApiKeyAvailableError(
                f"Chave armazenada de '{provider}' está corrompida; reconfigure no admin."
            ) from exc

        return key, plaintext

    def mark_exhausted(
        self,
        db: Session,
        key_id: int,
        *,
        cooldown: timedelta = timedelta(hours=24),
        reason: Optional[str] = None,
    ) -> None:
        key = db.get(models.ThreatIntelApiKey, key_id)
        if not key:
            return
        key.exhausted_until = datetime.utcnow() + cooldown
        key.exhausted_count = (key.exhausted_count or 0) + 1
        if reason:
            key.last_error = reason
        db.add(key)
        db.commit()

    def record_success(self, db: Session, key_id: int) -> None:
        key = db.get(models.ThreatIntelApiKey, key_id)
        if not key:
            return
        key.requests_count = (key.requests_count or 0) + 1
        key.last_error = None
        db.add(key)
        db.commit()

    def record_error(self, db: Session, key_id: int, message: str) -> None:
        key = db.get(models.ThreatIntelApiKey, key_id)
        if not key:
            return
        key.last_error = message[:500]
        db.add(key)
        db.commit()

    def keys_available(self, db: Session, provider: str) -> int:
        now = datetime.utcnow()
        return (
            db.query(models.ThreatIntelApiKey)
            .filter(models.ThreatIntelApiKey.provider == provider)
            .filter(models.ThreatIntelApiKey.is_active.is_(True))
            .filter(
                (models.ThreatIntelApiKey.exhausted_until.is_(None))
                | (models.ThreatIntelApiKey.exhausted_until < now)
            )
            .count()
        )


key_manager = ApiKeyManager()


def with_session():
    """Helper para uso em contextos sem dependency injection (background tasks)."""
    return database.SessionLocal()


__all__ = [
    "ApiKeyManager",
    "key_manager",
    "with_session",
    "PROVIDER_ABUSEIPDB",
    "PROVIDER_OTX",
    "NoApiKeyAvailableError",
]
