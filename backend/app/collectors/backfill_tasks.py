"""Task Celery para backfill de janela histórica.

O worker de backfill roda na fila ``collect.backfill`` com concurrency
baixa (2 workers) para não competir com o polling normal. Cada job:

1. Carrega a ``BackfillJob`` do banco.
2. Para cada stream: executa ``run_backfill_collection_once`` com cursor
   isolado (não toca ``CollectionState`` do polling).
3. Eventos passam pelo MESMO estágio de processamento da coleta ao vivo
   (ADR-0018 Onda 3d): vão ao tópico raw marcados como históricos e o papel
   ``processor`` os trata.
4. Dedupe pela janela da partição do processor — o que ela ainda lembra não é
   re-enviado.
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
    *,
    job_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Coleta histórica de uma janela explícita, processada pelo MESMO estágio
    da coleta ao vivo (ADR-0018 Onda 3d).

    Antes isto era uma cópia parcial do pipeline: mapping, envelope e
    roteamento, sem validação OCSF, sem enriquecimento, sem classificação em
    voo, sem supressão, sem amostras de drift e sem soltar as claims de dedupe
    quando o job falhava. O mesmo evento saía diferente pela coleta ao vivo e
    pelo backfill. Agora a coleta histórica só BUSCA: os eventos crus vão para o
    tópico raw (``RawProducer``) marcados como históricos, e o papel
    ``processor`` os trata como trata a coleta ao vivo — presença de ausência
    sim, batimento do observador não. ``job_id`` só identifica o job no log.

    Diferenças em relação ao ciclo ao vivo, e só estas:
    - cursor explícito (``initial_cursor``) com a janela, nunca o
      ``CollectionState`` — o cursor final volta para ``BackfillJob``;
    - ``bounded_per_cycle=False``: drena a janela inteira num run.

    Retorna ``cursor``, ``events_collected`` (buscados na fonte) e
    ``events_dispatched`` (gravados no raw).
    """
    import aiohttp

    # Tudo pelo módulo ``pipeline`` (resolvido na chamada): é o mesmo estágio, as
    # mesmas dependências e os mesmos pontos de troca que os testes do ciclo usam.
    from . import pipeline as _pl
    from .celery_app import get_worker_redis

    redis = get_worker_redis()
    final_cursor: Dict[str, Any] = initial_cursor or {}
    events_collected = 0
    skipped = {"cursor": final_cursor, "events_collected": 0, "events_dispatched": 0}

    sink: Optional[Any] = None

    try:
        view = _pl._load_cycle_integration(integration_id, stream)
        if view is None:
            return skipped
        registration = _pl._registration_for(view.platform, stream)
        if registration is None:
            return skipped
        integration = view.integration
        platform = view.platform
        organization_id = view.organization_id

        access_token = await _pl.get_or_refresh_token(
            redis,
            integration_id=integration_id,
            refresh_fn=registration.refresh_fn,
            vendor=platform,
        )
        headers = _pl._headers_for(platform, integration, access_token)

        config = await _pl.get_collector_config(redis)
        sink = _pl.RawProducer(integration_id, stream, config, historical=True)

        rate_limiter = _pl.RedisRateLimiter(redis, config.rate_limits_by_vendor)
        domain_limiter = _pl.DomainLimiter(redis, config.domain_concurrency_limits)

        # Cursor de backfill: inclui metadado de janela para que o
        # collector possa filtrar eventos fora da janela.
        backfill_cursor: Dict[str, Any] = {
            **(initial_cursor or {}),
            "backfill_from_ts": from_ts.isoformat(),
            "backfill_to_ts": to_ts.isoformat(),
        }

        async with _pl._aiohttp_session() as session:
            ctx = _pl.CollectorContext(
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
                # O backfill HONRA o filtro de coleta da integração. Ignorá-lo
                # recriaria, por outra porta, o problema que o filtro existe para
                # resolver: a janela voltaria cheia de eventos que a regra de
                # roteamento descarta em seguida, gastando o job inteiro com ruído.
                # Para recuperar de propósito o que foi filtrado, o caminho é
                # explícito — desligar o filtro, rodar o backfill, religar.
                filters=_pl._load_collection_filters(integration, registration, stream),
            )
            collector = registration.collector_cls(ctx)
            try:
                async for raw_event in collector.collect():
                    events_collected += 1
                    await sink.process(raw_event, _pl._resolve_message_id(collector, raw_event))
            except aiohttp.ClientResponseError as exc:
                await _pl._raise_vendor_auth_error(exc, redis, integration_id, stream, platform)
            # Flush TERMINAL: grava o resto no raw. Antes dele o cursor do job
            # não pode avançar.
            await sink.flush()

        # Captura cursor final do contexto.
        final_cursor = ctx.cursor or {}

        logger.info(
            "backfill ok",
            extra={
                "event": "backfill.complete",
                "integration_id": integration_id,
                "stream": stream,
                "events_count": events_collected,
                "events_dispatched": sink.events_count,
            },
        )

    finally:
        await redis.aclose()

    return {
        "cursor": final_cursor,
        "events_collected": events_collected,
        "events_dispatched": sink.events_count if sink is not None else 0,
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
            # "Não encontrado" quase nunca é definitivo: o publisher pode ainda
            # não ter commitado (ou há lag de réplica). Antes isto retornava
            # sucesso e a task morria em silêncio — sem retry, sem DLQ, e o job
            # ficava 'pending' para sempre. Retry com backoff dá tempo à
            # transação; só depois de esgotar é que viramos not_found.
            if self.request.retries < self.max_retries:
                delay = 2 ** self.request.retries  # 1s, 2s, 4s
                logger.warning(
                    "backfill job ainda não visível; reagendando",
                    extra={
                        "event": "backfill.job_not_visible_retry",
                        "job_id": job_id,
                        "retry": self.request.retries + 1,
                        "delay_s": delay,
                    },
                )
                raise self.retry(countdown=delay)

            logger.error(
                "backfill job não encontrado após %s tentativas",
                self.max_retries,
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
                    job_id=job_id,
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
