"""Backend default: Fernet derivada de ``APP_MASTER_KEY`` via PBKDF2.

Mantém o comportamento histórico de ``backend.app.core.crypto`` (que
agora delega para esta classe). Não usa serviço externo — útil em dev
e em deploys single-host. Para produção multi-tenant, trocar por
backend KMS-wrapped na Fase 4.
"""

from __future__ import annotations

import base64
import logging
from binascii import Error as BinasciiError

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from ..config import settings
from .backend import SecretsBackend

logger = logging.getLogger(__name__)

# Salt fixo preservado do módulo legado para retrocompatibilidade com
# secrets já cifrados em DB. Mudar este salt invalida todo dado cifrado
# existente — fazer apenas em janela de re-encrypt explícita.
_LEGACY_SALT = bytes.fromhex("736f70686f732d7365617263682d6775692d7631")
_ENCRYPTED_PREFIX = "enc::"
_PBKDF2_ITERATIONS = 480_000


def _derive_key(master: str) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_LEGACY_SALT,
        iterations=_PBKDF2_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(master.encode()))


def _looks_like_fernet_token(value: str) -> bool:
    """Detecta tokens Fernet legados (sem o prefixo ``enc::``)."""
    try:
        padded = value.encode() + b"=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(padded)
    except (ValueError, BinasciiError):
        return False
    # Fernet tokens começam com o byte de versão 0x80.
    return bool(decoded) and decoded[0] == 0x80


class LocalFernetBackend(SecretsBackend):
    """Cifra com Fernet (AES-128-CBC + HMAC-SHA256)."""

    def __init__(self, master_key: str | None = None) -> None:
        key = master_key if master_key is not None else settings.APP_MASTER_KEY
        self._fernet = Fernet(_derive_key(key))

    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            return plaintext
        token = self._fernet.encrypt(plaintext.encode()).decode()
        return f"{_ENCRYPTED_PREFIX}{token}"

    def decrypt(self, ciphertext: str) -> str:
        if not ciphertext:
            return ciphertext

        token = ciphertext
        if ciphertext.startswith(_ENCRYPTED_PREFIX):
            token = ciphertext[len(_ENCRYPTED_PREFIX):]
        elif not _looks_like_fernet_token(ciphertext):
            return ciphertext

        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            logger.error(
                "Stored secret could not be decrypted with the configured APP_MASTER_KEY"
            )
            raise ValueError(
                "Stored secret could not be decrypted with the configured APP_MASTER_KEY"
            ) from exc
