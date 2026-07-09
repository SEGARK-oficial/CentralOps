"""Testes para HIGH 1 — LocalKmsStub sem default /tmp + O_EXCL atomic.

Cenários cobertos:
- LocalKmsStubBackend sem master_key_path → TypeError (argumento obrigatório).
- Criação e leitura de chave em path explícito.
- Race condition simulada: FileExistsError → lê arquivo existente.
- Permissão 0600 garantida no arquivo criado.
- build_backend("kms_wrapped_fernet") sem kms_key_path → ValueError.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


# ── Testes de LocalKmsStubBackend ─────────────────────────────────────


def test_local_kms_stub_requires_master_key_path(tmp_path: Path) -> None:
    """LocalKmsStubBackend sem argumento obrigatório deve levantar TypeError."""
    from backend.app.core.secrets.local_kms_stub import LocalKmsStubBackend

    with pytest.raises(TypeError):
        LocalKmsStubBackend()  # type: ignore[call-arg]


def test_local_kms_stub_creates_key_with_explicit_path(tmp_path: Path) -> None:
    """LocalKmsStubBackend com path explícito deve criar e usar a chave."""
    from backend.app.core.secrets.local_kms_stub import LocalKmsStubBackend

    key_path = tmp_path / "kms-master.key"
    stub = LocalKmsStubBackend(master_key_path=str(key_path))

    assert key_path.exists()
    # Wrap/unwrap devem funcionar.
    from cryptography.fernet import Fernet
    dek = Fernet.generate_key()
    wrapped = stub.wrap(dek)
    unwrapped = stub.unwrap(wrapped)
    assert unwrapped == dek


def test_local_kms_stub_file_permission_is_0600(tmp_path: Path) -> None:
    """Arquivo de master key deve ter permissão 0600 (somente dono pode ler/escrever)."""
    from backend.app.core.secrets.local_kms_stub import LocalKmsStubBackend

    key_path = tmp_path / "kms-master-perm.key"
    LocalKmsStubBackend(master_key_path=str(key_path))

    file_stat = os.stat(key_path)
    # Extrai bits de permissão (ignora type bits).
    mode_bits = stat.S_IMODE(file_stat.st_mode)
    assert mode_bits == 0o600, f"Permissão esperada 0o600, encontrada {oct(mode_bits)}"


def test_local_kms_stub_race_condition_reads_existing_file(tmp_path: Path) -> None:
    """FileExistsError durante O_EXCL deve ler o arquivo criado concorrentemente."""
    from cryptography.fernet import Fernet
    from backend.app.core.secrets.local_kms_stub import LocalKmsStubBackend

    key_path = tmp_path / "race-kms.key"

    # Gera uma chave que "outro processo" já escreveu.
    pre_existing_key = Fernet.generate_key()
    fd = os.open(str(key_path), os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
    try:
        os.write(fd, pre_existing_key)
    finally:
        os.close(fd)

    # Mesmo com o arquivo já existindo, LocalKmsStubBackend deve ler o conteúdo.
    stub = LocalKmsStubBackend(master_key_path=str(key_path))
    assert stub._master_key == pre_existing_key


def test_local_kms_stub_two_instances_use_same_key(tmp_path: Path) -> None:
    """Duas instâncias com o mesmo path devem usar a mesma master key."""
    from backend.app.core.secrets.local_kms_stub import LocalKmsStubBackend

    key_path = tmp_path / "shared-kms.key"
    stub1 = LocalKmsStubBackend(master_key_path=str(key_path))
    stub2 = LocalKmsStubBackend(master_key_path=str(key_path))

    assert stub1._master_key == stub2._master_key
    assert stub1.key_id() == stub2.key_id()


# ── Testes de reencrypt_secrets.build_backend ─────────────────────────


def test_build_backend_kms_wrapped_requires_kms_key_path(tmp_path: Path) -> None:
    """build_backend('kms_wrapped_fernet') sem kms_key_path → ValueError."""
    scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    from reencrypt_secrets import build_backend  # type: ignore[import]

    with pytest.raises(ValueError, match="kms_key_path"):
        build_backend("kms_wrapped_fernet", kms_key_path=None)


def test_build_backend_kms_wrapped_with_explicit_path(tmp_path: Path) -> None:
    """build_backend('kms_wrapped_fernet') com kms_key_path explícito → OK."""
    scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    from reencrypt_secrets import build_backend  # type: ignore[import]

    key_path = tmp_path / "reencrypt-kms.key"
    backend = build_backend("kms_wrapped_fernet", kms_key_path=str(key_path))
    assert backend is not None
    assert key_path.exists()
