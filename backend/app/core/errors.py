"""Localized API errors.

``ApiError`` carries a stable machine ``code`` plus its message in each supported
locale (inline — no shared catalog file, so it's compiled-image/.so-safe and lets
the string-extraction fan out per-router without a coordination point). The handler
renders the message for the request's locale (``request_locale``), so EVERY client
— the SPA and API-only MSSP integrations alike — gets a localized message, while
the machine ``code`` stays constant for programmatic handling.

Envelope (matches the frontend ApiRequestError parser):
    {"error": {"code": "...", "message": "<localized>", "details": {...}}}

Migration is graceful: routers still raising plain ``HTTPException`` keep returning
``{"detail": "<pt>"}`` and render fine (Portuguese) until converted.
"""
from __future__ import annotations

from typing import Any, Mapping

from fastapi import Request
from fastapi.responses import JSONResponse

from .request_locale import DEFAULT_LOCALE, get_locale

# A message set: {"pt": "...", "en": "...", "es": "..."}. `pt` is mandatory (the
# source language / fallback); en/es SHOULD be present but degrade to pt if absent.
Messages = Mapping[str, str]


class ApiError(Exception):
    """A user-facing, localized, coded API error.

    Example::

        raise ApiError(
            "integration.not_found",
            status_code=404,
            messages={
                "pt": "Integração não encontrada.",
                "en": "Integration not found.",
                "es": "Integración no encontrada.",
            },
        )

    ``params`` are ICU-style interpolation values echoed back under ``details`` and
    substituted into the message with ``str.format`` (``{name}`` placeholders).
    """

    def __init__(
        self,
        code: str,
        status_code: int = 400,
        *,
        messages: Messages | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.messages: dict[str, str] = dict(messages or {})
        self.params: dict[str, Any] = dict(params or {})

    def render(self, locale: str | None = None) -> str:
        loc = locale or get_locale()
        text = self.messages.get(loc) or self.messages.get(DEFAULT_LOCALE) or self.code
        if self.params:
            try:
                text = text.format(**self.params)
            except (KeyError, IndexError, ValueError):
                pass
        return text


def api_error_response(exc: ApiError, locale: str | None = None) -> JSONResponse:
    message = exc.render(locale)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            # Structured envelope the SPA prefers (code → programmatic handling).
            "error": {
                "code": exc.code,
                "message": message,
                "details": exc.params,
            },
            # Backward-compat: FastAPI's default error shape is {"detail": ...}.
            # Keep it (now localized) so existing clients/tests that read `detail`
            # keep working — the SPA's parser already prefers `error` over `detail`.
            "detail": message,
        },
    )


async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
    """Registered on the app; renders in the request's locale."""
    return api_error_response(exc)
