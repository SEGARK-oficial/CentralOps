"""Testes determinísticos de VOLUME da camada de entrega.

Não são benchmarks de performance — são testes de CORREÇÃO sob volume:

(a) Lote grande (10 mil eventos) é chunkado por batch.max_items corretamente:
    - Conta chunks = ceil(total / max_items).
    - Nenhum evento duplicado; nenhum evento perdido.

(b) Concorrência por destino NUNCA excede o cap do semáforo:
    - Fake sender instrumentado registra concorrência simultânea.
    - Assertiva: max_concurrent <= dcfg.concurrency.

(c) Soak: muitos lotes em sequência não vazam estado:
    - Contadores de send são estáveis (linear com N lotes).
    - Pool de semáforos não acumula entradas obsoletas.

Marcados como não-benchmark — rodam no CI normal (sem flag de perf).
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.collectors.output.base import DeliveryResult
from backend.app.collectors.output.delivery_config import (
    BatchConfig,
    BreakerConfig,
    DeliveryConfig,
    RetryConfig,
)
from backend.app.db.database import Base
from backend.app.db import models


# ── Helpers ───────────────────────────────────────────────────────────────────


def _event(n: int) -> dict:
    return {
        "_centralops": {"event_id": f"evt-{n:05d}", "organization_id": 1},
        "raw": {},
    }


def _batch(size: int) -> list[dict]:
    return [_event(i) for i in range(size)]


def _dcfg(
    *,
    max_items: int = 500,
    max_retries: int = 0,
    concurrency: int = 4,
    timeout_ms: int = 30000,
) -> DeliveryConfig:
    """DeliveryConfig com backoff mínimo para testes rápidos."""
    return DeliveryConfig(
        batch=BatchConfig(max_items=max_items),
        retry=RetryConfig(max_retries=max_retries, initial_ms=10, max_ms=50, multiplier=2.0),
        timeout_ms=timeout_ms,
        concurrency=concurrency,
        breaker=BreakerConfig(failure_threshold=100, cooldown_s=1, window_s=60),
    )


def _dest_config_mock(dest_id: str = "load-dest-001", kind: str = "splunk_hec") -> MagicMock:
    cfg = MagicMock()
    cfg.destination_id = dest_id
    cfg.kind = kind
    cfg.delivery = {"breaker": {"failure_threshold": 100}}
    cfg.secret_ref = None
    cfg.config_version = "v1"
    cfg.name = "Load Test Destination"
    cfg.organization_id = None
    return cfg


def _stub_metrics() -> MagicMock:
    m = MagicMock()
    m.labels.return_value = m
    return m


def _stub_circuit_breaker() -> MagicMock:
    cb = MagicMock()
    cb.check_for_config = AsyncMock(return_value=None)
    cb.record_failure_for_config = AsyncMock(return_value=None)
    cb.record_success_for_config = AsyncMock(return_value=None)
    cb.BreakerOpen = type("BreakerOpen", (Exception,), {})
    return cb


# ── DB fixture para testes que precisam do dispatcher completo ─────────────────


@pytest.fixture()
def static_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    import backend.app.db.database as db_module

    original = db_module.SessionLocal
    db_module.SessionLocal = TestingSessionLocal
    yield TestingSessionLocal, engine
    db_module.SessionLocal = original
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def seeded_destination(static_db):
    TestingSessionLocal, _ = static_db
    dest_id = "load-splunk-001"
    with TestingSessionLocal() as session:
        session.add(
            models.Destination(
                id=dest_id,
                name="Load Splunk",
                kind="splunk_hec",
                enabled=True,
                config='{"url": "https://splunk:8088", "sourcetype": "load"}',
                secret_ref=None,
                delivery='{"retry": {"max_retries": 0}, "breaker": {"failure_threshold": 100}}',
                config_version="v1",
                organization_id=None,
            )
        )
        session.commit()
    return dest_id


# ── (a) Lote grande: chunked corretamente ────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "total,max_items,expected_chunks",
    [
        (10_000, 500, 20),       # 10k / 500 = 20 chunks exatos
        (10_000, 499, 21),       # ceil(10000/499) = 21
        (10_001, 500, 21),       # 10001 eventos → 21 chunks (20×500 + 1)
        (1, 500, 1),             # batch mínimo → 1 chunk
        (500, 500, 1),           # exatamente max_items → 1 chunk
        (501, 500, 2),           # um a mais → 2 chunks
    ],
)
async def test_large_batch_chunked_correctly(
    total: int,
    max_items: int,
    expected_chunks: int,
) -> None:
    """Lote de ``total`` eventos com max_items=``max_items`` → ``expected_chunks`` chunks.

    Conta chamadas ao send_batch e verifica que todos os eventos chegam
    exatamente uma vez (sem duplicação, sem perda).
    """
    received: list[list[dict]] = []

    async def capture_send(batch: list[dict]) -> DeliveryResult:
        received.append(list(batch))
        return DeliveryResult(accepted=len(batch))

    target = AsyncMock()
    target.send_batch.side_effect = capture_send

    dcfg = _dcfg(max_items=max_items)
    dc = _dest_config_mock()
    cb = _stub_circuit_breaker()
    labels = {"destination_id": dc.destination_id, "kind": dc.kind}
    m = _stub_metrics()

    batch = _batch(total)
    chunks = [batch[i : i + max_items] for i in range(0, len(batch), max_items)]

    from backend.app.collectors.pipeline import _send_chunk_with_retry

    for chunk in chunks:
        await _send_chunk_with_retry(
            target=target,
            chunk=chunk,
            dcfg=dcfg,
            dest_config=dc,
            labels=labels,
            redis=AsyncMock(),
            circuit_breaker=cb,
            persist_rejected_to_dlq=MagicMock(return_value=True),
            DELIVERY_LATENCY=m,
            DLQ_TOTAL=m,
            EVENTS_REJECTED=m,
            EVENTS_SENT=m,
            BYTES_SENT=m,
            RETRIES=m,
        )

    assert len(received) == expected_chunks, (
        f"total={total} max_items={max_items} → esperado {expected_chunks} chunks, "
        f"got {len(received)}"
    )

    # Nenhum evento perdido, nenhum duplicado
    all_ids = [e["_centralops"]["event_id"] for c in received for e in c]
    expected_ids = [e["_centralops"]["event_id"] for e in batch]
    assert sorted(all_ids) == sorted(expected_ids), (
        "Todos os eventos devem ser entregues exatamente uma vez"
    )

    # Todos os chunks respeitam o tamanho máximo
    for c in received[:-1]:
        assert len(c) <= max_items, (
            f"Chunk intermediário tem {len(c)} > max_items={max_items}"
        )


@pytest.mark.asyncio
async def test_10k_events_via_dispatcher(
    static_db, seeded_destination
) -> None:
    """10 mil eventos via dispatch_batch_to_destination com max_items=500.

    Verifica que o dispatcher chama send_batch exatamente 20 vezes
    (ceil(10000/500) = 20 chunks).
    """
    dest_id = seeded_destination

    send_call_sizes: list[int] = []

    async def capture_send(batch: list) -> DeliveryResult:
        send_call_sizes.append(len(batch))
        return DeliveryResult(accepted=len(batch))

    fake_target = AsyncMock()
    fake_target.send_batch.side_effect = capture_send

    import fakeredis.aioredis as fakeredis_aio

    # max_items=500 via delivery JSON no destino semeado
    with (
        patch("backend.app.core.secrets.get_default_backend", return_value=None),
        patch(
            "backend.app.collectors.output.destination_cache.get_destination",
            new_callable=AsyncMock,
            return_value=fake_target,
        ),
        patch(
            "backend.app.collectors.celery_app.get_worker_redis",
            return_value=fakeredis_aio.FakeRedis(decode_responses=True),
        ),
    ):
        from backend.app.collectors.pipeline import dispatch_batch_to_destination

        batch = _batch(10_000)
        await dispatch_batch_to_destination(dest_id, batch)

    total_delivered = sum(send_call_sizes)
    assert total_delivered == 10_000, (
        f"Todos 10.000 eventos devem ser entregues; got {total_delivered}"
    )
    # Padrão de delivery padrão (max_items=500): 20 chunks
    assert len(send_call_sizes) == 20, (
        f"10.000/500 = 20 chunks esperados, got {len(send_call_sizes)}: {send_call_sizes}"
    )
    # Nenhum chunk maior que max_items
    assert max(send_call_sizes) <= 500, (
        f"Nenhum chunk deve exceder max_items=500; max observado: {max(send_call_sizes)}"
    )


# ── (b) Concorrência por destino ≤ cap do semáforo ───────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency_cap", [1, 2, 4, 8])
async def test_semaphore_cap_never_exceeded(concurrency_cap: int) -> None:
    """Múltiplos senders concorrentes no mesmo destino não excedem o cap.

    Usa um sender fake que registra quantos sends simultâneos estão ativos.
    O máximo simultâneo observado deve ser <= concurrency_cap.
    """
    from backend.app.collectors.output.concurrency_pool import get_semaphore, reset

    # Limpa o pool para não herdar semáforos de outros testes
    reset()

    max_concurrent = 0
    current_concurrent = 0
    lock = asyncio.Lock()

    async def instrumented_send() -> DeliveryResult:
        nonlocal max_concurrent, current_concurrent
        async with lock:
            current_concurrent += 1
            if current_concurrent > max_concurrent:
                max_concurrent = current_concurrent
        # Cede o loop para permitir que outras coroutines avancem
        await asyncio.sleep(0)
        async with lock:
            current_concurrent -= 1
        return DeliveryResult(accepted=1)

    dest_id = f"sem-dest-cap{concurrency_cap}"
    sem = get_semaphore(dest_id, concurrency_cap)

    # Lança N tasks concorrentes (N > cap) — só cap delas podem estar dentro do sem
    n_tasks = concurrency_cap * 3 + 5
    results = await asyncio.gather(
        *[
            _send_with_sem(sem, instrumented_send)
            for _ in range(n_tasks)
        ]
    )

    assert max_concurrent <= concurrency_cap, (
        f"cap={concurrency_cap}: concorrência máxima observada={max_concurrent} "
        f"excede o cap"
    )
    assert max_concurrent > 0, "Pelo menos uma execução deve ter ocorrido"
    assert len(results) == n_tasks, f"Todas {n_tasks} tasks devem completar"

    reset()


async def _send_with_sem(sem: asyncio.Semaphore, send_fn: Any) -> DeliveryResult:
    """Adquire o semáforo e chama send_fn — replica o padrão do dispatcher."""
    async with sem:
        return await send_fn()


@pytest.mark.asyncio
async def test_slow_destination_does_not_starve_fast_destination() -> None:
    """Destino A (lento) não bloqueia destino B (rápido).

    Cada destino tem seu próprio semáforo — o isolamento de bulkhead E5
    garante que B entrega mesmo enquanto A está bloqueado.
    """
    from backend.app.collectors.output.concurrency_pool import get_semaphore, reset

    reset()

    a_entered = asyncio.Event()
    b_completed = asyncio.Event()
    unblock_a = asyncio.Event()

    async def slow_send_a() -> DeliveryResult:
        a_entered.set()
        await unblock_a.wait()
        return DeliveryResult(accepted=1)

    async def fast_send_b() -> DeliveryResult:
        b_completed.set()
        return DeliveryResult(accepted=1)

    sem_a = get_semaphore("slow-dest-a", 1)
    sem_b = get_semaphore("fast-dest-b", 1)

    async def task_a() -> None:
        async with sem_a:
            await slow_send_a()

    async def task_b() -> None:
        async with sem_b:
            await fast_send_b()

    # Inicia A (vai bloquear) e B em paralelo
    task_a_coro = asyncio.create_task(task_a())
    await a_entered.wait()  # A está bloqueado dentro do sem

    # B deve completar enquanto A está preso
    task_b_coro = asyncio.create_task(task_b())
    await asyncio.wait_for(b_completed.wait(), timeout=1.0)
    assert b_completed.is_set(), "B deve completar enquanto A está bloqueado"

    # Desbloqueia A
    unblock_a.set()
    await task_a_coro
    await task_b_coro

    reset()


@pytest.mark.asyncio
async def test_semaphore_cap_respected_via_dispatcher(
    static_db, seeded_destination
) -> None:
    """Concorrência real via dispatch_batch_to_destination com cap=2.

    Semeia destino com concurrency=2 e lança 6 calls concorrentes.
    Valida que no máximo 2 send_batch rodam simultaneamente.
    """
    import json

    TestingSessionLocal, _ = static_db
    dest_id = seeded_destination

    # Atualiza delivery com concurrency=2
    with TestingSessionLocal() as session:
        row = session.get(models.Destination, dest_id)
        row.delivery = json.dumps({
            "concurrency": 2,
            "retry": {"max_retries": 0},
            "breaker": {"failure_threshold": 100},
        })
        session.commit()

    max_concurrent = 0
    current_concurrent = 0
    lock = asyncio.Lock()
    gate = asyncio.Event()

    from backend.app.collectors.output.concurrency_pool import reset
    reset()  # garante semáforo limpo para este destino

    async def instrumented_send(batch: list) -> DeliveryResult:
        nonlocal max_concurrent, current_concurrent
        async with lock:
            current_concurrent += 1
            max_concurrent = max(max_concurrent, current_concurrent)
        await gate.wait()  # todos ficam presos até liberar
        async with lock:
            current_concurrent -= 1
        return DeliveryResult(accepted=len(batch))

    import fakeredis
    import fakeredis.aioredis as fakeredis_aio

    server = fakeredis.FakeServer()

    fake_target = AsyncMock()
    fake_target.send_batch.side_effect = instrumented_send

    def _redis_client():
        return fakeredis_aio.FakeRedis(decode_responses=True, server=server)

    with (
        patch("backend.app.core.secrets.get_default_backend", return_value=None),
        patch(
            "backend.app.collectors.output.destination_cache.get_destination",
            new_callable=AsyncMock,
            return_value=fake_target,
        ),
        patch(
            "backend.app.collectors.celery_app.get_worker_redis",
            side_effect=_redis_client,
        ),
    ):
        from backend.app.collectors.pipeline import dispatch_batch_to_destination

        # 6 tasks concorrentes — cap = 2
        tasks = [
            asyncio.create_task(
                dispatch_batch_to_destination(dest_id, [_event(i)])
            )
            for i in range(6)
        ]

        # Aguarda pelo menos 2 entrarem no send antes de liberar
        while True:
            async with lock:
                if current_concurrent >= 2:
                    break
            await asyncio.sleep(0.01)

        gate.set()  # libera todos

        results = await asyncio.gather(*tasks, return_exceptions=True)

    errors = [r for r in results if isinstance(r, Exception)]
    assert not errors, f"Nenhum dispatch deve falhar: {errors}"

    assert max_concurrent <= 2, (
        f"cap=2: concorrência máxima observada={max_concurrent} excede o cap"
    )
    assert max_concurrent > 0, "Pelo menos um send deve ter ocorrido"

    reset()


# ── (c) Soak: muitos lotes em sequência sem vazamento de estado ───────────────


@pytest.mark.asyncio
async def test_soak_sequential_batches_stable_counters() -> None:
    """N lotes em sequência → contadores lineares, sem acúmulo de estado."""
    N_BATCHES = 50
    BATCH_SIZE = 100

    send_count = 0

    async def counting_send(batch: list) -> DeliveryResult:
        nonlocal send_count
        send_count += len(batch)
        return DeliveryResult(accepted=len(batch))

    target = AsyncMock()
    target.send_batch.side_effect = counting_send

    dcfg = _dcfg(max_items=BATCH_SIZE)
    dc = _dest_config_mock()
    cb = _stub_circuit_breaker()
    labels = {"destination_id": dc.destination_id, "kind": dc.kind}
    m = _stub_metrics()

    from backend.app.collectors.pipeline import _send_chunk_with_retry

    for batch_idx in range(N_BATCHES):
        batch = _batch(BATCH_SIZE)
        await _send_chunk_with_retry(
            target=target,
            chunk=batch,
            dcfg=dcfg,
            dest_config=dc,
            labels=labels,
            redis=AsyncMock(),
            circuit_breaker=cb,
            persist_rejected_to_dlq=MagicMock(return_value=True),
            DELIVERY_LATENCY=m,
            DLQ_TOTAL=m,
            EVENTS_REJECTED=m,
            EVENTS_SENT=m,
            BYTES_SENT=m,
            RETRIES=m,
        )

    expected_total = N_BATCHES * BATCH_SIZE
    assert send_count == expected_total, (
        f"Soak: esperado {expected_total} eventos enviados, got {send_count}"
    )
    assert target.send_batch.call_count == N_BATCHES, (
        f"Soak: esperado {N_BATCHES} calls ao send_batch, "
        f"got {target.send_batch.call_count}"
    )


@pytest.mark.asyncio
async def test_soak_semaphore_pool_no_accumulation() -> None:
    """Pool de semáforos não acumula entradas: após N destinos distintos,
    o número de semáforos no pool é exatamente o número de destinos únicos."""
    from backend.app.collectors.output import concurrency_pool
    from backend.app.collectors.output.concurrency_pool import get_semaphore, reset

    reset()

    N_DESTINATIONS = 20
    dest_ids = [f"soak-dest-{i:03d}" for i in range(N_DESTINATIONS)]

    # Cria semáforos para destinos distintos
    for dest_id in dest_ids:
        get_semaphore(dest_id, 4)

    assert len(concurrency_pool._pool) == N_DESTINATIONS, (
        f"Pool deve ter exatamente {N_DESTINATIONS} entradas; "
        f"got {len(concurrency_pool._pool)}"
    )

    # Re-acessar os mesmos destinos não cria novas entradas
    for dest_id in dest_ids:
        get_semaphore(dest_id, 4)

    assert len(concurrency_pool._pool) == N_DESTINATIONS, (
        "Re-acesso ao mesmo destino não deve criar entrada duplicada no pool"
    )

    reset()
    assert len(concurrency_pool._pool) == 0, "reset() deve limpar o pool"


@pytest.mark.asyncio
async def test_soak_via_dispatcher_no_state_leak(
    static_db, seeded_destination
) -> None:
    """50 lotes sequenciais via dispatch_batch_to_destination.

    Valida que:
    - Todos os eventos são entregues (contadores lineares).
    - Nenhum estado vaza entre lotes (DLQ permanece vazia).
    """
    TestingSessionLocal, _ = static_db
    dest_id = seeded_destination

    N_BATCHES = 50
    EVENTS_PER_BATCH = 10

    total_sent = 0
    batch_counts: list[int] = []

    async def soak_send(batch: list) -> DeliveryResult:
        nonlocal total_sent
        total_sent += len(batch)
        batch_counts.append(len(batch))
        return DeliveryResult(accepted=len(batch))

    fake_target = AsyncMock()
    fake_target.send_batch.side_effect = soak_send

    import fakeredis.aioredis as fakeredis_aio

    with (
        patch("backend.app.core.secrets.get_default_backend", return_value=None),
        patch(
            "backend.app.collectors.output.destination_cache.get_destination",
            new_callable=AsyncMock,
            return_value=fake_target,
        ),
        patch(
            "backend.app.collectors.celery_app.get_worker_redis",
            return_value=fakeredis_aio.FakeRedis(decode_responses=True),
        ),
    ):
        from backend.app.collectors.pipeline import dispatch_batch_to_destination

        for batch_idx in range(N_BATCHES):
            # IDs únicos entre lotes para evitar ruído de DLQ dedup
            events = [_event(batch_idx * EVENTS_PER_BATCH + i) for i in range(EVENTS_PER_BATCH)]
            await dispatch_batch_to_destination(dest_id, events)

    # Todos os eventos entregues
    assert total_sent == N_BATCHES * EVENTS_PER_BATCH, (
        f"Soak via dispatcher: esperado {N_BATCHES * EVENTS_PER_BATCH} "
        f"eventos; got {total_sent}"
    )

    # Contadores lineares: cada batch com EVENTS_PER_BATCH (max_items padrão=500 > 10)
    assert all(c == EVENTS_PER_BATCH for c in batch_counts), (
        f"Todos os lotes devem ter {EVENTS_PER_BATCH} eventos; "
        f"distribuição observada: {set(batch_counts)}"
    )

    # Sem vazamento: DLQ permanece vazia
    with TestingSessionLocal() as session:
        dlq_count = (
            session.query(models.DestinationDeadLetter)
            .filter(models.DestinationDeadLetter.destination_id == dest_id)
            .count()
        )
    assert dlq_count == 0, (
        f"Soak: DLQ deve estar vazia após {N_BATCHES} lotes bem-sucedidos; "
        f"got {dlq_count} linhas"
    )
