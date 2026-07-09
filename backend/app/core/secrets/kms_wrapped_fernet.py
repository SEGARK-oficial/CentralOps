"""SecretsBackend com envelope encryption via KMS.

Cada chamada a :meth:`encrypt` gera uma DEK Fernet nova, usa-a para
cifrar o plaintext, e então pede ao KMS para cifrar (wrap) a DEK.
O resultado armazenado é:

    ``kmsenc::{wrapped_dek_b64}:{ciphertext_b64}``

Cada chamada a :meth:`decrypt` faz o caminho inverso: extrai wrapped DEK,
pede ao KMS para decifrar (unwrap), usa a DEK para decifrar o ciphertext.

Cache de DEK
~~~~~~~~~~~~
Para reduzir round-trips ao KMS em operações batch (ex: listar integrações),
o backend mantém um cache em memória de DEKs desempacotadas com TTL curto
(default 60s). A chave do cache é o sha256 do wrapped DEK — identificador
estável e sem exposição da DEK plaintext no cache key.

Fallback de migração
~~~~~~~~~~~~~~~~~~~~
Durante a transição de ``LocalFernetBackend`` (prefixo ``enc::``) para este
backend (prefixo ``kmsenc::``), vai existir uma janela onde secrets antigos
ainda estão no formato legado. O parâmetro ``legacy_fallback`` aceita uma
instância de :class:`SecretsBackend` que será delegada quando o ciphertext
começar com ``enc::``. Sem ele, tentar decifrar tokens legados lança
``ValueError``.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import time
from typing import Optional

from cryptography.fernet import Fernet

from .backend import SecretsBackend
from .kms import KmsBackend

logger = logging.getLogger(__name__)

# Prefixo que identifica tokens produzidos por este backend.
_KMS_PREFIX = "kmsenc::"
# Prefixo do backend legado LocalFernetBackend.
_LEGACY_FERNET_PREFIX = "enc::"


class KmsWrappedFernetBackend(SecretsBackend):
    """SecretsBackend com envelope encryption (KMS + Fernet DEK).

    Args:
        kms: Backend KMS responsável por wrap/unwrap das DEKs.
        legacy_fallback: Backend opcional para decifrar tokens com prefixo
            ``enc::`` (LocalFernet legado). Necessário durante a migração.
        dek_cache_ttl_seconds: TTL em segundos do cache in-memory de DEKs
            desempacotadas. Default 60s. Use 0 para desabilitar o cache.
    """

    PREFIX = _KMS_PREFIX

    def __init__(
        self,
        kms: KmsBackend,
        legacy_fallback: Optional[SecretsBackend] = None,
        dek_cache_ttl_seconds: int = 60,
    ) -> None:
        self.kms = kms
        self.legacy_fallback = legacy_fallback
        self._cache_ttl = dek_cache_ttl_seconds
        # Estrutura: {wrapped_dek_hash_hex: (dek_bytes, expires_at_timestamp)}
        self._dek_cache: dict[str, tuple[bytes, float]] = {}

    # ── API pública ───────────────────────────────────────────────────

    def encrypt(self, plaintext: str) -> str:
        """Cifra o plaintext com uma DEK nova, empacotada pelo KMS.

        Args:
            plaintext: String a cifrar. Strings vazias/None são retornadas
                sem modificação (compatibilidade com campos opcionais no DB).

        Returns:
            Token no formato ``kmsenc::{wrapped_dek_b64}:{ciphertext_b64}``.
        """
        if not plaintext:
            return plaintext

        # 1. Gera DEK nova a cada encrypt — nunca reutiliza DEK para plaintexts
        #    diferentes. Isso garante que comprometer um ciphertext não compromete
        #    outros cifrados com a mesma DEK.
        dek = Fernet.generate_key()

        # 2. Cifra o plaintext com a DEK.
        ciphertext_bytes = Fernet(dek).encrypt(plaintext.encode())

        # 3. Pede ao KMS para empacotar (wrap) a DEK.
        wrapped_dek = self.kms.wrap(dek)

        # 4. Serializa: prefixo + wrapped_dek em base64 + ":" + ciphertext em base64.
        wrapped_dek_b64 = base64.urlsafe_b64encode(wrapped_dek).decode()
        ciphertext_b64 = base64.urlsafe_b64encode(ciphertext_bytes).decode()

        token = f"{_KMS_PREFIX}{wrapped_dek_b64}:{ciphertext_b64}"
        logger.debug(
            "KmsWrappedFernetBackend.encrypt: cifrado com KMS key_id=%s",
            self.kms.key_id(),
        )
        return token

    def decrypt(self, ciphertext: str) -> str:
        """Decifra um token produzido por :meth:`encrypt` ou por backend legado.

        Detecta o prefixo para roteamento:
        - ``kmsenc::...`` → fluxo normal (unwrap DEK + Fernet decrypt).
        - ``enc::...`` → delega ao ``legacy_fallback`` se configurado;
          caso contrário, lança ``ValueError``.
        - Sem prefixo conhecido → retorna como veio (plaintext legado pré-cifragem).

        Args:
            ciphertext: Token a decifrar.

        Returns:
            Plaintext correspondente.

        Raises:
            ValueError: Token com prefixo ``kmsenc::`` mas formato inválido, ou
                token ``enc::`` sem ``legacy_fallback`` configurado.
        """
        if not ciphertext:
            return ciphertext

        # Rota 1: formato atual deste backend.
        if ciphertext.startswith(_KMS_PREFIX):
            return self._decrypt_kms_token(ciphertext)

        # Rota 2: formato legado LocalFernetBackend.
        if ciphertext.startswith(_LEGACY_FERNET_PREFIX):
            if self.legacy_fallback is None:
                raise ValueError(
                    "Token com prefixo 'enc::' (LocalFernet legado) encontrado, "
                    "mas nenhum legacy_fallback configurado em KmsWrappedFernetBackend. "
                    "Forneça legacy_fallback=LocalFernetBackend() para suporte à migração."
                )
            logger.debug("KmsWrappedFernetBackend.decrypt: delegando token 'enc::' ao legacy_fallback")
            return self.legacy_fallback.decrypt(ciphertext)

        # Rota 3: plaintext legado pré-cifragem (sem prefixo). Passthrough.
        logger.debug("KmsWrappedFernetBackend.decrypt: passthrough de valor sem prefixo")
        return ciphertext

    # ── Helpers privados ──────────────────────────────────────────────

    def _decrypt_kms_token(self, token: str) -> str:
        """Decifra um token no formato ``kmsenc::{wrapped_dek_b64}:{ciphertext_b64}``."""
        body = token[len(_KMS_PREFIX):]
        parts = body.split(":", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError(
                f"Token 'kmsenc::' com formato inválido. "
                "Esperado: 'kmsenc::{{wrapped_dek_b64}}:{{ciphertext_b64}}'. "
                f"Recebido prefixo do corpo: '{body[:40]}...'"
            )

        wrapped_dek_b64, ciphertext_b64 = parts[0], parts[1]

        # Decodifica as partes base64.
        try:
            wrapped_dek = base64.urlsafe_b64decode(wrapped_dek_b64)
            ciphertext_bytes = base64.urlsafe_b64decode(ciphertext_b64)
        except Exception as exc:
            raise ValueError(
                f"Token 'kmsenc::' com base64 inválido: {exc}"
            ) from exc

        # Unwrap DEK — com cache para reduzir round-trips KMS em batch.
        dek = self._unwrap_with_cache(wrapped_dek)

        # Decifra o ciphertext com a DEK.
        try:
            return Fernet(dek).decrypt(ciphertext_bytes).decode()
        except Exception as exc:
            raise ValueError(
                "Falha ao decifrar ciphertext com DEK desempacotada. "
                "O token pode estar corrompido."
            ) from exc

    def _unwrap_with_cache(self, wrapped_dek: bytes) -> bytes:
        """Desempacota a DEK, usando cache in-memory se disponível."""
        cache_key = hashlib.sha256(wrapped_dek).hexdigest()

        # Verifica cache — descarta entradas expiradas.
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        # Cache miss: round-trip ao KMS.
        dek = self.kms.unwrap(wrapped_dek)
        self._cache_put(cache_key, dek)
        return dek

    def _cache_get(self, wrapped_dek_hash: str) -> bytes | None:
        """Retorna a DEK do cache se ainda válida, ou None se expirada/ausente."""
        if self._cache_ttl <= 0:
            return None
        entry = self._dek_cache.get(wrapped_dek_hash)
        if entry is None:
            return None
        dek, expires_at = entry
        if time.monotonic() > expires_at:
            # Remove entrada expirada para não acumular lixo.
            del self._dek_cache[wrapped_dek_hash]
            return None
        return dek

    def _cache_put(self, wrapped_dek_hash: str, dek: bytes) -> None:
        """Armazena a DEK no cache com TTL."""
        if self._cache_ttl <= 0:
            return
        expires_at = time.monotonic() + self._cache_ttl
        self._dek_cache[wrapped_dek_hash] = (dek, expires_at)
