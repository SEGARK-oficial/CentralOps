"""Substrato do data-plane Kafka (codec, config, producer).

Sem broker real: o producer usa um AIOKafkaProducer FAKE (a thread/loop singleton
roda de verdade, validando o caminho de submit context-agnostic). O teste de
integração contra Redpanda real fica em módulo separado (marcado para pular sem broker).
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest
from pydantic import ValidationError

from backend.app.collectors import dataplane
from backend.app.collectors.dataplane import kafka_transport as kt
from backend.app.core.config import Settings, settings

_KEY = "test-master-key-for-centralops-suite-12345"


# ── Codec ──────────────────────────────────────────────────────────────────────

def test_codec_roundtrip():
    raw = dataplane.encode_delivery("dest-1", [{"id": "a"}, {"id": "b"}], {"traceparent": "tp"})
    decoded = dataplane.decode_delivery(raw)
    assert decoded["v"] == 1
    assert decoded["destination_id"] == "dest-1"
    assert decoded["batch"] == [{"id": "a"}, {"id": "b"}]
    assert decoded["trace"] == {"traceparent": "tp"}


def test_codec_no_trace():
    decoded = dataplane.decode_delivery(dataplane.encode_delivery("d", [{"x": 1}]))
    assert decoded["trace"] == {}


# ── Topic + client config ──────────────────────────────────────────────────────

def test_deliver_topic_uses_prefix(monkeypatch):
    monkeypatch.setattr(settings, "KAFKA_TOPIC_PREFIX", "acme")
    monkeypatch.setattr(settings, "KAFKA_DELIVER_TOPIC", "deliver")
    assert dataplane.deliver_topic() == "acme.deliver"


def test_client_kwargs_plaintext(monkeypatch):
    monkeypatch.setattr(settings, "KAFKA_SASL_MECHANISM", None)
    kw = kt._client_kwargs()
    assert kw["bootstrap_servers"] == settings.KAFKA_BOOTSTRAP_SERVERS
    assert "sasl_mechanism" not in kw


def test_client_kwargs_sasl(monkeypatch):
    monkeypatch.setattr(settings, "KAFKA_SASL_MECHANISM", "SCRAM-SHA-256")
    monkeypatch.setattr(settings, "KAFKA_SASL_USERNAME", "u")
    monkeypatch.setattr(settings, "KAFKA_SASL_PASSWORD", "p")
    kw = kt._client_kwargs()
    assert kw["sasl_mechanism"] == "SCRAM-SHA-256"
    assert kw["sasl_plain_username"] == "u"
    assert kw["sasl_plain_password"] == "p"


# ── EVENT_DATAPLANE validator ──────────────────────────────────────────────────

def test_event_dataplane_accepts_valid():
    for v in ("celery", "kafka", "KAFKA"):
        s = Settings(_env_file=None, APP_MASTER_KEY=_KEY, EVENT_DATAPLANE=v)
        assert s.EVENT_DATAPLANE == v.lower()


def test_event_dataplane_rejects_invalid():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, APP_MASTER_KEY=_KEY, EVENT_DATAPLANE="bogus")


# ── Producer singleton (aiokafka FAKE) ─────────────────────────────────────────

_SENT: list = []


class _FakeProducer:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def start(self):
        return None

    async def send_and_wait(self, topic, value=None, key=None):
        _SENT.append((topic, value, key))
        return None

    async def stop(self):
        return None


def test_produce_delivery_sends_keyed_to_topic(monkeypatch):
    import aiokafka

    monkeypatch.setattr(aiokafka, "AIOKafkaProducer", _FakeProducer)
    monkeypatch.setattr(settings, "KAFKA_TOPIC_PREFIX", "centralops")
    monkeypatch.setattr(settings, "KAFKA_DELIVER_TOPIC", "deliver")
    _SENT.clear()
    # Singleton fresco p/ isolar do estado de outros testes.
    monkeypatch.setattr(kt, "_producer_thread", kt._ProducerThread())

    kt._producer_thread.produce("dest-9", [{"a": 1}], {"traceparent": "tp"})

    assert len(_SENT) == 1
    topic, value, key = _SENT[0]
    assert topic == "centralops.deliver"
    assert key == b"dest-9"  # particionamento determinístico por destino
    decoded = dataplane.decode_delivery(value)
    assert decoded["destination_id"] == "dest-9"
    assert decoded["batch"] == [{"a": 1}]

    kt._producer_thread.shutdown()


# ── size-aware framing (anti-MessageSizeTooLarge) ─────────────────────────────


def test_frame_batch_splits_oversize_batch_under_limit():
    """Um lote grande é dividido em N mensagens, cada uma ≤ limite (raw), e NENHUM
    evento é perdido na divisão."""
    big = [{"_centralops": {"event_id": f"e{i}"}, "data": "x" * 1000} for i in range(40)]
    msgs = list(kt._frame_batch("d1", big, None, 5000))

    assert len(msgs) > 1
    assert all(len(m) <= 5000 for m in msgs)
    total = sum(len(dataplane.decode_delivery(m)["batch"]) for m in msgs)
    assert total == 40  # todos os eventos preservados


def test_frame_batch_single_event_emitted_even_if_oversize():
    """Um único evento que sozinho excede o limite é emitido mesmo assim (não dá p/
    dividir 1 evento). Quem decide o destino dele é ``produce``: vai à DLQ, porque a
    compressão não o salva (o aiokafka mede antes de comprimir)."""
    huge = [{"_centralops": {"event_id": "big"}, "data": "x" * 20000}]
    msgs = list(kt._frame_batch("d1", huge, None, 5000))
    assert len(msgs) == 1


def test_produce_splits_large_batch_into_multiple_records(monkeypatch):
    """produce() enquadra por tamanho: um lote grande vira VÁRIOS registros no tópico,
    cada um ≤ KAFKA_MAX_REQUEST_BYTES (prova o anti-MessageSizeTooLarge ponta-a-ponta)."""
    import aiokafka

    monkeypatch.setattr(aiokafka, "AIOKafkaProducer", _FakeProducer)
    monkeypatch.setattr(settings, "KAFKA_TOPIC_PREFIX", "centralops")
    monkeypatch.setattr(settings, "KAFKA_DELIVER_TOPIC", "deliver")
    monkeypatch.setattr(settings, "KAFKA_MAX_REQUEST_BYTES", 4000)
    _SENT.clear()
    monkeypatch.setattr(kt, "_producer_thread", kt._ProducerThread())

    big = [{"_centralops": {"event_id": f"e{i}"}, "data": "y" * 500} for i in range(30)]
    kt._producer_thread.produce("dest-big", big, None)

    assert len(_SENT) > 1  # dividido em múltiplos registros
    limit = int(4000 * 0.9)
    assert all(len(value) <= limit for _topic, value, _key in _SENT)
    total = sum(len(dataplane.decode_delivery(v)["batch"]) for _t, v, _k in _SENT)
    assert total == 30

    kt._producer_thread.shutdown()


# ── evento único acima do teto: DLQ, nunca exceção (incidente out/2026) ───────
#
# Antes, ``produce`` levantava ``MessageSizeTooLargeError`` para um evento de
# 1,16 MB contra o teto de 1 MiB. O ciclo de coleta revertia o cursor e soltava
# as claims, o ciclo seguinte buscava o MESMO evento e falhava no MESMO ponto: o
# coletor ficava parado nele para sempre.


def _evt(event_id: str, size: int) -> dict:
    return {
        "_centralops": {"event_id": event_id, "organization_id": 7},
        "data": "z" * size,
    }


@pytest.fixture
def dlq_calls(monkeypatch):
    """Registra cada ``persist_batch_dlq`` em vez de tocar o banco.

    Lista, não exceção: o ``_divert_oversized_to_dlq`` não engole nada, mas um
    espião que levanta seria engolido por qualquer ``except`` amplo futuro."""
    from backend.app.collectors import delivery

    calls: list = []

    def _fake(batch, *, destination_id, error_kind, organization_id=None, error_detail=None):
        calls.append(
            {
                "event_ids": [e["_centralops"]["event_id"] for e in batch],
                "destination_id": destination_id,
                "error_kind": error_kind,
                "organization_id": organization_id,
                "error_detail": error_detail,
            }
        )
        return True

    monkeypatch.setattr(delivery, "persist_batch_dlq", _fake)
    return calls


def _fresh_producer(monkeypatch, producer_cls, max_bytes: int):
    import aiokafka

    monkeypatch.setattr(aiokafka, "AIOKafkaProducer", producer_cls)
    monkeypatch.setattr(settings, "KAFKA_TOPIC_PREFIX", "centralops")
    monkeypatch.setattr(settings, "KAFKA_DELIVER_TOPIC", "deliver")
    monkeypatch.setattr(settings, "KAFKA_MAX_REQUEST_BYTES", max_bytes)
    _SENT.clear()
    monkeypatch.setattr(kt, "_producer_thread", kt._ProducerThread())


def _sent_event_ids() -> list:
    return [
        e["_centralops"]["event_id"]
        for _t, v, _k in _SENT
        for e in dataplane.decode_delivery(v)["batch"]
    ]


def test_produce_diverts_single_oversize_event_to_dlq_and_sends_the_rest(
    monkeypatch, dlq_calls
):
    """O evento que não cabe vai à DLQ (error_kind=size); os vizinhos dele no lote
    são entregues ao tópico; e ``produce`` NÃO levanta — é isso que destrava o
    ciclo de coleta."""
    _fresh_producer(monkeypatch, _FakeProducer, 4000)

    batch = [_evt("small-1", 100), _evt("huge", 20000), _evt("small-2", 100)]
    kt._producer_thread.produce("dest-x", batch, None)

    assert sorted(_sent_event_ids()) == ["small-1", "small-2"]
    assert len(dlq_calls) == 1
    call = dlq_calls[0]
    assert call["event_ids"] == ["huge"]
    assert call["destination_id"] == "dest-x"
    assert call["error_kind"] == "size"
    assert call["organization_id"] == 7
    # O detalhe leva o tamanho real e o teto — é o que o operador precisa para
    # escolher entre reduzir o evento e subir o teto.
    assert "KAFKA_MAX_REQUEST_BYTES=4000" in call["error_detail"]
    assert " bytes " in call["error_detail"]
    assert "z" * 50 not in call["error_detail"]  # nunca o conteúdo do evento

    kt._producer_thread.shutdown()


def test_produce_raises_when_the_dlq_cannot_keep_the_oversize_event(monkeypatch):
    """DLQ fora = evento não entregue E não guardado. Seguir perderia o evento em
    silêncio; levantar faz o ciclo reverter e tentar de novo."""
    from backend.app.collectors import delivery

    _fresh_producer(monkeypatch, _FakeProducer, 4000)
    monkeypatch.setattr(delivery, "persist_batch_dlq", lambda *a, **k: False)

    with pytest.raises(kt.OversizedEventDivertError):
        kt._producer_thread.produce("dest-x", [_evt("huge", 20000)], None)

    kt._producer_thread.shutdown()


class _BrokerLimitedProducer(_FakeProducer):
    """Broker com ``max.message.bytes`` MENOR que o ``KAFKA_MAX_REQUEST_BYTES``:
    o cliente deixa passar e o broker recusa (o desalinhamento que o comentário de
    config.py descreve como o bug original)."""

    broker_limit = 2000

    async def send_and_wait(self, topic, value=None, key=None):
        if len(value) > self.broker_limit:
            from aiokafka.errors import MessageSizeTooLargeError

            raise MessageSizeTooLargeError("broker: message too large")
        _SENT.append((topic, value, key))
        return None


def test_broker_side_size_rejection_resplits_instead_of_failing(monkeypatch, dlq_calls):
    """Registro com N eventos recusado pelo broker é re-dividido até caber; nada vai
    à DLQ, nenhum evento se perde, e nenhum registro passa do limite do broker."""
    pytest.importorskip("aiokafka.errors")
    _fresh_producer(monkeypatch, _BrokerLimitedProducer, 8000)

    batch = [_evt(f"e{i}", 300) for i in range(20)]
    kt._producer_thread.produce("dest-y", batch, None)

    assert sorted(_sent_event_ids()) == sorted(f"e{i}" for i in range(20))
    assert all(len(v) <= _BrokerLimitedProducer.broker_limit for _t, v, _k in _SENT)
    assert dlq_calls == []

    kt._producer_thread.shutdown()


def test_broker_side_rejection_of_a_single_event_goes_to_dlq(monkeypatch, dlq_calls):
    """Evento que passa no teto do cliente mas não no do broker: DLQ, sem exceção."""
    pytest.importorskip("aiokafka.errors")
    _fresh_producer(monkeypatch, _BrokerLimitedProducer, 8000)

    kt._producer_thread.produce("dest-y", [_evt("mid", 3000), _evt("ok", 100)], None)

    assert _sent_event_ids() == ["ok"]
    assert [c["event_ids"] for c in dlq_calls] == [["mid"]]

    kt._producer_thread.shutdown()


class _BrokerDownProducer(_FakeProducer):
    async def send_and_wait(self, topic, value=None, key=None):
        raise ConnectionError("broker down")


def test_transient_errors_still_propagate(monkeypatch, dlq_calls):
    """Broker fora NÃO é motivo de DLQ: o ciclo precisa reverter e tentar de novo,
    senão um apagão do Redpanda esvaziaria a coleta inteira na DLQ."""
    _fresh_producer(monkeypatch, _BrokerDownProducer, 4000)

    with pytest.raises(ConnectionError):
        kt._producer_thread.produce("dest-z", [_evt("a", 100)], None)
    assert dlq_calls == []

    kt._producer_thread.shutdown()


def test_is_message_too_large_classifies_client_and_broker_refusals():
    """A mensagem EXATA do incidente, embrulhada ou não, é classificada como
    recusa por tamanho; erro transitório não é."""
    errors = pytest.importorskip("aiokafka.errors")

    incident = errors.MessageSizeTooLargeError(
        "The message is 1162351 bytes when serialized which is larger than the "
        "maximum request size you have configured with the max_request_size "
        "configuration"
    )
    assert kt._is_message_too_large(incident) is True
    assert kt._is_message_too_large(errors.RecordTooLargeError("x")) is True

    wrapped = RuntimeError("wrap")
    wrapped.__cause__ = incident
    assert kt._is_message_too_large(wrapped) is True

    assert kt._is_message_too_large(errors.KafkaTimeoutError()) is False
    assert kt._is_message_too_large(ConnectionError("down")) is False


def test_precheck_is_never_more_permissive_than_aiokafka():
    """A pré-checagem desvia ANTES de ir ao broker. Se ela fosse mais permissiva
    que o ``_serialize`` do aiokafka, o desvio dependeria só do ``except``; se
    fosse rígida demais, mandaria à DLQ evento que caberia. Mede contra o SDK real
    em torno da fronteira."""
    import asyncio

    aiokafka = pytest.importorskip("aiokafka")
    errors = pytest.importorskip("aiokafka.errors")
    limit = 4000
    key = b"dest-1"

    async def _probe():
        producer = aiokafka.AIOKafkaProducer(
            bootstrap_servers="127.0.0.1:1", max_request_size=limit
        )
        rejected_by_sdk = []
        # O ``start()`` fixa o magic pela versão do broker (0 antes do 0.10, senão
        # 1); sem broker, os dois são varridos — o overhead maior é o que importa.
        for magic in (0, 1):
            producer._producer_magic = magic
            for size in range(limit - 200, limit + 50):
                value = b"v" * size
                try:
                    producer._serialize("t", key, value)
                except errors.MessageSizeTooLargeError:
                    rejected_by_sdk.append(value)
        return rejected_by_sdk

    original = settings.KAFKA_MAX_REQUEST_BYTES
    try:
        settings.KAFKA_MAX_REQUEST_BYTES = limit
        rejected_by_sdk = asyncio.run(_probe())
        assert rejected_by_sdk, "fronteira fora da faixa varrida — teste vazio"
        assert all(kt._exceeds_request_limit(v, key) for v in rejected_by_sdk)
        # E não recusa o que está bem abaixo do teto.
        assert kt._exceeds_request_limit(b"v" * (limit // 2), key) is False
    finally:
        settings.KAFKA_MAX_REQUEST_BYTES = original


def test_is_fatal_producer_error_detects_idempotence_poisoning():
    """Erros que envenenam o producer idempotente são detectados (inclusive embrulhados
    na cadeia de causas), p/ disparar a recriação self-heal."""
    aiokafka_errors = pytest.importorskip("aiokafka.errors")

    exc = aiokafka_errors.OutOfOrderSequenceNumber("seq broke")
    assert kt._is_fatal_producer_error(exc) is True

    wrapped = RuntimeError("wrap")
    wrapped.__cause__ = exc
    assert kt._is_fatal_producer_error(wrapped) is True

    assert kt._is_fatal_producer_error(ValueError("transient")) is False
