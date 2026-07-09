"""Sophos Central OAuth2 authentication service.

Supports three Sophos identity tiers:
  * ``tenant``       — single Sophos Central tenant (legacy default)
  * ``organization`` — Sophos Organization that owns multiple tenants
  * ``partner``      — Sophos Partner that owns multiple Organizations / tenants

The Partner/Organization tiers are reached with the same OAuth2 client_credentials
flow; the API path used to enumerate tenants is the only thing that changes.
Reference: ``IASOC/MCPs/sophos-central-mcp-main/src/client/tenant-resolver.ts``.
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, Tuple

import httpx


class SophosAuthService:
    TOKEN_URL = "https://id.sophos.com/api/v2/oauth2/token"
    WHOAMI_URL = "https://api.central.sophos.com/whoami/v1"
    GLOBAL_API = "https://api.central.sophos.com"

    # Sane defaults for tenant pagination. ``pageSize`` of 100 is the documented
    # max for /partner/v1/tenants; we default to 100 to minimise round-trips.
    DEFAULT_PAGE_SIZE = 100

    def __init__(self, client_id: str, client_secret: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self._http = httpx.Client(timeout=30.0)

    # ── OAuth2 ──────────────────────────────────────────────────────────

    def authenticate(self) -> Dict[str, str]:
        payload = {
            "grant_type": "client_credentials",
            "scope": "token",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        response = self._http.post(self.TOKEN_URL, data=payload, headers=headers)
        response.raise_for_status()
        return response.json()

    def refresh(self, refresh_token: str) -> Dict[str, str]:
        payload = {
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": refresh_token,
        }
        response = self._http.post(self.TOKEN_URL, json=payload)
        response.raise_for_status()
        return response.json()

    # ── Identity discovery (whoami) ────────────────────────────────────

    def _whoami(self, access_token: str) -> Dict[str, Any]:
        """Internal helper that performs ``GET /whoami/v1``.

        Returns the raw JSON body. Used by ``discover_*`` methods so callers
        that need both region+tenant+id_type in one shot only round-trip once.
        """
        headers = {"Authorization": f"Bearer {access_token}"}
        response = self._http.get(self.WHOAMI_URL, headers=headers)
        response.raise_for_status()
        return response.json()

    def discover_identity(self, access_token: str) -> Dict[str, Any]:
        """Return ``{id, id_type, api_hosts}`` from /whoami/v1.

        ``id_type`` ∈ {``"tenant"``, ``"organization"``, ``"partner"``}.
        ``api_hosts`` mirrors Sophos' shape: ``{"global": ..., "dataRegion": ...?}``.
        ``dataRegion`` is only present for ``tenant`` callers.
        """
        data = self._whoami(access_token)
        return {
            "id": data.get("id", ""),
            "id_type": data.get("idType", ""),
            "api_hosts": data.get("apiHosts", {}) or {},
        }

    def discover_region(self, access_token: str) -> str:
        data = self._whoami(access_token)
        url = data.get("apiHosts", {}).get("dataRegion", "")
        return self._extract_region(url)

    @staticmethod
    def _extract_region(url: str) -> str:
        prefix = "https://api-"
        suffix = ".central.sophos.com"
        if url.startswith(prefix) and url.endswith(suffix):
            return url[len(prefix) : -len(suffix)]
        return url

    def discover_tenant_id(self, access_token: str) -> str:
        data = self._whoami(access_token)
        return data.get("id", "")

    def discover_region_and_tenant(self, access_token: str) -> Tuple[str, str]:
        data = self._whoami(access_token)
        url = data.get("apiHosts", {}).get("dataRegion", "")
        region = self._extract_region(url)
        tenant_id = data.get("id", "")
        return region, tenant_id

    # ── Partner / Organization tenant enumeration ─────────────────────

    def _list_tenants_paginated(
        self,
        *,
        access_token: str,
        api_path: str,
        id_header_name: str,
        id_header_value: str,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> Iterator[Dict[str, Any]]:
        """Yield tenants from a paginated Sophos listing endpoint.

        Sophos returns ``{"items": [...], "pages": {"current", "total", "size", "items"}}``.
        We request ``pageTotal=true`` only on the first page to learn the
        page count, then loop. Each yielded item is the raw tenant dict
        ({id, name, dataRegion, dataGeography, apiHost, ...}).
        """
        page = 1
        total_pages = 1
        while page <= total_pages:
            url = f"{self.GLOBAL_API}{api_path}"
            params: Dict[str, Any] = {
                "page": page,
                "pageSize": page_size,
            }
            if page == 1:
                params["pageTotal"] = "true"
            headers = {
                "Authorization": f"Bearer {access_token}",
                id_header_name: id_header_value,
                "Accept": "application/json",
            }
            response = self._http.get(url, params=params, headers=headers)
            response.raise_for_status()
            data = response.json() or {}
            if page == 1:
                pages = data.get("pages") or {}
                # ``total`` is an int. Defensive: fall back to 1 if missing.
                total_pages = int(pages.get("total") or 1)
            for item in data.get("items") or []:
                yield item
            page += 1

    def list_partner_tenants(
        self,
        access_token: str,
        partner_id: str,
        *,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> Iterator[Dict[str, Any]]:
        """Iterate tenants under a Partner.

        Calls ``GET /partner/v1/tenants`` with ``X-Partner-ID``. ``page_size``
        capped at 100 by Sophos (we keep the default).
        """
        if not partner_id:
            raise ValueError("partner_id is required")
        return self._list_tenants_paginated(
            access_token=access_token,
            api_path="/partner/v1/tenants",
            id_header_name="X-Partner-ID",
            id_header_value=partner_id,
            page_size=page_size,
        )

    def list_organization_tenants(
        self,
        access_token: str,
        organization_id: str,
        *,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> Iterator[Dict[str, Any]]:
        """Iterate tenants under an Organization.

        Calls ``GET /organization/v1/tenants`` with ``X-Organization-ID``.
        """
        if not organization_id:
            raise ValueError("organization_id is required")
        return self._list_tenants_paginated(
            access_token=access_token,
            api_path="/organization/v1/tenants",
            id_header_name="X-Organization-ID",
            id_header_value=organization_id,
            page_size=page_size,
        )

    def discover_tenants(
        self,
        access_token: str,
        identity: Dict[str, Any],
    ) -> Iterator[Dict[str, Any]]:
        """Dispatch to the right enumeration endpoint based on ``identity.id_type``.

        For ``id_type="tenant"`` returns a single-element iterator with the
        caller's own identity (no enumeration is possible — they ARE the tenant).
        For ``"partner"`` / ``"organization"`` paginates the respective endpoint.
        """
        id_type = identity.get("id_type", "")
        identity_id = identity.get("id", "")
        if id_type == "partner":
            return self.list_partner_tenants(access_token, identity_id)
        if id_type == "organization":
            return self.list_organization_tenants(access_token, identity_id)
        if id_type == "tenant":
            # Single self-tenant — synthesise a single yield with the data we have.
            api_hosts = identity.get("api_hosts", {}) or {}
            data_region_url = api_hosts.get("dataRegion", "")
            return iter([
                {
                    "id": identity_id,
                    "name": "self",
                    "apiHost": data_region_url,
                    "dataRegion": self._extract_region(data_region_url),
                    "dataGeography": "",
                }
            ])
        raise ValueError(f"unknown Sophos id_type: {id_type!r}")

    def close(self) -> None:
        """Close the underlying HTTP client. Safe to call multiple times."""
        try:
            self._http.close()
        except Exception:  # noqa: BLE001
            pass
