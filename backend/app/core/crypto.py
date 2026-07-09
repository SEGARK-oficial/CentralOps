"""Wrapper de compatibilidade — delega ao backend default em ``secrets``.

Histórico: este módulo expunha ``encrypt``/``decrypt`` diretamente
acoplados a Fernet. Foi refatorado para delegar a um
:class:`SecretsBackend` plugável (ver ``backend.app.core.secrets``).
Os call-sites existentes (``from .core.crypto import encrypt, decrypt``)
continuam funcionando sem alteração.

Para trocar o backend (ex: KMS-wrapped, AWS Secrets Manager), use
``set_default_backend`` do pacote ``secrets`` no bootstrap do app —
não precisa tocar neste módulo.
"""

from __future__ import annotations

from .secrets import get_default_backend


def encrypt(plaintext: str) -> str:
    return get_default_backend().encrypt(plaintext)


def decrypt(ciphertext: str) -> str:
    return get_default_backend().decrypt(ciphertext)


__all__ = ["encrypt", "decrypt"]
