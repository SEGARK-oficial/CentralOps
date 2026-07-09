"""Kind ``kafka`` — destino Apache Kafka / message bus.

Publica cada evento canônico como uma
mensagem num tópico Kafka. É a porta para "lake/stream bus" (Kafka, Redpanda,
MSK, Confluent, Event Hubs via endpoint Kafka) — o padrão de fan-out de dados
mais ubíquo fora do par SIEM/HEC.

**Particionamento + idempotência:** a ``key`` de cada mensagem é o
``event_id`` do namespace ``_centralops`` (UTF-8). Key estável dá
particionamento determinístico (todos os eventos de um id na mesma partição) e,
combinada com ``enable_idempotence=True`` + ``acks="all"``, garante
exactly-once no nível do producer numa reentrega do mesmo lote (sem duplicar a
mensagem no broker dentro de uma sessão de producer).

**Credencial SASL:** a senha (``sasl_password``) fica em ``secret_ref`` (cofre),
nunca na config. Sem credencial (destino dormant), ``send_batch``/``test``
falham de forma descritiva sem levantar — fail-closed controlado.

**Dependência opcional:** ``aiokafka`` está em ``requirements-sinks.txt``
e é importado de forma TARDIA dentro de ``_make_producer`` — o módulo registra
normalmente mesmo sem o pacote instalado. Ausente em runtime → send/test falham
com instrução clara ("instale aiokafka: pip install -r requirements-sinks.txt").

O ``KafkaClient`` satisfaz o protocolo ``Destination`` diretamente: define
``kind``, ``format``, ``send_batch``, ``test`` e ``close`` (sem embrulho via
``LegacyTargetDestination`` — resultado nativo por item).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, List, Literal, Mapping, Optional

from pydantic import BaseModel, Field

from ....core.config import settings
from .._fastjson import dumps_bytes as _json_dumps
from ..base import DeliveryResult, RejectedEvent, TestResult
from .registry import DestinationConfig, DestinationRegistration, register

logger = logging.getLogger(__name__)

KIND = "kafka"

# Mensagem clara quando o SDK opcional não está instalado.
_MISSING_AIOKAFKA = (
    "aiokafka não está instalado — destino kafka inativo. "
    "Instale: pip install -r requirements-sinks.txt"
)


class KafkaConfig(BaseModel):
    """Schema de config do destino Kafka (exposto no catálogo da UI).

    A credencial SASL (``password``) **não** está aqui: fica em ``secret_ref``
    (cofre de secrets, campo lógico ``sasl_password``). ``username`` é
    identidade, não segredo, então fica na config.
    """

    bootstrap_servers: str = Field(
        description="Lista CSV de brokers (ex: 'b1:9092,b2:9092')",
    )
    topic: str = Field(description="Tópico de destino das mensagens")
    security_protocol: Literal[
        "PLAINTEXT", "SSL", "SASL_PLAINTEXT", "SASL_SSL"
    ] = Field(
        default="SASL_SSL",
        description="Protocolo de segurança do broker",
    )
    sasl_mechanism: Literal["PLAIN", "SCRAM-SHA-256", "SCRAM-SHA-512"] = Field(
        default="PLAIN",
        description="Mecanismo SASL (quando security_protocol é SASL_*)",
    )
    username: Optional[str] = Field(
        default=None,
        description="Usuário SASL (a senha vem de secret_ref)",
    )
    verify_tls: bool = Field(
        default=True,
        description="Verificar certificado TLS (quando security_protocol usa SSL)",
    )
    acks: Literal["all", "1", "0"] = Field(
        default="all",
        description="Garantia de ack do producer ('all' = todas as réplicas ISR)",
    )


def _event_id(event: Mapping[str, Any]) -> str:
    """event_id do namespace ``_centralops`` (key da mensagem), ou '?'."""
    meta = event.get("_centralops") or {}
    return str(meta.get("event_id") or "?")


def _factory(config: DestinationConfig, secrets: Optional[Any] = None) -> "KafkaClient":
    """Constrói um ``KafkaClient`` a partir da config resolvida.

    A senha SASL é decifrada via ``secrets.decrypt(config.secret_ref)`` quando
    ambos presentes. Ausente (dormant) → ``password=None``; send/test falham de
    forma descritiva sem levantar aqui (fail-closed controlado). Mecanismos sem
    SASL (PLAINTEXT/SSL) dispensam credencial.
    """
    cfg = KafkaConfig(**dict(config.config or {}))

    # Fail-fast: SASL exige username não-vazio. Sem ele o aiokafka recebe
    # credencial nula → erro não-determinístico no broker; recusa na construção.
    if cfg.security_protocol.startswith("SASL") and not cfg.username:
        raise ValueError(
            "security_protocol=%s requer username não-vazio" % cfg.security_protocol
        )

    password: Optional[str] = None
    if secrets is not None and config.secret_ref:
        try:
            password = secrets.decrypt(config.secret_ref)
        except Exception as exc:
            # NÃO logar secret_ref nem a exceção: o erro de decrypt pode conter
            # o path da master key (KMS). Só o tipo da exceção.
            logger.warning(
                "kafka: falha ao decifrar secret_ref (%s) — password=None (dormant)",
                type(exc).__name__,
            )

    return KafkaClient(
        bootstrap_servers=cfg.bootstrap_servers,
        topic=cfg.topic,
        password=password,
        security_protocol=cfg.security_protocol,
        sasl_mechanism=cfg.sasl_mechanism,
        username=cfg.username,
        verify_tls=cfg.verify_tls,
        acks=cfg.acks,
    )


class KafkaClient:
    """Cliente Apache Kafka com producer aiokafka iniciado lazily.

    Satisfaz o protocolo ``Destination`` diretamente: define ``kind``,
    ``format``, ``send_batch``, ``test`` e ``close``.

    O producer é criado uma única vez (``_make_producer``) e reutilizado entre
    lotes — ``_ensure_producer`` é idempotente. ``_make_producer`` isola o
    import TARDIO de ``aiokafka`` (o módulo carrega sem o SDK) e é o ponto de
    override para testes (que não podem importar ``aiokafka``).
    """

    kind: str = "kafka"

    def __init__(
        self,
        bootstrap_servers: str,
        topic: str,
        *,
        password: Optional[str] = None,
        security_protocol: str = "SASL_SSL",
        sasl_mechanism: str = "PLAIN",
        username: Optional[str] = None,
        verify_tls: bool = True,
        acks: str = "all",
    ) -> None:
        self._bootstrap_servers = bootstrap_servers
        self._topic = topic
        self._password = password
        self._security_protocol = security_protocol
        self._sasl_mechanism = sasl_mechanism
        self._username = username
        self._verify_tls = verify_tls
        self._acks = acks
        self._producer: Any = None
        self._started = False
        self._lock = asyncio.Lock()

    # ── Formatação ───────────────────────────────────────────────────────

    def format(self, envelope: Mapping[str, Any]) -> bytes:
        """Value da mensagem: o envelope canônico serializado em JSON bytes.

        Usa ``_fastjson.dumps_bytes`` (orjson quando disponível) — compacto,
        UTF-8 bruto, ``default=str``. Usado pelo envio e por shadow/preview.
        """
        return _json_dumps(dict(envelope))

    # ── Producer (lazy, reutilizado) ─────────────────────────────────────

    def _build_ssl_context(self) -> Any:
        """Contexto SSL quando o protocolo usa TLS e ``verify_tls`` está ligado.

        ``verify_tls=False`` → devolve um contexto que não valida o certificado
        (laboratório / broker com cert self-signed). ``None`` quando o protocolo
        não usa SSL (PLAINTEXT/SASL_PLAINTEXT) — aiokafka dispensa contexto.
        """
        if "SSL" not in self._security_protocol:
            return None
        import ssl

        ctx = ssl.create_default_context()
        if not self._verify_tls:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _make_producer(self) -> Any:
        """Cria o ``AIOKafkaProducer`` (import TARDIO de aiokafka).

        Ponto único de criação do producer — isola a dependência opcional e é
        o ponto de override para testes (que monkeypatcham este método com um
        fake async producer, sem nunca importar aiokafka).
        """
        try:
            from aiokafka import AIOKafkaProducer  # import tardio
        except ImportError as exc:  # pragma: no cover — caminho sem SDK
            raise RuntimeError(_MISSING_AIOKAFKA) from exc

        kwargs: dict[str, Any] = {
            "bootstrap_servers": self._bootstrap_servers,
            "acks": self._acks,
            "enable_idempotence": True,
            "security_protocol": self._security_protocol,
            # Alinha o teto do cliente ao do broker (== max.message.bytes) — sem isto
            # o producer cai no default 1MiB do aiokafka e diverge silenciosamente do
            # broker. Compressão encolhe o fio (maior alavanca anti-MessageSizeTooLarge).
            "max_request_size": settings.KAFKA_MAX_REQUEST_BYTES,
            "compression_type": _compression_type(),
        }
        ssl_ctx = self._build_ssl_context()
        if ssl_ctx is not None:
            kwargs["ssl_context"] = ssl_ctx
        if self._security_protocol.startswith("SASL"):
            kwargs["sasl_mechanism"] = self._sasl_mechanism
            kwargs["sasl_plain_username"] = self._username
            kwargs["sasl_plain_password"] = self._password
        return AIOKafkaProducer(**kwargs)

    async def _ensure_producer(self) -> Any:
        """Garante o producer criado e iniciado (idempotente + thread-safe).

        Serializa a criação/início sob ``self._lock`` para que múltiplos
        ``send_batch`` concorrentes (o client é cacheado/reusado) não chamem
        ``start()`` duas vezes (corrompendo o producer). Se ``start()`` levanta,
        descarta o producer (``_producer=None``) para que a próxima tentativa
        recrie um producer novo — em vez de retentar o mesmo morto em loop.
        """
        async with self._lock:
            if self._producer is None:
                self._producer = self._make_producer()
            if not self._started:
                try:
                    await self._producer.start()
                    self._started = True
                except Exception:
                    self._producer = None  # reset p/ recriar na próxima
                    self._started = False
                    raise
        return self._producer

    # ── Envio ────────────────────────────────────────────────────────────

    async def send_batch(self, batch: List[Mapping[str, Any]]) -> DeliveryResult:
        """Publica o lote no tópico; um ``send_and_wait`` por evento.

        Cada mensagem leva ``value=format(ev)`` e ``key=event_id`` (UTF-8) —
        particionamento estável + idempotência com ``enable_idempotence``. Os
        envios são disparados concorrentemente e aguardados em conjunto.

        - Tudo ok → ``DeliveryResult.ok(len)``.
        - Erro de broker transitório (timeout/coordenador/conexão) → parcial
          aceito + ``retryable=True`` (lote re-tentado).
        - Erro de auth/config (SASL/autorização) → ``rejected`` com
          ``error_kind='auth'``, ``retryable=False`` (→ DLQ).
        Nunca levanta exceção.
        """
        if not batch:
            return DeliveryResult.ok(0)

        try:
            producer = await self._ensure_producer()
        except Exception as exc:  # SDK ausente / start falhou → transitório
            logger.warning("kafka: producer indisponível: %s", exc)
            return DeliveryResult(accepted=0, retryable=True)

        # Dispara os envios concorrentemente; aguarda em conjunto preservando
        # a ordem (paralelo a ``batch``) para mapear falhas ao evento certo.
        coros = [
            producer.send_and_wait(
                self._topic,
                value=self.format(ev),
                key=_event_id(ev).encode("utf-8"),
            )
            for ev in batch
        ]
        results = await asyncio.gather(*coros, return_exceptions=True)

        accepted = 0
        rejected: list[RejectedEvent] = []
        any_retryable = False
        fatal_seen = False
        for ev, res in zip(batch, results):
            if not isinstance(res, BaseException):
                accepted += 1
                continue
            if _is_auth_error(res):
                rejected.append(
                    RejectedEvent(
                        event_id=_event_id(ev),
                        reason=f"kafka auth/config: {res}",
                        error_kind="auth",
                        retryable=False,
                    )
                )
            elif _is_message_too_large(res):
                # PERMANENTE p/ este payload (> broker max.message.bytes): retentar
                # não ajuda → DLQ. (Antes caía no ramo transitório = retry infinito.)
                rejected.append(
                    RejectedEvent(
                        event_id=_event_id(ev),
                        reason=f"kafka message too large (> broker max.message.bytes): {res}",
                        error_kind="size",
                        retryable=False,
                    )
                )
            elif _is_fatal_producer_error(res):
                # Idempotência envenenada (OutOfOrderSequence/UnknownProducerId/…) →
                # recria o producer e RETENTA o lote num producer limpo (self-heal).
                fatal_seen = True
                any_retryable = True
                logger.warning("kafka: erro fatal de idempotência no envio: %s", res)
            else:
                # Erro de broker transitório (timeout/coordenador/conexão).
                any_retryable = True
                logger.warning("kafka: erro transitório no envio: %s", res)

        if fatal_seen:
            # Descarta o producer envenenado p/ o próximo send_batch criar um novo
            # (sem isto, todo send seguinte falharia até reiniciar o processo).
            old = self._producer
            async with self._lock:
                if self._producer is old:
                    self._producer = None
                    self._started = False
            if old is not None:
                try:
                    await old.stop()
                except Exception:  # pragma: no cover — best-effort: só descartar
                    pass

        if rejected and accepted == 0 and not any_retryable:
            return DeliveryResult(accepted=0, rejected=rejected, retryable=False)
        return DeliveryResult(
            accepted=accepted,
            rejected=rejected,
            retryable=any_retryable,
        )

    # ── Probe ────────────────────────────────────────────────────────────

    async def test(self) -> TestResult:
        """Probe: inicia o producer e busca metadata/partições do tópico.

        Passa (com ``latency_ms``) se o tópico existe e o broker é alcançável;
        falha de forma descritiva se o tópico não existe, o broker está
        inalcançável ou a credencial é inválida.
        """
        started = time.monotonic()
        try:
            producer = await self._ensure_producer()
        except Exception as exc:
            return TestResult.failed(f"kafka: producer indisponível: {exc}")

        try:
            # Força atualização de metadata para refletir o estado real do cluster.
            client = getattr(producer, "client", None)
            if client is not None and hasattr(client, "force_metadata_update"):
                await client.force_metadata_update()
            partitions = producer.partitions_for(self._topic)
            if asyncio.iscoroutine(partitions):
                partitions = await partitions
        except Exception as exc:
            if _is_auth_error(exc):
                return TestResult.failed(f"kafka: credencial inválida: {exc}")
            return TestResult.failed(f"kafka: broker inalcançável: {exc}")

        latency_ms = (time.monotonic() - started) * 1000.0
        if not partitions:
            return TestResult.failed(
                f"kafka: tópico {self._topic!r} não existe ou sem partições"
            )
        return TestResult.passed(
            f"tópico {self._topic!r} ok ({len(partitions)} partições)",
            latency_ms=latency_ms,
        )

    # ── Fechamento ───────────────────────────────────────────────────────

    async def close(self) -> None:
        """Para o producer (flush + desconexão). Best-effort, não levanta."""
        if self._producer is not None and self._started:
            try:
                await self._producer.stop()
            except Exception:  # pragma: no cover — best-effort
                logger.exception("kafka: erro ao parar producer")
            finally:
                self._started = False
                self._producer = None


def _is_auth_error(exc: BaseException) -> bool:
    """Heurística: a exceção indica falha de auth/autorização/config (→ DLQ).

    aiokafka não está importado aqui; classificamos pelo nome da classe e da
    mensagem (Authentication/Authorization/SaslAuthentication/...), evitando
    acoplar a tipos do SDK. Tudo o mais é tratado como transitório (retry).
    """
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    needles = ("auth", "authoriz", "sasl", "credential", "unsupportedsasl")
    return any(n in name or n in text for n in needles)


def _is_message_too_large(exc: BaseException) -> bool:
    """A mensagem excedeu ``max.message.bytes`` do broker → erro PERMANENTE p/ este
    payload (retry não ajuda; vai à DLQ). Classifica por nome/texto (SDK não importado
    aqui), espelhando :func:`_is_auth_error`."""
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    return (
        "messagesizetoolarge" in name
        or "message too large" in text
        or "larger than" in text
        or "max.message.bytes" in text
    )


def _is_fatal_producer_error(exc: BaseException) -> bool:
    """Erro que ENVENENA o producer idempotente (sequência quebrada) → exige recriar o
    producer. Classifica por nome da classe (OutOfOrderSequenceNumber /
    UnknownProducerId / ProducerFenced / DuplicateSequenceNumber)."""
    name = type(exc).__name__.lower()
    return any(
        k in name
        for k in (
            "outofordersequence",
            "unknownproducerid",
            "producerfenced",
            "duplicatesequence",
        )
    )


def _compression_type() -> Optional[str]:
    """``KAFKA_COMPRESSION_TYPE`` normalizado p/ o aiokafka (``"none"`` → ``None``)."""
    ct = (settings.KAFKA_COMPRESSION_TYPE or "none").strip().lower()
    return None if ct in ("", "none") else ct


register(
    DestinationRegistration(
        kind=KIND,
        factory=_factory,
        config_schema=KafkaConfig,
        default_queue="dispatch.kafka",
        # "idempotent": enable_idempotence=True + key=event_id dá exactly-once
        # no producer dentro de uma sessão (sem duplicar mensagem numa reentrega
        # do lote). "tls" via SSL/SASL_SSL; "test" via metadata/partitions_for.
        capabilities=frozenset({"tls", "batch", "test", "idempotent"}),
        required_secrets=("sasl_password",),
        label="Apache Kafka",
        # Producer único reutilizado; o paralelismo é por send_and_wait interno.
        delivery_defaults={"concurrency": 4},
        # Campos de catálogo self-describing (galeria de destinos).
        category="Streaming",
        icon_id="apachekafka",
        tier="beta",
        order=90,
        description="Apache Kafka — barramento de eventos (NDJSON) para fan-out downstream.",
    )
)
