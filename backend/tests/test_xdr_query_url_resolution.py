"""Regressão: XDRQueryService usa api_host (Partner) ou rejeita geo-code.

Antes: ``base_url = f"https://api-{region}.central.sophos.com/xdr-query/v1"``
quando ``region="EU"`` (geo-code) virava ``api-EU.central.sophos.com`` —
NXDOMAIN ou 403 do load balancer. Tenants Partner sempre têm o ``api_host``
correto (do payload ``/partner/v1/tenants``), que deve ser preferido.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest


def test_xdr_uses_api_host_when_provided():
    """Partner-managed tenant: api_host vence sobre region (que pode ser geo-code)."""
    from backend.app.services.xdr_query import XDRQueryService

    svc = XDRQueryService(
        region="EU",  # geo-code (inválido sozinho)
        headers={"Authorization": "Bearer fake"},
        tenant_id="tenant-xyz",
        api_host="api-eu03.central.sophos.com",
    )
    try:
        assert svc.base_url == "https://api-eu03.central.sophos.com/xdr-query/v1"
    finally:
        svc.close()


def test_xdr_uses_region_slug_when_no_api_host():
    """Standalone tenant: region é datacenter slug → URL derivada normalmente."""
    from backend.app.services.xdr_query import XDRQueryService

    svc = XDRQueryService(
        region="us03",
        headers={"Authorization": "Bearer fake"},
        tenant_id="tenant-abc",
    )
    try:
        assert svc.base_url == "https://api-us03.central.sophos.com/xdr-query/v1"
    finally:
        svc.close()


def test_xdr_raises_when_only_geo_code_provided():
    """Sem api_host e region é geo-code → falha loud, não bate em DNS NXDOMAIN."""
    from backend.app.collectors.vendors._sophos_common import MissingApiHostError
    from backend.app.services.xdr_query import XDRQueryService

    with pytest.raises(MissingApiHostError):
        XDRQueryService(
            region="EU",  # geo-code, sem api_host
            headers={"Authorization": "Bearer fake"},
            tenant_id="tenant-xyz",
        )


def test_async_xdr_uses_api_host():
    """AsyncXDRQueryService propaga api_host para o sync interno."""
    from backend.app.services.xdr_query import AsyncXDRQueryService

    svc = AsyncXDRQueryService(
        region="EU",
        headers={"Authorization": "Bearer fake"},
        tenant_id="tenant-xyz",
        api_host="api-eu03.central.sophos.com",
    )
    try:
        assert svc._sync.base_url == "https://api-eu03.central.sophos.com/xdr-query/v1"
    finally:
        svc.close()
