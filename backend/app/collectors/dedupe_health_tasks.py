"""Amostragem periódica da saúde do Redis do dedupe (ADR-0015, Fase 0).

O dedupe (``state/dedupe.py::claim``) é o ÚNICO guard de idempotência do hot path
e custa 1 round-trip Redis POR EVENTO — é o gargalo dominante do pipeline. Sob o
``volatile-lru`` do compose, uma chave ``dedupe:*`` evictada faz ``claim()``
devolver ``True`` para um evento REENTREGUE, que é indistinguível de um evento
genuinamente novo. Nenhum contador no hot path detecta isso: só o sinal de
evicção do próprio Redis expõe a falha.

Por que uma task de beat e não uma chamada em ``claim()``: amostrar dentro do
``claim`` seria um 2º round-trip por evento, dobrando exatamente o gargalo que se
quer aliviar. Aqui o custo é 1 ``INFO`` por minuto, amortizado sobre milhares de
eventos — e ``memory_used_ratio`` alerta ANTES de a evicção começar, que é o único
momento em que ainda dá para agir.

Best-effort: falha aqui nunca afeta coleta nem entrega.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from celery import shared_task

from ..core.config import settings

logger = logging.getLogger(__name__)


@shared_task(bind=True, queue="maintenance", name="collectors.dedupe_sample_redis_health")
def sample_dedupe_redis_health(self: Any) -> dict[str, Any]:
    """Amostra ``INFO`` do Redis do dedupe e publica os gauges OTel.

    Returns: ``{"sampled": bool, "evicted_keys": int|None, "memory_used_ratio": float|None}``
    """

    async def _run() -> dict[str, Any]:
        import redis.asyncio as redis_async

        from .state.dedupe import sample_redis_health

        client = redis_async.from_url(settings.REDIS_URL)
        try:
            health = await sample_redis_health(client)
        finally:
            # Conexão efêmera por task — o worker recicla processos
            # (``worker_max_tasks_per_child``), então pool persistente aqui não paga.
            await client.aclose()

        if health is None:
            return {"sampled": False, "evicted_keys": None, "memory_used_ratio": None}
        return {
            "sampled": True,
            "evicted_keys": health.evicted_keys,
            "memory_used_ratio": health.memory_used_ratio,
        }

    try:
        return asyncio.run(_run())
    except Exception:
        # Best-effort: um Redis fora do ar já se manifesta no hot path; esta task
        # não deve somar ruído de alerta nem falhar a fila de maintenance.
        logger.warning("dedupe_sample_redis_health: amostragem falhou", exc_info=True)
        return {"sampled": False, "evicted_keys": None, "memory_used_ratio": None}
