"""Sophos Central — fetch licensed products for a child tenant.

Source: ``GET https://api.central.sophos.com/licenses/v1/licenses``
(global host, NOT the regional api-{region} host).  Confirmed against
the official Sophos Postman collection (``docs/Sophos Central
APIs.postman_collection.json``, "Licensing API > Licensing > Get
licenses") and validated empirically on real Partner-managed tenants.

Auth: Partner OAuth (``Authorization: Bearer …``) + ``X-Tenant-ID``
header scoped to the child tenant.

Returned shape (per element of the list):

.. code-block:: json

    {
      "code": "CIXAXDR",
      "label": "Sophos XDR - User",
      "category": "xdr",
      "details": {
        "type": "enterprise",
        "quantity": 2000,
        "unlimited": false,
        "perpetual": false,
        "startDate": "2026-01-18",
        "endDate": "2027-01-17",
        "usageCount": 1786,
        "licenseIdentifier": "D590962015"
      }
    }

``category`` is a coarse classification derived from product code and
name — only ``"xdr"``, ``"mdr"``, or ``None``.  This is what the UI uses
to render "the 403 in /detections is because XDR is not licensed" type
hints without dictating exact SKU semantics.

Endpoint behavior:

* ``200`` → list of licenses (may be empty for tenants with no SKUs)
* ``401`` → trigger reauth via the provider (handled internally, 1 retry)
* ``403`` → return ``[]`` (rare; caller treats as "no info available")
* ``429`` → respect ``Retry-After`` if ``<= _MAX_RETRY_AFTER_SECONDS``,
  otherwise return ``[]`` (no second retry — would just throttle worse)
* ``5xx`` and network errors → propagate to caller

Cache: Redis key ``sophos:licenses:{integration_id}`` with 6h TTL.
License changes are operational events (new SKU sold, renewal, churn),
not second-by-second signals, so 6h drastically reduces API calls while
keeping the UI usefully fresh.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from ...db.models import Integration
from .provider import SophosProvider

logger = logging.getLogger(__name__)

# Sophos Licensing API lives on the GLOBAL host, not the regional one.
# Regional hosts return HTTP 404 "Unable to identify proxy for host: api-XXX".
_LICENSES_URL = "https://api.central.sophos.com/licenses/v1/licenses"

_CACHE_TTL_SECONDS = 21_600  # 6h — licenças mudam em escala operacional, não em segundos
_MAX_RETRY_AFTER_SECONDS = 10  # cap: não bloqueia a request thread por muito tempo

# Tokens we look for in product code/name to surface "Detections/XDR/MDR
# is licensed?" cleanly in the UI.  Frugal on purpose — matching by SKU
# prefix would couple us to Sophos' internal naming and break whenever
# they introduce new families.
_XDR_TOKENS = ("XDR",)
_MDR_TOKENS = ("MDR", "MTR")


def _cache_key(integration_id: int) -> str:
    return f"sophos:licenses:{integration_id}"


def _get_redis() -> Any | None:
    """Sync Redis client.  Returns ``None`` when Redis is unavailable."""
    try:
        import redis as _redis_sync

        from ...core.config import settings

        return _redis_sync.Redis.from_url(
            settings.REDIS_URL or "redis://localhost:6379/0",
            decode_responses=True,
        )
    except Exception:  # noqa: BLE001
        return None


def _parse_retry_after(value: str | None) -> float:
    """Parse the ``Retry-After`` header. Returns seconds.

    Sophos pode enviar como numero (segundos) ou HTTP date. Se ausente,
    assume 1s. Se inválido/sem parse, devolve ``inf`` (caller trata como
    'longo demais').
    """
    if not value:
        return 1.0
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        from datetime import datetime, timezone
        from email.utils import parsedate_to_datetime

        target = parsedate_to_datetime(value)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        delta = (target - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, delta)
    except Exception:  # noqa: BLE001
        return float("inf")


def _categorize(code: str, name: str) -> str | None:
    """Classify a license as ``"xdr"`` / ``"mdr"`` / ``None``.

    Substring match on the uppercase concatenation of code+name. This is
    intentionally generic — Sophos uses different prefixes (``CIXAXDR``,
    ``SVRCIXAXDR``, ``XDR-MSP``) but the substring ``XDR`` always shows
    up in either field for true XDR SKUs.
    """
    haystack = f"{code} {name or ''}".upper()
    if any(tok in haystack for tok in _XDR_TOKENS):
        return "xdr"
    if any(tok in haystack for tok in _MDR_TOKENS):
        return "mdr"
    return None


def _normalize(licenses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize the raw Sophos response to our internal shape."""
    result: list[dict[str, Any]] = []
    for lic in licenses:
        product = lic.get("product") or {}
        code = product.get("code") or ""
        name = product.get("name") or ""
        usage_current = ((lic.get("usage") or {}).get("current")) or {}
        result.append(
            {
                "code": code,
                "label": name or code,
                "category": _categorize(code, name),
                "details": {
                    "type": lic.get("type"),
                    "quantity": lic.get("quantity"),
                    "unlimited": bool(lic.get("unlimited", False)),
                    "perpetual": bool(lic.get("perpetual", False)),
                    "startDate": lic.get("startDate"),
                    "endDate": lic.get("endDate"),
                    "usageCount": usage_current.get("count"),
                    "licenseIdentifier": lic.get("licenseIdentifier"),
                },
            }
        )
    return result


def _do_get(headers: dict[str, str]) -> httpx.Response:
    """Single HTTP GET to the licenses endpoint."""
    with httpx.Client(timeout=15.0) as client:
        return client.get(_LICENSES_URL, headers=headers)


def _fetch_with_429_handling(
    headers: dict[str, str],
) -> tuple[httpx.Response | None, bool]:
    """Call the endpoint, handling 429 with one short-sleep retry.

    Returns ``(response, give_up)`` where ``give_up=True`` means the caller
    should return ``[]`` immediately (persistent 429 or Retry-After too
    high). ``response is None`` only when ``give_up=True``.
    """
    resp = _do_get(headers)
    if resp.status_code != 429:
        return resp, False

    retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
    if retry_after > _MAX_RETRY_AFTER_SECONDS:
        logger.warning(
            "sophos:licenses: 429 with Retry-After=%.1fs (>%ds) — returning []",
            retry_after, _MAX_RETRY_AFTER_SECONDS,
        )
        return None, True

    logger.warning(
        "sophos:licenses: 429, sleeping %.1fs and retrying once",
        retry_after,
    )
    time.sleep(retry_after)
    resp = _do_get(headers)
    if resp.status_code == 429:
        logger.warning("sophos:licenses: 429 after retry — returning []")
        return None, True
    return resp, False


def fetch_licenses(integration: Integration) -> list[dict[str, Any]]:
    """Return the licensed Sophos products for a child tenant.

    Intended only for ``kind="tenant"`` + ``parent_integration_id IS NOT
    NULL``.  Callers SHOULD check this precondition; the function
    enforces it as ``RuntimeError`` to surface programming mistakes.

    On 5xx / network error the exception propagates — the caller (overview
    endpoint) decides whether to swallow it.  On 401, ``_on_401`` is
    consulted exactly once for reauth.  On 403 / persistent 429 we return
    ``[]`` so the UI shows "no license info" instead of blowing up.
    """
    if integration.kind != "tenant" or not integration.parent_integration_id:
        raise RuntimeError(
            f"fetch_licenses requires a child tenant integration "
            f"(got kind={integration.kind!r}, "
            f"parent_integration_id={integration.parent_integration_id!r})"
        )

    redis = _get_redis()
    key = _cache_key(integration.id)

    # ── Cache read ──
    if redis is not None:
        try:
            cached = redis.get(key)
            if cached is not None:
                return json.loads(cached)  # type: ignore[no-any-return]
        except Exception:  # noqa: BLE001
            pass  # degraded cache — proceed to live fetch

    provider = SophosProvider(integration)
    headers = provider._ensure_valid_token()

    # ── First attempt (with 429 handling) ──
    resp, give_up = _fetch_with_429_handling(headers)
    if give_up:
        return []

    # ── 401 reauth retry ──
    assert resp is not None  # mypy: give_up=False implies resp is set
    if resp.status_code == 401:
        headers = provider._on_401()
        resp, give_up = _fetch_with_429_handling(headers)
        if give_up:
            return []

    assert resp is not None
    if resp.status_code == 403:
        logger.warning(
            "sophos:licenses: 403 for integration_id=%s — returning []",
            integration.id,
        )
        result: list[dict[str, Any]] = []
    else:
        resp.raise_for_status()
        data = resp.json()
        licenses = data.get("licenses") or []
        result = _normalize(licenses)

    # ── Cache write ──
    if redis is not None:
        try:
            redis.setex(key, _CACHE_TTL_SECONDS, json.dumps(result))
        except Exception:  # noqa: BLE001
            pass
        try:
            redis.close()
        except Exception:  # noqa: BLE001
            pass

    return result
