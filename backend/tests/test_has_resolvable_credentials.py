"""Testes unitários do helper IntegrationRepository.has_resolvable_credentials.

Cobre os pré-requisitos para um refresh OAuth bem-sucedido, considerando
Partner-managed children (credencial vive no parent).
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

from unittest.mock import MagicMock

from backend.app.core.crypto import encrypt as _real_encrypt


class _FakeCred:
    def __init__(self, logical_name: str, plaintext: str):
        self.logical_name = logical_name
        self.secret_ref = _real_encrypt(plaintext)
        self.revoked_at = None


class _FakeIntegration:
    def __init__(
        self,
        *,
        id: int = 1,
        name: str = "test",
        is_active: bool = True,
        region: str | None = "us03",
        tenant_id: str | None = "tenant-xyz",
        client_id: str | None = "cid",
        client_secret: str | None = "enc::sec",
        parent_integration_id: int | None = None,
        api_host: str | None = None,
    ):
        self.id = id
        self.name = name
        self.is_active = is_active
        self.region = region
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.parent_integration_id = parent_integration_id
        self.api_host = api_host
        # o segredo client_secret vive no store vendor-neutro
        # (integration_credentials), lido via integration_secrets.has_secret,
        # não mais na coluna batizada. Constrói a relationship .credentials.
        self.credentials = []
        if client_secret is not None:
            plain = (
                client_secret[5:]
                if client_secret.startswith("enc::")
                else client_secret
            )
            self.credentials.append(_FakeCred("client_secret", plain))


def _make_repo(get_by_id_map: dict[int, _FakeIntegration]):
    """Builds an IntegrationRepository with a stubbed `.get(id)` lookup."""
    from backend.app.db.repository import IntegrationRepository

    repo = IntegrationRepository(MagicMock())

    def fake_get(integration_id: int):
        return get_by_id_map.get(integration_id)

    repo.get = fake_get  # type: ignore[assignment]
    return repo


class TestStandaloneIntegration:
    """Integrações sem parent_integration_id."""

    def test_ok_when_all_fields_present(self):
        integ = _FakeIntegration()
        repo = _make_repo({integ.id: integ})

        ok, error = repo.has_resolvable_credentials(integ)
        assert ok is True
        assert error is None

    def test_fails_without_region_and_api_host(self):
        integ = _FakeIntegration(region=None, api_host=None)
        repo = _make_repo({integ.id: integ})

        ok, error = repo.has_resolvable_credentials(integ)
        assert ok is False
        assert "missing region and api_host" in error

    def test_passes_with_api_host_only_when_region_null(self):
        """Tolerância: tenants Partner com region apagado pelo bug do
        test_connection ainda funcionam se api_host está populado.
        XDRQueryService resolve URL via api_host."""
        integ = _FakeIntegration(
            region=None,
            api_host="api-us03.central.sophos.com",
        )
        repo = _make_repo({integ.id: integ})

        ok, error = repo.has_resolvable_credentials(integ)
        assert ok is True
        assert error is None

    def test_fails_without_tenant_id(self):
        integ = _FakeIntegration(tenant_id=None)
        repo = _make_repo({integ.id: integ})

        ok, error = repo.has_resolvable_credentials(integ)
        assert ok is False
        assert "missing tenant_id" in error

    def test_fails_without_client_id(self):
        integ = _FakeIntegration(client_id=None)
        repo = _make_repo({integ.id: integ})

        ok, error = repo.has_resolvable_credentials(integ)
        assert ok is False
        assert "missing OAuth client_id" in error

    def test_fails_without_client_secret(self):
        integ = _FakeIntegration(client_secret=None)
        repo = _make_repo({integ.id: integ})

        ok, error = repo.has_resolvable_credentials(integ)
        assert ok is False
        assert "missing OAuth client_id/client_secret" in error


class TestPartnerManagedChild:
    """Children com parent_integration_id populado."""

    def test_ok_when_parent_active_has_credentials(self):
        parent = _FakeIntegration(
            id=10, name="Partner", client_id="cid", client_secret="enc::sec"
        )
        child = _FakeIntegration(
            id=20,
            name="Child Tenant",
            client_id=None,  # child não tem creds OAuth
            client_secret=None,
            parent_integration_id=parent.id,
        )
        repo = _make_repo({parent.id: parent, child.id: child})

        ok, error = repo.has_resolvable_credentials(child)
        assert ok is True
        assert error is None

    def test_fails_when_parent_inactive(self):
        parent = _FakeIntegration(
            id=10,
            name="Partner",
            is_active=False,
            client_id="cid",
            client_secret="enc::sec",
        )
        child = _FakeIntegration(
            id=20,
            name="Child Tenant",
            client_id=None,
            client_secret=None,
            parent_integration_id=parent.id,
        )
        repo = _make_repo({parent.id: parent, child.id: child})

        ok, error = repo.has_resolvable_credentials(child)
        assert ok is False
        assert "Partner-managed" in error
        assert "missing or inactive" in error

    def test_fails_when_parent_missing(self):
        child = _FakeIntegration(
            id=20,
            name="Child Tenant",
            client_id=None,
            client_secret=None,
            parent_integration_id=999,  # FK quebrada
        )
        repo = _make_repo({child.id: child})

        ok, error = repo.has_resolvable_credentials(child)
        assert ok is False
        assert "Partner-managed" in error

    def test_fails_when_parent_lacks_client_id(self):
        parent = _FakeIntegration(
            id=10, name="Partner", client_id=None, client_secret="enc::sec"
        )
        child = _FakeIntegration(
            id=20,
            name="Child Tenant",
            client_id=None,
            client_secret=None,
            parent_integration_id=parent.id,
        )
        repo = _make_repo({parent.id: parent, child.id: child})

        ok, error = repo.has_resolvable_credentials(child)
        assert ok is False
        assert "missing OAuth client_id" in error

    def test_child_region_and_tenant_id_used_even_when_parent_has_them(self):
        """region/tenant_id são lidos do CHILD, não do parent.

        Parent Partner tipicamente tem region/tenant_id NULL (é só portador
        de credencial). Child precisa ter os seus próprios.
        """
        parent = _FakeIntegration(
            id=10,
            name="Partner",
            client_id="cid",
            client_secret="enc::sec",
            region=None,
            tenant_id=None,
        )
        child = _FakeIntegration(
            id=20,
            name="Child Tenant",
            client_id=None,
            client_secret=None,
            parent_integration_id=parent.id,
            region=None,  # child sem region → falha mesmo com parent OK
        )
        repo = _make_repo({parent.id: parent, child.id: child})

        ok, error = repo.has_resolvable_credentials(child)
        assert ok is False
        assert "missing region" in error
