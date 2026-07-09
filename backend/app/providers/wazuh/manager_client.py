"""HTTP client for the Wazuh Manager API (port 55000).

Authentication uses JWT via POST /security/user/authenticate.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

from ...core.url_policy import normalize_service_url

logger = logging.getLogger(__name__)


class WazuhManagerClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        verify_ssl: bool = True,
    ) -> None:
        normalized_url = normalize_service_url(base_url)
        if not normalized_url:
            raise ValueError("Wazuh Manager base URL is required")

        self.base_url = normalized_url.rstrip("/")
        self._username = username
        self._password = password
        self._token: Optional[str] = None
        self._http = httpx.Client(
            timeout=httpx.Timeout(30.0, read=60.0),
            verify=verify_ssl,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._http.close()

    # ── Authentication ────────────────────────────────────────────────

    def _authenticate(self) -> str:
        resp = self._http.post(
            f"{self.base_url}/security/user/authenticate",
            auth=(self._username, self._password),
        )
        resp.raise_for_status()
        data = resp.json()
        token = data.get("data", {}).get("token", "")
        if not token:
            raise RuntimeError("Wazuh Manager: no JWT token in authenticate response")
        self._token = token
        return token

    def _ensure_token(self) -> str:
        if not self._token:
            return self._authenticate()
        return self._token

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._ensure_token()}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = f"{self.base_url}{path}"
        resp = self._http.request(method, url, headers=self._headers(), **kwargs)

        # Handle 401 — re-authenticate and retry once
        if resp.status_code == 401:
            self._token = None
            resp = self._http.request(method, url, headers=self._headers(), **kwargs)

        resp.raise_for_status()
        return resp

    # ── Manager info ──────────────────────────────────────────────────

    def get_manager_info(self) -> Dict[str, Any]:
        return self._request("GET", "/").json()

    def get_manager_status(self) -> Dict[str, Any]:
        return self._request("GET", "/manager/status").json()

    def get_manager_configuration(self) -> Dict[str, Any]:
        return self._request("GET", "/manager/configuration").json()

    # ── Cluster ───────────────────────────────────────────────────────

    def get_cluster_status(self) -> Dict[str, Any]:
        return self._request("GET", "/cluster/status").json()

    def get_cluster_nodes(self) -> Dict[str, Any]:
        return self._request("GET", "/cluster/nodes").json()

    # ── Agents ────────────────────────────────────────────────────────

    def list_agents(self, **params: Any) -> Dict[str, Any]:
        return self._request("GET", "/agents", params=params).json()

    def get_agent(self, agent_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/agents", params={"agents_list": agent_id}).json()

    def get_agents_summary(self) -> Dict[str, Any]:
        return self._request("GET", "/agents/summary/status").json()

    # ── Groups ────────────────────────────────────────────────────────

    def list_groups(self) -> Dict[str, Any]:
        return self._request("GET", "/groups").json()

    # ── Vulnerability ─────────────────────────────────────────────────

    def get_agent_vulnerabilities(self, agent_id: str, **params: Any) -> Dict[str, Any]:
        return self._request("GET", f"/vulnerability/{agent_id}", params=params).json()

    # ── Rules ─────────────────────────────────────────────────────────

    def list_rules(self, **params: Any) -> Dict[str, Any]:
        return self._request("GET", "/rules", params=params).json()
