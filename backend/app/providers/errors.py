"""Structured provider errors used by integration-facing routes."""

from __future__ import annotations

from typing import Any, Dict, Mapping


class ProviderError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str = "PROVIDER_ERROR",
        status_code: int = 500,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.details: Dict[str, Any] = dict(details or {})

    def to_payload(self, *, integration_id: int | None = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if integration_id is not None:
            payload["integration_id"] = integration_id
        if self.details:
            payload["details"] = self.details
        return payload


class ProviderInvalidRequestError(ProviderError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "PROVIDER_REQUEST_INVALID",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message, code=code, status_code=400, details=details)


class ProviderConfigurationError(ProviderError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "PROVIDER_CONFIGURATION_INVALID",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message, code=code, status_code=422, details=details)


class ProviderConnectivityError(ProviderError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "PROVIDER_UNAVAILABLE",
        details: Mapping[str, Any] | None = None,
        status_code: int = 503,
    ) -> None:
        super().__init__(message, code=code, status_code=status_code, details=details)


class ProviderQueryError(ProviderError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "PROVIDER_QUERY_FAILED",
        details: Mapping[str, Any] | None = None,
        status_code: int = 502,
    ) -> None:
        super().__init__(message, code=code, status_code=status_code, details=details)


class ProviderNotFoundError(ProviderError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "PROVIDER_RESOURCE_NOT_FOUND",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message, code=code, status_code=404, details=details)
