"""Cliente assíncrono para a API do AlienVault OTX."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


OTX_BASE_URL = "https://otx.alienvault.com/api/v1"


class OTXQuotaExceeded(RuntimeError):
    pass


class OTXAuthError(RuntimeError):
    pass


@dataclass
class OTXResult:
    pulse_count: int
    reputation: Optional[int] = None


class OTXClient:
    def __init__(self, timeout_seconds: int = 5) -> None:
        self._timeout = httpx.Timeout(float(timeout_seconds))

    async def lookup_ip(self, ip: str, api_key: str) -> OTXResult:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(
                f"{OTX_BASE_URL}/indicators/IPv4/{ip}/general",
                headers={"X-OTX-API-KEY": api_key, "Accept": "application/json"},
            )
        if resp.status_code == 429:
            raise OTXQuotaExceeded("otx quota exceeded")
        if resp.status_code in (401, 403):
            raise OTXAuthError(f"otx unauthorized: {resp.status_code}")
        resp.raise_for_status()
        data = resp.json() or {}
        pulse_info = data.get("pulse_info", {}) or {}
        return OTXResult(
            pulse_count=int(pulse_info.get("count", 0) or 0),
            reputation=data.get("reputation"),
        )


__all__ = ["OTXClient", "OTXResult", "OTXQuotaExceeded", "OTXAuthError"]
