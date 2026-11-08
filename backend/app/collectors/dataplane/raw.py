"""Tópico ``raw`` — o buffer durável da ingestão push (ADR-0018 Onda 2).

Antes, ``POST /api/ingest`` e o receptor syslog gravavam numa lista Redis que um
coletor drenava a cada 20 s. O Redis era o único lugar onde o evento push existia
entre o 200 e o processamento — sem réplica, com teto de memória e dividindo o
``volatile-lru`` com o dedupe. Agora o evento vai para ``<prefix>.raw`` com
``acks=all`` ANTES da resposta: um 200 quer dizer "está no log", e o papel
``processor`` (``dataplane/processor.py``) consome e roda o pipeline.

Registro = um lote de eventos de UM (integração, stream), enquadrado por tamanho
como o tópico de entrega. Desde a Onda 3 os coletores PULL também gravam aqui
("coleta só coleta", ``pipeline._collect_to_raw``) e mandam junto, em ``ids``, o
id de dedupe que o coletor calculou — o processor não instancia coletor. Registro
sem ``ids`` (push) tem o id derivado no processamento. Chave = ``<integração>:<stream>:<shard>``: o shard
espalha um stream grande por várias partições (e processadores), já que eventos
push não têm ordem a preservar.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

from ...core.config import settings

logger = logging.getLogger(__name__)

_RAW_VERSION = 1


class OversizedRawEventError(ValueError):
    """Um ÚNICO evento não cabe num registro do tópico raw (teto do broker)."""


def raw_topic() -> str:
    return f"{settings.KAFKA_TOPIC_PREFIX}.{settings.KAFKA_RAW_TOPIC}"


def raw_key(integration_id: int, stream: str, shard: Optional[int] = None) -> bytes:
    shards = max(1, int(settings.KAFKA_RAW_SHARDS_PER_STREAM or 1))
    if shard is None:
        shard = random.randrange(shards)
    return f"{integration_id}:{stream}:{shard % shards}".encode("utf-8")


def encode_raw(
    integration_id: int,
    stream: str,
    events: List[Mapping[str, Any]],
    *,
    received_at: Optional[float] = None,
    ids: Optional[List[str]] = None,
) -> bytes:
    doc: Dict[str, Any] = {
        "v": _RAW_VERSION,
        "integration_id": int(integration_id),
        "stream": stream,
        "received_at": received_at if received_at is not None else time.time(),
        "events": list(events),
    }
    if ids is not None:
        doc["ids"] = [str(i) for i in ids]
    return json.dumps(
        doc,
        separators=(",", ":"),
        default=str,
        ensure_ascii=False,
    ).encode("utf-8")


def decode_raw(value: bytes) -> Dict[str, Any]:
    """Valida o essencial; levanta ``ValueError`` em registro malformado."""
    doc = json.loads(value)
    if not isinstance(doc, dict):
        raise ValueError("registro raw não é um objeto")
    iid = doc.get("integration_id")
    stream = doc.get("stream")
    events = doc.get("events")
    if not isinstance(iid, int) or not isinstance(stream, str) or not stream:
        raise ValueError("registro raw sem integration_id/stream")
    if not isinstance(events, list):
        raise ValueError("registro raw sem lista de eventos")
    ids = doc.get("ids")
    if ids is not None and (not isinstance(ids, list) or len(ids) != len(events)):
        raise ValueError("registro raw com ids que não casam com os eventos")
    return doc


def _raw_frames(
    integration_id: int,
    stream: str,
    events: List[Mapping[str, Any]],
    limit: int,
    received_at: float,
    ids: Optional[List[str]] = None,
) -> Iterator[Tuple[List[Mapping[str, Any]], bytes]]:
    """Divide por bisseção até cada registro caber em ``limit`` bytes (os ``ids``
    acompanham os eventos na mesma posição)."""
    if not events:
        return
    value = encode_raw(integration_id, stream, events, received_at=received_at, ids=ids)
    if len(value) <= limit or len(events) == 1:
        yield events, value
        return
    mid = len(events) // 2
    left = ids[:mid] if ids is not None else None
    right = ids[mid:] if ids is not None else None
    yield from _raw_frames(integration_id, stream, events[:mid], limit, received_at, left)
    yield from _raw_frames(integration_id, stream, events[mid:], limit, received_at, right)


_raw_topic_ready = False


def _ensure_raw_topic() -> None:
    """Cria o tópico raw uma vez por processo (idempotente)."""
    global _raw_topic_ready
    if _raw_topic_ready:
        return
    from .kafka_transport import _ensure_topic, _producer_thread

    configs = {"retention.ms": str(int(settings.KAFKA_RAW_RETENTION_MS))}
    rf = int(settings.KAFKA_DELIVER_REPLICATION or 1)
    mir = settings.KAFKA_DELIVER_MIN_INSYNC_REPLICAS
    if mir is None:
        mir = 2 if rf >= 3 else (1 if rf == 2 else None)
    if mir:
        configs["min.insync.replicas"] = str(min(int(mir), rf))
    _producer_thread.run_in_loop(
        _ensure_topic(raw_topic(), int(settings.KAFKA_RAW_PARTITIONS), rf, configs),
        timeout=30,
    )
    _raw_topic_ready = True


def produce_raw(
    integration_id: int,
    stream: str,
    events: List[Mapping[str, Any]],
    ids: Optional[List[str]] = None,
) -> int:
    """Grava ``events`` no tópico raw e BLOQUEIA até o ack (``acks=all``).

    Devolve quantos eventos foram gravados. Levanta em falha do broker — o
    chamador responde 503 e a borda re-tenta; nada foi confirmado. Um evento
    sozinho maior que o teto levanta :class:`OversizedRawEventError` ANTES de
    gravar qualquer coisa do lote (o endpoint já recusa evento > 1 MiB no parse).
    ``ids`` (opcional) são os ids de dedupe, um por evento.
    """
    if not events:
        return 0
    if ids is not None and len(ids) != len(events):
        raise ValueError("ids e events com tamanhos diferentes")
    from .kafka_transport import _exceeds_request_limit, _producer_thread

    _ensure_raw_topic()
    key = raw_key(integration_id, stream)
    limit = max(1, int(settings.KAFKA_MAX_REQUEST_BYTES * 0.9))
    received_at = time.time()
    frames = list(
        _raw_frames(
            integration_id, stream, list(events), limit, received_at,
            list(ids) if ids is not None else None,
        )
    )
    for sub, value in frames:
        if len(sub) == 1 and _exceeds_request_limit(value, key):
            raise OversizedRawEventError(
                f"evento de {len(value)} bytes excede KAFKA_MAX_REQUEST_BYTES "
                f"(integration={integration_id} stream={stream})"
            )
    topic = raw_topic()
    for _sub, value in frames:
        _producer_thread._produce_value("__raw__", value, key, topic)
    return len(events)


async def produce_raw_async(
    integration_id: int,
    stream: str,
    events: List[Mapping[str, Any]],
    ids: Optional[List[str]] = None,
) -> int:
    """:func:`produce_raw` fora do event loop (o produce bloqueia até o ack)."""
    return await asyncio.to_thread(produce_raw, integration_id, stream, events, ids)
