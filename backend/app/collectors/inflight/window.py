"""Janela deslizante em voo (W1.6) — ``min_count`` em ``window_seconds`` para
regras ``eval_mode='inflight'``, com estado FORA do caminho quente.

Como funciona: o ciclo acumula os matches em memória como sempre (uma chave
por (regra, group_by), com ``hits`` = quantos eventos casaram nela neste
ciclo). No FLUSH, e só nele, cada chave incrementa o bucket corrente no Redis
(``INCRBY`` + ``EXPIRE``) e lê a soma dos buckets vivos da janela. A regra só
vira Detection quando essa soma alcança ``min_count``.

**Deslizante, não tumbling.** A janela tem ``N_BUCKETS`` fatias de
``window/N_BUCKETS`` segundos; "os últimos W segundos" é a soma das fatias
cujo índice cai em ``[agora-W, agora]``. Uma fatia velha sai da conta por
ÍNDICE, não por TTL — o TTL só recolhe o lixo. Por isso a leitura é
determinística e testável com relógio injetado.

**Fail-CLOSED para emissão.** Sem Redis não há como saber a contagem; emitir a
cada match (o que a regra faria sem janela) seria inundar o SIEM justamente
quando a infra está degradada. A chave é descartada do flush e contada em
``window_unavailable``, com aviso 1x por ciclo.

Custo: 2 pipelines por flush (escrita, leitura), não por evento; ≤ 500 chaves
por flush (``INFLIGHT_MAX_DETECTIONS_PER_FLUSH``), 11 GETs por chave.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

N_BUCKETS = 10
KEY_PREFIX = "inflight:win"


def bucket_seconds(window_seconds: int) -> int:
    return max(1, int(window_seconds) // N_BUCKETS)


def bucket_ids(window_seconds: int, now: float) -> Tuple[int, ...]:
    """Índices das fatias que cobrem ``[now - window, now]``, do mais velho ao atual."""
    bs = bucket_seconds(window_seconds)
    cur = int(now) // bs
    n = max(1, int(window_seconds) // bs)  # normalmente N_BUCKETS; +1 cobre a fatia parcial
    return tuple(range(cur - n, cur + 1))


def _key(dedup_key: str, bucket: int) -> str:
    return f"{KEY_PREFIX}:{dedup_key}:{bucket}"


def apply_windows(
    redis: Any,
    items: Mapping[str, Tuple[int, int, int]],
    *,
    now: Optional[float] = None,
) -> Dict[str, int]:
    """``items``: dedup_key → (hits_neste_ciclo, min_count, window_seconds).
    Devolve dedup_key → total na janela (após somar este ciclo). Levanta se o
    Redis falhar — o chamador decide (fail-closed)."""
    if not items:
        return {}
    ts = time.time() if now is None else now
    pipe = redis.pipeline()
    for dk, (hits, _mc, win) in items.items():
        cur = bucket_ids(win, ts)[-1]
        k = _key(dk, cur)
        pipe.incrby(k, int(hits))
        pipe.expire(k, int(win) + bucket_seconds(win) + 5)
    pipe.execute()

    pipe = redis.pipeline()
    order: List[Tuple[str, int]] = []
    for dk, (_hits, _mc, win) in items.items():
        ids = bucket_ids(win, ts)
        pipe.mget([_key(dk, b) for b in ids])
        order.append((dk, len(ids)))
    results = pipe.execute()
    totals: Dict[str, int] = {}
    for (dk, _n), vals in zip(order, results):
        totals[dk] = sum(int(v) for v in (vals or []) if v)
    return totals


def windowed(rule: Any) -> bool:
    """A regra pede contagem em janela? (``min_count > 1`` E ``window_seconds > 0``)."""
    return int(getattr(rule, "min_count", 1) or 1) > 1 and int(getattr(rule, "window_seconds", 0) or 0) > 0
