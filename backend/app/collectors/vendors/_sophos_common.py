"""Shared helpers for Sophos Central vendor collectors.

Currently exports ``MissingApiHostError`` + ``resolve_sophos_domain`` —
a strict resolver that prefers ``X-Api-Host`` (populated from
``integration.api_host`` by ``partner_sync_tasks``) and **fails loud**
when the only fallback would be a geo-code ``X-Region`` (``EU``/``US``
etc) that resolves to a non-existent DNS record.

Background — see also docstring in ``sophos.py``:

Sophos Central tenants live in named datacenters with slug hostnames
like ``api-eu03.central.sophos.com`` / ``api-us02.central.sophos.com``.
The slug format is ``XX##`` (lowercase 2-letter region + 2-digit DC index).
There is no ``api-EU.central.sophos.com`` or ``api-US.central.sophos.com``
record — the geo-code in ``dataGeography`` is a label, not a hostname.

Before this resolver, the collector blindly built ``f"api-{region}..."``
and produced NXDOMAIN when ``region`` carried a geo-code (which happened
because ``sync_sophos_partner`` had a separate Celery broker bug — see
diagnóstico Erro A — that prevented re-sync from repopulating ``api_host``
verbatim from the Sophos payload).

We now refuse to build a URL from a clearly-geo region. The operator gets
a structured error pointing to the integration that needs re-sync; the
collector does NOT spam DNS with bogus lookups.
"""

from __future__ import annotations

import logging
import re
from typing import Mapping, Optional

logger = logging.getLogger(__name__)


class MissingApiHostError(RuntimeError):
    """Raised when a Sophos collector has no resolvable host.

    This is a configuration/state error — the integration row in the DB
    needs ``api_host`` populated by re-running the partner sync (manually
    via ``POST /api/integrations/{id}/sync-tenants`` or by waiting for the
    daily Beat job). It is **not** transient: retrying without re-syncing
    will produce the same error.
    """

    def __init__(
        self,
        *,
        integration_id: Optional[int],
        region: Optional[str],
    ) -> None:
        self.integration_id = integration_id
        self.region = region
        message = (
            f"Sophos integration_id={integration_id} has no usable host: "
            f"X-Api-Host is empty and X-Region={region!r} looks like a "
            "geographic code (e.g. 'EU', 'US') rather than a datacenter "
            "slug (e.g. 'eu03', 'us02'). Re-run sync_sophos_partner to "
            "populate api_host from the Sophos /partner/v1/tenants payload."
        )
        super().__init__(message)


# Datacenter slug pattern: 2-letter region (lowercase) + 2-digit index.
# Reference: Sophos Central public docs list eu01/eu02/eu03 (Dublin),
# us01/us02/us03/us04 (Pennsylvania), de01 (Frankfurt), au01 (Sydney),
# in01 (Mumbai), ca01 (Canada), jp01 (Tokyo), br01 (São Paulo). The
# regex is permissive on digits to absorb future capacity additions.
_DATACENTER_SLUG_RE = re.compile(r"^[a-z]{2}\d{1,3}$")


def _looks_like_datacenter_slug(value: str) -> bool:
    return bool(_DATACENTER_SLUG_RE.match(value or ""))


def resolve_sophos_domain(
    headers: Mapping[str, str] | None = None,
    *,
    api_host: Optional[str] = None,
    region: Optional[str] = None,
    integration_id: Optional[int] = None,
) -> str:
    """Resolve the hostname for a Sophos Central API call.

    Resolution order:

    1. Explicit ``api_host`` kwarg (preferred) or ``X-Api-Host`` header
       (legacy). Used verbatim — Sophos guarantees this is the canonical
       host for the tenant, including post-failover/migration scenarios.
       Tolerates ``https://`` prefix; strips it.
    2. Explicit ``region`` kwarg or ``X-Region`` header (legacy). Only
       honored when the value looks like a real datacenter slug
       (``eu03``/``us02``/...). Geo codes (``EU``/``US``/...) raise
       ``MissingApiHostError`` — building ``api-EU.central.sophos.com``
       produces NXDOMAIN.

    Raises:
        MissingApiHostError: when neither ``api_host`` is set nor
            ``region`` is a recognizable datacenter slug.
    """
    explicit_host = (api_host or "").strip()
    if not explicit_host and headers is not None:
        explicit_host = (headers.get("X-Api-Host") or "").strip()
    if explicit_host:
        # Tolerate full URLs — caller may have stored ``https://api-...``.
        if explicit_host.startswith("https://"):
            explicit_host = explicit_host[len("https://"):]
        elif explicit_host.startswith("http://"):
            explicit_host = explicit_host[len("http://"):]
        return explicit_host.rstrip("/")

    region_raw = (region or "").strip()
    if not region_raw and headers is not None:
        region_raw = (headers.get("X-Region") or "").strip()
    if region_raw and _looks_like_datacenter_slug(region_raw):
        # Lower-case defensively — Sophos slugs are always lowercase but
        # legacy rows may have mixed case from manual entry.
        return f"api-{region_raw.lower()}.central.sophos.com"

    # Fail loud: refuse to spam DNS with a geo-code-derived URL that we
    # already know does not resolve. The operator must re-sync to get
    # ``api_host`` populated from the Sophos payload.
    raise MissingApiHostError(integration_id=integration_id, region=region_raw or None)
