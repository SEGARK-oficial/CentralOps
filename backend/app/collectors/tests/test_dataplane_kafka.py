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
    dividir 1 evento) — a compressão pode salvá-lo; senão o broker rejeita e vai à DLQ."""
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
