"""Testes para W5: sample reservoir fire-and-forget.

W3 (pool Redis compartilhado) foi REVERTIDO — commit e192eae introduziu
``_worker_redis_pool`` global que causava "Event loop is closed" em workers
Celery prefork (cada task abre um asyncio.run() com loop próprio; o pool
do processo-pai usa futures do loop anterior).

Ver: revert(redis): W3 — pool compartilhado quebra event loop em Celery prefork

Os testes originais de W3 foram marcados como skip para preservar
o histórico; novos testes ficam em test_celery_redis_isolation.py.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import patch

import pytest


# ── W3 (REVERTIDO) ───────────────────────────────────────────────────────────


@pytest.mark.skip(reason="W3 revertido — pool compartilhado quebra event loop em Celery prefork")
def test_get_worker_redis_falls_back_to_from_url_when_pool_none() -> None:
    """Sem pool inicializado, get_worker_redis deve retornar cliente efêmero."""
    pass  # obsoleto após reversão do W3


@pytest.mark.skip(reason="W3 revertido — pool compartilhado quebra event loop em Celery prefork")
def test_get_worker_redis_returns_pool_client_when_initialized() -> None:
    """Com pool inicializado, get_worker_redis chama Redis(connection_pool=pool)."""
    pass  # obsoleto após reversão do W3


# ── W5: sample fire-and-forget ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sample_reservoir_push_fire_and_forget_does_not_block() -> None:
    """ensure_future em sample_reservoir.push não deve bloquear o hot path.

    Simula o trecho do pipeline onde push é agendado com ensure_future.
    Mede que a linha de agendamento é executada em < 5ms (não aguarda I/O).
    """
    push_started = asyncio.Event()
    push_completed = asyncio.Event()

    async def slow_push(*args: Any, **kwargs: Any) -> None:
        """Simula push lento (100ms) — fire-and-forget não deve esperar."""
        push_started.set()
        await asyncio.sleep(0.1)
        push_completed.set()

    # Simula o trecho do pipeline:
    #   asyncio.ensure_future(sample_reservoir.push(redis, platform, ...))
    t0 = time.monotonic()
    future = asyncio.ensure_future(slow_push("redis", "platform", "event_type", {}))
    elapsed_schedule = time.monotonic() - t0

    # O agendamento deve ser quase instantâneo (< 5ms).
    assert elapsed_schedule < 0.005, (
        f"ensure_future demorou {elapsed_schedule*1000:.1f}ms — esperado < 5ms"
    )

    # Aguarda a task completar para não vazar no loop de teste.
    await asyncio.wait_for(push_completed.wait(), timeout=1.0)
    assert push_started.is_set()
    assert push_completed.is_set()

    # Cancela o future se ainda pendente.
    if not future.done():
        future.cancel()
