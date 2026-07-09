"""Sample reservoir Redis para dry-run de mapping.

Ring buffer de últimos N raw events por (vendor, event_type). A UI do
mapping editor consome o reservoir para alimentar o dry-run em tempo
real.

Implementação: ``LPUSH`` + ``LTRIM`` em chave única — um round-trip
de dois comandos via pipeline Redis. Não bloqueia a coleta.

Capacidade default: 100 eventos por (vendor, event_type).
Configurável via parâmetro do método.
"""

from __future__ import annotations

import json
import logging
from typing import Any, List, Mapping, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    import redis.asyncio as redis_async

logger = logging.getLogger(__name__)

DEFAULT_CAPACITY = 100
_KEY_PREFIX = "normalize:sample"


def _key(organization_id: int, vendor: str, event_type: str) -> str:
    # ``organization_id`` é o 1º segmento de escopo — isola o
    # reservoir por tenant. Sem ele, o dry-run da UI lia amostras raw de
    # OUTROS tenants do mesmo vendor (vazamento cross-tenant verificado).
    return f"{_KEY_PREFIX}:{organization_id}:{vendor}:{event_type}"


async def push(
    redis: "redis_async.Redis",
    organization_id: int,
    vendor: str,
    event_type: str,
    raw: Mapping[str, Any],
    *,
    capacity: int = DEFAULT_CAPACITY,
) -> None:
    """Empurra um evento raw para o ring buffer (escopo por ``organization_id``).

    Falha de Redis é logada mas não propaga — o reservoir é melhor-esforço
    para UI; a coleta principal não pode parar por causa disso.
    """
    if capacity <= 0:
        return
    key = _key(organization_id, vendor, event_type)
    try:
        serialized = json.dumps(raw, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        logger.warning(
            "sample_reservoir: payload não serializável vendor=%s event_type=%s",
            vendor, event_type,
        )
        return

    try:
        pipe = redis.pipeline()
        pipe.lpush(key, serialized)
        pipe.ltrim(key, 0, capacity - 1)
        await pipe.execute()
    except Exception as exc:  # pragma: no cover — best-effort
        logger.warning(
            "sample_reservoir: falha ao empurrar (%s): %s", key, exc
        )


async def peek(
    redis: "redis_async.Redis",
    organization_id: int,
    vendor: str,
    event_type: str,
    *,
    limit: Optional[int] = None,
) -> List[dict]:
    """Devolve os últimos eventos do ring buffer (mais recente primeiro).

    Escopado por ``organization_id``: o dry-run só enxerga amostras
    do PRÓPRIO tenant. Usado pelo dry-run da UI: chama isto +
    aplica a versão candidata do mapping para mostrar diff vs a atual.
    """
    key = _key(organization_id, vendor, event_type)
    end = (limit - 1) if limit and limit > 0 else -1
    try:
        raw_items = await redis.lrange(key, 0, end)
    except Exception as exc:
        logger.warning("sample_reservoir: falha ao ler (%s): %s", key, exc)
        return []

    out: List[dict] = []
    for item in raw_items or []:
        try:
            out.append(json.loads(item))
        except (TypeError, ValueError):
            # Item corrompido — ignora silenciosamente. Reservoir é
            # advisory; entradas inválidas serão expulsas pelo LTRIM
            # em ciclos futuros.
            continue
    return out


async def size(
    redis: "redis_async.Redis", organization_id: int, vendor: str, event_type: str
) -> int:
    """Tamanho atual do ring buffer do tenant (debug/observabilidade)."""
    try:
        return int(await redis.llen(_key(organization_id, vendor, event_type)))
    except Exception:
        return 0
