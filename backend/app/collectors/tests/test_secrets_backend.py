"""Backend default de secrets — comportamento equivalente ao crypto legado.

Verifica que o refator para ``SecretsBackend`` não muda o que call-sites
existentes observam: ``encrypt`` em ``core.crypto`` continua produzindo
um token Fernet com prefixo ``enc::``, e ``decrypt`` aceita tanto esse
formato quanto plaintext puro (compat com valores legados em DB).
"""

from __future__ import annotations

import os

import pytest

# Garante que ``settings`` carregue antes de importar crypto/secrets.
os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

from backend.app.core import crypto
from backend.app.core.secrets import (
    LocalFernetBackend,
    SecretsBackend,
    get_default_backend,
    set_default_backend,
)


def test_default_backend_is_local_fernet() -> None:
    backend = get_default_backend()
    assert isinstance(backend, SecretsBackend)
    assert isinstance(backend, LocalFernetBackend)


def test_encrypt_produces_prefixed_token_and_round_trips() -> None:
    plaintext = "super-secret-client-secret"
    token = crypto.encrypt(plaintext)
    assert token.startswith("enc::")
    assert token != plaintext
    assert crypto.decrypt(token) == plaintext


def test_encrypt_empty_string_is_passthrough() -> None:
    # Comportamento histórico: chamadas com string vazia não cifram.
    # Vários call-sites dependem disso (campos opcionais).
    assert crypto.encrypt("") == ""
    assert crypto.decrypt("") == ""


def test_decrypt_legacy_plaintext_returns_unchanged() -> None:
    # DB pode ter valores antigos sem cifragem. Não devem corromper.
    legacy = "plain-old-value"
    assert crypto.decrypt(legacy) == legacy


def test_decrypt_invalid_token_with_prefix_raises() -> None:
    # Token com prefixo ``enc::`` mas conteúdo inválido — falha dura
    # para evitar entregar credencial corrompida silenciosamente.
    with pytest.raises(ValueError):
        crypto.decrypt("enc::not-a-real-token")


def test_encrypt_is_non_deterministic_but_round_trips() -> None:
    # Fernet usa nonce aleatório — duas chamadas produzem tokens
    # diferentes, ambos decifram para o mesmo plaintext.
    plaintext = "rotation-test"
    a = crypto.encrypt(plaintext)
    b = crypto.encrypt(plaintext)
    assert a != b
    assert crypto.decrypt(a) == plaintext
    assert crypto.decrypt(b) == plaintext


def test_set_default_backend_replaces_active() -> None:
    # Sanity check do hook de troca — usado pela Fase 4 quando entrar
    # KMS/Vault. Restaura o default no fim para não vazar pra outros
    # testes.
    original = get_default_backend()

    class _RecordingBackend(SecretsBackend):
        def __init__(self) -> None:
            self.calls: list[str] = []

        def encrypt(self, plaintext: str) -> str:
            self.calls.append(f"enc:{plaintext}")
            return f"recorded::{plaintext}"

        def decrypt(self, ciphertext: str) -> str:
            self.calls.append(f"dec:{ciphertext}")
            if ciphertext.startswith("recorded::"):
                return ciphertext[len("recorded::"):]
            return ciphertext

    fake = _RecordingBackend()
    try:
        set_default_backend(fake)
        token = crypto.encrypt("abc")
        assert token == "recorded::abc"
        assert crypto.decrypt(token) == "abc"
        assert fake.calls == ["enc:abc", "dec:recorded::abc"]
    finally:
        set_default_backend(original)
