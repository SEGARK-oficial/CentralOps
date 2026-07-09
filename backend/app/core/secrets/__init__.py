"""Abstração de backend de secrets (RF1.3 / RNF4.1).

A implementação padrão continua sendo :class:`LocalFernetBackend` (Fernet
derivada de ``APP_MASTER_KEY`` via PBKDF2) para compatibilidade zero-churn
com o código existente. A Fase 4 plugou a opção :class:`KmsWrappedFernetBackend`
(envelope encryption) que pode ser ativada via variável de ambiente
``SECRETS_BACKEND=kms_wrapped_fernet``.

Fluxo de seleção de backend
~~~~~~~~~~~~~~~~~~~~~~~~~~~
Na inicialização do processo, :func:`_create_default_backend` lê
``settings.SECRETS_BACKEND`` e instancia o backend adequado. Para dev/test
com ``kms_wrapped_fernet``, usa :class:`LocalKmsStubBackend` como KMS.

O módulo ``backend.app.core.crypto`` re-exporta ``encrypt``/``decrypt``
apontando para o backend default — call-sites existentes continuam
funcionando sem alteração.
"""

from __future__ import annotations

import logging

from ..config import settings
from .backend import SecretsBackend
from .local_fernet import LocalFernetBackend

logger = logging.getLogger(__name__)


def _create_default_backend() -> SecretsBackend:
    """Instancia o backend de secrets conforme ``settings.SECRETS_BACKEND``.

    Valores aceitos:
    - ``"local_fernet"`` (default) — :class:`LocalFernetBackend`.
    - ``"kms_wrapped_fernet"`` — :class:`KmsWrappedFernetBackend` com
      :class:`LocalKmsStubBackend` como KMS stub e ``LocalFernetBackend``
      como fallback para tokens legados ``enc::``.

    Raises:
        ValueError: se ``SECRETS_BACKEND`` tiver valor desconhecido.
    """
    backend_name = settings.SECRETS_BACKEND.strip().lower()

    if backend_name == "local_fernet":
        logger.debug("SecretsBackend: usando LocalFernetBackend")
        return LocalFernetBackend()

    if backend_name == "kms_wrapped_fernet":
        # Importação local para não poluir o namespace quando o backend KMS
        # não for usado (caso default).
        from .kms_wrapped_fernet import KmsWrappedFernetBackend

        kms = _build_kms()
        # legacy_fallback: necessário durante a migração para decifrar tokens
        # antigos no formato "enc::" enquanto o re-encrypt não termina.
        legacy = LocalFernetBackend()
        logger.info(
            "SecretsBackend: usando KmsWrappedFernetBackend (provider=%s, "
            "KMS key_id=%s, cache_ttl=%ds, legacy_fallback=LocalFernetBackend)",
            settings.KMS_PROVIDER,
            kms.key_id(),
            settings.KMS_DEK_CACHE_TTL_SECONDS,
        )
        return KmsWrappedFernetBackend(
            kms=kms,
            legacy_fallback=legacy,
            dek_cache_ttl_seconds=settings.KMS_DEK_CACHE_TTL_SECONDS,
        )

    raise ValueError(
        f"SECRETS_BACKEND desconhecido: '{settings.SECRETS_BACKEND}'. "
        "Valores válidos: 'local_fernet', 'kms_wrapped_fernet'."
    )


def _build_kms():
    """Instancia o :class:`~.kms.KmsBackend` conforme ``settings.KMS_PROVIDER``.

    Separa o PROVEDOR de KMS do MECANISMO de envelope encryption — novos
    provedores (AWS KMS, etc.) plugam aqui sem novo ``SECRETS_BACKEND``.

    Valores aceitos:
    - ``"local_stub"`` (default) — :class:`LocalKmsStubBackend` (dev/test).
    - ``"vault_transit"`` — :class:`VaultTransitBackend` (Vault Transit, CE-ok).

    Raises:
        ValueError: provider desconhecido.
    """
    provider = settings.KMS_PROVIDER.strip().lower()

    if provider == "local_stub":
        from .local_kms_stub import LocalKmsStubBackend

        return LocalKmsStubBackend(
            master_key_path=settings.KMS_LOCAL_STUB_MASTER_KEY_PATH
        )

    if provider == "vault_transit":
        from .vault_transit import VaultTransitBackend

        return VaultTransitBackend(
            addr=settings.VAULT_ADDR,
            key_name=settings.VAULT_TRANSIT_KEY_NAME,
            mount_point=settings.VAULT_TRANSIT_MOUNT,
            auth_method=settings.VAULT_AUTH_METHOD,
            token=settings.VAULT_TOKEN,
            role_id=settings.VAULT_ROLE_ID,
            secret_id=settings.VAULT_SECRET_ID,
            approle_mount=settings.VAULT_APPROLE_MOUNT,
            namespace=settings.VAULT_NAMESPACE,
            verify_tls=settings.VAULT_VERIFY_TLS,
            timeout_seconds=settings.VAULT_TIMEOUT_SECONDS,
        )

    raise ValueError(
        f"KMS_PROVIDER desconhecido: '{settings.KMS_PROVIDER}'. "
        "Valores válidos: 'local_stub', 'vault_transit'."
    )


# Backend default — instanciado uma vez por processo.
_default_backend: SecretsBackend = _create_default_backend()


def get_default_backend() -> SecretsBackend:
    """Retorna o backend de secrets ativo no processo."""
    return _default_backend


def set_default_backend(backend: SecretsBackend) -> None:
    """Substitui o backend default.

    Uso esperado: testes e bootstrap. Em produção, a troca acontece via
    configuração ``SECRETS_BACKEND`` no import-time desta package.
    """
    global _default_backend
    _default_backend = backend


__all__ = [
    "SecretsBackend",
    "LocalFernetBackend",
    "get_default_backend",
    "set_default_backend",
]
