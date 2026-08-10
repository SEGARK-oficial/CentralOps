"""per-destination bulkhead.

Two mechanisms:
  - hash-routing: each destination_id maps to a STABLE shard queue
    (dispatch.destination.0..N-1) so a slow destination saturates only its
    shard; an operator isolates it with a dedicated per-shard worker.
  - concurrency semaphore: caps concurrent send_batch for ONE destination on
    ONE loop (per-process; see concurrency_pool docstring). Proven here at the
    asyncio level where it is effective.
"""

from __future__ import annotations

import asyncio
import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest

from backend.app.collectors import queues
from backend.app.collectors.output import concurrency_pool


# ── Hash-routing ───────────────────────────────────────────────────────────


def test_shard_queue_is_stable() -> None:
    """Same destination_id always maps to the same shard (ordering/socket reuse)."""
    a = queues.dispatch_dest_shard_queue("splunk-prod-001")
    b = queues.dispatch_dest_shard_queue("splunk-prod-001")
    assert a == b
    assert a.startswith("dispatch.destination.")
    shard = int(a.rsplit(".", 1)[1])
    assert 0 <= shard < queues.DISPATCH_DEST_SHARDS


def test_shard_queue_distributes_across_shards() -> None:
    """A spread of destination ids hits more than one shard (not all in shard 0)."""
    seen = {
        queues.dispatch_dest_shard_queue(f"dest-{i:04d}")
        for i in range(200)
    }
    assert len(seen) >= queues.DISPATCH_DEST_SHARDS // 2
    assert seen <= set(queues.all_dispatch_dest_queues())


def test_all_shard_queues_registered_in_celery() -> None:
    """Every shard queue is declared so the worker can consume it."""
    from backend.app.collectors.celery_app import celery_app

    declared = {q.name for q in celery_app.conf.task_queues}
    for shard_q in queues.all_dispatch_dest_queues():
        assert shard_q in declared, f"{shard_q} not declared in task_queues"


def test_compose_dispatcher_consumes_all_shards() -> None:
    """The collector-dispatcher's -Q list in docker-compose MUST be a superset of
    all shard queues — else routed batches strand in the broker. This
    machine-enforces the constant↔compose contract so a shard-count bump can't
    silently drift."""
    import pathlib

    compose = (
        pathlib.Path(__file__).resolve().parents[4] / "compose" / "docker-compose.yml"
    )
    text = compose.read_text()
    # The real -Q value line (not the explanatory comment, which says "0..7").
    q_lines = [
        ln
        for ln in text.splitlines()
        if "dispatch.destination.0" in ln and not ln.lstrip().startswith("#")
    ]
    assert q_lines, "no dispatcher -Q line containing the destination shards found"
    line = q_lines[0]
    for shard_q in queues.all_dispatch_dest_queues():
        assert shard_q in line, (
            f"{shard_q} not consumed by the dispatcher -Q in docker-compose.yml — "
            f"shard-count drift would strand its batches"
        )


# ── Concurrency semaphore ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_semaphore_caps_concurrency() -> None:
    """concurrency=1 → two coroutines for the same destination serialize."""
    concurrency_pool.reset()
    order: list[str] = []

    async def worker(tag: str) -> None:
        sem = concurrency_pool.get_semaphore("dest-x", concurrency=1)
        async with sem:
            order.append(f"{tag}:enter")
            await asyncio.sleep(0.02)
            order.append(f"{tag}:exit")

    await asyncio.gather(worker("a"), worker("b"))
    # With cap=1 the two must not interleave: each enter is followed by its exit.
    assert order in (
        ["a:enter", "a:exit", "b:enter", "b:exit"],
        ["b:enter", "b:exit", "a:enter", "a:exit"],
    )


@pytest.mark.asyncio
async def test_semaphore_allows_parallel_up_to_limit() -> None:
    """concurrency=2 → two coroutines run concurrently (both enter before exit)."""
    concurrency_pool.reset()
    entered = 0
    max_concurrent = 0

    async def worker() -> None:
        nonlocal entered, max_concurrent
        sem = concurrency_pool.get_semaphore("dest-y", concurrency=2)
        async with sem:
            entered += 1
            max_concurrent = max(max_concurrent, entered)
            await asyncio.sleep(0.02)
            entered -= 1

    await asyncio.gather(*[worker() for _ in range(4)])
    assert max_concurrent == 2


@pytest.mark.asyncio
async def test_different_destinations_do_not_share_semaphore() -> None:
    """A slow destination's saturated semaphore must NOT block another dest."""
    concurrency_pool.reset()
    fast_done = asyncio.Event()

    async def slow() -> None:
        sem = concurrency_pool.get_semaphore("slow", concurrency=1)
        async with sem:
            await asyncio.sleep(0.2)

    async def fast() -> None:
        sem = concurrency_pool.get_semaphore("fast", concurrency=1)
        async with sem:
            fast_done.set()

    task_slow = asyncio.create_task(slow())
    await asyncio.sleep(0.01)  # let slow acquire first
    await asyncio.wait_for(fast(), timeout=0.1)  # fast must not be blocked by slow
    assert fast_done.is_set()
    await task_slow


@pytest.mark.asyncio
async def test_semaphore_resizes_on_concurrency_change() -> None:
    """Mudar ``concurrency`` REDIMENSIONA o slot — não o substitui.

    A versão anterior deste teste afirmava ``s3 is not s1``, codificando o
    MECANISMO (trocar a entrada do pool) em vez do comportamento. Esse mecanismo
    era justamente o defeito: quem estava em voo continuava no semáforo antigo e
    quem chegava recebia um novo, então o teto efetivo virava ``antigo + novo``
    (medido: baixar de 8 para 2 levava a concorrência a 10). Ver
    ``test_lowering_the_cap_never_raises_concurrency`` abaixo e o docstring do
    módulo. Agora a identidade é ESTÁVEL e o que muda é a capacidade.
    """
    concurrency_pool.reset()
    s1 = concurrency_pool.get_semaphore("d", concurrency=2)
    s2 = concurrency_pool.get_semaphore("d", concurrency=2)
    assert s1 is s2  # same limit → same object
    assert s1.limit == 2

    s3 = concurrency_pool.get_semaphore("d", concurrency=4)
    assert s3 is s1, "slot ocioso deve ser redimensionado, não substituído"
    assert s3.limit == 4, "a nova capacidade tem de estar em vigor"

    # E o redimensionamento é REAL, não só um número: 4 entram em paralelo.
    live = 0
    peak = 0
    gate = asyncio.Event()

    async def worker() -> None:
        nonlocal live, peak
        async with concurrency_pool.get_semaphore("d", concurrency=4):
            live += 1
            peak = max(peak, live)
            await gate.wait()
            live -= 1

    tasks = [asyncio.create_task(worker()) for _ in range(6)]
    await asyncio.sleep(0.02)
    gate.set()
    await asyncio.gather(*tasks)
    assert peak == 4


@pytest.mark.asyncio
async def test_lowering_the_cap_never_raises_concurrency() -> None:
    """Regressão: baixar o limite não pode AUMENTAR a concorrência.

    Bug de produção encontrado em ago/2026, provado deterministicamente aqui.
    ``_load_destination_config`` lê o DB a CADA despacho, então editar
    ``delivery.concurrency`` no console entra em vigor imediatamente para os
    despachos novos. Com o pool antigo, os 8 sends em voo seguiam no semáforo de
    8 permits enquanto os novos recebiam um semáforo de 2 → **10 simultâneos**.

    O cenário é o pior possível: o operador baixa o limite justamente porque o
    sink começou a devolver 429, e o efeito era o contrário do pedido.
    """
    concurrency_pool.reset()
    live = 0
    peak = 0
    gate = asyncio.Event()

    async def worker(cap: int) -> None:
        nonlocal live, peak
        async with concurrency_pool.get_semaphore("dest-prod", concurrency=cap):
            live += 1
            peak = max(peak, live)
            await gate.wait()
            live -= 1

    # 8 entregas em voo sob concurrency=8.
    a = [asyncio.create_task(worker(8)) for _ in range(8)]
    await asyncio.sleep(0.02)
    assert live == 8, "pré-condição: as 8 entregas precisam estar em voo"

    # Operador BAIXA para 2. Duas entregas novas chegam nessa janela.
    b = [asyncio.create_task(worker(2)) for _ in range(2)]
    await asyncio.sleep(0.02)

    gate.set()
    await asyncio.gather(*a, *b)

    assert peak <= 8, (
        f"pico={peak} excede o maior cap já configurado (8) — baixar o limite "
        "aumentou a concorrência"
    )


@pytest.mark.asyncio
async def test_lowered_cap_takes_effect_once_inflight_work_drains() -> None:
    """A redução não é só adiada — ela converge, e sem nunca abrir vaga a mais."""
    concurrency_pool.reset()
    gate = asyncio.Event()
    slot = concurrency_pool.get_semaphore("dest-drain", concurrency=4)

    async def hold() -> None:
        async with concurrency_pool.get_semaphore("dest-drain", concurrency=4):
            await gate.wait()

    tasks = [asyncio.create_task(hold()) for _ in range(4)]
    await asyncio.sleep(0.02)

    concurrency_pool.get_semaphore("dest-drain", concurrency=1)
    assert slot.target == 1
    assert slot.limit == 4, "com trabalho em voo a capacidade só cai ao drenar"

    gate.set()
    await asyncio.gather(*tasks)
    assert slot.limit == 1, "drenado ⇒ a capacidade nova está em vigor"

    # Prova o efeito: agora só UM entra por vez.
    live = 0
    peak = 0
    gate2 = asyncio.Event()

    async def worker() -> None:
        nonlocal live, peak
        async with concurrency_pool.get_semaphore("dest-drain", concurrency=1):
            live += 1
            peak = max(peak, live)
            await gate2.wait()
            live -= 1

    t2 = [asyncio.create_task(worker()) for _ in range(3)]
    await asyncio.sleep(0.02)
    gate2.set()
    await asyncio.gather(*t2)
    assert peak == 1
