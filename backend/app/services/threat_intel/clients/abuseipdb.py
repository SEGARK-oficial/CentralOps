"""Cliente assíncrono para a API do AbuseIPDB."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


ABUSEIPDB_BASE_URL = "https://api.abuseipdb.com/api/v2"


class AbuseIPDBQuotaExceeded(RuntimeError):
    """HTTP 429 — chave atingiu cota."""


class AbuseIPDBAuthError(RuntimeError):
    """HTTP 401/403 — chave inválida."""


@dataclass
class AbuseIPDBResult:
    abuse_confidence_score: Optional[int]
    country_code: Optional[str]
    usage_type: Optional[str]
    domain: Optional[str]
    total_reports: Optional[int]


class AbuseIPDBClient:
    """Wrapper sobre httpx.AsyncClient mantendo as chamadas que precisamos."""

    def __init__(self, timeout_seconds: int = 5) -> None:
        self._timeout = httpx.Timeout(float(timeout_seconds))

    async def check_ip(
        self,
        ip: str,
        api_key: str,
        *,
        max_age_days: int = 30,
    ) -> AbuseIPDBResult:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(
                f"{ABUSEIPDB_BASE_URL}/check",
                headers={"Key": api_key, "Accept": "application/json"},
                params={"ipAddress": ip, "maxAgeInDays": max_age_days},
            )
        return self._parse_check(resp)

    async def download_blacklist(
        self,
        api_key: str,
        *,
        confidence_minimum: int = 80,
        limit: int = 10000,
    ) -> list[str]:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            resp = await client.get(
                f"{ABUSEIPDB_BASE_URL}/blacklist",
                headers={"Key": api_key, "Accept": "application/json"},
                params={
                    "confidenceMinimum": confidence_minimum,
                    "limit": limit,
                },
            )
        if resp.status_code == 429:
            raise AbuseIPDBQuotaExceeded("blacklist quota exceeded")
        if resp.status_code in (401, 403):
            raise AbuseIPDBAuthError(f"invalid blacklist key: {resp.status_code}")
        resp.raise_for_status()
        payload = resp.json().get("data", []) or []
        return [item.get("ipAddress") for item in payload if item.get("ipAddress")]

    @staticmethod
    def _parse_check(response: httpx.Response) -> AbuseIPDBResult:
        if response.status_code == 429:
            raise AbuseIPDBQuotaExceeded("quota exceeded")
        if response.status_code in (401, 403):
            raise AbuseIPDBAuthError(f"unauthorized: {response.status_code}")
        response.raise_for_status()
        data = response.json().get("data", {}) or {}
        return AbuseIPDBResult(
            abuse_confidence_score=data.get("abuseConfidenceScore"),
            country_code=data.get("countryCode"),
            usage_type=data.get("usageType"),
            domain=data.get("domain"),
            total_reports=data.get("totalReports"),
        )


__all__ = [
    "AbuseIPDBClient",
    "AbuseIPDBResult",
    "AbuseIPDBQuotaExceeded",
    "AbuseIPDBAuthError",
]
