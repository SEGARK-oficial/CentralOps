"""Canal de VALOR de credencial é plugin-driven.

Vendors declaram chaves de credencial INÉDITAS (okta ``api_token``, cloudtrail
``secret_access_key``) que NÃO existem no schema fixo IntegrationCreate. Antes,
``getattr(data, key)`` devolvia None → required → 400 permanente (vendor não
criável). Com ``extra="allow"`` + leitura de ``model_extra``, a chave flui ao
store ``integration_credentials``.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest

from backend.app.api import schemas
from backend.app.collectors.registry import get_platform
from backend.app.db import models
from backend.app.routers.integrations import _assign_credentials
from backend.app.services import integration_secrets


def _integration(platform: str) -> models.Integration:
    return models.Integration(name="x", organization_id=1, platform=platform, kind="tenant")


def test_okta_api_token_flows_from_extra_to_store():
    """okta declara api_token (inédito) — postado por key no topo, vai ao store."""
    data = schemas.IntegrationCreate(
        organization_id=1, name="okta-1", platform="okta",
        base_url="https://acme.okta.com", api_token="ssws-secret-xyz",
    )
    # extra="allow" preserva a chave inédita.
    assert (data.model_extra or {}).get("api_token") == "ssws-secret-xyz"

    integ = _integration("okta")
    _assign_credentials(integ, data, get_platform("okta"), "tenant")

    assert integration_secrets.read_secret(integ, "api_token") == "ssws-secret-xyz"


def test_cloudtrail_secret_access_key_flows_from_extra():
    """aws_cloudtrail declara secret_access_key (inédito) — idem."""
    data = schemas.IntegrationCreate(
        organization_id=1, name="ct-1", platform="aws_cloudtrail",
        client_id="AKIAFAKE", base_url="my-bucket", tenant_id="123456789012",
        region="us-east-1", secret_access_key="aws-secret-zzz",
    )
    integ = _integration("aws_cloudtrail")
    _assign_credentials(integ, data, get_platform("aws_cloudtrail"), "tenant")

    assert integration_secrets.read_secret(integ, "secret_access_key") == "aws-secret-zzz"
    # chave não-secreta inédita mapeada a coluna existente (tenant_id=account_id)
    assert integ.tenant_id == "123456789012"


def test_missing_required_novel_key_raises_400():
    """Sem a chave inédita obrigatória → 400 descritivo (não silencioso)."""
    # i18n: o helper agora sinaliza via ApiError (localizado server-side),
    # não HTTPException. Continua sendo status 400 e código estável.
    from backend.app.core.errors import ApiError

    data = schemas.IntegrationCreate(
        organization_id=1, name="okta-2", platform="okta",
        base_url="https://acme.okta.com",  # sem api_token
    )
    integ = _integration("okta")
    with pytest.raises(ApiError) as ei:
        _assign_credentials(integ, data, get_platform("okta"), "tenant")
    assert ei.value.status_code == 400
    assert ei.value.code == "integration.missing_required_fields"
