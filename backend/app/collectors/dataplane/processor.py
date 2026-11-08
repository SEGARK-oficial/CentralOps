"""Papel ``processor`` — consome o tópico raw e roda o pipeline (ADR-0018).

Desde a Onda 3 chama o estágio de processamento direto
(``pipeline.process_raw_events`` → ``StreamProcessor``), sem o ciclo de coleta no
meio: nada de token, cursor, coletor instanciado ou sessão HTTP por lote. O raw
recebe eventos push (ingest HTTP, syslog) e, das integrações em modo raw, o que os
coletores pull buscaram.

Semântica:
  * o offset só é commitado DEPOIS que o ciclo terminou — os eventos já saíram
    para o tópico de entrega (ou para a quarentena/DLQ do próprio ciclo);
  * falha do ciclo → ``seek`` + backoff, até ``KAFKA_MAX_DELIVERY_ATTEMPTS``;
    esgotado, o lote vai à DLQ ``__raw__`` e só então o offset avança;
  * lote que não foi processado (integração inativa, coletor não registrado)
    → DLQ ``__raw__`` (``integration_unavailable``), nunca um commit mudo;
  * a DLQ que não grava segura o offset (o lote continua no tópico).

Vários processors tratam o mesmo (integração, stream) em partições diferentes, então
o processamento roda SEM a trava do stream e com o conjunto de claims pendentes
escopado pela partição: cada partição tem um consumidor por vez.

Entrypoint igual ao dispatcher (``python -c "...processor import main; main()"``):
na imagem Cython o módulo é ``.so`` e ``python -m`` não funciona.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ...core.config import settings
from .raw import decode_raw, raw_topic

logger = logging.getLogger(__name__)

_READY_FILE = "/tmp/processor-ready"
_HEARTBEAT_FILE = "/tmp/processor-heartbeat"
_DRAIN_DEADLINE_S = 900.0
_DLQ_FAILURE_BACKOFF_S = 10.0
RAW_DLQ_DESTINATION = "__raw__"


@dataclass
class _Group:
    """Registros CONSECUTIVOS de uma partição para o mesmo (integração, stream)."""

    first_offset: int
    last_offset: int
    integration_id: Optional[int] = None
    stream: str = ""
    events: List[Dict[str, Any]] = field(default_factory=list)
    # Paralelo a ``events``: id de dedupe gravado pelo coletor pull, "" quando o
    # registro não trouxe (push) — derivado no processamento.
    ids: List[str] = field(default_factory=list)
    invalid: bool = False


def _group_records(msgs: List[Any]) -> Iterator[_Group]:
    """Agrupa registros consecutivos do mesmo (integração, stream). Registro que não
    decodifica vira um grupo ``invalid`` próprio (descartado e contado)."""
    current: Optional[_Group] = None
    for msg in msgs:
        try:
            doc = decode_raw(msg.value)
        except Exception:  # noqa: BLE001 — poison de schema
            if current is not None:
                yield current
                current = None
            yield _Group(msg.offset, msg.offset, invalid=True)
            continue
        iid, stream = doc["integration_id"], doc["stream"]
        events = doc["events"]
        ids = doc.get("ids") or [""] * len(events)
        if current is not None and (current.integration_id, current.stream) == (iid, stream):
            current.events.extend(events)
            current.ids.extend(ids)
            current.last_offset = msg.offset
            continue
        if current is not None:
            yield current
        current = _Group(msg.offset, msg.offset, iid, stream, list(events), list(ids))
    if current is not None:
        yield current


#: Janela de dedupe em memória por partição (ADR-0018 Onda 3c). Uma partição tem
#: um consumidor por vez, então a janela vive no processo que a detém. Mantida
#: depois de um rebalance: se a partição voltar, os ids vistos continuam válidos.
_DEDUPE_WINDOWS: Dict[int, Any] = {}


def _window_for(partition: int) -> Any:
    window = _DEDUPE_WINDOWS.get(partition)
    if window is None:
        from ..state.dedupe_window import DedupeWindow

        window = DedupeWindow(
            max_ids=settings.RAW_DEDUPE_WINDOW_MAX_IDS,
            ttl_seconds=settings.RAW_DEDUPE_WINDOW_SECONDS,
        )
        _DEDUPE_WINDOWS[partition] = window
    return window


async def process_group(
    integration_id: int,
    stream: str,
    events: List[Dict[str, Any]],
    partition: int,
    ids: Optional[List[str]] = None,
) -> bool:
    """Processa ``events`` no estágio de processamento. Devolve se o lote foi
    PROCESSADO (False = integração inativa ou coletor não registrado). Levanta se
    o processamento falhou."""
    from .. import pipeline

    return await pipeline.process_raw_events(
        integration_id, stream, events, ids=ids, scope=f"raw-p{partition}",
        dedupe=_window_for(partition),
    )


def _touch(path: str) -> None:
    from .kafka_transport import _touch_health

    _touch_health(path)


async def _persist_raw_dlq(group: _Group, error_kind: str, detail: str) -> bool:
    from ..delivery import persist_batch_dlq

    try:
        ok = await asyncio.to_thread(
            persist_batch_dlq,
            group.events,
            destination_id=RAW_DLQ_DESTINATION,
            error_kind=error_kind,
            organization_id=None,
            error_detail=f"integration={group.integration_id} stream={group.stream}: {detail}",
        )
    except Exception:  # noqa: BLE001
        logger.exception("processor: falha ao gravar DLQ __raw__")
        return False
    return ok is True


async def run_raw_processor(stop_event: Optional[threading.Event] = None) -> None:
    from aiokafka import AIOKafkaConsumer

    from .. import metrics
    from .kafka_transport import _client_kwargs, owns_partition
    from .raw import _ensure_raw_topic

    await asyncio.to_thread(_ensure_raw_topic)
    # Configuração por snapshot com last-known-good (ADR-0018 Onda 3b): um por
    # processo, vivo enquanto o consumer roda. Tira o Postgres do caminho de cada
    # lote e mantém o processamento de pé com o banco fora.
    from ..config_snapshot import ACTIVE, SnapshotCache

    ACTIVE.set(
        SnapshotCache(
            ttl_seconds=settings.CONFIG_SNAPSHOT_TTL_SECONDS,
            max_stale_seconds=settings.CONFIG_SNAPSHOT_MAX_STALE_SECONDS,
        )
    )
    consumer = AIOKafkaConsumer(
        raw_topic(),
        **_client_kwargs(),
        group_id=settings.KAFKA_PROCESSOR_GROUP,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        max_partition_fetch_bytes=settings.KAFKA_MAX_REQUEST_BYTES,
        max_poll_interval_ms=settings.KAFKA_MAX_POLL_INTERVAL_MS,
    )
    await consumer.start()
    _touch(_READY_FILE)
    logger.info(
        "processor: consumindo topic=%s group=%s", raw_topic(), settings.KAFKA_PROCESSOR_GROUP
    )
    attempts: Dict[Tuple[int, int], int] = {}

    async def _lag() -> None:
        while True:
            await asyncio.sleep(max(1, settings.KAFKA_LAG_REFRESH_SECONDS))
            _touch(_HEARTBEAT_FILE)
            try:
                assigned = consumer.assignment()
                if not assigned:
                    continue
                end = await consumer.end_offsets(assigned)
                for tp in assigned:
                    pos = await consumer.position(tp)
                    hw = end.get(tp)
                    if hw is not None and pos is not None:
                        metrics.RAW_CONSUMER_LAG.labels(partition=str(tp.partition)).set(
                            max(0, hw - pos)
                        )
            except Exception:  # pragma: no cover — lag é advisory
                pass

    lag_task = asyncio.create_task(_lag())
    try:
        while not (stop_event is not None and stop_event.is_set()):
            batches = await consumer.getmany(
                timeout_ms=1000, max_records=settings.RAW_PROCESSOR_MAX_RECORDS
            )
            _touch(_HEARTBEAT_FILE)
            for tp, msgs in batches.items():
                for group in _group_records(msgs):
                    if not owns_partition(consumer, tp):
                        break  # revogada no rebalance: o novo dono processa o resto
                    done = await _handle_group(consumer, tp, group, attempts, metrics)
                    if not done:
                        break  # seek feito: o resto desta partição é relido depois
    finally:
        lag_task.cancel()
        try:
            await lag_task
        except BaseException:  # pragma: no cover
            pass
        await consumer.stop()


async def _handle_group(consumer: Any, tp: Any, group: _Group, attempts: Dict, metrics: Any) -> bool:
    """Processa um grupo e decide o offset. True = commitado (segue); False = seek."""

    from .kafka_transport import safe_commit, safe_seek

    async def _commit() -> None:
        # Partição revogada num rebalance durante o ciclo: não commita (o novo dono
        # relê; o dedupe absorve) em vez de derrubar o processo.
        await safe_commit(consumer, tp, group.last_offset + 1)

    if group.invalid:
        logger.error("processor: registro raw malformado (offset=%s) — descartado", group.first_offset)
        metrics.RAW_PROCESSED.labels(outcome="invalid").inc()
        await _commit()
        return True

    key = (tp.partition, group.first_offset)
    error: Optional[BaseException] = None
    consumed = False
    try:
        consumed = await process_group(
            group.integration_id, group.stream, group.events, tp.partition, ids=group.ids
        )
    except Exception as exc:  # noqa: BLE001 — classificado abaixo
        error = exc

    if error is None and consumed:
        attempts.pop(key, None)
        metrics.RAW_PROCESSED.labels(outcome="ok").inc(len(group.events))
        await _commit()
        return True

    if error is None:
        # O lote não foi processado: integração inativa/apagada ou coletor não
        # registrado. Commitar seria perda muda — vai à DLQ __raw__.
        kind, detail = "integration_unavailable", "integração inativa ou sem coletor"
    else:
        attempts[key] = attempts.get(key, 0) + 1
        if attempts[key] <= settings.KAFKA_MAX_DELIVERY_ATTEMPTS:
            logger.warning(
                "processor: ciclo falhou integration=%s stream=%s offset=%s (tentativa %d): %s",
                group.integration_id, group.stream, group.first_offset, attempts[key],
                type(error).__name__,
            )
            metrics.RAW_PROCESSED.labels(outcome="retry").inc()
            safe_seek(consumer, tp, group.first_offset)
            await asyncio.sleep(float(min(2 ** attempts[key], 30)))
            return False
        kind, detail = "processing_failed", f"{type(error).__name__}: {error}"[:500]

    if await _persist_raw_dlq(group, kind, detail):
        attempts.pop(key, None)
        metrics.RAW_PROCESSED.labels(outcome="dlq").inc(len(group.events))
        await _commit()
        return True
    metrics.RAW_PROCESSED.labels(outcome="dlq_failed").inc()
    safe_seek(consumer, tp, group.first_offset)
    await asyncio.sleep(_DLQ_FAILURE_BACKOFF_S)
    return False


async def _amain() -> None:
    stop_flag = threading.Event()
    task = asyncio.create_task(run_raw_processor(stop_event=stop_flag))
    sig_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, sig_event.set)
        except NotImplementedError:  # pragma: no cover
            signal.signal(sig, lambda *_: sig_event.set())
    await asyncio.wait(
        {task, asyncio.create_task(sig_event.wait())}, return_when=asyncio.FIRST_COMPLETED
    )
    if not task.done():
        logger.info("processor: sinal recebido — termina o lote em voo e sai")
        stop_flag.set()
        try:
            await asyncio.wait_for(task, timeout=_DRAIN_DEADLINE_S)
        except asyncio.TimeoutError:  # pragma: no cover
            task.cancel()
    elif task.exception() is not None:
        raise task.exception()


def main() -> None:
    from .dispatcher import _init_observability

    logging.basicConfig(level=logging.INFO)
    _init_observability()
    logger.info("processor: iniciando consumer do tópico raw")
    asyncio.run(_amain())


if __name__ == "__main__":  # pragma: no cover
    main()
