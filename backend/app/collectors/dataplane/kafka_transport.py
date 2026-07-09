"""Transporte de data-plane via Kafka/Redpanda.

Separa o TRANSPORTE de evento (data-plane durável) do control-plane (Celery+Redis:
scheduling, OAuth, breaker, rate-limit, observability — que PERMANECE no Celery). O
fan-out de roteamento PRODUZ cada sub-lote (por destino) num tópico único
``<prefix>.deliver`` (N partições, **key = destination_id** → mesma destinação na
mesma partição: ordem + head-of-line-blocking limitado, espelhando o
``DISPATCH_DEST_SHARDS`` do caminho Celery). Evita o anti-pattern "1 tópico por
destino" (cardinalidade explode com N tenants × M destinos). Um role ``dispatcher``
CONSOME o tópico e chama o despacho existente (``dispatch_batch_to_destination`` —
breaker/chunk/retry/DLQ reusados, sem reescrita).

**Producer (singleton em loop-thread).** O fan-out (``_enqueue_routed``) é SÍNCRONO
e é chamado tanto de contexto async (poller) quanto sync (backfill/scheduler/tasks).
Um aiokafka producer roda num event-loop em **thread dedicada**; o ``produce`` de
qualquer contexto é submetido via ``run_coroutine_threadsafe`` e aguarda o ack.
Isso dá um producer **persistente** (sem criar loop/conexão por ciclo) e
**context-agnostic**, com uma única lib Kafka (aiokafka) e sem C-extension.

**Semântica.** at-least-once + dedupe no destino por ``event_id``. Producer
idempotente (``enable_idempotence`` + ``acks=all``) evita duplicata na reentrega
dentro de uma sessão de producer. O consumer só commita o offset após o despacho
(transitório → reprocessa via ``seek``; terminal → já foi para a DLQ no dispatch).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import threading
import time
from typing import Any, Dict, Iterator, List, Mapping, Optional

from ...core.config import settings

logger = logging.getLogger(__name__)

_ENVELOPE_VERSION = 1

# ── Health files p/ probes do dispatcher (k8s) ────────────────────────────────
# O readiness aguarda READY (criado após consumer.start()); o liveness checa o
# mtime do HEARTBEAT (tocado a cada msg E pelo emissor de lag em background, p/
# que um dispatcher OCIOSO não seja morto). Best-effort: /tmp pode ser read-only.
_READY_FILE = "/tmp/dispatcher-ready"
_HEARTBEAT_FILE = "/tmp/dispatcher-heartbeat"


def _touch_health(path: str) -> None:
    try:
        with open(path, "w") as fh:
            fh.write(str(time.time()))
    except Exception:  # pragma: no cover — probe é best-effort
        pass


# ── Topic + client config ─────────────────────────────────────────────────────

def deliver_topic() -> str:
    """Nome completo do tópico de entrega (``<prefix>.<deliver>``)."""
    return f"{settings.KAFKA_TOPIC_PREFIX}.{settings.KAFKA_DELIVER_TOPIC}"


def _client_kwargs() -> Dict[str, Any]:
    """kwargs comuns de bootstrap/segurança para producer e consumer."""
    kwargs: Dict[str, Any] = {
        "bootstrap_servers": settings.KAFKA_BOOTSTRAP_SERVERS,
        "security_protocol": settings.KAFKA_SECURITY_PROTOCOL,
    }
    if settings.KAFKA_SASL_MECHANISM:
        kwargs.update(
            sasl_mechanism=settings.KAFKA_SASL_MECHANISM,
            sasl_plain_username=settings.KAFKA_SASL_USERNAME,
            sasl_plain_password=settings.KAFKA_SASL_PASSWORD,
        )
    return kwargs


async def _ensure_deliver_topic() -> None:
    """Cria o tópico de entrega com ``KAFKA_DELIVER_PARTITIONS`` partições, de
    forma IDEMPOTENTE. Sem isto o broker auto-cria com 1 partição (default
    Redpanda/MSK): o fan-out por key=destination_id colapsa numa partição, a ordem
    e o HoL-blocking limitado se perdem, e o KEDA fica preso em 1 réplica (o scaler
    Kafka não passa do nº de partições do group). Em MSK (auto-create OFF) o 1º
    produce falharia — aqui criamos explicitamente em todos os alvos de deploy."""
    from aiokafka.admin import AIOKafkaAdminClient, NewTopic
    from aiokafka.errors import TopicAlreadyExistsError

    admin = AIOKafkaAdminClient(**_client_kwargs())
    try:
        await admin.start()
        await admin.create_topics(
            [
                NewTopic(
                    deliver_topic(),
                    num_partitions=settings.KAFKA_DELIVER_PARTITIONS,
                    replication_factor=settings.KAFKA_DELIVER_REPLICATION,
                )
            ]
        )
        logger.info(
            "data-plane: tópico %s garantido (partitions=%d rf=%d)",
            deliver_topic(),
            settings.KAFKA_DELIVER_PARTITIONS,
            settings.KAFKA_DELIVER_REPLICATION,
        )
    except TopicAlreadyExistsError:
        pass  # idempotente — já existe (note: aumentar partições exige repartição manual)
    except Exception:  # pragma: no cover — não bloqueia o boot; broker pode auto-criar
        logger.warning(
            "data-plane: ensure-topic falhou p/ %s (segue) — confira partições no broker",
            deliver_topic(),
            exc_info=True,
        )
    finally:
        try:
            await admin.close()
        except Exception:  # pragma: no cover
            pass


# ── Codec do envelope de entrega ──────────────────────────────────────────────

def encode_delivery(
    destination_id: str,
    batch: List[Dict[str, Any]],
    trace_context: Optional[Mapping[str, str]] = None,
) -> bytes:
    """Serializa ``{destination_id, batch, trace}`` para o valor da mensagem."""
    return json.dumps(
        {
            "v": _ENVELOPE_VERSION,
            "destination_id": destination_id,
            "batch": batch,
            "trace": dict(trace_context) if trace_context else {},
        },
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def decode_delivery(raw: bytes) -> Dict[str, Any]:
    """Desserializa o valor da mensagem. Levanta em payload inválido (vai ao log)."""
    return json.loads(raw)


def _compression_type() -> Optional[str]:
    """``KAFKA_COMPRESSION_TYPE`` normalizado p/ o aiokafka (``"none"`` → ``None``)."""
    ct = (settings.KAFKA_COMPRESSION_TYPE or "none").strip().lower()
    return None if ct in ("", "none") else ct


def _frame_batch(
    destination_id: str,
    batch: List[Dict[str, Any]],
    trace_context: Optional[Mapping[str, str]],
    limit: int,
) -> Iterator[bytes]:
    """Enquadra ``batch`` em uma ou mais mensagens, cada uma ≤ ``limit`` bytes (RAW).

    Split por bisseção até caber ou sobrar 1 evento — garante que NENHUM registro
    produzido estoure o limite do broker (== ``KAFKA_MAX_REQUEST_BYTES``), eliminando
    o ``MessageSizeTooLargeError`` por lote grande. Um único evento que ainda exceda
    é emitido sozinho (a compressão pode trazê-lo p/ baixo; senão o broker rejeita e
    o caller manda à DLQ). O split é por TAMANHO RAW, então cabe mesmo sem compressão.
    """
    if not batch:
        return
    value = encode_delivery(destination_id, batch, trace_context)
    if len(value) <= limit or len(batch) == 1:
        yield value
        return
    mid = len(batch) // 2
    yield from _frame_batch(destination_id, batch[:mid], trace_context, limit)
    yield from _frame_batch(destination_id, batch[mid:], trace_context, limit)


def _is_fatal_producer_error(exc: BaseException) -> bool:
    """True p/ erros que ENVENENAM o producer idempotente (a sequência quebra e todo
    send seguinte falha) — exige recriar o producer. Anda pela cadeia de causas pois
    o erro chega embrulhado (wait_for/run_coroutine_threadsafe)."""
    try:
        from aiokafka.errors import (  # import tardio (SDK opcional)
            DuplicateSequenceNumber,
            OutOfOrderSequenceNumber,
            ProducerFenced,
            UnknownProducerId,
        )

        fatal = (
            OutOfOrderSequenceNumber,
            ProducerFenced,
            UnknownProducerId,
            DuplicateSequenceNumber,
        )
    except Exception:  # pragma: no cover — sem SDK não há producer p/ envenenar
        return False
    cur: Optional[BaseException] = exc
    for _ in range(6):  # guard anti-ciclo
        if cur is None:
            break
        if isinstance(cur, fatal):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


# ── Producer singleton (event-loop em thread dedicada) ────────────────────────

class _ProducerThread:
    """Mantém um aiokafka producer vivo num loop de fundo; aceita produce de
    qualquer contexto (sync/async) via ``run_coroutine_threadsafe``."""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._producer: Any = None
        self._lock = threading.Lock()

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        loop.run_forever()

    async def _start_producer(self) -> Any:
        from aiokafka import AIOKafkaProducer

        # Garante o tópico com N partições ANTES do 1º produce — senão o broker
        # auto-cria com 1 partição (KEDA preso em 1 réplica + ordem/HoL quebrados).
        await _ensure_deliver_topic()
        producer = AIOKafkaProducer(
            **_client_kwargs(),
            enable_idempotence=True,
            acks="all",
            compression_type=_compression_type(),
            max_request_size=settings.KAFKA_MAX_REQUEST_BYTES,
            # Timeouts EXPLÍCITOS p/ falhar rápido em broker down/lento —
            # senão aiokafka usa ~40s default e o produce trava o hot-path de coleta.
            request_timeout_ms=settings.KAFKA_REQUEST_TIMEOUT_MS,
            metadata_max_age_ms=settings.KAFKA_METADATA_MAX_AGE_MS,
            value_serializer=None,  # já enviamos bytes
            key_serializer=None,
        )
        await producer.start()
        return producer

    async def _bounded_send(self, value: bytes, key: bytes) -> Any:
        """``send_and_wait`` com teto INTERNO (``asyncio.wait_for``) — garante que
        o cancelamento propague p/ a corrotina mesmo no wait cross-thread."""
        return await asyncio.wait_for(
            self._producer.send_and_wait(deliver_topic(), value=value, key=key),
            timeout=settings.KAFKA_PRODUCE_WAIT_S,
        )

    def _ensure_started(self) -> None:
        if self._producer is not None:
            return
        with self._lock:
            if self._producer is not None:
                return
            # Sobe a thread/loop de fundo apenas no PRIMEIRO start (ou se o loop
            # morreu). Numa RECRIAÇÃO pós-erro fatal, o loop segue vivo — só (re)cria
            # o producer nele; jamais uma 2ª thread.
            if self._loop is None or not self._loop.is_running():
                self._thread = threading.Thread(
                    target=self._run_loop, name="kafka-dataplane-producer", daemon=True
                )
                self._thread.start()
                deadline = time.monotonic() + 10.0
                while self._loop is None or not self._loop.is_running():
                    if time.monotonic() > deadline:
                        raise RuntimeError("kafka data-plane producer loop não iniciou")
                    time.sleep(0.005)
            fut = asyncio.run_coroutine_threadsafe(self._start_producer(), self._loop)
            self._producer = fut.result(timeout=30)
            logger.info(
                "data-plane: producer Kafka iniciado (bootstrap=%s topic=%s)",
                settings.KAFKA_BOOTSTRAP_SERVERS, deliver_topic(),
            )

    def _recreate_producer(self) -> None:
        """Descarta o producer ENVENENADO (após erro fatal de idempotência) p/ que o
        próximo ``_ensure_started`` crie um novo no MESMO loop. Self-healing: sem isto,
        um ``OutOfOrderSequenceNumber`` derrubava TODO produce até reiniciar o processo.
        """
        with self._lock:
            old = self._producer
            self._producer = None
        if old is not None and self._loop is not None and self._loop.is_running():
            try:
                fut = asyncio.run_coroutine_threadsafe(old.stop(), self._loop)
                fut.result(timeout=10)
            except Exception:  # pragma: no cover — best-effort: o objetivo é descartar
                logger.warning(
                    "data-plane: stop do producer envenenado falhou (ignorado)"
                )
        logger.warning(
            "data-plane: producer descartado após erro fatal de idempotência; "
            "será recriado no próximo produce (self-heal)"
        )

    def produce(
        self,
        destination_id: str,
        batch: List[Dict[str, Any]],
        trace_context: Optional[Mapping[str, str]] = None,
    ) -> None:
        """Produz UM sub-lote ao tópico de entrega e BLOQUEIA até o ack (acks=all).

        Bloquear dá durabilidade (não perde o lote num crash pós-roteamento). O
        volume é por-destino-por-ciclo (não por evento), então o custo é aceitável.
        """
        self._ensure_started()
        key = destination_id.encode("utf-8")
        # Enquadramento por tamanho: divide o lote em N registros, cada um ≤ 90% do
        # teto (folga p/ framing) — nunca emite um registro acima de
        # KAFKA_MAX_REQUEST_BYTES (== limite do broker), eliminando o
        # MessageSizeTooLargeError por lote grande.
        limit = max(1, int(settings.KAFKA_MAX_REQUEST_BYTES * 0.9))
        for value in _frame_batch(destination_id, batch, trace_context, limit):
            self._produce_value(destination_id, value, key)

    def _produce_value(self, destination_id: str, value: bytes, key: bytes) -> None:
        """Envia UM registro já enquadrado e BLOQUEIA até o ack. Em erro FATAL de
        idempotência, recria o producer (self-heal) e propaga o erro p/ o caller."""
        # Telemetria: latência + outcome do produce. Métricas OTel são
        # thread-safe, então emitir da thread do loop é seguro. No-op se OTel off.
        from ..metrics import DATAPLANE_PRODUCED, DATAPLANE_PRODUCE_LATENCY

        _start = time.perf_counter()
        outcome = "ok"
        fut: Optional[concurrent.futures.Future] = None
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._bounded_send(value, key), self._loop
            )
            # +2s de folga sobre o teto INTERNO → o wait_for interno dispara
            # primeiro (cancela a send), e este result só colhe resultado/erro.
            fut.result(timeout=settings.KAFKA_PRODUCE_WAIT_S + 2)
        except Exception as exc:
            outcome = "error"
            # Cancela a corrotina órfã no loop-thread (senão segue rodando até o
            # delivery_timeout do aiokafka, vazando in-flight no broker down).
            if fut is not None:
                fut.cancel()
            # Self-heal: um erro fatal de idempotência (OutOfOrderSequenceNumber etc.)
            # envenena o producer — descarta p/ o próximo produce usar um novo.
            if _is_fatal_producer_error(exc):
                outcome = "fatal_recreate"
                self._recreate_producer()
            raise
        finally:
            DATAPLANE_PRODUCE_LATENCY.labels(destination_id=destination_id).observe(
                time.perf_counter() - _start
            )
            DATAPLANE_PRODUCED.labels(destination_id=destination_id, outcome=outcome).inc()

    def shutdown(self) -> None:
        if self._producer is None or self._loop is None:
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(self._producer.stop(), self._loop)
            fut.result(timeout=15)
        except Exception:  # pragma: no cover — best-effort no shutdown
            logger.warning("data-plane: falha ao parar producer Kafka", exc_info=True)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._producer = None


_producer_thread = _ProducerThread()


def produce_delivery(
    destination_id: str,
    batch: List[Dict[str, Any]],
    trace_context: Optional[Mapping[str, str]] = None,
) -> None:
    """Produz um sub-lote roteado ao tópico de entrega (data-plane Kafka)."""
    _producer_thread.produce(destination_id, batch, trace_context)


def shutdown_producer() -> None:
    """Flush + stop do producer (chamado no worker_process_shutdown)."""
    _producer_thread.shutdown()


# ── Consumer (role dispatcher) ────────────────────────────────────────────────

async def _emit_consumer_lag(consumer: Any, metrics: Any) -> None:
    """Emite o lag de TODAS as partições atribuídas (highwater − position).

    Usa ``end_offsets`` (não o ``highwater()`` cacheado) p/ obter o highwater atual
    mesmo numa partição OCIOSA — senão o gauge congela no último valor quando a
    partição zera, o pior momento p/ o SRE. Itera a atribuição inteira
    (não só a partição da msg corrente) p/ reportar partições sem tráfego."""
    assigned = consumer.assignment()
    if not assigned:
        return
    end = await consumer.end_offsets(assigned)
    for _tp in assigned:
        pos = await consumer.position(_tp)
        hw = end.get(_tp)
        if hw is not None and pos is not None:
            metrics.DATAPLANE_CONSUMER_LAG.labels(partition=str(_tp.partition)).set(
                max(0, hw - pos)
            )


async def run_dispatch_consumer(stop_event: Optional[threading.Event] = None) -> None:
    """Loop do consumer do role ``dispatcher``: consome o tópico de entrega e chama
    o despacho existente. at-least-once com commit manual EXPLÍCITO por offset:

      - sucesso                  → commita ``{tp: offset+1}``;
      - breaker OPEN             → lote à DLQ (error_kind=breaker_open) + commita;
      - transitório < N tentat.  → ``seek`` de volta + backoff (reprocessa);
      - transitório esgotado     → lote à DLQ (error_kind=exhausted) + commita
                                    (desbloqueia a partição — sem poison-trap infinito);
      - envelope inválido/malformado → descarta (outcome=invalid) + commita;
      - não-transitório          → rejeições já na DLQ pelo dispatch; commita.

    Cooperativo: ``stop_event`` faz o loop sair APÓS terminar+commitar o registro
    corrente (drain limpo). Lag é emitido por uma task de BACKGROUND sobre TODAS
    as partições atribuídas (não só a da msg corrente), p/ não congelar quando a
    partição zera. Heartbeat/readiness em /tmp alimentam as probes do k8s.
    """
    from aiokafka import AIOKafkaConsumer
    from aiokafka.errors import CommitFailedError
    from aiokafka.structs import TopicPartition

    from .. import circuit_breaker
    from ..delivery import persist_batch_dlq
    from ..pipeline import _batch_org_id, dispatch_batch_to_destination

    try:
        from ..delivery import TransientDeliveryError  # type: ignore
    except Exception:  # pragma: no cover — fallback se o símbolo mudar de lugar
        class TransientDeliveryError(Exception):  # type: ignore
            ...

    # Garante o tópico com N partições antes de subscrever.
    await _ensure_deliver_topic()

    consumer = AIOKafkaConsumer(
        deliver_topic(),
        **_client_kwargs(),
        group_id=settings.KAFKA_CONSUMER_GROUP,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        max_partition_fetch_bytes=settings.KAFKA_MAX_REQUEST_BYTES,
        # Folga p/ um dispatch lento não ser tido como "consumer morto" → rebalance
        # espúrio + re-entrega.
        max_poll_interval_ms=settings.KAFKA_MAX_POLL_INTERVAL_MS,
    )
    # Telemetria do data-plane: métricas de consumo/lag + continuidade
    # de trace (span FILHO do carrier W3C carregado na mensagem). No-op se OTel off.
    from .. import metrics, tracing

    await consumer.start()
    _touch_health(_READY_FILE)  # readiness: consumer.start() ok
    logger.info(
        "data-plane: dispatcher consumindo topic=%s group=%s",
        deliver_topic(), settings.KAFKA_CONSUMER_GROUP,
    )

    # Contador de tentativas transitórias por (partition, offset) — bound + DLQ.
    attempts: Dict[tuple, int] = {}

    async def _emit_lag() -> None:
        """Emite lag de TODAS as partições atribuídas em background e toca o
        heartbeat — roda mesmo com a partição ociosa/zerada."""
        while True:
            await asyncio.sleep(max(1, settings.KAFKA_LAG_REFRESH_SECONDS))
            _touch_health(_HEARTBEAT_FILE)
            try:
                await _emit_consumer_lag(consumer, metrics)
            except Exception:  # pragma: no cover — lag é advisory, nunca derruba o loop
                pass

    async def _commit(tp: "TopicPartition", offset: int) -> None:
        """Commit EXPLÍCITO do offset processado (não bare commit de todas as
        partições) + tolera falha pós-revoke no rebalance."""
        try:
            await consumer.commit({tp: offset + 1})
        except CommitFailedError:  # pragma: no cover — perdeu a partição no rebalance
            logger.warning(
                "data-plane: commit falhou (rebalance?) tp=%s offset=%s", tp, offset
            )

    lag_task = asyncio.create_task(_emit_lag())
    # Poll com timeout (em vez de ``async for``, que bloqueia): permite checar o
    # ``stop_event`` a cada tick → drain cooperativo PRONTO mesmo com a partição
    # ociosa, sem esperar o deadline de hard-cancel.
    _poll_timeout = 1.0
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                msg = await asyncio.wait_for(consumer.getone(), timeout=_poll_timeout)
            except asyncio.TimeoutError:
                continue  # nenhuma msg neste tick — re-checa stop_event no topo
            except StopAsyncIteration:
                break  # stream encerrado (fake/teste); o getone() real bloqueia
            _touch_health(_HEARTBEAT_FILE)  # progresso → liveness
            tp = TopicPartition(msg.topic, msg.partition)
            key = (tp.partition, msg.offset)

            # Decode + GUARD de schema: JSON inválido OU envelope sem batch/dest
            # válido = poison message → descarta+commita (não cai no except-genérico
            # que assumiria "DLQ no dispatch").
            try:
                payload = decode_delivery(msg.value)
            except Exception:
                logger.exception(
                    "data-plane: payload inválido (offset=%s) — descartado", msg.offset
                )
                metrics.DATAPLANE_CONSUMED.labels(outcome="invalid").inc()
                await _commit(tp, msg.offset)
                attempts.pop(key, None)
                continue

            dest_id = payload.get("destination_id")
            batch = payload.get("batch")
            if not isinstance(dest_id, str) or not dest_id or not isinstance(batch, list):
                logger.error(
                    "data-plane: envelope com schema inválido (offset=%s dest=%r) — descartado",
                    msg.offset, dest_id,
                )
                metrics.DATAPLANE_CONSUMED.labels(outcome="invalid").inc()
                await _commit(tp, msg.offset)
                attempts.pop(key, None)
                continue

            _start = time.perf_counter()
            outcome = "ok"
            backoff = 0.0  # >0 → retry transitório (seek já feito; dorme após medir)
            try:
                # Continua o trace fim-a-fim: span filho do traceparent propagado
                # pelo producer (fecha o gap do "hop Kafka" — SRE vê collect→deliver).
                with tracing.span_with_parent(
                    "dataplane.dispatch",
                    payload.get("trace"),
                    **{"centralops.destination_id": str(dest_id)},
                ):
                    await dispatch_batch_to_destination(dest_id, batch)
                await _commit(tp, msg.offset)
                attempts.pop(key, None)
            except circuit_breaker.BreakerOpen:
                # Breaker OPEN: o lote NUNCA foi tentado e NÃO foi persistido pelo
                # dispatch. Espelha a lane Celery: DLQ (recuperável) ANTES de commitar,
                # senão os eventos somem.
                outcome = "breaker_open"
                await asyncio.to_thread(
                    persist_batch_dlq,
                    batch,
                    destination_id=str(dest_id),
                    error_kind="breaker_open",
                    organization_id=_batch_org_id(batch),
                )
                logger.warning(
                    "data-plane: breaker OPEN dest=%s — lote na DLQ (breaker_open)", dest_id
                )
                await _commit(tp, msg.offset)
                attempts.pop(key, None)
            except TransientDeliveryError:
                attempts[key] = attempts.get(key, 0) + 1
                if attempts[key] > settings.KAFKA_MAX_DELIVERY_ATTEMPTS:
                    # Esgotou: manda à DLQ + commita p/ DESBLOQUEAR a partição
                    # (sem isto, um destino transitório-permanente prende a partição
                    # em loop de seek e faz HoL-blocking nos co-tenants).
                    outcome = "exhausted"
                    logger.error(
                        "data-plane: transitório esgotado dest=%s offset=%s (%d tentativas) — DLQ",
                        dest_id, msg.offset, attempts[key],
                    )
                    await asyncio.to_thread(
                        persist_batch_dlq,
                        batch,
                        destination_id=str(dest_id),
                        error_kind="exhausted",
                        organization_id=_batch_org_id(batch),
                    )
                    attempts.pop(key, None)
                    await _commit(tp, msg.offset)
                else:
                    # at-least-once: volta a posição e reprocessa com backoff exponencial.
                    outcome = "transient"
                    consumer.seek(tp, msg.offset)
                    backoff = float(min(2 ** attempts[key], 30))
            except Exception:
                # Não-transitório (ex.: rejeições 4xx já persistidas na DLQ DENTRO do
                # dispatch). Commita p/ não reprocessar.
                outcome = "failed"
                logger.exception(
                    "data-plane: dispatch não-transitório dest=%s (rejeições já na DLQ pelo dispatch)",
                    dest_id,
                )
                await _commit(tp, msg.offset)
                attempts.pop(key, None)

            # Latência do ATTEMPT de dispatch — EXCLUI o backoff do transitório.
            metrics.DATAPLANE_CONSUME_LATENCY.observe(time.perf_counter() - _start)
            metrics.DATAPLANE_CONSUMED.labels(outcome=outcome).inc()

            if backoff:
                await asyncio.sleep(backoff)
            # stop_event é re-checado no TOPO do while (drain após este registro).
    finally:
        lag_task.cancel()
        try:
            await lag_task
        except BaseException:  # pragma: no cover — CancelledError no shutdown
            pass
        await consumer.stop()
