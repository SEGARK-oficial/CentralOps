"""Testes do VaultTransitBackend (KMS via Vault Transit) — secrets.

``hvac`` é um EXTRA opcional (requirements-vault.txt) e não está instalado no
sweep. Estes testes injetam um FAKE ``hvac`` em ``sys.modules`` que replica o
round-trip do Transit (encrypt→"vault:v1:..", decrypt→plaintext, valida base64
como o Vault real) e a expiração/re-login do AppRole — provando wrap/unwrap,
auth PREGUIÇOSA (boot resiliente), re-auth DEDUPLICADA sob concorrência, timeout/
transporte, e sanitização de erro — tudo sem Vault real.
"""

from __future__ import annotations

import base64
import os
import sys
import threading
import types

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import pytest

# ``requests`` vem junto com ``hvac`` (extra opcional, requirements-vault.txt) e
# NÃO está na imagem base nem no sweep de testes do Docker (compose/cython-tests.sh).
# Os casos de erro de transporte abaixo dependem dele → skip LIMPO quando ausente
# (espelha o guard do próprio backend em vault_transit.py:137). Sem isto, a coleta
# do pytest no sweep falha com ModuleNotFoundError e quebra o build da imagem.
pytest.importorskip("requests")
from requests.exceptions import ConnectionError as ReqConnectionError


# ────────────────────────────────────────────────────────────────────────────
# Fake hvac — replica a superfície do Transit + expiração de token AppRole.
# ────────────────────────────────────────────────────────────────────────────
class _VaultError(Exception):
    pass


class _Forbidden(_VaultError):  # no hvac real, Forbidden É um VaultError
    pass


class _FakeTransit:
    """Transit fake fiel: ciphertext='vault:v1:'+plaintext(b64); valida base64."""

    def __init__(self, client: "_FakeClient") -> None:
        self._client = client

    def encrypt_data(self, *, name, plaintext, mount_point):
        self._client._guard()
        # Fidelidade: o Vault EXIGE plaintext base64; um regress de encoding falha aqui.
        base64.b64decode(plaintext, validate=True)
        self._client.encrypt_calls += 1
        return {"data": {"ciphertext": f"vault:v1:{plaintext}"}}

    def decrypt_data(self, *, name, ciphertext, mount_point):
        self._client._guard()
        if not ciphertext.startswith("vault:v1:"):
            raise _VaultError("formato inválido")
        return {"data": {"plaintext": ciphertext[len("vault:v1:") :]}}

    def read_key(self, *, name, mount_point):
        self._client._guard()
        return {"data": {"latest_version": 7}}


class _FakeApprole:
    def __init__(self, client: "_FakeClient") -> None:
        self._client = client

    def login(self, *, role_id, secret_id, mount_point):
        if self._client.login_raises:
            raise ReqConnectionError("vault unreachable")
        self._client.login_calls += 1
        token = f"s.token-{self._client.login_calls}"
        self._client.token = token
        self._client.force_forbidden = False  # o re-login renova o token
        return {"auth": {"client_token": token}}


class _FakeAuth:
    def __init__(self, client: "_FakeClient") -> None:
        self.approle = _FakeApprole(client)


class _FakeSecrets:
    def __init__(self, client: "_FakeClient") -> None:
        self.transit = _FakeTransit(client)


class _FakeClient:
    def __init__(self, url=None, namespace=None, verify=True, timeout=None):
        self.url = url
        self.namespace = namespace
        self.verify = verify
        self.timeout = timeout
        self.token = None
        self.login_calls = 0
        self.encrypt_calls = 0
        # Flags de simulação:
        self.force_forbidden = False   # token "expirado" → toda op dá Forbidden
        self.login_raises = False      # Vault fora → login levanta ConnectionError
        self.transport_error = False   # blip de rede nas ops de dados
        self.auth = _FakeAuth(self)
        self.secrets = _FakeSecrets(self)

    def _guard(self):
        if self.transport_error:
            raise ReqConnectionError("read timeout")
        if self.force_forbidden:
            raise _Forbidden("permission denied")
        if not self.token:
            raise _Forbidden("missing token")


def _install_fake_hvac(monkeypatch):
    mod = types.ModuleType("hvac")
    exc = types.ModuleType("hvac.exceptions")
    exc.Forbidden = _Forbidden
    exc.VaultError = _VaultError
    exc.InvalidPath = type("InvalidPath", (_VaultError,), {})
    mod.exceptions = exc
    mod.Client = _FakeClient
    monkeypatch.setitem(sys.modules, "hvac", mod)
    monkeypatch.setitem(sys.modules, "hvac.exceptions", exc)
    return mod


def _make_backend(monkeypatch, **overrides):
    _install_fake_hvac(monkeypatch)
    from backend.app.core.secrets.vault_transit import VaultTransitBackend

    kwargs = dict(addr="https://vault.test:8200", key_name="centralops", token="s.root")
    kwargs.update(overrides)
    return VaultTransitBackend(**kwargs)


def _make_approle(monkeypatch, **overrides):
    return _make_backend(
        monkeypatch, auth_method="approle", token=None,
        role_id="r-1", secret_id="s-1", **overrides,
    )


def _dek():
    return base64.urlsafe_b64encode(b"0" * 32)


# ── Round-trip ──────────────────────────────────────────────────────────────
def test_wrap_unwrap_roundtrip(monkeypatch):
    be = _make_backend(monkeypatch)
    dek = _dek()
    wrapped = be.wrap(dek)
    assert wrapped.startswith(b"vault:v1:")
    assert be.unwrap(wrapped) == dek


# ── Auth PREGUIÇOSA + boot resiliente ───────────────────────────────────────
def test_init_does_no_network(monkeypatch):
    """Construir NÃO autentica (I/O-free) — boot não depende do Vault de pé."""
    be = _make_approle(monkeypatch)
    assert be._client.login_calls == 0          # nenhum login no __init__
    assert be.key_id().endswith(":v?")           # versão lazy, sem rede

    be.wrap(_dek())                              # 1º uso → autentica + lê versão
    assert be._client.login_calls == 1
    assert be.key_id() == "vault-transit:transit/centralops:v7"


def test_vault_down_does_not_raise_at_construction(monkeypatch):
    """Vault fora no boot: construir NÃO levanta; a 1ª op vira ValueError limpo."""
    be = _make_approle(monkeypatch)              # construído sem erro
    be._client.login_raises = True               # Vault fora a partir de agora
    with pytest.raises(ValueError):
        be.wrap(_dek())
    assert be._authed is False                   # re-tenta no próximo uso (resiliente)


# ── Validação de config (sem rede) ──────────────────────────────────────────
def test_missing_addr_raises(monkeypatch):
    with pytest.raises(ValueError, match="VAULT_ADDR"):
        _make_backend(monkeypatch, addr="")


def test_approle_requires_role_and_secret(monkeypatch):
    with pytest.raises(ValueError, match="VAULT_ROLE_ID"):
        _make_backend(monkeypatch, auth_method="approle", token=None)


def test_unknown_auth_method_raises(monkeypatch):
    with pytest.raises(ValueError, match="VAULT_AUTH_METHOD"):
        _make_backend(monkeypatch, auth_method="ldap")


# ── Re-auth AppRole DEDUPLICADA sob concorrência (thundering herd) ──────────
def test_concurrent_reauth_single_login(monkeypatch):
    be = _make_approle(monkeypatch)
    be.wrap(_dek())                              # autentica (login_calls=1)
    assert be._client.login_calls == 1

    N = 8
    be._client.force_forbidden = True            # token "expirou" p/ todas as threads
    barrier = threading.Barrier(N)
    errors: list = []

    def worker():
        barrier.wait()                           # dispara as N juntas
        try:
            be.wrap(_dek())
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    # Double-check por geração: UM re-login serve as N threads (não N).
    assert be._client.login_calls == 2


def test_token_auth_does_not_reauth(monkeypatch):
    be = _make_backend(monkeypatch)              # token estático
    be.wrap(_dek())                              # autentica
    be._client.force_forbidden = True
    with pytest.raises(ValueError):              # Forbidden→ValueError; NÃO re-loga
        be.wrap(_dek())


# ── Transporte/outage → ValueError (sem vazar) ──────────────────────────────
def test_transport_error_becomes_valueerror(monkeypatch):
    be = _make_backend(monkeypatch)
    be.wrap(_dek())
    be._client.transport_error = True            # blip de rede
    with pytest.raises(ValueError, match="recusou o wrap"):
        be.wrap(_dek())


def test_unwrap_bad_ciphertext_raises_valueerror(monkeypatch):
    be = _make_backend(monkeypatch)
    be.wrap(_dek())
    # Ciphertext que o Vault rejeita (não é "vault:v1:..") → VaultError → ValueError.
    with pytest.raises(ValueError, match="recusou o unwrap"):
        be.unwrap(b"not-a-vault-ciphertext")


def test_unwrap_non_ascii_raises_valueerror(monkeypatch):
    be = _make_backend(monkeypatch)
    with pytest.raises(ValueError, match="não-ASCII"):
        be.unwrap(b"\xff\xfe\x00corrompido")


def test_wrap_error_is_sanitized_no_vault_detail(monkeypatch):
    be = _make_backend(monkeypatch)
    be.wrap(_dek())
    be._client.force_forbidden = True
    try:
        be.wrap(_dek())
        assert False, "deveria levantar"
    except ValueError as exc:
        # Não vaza path/policy/mount do Vault — só o tipo da exceção.
        s = str(exc)
        assert "transit/" not in s and "policy" not in s.lower()


# ── Envelope completo + factory ─────────────────────────────────────────────
def test_full_envelope_through_kms_wrapped_fernet(monkeypatch):
    _install_fake_hvac(monkeypatch)
    from backend.app.core.secrets.vault_transit import VaultTransitBackend
    from backend.app.core.secrets.kms_wrapped_fernet import KmsWrappedFernetBackend
    from backend.app.core.secrets.local_fernet import LocalFernetBackend

    kms = VaultTransitBackend(addr="https://vault.test:8200", key_name="centralops", token="s.root")
    be = KmsWrappedFernetBackend(kms=kms, legacy_fallback=LocalFernetBackend(), dek_cache_ttl_seconds=0)
    token = be.encrypt("super-secret-credential")
    assert token.startswith("kmsenc::")
    assert be.decrypt(token) == "super-secret-credential"


def test_factory_builds_vault_transit(monkeypatch):
    _install_fake_hvac(monkeypatch)
    from backend.app.core import secrets as secrets_pkg
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "SECRETS_BACKEND", "kms_wrapped_fernet")
    monkeypatch.setattr(settings, "KMS_PROVIDER", "vault_transit")
    monkeypatch.setattr(settings, "VAULT_ADDR", "https://vault.test:8200")
    monkeypatch.setattr(settings, "VAULT_AUTH_METHOD", "token")
    monkeypatch.setattr(settings, "VAULT_TOKEN", "s.root")

    kms = secrets_pkg._build_kms()
    assert type(kms).__name__ == "VaultTransitBackend"
    assert kms.key_id().startswith("vault-transit:")


def test_factory_unknown_provider_raises(monkeypatch):
    from backend.app.core import secrets as secrets_pkg
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "KMS_PROVIDER", "nope")
    with pytest.raises(ValueError, match="KMS_PROVIDER desconhecido"):
        secrets_pkg._build_kms()
