"""Liveness/readiness probes.

Três sinais distintos, como manda o manual de operação enterprise:

- ``GET /livez``  — **liveness**: o processo está vivo e o event loop responde.
  SEM dependências (não toca DB/Redis). É o sinal para *reiniciar* um processo
  travado. Uma falha aqui = mate e suba de novo.
- ``GET /readyz`` — **readiness**: as dependências críticas (Postgres + Redis)
  estão alcançáveis. 200 quando a réplica pode receber tráfego; **503** quando
  uma dependência está fora (o LB/orquestrador/k8s deve *remover a réplica do
  pool* sem matá-la). Um DB lento não deve reiniciar o container — só tirá-lo do LB.

Ambas são **públicas** (sem auth) e **fora do prefixo ``/api``** — não passam
pelo audit middleware nem exigem sessão. O healthcheck (Docker Compose/k8s) bate
direto no processo uvicorn (``:8000``), não pelo nginx.

Distinção deliberada vs. o proxy ``/api/auth/status`` usado antes como
healthcheck: aquele toca o DB, logo conflundia liveness com readiness — um DB
lento derrubaria o container sob uma liveness probe. ``/livez`` nunca toca o DB.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from ..core.config import settings
from ..db.database import SessionLocal

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/livez", include_in_schema=False)
async def livez() -> dict:
    """Liveness: sem dependências. 200 enquanto o event loop responde."""
    return {"status": "alive"}


def _ping_db() -> None:
    """``SELECT 1`` síncrono. Rápido (<1ms); levanta em falha de conexão."""
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
    finally:
        db.close()


async def _ping_redis() -> None:
    """Ping real ao Redis configurado.

    Usa um cliente direto (NÃO o ``redis_client`` com fallback in-memory): a
    readiness precisa refletir o estado REAL do Redis, não degradar em silêncio.
    Sem ``REDIS_URL``, Redis não é dependência deste deploy → skip (ready).
    """
    if not settings.REDIS_URL:
        return
    import redis.asyncio as redis_async

    client = redis_async.from_url(settings.REDIS_URL)
    try:
        await client.ping()
    finally:
        # redis-py >=5 usa aclose(); <5 usa close(). Best-effort.
        closer = getattr(client, "aclose", None) or client.close
        try:
            await closer()
        except Exception:  # pragma: no cover — fechamento best-effort
            pass


@router.get("/readyz", include_in_schema=False)
async def readyz(response: Response) -> dict:
    """Readiness: 200 se DB+Redis ok; 503 caso contrário (tira do LB)."""
    checks: dict[str, str] = {"db": "ok", "redis": "ok"}
    ready = True

    try:
        _ping_db()
    except Exception as exc:  # noqa: BLE001 — probe reporta, não propaga
        checks["db"] = f"error: {type(exc).__name__}"
        ready = False
        logger.warning("readyz: checagem de DB falhou: %s", exc)

    try:
        await _ping_redis()
    except Exception as exc:  # noqa: BLE001
        checks["redis"] = f"error: {type(exc).__name__}"
        ready = False
        logger.warning("readyz: checagem de Redis falhou: %s", exc)

    # Integridade de edição: um pod Enterprise mal-configurado (licença paga
    # mas o pacote Enterprise não ativou → sem subtree scope) NÃO deve receber
    # tráfego — senão serviria multi-tenant em silêncio como FLAT. Fail-loud (503).
    try:
        from ..core import edition as _edition

        problem = _edition.enterprise_integrity_problem()
        if problem:
            checks["edition"] = f"misconfigured: {problem}"
            ready = False
            logger.error("readyz: integridade de edição falhou: %s", problem)
    except Exception as exc:  # noqa: BLE001 — probe nunca propaga
        logger.warning("readyz: checagem de integridade de edição falhou: %s", exc)

    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if ready else "not_ready", "checks": checks}
