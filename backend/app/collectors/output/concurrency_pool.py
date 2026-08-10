"""Per-destination concurrency bulkhead — SECOND layer.

An ``asyncio.Semaphore`` per ``(destination_id, event_loop)``, sized from
``DeliveryConfig.concurrency``, acquired around ``send_batch`` so a single
destination cannot monopolise a loop's in-flight send capacity at the expense
of other destinations sharing that loop.

SCOPE — read carefully (prefork landmine):
  This semaphore is **per event-loop / per worker process**, NOT global. Under
  the Celery prefork pool each task runs ``asyncio.run`` (or submits to the
  per-process persistent loop) and a child process executes ONE task at a time,
  so within a single process the semaphore rarely contends. It becomes
  meaningful when multiple ``send_batch`` coroutines share one loop (the
  ``DISPATCH_PERSISTENT_LOOP`` runtime, future async-executor models, or tests).
  Left alone it would let ``W`` workers run ``W × concurrency`` concurrent sends
  against one sink — which is exactly why it is NOT the only layer.

  The cross-process ceiling is
  ``destination_limiter.DestinationLimiter`` — a Redis leaky-lease that caps
  concurrent ``send_batch`` for one destination across ALL worker processes and
  hosts. ``dispatch_batch_to_destination`` acquires the Redis lease FIRST and
  this semaphore SECOND, so this module is the cheap intra-loop fan-out bound
  underneath the global cap. Defence-in-depth alongside **hash-routing to shard
  queues** (``queues.dispatch_dest_shard_queue``) + per-shard worker deployment
  (OS-level bulkhead) + the **circuit breaker** (a hung destination trips OPEN
  and fast-fails instead of holding worker slots).

  The semaphore is event-loop-bound (``asyncio.Semaphore`` cannot be awaited
  across loops) — keyed by ``(destination_id, id(loop))`` mirroring
  ``destination_cache``'s loop tracking, so a fork/new-loop never reuses a
  stale semaphore.

REDIMENSIONAMENTO — por que o slot existe (correção de ago/2026):
  A versão anterior tratava mudança de ``concurrency`` **substituindo** a entrada
  do pool: quem já estava em voo continuava segurando o semáforo ANTIGO e quem
  chegava recebia um NOVO. Durante a sobreposição o teto efetivo virava
  ``cap_antigo + cap_novo``. O docstring chamava isso de "brief transient
  over-limit on a rare config bump is acceptable"; medido, não é nenhum dos três:

      8 entregas em voo sob concurrency=8, operador BAIXA para 2
      → pico observado de 10 sends simultâneos.

  Ou seja, baixar o limite AUMENTAVA a concorrência — exatamente ao contrário da
  intenção do operador, e no momento em que ele mais precisa do limite (o sink
  começou a devolver 429). E como ``_load_destination_config`` lê o DB a cada
  despacho, qualquer edição de config no console dispara a janela.

  Agora o teto é redimensionado **no lugar**, e a capacidade nunca excede o maior
  valor vigente:

  * subir  — libera os permits extras na hora (aumentar nunca viola nada);
  * baixar com o destino ocioso — troca o semáforo interno na hora;
  * baixar com entregas em voo — o permit devolvido em cada saída é ABSORVIDO em
    vez de liberado, até a capacidade convergir. O limite novo passa a valer à
    medida que o trabalho drena, sem nunca abrir uma vaga a mais.

  Consequência de contrato: ``get_semaphore`` devolve um **slot** (async context
  manager), não um ``asyncio.Semaphore`` cru. O uso — ``async with slot:`` — é
  idêntico, e o objeto continua sendo o MESMO entre chamadas com os mesmos
  parâmetros (a identidade que os testes de bulkhead verificam).
"""

from __future__ import annotations

import asyncio
from typing import Dict, Optional, Tuple


class DestinationSlot:
    """Bulkhead de UM destino num loop, com capacidade ajustável em voo.

    ``_pending`` conta **holders + waiters**, não só holders. A distinção não é
    zelo: uma corrotina suspensa em ``acquire()`` ainda não é holder, mas trocar
    o semáforo debaixo dela a deixaria esperando num objeto órfão enquanto os
    novos entram por outro — o mesmo over-limit que esta classe existe para
    eliminar, só que mais difícil de enxergar.
    """

    __slots__ = ("_sem", "_limit", "_target", "_pending")

    def __init__(self, limit: int) -> None:
        self._sem = asyncio.Semaphore(limit)
        self._limit = limit
        self._target = limit
        self._pending = 0

    @property
    def limit(self) -> int:
        """Capacidade VIGENTE (a que está de fato em vigor)."""
        return self._limit

    @property
    def target(self) -> int:
        """Capacidade desejada. Difere de :attr:`limit` enquanto uma redução drena."""
        return self._target

    @property
    def in_use(self) -> bool:
        return self._pending > 0

    def retarget(self, new_limit: int) -> None:
        """Aplica uma mudança de configuração SEM nunca exceder o teto vigente."""
        if new_limit < 1:
            new_limit = 1
        self._target = new_limit
        if new_limit == self._limit:
            return
        if self._pending == 0:
            # Ocioso: ninguém segura nem espera ⇒ trocar o semáforo é atômico do
            # ponto de vista do loop (não há await entre aqui e o próximo acquire).
            self._sem = asyncio.Semaphore(new_limit)
            self._limit = new_limit
            return
        if new_limit > self._limit:
            # Subir é sempre seguro: acrescenta permits ao semáforo em uso.
            for _ in range(new_limit - self._limit):
                self._sem.release()
            self._limit = new_limit
        # Baixar com trabalho em voo: nada a fazer agora. Cada ``__aexit__``
        # absorve um permit até ``_limit`` alcançar ``_target``. Abrir vaga
        # imediatamente é precisamente o bug que esta classe corrige.

    async def __aenter__(self) -> "DestinationSlot":
        # Incrementa ANTES do await: entre o incremento e a aquisição a corrotina
        # é waiter, e ``retarget`` precisa enxergá-la para não trocar o semáforo.
        self._pending += 1
        try:
            await self._sem.acquire()
        except BaseException:
            self._pending -= 1
            raise
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self._pending -= 1
        if self._limit > self._target:
            # Absorve o permit em vez de devolvê-lo: é assim que uma redução
            # entra em vigor sem jamais abrir uma vaga a mais.
            self._limit -= 1
        else:
            self._sem.release()
        return False


# destination_id → (slot, loop). Keyed by destination ALONE and SELF-HEALING
# (mirrors destination_cache): a new running loop (the default asyncio.run-per-task
# model closes its loop each task) REPLACES the entry, so the pool stays at one
# entry per destination and never accumulates stale closed-loop slots. We hold the
# actual loop object (not ``id(loop)``, which can be reused after GC) for an exact
# identity check. Mudança de ``concurrency`` NÃO troca mais a entrada — o slot se
# redimensiona (ver o docstring do módulo).
_pool: Dict[str, Tuple[DestinationSlot, asyncio.AbstractEventLoop]] = {}


def get_semaphore(destination_id: str, concurrency: int) -> DestinationSlot:
    """Return the per-destination bulkhead slot for the CURRENT loop.

    Self-heals: se a entrada em cache estava presa a outro loop (agora fechado),
    um slot novo a substitui — um semáforo de loop morto não pode ser aguardado, e
    seus holders não existem mais. Se apenas ``concurrency`` mudou, o MESMO slot é
    redimensionado (:meth:`DestinationSlot.retarget`), preservando a identidade e,
    principalmente, o teto.

    Must be called from within a running event loop.
    """
    if concurrency < 1:
        concurrency = 1
    loop = asyncio.get_running_loop()
    existing = _pool.get(destination_id)
    if existing is not None and existing[1] is loop:
        slot = existing[0]
        if slot.target != concurrency:
            slot.retarget(concurrency)
        return slot
    slot = DestinationSlot(concurrency)
    _pool[destination_id] = (slot, loop)
    return slot


def reset() -> None:
    """Drop all slots — test seam (mirrors destination_cache.reset)."""
    _pool.clear()
