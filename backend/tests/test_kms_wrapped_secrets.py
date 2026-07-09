"""Testes para KmsBackend, LocalKmsStubBackend e KmsWrappedFernetBackend.

Cobre:
- Criação/persistência de master key do stub.
- Wrap/unwrap roundtrip.
- Encrypt/decrypt roundtrip do backend KMS-wrapped.
- Cache de DEK (hit após primeira decrypt, expiração por TTL).
- Fallback de migração para tokens "enc::".
- Passthrough de plaintext legado sem prefixo.
- Erros claros em formato inválido.
- Factory _create_default_backend lê settings.SECRETS_BACKEND.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend.app.core.secrets.kms import KmsBackend
from backend.app.core.secrets.kms_wrapped_fernet import KmsWrappedFernetBackend
from backend.app.core.secrets.local_fernet import LocalFernetBackend
from backend.app.core.secrets.local_kms_stub import LocalKmsStubBackend


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def tmp_kms_key_path(tmp_path: Path) -> str:
    """Caminho temporário para arquivo de master key do stub."""
    return str(tmp_path / "test-kms-master.key")


@pytest.fixture
def stub(tmp_kms_key_path: str) -> LocalKmsStubBackend:
    """LocalKmsStubBackend com master key em diretório temporário."""
    return LocalKmsStubBackend(master_key_path=tmp_kms_key_path)


@pytest.fixture
def kms_backend(stub: LocalKmsStubBackend) -> KmsWrappedFernetBackend:
    """KmsWrappedFernetBackend com LocalKmsStubBackend e legacy fallback."""
    legacy = LocalFernetBackend()
    return KmsWrappedFernetBackend(kms=stub, legacy_fallback=legacy)


# ── LocalKmsStubBackend ───────────────────────────────────────────────


class TestLocalKmsStubBackend:
    def test_creates_master_key_on_first_use(self, tmp_kms_key_path: str) -> None:
        """Na primeira instância, o arquivo de master key deve ser criado."""
        path = Path(tmp_kms_key_path)
        assert not path.exists()

        LocalKmsStubBackend(master_key_path=tmp_kms_key_path)

        assert path.exists()
        key_bytes = path.read_bytes()
        # Chave Fernet é 44 bytes urlsafe-base64.
        assert len(key_bytes) == 44

    def test_creates_master_key_with_restricted_permissions(
        self, tmp_kms_key_path: str
    ) -> None:
        """O arquivo deve ter permissão 0600 (owner rw apenas)."""
        LocalKmsStubBackend(master_key_path=tmp_kms_key_path)
        path = Path(tmp_kms_key_path)

        mode = path.stat().st_mode & 0o777
        assert mode == 0o600

    def test_persists_master_key_across_instances(self, tmp_kms_key_path: str) -> None:
        """Duas instâncias com mesmo path devem usar a mesma master key."""
        stub1 = LocalKmsStubBackend(master_key_path=tmp_kms_key_path)
        stub2 = LocalKmsStubBackend(master_key_path=tmp_kms_key_path)

        assert stub1._master_key == stub2._master_key

    def test_wrap_unwrap_roundtrip(self, stub: LocalKmsStubBackend) -> None:
        """Wrap seguido de unwrap deve devolver os bytes originais."""
        from cryptography.fernet import Fernet

        plaintext_dek = Fernet.generate_key()
        wrapped = stub.wrap(plaintext_dek)

        # Wrapped DEK deve ser diferente da DEK em claro.
        assert wrapped != plaintext_dek

        recovered = stub.unwrap(wrapped)
        assert recovered == plaintext_dek

    def test_wrap_produces_different_bytes_each_call(
        self, stub: LocalKmsStubBackend
    ) -> None:
        """Fernet usa nonce — wrapping do mesmo valor deve gerar bytes distintos."""
        from cryptography.fernet import Fernet

        dek = Fernet.generate_key()
        wrapped1 = stub.wrap(dek)
        wrapped2 = stub.wrap(dek)
        assert wrapped1 != wrapped2

    def test_unwrap_raises_on_wrong_key(self, tmp_path: Path) -> None:
        """Tentar desempacotar com outra master key deve levantar ValueError."""
        from cryptography.fernet import Fernet

        # Stub A cifra a DEK.
        stub_a = LocalKmsStubBackend(master_key_path=str(tmp_path / "key_a.key"))
        dek = Fernet.generate_key()
        wrapped = stub_a.wrap(dek)

        # Stub B tem master key diferente — não consegue desempacotar.
        stub_b = LocalKmsStubBackend(master_key_path=str(tmp_path / "key_b.key"))
        with pytest.raises(ValueError, match="wrapped DEK"):
            stub_b.unwrap(wrapped)

    def test_key_id_format(self, stub: LocalKmsStubBackend) -> None:
        """key_id deve seguir formato 'local-kms-stub:{8 hex chars}'."""
        key_id = stub.key_id()
        assert key_id.startswith("local-kms-stub:")
        suffix = key_id[len("local-kms-stub:"):]
        # 8 caracteres hexadecimais.
        assert len(suffix) == 8
        assert all(c in "0123456789abcdef" for c in suffix)

    def test_key_id_matches_master_key_hash(self, stub: LocalKmsStubBackend) -> None:
        """key_id deve ser derivado dos primeiros 8 hex do sha256 da master key."""
        expected_digest = hashlib.sha256(stub._master_key).hexdigest()[:8]
        assert stub.key_id() == f"local-kms-stub:{expected_digest}"

    def test_key_id_stable_across_instances(self, tmp_kms_key_path: str) -> None:
        """key_id deve ser idêntico entre instâncias com a mesma master key."""
        stub1 = LocalKmsStubBackend(master_key_path=tmp_kms_key_path)
        stub2 = LocalKmsStubBackend(master_key_path=tmp_kms_key_path)
        assert stub1.key_id() == stub2.key_id()


# ── KmsWrappedFernetBackend ───────────────────────────────────────────


class TestKmsWrappedFernetBackendRoundtrip:
    @pytest.mark.parametrize(
        "plaintext",
        [
            "segredo-simples",
            "senha com espaços e acentuação",
            "S3cr3t!@#$%^&*()",
            "a" * 1000,  # string longa
        ],
    )
    def test_encrypt_decrypt_roundtrip(
        self, kms_backend: KmsWrappedFernetBackend, plaintext: str
    ) -> None:
        """Encrypt seguido de decrypt deve devolver o plaintext original."""
        token = kms_backend.encrypt(plaintext)
        assert kms_backend.decrypt(token) == plaintext

    def test_encrypt_produces_kmsenc_prefix(
        self, kms_backend: KmsWrappedFernetBackend
    ) -> None:
        """Token cifrado deve começar com 'kmsenc::'."""
        token = kms_backend.encrypt("meu-segredo")
        assert token.startswith("kmsenc::")

    def test_encrypt_same_plaintext_produces_different_tokens(
        self, kms_backend: KmsWrappedFernetBackend
    ) -> None:
        """Cada encrypt deve gerar uma DEK nova — tokens diferentes para mesmo input."""
        t1 = kms_backend.encrypt("mesmo-valor")
        t2 = kms_backend.encrypt("mesmo-valor")
        assert t1 != t2

    def test_empty_string_passthrough(
        self, kms_backend: KmsWrappedFernetBackend
    ) -> None:
        """String vazia deve ser retornada sem cifragem."""
        assert kms_backend.encrypt("") == ""
        assert kms_backend.decrypt("") == ""


class TestKmsWrappedFernetBackendCache:
    def test_uses_cache_after_first_unwrap(self, stub: LocalKmsStubBackend) -> None:
        """Segunda decrypt com mesmo wrapped DEK não deve chamar KMS.unwrap novamente."""
        # Usa MagicMock wrapping stub para espionar chamadas.
        mock_kms = MagicMock(spec=KmsBackend)
        mock_kms.wrap.side_effect = stub.wrap
        mock_kms.unwrap.side_effect = stub.unwrap
        mock_kms.key_id.return_value = stub.key_id()

        backend = KmsWrappedFernetBackend(kms=mock_kms, dek_cache_ttl_seconds=60)

        token = backend.encrypt("meu-segredo")
        # Primeiro decrypt: chama unwrap (cache miss).
        backend.decrypt(token)
        assert mock_kms.unwrap.call_count == 1

        # Segundo decrypt com mesmo token: deve usar cache (sem chamar unwrap).
        backend.decrypt(token)
        assert mock_kms.unwrap.call_count == 1  # não incrementou

    def test_cache_expires_after_ttl(self, stub: LocalKmsStubBackend) -> None:
        """Após TTL expirar, a entrada não deve mais estar no cache."""
        mock_kms = MagicMock(spec=KmsBackend)
        mock_kms.wrap.side_effect = stub.wrap
        mock_kms.unwrap.side_effect = stub.unwrap
        mock_kms.key_id.return_value = stub.key_id()

        # TTL de 0.1s para forçar expiração rápida no teste.
        backend = KmsWrappedFernetBackend(kms=mock_kms, dek_cache_ttl_seconds=0.1)

        token = backend.encrypt("segredo-ttl")
        backend.decrypt(token)
        assert mock_kms.unwrap.call_count == 1

        # Avança o monotonic clock via patch para simular expiração.
        with patch("backend.app.core.secrets.kms_wrapped_fernet.time") as mock_time:
            mock_time.monotonic.return_value = time.monotonic() + 10.0
            # Extrai wrapped_dek do token para checar cache.
            import base64
            body = token[len("kmsenc::"):]
            wrapped_dek_b64, _ = body.split(":", 1)
            wrapped_dek = base64.urlsafe_b64decode(wrapped_dek_b64)
            cache_key = hashlib.sha256(wrapped_dek).hexdigest()
            # Cache expirado: _cache_get deve retornar None.
            result = backend._cache_get(cache_key)
            assert result is None

    def test_cache_disabled_with_ttl_zero(self, stub: LocalKmsStubBackend) -> None:
        """TTL=0 deve desabilitar o cache completamente."""
        mock_kms = MagicMock(spec=KmsBackend)
        mock_kms.wrap.side_effect = stub.wrap
        mock_kms.unwrap.side_effect = stub.unwrap
        mock_kms.key_id.return_value = stub.key_id()

        backend = KmsWrappedFernetBackend(kms=mock_kms, dek_cache_ttl_seconds=0)
        token = backend.encrypt("sem-cache")

        backend.decrypt(token)
        backend.decrypt(token)

        # Duas decrypts sem cache = dois unwrap.
        assert mock_kms.unwrap.call_count == 2


class TestKmsWrappedFernetBackendFallback:
    def test_handles_legacy_enc_prefix(self, stub: LocalKmsStubBackend) -> None:
        """Tokens 'enc::' devem ser decifrados pelo legacy_fallback."""
        legacy = LocalFernetBackend()
        backend = KmsWrappedFernetBackend(kms=stub, legacy_fallback=legacy)

        # Cifra com o backend legado.
        legacy_token = legacy.encrypt("segredo-legado")
        assert legacy_token.startswith("enc::")

        # Backend KMS-wrapped deve delegar ao legacy_fallback.
        result = backend.decrypt(legacy_token)
        assert result == "segredo-legado"

    def test_raises_without_legacy_fallback_for_enc_token(
        self, stub: LocalKmsStubBackend
    ) -> None:
        """Token 'enc::' sem legacy_fallback deve levantar ValueError."""
        backend = KmsWrappedFernetBackend(kms=stub, legacy_fallback=None)
        with pytest.raises(ValueError, match="legacy_fallback"):
            backend.decrypt("enc::alguma-coisa")

    def test_passthrough_plaintext_legacy(
        self, kms_backend: KmsWrappedFernetBackend
    ) -> None:
        """String sem prefixo reconhecido deve ser retornada sem modificação."""
        plaintext_legacy = "valor-em-claro-antes-da-cifragem"
        assert kms_backend.decrypt(plaintext_legacy) == plaintext_legacy

    def test_passthrough_preserves_empty_string(
        self, kms_backend: KmsWrappedFernetBackend
    ) -> None:
        """String vazia deve ser retornada diretamente (campos opcionais no DB)."""
        assert kms_backend.decrypt("") == ""


class TestKmsWrappedFernetBackendErrors:
    @pytest.mark.parametrize(
        "bad_token",
        [
            "kmsenc::",  # vazio após prefixo
            "kmsenc::semapaartedois",  # sem separador ":"
            "kmsenc::aaa:bbb",  # base64 válido mas Fernet inválido
        ],
    )
    def test_handles_invalid_format(
        self, kms_backend: KmsWrappedFernetBackend, bad_token: str
    ) -> None:
        """Tokens malformados devem levantar ValueError com mensagem clara."""
        with pytest.raises(ValueError):
            kms_backend.decrypt(bad_token)


# ── Factory _create_default_backend ──────────────────────────────────


class TestSecretsBackendFactory:
    def test_creates_local_fernet_by_default(self) -> None:
        """SECRETS_BACKEND='local_fernet' deve criar LocalFernetBackend."""
        from backend.app.core.secrets import _create_default_backend

        with patch("backend.app.core.secrets.settings") as mock_settings:
            mock_settings.SECRETS_BACKEND = "local_fernet"
            backend = _create_default_backend()

        assert isinstance(backend, LocalFernetBackend)

    def test_creates_kms_wrapped_when_configured(self, tmp_path: Path) -> None:
        """SECRETS_BACKEND='kms_wrapped_fernet' deve criar KmsWrappedFernetBackend."""
        from backend.app.core.secrets import _create_default_backend

        key_path = str(tmp_path / "factory-test.key")
        with patch("backend.app.core.secrets.settings") as mock_settings:
            mock_settings.SECRETS_BACKEND = "kms_wrapped_fernet"
            mock_settings.KMS_PROVIDER = "local_stub"
            mock_settings.KMS_LOCAL_STUB_MASTER_KEY_PATH = key_path
            mock_settings.KMS_DEK_CACHE_TTL_SECONDS = 60
            backend = _create_default_backend()

        assert isinstance(backend, KmsWrappedFernetBackend)
        assert backend.legacy_fallback is not None
        assert isinstance(backend.legacy_fallback, LocalFernetBackend)

    def test_raises_on_unknown_backend_name(self) -> None:
        """Nome de backend desconhecido deve levantar ValueError."""
        from backend.app.core.secrets import _create_default_backend

        with patch("backend.app.core.secrets.settings") as mock_settings:
            mock_settings.SECRETS_BACKEND = "invalid_backend_xyz"
            with pytest.raises(ValueError, match="SECRETS_BACKEND desconhecido"):
                _create_default_backend()

    def test_kms_wrapped_backend_is_functional_end_to_end(
        self, tmp_path: Path
    ) -> None:
        """Backend criado pela factory deve ser capaz de encrypt/decrypt."""
        from backend.app.core.secrets import _create_default_backend

        key_path = str(tmp_path / "e2e-factory.key")
        with patch("backend.app.core.secrets.settings") as mock_settings:
            mock_settings.SECRETS_BACKEND = "kms_wrapped_fernet"
            mock_settings.KMS_PROVIDER = "local_stub"
            mock_settings.KMS_LOCAL_STUB_MASTER_KEY_PATH = key_path
            mock_settings.KMS_DEK_CACHE_TTL_SECONDS = 60
            backend = _create_default_backend()

        secret = "segredo-de-ponta-a-ponta"
        token = backend.encrypt(secret)
        assert token.startswith("kmsenc::")
        assert backend.decrypt(token) == secret
