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
  persist_batch_dlq        — whole-batch DLQ write (dispatcher poison, DLQ drain).

All DB writes are sync (SessionLocal context manager) and are called from
`asyncio.to_thread` in the async dispatcher, keeping the event loop free.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Optional

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


def _upsert_dead_letters(session: Any, rows: list[dict]) -> None:
    """Grava linhas da DLQ EM LOTE (ADR-0018 Onda 4c): ``INSERT ... ON CONFLICT
    (destination_id, event_id) DO UPDATE`` do motivo, em blocos de 500.

    Mesma semântica do laço por linha que substitui — inserir ou, se o evento já
    está na DLQ, ATUALIZAR ``error_kind``/``error_detail`` (o motivo mais recente
    na tela, e o sinal que o dreno usa) — mas numa instrução por bloco, em vez de
    um SAVEPOINT + flush + round-trip por evento. Repetição do mesmo evento no
    lote vale a última (o Postgres recusa o mesmo alvo duas vezes numa instrução).
    Banco sem upsert nativo segue pelo laço."""
    if not rows:
        return
    import uuid
    from datetime import datetime

    by_key: dict = {}
    for row in rows:
        by_key[(row["destination_id"], row["event_id"])] = row
    now = datetime.utcnow()
    unique = [
        {"id": str(uuid.uuid4()), "created_at": now, **row} for row in by_key.values()
    ]
    dialect = session.get_bind().dialect.name
    if dialect in ("postgresql", "sqlite"):
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        table = models.DestinationDeadLetter.__table__
        for start in range(0, len(unique), 500):
            stmt = insert(table).values(unique[start:start + 500])
            stmt = stmt.on_conflict_do_update(
                index_elements=["destination_id", "event_id"],
                set_={
                    "error_kind": stmt.excluded.error_kind,
                    "error_detail": stmt.excluded.error_detail,
                },
            )
            session.execute(stmt)
        return
    for row in unique:  # pragma: no cover — banco sem upsert nativo
        try:
            with session.begin_nested():
                session.add(models.DestinationDeadLetter(**row))
                session.flush()
        except IntegrityError:
            session.query(models.DestinationDeadLetter).filter(
                models.DestinationDeadLetter.destination_id == row["destination_id"],
                models.DestinationDeadLetter.event_id == row["event_id"],
            ).update(
                {
                    models.DestinationDeadLetter.error_kind: row["error_kind"],
                    models.DestinationDeadLetter.error_detail: row["error_detail"],
                },
                synchronize_session=False,
            )


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
        rows: list[dict] = []
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
            # Já existe (destination_id, event_id)? O upsert ATUALIZA o motivo em
            # vez de pular, por dois motivos. O operador: pular deixava o motivo
            # VELHO na tela (um evento que falhou por breaker e depois por schema
            # continuava dizendo "breaker"). O dreno: ele carimba a linha com o
            # sentinela ``reprocessing`` antes de reenviar e decide o desfecho
            # olhando se o carimbo sobreviveu — pular faria o dreno apagar um
            # evento que o destino recusou. Uma linha por (destino, evento).
            rows.append(
                {
                    "destination_id": dest_config.destination_id,
                    "event_id": rej.event_id,
                    "organization_id": row_org,
                    "error_kind": rej.error_kind,
                    "error_detail": rej.reason,
                    "payload": payload_json,
                }
            )
        with database.SessionLocal() as session:
            _upsert_dead_letters(session, rows)
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


#: Texto do ``error_detail`` por ``error_kind`` no fallback de lote inteiro.
#:
#: Existe porque o texto era HARDCODED como "exhausted retries" para os quatro
#: chamadores, e em três deles nenhum retry aconteceu. Num incidente real, 10.852
#: linhas de ``breaker_open`` afirmavam ter esgotado tentativas sem terem sido
#: enviadas uma única vez, o que manda a investigação para o lugar errado: quem
#: lê "esgotou retries" procura o defeito no destino, e o defeito estava na
#: configuração local que abriu o disjuntor.
_DETALHE_POR_KIND = {
    "breaker_open": (
        "disjuntor aberto para este destino — o lote NÃO foi enviado. "
        "Corrija a causa das falhas anteriores e reprocesse."
    ),
    "exhausted": "esgotou as tentativas de reenvio ao destino",
    "destination_missing": (
        "o destino não existe mais quando o lote foi despachado — nada foi enviado"
    ),
    "cross_tenant_destination": (
        "bloqueio fail-closed: o destino é de outra organização — nada foi enviado"
    ),
    "size": (
        "evento maior que o teto de transporte — NÃO foi enviado. Reduza o evento "
        "ou suba o teto, e reprocesse."
    ),
}
_DETALHE_PADRAO = "lote inteiro para DLQ (sem detalhe por evento)"


def persist_batch_dlq(
    batch: list[dict],
    *,
    destination_id: str,
    error_kind: str,
    organization_id: Optional[int] = None,
    error_detail: Optional[str] = None,
) -> bool:
    """Whole-batch DLQ write (CHUNK C): poison do dispatcher e dreno da DLQ.

    One DestinationDeadLetter row per event in ``batch``.  Uses
    ``_centralops.event_id`` for dedup; generates a fallback UUID-style key
    when absent (envelope antigo / malformed).

    ``error_detail`` explícito vence; sem ele, o texto é derivado de
    ``error_kind`` (ver ``_DETALHE_POR_KIND``) em vez de afirmar retry que não
    houve. ``persist_rejected_to_dlq`` já aceitava detalhe por evento; esta era
    a face do lote que não aceitava.

    Returns ``True`` on commit, ``False`` on failure (best-effort: the caller
    logs and the message has already exhausted retries). Idempotent + race-safe
    via INSERT-and-catch on a per-row SAVEPOINT.
    """
    if not batch:
        return True

    detalhe = error_detail or _DETALHE_POR_KIND.get(error_kind, _DETALHE_PADRAO)

    import uuid

    try:
        rows = []
        for envelope in batch:
            meta = envelope.get("_centralops") or {}
            try:
                payload_json = json.dumps(envelope)
            except (TypeError, ValueError):
                payload_json = "{}"
            # Já existe (destination_id, event_id): o upsert atualiza o motivo
            # — mesma razão de ``persist_rejected_to_dlq``.
            rows.append(
                {
                    "destination_id": destination_id,
                    "event_id": str(meta.get("event_id") or uuid.uuid4()),
                    "organization_id": organization_id,
                    "error_kind": error_kind,
                    "error_detail": detalhe,
                    "payload": payload_json,
                }
            )
        with database.SessionLocal() as session:
            _upsert_dead_letters(session, rows)
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
