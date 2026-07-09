"""Service for Sophos XDR Query API (``/xdr-query/v1``).

Replaces the deprecated XDR Search API.  All SQL queries are executed
via ``POST /queries/runs`` and results are fetched with automatic
pagination.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Dict, List, Optional

import httpx

from ..core.config import settings
from ..core.rate_limiter import query_run_limiter, rate_limiter
from .sophos_errors import parse_sophos_error, raise_sophos_api_error

logger = logging.getLogger(__name__)


class XDRQueryService:
    """Interact with the Sophos XDR Query API for a single tenant."""

    def __init__(
        self,
        region: str,
        headers: Dict[str, str],
        tenant_id: str,
        on_401: Optional[Callable[[], Dict[str, str]]] = None,
        *,
        api_host: Optional[str] = None,
    ) -> None:
        """Build the service.

        ``api_host`` (Partner-managed integrations) takes precedence over
        ``region``. When neither is a valid datacenter slug, raises
        :class:`MissingApiHostError`.

        Background: Sophos returns ``apiHost`` (datacenter slug like
        ``api-eu03.central.sophos.com``) in ``/partner/v1/tenants``.
        ``dataGeography`` is a geo-code (``EU``/``US``) that does NOT
        resolve as a hostname — so we must NOT derive ``api-{region}``
        when the value is a geo-code.
        """
        from ..collectors.vendors._sophos_common import resolve_sophos_domain

        self.region = region
        self.api_host = api_host
        host = resolve_sophos_domain(api_host=api_host, region=region)
        self.base_url = f"https://{host}/xdr-query/v1"
        self.headers = dict(headers)
        self.tenant_id = tenant_id
        self._on_401 = on_401
        self._client = httpx.Client(timeout=httpx.Timeout(30.0, read=120.0))

    def close(self) -> None:
        self._client.close()

    # ── internal helpers ──────────────────────────────────────────────

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        resp = self._client.request(method, url, headers=self.headers, **kwargs)

        # Handle 429 – respect Retry-After
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "5"))
            rate_limiter.handle_429(self.tenant_id, retry_after)
            logger.warning("XDR Query: 429 received, will wait %ds", retry_after)
            time.sleep(retry_after)
            resp = self._client.request(method, url, headers=self.headers, **kwargs)

        # Handle 401 – try to refresh token
        if resp.status_code == 401 and self._on_401:
            logger.info("XDR Query: 401 received, attempting token refresh")
            new_headers = self._on_401()
            self.headers.update(new_headers)
            resp = self._client.request(method, url, headers=self.headers, **kwargs)

        return resp

    # ── public API ────────────────────────────────────────────────────

    def run_query(self, sql: str, from_ts: str, to_ts: str) -> Dict[str, Any]:
        """Submit an ad-hoc SQL query and return the run metadata.

        Returns dict with at least ``{"id": "<run_id>", "status": "..."}``.
        """
        payload = {
            "adHocQuery": {"template": sql},
            "from": from_ts,
            "to": to_ts,
        }
        logger.info("XDR Query: POST %s/queries/runs", self.base_url)

        resp = self._request("POST", f"{self.base_url}/queries/runs", json=payload)

        if resp.status_code not in (200, 201):
            parsed = parse_sophos_error(resp)
            # Hint detalhado vai SÓ para o log do operador. A imagem é pública —
            # o cliente HTTP recebe apenas a mensagem genérica da Sophos para
            # não expor detalhes de role/entitlement como roadmap para um
            # atacante que esteja testando credenciais roubadas.
            log_hint: str | None = None
            message_l = (parsed.message or "").lower()
            if resp.status_code == 403 and parsed.error == "UNAUTHORIZED_ACCESS":
                log_hint = (
                    "Sophos API Credential role insufficient for XDR Query. "
                    "Required: 'Service Principal Forensics' or 'Service Principal Super Admin'. "
                    "Roles 'Read-Only' and 'Management' are explicitly blocked by Sophos from "
                    "running queries (per Sophos Central docs). Fix in Sophos Central → "
                    "Settings → API Credentials. Also confirm this tenant has the "
                    "Sophos XDR/Data Lake product licensed."
                )
            elif (
                resp.status_code == 400
                and "no accounts found for request" in message_l
                and "xdr_data=[xdr_endpoint]" in message_l
            ):
                log_hint = (
                    "Tenant is authenticated, but no XDR Data Lake account was found for the "
                    "xdr_endpoint dataset in this region. Re-authenticate the client and verify "
                    "XDR/Data Lake entitlement plus Endpoint data source availability for this tenant."
                )
            logger.error(
                "XDR Query: run_query failed %d: %s | hint=%s",
                resp.status_code, resp.text, log_hint or "(none)",
            )
            raise_sophos_api_error("run_query", resp, hint=None)

        return resp.json()

    def get_run_status(self, run_id: str) -> Dict[str, Any]:
        """GET the current status of a query run."""
        resp = self._request("GET", f"{self.base_url}/queries/runs/{run_id}")
        if not resp.is_success:
            raise_sophos_api_error("get_run_status", resp)
        return resp.json()

    def get_results(self, run_id: str, page_size: int = 50) -> List[Dict[str, Any]]:
        """Fetch **all** result pages for a finished query run.

        The XDR Query results endpoint is paged. According to the API contract
        used in the bundled Postman collection, pagination is controlled by the
        ``pageSize`` and ``pageFromKey`` query parameters and the next cursor is
        returned in ``pages.nextKey``.
        """
        all_items: List[Dict[str, Any]] = []
        url = f"{self.base_url}/queries/runs/{run_id}/results"
        try:
            normalized_page_size = max(1, min(int(page_size), 50))
        except (TypeError, ValueError):
            normalized_page_size = 50
        params: Dict[str, Any] = {"pageSize": normalized_page_size}
        seen_page_keys: set[str] = set()

        while True:
            resp = self._request("GET", url, params=params)
            if not resp.is_success:
                raise_sophos_api_error("get_results", resp)
            data = resp.json()

            items = data.get("items", [])
            if not isinstance(items, list):
                items = []
            all_items.extend(items)

            pages = data.get("pages") or {}
            next_key = pages.get("nextKey")
            if not next_key:
                break
            if next_key in seen_page_keys:
                logger.warning(
                    "XDR Query: repeated page cursor %s detected for run %s, stopping pagination",
                    next_key,
                    run_id,
                )
                break
            seen_page_keys.add(next_key)
            params["pageFromKey"] = next_key

        logger.info("XDR Query: fetched %d items for run %s", len(all_items), run_id)
        return all_items

    def wait_and_fetch(
        self,
        run_id: str,
        poll_interval: int | None = None,
        timeout: int | None = None,
    ) -> Dict[str, Any]:
        """Poll until the query run finishes, then return items.

        Returns ``{"items": [...], "status": "finished", "run_id": "..."}``.

        Raises ``TimeoutError`` if the run does not finish in time.
        Raises ``RuntimeError`` if the run fails/is cancelled.
        """
        interval = poll_interval or settings.QUERY_POLL_INTERVAL
        max_wait = timeout or settings.QUERY_POLL_TIMEOUT

        start = time.monotonic()
        while time.monotonic() - start <= max_wait:
            status_data = self.get_run_status(run_id)
            status = status_data.get("status", "")
            result = status_data.get("result", "")

            if status == "finished" and result == "succeeded":
                items = self.get_results(run_id)
                return {"items": items, "status": "finished", "run_id": run_id}

            if status in {"failed", "cancelled", "canceled"}:
                error = status_data.get("error", "unknown error")
                raise RuntimeError(
                    f"Query run {run_id} ended with status={status}: {error}"
                )

            time.sleep(interval)

        raise TimeoutError(f"Query run {run_id} did not finish within {max_wait}s")


class AsyncXDRQueryService:
    """Async wrapper around :class:`XDRQueryService` for use in FastAPI routes."""

    def __init__(
        self,
        region: str,
        headers: Dict[str, str],
        tenant_id: str,
        on_401: Optional[Callable[[], Dict[str, str]]] = None,
        *,
        api_host: Optional[str] = None,
    ) -> None:
        self._sync = XDRQueryService(
            region, headers, tenant_id, on_401, api_host=api_host
        )

    def close(self) -> None:
        self._sync.close()

    async def run_query(self, sql: str, from_ts: str, to_ts: str) -> Dict[str, Any]:
        await rate_limiter.acquire(self._sync.tenant_id)
        await query_run_limiter.acquire(self._sync.tenant_id)
        return await asyncio.to_thread(self._sync.run_query, sql, from_ts, to_ts)

    async def get_run_status(self, run_id: str) -> Dict[str, Any]:
        await rate_limiter.acquire(self._sync.tenant_id)
        return await asyncio.to_thread(self._sync.get_run_status, run_id)

    async def get_results(self, run_id: str, page_size: int = 50) -> List[Dict[str, Any]]:
        await rate_limiter.acquire(self._sync.tenant_id)
        return await asyncio.to_thread(self._sync.get_results, run_id, page_size)

    async def wait_and_fetch(self, run_id: str) -> Dict[str, Any]:
        return await asyncio.to_thread(self._sync.wait_and_fetch, run_id)
