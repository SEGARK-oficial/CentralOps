"""HTTP client for the Wazuh Indexer API (port 9200).

Uses basic auth against the OpenSearch-compatible API to query indices
like wazuh-alerts-*, wazuh-archives-*, wazuh-states-vulnerabilities-*.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

import httpx

from ...core.url_policy import normalize_service_url

logger = logging.getLogger(__name__)


class WazuhIndexerClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        verify_ssl: bool = True,
    ) -> None:
        normalized_url = normalize_service_url(base_url)
        if not normalized_url:
            raise ValueError("Wazuh Indexer base URL is required")

        self.base_url = normalized_url.rstrip("/")
        self._http = httpx.Client(
            timeout=httpx.Timeout(30.0, read=120.0),
            verify=verify_ssl,
            auth=(username, password),
            follow_redirects=False,
        )

    def close(self) -> None:
        self._http.close()

    # ── Cluster health ────────────────────────────────────────────────

    def get_cluster_health(self) -> Dict[str, Any]:
        resp = self._http.get(f"{self.base_url}/_cluster/health")
        resp.raise_for_status()
        return resp.json()

    def get_cluster_stats(self) -> Dict[str, Any]:
        resp = self._http.get(f"{self.base_url}/_cluster/stats")
        resp.raise_for_status()
        return resp.json()

    # ── Index operations ──────────────────────────────────────────────

    def list_indices(self, pattern: str = "wazuh-*") -> Dict[str, Any]:
        resp = self._http.get(
            f"{self.base_url}/_cat/indices/{pattern}",
            params={"format": "json", "h": "index,health,status,docs.count,store.size"},
        )
        resp.raise_for_status()
        return resp.json()

    # ── Search ────────────────────────────────────────────────────────

    def search(self, index: str, body: Dict[str, Any]) -> Dict[str, Any]:
        resp = self._http.post(
            f"{self.base_url}/{index}/_search",
            json=body,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()

    def count(self, index: str, body: Dict[str, Any] | None = None) -> int:
        url = f"{self.base_url}/{index}/_count"
        if body:
            resp = self._http.post(url, json=body, headers={"Content-Type": "application/json"})
        else:
            resp = self._http.get(url)
        resp.raise_for_status()
        return resp.json().get("count", 0)
