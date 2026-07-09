"""Testes do destino Kafka (kind='kafka').

CRÍTICO: ``aiokafka`` NÃO está instalado no venv de teste. Estes testes NUNCA
importam ``aiokafka``. Em vez disso, monkeypatcham ``KafkaClient._make_producer``
para devolver um FAKE async producer (``FakeProducer``) que captura
``start``/``send_and_wait``/``stop``/``partitions_for`` — provando o contrato sem
o SDK.

Cobre:
(a) ``format`` — value é JSON bytes do envelope.
(b) ``send_batch`` feliz — ``send_and_wait`` por evento com ``key=event_id`` e
    value JSON correto; producer reutilizado entre lotes (start uma vez).
(c) ``send_batch`` — erro de broker transitório → retryable; erro de auth →
    rejected error_kind='auth' não-retryable.
(d) ``test()`` — passed (tópico existe) e failed (tópico ausente / broker down).
(e) ``close()`` — chama stop.
(f) Registry — kind registrado, build, metadados.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, List, Optional
from unittest.mock import MagicMock

import pytest

from backend.app.collectors.output.base import DeliveryResult, TestResult
from backend.app.collectors.output.destinations import registry
from backend.app.collectors.output.destinations.kafka import KafkaClient
from backend.app.collectors.output.destinations.registry import (
    DestinationConfig,
    compute_config_version,
)


# ── Fake async producer (substitui aiokafka — nunca importado) ───────────


class FakeProducer:
    """Producer assíncrono falso que captura todas as interações.

    Reproduz a superfície de ``AIOKafkaProducer`` que o ``KafkaClient`` usa:
    ``start``/``stop`` (corrotinas), ``send_and_wait`` (corrotina por mensagem),
    ``partitions_for`` (sync) e ``client.force_metadata_update`` (corrotina).
    """

    def __init__(
        self,
        *,
        partitions: Optional[set] = None,
        send_exc: Optional[BaseException] = None,
        metadata_exc: Optional[BaseException] = None,
    ) -> None:
        self.partitions = partitions if partitions is not None else {0, 1, 2}
        self._send_exc = send_exc
        self._metadata_exc = metadata_exc
        self.start_calls = 0
        self.stop_calls = 0
        self.sent: List[dict] = []
        self.client = MagicMock()

        async def _force_metadata_update() -> None:
            if self._metadata_exc is not None:
                raise self._metadata_exc

        self.client.force_metadata_update = _force_metadata_update

    async def start(self) -> None:
        self.start_calls += 1

    async def stop(self) -> None:
        self.stop_calls += 1

    async def send_and_wait(self, topic: str, *, value: Any, key: Any) -> Any:
        self.sent.append({"topic": topic, "value": value, "key": key})
        if self._send_exc is not None:
            raise self._send_exc
        return MagicMock()  # RecordMetadata-like

    def partitions_for(self, topic: str) -> set:
        return self.partitions


class AuthError(Exception):
    """Simula uma SaslAuthenticationFailedError do aiokafka (pelo nome/mensagem)."""


class BrokerTimeout(Exception):
    """Simula um erro transitório de broker (timeout/coordenador)."""


class MessageSizeTooLargeError(Exception):
    """Simula o aiokafka MessageSizeTooLargeError (classificado por nome)."""


class OutOfOrderSequenceNumber(Exception):
    """Simula o erro FATAL de idempotência do aiokafka (classificado por nome)."""


class SlowStartProducer(FakeProducer):
    """FakeProducer cujo ``start`` cede o loop — expõe a race de _ensure_producer.

    Ao ``await asyncio.sleep(0)`` antes de incrementar ``start_calls``, vários
    callers concorrentes que passassem o check ``if not self._started`` sem
    serialização entrariam todos em ``start`` → ``start_calls > 1``. Com o lock,
    só o primeiro entra.
    """

    async def start(self) -> None:
        await asyncio.sleep(0)
        self.start_calls += 1


def _make_client(producer: FakeProducer, **overrides: Any) -> KafkaClient:
    """Constrói um KafkaClient com ``_make_producer`` apontando ao fake."""
    cfg: dict[str, Any] = {
        "bootstrap_servers": "b1:9092,b2:9092",
        "topic": "centralops-events",
        "security_protocol": "SASL_SSL",
        "username": "svc-centralops",
        "password": "s3cr3t",
    }
    cfg.update(overrides)
    client = KafkaClient(**cfg)
    client._make_producer = lambda: producer  # type: ignore[method-assign]
    return client


@pytest.fixture
def sample_event() -> dict:
    """Evento canônico mínimo com namespace _centralops."""
    return {
        "_centralops": {
            "vendor": "sophos",
            "event_id": "evt-abc123",
        },
        "data": {"id": "evt-1", "severity": "Critical"},
    }


# ── (a) format ───────────────────────────────────────────────────────────


def test_format_returns_json_bytes(sample_event: dict) -> None:
    client = KafkaClient(bootstrap_servers="b1:9092", topic="t")
    value = client.format(sample_event)
    assert isinstance(value, bytes)
    parsed = json.loads(value.decode("utf-8"))
    assert parsed["_centralops"]["event_id"] == "evt-abc123"
    assert parsed["data"]["severity"] == "Critical"


# ── (b) send_batch feliz ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_batch_success_send_and_wait_per_event(sample_event: dict) -> None:
    """Caminho feliz: um send_and_wait por evento, key=event_id, value JSON."""
    producer = FakeProducer()
    client = _make_client(producer)

    result = await client.send_batch([sample_event])

    assert isinstance(result, DeliveryResult)
    assert result.accepted == 1
    assert result.all_accepted
    assert not result.retryable
    assert producer.start_calls == 1
    assert len(producer.sent) == 1
    msg = producer.sent[0]
    assert msg["topic"] == "centralops-events"
    assert msg["key"] == b"evt-abc123"
    assert json.loads(msg["value"].decode("utf-8"))["data"]["id"] == "evt-1"


@pytest.mark.asyncio
async def test_send_batch_multiple_events_keys(sample_event: dict) -> None:
    """Cada evento vira uma mensagem com sua própria key=event_id."""
    producer = FakeProducer()
    client = _make_client(producer)

    ev2 = {"_centralops": {"event_id": "evt-xyz789"}, "data": {"id": "evt-2"}}
    result = await client.send_batch([sample_event, ev2])

    assert result.accepted == 2
    keys = [m["key"] for m in producer.sent]
    assert keys == [b"evt-abc123", b"evt-xyz789"]


@pytest.mark.asyncio
async def test_send_batch_message_too_large_is_non_retryable_dlq(
    sample_event: dict,
) -> None:
    """MessageSizeTooLarge é PERMANENTE p/ o payload (> broker max.message.bytes) →
    rejected error_kind='size', retryable=False (vai à DLQ) — não retry infinito
    (antes caía no ramo transitório = retentado pra sempre)."""
    producer = FakeProducer(
        send_exc=MessageSizeTooLargeError("message is 2MB, larger than 1MB")
    )
    client = _make_client(producer)

    result = await client.send_batch([sample_event])

    assert result.accepted == 0
    assert result.retryable is False
    assert len(result.rejected) == 1
    assert result.rejected[0].error_kind == "size"
    assert result.rejected[0].retryable is False


@pytest.mark.asyncio
async def test_send_batch_fatal_idempotence_recreates_producer(
    sample_event: dict,
) -> None:
    """Erro FATAL de idempotência (OutOfOrderSequenceNumber) → lote retryable +
    producer DESCARTADO (stop + ref limpa) p/ o próximo send recriar um limpo
    (self-heal). Antes, o producer envenenado era reusado e TODO send seguia falhando
    até reiniciar o processo."""
    producer = FakeProducer(send_exc=OutOfOrderSequenceNumber("sequence broke"))
    client = _make_client(producer)

    result = await client.send_batch([sample_event])

    assert result.retryable is True  # retenta num producer limpo
    assert client._producer is None  # o envenenado foi descartado
    assert producer.stop_calls == 1  # e parado (best-effort)


@pytest.mark.asyncio
async def test_send_batch_event_without_id_uses_fallback_key() -> None:
    """Evento sem _centralops.event_id usa '?' como key."""
    producer = FakeProducer()
    client = _make_client(producer)

    await client.send_batch([{"data": "x", "_centralops": {}}])
    assert producer.sent[0]["key"] == b"?"


@pytest.mark.asyncio
async def test_producer_reused_between_batches(sample_event: dict) -> None:
    """Producer iniciado UMA vez e reutilizado entre lotes (start_calls==1)."""
    producer = FakeProducer()
    client = _make_client(producer)

    await client.send_batch([sample_event])
    await client.send_batch([sample_event])

    assert producer.start_calls == 1  # start não repete
    assert len(producer.sent) == 2


@pytest.mark.asyncio
async def test_ensure_producer_concurrent_starts_once(sample_event: dict) -> None:
    """~8 send_batch concorrentes num client novo iniciam o producer UMA vez.

    Sem o lock em _ensure_producer, vários callers passariam o check
    ``if not self._started`` e chamariam start() múltiplas vezes (race que
    corrompe o producer). O SlowStartProducer cede o loop em start para forçar
    a janela; o lock garante start_calls == 1.
    """
    producer = SlowStartProducer()
    client = _make_client(producer)

    results = await asyncio.gather(
        *[client.send_batch([sample_event]) for _ in range(8)]
    )

    assert producer.start_calls == 1
    assert all(r.accepted == 1 for r in results)
    assert len(producer.sent) == 8


@pytest.mark.asyncio
async def test_ensure_producer_recreates_after_start_failure(sample_event: dict) -> None:
    """Se o 1º start() levanta, o producer morto é descartado e a 2ª tentativa
    CRIA um producer novo (não reusa o que falhou)."""

    class FailingStartProducer(FakeProducer):
        async def start(self) -> None:
            self.start_calls += 1
            raise BrokerTimeout("broker indisponível no start")

    bad = FailingStartProducer()
    good = FakeProducer()
    produced: list[FakeProducer] = [bad, good]

    client = _make_client(bad)
    # _make_producer devolve o próximo da fila a cada chamada.
    client._make_producer = lambda: produced.pop(0)  # type: ignore[method-assign]

    # 1ª tentativa: start falha → send_batch trata como transitório (retryable).
    first = await client.send_batch([sample_event])
    assert first.retryable is True
    assert first.accepted == 0
    assert bad.start_calls == 1
    # producer morto foi descartado (reset p/ recriar na próxima).
    assert client._producer is None
    assert client._started is False

    # 2ª tentativa: cria um producer NOVO (good), inicia e envia com sucesso.
    second = await client.send_batch([sample_event])
    assert second.accepted == 1
    assert good.start_calls == 1
    assert len(good.sent) == 1
    assert client._producer is good


@pytest.mark.asyncio
async def test_send_batch_empty_returns_ok_zero() -> None:
    """Lote vazio → ok(0), sem criar/iniciar producer."""
    producer = FakeProducer()
    client = _make_client(producer)

    result = await client.send_batch([])

    assert result.accepted == 0
    assert result.all_accepted
    assert producer.start_calls == 0
    assert producer.sent == []


# ── (c) send_batch erros ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_batch_broker_transient_error_is_retryable(sample_event: dict) -> None:
    """Erro de broker transitório → retryable=True, nada rejeitado."""
    producer = FakeProducer(send_exc=BrokerTimeout("request timed out"))
    client = _make_client(producer)

    result = await client.send_batch([sample_event])

    assert result.accepted == 0
    assert result.retryable is True
    assert not result.rejected


@pytest.mark.asyncio
async def test_send_batch_auth_error_is_rejected_non_retryable(sample_event: dict) -> None:
    """Erro de auth/config → rejected error_kind='auth', retryable=False."""
    producer = FakeProducer(send_exc=AuthError("SASL authentication failed"))
    client = _make_client(producer)

    result = await client.send_batch([sample_event])

    assert result.accepted == 0
    assert result.retryable is False
    assert len(result.rejected) == 1
    rej = result.rejected[0]
    assert rej.error_kind == "auth"
    assert rej.retryable is False
    assert rej.event_id == "evt-abc123"


@pytest.mark.asyncio
async def test_send_batch_partial_failure(sample_event: dict) -> None:
    """Um evento ok + um transitório → accepted parcial, retryable=True."""

    class FlakyProducer(FakeProducer):
        async def send_and_wait(self, topic: str, *, value: Any, key: Any) -> Any:
            self.sent.append({"topic": topic, "value": value, "key": key})
            if key == b"evt-bad":
                raise BrokerTimeout("coordinator not available")
            return MagicMock()

    producer = FlakyProducer()
    client = _make_client(producer)

    ev_bad = {"_centralops": {"event_id": "evt-bad"}, "data": {}}
    result = await client.send_batch([sample_event, ev_bad])

    assert result.accepted == 1
    assert result.retryable is True


# ── (d) test() ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_test_passed_when_topic_exists(sample_event: dict) -> None:
    """Tópico com partições → TestResult.passed com latency_ms."""
    producer = FakeProducer(partitions={0, 1, 2})
    client = _make_client(producer)

    result = await client.test()

    assert isinstance(result, TestResult)
    assert result.ok is True
    assert result.latency_ms is not None
    assert "3 part" in result.detail


@pytest.mark.asyncio
async def test_test_failed_when_topic_absent() -> None:
    """Tópico inexistente (partitions_for -> None/vazio) → TestResult.failed."""
    producer = FakeProducer(partitions=set())
    client = _make_client(producer)

    result = await client.test()

    assert result.ok is False
    assert "não existe" in result.detail or "partições" in result.detail


@pytest.mark.asyncio
async def test_test_failed_when_broker_down() -> None:
    """force_metadata_update levanta → TestResult.failed (broker inalcançável)."""
    producer = FakeProducer(metadata_exc=BrokerTimeout("no brokers available"))
    client = _make_client(producer)

    result = await client.test()

    assert result.ok is False
    assert "inalcançável" in result.detail or "broker" in result.detail.lower()


@pytest.mark.asyncio
async def test_test_failed_on_auth_error() -> None:
    """Metadata com erro de auth → failed com 'credencial'."""
    producer = FakeProducer(metadata_exc=AuthError("SASL authentication failed"))
    client = _make_client(producer)

    result = await client.test()

    assert result.ok is False
    assert "credencial" in result.detail.lower()


# ── (e) close() ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_close_calls_stop(sample_event: dict) -> None:
    """close() chama producer.stop() e zera o estado."""
    producer = FakeProducer()
    client = _make_client(producer)
    await client.send_batch([sample_event])  # inicia o producer

    await client.close()

    assert producer.stop_calls == 1
    assert client._producer is None
    assert client._started is False


@pytest.mark.asyncio
async def test_close_noop_when_not_started() -> None:
    """close() sem producer iniciado é no-op (não levanta)."""
    producer = FakeProducer()
    client = _make_client(producer)
    await client.close()  # nunca enviou → producer não iniciado
    assert producer.stop_calls == 0


# ── (f) Registry ─────────────────────────────────────────────────────────


def test_kafka_registered() -> None:
    assert "kafka" in registry.all_kinds()


def test_kafka_build_returns_destination_with_correct_kind() -> None:
    config = {"bootstrap_servers": "b1:9092", "topic": "t", "username": "svc"}
    dest_config = DestinationConfig(
        destination_id="test-kafka-registry",
        kind="kafka",
        config=config,
        config_version=compute_config_version(config, {}),
    )
    dest = registry.build(dest_config)
    assert dest.kind == "kafka"
    assert isinstance(dest, KafkaClient)


def test_kafka_build_without_secret_has_none_password() -> None:
    config = {"bootstrap_servers": "b1:9092", "topic": "t", "username": "svc"}
    dest_config = DestinationConfig(
        destination_id="dormant-kafka",
        kind="kafka",
        config=config,
        secret_ref=None,
    )
    dest = registry.build(dest_config, secrets=None)
    assert isinstance(dest, KafkaClient)
    assert dest._password is None


def test_kafka_build_with_secret_resolves_password() -> None:
    config = {"bootstrap_servers": "b1:9092", "topic": "t", "username": "svc"}
    dest_config = DestinationConfig(
        destination_id="secret-kafka",
        kind="kafka",
        config=config,
        secret_ref="enc::kafka",
    )
    mock_secrets = MagicMock()
    mock_secrets.decrypt.return_value = "plain-sasl-pass"

    dest = registry.build(dest_config, secrets=mock_secrets)

    assert isinstance(dest, KafkaClient)
    assert dest._password == "plain-sasl-pass"
    mock_secrets.decrypt.assert_called_once_with("enc::kafka")


def test_kafka_build_sasl_without_username_raises() -> None:
    """SASL + username None/vazio → ValueError fail-fast na construção (_factory)."""
    config = {
        "bootstrap_servers": "b1:9092",
        "topic": "t",
        "security_protocol": "SASL_SSL",
        # username ausente → None
    }
    dest_config = DestinationConfig(
        destination_id="sasl-no-user",
        kind="kafka",
        config=config,
        secret_ref="enc::kafka",
    )
    mock_secrets = MagicMock()
    mock_secrets.decrypt.return_value = "plain-sasl-pass"

    with pytest.raises(ValueError, match="username"):
        registry.build(dest_config, secrets=mock_secrets)


def test_kafka_registration_metadata() -> None:
    reg = registry.get("kafka")
    assert reg.default_queue == "dispatch.kafka"
    assert reg.capabilities == frozenset({"tls", "batch", "test", "idempotent"})
    assert reg.required_secrets == ("sasl_password",)
    assert reg.label == "Apache Kafka"


def test_module_imports_without_aiokafka() -> None:
    """O sink kafka NÃO vincula aiokafka no carregamento do módulo (import tardio
    em _make_producer).

    Order-independent: aiokafka é dependência CORE e
    o data-plane (collectors/dataplane) pode tê-lo importado em ``sys.modules`` —
    então a antiga checagem global ``aiokafka not in sys.modules`` deixou de ser
    válida. Verificamos o NAMESPACE do módulo do sink: o invariante real é que o
    sink não faz ``import aiokafka`` no topo (continua tardio em _make_producer)."""
    from backend.app.collectors.output.destinations import kafka as _ksink

    assert not hasattr(_ksink, "aiokafka"), (
        "o sink kafka não deve vincular aiokafka no nível do módulo "
        "(o import deve ser tardio em _make_producer)"
    )
