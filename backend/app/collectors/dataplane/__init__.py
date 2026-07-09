"""Data-plane durável (control/data-plane split).

Transporte de EVENTO desacoplado do control-plane (Celery+Redis). O fan-out de
roteamento produz cada sub-lote num tópico Kafka/Redpanda; o role ``dispatcher``
consome e despacha. Ver :mod:`.kafka_transport`.
"""

from __future__ import annotations

from .kafka_transport import (  # noqa: F401
    decode_delivery,
    deliver_topic,
    encode_delivery,
    produce_delivery,
    run_dispatch_consumer,
    shutdown_producer,
)
