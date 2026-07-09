"""Unit tests for backend.app.providers.sophos.licensing.fetch_licenses.

Targets the licenses-v1 endpoint on the GLOBAL Sophos host (not regional).
Validated against the official Sophos Postman collection (docs/Sophos
Central APIs.postman_collection.json) and real Partner-managed tenants.
"""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import MagicMock, patch

import fakeredis
import httpx
import pytest

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

_MOD = "backend.app.providers.sophos.licensing"


# ── Helpers ───────────────────────────────────────────────────────────


class _FakeIntegration:
    """Minimal Integration stand-in — mirrors fields used by fetch_licenses."""

    def __init__(
        self,
        *,
        id: int = 42,
        kind: str = "tenant",
        parent_integration_id: int | None = 10,
        api_host: str | None = "api-eu03.central.sophos.com",
        region: str | None = "eu03",
        access_token: str | None = "enc::tok",
        platform: str = "sophos",
    ) -> None:
        self.id = id
        self.kind = kind
        self.parent_integration_id = parent_integration_id
        self.api_host = api_host
        self.region = region
        self.access_token = access_token
        self.platform = platform
        # Fields SophosProvider reads even when we mock it away
        self.name = "Test Tenant"
        self.external_id = "tenant-uuid"
        self.tenant_id = "tenant-uuid"
        self.id_type = "tenant"
        self.client_id = None
        self.client_secret = None
        self.refresh_token = None
        self.auth_status = None


def _mock_response(
    status_code: int, body: dict[str, Any], headers: dict[str, str] | None = None
) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = body
    resp.headers = headers or {}
    resp.raise_for_status = MagicMock()
    return resp


def _make_http_client_mock(responses: list[MagicMock]) -> MagicMock:
    """Return a context-manager-compatible httpx.Client mock."""
    client = MagicMock()
    client.get = MagicMock(side_effect=responses)
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=client)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def _fake_headers() -> dict[str, str]:
    return {"Authorization": "Bearer tok", "X-Tenant-ID": "tenant-uuid"}


def _sample_license_payload(
    product_code: str = "CIXAXDR",
    product_name: str = "Sophos XDR - User",
    quantity: int = 2000,
    usage_count: int | None = 1786,
) -> dict[str, Any]:
    """Build a single license dict shaped like the real Sophos response."""
    return {
        "id": "WPT6-WJ3H-T9CF-KJKL",
        "licenseIdentifier": "D590962015",
        "product": {"code": product_code, "name": product_name},
        "startDate": "2026-01-18",
        "endDate": "2027-01-17",
        "perpetual": False,
        "type": "enterprise",
        "quantity": quantity,
        "unlimited": False,
        "usage": (
            {"current": {"count": usage_count, "date": "2026-05-26"}}
            if usage_count is not None
            else None
        ),
    }


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def integration() -> _FakeIntegration:
    return _FakeIntegration()


@pytest.fixture()
def fake_redis_client() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis(decode_responses=True)


# ── Tests: happy path + normalization ──────────────────────────────────


def test_happy_path_returns_normalized_licenses(
    integration: _FakeIntegration,
) -> None:
    """200 with two licenses → returns normalized list with category and details."""
    payload = {
        "tenant": {"id": "tenant-uuid"},
        "licenses": [
            _sample_license_payload("CIXAXDR", "Sophos XDR - User"),
            _sample_license_payload(
                "CW7-SUP",
                "Sophos Endpoint for Legacy Platforms",
                quantity=499,
                usage_count=63,
            ),
        ],
    }
    resp = _mock_response(200, payload)
    cm = _make_http_client_mock([resp])

    with (
        patch(f"{_MOD}.SophosProvider") as MockProvider,
        patch(f"{_MOD}.httpx.Client", return_value=cm),
        patch(f"{_MOD}._get_redis", return_value=None),
    ):
        MockProvider.return_value._ensure_valid_token.return_value = _fake_headers()
        from backend.app.providers.sophos.licensing import fetch_licenses

        result = fetch_licenses(integration)

    assert len(result) == 2

    xdr = result[0]
    assert xdr["code"] == "CIXAXDR"
    assert xdr["label"] == "Sophos XDR - User"
    assert xdr["category"] == "xdr"
    assert xdr["details"]["quantity"] == 2000
    assert xdr["details"]["usageCount"] == 1786
    assert xdr["details"]["type"] == "enterprise"
    assert xdr["details"]["startDate"] == "2026-01-18"
    assert xdr["details"]["endDate"] == "2027-01-17"
    assert xdr["details"]["licenseIdentifier"] == "D590962015"

    legacy = result[1]
    assert legacy["code"] == "CW7-SUP"
    assert legacy["label"] == "Sophos Endpoint for Legacy Platforms"
    assert legacy["category"] is None  # neither XDR nor MDR
    assert legacy["details"]["quantity"] == 499


def test_uses_global_host_not_regional(integration: _FakeIntegration) -> None:
    """The endpoint MUST be api.central.sophos.com (not api-{region}.central…)."""
    resp = _mock_response(200, {"licenses": []})
    cm = _make_http_client_mock([resp])

    with (
        patch(f"{_MOD}.SophosProvider") as MockProvider,
        patch(f"{_MOD}.httpx.Client", return_value=cm) as MockClient,
        patch(f"{_MOD}._get_redis", return_value=None),
    ):
        MockProvider.return_value._ensure_valid_token.return_value = _fake_headers()
        from backend.app.providers.sophos.licensing import fetch_licenses

        fetch_licenses(integration)

    # Inspect the URL used in client.get(...)
    client_instance = cm.__enter__.return_value
    args, _ = client_instance.get.call_args
    assert args[0] == "https://api.central.sophos.com/licenses/v1/licenses"
    # And the timeout-bearing Client was constructed
    MockClient.assert_called()


@pytest.mark.parametrize(
    "code,name,expected_category",
    [
        ("CIXAXDR", "Sophos XDR - User", "xdr"),
        ("SVRCIXAXDR", "Sophos XDR - Server", "xdr"),
        ("XDR-MSP", "Sophos XDR - MSP Monthly", "xdr"),
        ("CIXAMTR-ADV-MSP", "Sophos MDR Advanced - MSP", "mdr"),
        ("MDR-COMPLETE", "Sophos MDR Complete", "mdr"),
        ("CIXA-MSP", "Sophos Endpoint - User MSP Monthly", None),
        ("SVRCLOUDADV-MSP", "Sophos Endpoint - Server MSP Monthly", None),
        ("CEMA-MSP", "Sophos Email MSP Monthly", None),
    ],
)
def test_categorization(
    integration: _FakeIntegration,
    code: str,
    name: str,
    expected_category: str | None,
) -> None:
    """XDR/MDR substring detection on code+name; everything else is None."""
    payload = {
        "tenant": {"id": "tenant-uuid"},
        "licenses": [_sample_license_payload(code, name)],
    }
    resp = _mock_response(200, payload)
    cm = _make_http_client_mock([resp])

    with (
        patch(f"{_MOD}.SophosProvider") as MockProvider,
        patch(f"{_MOD}.httpx.Client", return_value=cm),
        patch(f"{_MOD}._get_redis", return_value=None),
    ):
        MockProvider.return_value._ensure_valid_token.return_value = _fake_headers()
        from backend.app.providers.sophos.licensing import fetch_licenses

        result = fetch_licenses(integration)

    assert result[0]["category"] == expected_category


def test_license_without_usage_section(integration: _FakeIntegration) -> None:
    """A license missing the ``usage`` block → usageCount is None, not a crash."""
    payload = {
        "tenant": {"id": "tenant-uuid"},
        "licenses": [
            _sample_license_payload("COPX-MSP", "Cloud Optix MSP", usage_count=None),
        ],
    }
    resp = _mock_response(200, payload)
    cm = _make_http_client_mock([resp])

    with (
        patch(f"{_MOD}.SophosProvider") as MockProvider,
        patch(f"{_MOD}.httpx.Client", return_value=cm),
        patch(f"{_MOD}._get_redis", return_value=None),
    ):
        MockProvider.return_value._ensure_valid_token.return_value = _fake_headers()
        from backend.app.providers.sophos.licensing import fetch_licenses

        result = fetch_licenses(integration)

    assert result[0]["details"]["usageCount"] is None


def test_empty_licenses_list(integration: _FakeIntegration) -> None:
    """200 with ``licenses: []`` → returns []."""
    resp = _mock_response(200, {"tenant": {"id": "tenant-uuid"}, "licenses": []})
    cm = _make_http_client_mock([resp])

    with (
        patch(f"{_MOD}.SophosProvider") as MockProvider,
        patch(f"{_MOD}.httpx.Client", return_value=cm),
        patch(f"{_MOD}._get_redis", return_value=None),
    ):
        MockProvider.return_value._ensure_valid_token.return_value = _fake_headers()
        from backend.app.providers.sophos.licensing import fetch_licenses

        result = fetch_licenses(integration)

    assert result == []


# ── Tests: 401 / 403 / 429 / errors ────────────────────────────────────


def test_403_returns_empty_no_exception(
    integration: _FakeIntegration, caplog: pytest.LogCaptureFixture
) -> None:
    """403 → logger.warning called, returns [], no exception raised."""
    resp = _mock_response(403, {})
    cm = _make_http_client_mock([resp])

    with (
        patch(f"{_MOD}.SophosProvider") as MockProvider,
        patch(f"{_MOD}.httpx.Client", return_value=cm),
        patch(f"{_MOD}._get_redis", return_value=None),
        caplog.at_level("WARNING", logger=_MOD),
    ):
        MockProvider.return_value._ensure_valid_token.return_value = _fake_headers()
        from backend.app.providers.sophos import licensing as lic_mod

        result = lic_mod.fetch_licenses(integration)

    assert result == []
    assert any("403" in r.message for r in caplog.records)


def test_401_calls_on_401_once_then_retries(integration: _FakeIntegration) -> None:
    """401 → _on_401 called exactly once, retry with new headers succeeds."""
    unauth = _mock_response(401, {})
    success = _mock_response(
        200,
        {
            "tenant": {"id": "tenant-uuid"},
            "licenses": [_sample_license_payload("CIXAXDR", "Sophos XDR - User")],
        },
    )

    cm_1 = _make_http_client_mock([unauth])
    cm_2 = _make_http_client_mock([success])
    call_count = {"n": 0}

    def _client_factory(**_kwargs: Any) -> MagicMock:
        call_count["n"] += 1
        return cm_1 if call_count["n"] == 1 else cm_2

    fresh_headers = {"Authorization": "Bearer newtoken", "X-Tenant-ID": "tenant-uuid"}

    with (
        patch(f"{_MOD}.SophosProvider") as MockProvider,
        patch(f"{_MOD}.httpx.Client", side_effect=_client_factory),
        patch(f"{_MOD}._get_redis", return_value=None),
    ):
        instance = MockProvider.return_value
        instance._ensure_valid_token.return_value = _fake_headers()
        instance._on_401.return_value = fresh_headers

        from backend.app.providers.sophos import licensing as lic_mod

        result = lic_mod.fetch_licenses(integration)

    instance._on_401.assert_called_once()
    assert len(result) == 1
    assert result[0]["code"] == "CIXAXDR"


def test_429_short_retry_after_succeeds_on_retry(
    integration: _FakeIntegration,
) -> None:
    """429 + Retry-After=1s → sleep, second call succeeds."""
    rate_limited = _mock_response(429, {}, headers={"Retry-After": "1"})
    success = _mock_response(
        200,
        {
            "tenant": {"id": "tenant-uuid"},
            "licenses": [_sample_license_payload("CIXAXDR", "Sophos XDR - User")],
        },
    )

    cm_1 = _make_http_client_mock([rate_limited])
    cm_2 = _make_http_client_mock([success])
    call_count = {"n": 0}

    def _client_factory(**_kwargs: Any) -> MagicMock:
        call_count["n"] += 1
        return cm_1 if call_count["n"] == 1 else cm_2

    with (
        patch(f"{_MOD}.SophosProvider") as MockProvider,
        patch(f"{_MOD}.httpx.Client", side_effect=_client_factory),
        patch(f"{_MOD}._get_redis", return_value=None),
        patch(f"{_MOD}.time.sleep") as mock_sleep,
    ):
        MockProvider.return_value._ensure_valid_token.return_value = _fake_headers()
        from backend.app.providers.sophos import licensing as lic_mod

        result = lic_mod.fetch_licenses(integration)

    mock_sleep.assert_called_once_with(1.0)
    assert len(result) == 1
    assert result[0]["code"] == "CIXAXDR"


def test_429_persistent_returns_empty(
    integration: _FakeIntegration, caplog: pytest.LogCaptureFixture
) -> None:
    """429 → sleep → 429 again → returns []."""
    r1 = _mock_response(429, {}, headers={"Retry-After": "2"})
    r2 = _mock_response(429, {}, headers={"Retry-After": "5"})

    cm_1 = _make_http_client_mock([r1])
    cm_2 = _make_http_client_mock([r2])
    call_count = {"n": 0}

    def _client_factory(**_kwargs: Any) -> MagicMock:
        call_count["n"] += 1
        return cm_1 if call_count["n"] == 1 else cm_2

    with (
        patch(f"{_MOD}.SophosProvider") as MockProvider,
        patch(f"{_MOD}.httpx.Client", side_effect=_client_factory),
        patch(f"{_MOD}._get_redis", return_value=None),
        patch(f"{_MOD}.time.sleep"),
        caplog.at_level("WARNING", logger=_MOD),
    ):
        MockProvider.return_value._ensure_valid_token.return_value = _fake_headers()
        from backend.app.providers.sophos import licensing as lic_mod

        result = lic_mod.fetch_licenses(integration)

    assert result == []
    assert call_count["n"] == 2  # original + 1 retry, no fallback
    assert any("429" in r.message and "after retry" in r.message for r in caplog.records)


def test_429_long_retry_after_returns_empty_no_sleep(
    integration: _FakeIntegration,
) -> None:
    """429 with Retry-After above cap → no sleep, no retry, returns []."""
    rate_limited = _mock_response(429, {}, headers={"Retry-After": "60"})
    cm = _make_http_client_mock([rate_limited])

    with (
        patch(f"{_MOD}.SophosProvider") as MockProvider,
        patch(f"{_MOD}.httpx.Client", return_value=cm),
        patch(f"{_MOD}._get_redis", return_value=None),
        patch(f"{_MOD}.time.sleep") as mock_sleep,
    ):
        MockProvider.return_value._ensure_valid_token.return_value = _fake_headers()
        from backend.app.providers.sophos import licensing as lic_mod

        result = lic_mod.fetch_licenses(integration)

    mock_sleep.assert_not_called()
    assert result == []


# ── Tests: cache ──────────────────────────────────────────────────────


def test_cache_hit_skips_api(
    integration: _FakeIntegration, fake_redis_client: fakeredis.FakeRedis
) -> None:
    """Cached result is returned without hitting the API on the second call."""
    cached_payload = [
        {
            "code": "CIXAXDR",
            "label": "Sophos XDR - User",
            "category": "xdr",
            "details": {"type": "enterprise"},
        }
    ]
    key = f"sophos:licenses:{integration.id}"
    fake_redis_client.setex(key, 300, json.dumps(cached_payload))

    with (
        patch(f"{_MOD}.SophosProvider") as MockProvider,
        patch(f"{_MOD}.httpx.Client") as MockHttpx,
        patch(f"{_MOD}._get_redis", return_value=fake_redis_client),
    ):
        from backend.app.providers.sophos import licensing as lic_mod

        result = lic_mod.fetch_licenses(integration)

    MockHttpx.assert_not_called()
    MockProvider.assert_not_called()
    assert result == cached_payload


def test_cache_populated_after_live_fetch(
    integration: _FakeIntegration, fake_redis_client: fakeredis.FakeRedis
) -> None:
    """After a live fetch, result is written to Redis with the 6h TTL."""
    payload = {
        "tenant": {"id": "tenant-uuid"},
        "licenses": [_sample_license_payload("CIXAXDR", "Sophos XDR - User")],
    }
    resp = _mock_response(200, payload)
    cm = _make_http_client_mock([resp])

    with (
        patch(f"{_MOD}.SophosProvider") as MockProvider,
        patch(f"{_MOD}.httpx.Client", return_value=cm),
        patch(f"{_MOD}._get_redis", return_value=fake_redis_client),
    ):
        MockProvider.return_value._ensure_valid_token.return_value = _fake_headers()
        from backend.app.providers.sophos import licensing as lic_mod

        result = lic_mod.fetch_licenses(integration)

    key = f"sophos:licenses:{integration.id}"
    cached_raw = fake_redis_client.get(key)
    assert cached_raw is not None
    cached = json.loads(cached_raw)
    assert cached == result
    ttl = fake_redis_client.ttl(key)
    assert 0 < ttl <= 21_600


# ── Tests: misc helpers ───────────────────────────────────────────────


def test_parse_retry_after_handles_seconds_and_http_date() -> None:
    """Numeric seconds, HTTP date, invalid → covers all paths."""
    from backend.app.providers.sophos.licensing import _parse_retry_after

    assert _parse_retry_after("5") == 5.0
    assert _parse_retry_after("0") == 0.0
    assert _parse_retry_after(None) == 1.0
    assert _parse_retry_after("") == 1.0
    assert _parse_retry_after("not-a-number") == float("inf")
    assert _parse_retry_after("Mon, 01 Jan 2001 00:00:00 GMT") == 0.0


def test_not_child_tenant_raises(integration: _FakeIntegration) -> None:
    """Partner/Organization or standalone tenant raises RuntimeError."""
    from backend.app.providers.sophos.licensing import fetch_licenses

    standalone = _FakeIntegration(parent_integration_id=None)
    with pytest.raises(RuntimeError, match="child tenant"):
        fetch_licenses(standalone)

    partner = _FakeIntegration(kind="partner", parent_integration_id=None)
    with pytest.raises(RuntimeError, match="child tenant"):
        fetch_licenses(partner)
