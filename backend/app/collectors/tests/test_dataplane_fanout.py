"""Cut-over do fan-out para o data-plane Kafka.

Verifica que ``_enqueue_routed`` produz ao tópico de entrega quando
EVENT_DATAPLANE=kafka (e NÃO usa o Celery dispatch). Vendor-neutro:
o ``wazuh-default`` NÃO é mais special-case — ele flui pela
MESMA via uniforme de qualquer outro destino (Kafka em kafka-mode,
``dispatch_to_destination`` shardeado em celery-mode), e ``dispatch_to_wazuh``
NÃO é mais chamado pelo fan-out. Com EVENT_DATAPLANE=celery nada muda além
disso.
"""

from __future__ import annotations

import os
import types
from unittest.mock import MagicMock

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

from backend.app.collectors import dataplane, pipeline, routing, tasks, tracing
from backend.app.core.config import settings


def _fake_result(sub_batches):
    return types.SimpleNamespace(
        routed=0, dropped=0, fallback=0, residency_blocked=0,
        loop_blocked=0, unrouted=0, unrouted_events=[], per_route={},
        sub_batches=sub_batches,
    )


def _common_patches(monkeypatch, sub_batches):
    monkeypatch.setattr(pipeline, "_load_destination_residency", lambda ids: {})
    monkeypatch.setattr(pipeline, "_load_wazuh_loop_destination_ids", lambda ids: frozenset())
    monkeypatch.setattr(pipeline, "_load_fallback_destination_id", lambda org: None)
    monkeypatch.setattr(routing, "route_batch", lambda *a, **k: _fake_result(sub_batches))
    monkeypatch.setattr(tracing, "carrier", lambda: {})
    # Vendor-neutro: a lane dedicada ``dispatch_to_wazuh`` foi
    # DELETADA. O fan-out despacha TODO destino (inclusive ``wazuh-default``)
    # uniformemente via ``dispatch_to_destination`` (celery) ou ``produce_delivery``
    # (kafka). Só patchamos a via uniforme.
    dd = MagicMock()
    monkeypatch.setattr(tasks, "dispatch_to_destination", dd)
    return dd


_ROUTE = types.SimpleNamespace(id=1, action="route", destination_ids=["dest-1"])


def test_fanout_kafka_produces_all_dests_incl_wazuh_default(monkeypatch):
    """Vendor-neutro: em kafka-mode TODOS os destinos —
    inclusive o ``wazuh-default`` — são produzidos ao tópico ``deliver`` pela
    MESMA via uniforme. Nenhum Celery dispatch é chamado
    (``dispatch_to_destination``)."""
    monkeypatch.setattr(settings, "EVENT_DATAPLANE", "kafka")
    dd = _common_patches(
        monkeypatch, {"dest-1": [{"e": 1}], "wazuh-default": [{"e": 2}]}
    )
    produced = []
    monkeypatch.setattr(dataplane, "produce_delivery", lambda d, b, t: produced.append((d, b, t)))

    pipeline._enqueue_routed([{"e": 1}], [_ROUTE])

    # TODOS os destinos → Kafka (key=dest, tp vazio → None), wazuh-default incluso.
    assert sorted(produced) == [
        ("dest-1", [{"e": 1}], None),
        ("wazuh-default", [{"e": 2}], None),
    ]
    # Nenhuma lane Celery é usada — wazuh-default não é mais special-case.
    dd.apply_async.assert_not_called()


def test_fanout_kafka_oversize_event_does_not_raise_into_the_collection_cycle(monkeypatch):
    """Incidente out/2026: UM evento de 1,16 MB contra o teto de 1 MiB fazia o
    ``produce_delivery`` levantar ``MessageSizeTooLargeError`` de dentro do
    ``_enqueue_routed``. O ciclo revertia o cursor e soltava as claims, e o ciclo
    seguinte buscava o mesmo evento: coletor parado para sempre.

    Aqui o ``produce_delivery`` é o REAL (só o aiokafka é falso): o fan-out
    termina sem exceção, o evento gigante vai à DLQ do destino e o resto do
    sub-lote chega ao tópico."""
    import aiokafka

    from backend.app.collectors import delivery
    from backend.app.collectors.dataplane import kafka_transport as kt

    from aiokafka.errors import MessageSizeTooLargeError

    sent: list = []

    class _Producer:
        """Imita o check de CLIENTE do aiokafka (``_serialize``): mede key+value
        contra ``max_request_size`` e levanta — é esse o erro do incidente."""

        def __init__(self, **kwargs):
            self._max = kwargs["max_request_size"]

        async def start(self):
            return None

        async def send_and_wait(self, topic, value=None, key=None):
            if len(key) + len(value) > self._max:
                raise MessageSizeTooLargeError(
                    "The message is %d bytes when serialized which is larger than "
                    "the maximum request size you have configured with the "
                    "max_request_size configuration" % (len(key) + len(value))
                )
            sent.append(value)

        async def stop(self):
            return None

    dlq: list = []
    monkeypatch.setattr(
        delivery,
        "persist_batch_dlq",
        lambda batch, **kw: dlq.append(([e["_centralops"]["event_id"] for e in batch], kw)) or True,
    )
    monkeypatch.setattr(aiokafka, "AIOKafkaProducer", _Producer)
    monkeypatch.setattr(settings, "EVENT_DATAPLANE", "kafka")
    monkeypatch.setattr(settings, "KAFKA_MAX_REQUEST_BYTES", 4000)
    monkeypatch.setattr(kt, "_producer_thread", kt._ProducerThread())

    def _e(eid, size):
        return {"_centralops": {"event_id": eid, "organization_id": 3}, "d": "q" * size}

    _common_patches(
        monkeypatch, {"dest-1": [_e("a", 50), _e("gigante", 9000), _e("b", 50)]}
    )

    pipeline._enqueue_routed([_e("a", 50)], [_ROUTE])  # não levanta

    delivered = [
        e["_centralops"]["event_id"] for v in sent for e in dataplane.decode_delivery(v)["batch"]
    ]
    assert sorted(delivered) == ["a", "b"]
    assert [ids for ids, _kw in dlq] == [["gigante"]]
    assert dlq[0][1]["error_kind"] == "size"
    assert dlq[0][1]["destination_id"] == "dest-1"

    kt._producer_thread.shutdown()


def test_fanout_celery_routes_all_dests_via_dispatch_to_destination(monkeypatch):
    """Em celery-mode, TODOS os destinos — inclusive o ``wazuh-default`` — vão
    pela MESMA via ``dispatch_to_destination`` shardeada.
    ``wazuh-default`` não é mais desviado para uma lane dedicada no fan-out
    (essa via foi deletada); e o Kafka nunca é tocado neste caminho."""
    monkeypatch.setattr(settings, "EVENT_DATAPLANE", "celery")
    dd = _common_patches(
        monkeypatch, {"dest-1": [{"e": 1}], "wazuh-default": [{"e": 2}]}
    )

    def _boom(*a, **k):
        raise AssertionError("não deveria produzir ao Kafka no caminho celery")

    monkeypatch.setattr(dataplane, "produce_delivery", _boom)

    pipeline._enqueue_routed([{"e": 1}], [_ROUTE])

    # Ambos os destinos despachados via dispatch_to_destination (via uniforme),
    # inclusive wazuh-default — sem special-case.
    assert dd.apply_async.call_count == 2
    dispatched = {
        c.kwargs["kwargs"]["destination_id"] for c in dd.apply_async.call_args_list
    }
    assert dispatched == {"dest-1", "wazuh-default"}
