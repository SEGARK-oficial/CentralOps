"""Entrega em ordem por destino (ADR-0018 Onda 4e).

A entrega concorrente (Onda 4b), as várias partições por destino (4c) e o
estacionamento (4a) trocam a ordem de chegada dos lotes no destino. Para quem
indexa pela hora do evento (Indexer, ClickHouse, LogScale) isso não muda nada.
Para quem correlaciona pela ORDEM DE CHEGADA — as regras de sequência e de
frequência do Wazuh (``if_matched_sid``, ``frequency``/``timeframe``) — um "A
seguido de B" que chega B, A pode não disparar. Destino com
``delivery.ordered = true`` troca vazão por ordem:

* **Uma partição.** O processador produz todos os lotes dele numa partição só
  (``kafka_transport.produce``), e o envelope sai com ``"ordered": true``.
* **Um lote por vez.** O dispatcher encadeia os lotes do destino: cada um só
  decide depois que o anterior foi entregue ou estacionado.
* **Estacionado não é ultrapassado.** Ao estacionar um lote, o dispatcher grava
  no Redis o número de sequência dele (``reserve``) ANTES de produzir; enquanto
  essa marca existir, os lotes seguintes também estacionam, em ordem, atrás dele.
  O drenador apaga a marca só ao entregar o lote com aquele número (``release``,
  compare-and-delete): nesse instante não há lote do destino estacionado à frente
  de nenhum outro, e o caminho direto volta.

Custos, aceitos ao ligar a opção: o destino tem o teto de um dispatcher; na volta
de uma queda ele só recebe dado novo depois de esvaziar o estacionamento; e um
lote cortado no meio pelo tempo de espera pode chegar duas vezes (at-least-once).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, Optional, Tuple

from ...core.config import settings

logger = logging.getLogger(__name__)

#: Quanto tempo o processador confia na leitura do ``delivery.ordered`` de um
#: destino antes de reler do banco.
CONFIG_TTL_S = 30.0

_cache: Dict[str, Tuple[float, bool]] = {}
_cache_lock = threading.Lock()

# Compare-and-delete: só apaga a marca se ela ainda é a do lote entregue. Um
# GET seguido de DEL deixaria o dispatcher gravar uma marca nova entre os dois.
_RELEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)


def _load(destination_id: str) -> bool:
    from ...db import database, models
    from ..output.delivery_config import parse_delivery_lenient

    with database.SessionLocal() as session:
        row = session.get(models.Destination, destination_id)
        if row is None:
            return False
        try:
            delivery = json.loads(row.delivery or "{}")
        except (TypeError, ValueError):
            delivery = {}
        return bool(parse_delivery_lenient(row.kind, delivery).ordered)


def is_ordered(destination_id: str) -> bool:
    """``delivery.ordered`` do destino, com cache de ``CONFIG_TTL_S``. Banco fora
    mantém a última leitura (ou ``False`` se nunca leu)."""
    now = time.monotonic()
    hit = _cache.get(destination_id)
    if hit is not None and hit[0] > now:
        return hit[1]
    try:
        value = _load(destination_id)
    except Exception:  # noqa: BLE001 — o processador não para por isso
        logger.warning(
            "data-plane: não li delivery.ordered do destino %s — mantendo %s",
            destination_id, hit[1] if hit else False, exc_info=True,
        )
        value = hit[1] if hit else False
    with _cache_lock:
        _cache[destination_id] = (now + CONFIG_TTL_S, value)
    return value


def reset_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _last_key(destination_id: str) -> str:
    return f"dataplane:ordered:{destination_id}:last"


def _seq_key(destination_id: str) -> str:
    return f"dataplane:ordered:{destination_id}:seq"


async def has_backlog(redis: Any, destination_id: str) -> bool:
    """True se há lote do destino estacionado à espera do drenador."""
    return bool(await redis.exists(_last_key(destination_id)))


async def reserve(redis: Any, destination_id: str) -> int:
    """Número do próximo lote a estacionar, já gravado como a marca do destino.

    Grava ANTES de produzir: se gravasse depois, o drenador podia entregar o lote
    e não achar marca nenhuma para apagar, e a marca gravada em seguida nunca mais
    sairia. A marca expira com a retenção do estacionamento, e uma marca sem lote
    (produção que falhou e o processo parou) sai sozinha no próximo lote do
    destino, que estaciona e grava a dele."""
    seq = int(await redis.incr(_seq_key(destination_id)))
    ttl_s = max(1, int(settings.KAFKA_PARKED_RETENTION_MS) // 1000)
    await redis.set(_last_key(destination_id), str(seq), ex=ttl_s)
    return seq


async def release(redis: Any, destination_id: str, seq: Any) -> bool:
    """Apaga a marca se ela é a do lote ``seq`` (o último estacionado)."""
    return bool(await redis.eval(_RELEASE_LUA, 1, _last_key(destination_id), str(seq)))


def stamp(value: bytes, seq: int) -> bytes:
    """O registro de entrega com o número de sequência para o drenador."""
    payload = json.loads(value)
    payload["ord_seq"] = int(seq)
    return json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")


def sequence_of(payload: Dict[str, Any]) -> Optional[int]:
    seq = payload.get("ord_seq")
    return int(seq) if isinstance(seq, int) and not isinstance(seq, bool) else None


__all__ = [
    "CONFIG_TTL_S",
    "has_backlog",
    "is_ordered",
    "release",
    "reserve",
    "reset_cache",
    "sequence_of",
    "stamp",
]
