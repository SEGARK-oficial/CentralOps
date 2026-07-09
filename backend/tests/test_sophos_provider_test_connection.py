"""Regressão: test_connection() não pode sobrescrever region/tenant_id em children Partner.

Bug encontrado em produção: user clicou "Test Connection" em integrações
filhas (kind="tenant", parent_integration_id=N). O fluxo legado caía em
discover_region_and_tenant(token_do_partner) → whoami do Partner retorna
dataRegion="" e id=<partner_uuid>. Isso sobrescrevia o child no DB com
region NULL e tenant_id=partner_uuid (errados).

O fix: detectar parent_integration_id IS NOT NULL e pular o fluxo de
discover_region_and_tenant — metadados (region/tenant_id/api_host) já
vieram do payload /partner/v1/tenants no Partner sync e não devem ser
tocados pelo test_connection.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

from unittest.mock import MagicMock, patch


_PROVIDER_MOD = "backend.app.providers.sophos.provider"


class _FakeIntegration:
    def __init__(
        self,
        *,
        id: int = 20,
        name: str = "Child Tenant",
        kind: str = "tenant",
        region: str | None = "us03",
        tenant_id: str | None = "tenant-xyz-child",
        external_id: str | None = "tenant-xyz-child",
        api_host: str | None = "api-us03.central.sophos.com",
        id_type: str | None = "tenant",
        parent_integration_id: int | None = 10,
        client_id: str | None = None,
        client_secret: str | None = None,
        access_token: str | None = None,
        refresh_token: str | None = None,
        platform: str = "sophos",
    ):
        self.id = id
        self.name = name
        self.kind = kind
        self.region = region
        self.tenant_id = tenant_id
        self.external_id = external_id
        self.api_host = api_host
        self.id_type = id_type
        self.parent_integration_id = parent_integration_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.platform = platform
        self.updated_at = None
        # secrets no store; _persit_tokens apenda/rotaciona aqui.
        self.credentials: list = []


def test_test_connection_does_not_overwrite_child_region_or_tenant():
    """child Partner: test_connection retorna healthy SEM mexer em region/tenant_id."""
    child = _FakeIntegration(
        id=20,
        region="us03",
        tenant_id="tenant-xyz-child",
        external_id="tenant-xyz-child",
        api_host="api-us03.central.sophos.com",
        parent_integration_id=10,
    )

    fake_auth = MagicMock()
    fake_auth.authenticate.return_value = {
        "access_token": "partner-token",
        "refresh_token": "partner-refresh",
    }
    # Esta chamada NÃO deve ocorrer para child Partner.
    fake_auth.discover_region_and_tenant.side_effect = AssertionError(
        "discover_region_and_tenant não deve ser chamado para child Partner"
    )

    from backend.app.providers.sophos.provider import SophosProvider

    with patch.object(SophosProvider, "_get_auth_service", return_value=fake_auth):
        provider = SophosProvider(child)
        result = provider.test_connection()

    assert result.status == "healthy"
    # Metadados ORIGINAIS preservados — nada foi sobrescrito.
    assert child.region == "us03"
    assert child.tenant_id == "tenant-xyz-child"
    assert child.external_id == "tenant-xyz-child"
    assert child.api_host == "api-us03.central.sophos.com"
    # Detalhes operacionais úteis na UI.
    assert result.details["region"] == "us03"
    assert result.details["api_host"] == "api-us03.central.sophos.com"
    assert "Partner" in result.details["message"]


def test_test_connection_partner_root_still_works():
    """Partner root (kind='partner'): fluxo legado preservado, discover_identity é chamado."""
    partner = _FakeIntegration(
        id=10,
        name="Acme Partner",
        kind="partner",
        region=None,
        tenant_id=None,
        external_id="partner-uuid",
        api_host=None,
        parent_integration_id=None,
        client_id="cid",
        client_secret="enc::sec",
    )

    fake_auth = MagicMock()
    fake_auth.authenticate.return_value = {
        "access_token": "tok",
        "refresh_token": "ref",
    }
    fake_auth.discover_identity.return_value = {
        "id": "partner-uuid",
        "id_type": "partner",
        "api_hosts": {},
    }

    from backend.app.providers.sophos.provider import SophosProvider

    # SessionLocal mock — partner flow grava token via DB.
    fake_session = MagicMock()
    fake_session.__enter__ = MagicMock(return_value=fake_session)
    fake_session.__exit__ = MagicMock(return_value=False)
    fake_row = MagicMock()
    fake_row.credentials = []  # write_secret apenda os tokens aqui
    fake_session.get.return_value = fake_row

    with (
        patch.object(SophosProvider, "_get_auth_service", return_value=fake_auth),
        patch(f"{_PROVIDER_MOD}._db_module.SessionLocal", return_value=fake_session),
    ):
        provider = SophosProvider(partner)
        result = provider.test_connection()

    assert result.status == "healthy"
    # Partner: identity validado, mas region/tenant_id ficam None.
    assert partner.region is None
    assert partner.tenant_id is None
    fake_auth.discover_identity.assert_called_once()
    # discover_region_and_tenant NÃO é chamado pra partner — só pra standalone.
    fake_auth.discover_region_and_tenant.assert_not_called()


def test_test_connection_standalone_tenant_still_discovers():
    """Standalone (kind='tenant', sem parent): fluxo legado preservado.

    Aqui é correto chamar discover_region_and_tenant porque o whoami
    com token do próprio tenant retorna dataRegion correto.
    """
    standalone = _FakeIntegration(
        id=99,
        name="Standalone Acme",
        kind="tenant",
        region=None,  # ainda não descoberto
        tenant_id=None,
        external_id=None,
        parent_integration_id=None,  # sem parent — standalone
        client_id="cid",
        client_secret="enc::sec",
    )

    fake_auth = MagicMock()
    fake_auth.authenticate.return_value = {
        "access_token": "tok",
        "refresh_token": "ref",
    }
    fake_auth.discover_region_and_tenant.return_value = ("us02", "tenant-acme-uuid")

    from backend.app.providers.sophos.provider import SophosProvider

    fake_session = MagicMock()
    fake_session.__enter__ = MagicMock(return_value=fake_session)
    fake_session.__exit__ = MagicMock(return_value=False)
    fake_row = MagicMock()
    fake_row.external_id = None
    fake_row.credentials = []  # write_secret apenda os tokens aqui
    fake_session.get.return_value = fake_row

    with (
        patch.object(SophosProvider, "_get_auth_service", return_value=fake_auth),
        patch(f"{_PROVIDER_MOD}._db_module.SessionLocal", return_value=fake_session),
    ):
        provider = SophosProvider(standalone)
        result = provider.test_connection()

    assert result.status == "healthy"
    # Standalone: region/tenant_id descobertos e gravados.
    assert standalone.region == "us02"
    assert standalone.tenant_id == "tenant-acme-uuid"
    fake_auth.discover_region_and_tenant.assert_called_once()
