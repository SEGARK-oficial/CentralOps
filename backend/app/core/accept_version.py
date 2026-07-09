"""Dependency FastAPI para negociação de versão via header Accept.

Clientes que enviam ``Accept: application/vnd.centralops.v1+json``
recebem o shape legado v1 com header ``X-API-Deprecation``.
Qualquer outro valor (incluindo ausência ou ``*/*``) recebe v2.

Uso:
    from ..core.accept_version import resolve_api_version, V1_MEDIA_TYPE

    @router.get("/foo")
    def get_foo(version: int = Depends(resolve_api_version)):
        if version == 1:
            return build_v1_response()
        return build_v2_response()
"""
from __future__ import annotations

from fastapi import Request

V1_MEDIA_TYPE = "application/vnd.centralops.v1+json"


def resolve_api_version(request: Request) -> int:
    """Return 1 if client explicitly requests v1, 2 otherwise."""
    accept = request.headers.get("accept", "")
    if V1_MEDIA_TYPE in accept:
        return 1
    return 2
