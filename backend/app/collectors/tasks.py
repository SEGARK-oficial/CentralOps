"""Celery tasks.

Duas famílias:

- ``collect_vendor_logs_{priority,bulk}`` — coleta EDR real-time / bulk.
- ``dispatch_to_destination`` / ``dispatch_to_dlq`` — despacho de lotes por destino.

Backoff exponencial + jitter delegado ao Celery via ``autoretry_for``.
Tasks **não** executam código async diretamente — chamam
``asyncio.run(…)`` dentro do prefork worker; isso mantém o modelo
mental simples e permite escalar com ``--concurrency=N``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from celery.exceptions import SoftTimeLimitExceeded

from ..db import database
from . import circuit_breaker  # BreakerOpen terminal routing
from .celery_app import celery_app
from .dispatch_runtime import DISPATCH_RESULT_TIMEOUT, run_coro_blocking
from .metrics import (
    DELIVERY_LATENCY,
    DISPATCH_FAILURES,
    EVENTS_SENT,
    TASK_DURATION,
)
from .pipeline import (
    VendorAuthError,
    dispatch_batch_to_destination,
    run_collection_once,
)
from .delivery import TransientDeliveryError
from .vendors._rate_limit import VendorRateLimitedError

logger = logging.getLogger(__name__)

# Erros "infra" que sempre justificam retry automático + erros de auth
# do vendor (token foi invalidado in-flight — o próximo ciclo pega um
# token fresco via ``oauth_cache``).
# TransientDeliveryError adicionado — 429/5xx transitório por destino.
# NOTE: BreakerOpen é TERMINAL e NÃO deve estar aqui.
_RETRYABLE = (ConnectionError, TimeoutError, OSError, VendorAuthError, TransientDeliveryError)


def _dispose_db_pool_after_interrupt() -> None:
    """Descarta o pool de DB deste processo após um soft-timeout.

    O SIGALRM do soft-limit pode interromper o psycopg2 no MEIO de um read;
    a conexão volta ao pool com o protocolo corrompido e envenena as PRÓXIMAS
    tasks do processo ("error with status PGRES_TUPLES_OK and no message from
    the libpq" / IndexError no resultproxy — incidente jul/2026; o pre_ping
    não pega corrupção de offset em conexão que ainda responde SELECT 1).
    ``dispose()`` fecha as conexões em repouso; o próximo checkout abre
    conexão limpa — custo de 1 reconexão, best-effort.
    """
    try:
        database.engine.dispose()
    except Exception:  # pragma: no cover — best-effort, nunca mascara o erro original
        logger.warning("engine.dispose() pós-soft-timeout falhou", exc_info=True)


@celery_app.task(
    name="collectors.collect_vendor_logs_priority",
    bind=True,
    autoretry_for=_RETRYABLE,
    retry_backoff=True,
    retry_backoff_max=120,
    retry_jitter=True,
    max_retries=5,
    acks_late=True,
)
def collect_vendor_logs_priority(self, integration_id: int, stream: str) -> None:
    """Coleta real-time (EDR alerts, incidentes)."""
    with TASK_DURATION.labels(stream=stream, queue="collect.priority").time():
        try:
            asyncio.run(run_collection_once(integration_id, stream))
        except SoftTimeLimitExceeded:
            logger.error(
                "soft-timeout collect integration=%s stream=%s",
                integration_id, stream,
            )
            _dispose_db_pool_after_interrupt()
            raise
        except VendorRateLimitedError as exc:
            # Honra Retry-After do servidor em vez do backoff cego.
            # Logado em INFO porque é comportamento esperado sob carga
            # (Sophos rate-limit por partner, p.ex. 151 children × 3 streams).
            logger.info(
                "rate-limited integration=%s stream=%s vendor=%s retry_after=%ss",
                integration_id, stream, exc.vendor or "?", exc.retry_after,
            )
            raise self.retry(exc=exc, countdown=exc.retry_after, max_retries=10)


@celery_app.task(
    name="collectors.collect_vendor_logs_bulk",
    bind=True,
    autoretry_for=_RETRYABLE,
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=8,
    acks_late=True,
)
def collect_vendor_logs_bulk(self, integration_id: int, stream: str) -> None:
    """Coleta bulk/auditoria (NinjaOne activities, Sophos detections)."""
    with TASK_DURATION.labels(stream=stream, queue="collect.bulk").time():
        try:
            asyncio.run(run_collection_once(integration_id, stream))
        except SoftTimeLimitExceeded:
            logger.error(
                "soft-timeout collect integration=%s stream=%s",
                integration_id, stream,
            )
            _dispose_db_pool_after_interrupt()
            raise
        except VendorRateLimitedError as exc:
            logger.info(
                "rate-limited integration=%s stream=%s vendor=%s retry_after=%ss",
                integration_id, stream, exc.vendor or "?", exc.retry_after,
            )
            raise self.retry(exc=exc, countdown=exc.retry_after, max_retries=10)


@celery_app.task(
    name="collectors.dispatch_to_destination",
    bind=True,
    autoretry_for=_RETRYABLE,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=10,
    acks_late=True,
)
def dispatch_to_destination(
    self,
    destination_id: str,
    batch: list[dict],
    traceparent: str | None = None,
    tracestate: str | None = None,
) -> None:
    """Despacho genérico por destino.

    Lane ÚNICA de entrega, parametrizada por ``destination_id``. Não há lane
    dedicada do Wazuh: ``wazuh-default`` é um destino como qualquer
    outro. O ``_enqueue_dispatch`` o enfileira via o roteamento por regra,
    roteado para a shard queue ``dispatch.destination.N`` do destino.

    ``traceparent``/``tracestate`` (defaults None ⇒ back-compat) linkam
    o span ``dispatch.destination`` ao ciclo de coleta. No-op se OTEL off.
    """
    from . import tracing

    _carrier = {
        k: v
        for k, v in (("traceparent", traceparent), ("tracestate", tracestate))
        if v
    }
    with tracing.span_with_parent(
        "dispatch.destination",
        _carrier,
        **{"centralops.destination_id": destination_id, "centralops.batch_size": len(batch)},
    ):
        _run_dispatch_to_destination(self, destination_id, batch)


def _run_dispatch_to_destination(self, destination_id: str, batch: list[dict]) -> None:
    try:
        run_coro_blocking(
            dispatch_batch_to_destination(destination_id, batch),
            timeout=DISPATCH_RESULT_TIMEOUT,
        )
    except _RETRYABLE:
        raise
    except circuit_breaker.BreakerOpen:
        # breaker is OPEN — the batch was NEVER attempted. Route to DLQ
        # with a distinct error_kind so triage can tell "never tried" from
        # "exhausted retries". Terminal: no autoretry, no re-raise —
        # the batch is safely captured and the task succeeds (acks the message).
        DISPATCH_FAILURES.labels(
            target="destination", reason="breaker_open", destination_id=destination_id
        ).inc()
        dispatch_to_dlq.apply_async(
            kwargs={
                "batch": batch,
                "destination_id": destination_id,
                "kind": "unknown",
                "error_kind": "breaker_open",
            },
            queue="dispatch.dlq",
        )
        return
    except Exception:
        DISPATCH_FAILURES.labels(
            target="destination", reason="exhausted", destination_id=destination_id
        ).inc()
        # passes destination_id for structured DLQ persistence.
        dispatch_to_dlq.apply_async(
            kwargs={
                "batch": batch,
                "destination_id": destination_id,
                "kind": "unknown",
                "error_kind": "exhausted",
            },
            queue="dispatch.dlq",
        )
        raise


@celery_app.task(
    name="collectors.dispatch_to_dlq",
    bind=True,
    acks_late=True,
)
def dispatch_to_dlq(
    self,
    batch: list[dict],
    destination_id: str = "unknown",
    kind: str = "unknown",
    error_kind: str = "exhausted",
) -> None:
    """Recepção de dead-letter. Persiste uma row por evento no DB.

    Assinatura com defaults backward-compatible: mensagens antigas enfileiradas
    sem os kwargs extras desserializam normalmente (Celery preenche os defaults).

    Incrementa DLQ_TOTAL por destino/kind/error_kind.
    """
    from .delivery import persist_batch_dlq
    from .metrics import DLQ_TOTAL

    logger.error(
        "dispatch.dlq: lote falhou após retries destination_id=%s kind=%s "
        "error_kind=%s size=%d sample_ids=%s",
        destination_id,
        kind,
        error_kind,
        len(batch),
        [(e.get("_centralops") or {}).get("integration_id") for e in batch[:3]],
    )

    # persist to DestinationDeadLetter (best-effort — log only on fail).
    org_id: int | None = None
    if batch:
        meta = batch[0].get("_centralops") or {}
        raw_org = meta.get("organization_id")
        if isinstance(raw_org, int):
            org_id = raw_org

    persist_batch_dlq(
        batch,
        destination_id=destination_id,
        error_kind=error_kind,
        organization_id=org_id,
    )

    for _ in batch:
        DLQ_TOTAL.labels(
            destination_id=destination_id, kind=kind, error_kind=error_kind
        ).inc()


@celery_app.task(
    name="collectors.reprocess_quarantine_event",
    bind=True,
    autoretry_for=_RETRYABLE,
    retry_backoff=True,
    retry_backoff_max=120,
    retry_jitter=True,
    max_retries=3,
    acks_late=True,
)
def reprocess_quarantine_event(self, event_id: str, actor_user_id: int) -> None:
    """Reprocessa um evento de quarentena de forma assíncrona.

    Replica a lógica do endpoint single ``POST /api/quarantine/{id}/reprocess``,
    mas roda no worker Celery — é o backend do bulk reprocess.

    1. Abre session DB efêmera (workers não têm session de request).
    2. Carrega o evento; ausente/expirado/já reprocessado → no-op.
    3. Aplica mapping via ``attempt_reprocess`` (puro). Sucesso → enfileira
       em ``dispatch.wazuh`` + marca ``reprocessed_at``. Falha → atualiza
       ``error_kind/error_detail`` mantendo o item na quarentena.
    4. Audit log com ``actor_user_id``.
    """
    import json
    from datetime import datetime
    from sqlalchemy import select

    from ..db import database, models
    from .normalize.reprocess import attempt_reprocess

    with database.SessionLocal() as db:
        ev = db.get(models.QuarantineEvent, event_id)
        if ev is None:
            logger.warning(
                "reprocess_quarantine_event: event_id=%s não encontrado",
                event_id,
            )
            return
        if ev.reprocessed_at is not None:
            logger.info(
                "reprocess_quarantine_event: event_id=%s já reprocessado, no-op",
                event_id,
            )
            return
        now = datetime.utcnow()
        if ev.expires_at < now:
            logger.warning(
                "reprocess_quarantine_event: event_id=%s expirado, no-op",
                event_id,
            )
            return

        organization_id = None
        if ev.integration_id is not None:
            integration = db.scalar(
                select(models.Integration).where(
                    models.Integration.id == ev.integration_id
                )
            )
            if integration is not None:
                organization_id = integration.organization_id

        if organization_id is None:
            ev.error_kind = "missing_customer_id"
            ev.error_detail = (
                "bulk_reprocess: integration removida; impossível resolver org"
            )
            db.commit()
            return

        result = attempt_reprocess(
            raw_payload=ev.raw_payload,
            vendor=ev.vendor,
            event_type=ev.event_type,
            integration_id=ev.integration_id or 0,
            organization_id=organization_id,
            db=db,
        )

        error_kind_before = ev.error_kind

        if not result.success:
            ev.error_kind = result.error_kind or ev.error_kind
            ev.error_detail = result.error_detail
            if result.mapping_version_id:
                ev.mapping_version_id = result.mapping_version_id
            db.add(
                models.MappingAuditLog(
                    mapping_definition_id=None,
                    mapping_version_id=result.mapping_version_id,
                    integration_id=ev.integration_id,
                    action="reprocess_quarantine_failed",
                    user_id=actor_user_id,
                    username=f"bulk:user-{actor_user_id}",
                    user_role="bulk",
                    detail=json.dumps({
                        "quarantine_event_id": ev.id,
                        "vendor": ev.vendor,
                        "event_type": ev.event_type,
                        "error_kind_before": error_kind_before,
                        "error_kind_after": ev.error_kind,
                        "error_detail": result.error_detail,
                        "bulk": True,
                    }),
                )
            )
            db.commit()
            return

        if result.envelope is None:
            logger.error(
                "reprocess_quarantine_event: envelope nulo em result.success "
                "event_id=%s — bug interno",
                event_id,
            )
            return

        # Enfileira ANTES de marcar reprocessed_at (mesma ordem do
        # endpoint single — evita evento "reprocessado mas nunca enviado").
        # via o helper ÚNICO (Wazuh byte-idêntico + fan-out
        # aditivo + roteamento por regra), não direto a dispatch.wazuh.
        from .pipeline import _enqueue_dispatch

        _enqueue_dispatch([result.envelope])

        ev.reprocessed_at = now
        if result.mapping_version_id:
            ev.mapping_version_id = result.mapping_version_id

        centralops_meta = result.envelope.get("_centralops", {})
        event_id_dispatched = centralops_meta.get("event_id", "")

        db.add(
            models.MappingAuditLog(
                mapping_definition_id=None,
                mapping_version_id=result.mapping_version_id,
                integration_id=ev.integration_id,
                action="reprocess_quarantine_success",
                user_id=actor_user_id,
                username=f"bulk:user-{actor_user_id}",
                user_role="bulk",
                detail=json.dumps({
                    "quarantine_event_id": ev.id,
                    "vendor": ev.vendor,
                    "event_type": ev.event_type,
                    "error_kind_before": error_kind_before,
                    "error_kind_after": None,
                    "event_id_dispatched": event_id_dispatched,
                    "bulk": True,
                }),
            )
        )
        db.commit()


@celery_app.task(
    name="collectors.drain_destination_dlq",
    bind=True,
    acks_late=True,
    # No autoretry: the task itself is idempotent and iterates per-event.
    # Transient delivery errors on individual events are captured as updated
    # error_detail on their DLQ row rather than retrying the whole task.
)
def drain_destination_dlq(
    self,
    destination_id: str,
    event_ids: Optional[list[str]] = None,
    org_id: Optional[int] = None,
    global_scope: bool = True,
) -> dict:
    """DLQ drain / reprocess task.

    Re-delivers dead-lettered events to their destination via the existing
    ``dispatch_batch_to_destination`` send path (reuse, not rewrite).

    Per-event outcome:
      - **Success**: the DLQ row is hard-deleted (event is now in flight again).
      - **Failure**: the DLQ row stays; ``error_detail`` is updated so the
        operator can see the latest failure reason and retry later.

    Idempotent and safe for re-entrance:
      - Rows already deleted by a concurrent invocation are silently skipped.
      - The unique constraint on ``(destination_id, event_id)`` prevents
        double-writes if the event ends up DLQ'd again during re-delivery.

    Args:
        destination_id: The destination to drain.
        event_ids:      Specific event_ids to drain; ``None`` → all rows.
        org_id:         Org scope for the DLQ query (matches what the endpoint
                        resolved for the requesting user).
        global_scope:   When True the DLQ query is unscoped (global admin).
    """
    from ..db import database, repository

    delivered = 0
    failed = 0

    with database.SessionLocal() as db:
        repo = repository.DestinationRepository(db)
        rows = repo.list_dlq_for_reprocess(
            destination_id,
            event_ids=event_ids or None,
            org_id=org_id,
            global_scope=global_scope,
        )

        for dlq_row in rows:
            dlq_id = str(dlq_row.id)
            raw_payload = dlq_row.payload
            if not raw_payload:
                # Row has no payload — nothing to redeliver; remove it.
                repo.delete_dlq_entry(dlq_id)
                delivered += 1
                continue

            try:
                envelope = json.loads(str(raw_payload))
            except (TypeError, ValueError):
                # Malformed JSON — update error and skip rather than crash the task.
                repo.update_dlq_error(
                    dlq_id,
                    error_kind="reprocess_parse_error",
                    error_detail="payload could not be parsed as JSON during reprocess",
                )
                failed += 1
                continue

            try:
                run_coro_blocking(
                    dispatch_batch_to_destination(destination_id, [envelope]),
                    timeout=DISPATCH_RESULT_TIMEOUT,
                )
                # Successful delivery — remove from DLQ.
                repo.delete_dlq_entry(dlq_id)
                delivered += 1
                logger.info(
                    "drain_destination_dlq: delivered dlq_id=%s destination_id=%s event_id=%s",
                    dlq_id,
                    destination_id,
                    str(dlq_row.event_id),
                )
            except Exception as exc:
                # Keep the row; update the error so the operator can triage.
                error_detail = f"reprocess attempt failed: {type(exc).__name__}: {exc}"[:500]
                repo.update_dlq_error(
                    dlq_id,
                    error_kind="reprocess_failed",
                    error_detail=error_detail,
                )
                failed += 1
                logger.warning(
                    "drain_destination_dlq: re-delivery failed dlq_id=%s destination_id=%s: %s",
                    dlq_id,
                    destination_id,
                    error_detail,
                )

    logger.info(
        "drain_destination_dlq: done destination_id=%s delivered=%d failed=%d",
        destination_id,
        delivered,
        failed,
    )
    return {"delivered": delivered, "failed": failed}
