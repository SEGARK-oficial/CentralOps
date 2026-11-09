"""Store-and-forward por destino no Kafka (ADR-0018 Onda 4a).

Destino fora do ar não pode virar linha no Postgres. Antes, cada lote de um
destino com o disjuntor aberto ia para a DLQ do banco, evento a evento: 24 h de
queda a 10k eps seriam ~864 milhões de linhas, e a volta do destino dependia de
um dreno manual. Agora o lote ESTACIONA num tópico próprio e sai sozinho quando o
destino volta:

* :func:`park` — o ``dispatcher`` grava o registro de entrega como veio, com
  ``acks=all``, no tópico ``<prefix>.deliver.parked`` e só então commita o offset
  do tópico de entrega. Cada destino cai sempre na MESMA partição do
  estacionamento (``crc32(destino) % partições``), o que mantém um destino fora
  do caminho dos outros.
* :func:`run_parked_consumer` — o drenador (grupo ``centralops-parked``, no
  mesmo processo do ``dispatcher``) tenta entregar cada lote estacionado. Com o
  destino ainda fora, ele volta o offset e PAUSA aquela partição por um ciclo do
  disjuntor; as outras partições seguem. Lote só sai do estacionamento entregue
  (commit) ou recusado como poison pelo próprio despacho (DLQ). Erro transitório
  nunca vira DLQ aqui: o lote espera no Kafka até a retenção do tópico. Lote de
  destino com entrega em ordem traz ``ord_seq``; entregá-lo libera o caminho
  direto do destino (``ordered.release``, Onda 4e).

A escolha (Questão 3 do ADR): pausar a partição do tópico de entrega só isola
destinos com uma partição para cada destino ativo, e grupo de consumo por destino
grande custa um grupo dinâmico por destino. O estacionamento tira o destino fora
do caminho no primeiro lote recusado, com uma cópia por lote estacionado.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import zlib
from typing import Any, Dict, Optional

from ...core.config import settings

logger = logging.getLogger(__name__)


def parked_topic() -> str:
    return f"{settings.KAFKA_TOPIC_PREFIX}.{settings.KAFKA_PARKED_TOPIC}"


def destination_base(destination_id: str) -> int:
    """Partição-base estável do destino no estacionamento."""
    return zlib.crc32(str(destination_id).encode("utf-8"))


async def ensure_parked_topic() -> None:
    from .kafka_transport import _deliver_topic_configs, _ensure_topic

    configs = dict(_deliver_topic_configs())
    configs["retention.ms"] = str(int(settings.KAFKA_PARKED_RETENTION_MS))
    await _ensure_topic(
        parked_topic(),
        int(settings.KAFKA_PARKED_PARTITIONS),
        int(settings.KAFKA_DELIVER_REPLICATION or 1),
        configs,
    )


async def park(value: bytes, destination_id: str, reason: str) -> bool:
    """Grava o registro de entrega no estacionamento e espera o ack. True = gravado
    (o chamador pode commitar o offset de entrega); False = não gravou (não
    commitar: o lote segue no tópico de entrega e é relido)."""
    from .. import metrics
    from .kafka_transport import _producer_thread

    def _produce() -> None:
        # ``_produce_value`` não sobe o producer sozinho (quem sobe é o
        # ``produce`` de entrega, e o processo do dispatcher não produz entrega).
        _producer_thread._ensure_started()
        _producer_thread._produce_value(
            str(destination_id),
            value,
            str(destination_id).encode("utf-8"),
            parked_topic(),
            spread=(destination_base(destination_id), 0),
        )

    try:
        await asyncio.to_thread(_produce)
    except Exception:  # noqa: BLE001 — o lote fica no tópico de entrega
        logger.exception(
            "data-plane: falha ao estacionar lote dest=%s (%s) — mantido no tópico de entrega",
            destination_id, reason,
        )
        return False
    metrics.PARKED.labels(reason=reason).inc()
    return True


async def run_parked_consumer(
    stop_event: Optional[threading.Event] = None,
    *,
    hold_seconds: Optional[float] = None,
) -> None:
    """Drenador do estacionamento. Cooperativo como o consumer de entrega: o
    ``stop_event`` faz o laço sair depois do registro corrente."""
    from aiokafka import AIOKafkaConsumer
    from aiokafka.structs import TopicPartition

    from .. import circuit_breaker, metrics
    from ..pipeline import DISPATCH_REDIS, dispatch_batch_to_destination
    from . import ordered
    from .kafka_transport import (
        _client_kwargs,
        decode_delivery,
        group_timeouts,
        owns_partition,
        safe_commit,
        safe_seek,
    )

    hold = float(hold_seconds if hold_seconds is not None else settings.KAFKA_PARKED_HOLD_SECONDS)
    await ensure_parked_topic()
    consumer = AIOKafkaConsumer(
        parked_topic(),
        **_client_kwargs(),
        group_id=settings.KAFKA_PARKED_GROUP,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        max_partition_fetch_bytes=settings.KAFKA_MAX_REQUEST_BYTES,
        max_poll_interval_ms=settings.KAFKA_MAX_POLL_INTERVAL_MS,
        **group_timeouts(),
    )
    await consumer.start()
    logger.info(
        "data-plane: drenador do estacionamento consumindo topic=%s group=%s",
        parked_topic(), settings.KAFKA_PARKED_GROUP,
    )
    loop = asyncio.get_running_loop()
    timers: Dict[Any, asyncio.TimerHandle] = {}
    attempts: Dict[tuple, int] = {}

    def _resume(tp: Any) -> None:
        timers.pop(tp, None)
        if owns_partition(consumer, tp):
            consumer.resume(tp)

    def _hold(tp: Any, offset: int, delay: float) -> None:
        """Volta o offset e tira a partição do fetch por ``delay`` s: só os
        destinos dela esperam; as outras partições seguem drenando."""
        safe_seek(consumer, tp, offset)
        consumer.pause(tp)
        old = timers.pop(tp, None)
        if old is not None:
            old.cancel()
        timers[tp] = loop.call_later(delay, _resume, tp)

    async def _lag() -> None:
        while True:
            await asyncio.sleep(max(1, settings.KAFKA_LAG_REFRESH_SECONDS))
            try:
                assigned = consumer.assignment()
                if not assigned:
                    continue
                end = await consumer.end_offsets(assigned)
                for tp in assigned:
                    pos = await consumer.position(tp)
                    if end.get(tp) is not None and pos is not None:
                        metrics.PARKED_LAG.labels(partition=str(tp.partition)).set(
                            max(0, end[tp] - pos)
                        )
            except Exception:  # pragma: no cover — lag é advisory
                pass

    lag_task = asyncio.create_task(_lag())
    # Um cliente Redis para o processo, como no consumer de entrega (Onda 3f).
    from ..celery_app import get_worker_redis

    shared_redis = get_worker_redis()
    redis_token = DISPATCH_REDIS.set(shared_redis)
    try:
        while not (stop_event is not None and stop_event.is_set()):
            try:
                msg = await asyncio.wait_for(consumer.getone(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except StopAsyncIteration:
                break
            tp = TopicPartition(msg.topic, msg.partition)
            try:
                payload = decode_delivery(msg.value)
                dest_id = payload["destination_id"]
                batch = payload["batch"]
                if not isinstance(dest_id, str) or not isinstance(batch, list):
                    raise ValueError("envelope sem destination_id/batch")
            except Exception:  # noqa: BLE001 — não veio do dispatcher: poison
                logger.exception(
                    "data-plane: registro estacionado inválido (offset=%s) — descartado",
                    msg.offset,
                )
                metrics.PARKED_DRAINED.labels(outcome="invalid").inc()
                await safe_commit(consumer, tp, msg.offset + 1)
                continue

            key = (tp.partition, msg.offset)
            try:
                await dispatch_batch_to_destination(dest_id, batch)
            except circuit_breaker.BreakerOpen:
                metrics.PARKED_DRAINED.labels(outcome="held").inc()
                _hold(tp, msg.offset, hold)
                continue
            except Exception as exc:  # noqa: BLE001 — transitório: segue estacionado
                attempts[key] = attempts.get(key, 0) + 1
                delay = min(float(2 ** attempts[key]), hold)
                logger.warning(
                    "data-plane: lote estacionado dest=%s offset=%s falhou (%s, tentativa %d) "
                    "— segue estacionado, nova tentativa em %.0f s",
                    dest_id, msg.offset, type(exc).__name__, attempts[key], delay,
                )
                metrics.PARKED_DRAINED.labels(outcome="retry").inc()
                _hold(tp, msg.offset, delay)
                continue
            attempts.pop(key, None)
            seq = ordered.sequence_of(payload)
            if seq is not None:
                # Destino com entrega em ordem (Onda 4e): o último estacionado
                # saiu, o caminho direto volta. ANTES do commit: uma queda entre
                # os dois re-entrega este lote, nunca deixa a marca órfã.
                try:
                    await ordered.release(shared_redis, dest_id, seq)
                except Exception:  # noqa: BLE001 — a marca sai no próximo lote
                    logger.warning(
                        "data-plane: não apaguei a marca de ordem dest=%s seq=%s",
                        dest_id, seq, exc_info=True,
                    )
            metrics.PARKED_DRAINED.labels(outcome="delivered").inc()
            await safe_commit(consumer, tp, msg.offset + 1)
    finally:
        lag_task.cancel()
        for handle in timers.values():
            handle.cancel()
        try:
            await lag_task
        except BaseException:  # pragma: no cover
            pass
        DISPATCH_REDIS.reset(redis_token)
        try:
            await shared_redis.aclose()
        except Exception:  # pragma: no cover — encerramento best-effort
            pass
        await consumer.stop()


__all__ = [
    "destination_base",
    "ensure_parked_topic",
    "park",
    "parked_topic",
    "run_parked_consumer",
]
