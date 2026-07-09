"""Helpers for formatting Sophos API errors consistently."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class SophosErrorPayload:
    error: str | None
    message: str
    code: str | int | None
    correlation_id: str | None
    request_id: str | None
    doc_url: str | None


class SophosAPIError(RuntimeError):
    """Raised when a Sophos API returns a non-success status code."""

    def __init__(
        self,
        status_code: int,
        action: str,
        payload: SophosErrorPayload,
        hint: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.action = action
        self.payload = payload
        self.hint = hint
        super().__init__(self._build_message())

    def _build_message(self) -> str:
        base = f"{self.action} failed (HTTP {self.status_code})"
        if self.payload.error:
            base += f" [{self.payload.error}]"
        base += f": {self.payload.message}"

        details: list[str] = []
        if self.payload.correlation_id:
            details.append(f"correlationId={self.payload.correlation_id}")
        if self.payload.request_id:
            details.append(f"requestId={self.payload.request_id}")
        if self.payload.code:
            details.append(f"code={self.payload.code}")
        if self.payload.doc_url:
            details.append(f"docUrl={self.payload.doc_url}")

        if details:
            base += " | " + ", ".join(details)
        if self.hint:
            base += f" | Hint: {self.hint}"
        return base


def parse_sophos_error(response: httpx.Response) -> SophosErrorPayload:
    """Parse a Sophos-standard error payload from an HTTP response."""
    data: dict[str, Any] = {}
    try:
        maybe_data = response.json()
        if isinstance(maybe_data, dict):
            data = maybe_data
    except Exception:
        data = {}

    response_text = (response.text or "").strip()
    message = (
        data.get("message")
        or data.get("detail")
        or response.reason_phrase
        or (response_text[:240] if response_text else "Unknown error")
    )

    return SophosErrorPayload(
        error=data.get("error"),
        message=message,
        code=data.get("code"),
        correlation_id=data.get("correlationId"),
        request_id=data.get("requestId"),
        doc_url=data.get("docUrl"),
    )


def raise_sophos_api_error(
    action: str,
    response: httpx.Response,
    hint: str | None = None,
) -> None:
    """Raise a rich exception for non-success Sophos API responses."""
    raise SophosAPIError(
        status_code=response.status_code,
        action=action,
        payload=parse_sophos_error(response),
        hint=hint,
    )
