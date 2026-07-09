"""Delivery-layer helpers for the multi-destination dispatcher.

Three responsibilities, split by CHUNK to enable atomic PR commits:

CHUNK A — contract fixes:
  TransientDeliveryError — carries destination_id; lives in _RETRYABLE so Celery
  autoretries the **same destination task** on 429/5xx rather than silently
  succeeding.

CHUNK B — gated fan-out:
  resolve_destination_ids — sync, fail-safe lookup of enabled non-wazuh-default
  destinations for a given org_id.

CHUNK C — per-destination DLQ persistence:
  persist_rejected_to_dlq  — idempotent, best-effort, maps RejectedEvent → DLQ row.
  persist_batch_dlq        — whole-batch fallback used by dispatch_to_dlq.

All DB writes are sync (SessionLocal context manager) and are called from
`asyncio.to_thread` in the async dispatcher, keeping the event loop free.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Optional

from sqlalchemy.exc import IntegrityError

from ..db import database, models

if TYPE_CHECKING:
    from .output.base import RejectedEvent
    from .output.destinations.registry import DestinationConfig

logger = logging.getLogger(__name__)


# ── CHUNK A ───────────────────────────────────────────────────────────────────


class TransientDeliveryError(Exception):
    """Raised by dispatch_batch_to_destination when the sink returns a
    retryable failure (429 / 5xx).  Carries ``destination_id`` for structured
    logging.  Registered in ``tasks._RETRYABLE`` so Celery autoretries the
    **per-destination task** — isolating the retry to that destination only.

    MUST NOT be raised for circuit-breaker open (BreakerOpen): that is a
    terminal condition that goes to DLQ, not retry.
    """

    def __init__(self, destination_id: str) -> None:
        self.destination_id = destination_id
        super().__init__(
            f"transient delivery failure for destination_id={destination_id!r}"
        )


# ── CHUNK B ───────────────────────────────────────────────────────────────────


def resolve_destination_ids(org_id: Optional[int]) -> list[str]:
    """Return ids of enabled destinations for ``org_id``.

    O wazuh-default não é mais excluído — é um destino normal
    (syslog_rfc3164) tratado como qualquer outro (sem lane legada).

    Destinations with ``organization_id IS NULL`` are global (all orgs).
    Destinations scoped to ``org_id`` are included when ``org_id`` is given.

    Fail-safe: any exception returns [] and logs ERROR — collection must never
    break because of a bad destination row.
    """
    try:
        with database.SessionLocal() as session:
            rows = session.query(models.Destination).filter(
                models.Destination.enabled.is_(True),
            ).all()

            result: list[str] = []
            for row in rows:
                # NULL organization_id = global; scoped = must match org_id.
                if row.organization_id is None or row.organization_id == org_id:
                    result.append(str(row.id))
            return result
    except Exception:
        logger.exception(
            "resolve_destination_ids: falha ao consultar destinos (org_id=%s) — "
            "retornando [] (fail-safe, coleta não interrompida)",
            org_id,
        )
        return []


def resolve_dispatch_plan(org_id: Optional[int]) -> list[dict]:
    """Per-cycle dispatch plan for ``org_id``.

    Like :func:`resolve_destination_ids` but returns, per enabled
    destination, the data the producer needs to route + shed WITHOUT loading
    config again per batch flush:

        {destination_id, kind, shard_queue, queue_ceiling, backpressure}

    Inclui o wazuh-default (destino normal, sem lane legada).
    Resolved ONCE per collection cycle (off-loop). Fail-safe: [] on any error.
    """
    from .output.delivery_config import parse_delivery_lenient
    from .queues import dispatch_dest_shard_queue

    try:
        with database.SessionLocal() as session:
            rows = session.query(models.Destination).filter(
                models.Destination.enabled.is_(True),
            ).all()

            plan: list[dict] = []
            for row in rows:
                if not (row.organization_id is None or row.organization_id == org_id):
                    continue
                try:
                    raw_delivery = json.loads(row.delivery or "{}")
                except (TypeError, ValueError):
                    raw_delivery = {}
                dcfg = parse_delivery_lenient(row.kind, raw_delivery)
                dest_id = str(row.id)
                plan.append(
                    {
                        "destination_id": dest_id,
                        "kind": row.kind,
                        "shard_queue": dispatch_dest_shard_queue(dest_id),
                        "queue_ceiling": dcfg.queue_ceiling,
                        "backpressure": dcfg.backpressure,
                    }
                )
            return plan
    except Exception:
        logger.exception(
            "resolve_dispatch_plan: falha ao montar plano (org_id=%s) — "
            "retornando [] (fail-safe, coleta não interrompida)",
            org_id,
        )
        return []


# ── CHUNK C ───────────────────────────────────────────────────────────────────


def _build_event_map(batch: list[dict]) -> dict[str, dict]:
    """Build event_id → envelope map from a batch (uses _centralops.event_id)."""
    event_map: dict[str, dict] = {}
    for envelope in batch:
        meta = envelope.get("_centralops") or {}
        eid = meta.get("event_id")
        if eid:
            event_map[str(eid)] = envelope
    return event_map


def persist_rejected_to_dlq(
    dest_config: "DestinationConfig",
    rejected: list["RejectedEvent"],
    batch: list[dict],
) -> bool:
    """Persist per-event DLQ rows for a partial-batch rejection.

    Returns ``True`` when the rows are committed (or were already present),
    ``False`` when the write FAILED. The dispatcher uses this signal to retry
    the batch instead of silently acking it under ``acks_late`` (a swallowed
    DLQ write would otherwise drop rejected events).

    Idempotent + race-safe: dedup is enforced by the DB unique
    constraint ``uq_dest_dlq_dest_event`` via INSERT-and-catch on a per-row
    SAVEPOINT — a concurrent redelivery that loses the race rolls back only its
    savepoint and the rest commit. No check-then-act TOCTOU.

    Args:
        dest_config: resolved DestinationConfig for the destination.
        rejected:    list of RejectedEvent from DeliveryResult.rejected.
        batch:       original batch of envelopes (to look up raw payload by event_id).
    """
    if not rejected:
        return True

    event_map = _build_event_map(batch)

    try:
        with database.SessionLocal() as session:
            for rej in rejected:
                envelope = event_map.get(rej.event_id, {})
                try:
                    payload_json = json.dumps(envelope)
                except (TypeError, ValueError):
                    payload_json = "{}"

                # Org-scoping correto = org do EVENTO, não do destino:
                # num destino global (org=NULL, ex. wazuh-default) carimbar a org
                # do destino tornaria toda row NULL-orged e visível cross-tenant
                # se a leitura algum dia for aberta a um caller não-global. Usa a
                # org do envelope; cai p/ a do destino quando ausente.
                _meta = envelope.get("_centralops") or {}
                _evt_org = _meta.get("organization_id")
                row_org = (
                    int(_evt_org)
                    if isinstance(_evt_org, int)
                    else dest_config.organization_id
                )

                row = models.DestinationDeadLetter(
                    destination_id=dest_config.destination_id,
                    event_id=rej.event_id,
                    organization_id=row_org,
                    error_kind=rej.error_kind,
                    error_detail=rej.reason,
                    payload=payload_json,
                )
                try:
                    with session.begin_nested():
                        session.add(row)
                        session.flush()
                except IntegrityError:
                    # (destination_id, event_id) already present — skip.
                    logger.debug(
                        "persist_rejected_to_dlq: (destination_id=%s, event_id=%s) "
                        "já existe — skipping (E1 idempotente)",
                        dest_config.destination_id,
                        rej.event_id,
                    )

            session.commit()
        return True
    except Exception:
        logger.exception(
            "persist_rejected_to_dlq: falha ao persistir DLQ "
            "(destination_id=%s, %d itens rejeitados) — sinalizando falha "
            "para re-tentativa do lote (E3 durability)",
            dest_config.destination_id,
            len(rejected),
        )
        return False


def persist_batch_dlq(
    batch: list[dict],
    *,
    destination_id: str,
    error_kind: str,
    organization_id: Optional[int] = None,
) -> bool:
    """Whole-batch DLQ fallback used by ``dispatch_to_dlq`` task (CHUNK C).

    One DestinationDeadLetter row per event in ``batch``.  Uses
    ``_centralops.event_id`` for dedup; generates a fallback UUID-style key
    when absent (envelope antigo / malformed).

    Returns ``True`` on commit, ``False`` on failure (best-effort: the caller
    logs and the message has already exhausted retries). Idempotent + race-safe
    via INSERT-and-catch on a per-row SAVEPOINT.
    """
    if not batch:
        return True

    import uuid

    try:
        with database.SessionLocal() as session:
            for envelope in batch:
                meta = envelope.get("_centralops") or {}
                event_id = str(meta.get("event_id") or uuid.uuid4())

                try:
                    payload_json = json.dumps(envelope)
                except (TypeError, ValueError):
                    payload_json = "{}"

                row = models.DestinationDeadLetter(
                    destination_id=destination_id,
                    event_id=event_id,
                    organization_id=organization_id,
                    error_kind=error_kind,
                    error_detail="exhausted retries — whole-batch DLQ fallback",
                    payload=payload_json,
                )
                try:
                    with session.begin_nested():
                        session.add(row)
                        session.flush()
                except IntegrityError:
                    # Idempotent — duplicate (destination_id, event_id).
                    continue

            session.commit()
        return True
    except Exception:
        logger.exception(
            "persist_batch_dlq: falha ao persistir DLQ "
            "(destination_id=%s, batch_size=%d) — best-effort, ignorando",
            destination_id,
            len(batch),
        )
        return False
