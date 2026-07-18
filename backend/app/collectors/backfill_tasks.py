"""Task Celery para backfill de janela histórica.

O worker de backfill roda na fila ``collect.backfill`` com concurrency
baixa (2 workers) para não competir com o polling normal. Cada job:

1. Carrega a ``BackfillJob`` do banco.
2. Para cada stream: executa ``run_backfill_collection_once`` com cursor
   isolado (não toca ``CollectionState`` do polling).
3. Eventos passam pelo mesmo pipeline (mapping + envelope + dispatch).
4. Dedupe Redis funciona normalmente — eventos já vistos não são
   re-enviados ao Wazuh.
5. A cada iteração, recarrega o job e verifica se foi cancelado.

Garantias:
- Cursor de backfill é salvo em ``BackfillJob.current_cursor`` (não em
  ``CollectionState``). O cursor de polling normal permanece intocado.
- Em caso de erro não-retryable: job → status="failed", last_error=str(exc).
- Em caso de cancelamento: worker detecta status="cancelled" e sai limpo.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from .celery_app import celery_app
from ..db import database, models

logger = logging.getLogger(__name__)


# ── HMAC signing de cursor BackfillJob ──────────────
# Atacante com acesso SQL direto pode corromper BackfillJob.current_cursor
# → SSRF/path traversal no vendor. Assinamos o cursor com APP_MASTER_KEY
# via HMAC-SHA256 para detectar tampering.


def _sign_cursor(cursor_dict: Dict[str, Any], secret: str) -> str:
    """Serializa cursor dict + HMAC-SHA256, retorna JSON assinado.

    Formato persistido:
        {"_payload": {...}, "_sig": "<64-hex-chars>"}

    Args:
        cursor_dict: cursor bruto do collector (JSON-serializável).
        secret: APP_MASTER_KEY ou equivalente.

    Returns:
        JSON string com _payload e _sig.
    """
    payload_str = json.dumps(cursor_dict, sort_keys=True, separators=(",", ":"), default=str)
    sig = hmac.new(
        secret.encode(),
        payload_str.encode(),
        hashlib.sha256,
    ).hexdigest()
    return json.dumps({"_payload": cursor_dict, "_sig": sig}, separators=(",", ":"), default=str)


def _verify_cursor(serialized: str, secret: str) -> Dict[str, Any]:
    """Verifica HMAC do cursor e retorna o payload.

    Backward-compat: cursores sem ``_sig`` (job antigo sem assinatura)
    são aceitos com warning de migração — evita quebra em upgrade rolling.

    Args:
        serialized: JSON string (pode ser assinado ou legado sem _sig).
        secret: APP_MASTER_KEY ou equivalente.

    Returns:
        cursor dict (payload interno).

    Raises:
        ValueError: se a assinatura estiver presente mas inválida (tampering).
        json.JSONDecodeError: se o JSON for inválido.
    """
    obj = json.loads(serialized)

    # Cursor legado (sem _payload/_sig) — backward compat durante upgrade.
    if "_sig" not in obj:
        logger.warning(
            "cursor sem assinatura HMAC (job legado) — aceito mas migração necessária",
            extra={"event": "backfill.cursor_legacy_unsigned"},
        )
        return obj  # Cursor legado é o próprio dict

    payload = obj["_payload"]
    received_sig = obj["_sig"]

    payload_str = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    expected_sig = hmac.new(
        secret.encode(),
        payload_str.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(received_sig, expected_sig):
        raise ValueError(
            "cursor signature mismatch — possível tampering no BackfillJob.current_cursor"
        )

    return payload  # type: ignore[return-value]


# ── Função de coleta com janela explícita ──────────────────────────────


async def run_backfill_collection_once(
    integration_id: int,
    stream: str,
    from_ts: datetime,
    to_ts: datetime,
    initial_cursor: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Executa um ciclo de coleta histórica com janela explícita.

    Diferenças em relação a ``run_collection_once``:
    - Usa cursor explícito (``initial_cursor``) em vez do cursor do polling.
    - Não persiste cursor em ``CollectionState`` — apenas retorna o cursor
      final para que o worker o salve em ``BackfillJob.current_cursor``.
    - Passa ``from_ts`` / ``to_ts`` para o collector via
      ``CollectorContext.cursor`` como metadado de janela.

    Retorna dict com:
    - ``cursor``: cursor final após a coleta.
    - ``events_collected``: número de eventos que passaram pelo pipeline.
    - ``events_dispatched``: número de eventos enviados para a fila de dispatch.
    """
    import aiohttp

    import redis.asyncio as redis_async
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from ..core.config import settings
    from ..db import database, models

    from . import quarantine
    from .auth.oauth_cache import get_or_refresh_token
    from .base import CollectorContext
    from .config_loader import get_collector_config
    from .domain_limiter import DomainLimiter
    from .metrics import DEDUPE_DROPS, EVENTS_TOTAL, NORMALIZE_LATENCY, QUARANTINE_TOTAL
    from .normalize.engine import (
        MappingError,
        MappingRequiredFieldError,
        default_engine,
    )
    from .normalize.envelope import EnvelopeContext, build_envelope, has_customer_id
    from .pipeline import (
        VendorAuthError,
        _aiohttp_session,
        _enqueue_dispatch,
        _headers_for,
        _load_current_mapping,
        _quarantine_async,
    )
    from .rate_limit_redis import RedisRateLimiter
    from .registry import get as registry_get, has as registry_has
    from .state.dedupe import claim, compute_message_id

    redis = redis_async.from_url(
        settings.REDIS_URL or "redis://localhost:6379/0",
        decode_responses=True,
    )

    events_collected = 0
    events_dispatched = 0
    final_cursor: Dict[str, Any] = initial_cursor or {}

    try:
        # Carrega integration.
        with database.SessionLocal() as db:
            integration = db.scalar(
                select(models.Integration)
                .where(models.Integration.id == integration_id)
                .options(selectinload(models.Integration.organization))
            )
            if not integration or not integration.is_active:
                logger.warning(
                    "integration inativa ou inexistente no backfill",
                    extra={
                        "event": "backfill.skip_inactive",
                        "integration_id": integration_id,
                    },
                )
                return {
                    "cursor": final_cursor,
                    "events_collected": 0,
                    "events_dispatched": 0,
                }
            platform = integration.platform
            organization_id = integration.organization_id
            organization_name: Optional[str] = (
                integration.organization.name
                if integration.organization is not None
                else None
            )
            # ``customer_id`` do envelope = ``Organization.id`` interno
            # (mesma semântica do ``pipeline.py`` — backfill é o mesmo fluxo, só
            # com janela histórica). Sem dependência do IRIS.
            envelope_customer_id: Optional[int] = organization_id
            db.expunge(integration)

        if not registry_has(platform, stream):
            logger.error(
                "collector não registrado no backfill",
                extra={
                    "event": "backfill.unregistered_collector",
                    "platform": platform,
                    "stream": stream,
                },
            )
            return {
                "cursor": final_cursor,
                "events_collected": 0,
                "events_dispatched": 0,
            }

        registration = registry_get(platform, stream)
        collector_cls = registration.collector_cls
        event_type = collector_cls.event_type

        # Token OAuth.
        access_token = await get_or_refresh_token(
            redis,
            integration_id=integration_id,
            refresh_fn=registration.refresh_fn,
            vendor=platform,
        )
        headers = _headers_for(platform, integration, access_token)

        # Config + mapping.
        config = await get_collector_config(redis)
        mapping_current = await asyncio.to_thread(
            _load_current_mapping, platform, event_type
        )

        rate_limiter = RedisRateLimiter(redis, config.rate_limits_by_vendor)
        domain_limiter = DomainLimiter(redis, config.domain_concurrency_limits)

        # Cursor de backfill: inclui metadado de janela para que o
        # collector possa filtrar eventos fora da janela.
        backfill_cursor: Dict[str, Any] = {
            **(initial_cursor or {}),
            "backfill_from_ts": from_ts.isoformat(),
            "backfill_to_ts": to_ts.isoformat(),
        }

        batch: List[Dict[str, Any]] = []
        last_flush = time.monotonic()

        async with _aiohttp_session() as session:
            ctx = CollectorContext(
                integration_id=integration_id,
                organization_id=organization_id,
                platform=platform,
                headers=headers,
                session=session,
                cursor=backfill_cursor,
                domain_limiter=domain_limiter,
                rate_limiter=rate_limiter,
                redis=redis,
                # BACKFILL: drena a janela INTEIRA num run — o teto por-ciclo dos
                # coletores é só p/ o polling agendado (que retoma via beat). O
                # orquestrador de backfill invoca collect() UMA vez e marca o job
                # completo; capar aqui truncaria o job silenciosamente.
                bounded_per_cycle=False,
            )
            collector = collector_cls(ctx)

            try:
                async for raw_event in collector.collect():
                    msg_id = collector.extract_message_id(raw_event)
                    if not msg_id:
                        msg_id = compute_message_id(raw_event)

                    # Dedupe normal — eventos já vistos pelo polling não
                    # são re-enviados.
                    if not await claim(
                        redis,
                        integration_id,
                        msg_id,
                        ttl_days=config.dedupe_ttl_days,
                    ):
                        DEDUPE_DROPS.labels(
                            vendor=platform, stream=stream
                        ).inc()
                        continue

                    # Normalização.
                    if mapping_current is None:
                        await _quarantine_async(
                            integration_id=integration_id,
                            vendor=platform,
                            event_type=event_type,
                            raw=raw_event,
                            error_kind=quarantine.ERROR_KIND_MISSING_MAPPING,
                            error_detail="no current MappingVersion configured (backfill)",
                        )
                        QUARANTINE_TOTAL.labels(
                            vendor=platform,
                            event_type=event_type,
                            error_kind=quarantine.ERROR_KIND_MISSING_MAPPING,
                        ).inc()
                        continue

                    mapping_version_id, rules, dsl_version = mapping_current
                    normalize_started = time.monotonic()
                    try:
                        applied = default_engine.apply(
                            mapping_version_id, rules, raw_event,
                            dsl_version=dsl_version,
                        )
                    except MappingRequiredFieldError as exc:
                        await _quarantine_async(
                            integration_id=integration_id,
                            vendor=platform,
                            event_type=event_type,
                            raw=raw_event,
                            error_kind=quarantine.ERROR_KIND_MAP,
                            error_detail=str(exc),
                            mapping_version_id=mapping_version_id,
                        )
                        QUARANTINE_TOTAL.labels(
                            vendor=platform,
                            event_type=event_type,
                            error_kind=quarantine.ERROR_KIND_MAP,
                        ).inc()
                        continue
                    except MappingError as exc:
                        logger.warning(
                            "backfill_pipeline: mapping error vendor=%s event_type=%s: %s",
                            platform, event_type, exc,
                        )
                        await _quarantine_async(
                            integration_id=integration_id,
                            vendor=platform,
                            event_type=event_type,
                            raw=raw_event,
                            error_kind=quarantine.ERROR_KIND_MAP,
                            error_detail=str(exc),
                            mapping_version_id=mapping_version_id,
                        )
                        QUARANTINE_TOTAL.labels(
                            vendor=platform,
                            event_type=event_type,
                            error_kind=quarantine.ERROR_KIND_MAP,
                        ).inc()
                        continue

                    envelope_ctx = EnvelopeContext(
                        vendor=platform,
                        integration_id=integration_id,
                        # = Organization.id interno (resolvido acima).
                        customer_id=envelope_customer_id,
                        customer_name=organization_name,
                        stream=stream,
                        event_type=event_type,
                        mapping_version_id=mapping_version_id,
                    )
                    envelope = build_envelope(
                        raw_event,
                        applied.output,
                        envelope_ctx,
                        vendor_msg_id=msg_id,
                    )
                    NORMALIZE_LATENCY.labels(
                        vendor=platform, event_type=event_type
                    ).observe(time.monotonic() - normalize_started)

                    # customer_id obrigatório.
                    if not has_customer_id(envelope):
                        await _quarantine_async(
                            integration_id=integration_id,
                            vendor=platform,
                            event_type=event_type,
                            raw=raw_event,
                            error_kind=quarantine.ERROR_KIND_MISSING_CUSTOMER_ID,
                            error_detail="customer_id resolved to empty (backfill)",
                            mapping_version_id=mapping_version_id,
                        )
                        QUARANTINE_TOTAL.labels(
                            vendor=platform,
                            event_type=event_type,
                            error_kind=quarantine.ERROR_KIND_MISSING_CUSTOMER_ID,
                        ).inc()
                        continue

                    batch.append(envelope)
                    events_collected += 1

                    if (
                        len(batch) >= config.collector_batch_size
                        or (time.monotonic() - last_flush)
                        >= config.collector_batch_flush_seconds
                    ):
                        _enqueue_dispatch(batch)
                        EVENTS_TOTAL.labels(
                            vendor=platform,
                            tenant=str(organization_id),
                            stream=stream,
                        ).inc(len(batch))
                        events_dispatched += len(batch)
                        batch = []
                        last_flush = time.monotonic()

            except aiohttp.ClientResponseError as exc:
                if exc.status == 401:
                    from .auth.oauth_cache import invalidate as invalidate_token
                    logger.warning(
                        "vendor retornou 401 no backfill; invalidando cache OAuth",
                        extra={
                            "event": "backfill.vendor_auth_error",
                            "integration_id": integration_id,
                            "stream": stream,
                        },
                    )
                    try:
                        await invalidate_token(redis, integration_id)
                    except Exception:
                        logger.exception(
                            "backfill_pipeline: falha ao invalidar cache oauth"
                        )
                    raise VendorAuthError(integration_id, platform) from exc
                raise

            if batch:
                _enqueue_dispatch(batch)
                EVENTS_TOTAL.labels(
                    vendor=platform,
                    tenant=str(organization_id),
                    stream=stream,
                ).inc(len(batch))
                events_dispatched += len(batch)

        # Captura cursor final do contexto.
        final_cursor = ctx.cursor or {}

        logger.info(
            "backfill ok",
            extra={
                "event": "backfill.complete",
                "integration_id": integration_id,
                "stream": stream,
                "events_count": events_collected,
                "events_dispatched": events_dispatched,
            },
        )

    finally:
        await redis.aclose()

    return {
        "cursor": final_cursor,
        "events_collected": events_collected,
        "events_dispatched": events_dispatched,
    }


# ── Task Celery ────────────────────────────────────────────────────────


@celery_app.task(
    name="collectors.collect_backfill_job",
    bind=True,
    queue="collect.backfill",
    max_retries=3,
    acks_late=True,
    task_reject_on_worker_lost=True,
)
def collect_backfill_job(self, job_id: str) -> Dict[str, Any]:
    """Executa backfill de uma janela histórica.

    Reusa o pipeline de coleta (mapping + envelope + dispatch) com cursor
    isolado: o cursor do polling normal (``CollectionState``) não é tocado.

    Fluxo:
    1. Carrega o job. Se status != "pending" | "running": aborta.
    2. Marca status="running", started_at=now.
    3. Para cada stream em job.streams:
       a. Executa ``run_backfill_collection_once`` com janela explícita.
       b. Persiste cursor em ``BackfillJob.current_cursor`` (não em
          ``CollectionState``).
       c. Atualiza contadores de progresso.
       d. Verifica cancelamento a cada iteração de stream.
    4. Marca status="completed", finished_at=now.
    5. Em caso de erro: status="failed", last_error=str(exc).

    Cancellamento: o worker checa ``job.status == 'cancelled'`` antes de
    processar cada stream. O endpoint de cancel chama ``AsyncResult.revoke``
    para interromper o worker antes do próximo ``execute()``.
    """
    logger.info(
        "backfill task iniciando",
        extra={"event": "backfill.task_start", "job_id": job_id},
    )

    # 1. Carrega job do banco.
    with database.SessionLocal() as db:
        job = db.get(models.BackfillJob, job_id)
        if job is None:
            logger.error(
                "backfill job não encontrado",
                extra={"event": "backfill.job_not_found", "job_id": job_id},
            )
            return {"status": "not_found"}

        if job.status not in {"pending", "running"}:
            logger.info(
                "backfill job em status inesperado; ignorando",
                extra={
                    "event": "backfill.task_skip_status",
                    "job_id": job_id,
                    "status": job.status,
                },
            )
            return {"status": job.status, "skipped": True}

        # Copia atributos necessários antes de fechar a sessão.
        integration_id = job.integration_id
        streams: List[str] = []
        try:
            streams = json.loads(job.streams)
        except (TypeError, ValueError):
            logger.error(
                "backfill_task: streams inválido job_id=%s", job_id
            )
        from_ts = job.from_ts
        to_ts = job.to_ts

        # 2. Marca como running.
        now = datetime.utcnow()
        job.status = "running"
        job.started_at = now
        db.commit()

    total_streams = len(streams)
    total_collected = 0
    total_dispatched = 0
    current_cursor: Optional[Dict[str, Any]] = None

    try:
        for stream_index, stream in enumerate(streams):
            # Verifica cancelamento antes de cada stream.
            with database.SessionLocal() as db:
                fresh = db.get(models.BackfillJob, job_id)
                if fresh is None or fresh.status == "cancelled":
                    logger.info(
                        "backfill job cancelado",
                        extra={"event": "backfill.task_cancelled", "job_id": job_id},
                    )
                    return {"status": "cancelled", "job_id": job_id}
                # Recupera cursor parcial salvo de execuções anteriores.
                # Verifica HMAC antes de usar.
                if fresh.current_cursor:
                    try:
                        from ..core.config import settings as _settings
                        current_cursor = _verify_cursor(
                            fresh.current_cursor, _settings.APP_MASTER_KEY
                        )
                    except (TypeError, ValueError, json.JSONDecodeError) as _exc:
                        logger.error(
                            "cursor inválido ou adulterado job_id=%s: %s — ignorando cursor",
                            job_id,
                            _exc,
                        )
                        current_cursor = None

            logger.info(
                "backfill processando stream",
                extra={
                    "event": "backfill.stream_start",
                    "stream": stream,
                    "stream_index": stream_index + 1,
                    "total_streams": total_streams,
                    "job_id": job_id,
                },
            )

            # Executa a coleta histórica para este stream.
            result = asyncio.run(
                run_backfill_collection_once(
                    integration_id=integration_id,
                    stream=stream,
                    from_ts=from_ts,
                    to_ts=to_ts,
                    initial_cursor=current_cursor,
                )
            )

            total_collected += result["events_collected"]
            total_dispatched += result["events_dispatched"]
            current_cursor = result["cursor"]

            # Persiste progresso a cada stream concluído.
            progress_pct = int(((stream_index + 1) / total_streams) * 100)
            with database.SessionLocal() as db:
                fresh = db.get(models.BackfillJob, job_id)
                if fresh is None:
                    logger.error(
                        "backfill job desapareceu do banco durante execução",
                        extra={"event": "backfill.job_vanished", "job_id": job_id},
                    )
                    return {"status": "error", "job_id": job_id}

                fresh.events_collected = total_collected
                fresh.events_dispatched = total_dispatched
                fresh.progress_pct = progress_pct
                # Assina cursor antes de persistir.
                from ..core.config import settings as _settings
                fresh.current_cursor = _sign_cursor(
                    current_cursor or {}, _settings.APP_MASTER_KEY
                )
                db.commit()

        # 4. Conclui com sucesso.
        with database.SessionLocal() as db:
            fresh = db.get(models.BackfillJob, job_id)
            if fresh and fresh.status != "cancelled":
                fresh.status = "completed"
                fresh.finished_at = datetime.utcnow()
                fresh.events_collected = total_collected
                fresh.events_dispatched = total_dispatched
                fresh.progress_pct = 100
                db.commit()

        logger.info(
            "backfill task concluído",
            extra={
                "event": "backfill.task_complete",
                "job_id": job_id,
                "events_count": total_collected,
                "events_dispatched": total_dispatched,
            },
        )
        return {
            "status": "completed",
            "job_id": job_id,
            "events_collected": total_collected,
            "events_dispatched": total_dispatched,
        }

    except Exception as exc:
        # 5. Marca como falho.
        logger.exception(
            "backfill task falhou",
            extra={"event": "backfill.task_error", "job_id": job_id},
        )
        with database.SessionLocal() as db:
            fresh = db.get(models.BackfillJob, job_id)
            if fresh and fresh.status not in {"cancelled", "completed"}:
                fresh.status = "failed"
                fresh.finished_at = datetime.utcnow()
                fresh.last_error = str(exc)[:2000]
                db.commit()

        raise
