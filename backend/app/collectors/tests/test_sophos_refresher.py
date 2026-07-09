"""sophos_refresher — garante que vai ao IdP em vez de devolver token
velho do banco (bug real que causava 401 em produção após 1h).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from backend.app.core.crypto import encrypt as _real_encrypt

from .. import auth as auth_pkg  # noqa: F401  — ensure package importable
from ..auth import refreshers


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
    def __init__(self) -> None:
        self.id = 42
        self.client_id = "client-abc"
        self.region = "eu03"
        self.tenant_id = "tenant-1"
        self.name = "test-int"
        # secrets no store vendor-neutro (não em colunas).
        # read_secret/has_secret iteram ``.credentials`` (lista detached-friendly).
        self.credentials: list[_FakeCred] = []
        for logical_name, value in (
            ("client_secret", "enc::cipher-secret"),
            ("access_token", "enc::very-old-token-issued-weeks-ago"),
            ("refresh_token", "enc::the-refresh-token"),
        ):
            plain = _plain(value)
            if plain is not None:
                self.credentials.append(_FakeCred(logical_name, plain))


class _FakeDb:
    def __init__(self, integration: _FakeIntegration) -> None:
        self._integration = integration

    def query(self, _model):
        class _Q:
            def __init__(self, integ):
                self._integ = integ

            def filter(self, _criterion):
                return self

            def first(self_inner):
                return self_inner._integ  # type: ignore[misc]

        return _Q(self._integration)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture()
def fake_integration() -> _FakeIntegration:
    return _FakeIntegration()


@pytest.mark.asyncio
async def test_refresher_hits_idp_via_refresh_token_not_db_cache(
    fake_integration: _FakeIntegration,
) -> None:
    """Caso feliz: refresh_token existe → chama ``auth.refresh(...)``.

    **Crítico**: o refresher NUNCA pode devolver o ``access_token`` do
    banco. O ``oauth_cache`` é a única autoridade de cache. Se este
    teste falhar, o bug de 401 com token de 15 dias volta.
    """
    new_tokens = {
        "access_token": "fresh-token-just-from-idp",
        "refresh_token": "rotated-rt",
        "expires_in": 3600,
    }

    fake_auth_service = MagicMock()
    fake_auth_service.refresh.return_value = new_tokens
    fake_repo = MagicMock()

    with patch.object(refreshers, "database") as mock_db_module, \
         patch("backend.app.services.auth.SophosAuthService", return_value=fake_auth_service), \
         patch("backend.app.db.repository.IntegrationRepository", return_value=fake_repo):
        mock_db_module.SessionLocal.return_value = _FakeDb(fake_integration)

        result = await refreshers.sophos_refresher(fake_integration.id)

    # Garantia central: o IdP foi chamado de verdade, não o banco.
    # O refresh_token vem do store (integration_credentials), em plaintext.
    fake_auth_service.refresh.assert_called_once_with("the-refresh-token")
    # Garantia secundária: nunca devolveu o token velho do banco.
    assert result["access_token"] != "very-old-token-issued-weeks-ago"
    assert result["access_token"] == "fresh-token-just-from-idp"
    assert result["expires_in"] == 3600
    # Token novo foi persistido — o repo recebe PLAINTEXT (write_secret cifra).
    fake_repo.update_tokens.assert_called_once()
    assert fake_repo.update_tokens.call_args.kwargs["access_token"] == "fresh-token-just-from-idp"


@pytest.mark.asyncio
async def test_refresher_falls_back_to_client_credentials_when_refresh_fails(
    fake_integration: _FakeIntegration,
) -> None:
    """Se o refresh_token foi revogado, cai em ``authenticate()`` completo."""
    fake_auth_service = MagicMock()
    fake_auth_service.refresh.side_effect = RuntimeError("invalid_grant")
    fake_auth_service.authenticate.return_value = {
        "access_token": "full-reauth-token",
        "refresh_token": "new-rt",
        "expires_in": 3600,
    }
    fake_auth_service.discover_region_and_tenant.return_value = ("eu03", "tenant-x")
    fake_repo = MagicMock()

    with patch.object(refreshers, "database") as mock_db_module, \
         patch("backend.app.services.auth.SophosAuthService", return_value=fake_auth_service), \
         patch("backend.app.db.repository.IntegrationRepository", return_value=fake_repo):
        mock_db_module.SessionLocal.return_value = _FakeDb(fake_integration)

        result = await refreshers.sophos_refresher(fake_integration.id)

    fake_auth_service.refresh.assert_called_once()
    fake_auth_service.authenticate.assert_called_once()
    fake_auth_service.discover_region_and_tenant.assert_called_once_with("full-reauth-token")
    assert result["access_token"] == "full-reauth-token"
    # Persiste também region + tenant_id (novo no full re-auth)
    fake_repo.update_integration_tokens.assert_called_once()
    # Regressão: integration_id deve ser int, NÃO o objeto ORM. Antes do
    # fix, callers passavam `integ` direto, causando psycopg2
    # ProgrammingError "can't adapt type 'Integration'" em produção.
    call_kwargs = fake_repo.update_integration_tokens.call_args.kwargs
    assert call_kwargs.get("integration_id") == fake_integration.id, (
        "update_integration_tokens deve receber integration_id=<int>, "
        f"recebeu {call_kwargs.get('integration_id')!r}"
    )
    assert isinstance(call_kwargs["integration_id"], int)


@pytest.mark.asyncio
async def test_refresher_uses_client_credentials_when_no_refresh_token(
    fake_integration: _FakeIntegration,
) -> None:
    """Sem refresh_token persistido, pula direto para client_credentials."""
    # "sem refresh_token" = sem a credencial no store, não a
    # coluna legada (que não existe mais). read_secret retorna None.
    fake_integration.credentials = [
        c for c in fake_integration.credentials if c.logical_name != "refresh_token"
    ]

    fake_auth_service = MagicMock()
    fake_auth_service.authenticate.return_value = {
        "access_token": "new-cc-token",
        "refresh_token": "",
        "expires_in": 3600,
    }
    fake_auth_service.discover_region_and_tenant.return_value = ("eu03", "tenant-y")
    fake_repo = MagicMock()

    with patch.object(refreshers, "database") as mock_db_module, \
         patch("backend.app.services.auth.SophosAuthService", return_value=fake_auth_service), \
         patch("backend.app.db.repository.IntegrationRepository", return_value=fake_repo):
        mock_db_module.SessionLocal.return_value = _FakeDb(fake_integration)

        result = await refreshers.sophos_refresher(fake_integration.id)

    # Não tentou refresh (não tinha token).
    fake_auth_service.refresh.assert_not_called()
    fake_auth_service.authenticate.assert_called_once()
    assert result["access_token"] == "new-cc-token"
    # Regressão: integration_id deve ser int (ver comentário no teste anterior).
    fake_repo.update_integration_tokens.assert_called_once()
    call_kwargs = fake_repo.update_integration_tokens.call_args.kwargs
    assert call_kwargs.get("integration_id") == fake_integration.id
    assert isinstance(call_kwargs["integration_id"], int)
