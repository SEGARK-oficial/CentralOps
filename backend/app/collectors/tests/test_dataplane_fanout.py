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
