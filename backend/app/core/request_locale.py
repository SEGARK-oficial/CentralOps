"""Request-scoped locale.

The SPA sends the user's chosen language on every API call via ``Accept-Language``.
This module parses it once per request into a contextvar so the rest of the request
(error messages, emails) can render in that language. Operational LOGS stay English.

Compiled-image safe: pure-Python, no on-disk assets. Locale codes are the same base
codes the frontend uses (``pt``/``en``/``es``); ``pt`` is the default/fallback.
"""
from __future__ import annotations

import contextvars

SUPPORTED_LOCALES = ("pt", "en", "es")
DEFAULT_LOCALE = "pt"

_locale_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_locale", default=DEFAULT_LOCALE
)


def normalize_locale(raw: str | None) -> str:
    """Map an arbitrary tag (``pt-BR``, ``en-US``, ``es-419``) to a supported base
    code, falling back to the default. Never raises."""
    if not raw:
        return DEFAULT_LOCALE
    base = raw.strip().lower().replace("_", "-").split("-", 1)[0]
    return base if base in SUPPORTED_LOCALES else DEFAULT_LOCALE


def parse_accept_language(header: str | None) -> str:
    """Pick the highest-priority supported locale from an ``Accept-Language`` header
    (``pt-BR,pt;q=0.9,en;q=0.8``). Returns the default if none match."""
    if not header:
        return DEFAULT_LOCALE
    best: tuple[float, str] | None = None
    for part in header.split(","):
        token = part.strip()
        if not token:
            continue
        tag, _, params = token.partition(";")
        q = 1.0
        if params:
            _, _, qval = params.partition("=")
            try:
                q = float(qval)
            except ValueError:
                q = 1.0
        code = normalize_locale(tag)
        if code in SUPPORTED_LOCALES and (best is None or q > best[0]):
            best = (q, code)
    return best[1] if best else DEFAULT_LOCALE


def set_locale(locale: str) -> None:
    _locale_var.set(normalize_locale(locale))


def get_locale() -> str:
    """The current request's locale (or the default outside a request)."""
    return _locale_var.get()
