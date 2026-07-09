"""Testes de TokenManager com credential_source.

Cobre:
- ensure_valid_token com credential_source=parent: persiste token no parent,
  não no child; child não recebe os tokens.
- ensure_valid_token standalone (sem credential_source): comportamento legado.
- refresh_after_401 com credential_source=parent: persiste no parent.

Os secrets (client_secret/access_token/refresh_token) vivem no
store ``integration_credentials`` — os fakes populam ``.credentials`` e o
``TokenManager`` lê/escreve via ``integration_secrets`` (sem colunas legadas).
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

from unittest.mock import MagicMock, patch

import pytest

from backend.app.core.crypto import encrypt as _real_encrypt

_SVC_MOD = "backend.app.services.token_manager"


# ── Fake helpers ──────────────────────────────────────────────────────


class _FakeCred:
    """Espelha ``IntegrationCredential`` o suficiente p/ integration_secrets."""

    def __init__(self, logical_name: str, plaintext: str) -> None:
        self.logical_name = logical_name
        self.secret_ref = _real_encrypt(plaintext)
        self.revoked_at = None


def _plain(value: str | None) -> str | None:
    """Aceita o estilo legado ``enc::X`` dos fixtures e devolve o plaintext."""
    if value is None:
        return None
    return value[5:] if value.startswith("enc::") else value


class _FakeIntegration:
    _id_counter = 0

    def __init__(
        self,
        *,
        id: int | None = None,
        name: str = "test",
        kind: str = "tenant",
        tenant_id: str | None = "tenant-xyz",
        client_id: str | None = None,
        client_secret: str | None = None,
        access_token: str | None = None,
        refresh_token: str | None = None,
        region: str | None = "us03",
        parent_integration_id: int | None = None,
    ):
        _FakeIntegration._id_counter += 1
        self.id = id if id is not None else _FakeIntegration._id_counter
        self.name = name
        self.kind = kind
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.region = region
        self.parent_integration_id = parent_integration_id
        self.updated_at = None
        # secrets no store vendor-neutro (não em colunas).
        self.credentials: list[_FakeCred] = []
        for logical_name, value in (
            ("client_secret", client_secret),
            ("access_token", access_token),
            ("refresh_token", refresh_token),
        ):
            plain = _plain(value)
            if plain is not None:
                self.credentials.append(_FakeCred(logical_name, plain))


# ── Tests ─────────────────────────────────────────────────────────────


class TestEnsureValidTokenWithCredentialSource:
    """ensure_valid_token com credential_source=parent persiste no parent."""

    def test_refreshes_into_credential_source_not_child(self):
        """Token novo gravado no parent; child não recebe credencial."""
        parent = _FakeIntegration(
            id=10,
            name="Partner Acme",
            kind="partner",
            client_id="cid",
            client_secret="enc::secret",
            access_token=None,
            refresh_token="enc::refresh-tok",
            tenant_id=None,
        )
        child = _FakeIntegration(
            id=20,
            name="Tenant Beta",
            kind="tenant",
            tenant_id="tenant-xyz",
            access_token=None,
            refresh_token=None,
            parent_integration_id=parent.id,
        )

        tokens_written: dict = {}

        class _FakeRepo:
            def update_tokens(self, integration, *, access_token, refresh_token):
                # tokens chegam em PLAINTEXT (write_secret cifra).
                tokens_written["integration_id"] = integration.id
                tokens_written["access_token"] = access_token
                tokens_written["refresh_token"] = refresh_token

        fake_repo_instance = _FakeRepo()

        def _fake_auth_refresh(refresh_token):
            return {"access_token": "new-access", "refresh_token": "new-refresh"}

        with (
            patch(f"{_SVC_MOD}.IntegrationRepository", return_value=fake_repo_instance),
            patch(f"{_SVC_MOD}.SophosAuthService") as mock_auth_cls,
        ):
            mock_auth_instance = MagicMock()
            mock_auth_instance.refresh.side_effect = _fake_auth_refresh
            mock_auth_cls.return_value = mock_auth_instance

            from backend.app.services.token_manager import TokenManager

            headers = TokenManager.ensure_valid_token(
                child, MagicMock(), credential_source=parent
            )

        # Headers corretos: tenant_id do child, token do parent
        assert headers["X-Tenant-ID"] == "tenant-xyz"
        assert "new-access" in headers["Authorization"]

        # Token persistido no parent (id=10), em PLAINTEXT (repo cifra via store)
        assert tokens_written["integration_id"] == parent.id
        assert tokens_written["access_token"] == "new-access"

        # child não ganhou credencial (token vive no parent)
        assert not any(c.logical_name == "access_token" for c in child.credentials)

    def test_uses_existing_access_token_from_source(self):
        """Se source tem access_token no store, usa direto sem SophosAuthService."""
        parent = _FakeIntegration(
            id=10,
            name="Partner Acme",
            kind="partner",
            client_id="cid",
            client_secret="enc::secret",
            access_token="enc::valid-tok",
            tenant_id=None,
        )
        child = _FakeIntegration(
            id=20,
            name="Tenant Beta",
            tenant_id="tenant-xyz",
            access_token=None,
            parent_integration_id=parent.id,
        )

        with (
            patch(f"{_SVC_MOD}.IntegrationRepository"),
            patch(f"{_SVC_MOD}.SophosAuthService") as mock_auth_cls,
        ):
            from backend.app.services.token_manager import TokenManager

            headers = TokenManager.ensure_valid_token(
                child, MagicMock(), credential_source=parent
            )

            # Não deve chamar SophosAuthService pois já há access_token
            mock_auth_cls.assert_not_called()

        assert headers["X-Tenant-ID"] == "tenant-xyz"
        assert "valid-tok" in headers["Authorization"]

    def test_missing_tenant_id_raises(self):
        """child sem tenant_id levanta RuntimeError antes de qualquer auth."""
        child = _FakeIntegration(id=20, name="Tenant Beta", tenant_id=None)
        parent = _FakeIntegration(id=10, name="Parent", client_id="cid", client_secret="enc::s")

        from backend.app.services.token_manager import TokenManager

        with pytest.raises(RuntimeError, match="missing tenant_id"):
            TokenManager.ensure_valid_token(child, MagicMock(), credential_source=parent)

    def test_missing_client_id_in_source_raises(self):
        """source sem client_id levanta RuntimeError."""
        parent = _FakeIntegration(id=10, name="Partner", client_id=None, client_secret="enc::s")
        child = _FakeIntegration(
            id=20, name="Tenant", tenant_id="t-xyz", parent_integration_id=parent.id
        )

        from backend.app.services.token_manager import TokenManager

        with pytest.raises(RuntimeError, match="invalid client_id"):
            TokenManager.ensure_valid_token(child, MagicMock(), credential_source=parent)


class TestRefreshAfter401WithCredentialSource:
    """refresh_after_401 com credential_source=parent persiste no parent."""

    def test_persists_new_token_on_source(self):
        parent = _FakeIntegration(
            id=10,
            name="Partner Acme",
            kind="partner",
            client_id="cid",
            client_secret="enc::secret",
            tenant_id=None,
        )
        child = _FakeIntegration(
            id=20,
            name="Tenant Beta",
            tenant_id="tenant-xyz",
            parent_integration_id=parent.id,
        )

        tokens_written: dict = {}

        class _FakeRepo:
            def update_tokens(self, integration, *, access_token, refresh_token):
                tokens_written["integration_id"] = integration.id
                tokens_written["access_token"] = access_token

        fake_repo_instance = _FakeRepo()

        with (
            patch(f"{_SVC_MOD}.IntegrationRepository", return_value=fake_repo_instance),
            patch(f"{_SVC_MOD}.SophosAuthService") as mock_auth_cls,
        ):
            mock_auth_instance = MagicMock()
            mock_auth_instance.authenticate.return_value = {
                "access_token": "fresh-tok",
                "refresh_token": "fresh-refresh",
            }
            mock_auth_cls.return_value = mock_auth_instance

            from backend.app.services.token_manager import TokenManager

            headers = TokenManager.refresh_after_401(
                child, MagicMock(), credential_source=parent
            )

        assert headers["X-Tenant-ID"] == "tenant-xyz"
        assert "fresh-tok" in headers["Authorization"]
        assert tokens_written["integration_id"] == parent.id


class TestEnsureValidTokenStandalone:
    """Regressão — standalone sem credential_source usa comportamento legado."""

    def test_standalone_with_access_token_works(self):
        standalone = _FakeIntegration(
            id=30,
            name="Standalone",
            tenant_id="t-abc",
            client_id="cid",
            client_secret="enc::sec",
            access_token="enc::valid-tok",
        )

        with (
            patch(f"{_SVC_MOD}.IntegrationRepository"),
            patch(f"{_SVC_MOD}.SophosAuthService") as mock_auth_cls,
        ):
            from backend.app.services.token_manager import TokenManager

            headers = TokenManager.ensure_valid_token(standalone, MagicMock())
            mock_auth_cls.assert_not_called()

        assert headers["X-Tenant-ID"] == "t-abc"
        assert "valid-tok" in headers["Authorization"]


class TestAutoResolveCredentialSource:
    """credential_source=None + parent_integration_id populado → auto-resolve."""

    def test_auto_resolves_parent_when_credential_source_omitted(self):
        """child sem credential_source explícito → auto-resolve via parent."""
        parent = _FakeIntegration(
            id=10,
            name="Partner Acme",
            kind="partner",
            client_id="cid",
            client_secret="enc::secret",
            access_token="enc::parent-tok",
            tenant_id=None,
        )
        child = _FakeIntegration(
            id=20,
            name="Tenant Beta",
            kind="tenant",
            tenant_id="tenant-xyz",
            access_token=None,
            parent_integration_id=parent.id,
        )

        class _FakeRepo:
            def get_credential_source(self, integration):
                return parent

            def update_tokens(self, *args, **kwargs):
                pass

        with (
            patch(f"{_SVC_MOD}.IntegrationRepository", return_value=_FakeRepo()),
            patch(f"{_SVC_MOD}.SophosAuthService") as mock_auth_cls,
        ):
            from backend.app.services.token_manager import TokenManager

            # NÃO passa credential_source — deve auto-resolver
            headers = TokenManager.ensure_valid_token(child, MagicMock())
            mock_auth_cls.assert_not_called()

        assert headers["X-Tenant-ID"] == "tenant-xyz"
        assert "parent-tok" in headers["Authorization"]

    def test_raises_when_parent_inactive_and_credential_source_omitted(self):
        """child com parent inativo/missing → RuntimeError."""
        child = _FakeIntegration(
            id=20,
            name="Tenant Beta",
            tenant_id="tenant-xyz",
            parent_integration_id=999,
        )

        class _FakeRepo:
            def get_credential_source(self, integration):
                return None  # parent inativo

        with patch(f"{_SVC_MOD}.IntegrationRepository", return_value=_FakeRepo()):
            from backend.app.services.token_manager import TokenManager

            with pytest.raises(RuntimeError, match="Partner-managed child but parent"):
                TokenManager.ensure_valid_token(child, MagicMock())
