"""Base class compartilhada para erros de rate-limit dos vendors.

Todos os vendors (Sophos alerts/cases/detections, Defender, NinjaOne, ...)
levantam alguma forma de ``RateLimitedError`` quando o servidor responde
HTTP 429 com ``Retry-After: <seconds>``. Sem uma base comum, cada um vira
uma classe separada e o Celery ``autoretry_for`` precisaria listar todas.

Esta base permite ``isinstance(exc, VendorRateLimitedError)`` em
``tasks.py``, e a task respeitar ``exc.retry_after`` via
``self.retry(countdown=exc.retry_after, exc=exc)`` em vez do backoff
exponencial cego (que ignora o ``Retry-After`` do servidor).
"""

from __future__ import annotations


class VendorRateLimitedError(Exception):
    """Base class — vendors específicos herdam pra preservar nomes legados."""

    def __init__(self, retry_after: int, vendor: str | None = None) -> None:
        self.retry_after = retry_after
        self.vendor = vendor
        suffix = f" (vendor={vendor})" if vendor else ""
        super().__init__(f"vendor 429 retry_after={retry_after}s{suffix}")
