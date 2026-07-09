"""Endpoints admin da integração DFIR-IRIS.

O IRIS é uma integração de BORDA OPCIONAL — não participa do hot path de entrega
(o envelope usa ``Organization.id`` interno; o customer id externo vive em
``destination_customer_mappings``). Estes endpoints são para o operador
gerenciar/observar a integração.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, Response, status

from ..core import auth as app_auth
from ..db import models

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/iris", tags=["iris"])


@router.get("/health")
def iris_health(
    response: Response,
    _: models.AppUser = Depends(
        app_auth.require_permission(app_auth.Permission.ORG_MANAGE)
    ),
) -> dict:
    """Probe admin do DFIR-IRIS (conectividade + auth).

    - ``not_configured`` (200): DFIR_IRIS_* ausente — integração desabilitada
      (estado válido, NÃO um erro: o IRIS é opcional).
    - ``reachable`` (200): a API do IRIS respondeu (lista de customers).
    - ``unreachable`` (503): configurado mas a API recusou/timeout.

    Admin-scoped: faz uma chamada de saída ao IRIS, então NÃO pode ser um probe
    público (vs. /livez,/readyz).
    """
    from ..services.iris_client import (
        IrisApiError,
        IrisClient,
        IrisConfigurationError,
    )

    client = IrisClient()
    try:
        try:
            client._ensure_configured()  # noqa: SLF001
        except IrisConfigurationError:
            return {"status": "not_configured", "configured": False}

        started = time.monotonic()
        try:
            # Exercita rede+auth via um endpoint conhecido (lista de customers).
            client.find_customer_by_name("__centralops_health_probe__")
        except IrisApiError as exc:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            logger.warning("iris_health: IRIS inacessível (%s)", type(exc).__name__)
            return {
                "status": "unreachable",
                "configured": True,
                "url": client.base_url,
                "error": type(exc).__name__,
            }
        latency_ms = round((time.monotonic() - started) * 1000, 1)
        return {
            "status": "reachable",
            "configured": True,
            "url": client.base_url,
            "latency_ms": latency_ms,
        }
    finally:
        client.close()
