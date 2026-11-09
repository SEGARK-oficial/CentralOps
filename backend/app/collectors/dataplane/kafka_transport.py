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
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

from ...core.config import settings

logger = logging.getLogger(__name__)

_ENVELOPE_VERSION = 1

#: Espera antes de reler um lote cuja gravação na DLQ falhou (Postgres fora).
#: O lote continua no tópico; reler cedo demais só martela o banco caído.
_DLQ_FAILURE_BACKOFF_S = 10.0

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


def _ssl_context() -> Optional["ssl.SSLContext"]:
    """Contexto TLS do data-plane quando o protocolo usa SSL, senão ``None``.

    O aiokafka exige ``ssl_context`` para SSL/SASL_SSL e recusa o cliente sem ele
    — era o que impedia o data-plane de falar com broker gerenciado (ADR-0018
    Onda 1). ``KAFKA_SSL_CAFILE`` aponta a CA privada; vazio usa as CAs do
    sistema. ``KAFKA_SSL_VERIFY=false`` desliga a verificação (só laboratório)."""
    import ssl

    if "SSL" not in (settings.KAFKA_SECURITY_PROTOCOL or "").upper():
        return None
    ctx = ssl.create_default_context(cafile=settings.KAFKA_SSL_CAFILE or None)
    if not settings.KAFKA_SSL_VERIFY:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _deliver_topic_configs() -> Dict[str, str]:
    """Configs do tópico de entrega aplicadas na criação: ``min.insync.replicas``
    explícito, ou derivado do RF (RF ≥ 3 → 2; RF 2 → 1; RF 1 → nada)."""
    rf = int(settings.KAFKA_DELIVER_REPLICATION or 1)
    mir = settings.KAFKA_DELIVER_MIN_INSYNC_REPLICAS
    if mir is None:
        mir = 2 if rf >= 3 else (1 if rf == 2 else None)
    if not mir:
        return {}
    return {"min.insync.replicas": str(min(int(mir), rf))}


def _client_kwargs() -> Dict[str, Any]:
    """kwargs comuns de bootstrap/segurança para producer, consumer e admin."""
    kwargs: Dict[str, Any] = {
        "bootstrap_servers": settings.KAFKA_BOOTSTRAP_SERVERS,
        "security_protocol": settings.KAFKA_SECURITY_PROTOCOL,
    }
    ctx = _ssl_context()
    if ctx is not None:
        kwargs["ssl_context"] = ctx
    if settings.KAFKA_SASL_MECHANISM:
        kwargs.update(
            sasl_mechanism=settings.KAFKA_SASL_MECHANISM,
            sasl_plain_username=settings.KAFKA_SASL_USERNAME,
            sasl_plain_password=settings.KAFKA_SASL_PASSWORD,
        )
    return kwargs


async def _ensure_topic(
    name: str, partitions: int, replication: int, configs: Dict[str, str]
) -> None:
    """Cria ``name`` de forma IDEMPOTENTE (já existe = ok). Falha não bloqueia o
    boot: o broker pode auto-criar, e o log diz para conferir as partições."""
    from aiokafka.admin import AIOKafkaAdminClient, NewTopic
    from aiokafka.errors import TopicAlreadyExistsError

    admin = AIOKafkaAdminClient(**_client_kwargs())
    try:
        await admin.start()
        await admin.create_topics(
            [
                NewTopic(
                    name,
                    num_partitions=partitions,
                    replication_factor=replication,
                    topic_configs=configs,
                )
            ]
        )
        logger.info(
            "data-plane: tópico %s garantido (partitions=%d rf=%d configs=%s)",
            name, partitions, replication, configs,
        )
    except TopicAlreadyExistsError:
        pass
    except Exception:  # pragma: no cover — não bloqueia o boot
        logger.warning(
            "data-plane: ensure-topic falhou p/ %s (segue) — confira partições no broker",
            name, exc_info=True,
        )
    finally:
        try:
            await admin.close()
        except Exception:  # pragma: no cover
            pass


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
                    topic_configs=_deliver_topic_configs(),
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


def _frames(
    destination_id: str,
    batch: List[Dict[str, Any]],
    trace_context: Optional[Mapping[str, str]],
    limit: int,
) -> Iterator[tuple]:
    """``(sub_lote, valor)`` de cada registro de :func:`_frame_batch`.

    O sub-lote acompanha o valor porque quem produz precisa saber QUAIS eventos
    estão num registro recusado — para re-dividir, ou para mandar à DLQ só o
    evento que não cabe.
    """
    if not batch:
        return
    value = encode_delivery(destination_id, batch, trace_context)
    if len(value) <= limit or len(batch) == 1:
        yield batch, value
        return
    mid = len(batch) // 2
    yield from _frames(destination_id, batch[:mid], trace_context, limit)
    yield from _frames(destination_id, batch[mid:], trace_context, limit)


def _frame_batch(
    destination_id: str,
    batch: List[Dict[str, Any]],
    trace_context: Optional[Mapping[str, str]],
    limit: int,
) -> Iterator[bytes]:
    """Enquadra ``batch`` em uma ou mais mensagens, cada uma ≤ ``limit`` bytes (RAW).

    Split por bisseção até caber ou sobrar 1 evento — garante que NENHUM registro
    com 2+ eventos estoure o limite, eliminando o ``MessageSizeTooLargeError`` por
    lote grande. Um único evento que ainda exceda é emitido sozinho, e quem produz
    o desvia para a DLQ (ver ``_ProducerThread.produce``). A compressão NÃO o
    salva: o aiokafka mede ``key + value`` ANTES de comprimir
    (``AIOKafkaProducer._serialize``), então um evento acima do teto falha no
    cliente com qualquer ``KAFKA_COMPRESSION_TYPE``.
    """
    for _sub_batch, value in _frames(destination_id, batch, trace_context, limit):
        yield value


#: Folga sobre ``len(key) + len(value)`` na pré-checagem do teto. O aiokafka soma
#: o overhead de registro do formato legado (dezenas de bytes) antes de comparar
#: com ``max_request_size``; 512 cobre com sobra sem recusar evento que caberia.
_RECORD_OVERHEAD_SLACK = 512


def _exceeds_request_limit(value: bytes, key: bytes) -> bool:
    """True se o aiokafka vai recusar este registro no cliente, por tamanho."""
    return len(value) + len(key) + _RECORD_OVERHEAD_SLACK > settings.KAFKA_MAX_REQUEST_BYTES


def _is_message_too_large(exc: BaseException) -> bool:
    """True se o registro foi recusado por TAMANHO — no cliente (o check de
    ``max_request_size``) ou no broker (``max.message.bytes`` menor que o cliente).

    É permanente para aquele payload: retentar só repete a recusa. Anda pela cadeia
    de causas como :func:`_is_fatal_producer_error`; classifica por tipo E por nome
    porque os fakes de teste não importam o SDK.
    """
    try:
        from aiokafka.errors import (  # import tardio (SDK opcional)
            MessageSizeTooLargeError,
            RecordListTooLargeError,
            RecordTooLargeError,
        )

        too_large: tuple = (
            MessageSizeTooLargeError,
            RecordListTooLargeError,
            RecordTooLargeError,
        )
    except Exception:  # pragma: no cover — sem SDK, só o nome decide
        too_large = ()
    cur: Optional[BaseException] = exc
    for _ in range(6):  # guard anti-ciclo
        if cur is None:
            break
        if too_large and isinstance(cur, too_large):
            return True
        name = type(cur).__name__.lower()
        if any(k in name for k in ("messagesizetoolarge", "recordtoolarge", "recordlisttoolarge")):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


class OversizedEventDivertError(RuntimeError):
    """Um evento não cabe no tópico E a DLQ não conseguiu guardá-lo.

    Levantar é a escolha certa aqui: seguir em frente perderia o evento em
    silêncio. O ciclo de coleta reverte e tenta de novo, e na próxima vez a DLQ
    (banco) provavelmente já voltou.
    """


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

    async def _bounded_send(
        self,
        value: bytes,
        key: bytes,
        topic: Optional[str] = None,
        spread: Optional[Tuple[int, int]] = None,
    ) -> Any:
        """``send_and_wait`` com teto INTERNO (``asyncio.wait_for``) — garante que
        o cancelamento propague p/ a corrotina mesmo no wait cross-thread.

        ``spread=(base, shard)`` escolhe a partição EXPLICITAMENTE: a de índice
        ``(base + shard) % partições`` (ADR-0018 Onda 3f). Pelo hash da chave, os
        shards de um mesmo stream colidiam — 4 shards caíam em 3 de 8 partições,
        uma delas com metade do tráfego, e o stream não escalava além de ~2
        processors. Assim, shards distintos de um stream nunca dividem partição."""
        target = topic or deliver_topic()

        async def _send() -> Any:
            if spread is None:
                # Entrega (key=destination_id): partição pelo hash da chave, como sempre.
                return await self._producer.send_and_wait(target, value=value, key=key)
            parts = sorted(await self._producer.partitions_for(target))
            base, shard = spread
            return await self._producer.send_and_wait(
                target, value=value, key=key, partition=parts[(base + shard) % len(parts)]
            )

        return await asyncio.wait_for(_send(), timeout=settings.KAFKA_PRODUCE_WAIT_S)

    def run_in_loop(self, coro: Any, timeout: float) -> Any:
        """Roda ``coro`` no loop do producer e espera o resultado (sobe o producer
        se preciso). Usado para preparar outros tópicos no mesmo cliente."""
        self._ensure_started()
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

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

        **Evento maior que o teto NÃO sobe como exceção.** Antes subia, e o ciclo de
        coleta revertia o cursor e soltava as claims: o ciclo seguinte buscava o
        MESMO evento, falhava no MESMO ponto, e o coletor ficava parado nele para
        sempre (incidente out/2026, ``MessageSizeTooLargeError`` de 1,16 MB contra o
        teto de 1 MiB). Agora esse evento vai para a DLQ do destino com
        ``error_kind="size"`` e o resto do lote segue. O dreno da DLQ despacha
        direto ao destino, sem passar pelo Kafka, então o evento continua
        entregável. Erro transitório (broker fora) continua subindo: aí reverter e
        tentar de novo é o comportamento certo.
        """
        self._ensure_started()
        key = destination_id.encode("utf-8")
        # Enquadramento por tamanho: divide o lote em N registros, cada um ≤ 90% do
        # teto (folga p/ framing) — nunca emite um registro com 2+ eventos acima de
        # KAFKA_MAX_REQUEST_BYTES (== limite do broker).
        limit = max(1, int(settings.KAFKA_MAX_REQUEST_BYTES * 0.9))
        oversized: List[Dict[str, Any]] = []
        self._produce_framed(destination_id, batch, trace_context, key, limit, oversized)
        if oversized:
            _divert_oversized_to_dlq(destination_id, oversized, trace_context)

    def _produce_framed(
        self,
        destination_id: str,
        batch: List[Dict[str, Any]],
        trace_context: Optional[Mapping[str, str]],
        key: bytes,
        limit: int,
        oversized: List[Dict[str, Any]],
    ) -> None:
        """Produz ``batch`` em registros ≤ ``limit``; o que não cabe vai p/ ``oversized``.

        Duas recusas por tamanho são tratadas: a do CLIENTE (pré-checada, sem ida
        ao broker) e a do BROKER, quando o ``max.message.bytes`` dele é menor que o
        ``KAFKA_MAX_REQUEST_BYTES`` configurado aqui. Na do broker, um registro com
        2+ eventos é re-dividido com metade do tamanho que ele tinha; a recursão
        termina porque o limite cai pela metade a cada nível até sobrar 1 evento.
        """
        for sub_batch, value in _frames(destination_id, batch, trace_context, limit):
            if len(sub_batch) == 1 and _exceeds_request_limit(value, key):
                oversized.extend(sub_batch)
                continue
            try:
                self._produce_value(destination_id, value, key)
            except Exception as exc:
                if not _is_message_too_large(exc):
                    raise
                if len(sub_batch) == 1:
                    oversized.extend(sub_batch)
                    continue
                logger.warning(
                    "data-plane: broker recusou registro de %d bytes com %d eventos "
                    "(max.message.bytes do broker < KAFKA_MAX_REQUEST_BYTES=%d?) — "
                    "re-dividindo dest=%s",
                    len(value), len(sub_batch), settings.KAFKA_MAX_REQUEST_BYTES,
                    destination_id,
                )
                self._produce_framed(
                    destination_id, sub_batch, trace_context, key,
                    max(1, len(value) // 2), oversized,
                )

    def _produce_value(
        self,
        destination_id: str,
        value: bytes,
        key: bytes,
        topic: Optional[str] = None,
        spread: Optional[Tuple[int, int]] = None,
    ) -> None:
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
                self._bounded_send(value, key, topic, spread), self._loop
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


def _divert_oversized_to_dlq(
    destination_id: str,
    events: List[Dict[str, Any]],
    trace_context: Optional[Mapping[str, str]],
) -> None:
    """Grava na DLQ do destino cada evento que não cabe num registro Kafka.

    Uma linha por evento, cada uma com o PRÓPRIO tamanho no ``error_detail`` — é
    o número que o operador precisa para escolher entre reduzir o evento e subir
    o teto. O log leva só ``event_id`` e bytes, nunca o conteúdo.

    Se a DLQ falhar, levanta :class:`OversizedEventDivertError`: o evento não foi
    entregue nem guardado, e só reverter o ciclo evita perdê-lo em silêncio.
    """
    from ..delivery import persist_batch_dlq
    from ..metrics import DATAPLANE_PRODUCED
    from ..pipeline import _batch_org_id

    limit = settings.KAFKA_MAX_REQUEST_BYTES
    for event in events:
        size = len(encode_delivery(destination_id, [event], trace_context))
        event_id = str((event.get("_centralops") or {}).get("event_id") or "?")
        detail = (
            f"evento de {size} bytes excede o teto do data-plane Kafka "
            f"(KAFKA_MAX_REQUEST_BYTES={limit}) e NÃO foi enviado ao tópico. "
            "Reprocessar pela DLQ entrega direto ao destino, sem Kafka. Para não se "
            "repetir: reduza o evento (drop_raw na rota ou raw_reduction no mapping) "
            "ou suba KAFKA_MAX_REQUEST_BYTES junto com o max.message.bytes do broker."
        )
        logger.warning(
            "data-plane: evento %s de %d bytes > KAFKA_MAX_REQUEST_BYTES=%d — "
            "desviado p/ DLQ (error_kind=size) dest=%s; o ciclo de coleta segue",
            event_id, size, limit, destination_id,
        )
        ok = persist_batch_dlq(
            [event],
            destination_id=destination_id,
            error_kind="size",
            organization_id=_batch_org_id([event]),
            error_detail=detail,
        )
        if not ok:
            raise OversizedEventDivertError(
                f"evento {event_id} ({size} bytes) não cabe no tópico e a DLQ falhou "
                f"(dest={destination_id})"
            )
        DATAPLANE_PRODUCED.labels(destination_id=destination_id, outcome="oversize_dlq").inc()


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

def group_timeouts() -> Dict[str, int]:
    """Sessão, heartbeat e rebalance dos grupos de consumo (processor e
    dispatcher) — ver ``KAFKA_SESSION_TIMEOUT_MS`` em ``config``."""
    session = int(settings.KAFKA_SESSION_TIMEOUT_MS)
    return {
        "session_timeout_ms": session,
        "heartbeat_interval_ms": min(int(settings.KAFKA_HEARTBEAT_INTERVAL_MS), session // 3),
        "rebalance_timeout_ms": max(int(settings.KAFKA_REBALANCE_TIMEOUT_MS), session),
    }


def owns_partition(consumer: Any, tp: Any) -> bool:
    """True se ``tp`` ainda está atribuída a este consumer.

    Durante um lote longo, um rebalance pode tirar a partição deste processo. Aí
    ``commit``/``seek`` nela levantam ``IllegalStateError`` e derrubavam o consumer
    (visto no loadtest da Onda 2 ao subir réplicas). Partição revogada não é erro:
    o novo dono relê a partir do último offset commitado (at-least-once)."""
    try:
        return tp in consumer.assignment()
    except Exception:  # noqa: BLE001 — consumer parando
        return False


async def safe_commit(consumer: Any, tp: Any, next_offset: int) -> bool:
    """Commita ``tp`` em ``next_offset`` se a partição ainda é deste consumer.
    Devolve se commitou; revogada ou commit recusado no rebalance = False."""
    if not owns_partition(consumer, tp):
        logger.info("data-plane: partição %s revogada antes do commit — o novo dono relê", tp)
        return False
    try:
        from aiokafka.errors import CommitFailedError, IllegalStateError
    except Exception:  # pragma: no cover — SDK ausente (fakes de teste)
        CommitFailedError = IllegalStateError = RuntimeError  # type: ignore
    try:
        await consumer.commit({tp: next_offset})
        return True
    except (CommitFailedError, IllegalStateError):
        logger.warning("data-plane: commit falhou (rebalance?) tp=%s offset=%s", tp, next_offset)
        return False


def safe_seek(consumer: Any, tp: Any, offset: int) -> bool:
    """``seek`` só se a partição ainda é deste consumer (revogada = o novo dono relê)."""
    if not owns_partition(consumer, tp):
        return False
    consumer.seek(tp, offset)
    return True


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
      - erro NÃO classificado    → tratado como transitório (seek + backoff, depois DLQ);
      - DLQ não confirmou        → NÃO commita: seek + backoff, o lote segue no tópico;
      - envelope inválido/malformado → descarta (outcome=invalid) + commita.

    Invariante (ADR-0018 Onda 0): nenhum offset é commitado sem a entrega ou a
    gravação na DLQ confirmadas.

    Cooperativo: ``stop_event`` faz o loop sair APÓS terminar+commitar o registro
    corrente (drain limpo). Lag é emitido por uma task de BACKGROUND sobre TODAS
    as partições atribuídas (não só a da msg corrente), p/ não congelar quando a
    partição zera. Heartbeat/readiness em /tmp alimentam as probes do k8s.
    """
    from aiokafka import AIOKafkaConsumer
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
        **group_timeouts(),
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

    async def _persist_dlq(batch: list, dest_id: Any, error_kind: str) -> bool:
        """Grava o lote na DLQ e diz se GRAVOU. ``persist_batch_dlq`` devolve
        False em falha (não levanta); qualquer exceção também conta como falha.
        O chamador só commita o offset com True."""
        try:
            ok = await asyncio.to_thread(
                persist_batch_dlq,
                batch,
                destination_id=str(dest_id),
                error_kind=error_kind,
                organization_id=_batch_org_id(batch),
            )
        except Exception:  # noqa: BLE001 — falha da DLQ = não commitar
            logger.exception(
                "data-plane: falha ao gravar DLQ dest=%s kind=%s — lote mantido no tópico",
                dest_id, error_kind,
            )
            return False
        if ok is not True:
            logger.error(
                "data-plane: DLQ não confirmou dest=%s kind=%s — lote mantido no tópico",
                dest_id, error_kind,
            )
            return False
        return True

    async def _commit(tp: "TopicPartition", offset: int) -> None:
        """Commit EXPLÍCITO do offset processado (não bare commit de todas as
        partições) + tolera partição revogada no rebalance."""
        await safe_commit(consumer, tp, offset + 1)

    lag_task = asyncio.create_task(_emit_lag())
    # UM cliente Redis para o processo (ADR-0018 Onda 3f). O despacho criava um
    # por lote — o padrão das tasks Celery, cujo loop morre a cada task — e cada
    # cliente novo pagava conexão + ``importlib.metadata.version`` do redis-py:
    # ~1/4 do tempo do dispatcher no gate de carga. Aqui o loop vive o processo
    # inteiro, então o cliente (e o pool dele) também.
    from ..celery_app import get_worker_redis
    from ..pipeline import DISPATCH_REDIS

    shared_redis = get_worker_redis()
    redis_token = DISPATCH_REDIS.set(shared_redis)
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
                    span_kind="consumer",
                    **{
                        "centralops.destination_id": str(dest_id),
                        "centralops.batch_size": len(batch),
                        "messaging.system": "kafka",
                        "messaging.operation.type": "process",
                        "messaging.destination.name": getattr(msg, "topic", None),
                        "messaging.destination.partition.id": (
                            str(msg.partition) if getattr(msg, "partition", None) is not None else None
                        ),
                    },
                ):
                    outcome = await dispatch_batch_to_destination(dest_id, batch)
                    # Desvio para a DLQ sem exceção (destino ausente/de outro
                    # tenant, rejeição pelo sink) também é erro no span.
                    tracing.record_dispatch_outcome(outcome)
                await _commit(tp, msg.offset)
                attempts.pop(key, None)
            except circuit_breaker.BreakerOpen:
                # Breaker OPEN: o lote NUNCA foi tentado e NÃO foi persistido pelo
                # dispatch. DLQ (recuperável) ANTES de commitar — e só commita se a
                # DLQ CONFIRMAR a gravação (ADR-0018 Onda 0): com o Postgres fora,
                # o lote fica no Kafka e é relido, em vez de sumir.
                outcome = "breaker_open"
                if await _persist_dlq(batch, dest_id, "breaker_open"):
                    logger.warning(
                        "data-plane: breaker OPEN dest=%s — lote na DLQ (breaker_open)", dest_id
                    )
                    await _commit(tp, msg.offset)
                    attempts.pop(key, None)
                else:
                    outcome = "dlq_failed"
                    safe_seek(consumer, tp, msg.offset)
                    backoff = _DLQ_FAILURE_BACKOFF_S
            except Exception as exc:  # noqa: BLE001 — nenhum erro commita sem DLQ
                # TransientDeliveryError E qualquer erro NÃO classificado. Antes, o
                # não classificado caía num ramo que commitava supondo "rejeições já
                # na DLQ pelo dispatch" — falso para erro de conexão do sender, falha
                # do Postgres ao ler o destino, bug no formatter. Era perda silenciosa
                # (ADR-0018 Onda 0). Agora todo erro é re-tentado com backoff e, ao
                # esgotar, vai à DLQ; o commit só acontece com a DLQ confirmada.
                attempts[key] = attempts.get(key, 0) + 1
                if attempts[key] > settings.KAFKA_MAX_DELIVERY_ATTEMPTS:
                    # Esgotou: DLQ + commit p/ DESBLOQUEAR a partição (sem isto, um
                    # destino transitório-permanente prende a partição em loop de
                    # seek e faz HoL-blocking nos co-tenants).
                    outcome = "exhausted"
                    logger.error(
                        "data-plane: transitório esgotado dest=%s offset=%s (%d tentativas, %s) — DLQ",
                        dest_id, msg.offset, attempts[key], type(exc).__name__,
                    )
                    if await _persist_dlq(batch, dest_id, "exhausted"):
                        attempts.pop(key, None)
                        await _commit(tp, msg.offset)
                    else:
                        outcome = "dlq_failed"
                        safe_seek(consumer, tp, msg.offset)
                        backoff = _DLQ_FAILURE_BACKOFF_S
                else:
                    # at-least-once: volta a posição e reprocessa com backoff exponencial.
                    if isinstance(exc, TransientDeliveryError):
                        outcome = "transient"
                    else:
                        outcome = "unclassified"
                        logger.warning(
                            "data-plane: erro não classificado no dispatch dest=%s offset=%s "
                            "(tentativa %d) — tratado como transitório",
                            dest_id, msg.offset, attempts[key], exc_info=True,
                        )
                    safe_seek(consumer, tp, msg.offset)
                    backoff = float(min(2 ** attempts[key], 30))

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
        DISPATCH_REDIS.reset(redis_token)
        try:
            await shared_redis.aclose()
        except Exception:  # pragma: no cover — encerramento best-effort
            pass
        await consumer.stop()
