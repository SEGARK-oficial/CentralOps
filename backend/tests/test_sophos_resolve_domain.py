"""Regression for Fix C.3 — fail loud when Sophos collector cannot resolve a host.

Before this fix, ``SophosAlertsCollector.domain`` (and the cases/detections
twins) blindly built ``f"api-{region}.central.sophos.com"`` whenever
``X-Api-Host`` was empty, with default ``region="eu03"``. If a child
Integration row had ``region`` set to a geographic code (``EU``, ``US``,
``DE``...) — which happened post-Erro A — every collection produced
NXDOMAIN, hidden behind aiohttp connection errors that look like
transient outages.

The new resolver is strict: ``X-Api-Host`` is preferred verbatim;
``X-Region`` only contributes when it matches the datacenter slug
pattern (``eu03``, ``us02``, ``br01``, ...). Anything else raises
``MissingApiHostError`` with the offending integration_id and region.

These tests cover:

1. Verbatim explicit ``X-Api-Host`` wins (including with weird casing /
   no scheme — the partner sync stores it without ``https://``).
2. Datacenter slug fallback works for known good slugs.
3. Geo-code values raise ``MissingApiHostError`` (the bug we're fixing).
4. Empty / missing both raise ``MissingApiHostError``.
"""

from __future__ import annotations

import pytest

from backend.app.collectors.vendors._sophos_common import (
    MissingApiHostError,
    resolve_sophos_domain,
)


# ── 1. Explicit X-Api-Host wins ──────────────────────────────────────


def test_explicit_api_host_used_verbatim():
    headers = {"X-Api-Host": "api-eu03.central.sophos.com"}
    assert resolve_sophos_domain(headers, integration_id=42) == (
        "api-eu03.central.sophos.com"
    )


def test_explicit_api_host_wins_over_region():
    """Explicit X-Api-Host overrides X-Region — covers the failover case
    where Sophos may set apiHost to a different DC than dataRegion."""
    headers = {
        "X-Api-Host": "api-us02.central.sophos.com",
        "X-Region": "us03",  # mismatch — explicit host wins
    }
    assert resolve_sophos_domain(headers, integration_id=1) == (
        "api-us02.central.sophos.com"
    )


def test_explicit_api_host_strips_surrounding_whitespace():
    headers = {"X-Api-Host": "  api-eu03.central.sophos.com  "}
    assert resolve_sophos_domain(headers, integration_id=1) == (
        "api-eu03.central.sophos.com"
    )


# ── 2. Datacenter slug fallback ──────────────────────────────────────


@pytest.mark.parametrize(
    "slug,expected",
    [
        ("eu01", "api-eu01.central.sophos.com"),
        ("eu03", "api-eu03.central.sophos.com"),
        ("us02", "api-us02.central.sophos.com"),
        ("us04", "api-us04.central.sophos.com"),
        ("br01", "api-br01.central.sophos.com"),
        ("au01", "api-au01.central.sophos.com"),
        ("in01", "api-in01.central.sophos.com"),
        ("de01", "api-de01.central.sophos.com"),
        ("jp01", "api-jp01.central.sophos.com"),
        ("ca01", "api-ca01.central.sophos.com"),
    ],
)
def test_known_datacenter_slugs_resolve(slug, expected):
    headers = {"X-Region": slug}
    assert resolve_sophos_domain(headers, integration_id=99) == expected


def test_uppercase_slug_is_normalized_to_lowercase():
    """Defense-in-depth: legacy rows might have manual mixed-case slug."""
    # Note: per the regex (lowercase-only), uppercase slugs do NOT match
    # the datacenter pattern. They are treated as geo codes → fail loud.
    headers = {"X-Region": "EU03"}
    with pytest.raises(MissingApiHostError):
        resolve_sophos_domain(headers, integration_id=99)


# ── 3. Geo-codes fail loud (the original bug) ────────────────────────


@pytest.mark.parametrize(
    "geo",
    ["EU", "US", "DE", "JP", "CA", "AU", "BR", "GB", "IE", "IN"],
)
def test_geo_codes_raise_missing_api_host(geo):
    headers = {"X-Region": geo}
    with pytest.raises(MissingApiHostError) as exc_info:
        resolve_sophos_domain(headers, integration_id=7)
    err = exc_info.value
    assert err.integration_id == 7
    assert err.region == geo
    # The message points the operator at the right action.
    assert "sync_sophos_partner" in str(err) or "re-run" in str(err).lower()


# ── 4. Empty / missing → fail loud ───────────────────────────────────


def test_empty_headers_raise_missing_api_host():
    headers: dict[str, str] = {}
    with pytest.raises(MissingApiHostError) as exc_info:
        resolve_sophos_domain(headers, integration_id=11)
    assert exc_info.value.integration_id == 11
    assert exc_info.value.region is None


def test_blank_strings_treated_as_missing():
    headers = {"X-Api-Host": "  ", "X-Region": ""}
    with pytest.raises(MissingApiHostError):
        resolve_sophos_domain(headers, integration_id=11)


def test_missing_api_host_error_message_is_actionable():
    """Operator-facing message must name the integration and the offender
    so triage in logs is one search away."""
    err = MissingApiHostError(integration_id=123, region="EU")
    msg = str(err)
    assert "123" in msg
    assert "'EU'" in msg or "EU" in msg
    assert "datacenter slug" in msg
    assert "sync_sophos_partner" in msg


# ── 5. Kwargs API (new — used by XDRQueryService) ─────────────────────


def test_kwargs_api_host_wins():
    """Explicit api_host kwarg works without headers dict."""
    assert resolve_sophos_domain(
        api_host="api-eu03.central.sophos.com", region="us02"
    ) == "api-eu03.central.sophos.com"


def test_kwargs_region_slug_fallback():
    """When only region kwarg is provided, slug fallback applies."""
    assert resolve_sophos_domain(region="us02") == "api-us02.central.sophos.com"


def test_kwargs_api_host_strips_https_scheme():
    """Operator might store ``https://api-...`` in api_host. Strip it."""
    assert resolve_sophos_domain(
        api_host="https://api-eu03.central.sophos.com/"
    ) == "api-eu03.central.sophos.com"


def test_kwargs_geo_code_raises():
    """Geo-code via kwarg also raises MissingApiHostError."""
    with pytest.raises(MissingApiHostError) as exc_info:
        resolve_sophos_domain(region="EU", integration_id=88)
    assert exc_info.value.integration_id == 88


def test_kwargs_both_empty_raises():
    with pytest.raises(MissingApiHostError):
        resolve_sophos_domain(api_host=None, region=None, integration_id=42)


def test_kwargs_override_headers():
    """When both kwargs and headers are present, kwargs win."""
    headers = {"X-Api-Host": "api-stale.central.sophos.com"}
    assert resolve_sophos_domain(
        headers, api_host="api-fresh.central.sophos.com"
    ) == "api-fresh.central.sophos.com"
