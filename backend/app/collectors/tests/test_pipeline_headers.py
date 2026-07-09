"""Regression tests for ``_headers_for`` — montagem do header ``X-Tenant-ID``.

Bug observado em produção: tenants Partner-managed (Sophos child) tinham
``integration.tenant_id`` populado com o UUID do PARTNER em vez do
tenant (provavelmente por um sync legado de Partner que escreveu o id
errado). Isso causava 403 em ``POST /detections/v1/queries/detections``,
enquanto ``GET /common/v1/alerts`` e ``GET /cases/v1/cases`` toleravam
silenciosamente.

A fix usa ``external_id`` como fonte de verdade (preenchido a partir
de ``/partner/v1/tenants`` no Partner sync) e mantém ``tenant_id``
apenas como fallback.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from ..pipeline import _headers_for


def _fake_integration(**fields):
    """Cria uma Integration stand-in sem precisar tocar o ORM."""
    integ = MagicMock()
    # Defaults seguros
    integ.tenant_id = None
    integ.external_id = None
    integ.region = None
    integ.api_host = None
    for k, v in fields.items():
        setattr(integ, k, v)
    return integ


def test_x_tenant_id_prefers_external_id_over_tenant_id() -> None:
    """Bug em produção: ``tenant_id`` foi populado com Partner UUID em
    algumas children. ``external_id`` é a fonte canônica.
    """
    integ = _fake_integration(
        tenant_id="3550ba6d-0f4e-4c5d-9097-2560520c16ff",  # Partner UUID (errado)
        external_id="31697ce0-b83a-442b-a63f-1566484bb5a9",  # Tenant UUID (correto)
        region="br01",
    )
    headers = _headers_for("sophos", integ, "test-token")
    assert headers["X-Tenant-ID"] == "31697ce0-b83a-442b-a63f-1566484bb5a9", (
        "X-Tenant-ID deve usar external_id (canônico), não tenant_id legacy"
    )


def test_x_tenant_id_falls_back_to_tenant_id_when_external_id_missing() -> None:
    """Standalone tenants podem ter ``external_id`` vazio — manter compat."""
    integ = _fake_integration(
        tenant_id="033dc4dd-4894-4669-aec0-9a76090e7d73",
        external_id=None,
        region="br01",
    )
    headers = _headers_for("sophos", integ, "test-token")
    assert headers["X-Tenant-ID"] == "033dc4dd-4894-4669-aec0-9a76090e7d73"


def test_x_tenant_id_falls_back_when_external_id_is_empty_string() -> None:
    """external_id="" não deve mascarar tenant_id válido."""
    integ = _fake_integration(
        tenant_id="033dc4dd-4894-4669-aec0-9a76090e7d73",
        external_id="",
        region="br01",
    )
    headers = _headers_for("sophos", integ, "test-token")
    assert headers["X-Tenant-ID"] == "033dc4dd-4894-4669-aec0-9a76090e7d73"


def test_x_tenant_id_omitted_when_both_empty() -> None:
    """Nenhum dos dois → não envia X-Tenant-ID (Partner-level call)."""
    integ = _fake_integration(tenant_id=None, external_id=None, region="us03")
    headers = _headers_for("sophos", integ, "test-token")
    assert "X-Tenant-ID" not in headers


def test_x_tenant_id_strips_whitespace() -> None:
    """Defensivo: valores com espaços não quebram a auth da Sophos."""
    integ = _fake_integration(
        tenant_id=None,
        external_id="  31697ce0-b83a-442b-a63f-1566484bb5a9  ",
        region="br01",
    )
    headers = _headers_for("sophos", integ, "test-token")
    assert headers["X-Tenant-ID"] == "31697ce0-b83a-442b-a63f-1566484bb5a9"


def test_other_sophos_headers_still_present() -> None:
    """Garante que X-Region e X-Api-Host continuam sendo montados."""
    integ = _fake_integration(
        tenant_id=None,
        external_id="31697ce0-b83a-442b-a63f-1566484bb5a9",
        region="br01",
        api_host="api-br01.central.sophos.com",
    )
    headers = _headers_for("sophos", integ, "test-token")
    assert headers["X-Region"] == "br01"
    assert headers["X-Api-Host"] == "api-br01.central.sophos.com"
    assert headers["Authorization"] == "Bearer test-token"


def test_non_sophos_platform_skips_sophos_headers() -> None:
    """Wazuh, Defender etc. não recebem X-Tenant-ID."""
    integ = _fake_integration(
        tenant_id="some-uuid",
        external_id="other-uuid",
        region="us-east-1",
    )
    headers = _headers_for("wazuh", integ, "test-token")
    assert "X-Tenant-ID" not in headers
    assert "X-Region" not in headers
    assert "X-Api-Host" not in headers
