"""Hardening do store de segredos.

- read_secret é FAIL-CLOSED: se o secret_ref não estiver cifrado (passthrough do
  decrypt), NÃO devolve a credencial não-autenticada — retorna None.
- write_secret valida entradas (logical_name/plaintext não-vazios).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.app.core.crypto import encrypt
from backend.app.services import integration_secrets as svc


def _integration(creds):
    return SimpleNamespace(id=1, credentials=list(creds))


def _cred(logical_name: str, secret_ref: str, revoked_at=None):
    return SimpleNamespace(logical_name=logical_name, secret_ref=secret_ref, revoked_at=revoked_at)


# ── read_secret fail-closed ──────────────────────────────────────────────


def test_read_secret_returns_plaintext_for_encrypted_ref() -> None:
    integ = _integration([_cred("client_secret", encrypt("real-value"))])
    assert svc.read_secret(integ, "client_secret") == "real-value"


def test_read_secret_fail_closed_on_unencrypted_ref() -> None:
    """secret_ref em claro (corrupção/edição manual) → None, não vaza plaintext."""
    integ = _integration([_cred("client_secret", "plaintext-not-encrypted")])
    assert svc.read_secret(integ, "client_secret") is None


def test_read_secret_none_for_empty_ref() -> None:
    integ = _integration([_cred("client_secret", "")])
    assert svc.read_secret(integ, "client_secret") is None


def test_read_secret_ignores_revoked() -> None:
    integ = _integration([_cred("client_secret", encrypt("v"), revoked_at="2026-01-01")])
    assert svc.read_secret(integ, "client_secret") is None


# ── write_secret validation ──────────────────────────────────────────────


def test_write_secret_rejects_empty_logical_name() -> None:
    integ = _integration([])
    with pytest.raises(ValueError, match="logical_name"):
        svc.write_secret(integ, "  ", "value")


def test_write_secret_rejects_empty_plaintext() -> None:
    integ = _integration([])
    with pytest.raises(ValueError, match="plaintext"):
        svc.write_secret(integ, "client_secret", "")
