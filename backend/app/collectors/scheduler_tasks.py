"""Migração do ``services/scheduler.py`` (threading) para Celery Beat.

O scheduler legado era um ``threading.Thread`` daemon que rodava em loop
``while True: sleep(60)`` dentro do processo FastAPI. Problemas:

- **Acoplamento**: matar o API = perder o scheduler.
- **Escala vertical apenas**: uma única thread sequencial, não paraleliza
  schedules nem clientes.
- **Sem retry estruturado**: qualquer exceção poluía logs mas sem backoff.
- **Stateful** (thread.Thread vive no processo).

Esta migração preserva o **contrato externo** (linhas em ``ScheduledQuery``
controlam cadência e ``next_run``; ``SearchResult`` recebe o output) mas
move a execução para Celery:

- **``dispatch_due_scheduled_queries``** — tick a cada 60s (via Beat).
  Varre ``ScheduledQuery.next_run <= now`` e enfileira uma task Celery
  por schedule. Não bloqueia: só despacha.
- **``run_scheduled_query(sched_id)``** — executa a query do schedule.
  Cada worker pega uma task independentemente → paralelismo natural.
  Backoff/retry delegado ao Celery (``autoretry_for`` + jitter).
- **``prune_search_result_retention``** — substitui a chamada ao
  ``SearchResultRetentionService`` que antes vivia no loop. Agenda 1×/dia.

Ambas as tasks rodam na queue ``collect.bulk`` — não são tempo-real.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from typing import Optional
from uuid import uuid4

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.orm import Session

from ..core.config import settings
from ..db import database, models, repository
from ..services.emailer import send_email
from ..services.history import HistoryService
from ..services.search_results import SearchResultRetentionService
from .celery_app import celery_app
from .metrics import TASK_DURATION
from .queues import (
    Q_QUERY,
    T_SCHED_DISPATCH_DUE,
    T_SCHED_PRUNE_RESULTS,
    T_SCHED_RUN,
)
from .registry import get_provider, integration_query_capability

logger = logging.getLogger(__name__)


# ── Utilitários preservados da versão legada ─────────────────────────


def _convert_to_timedelta(value: int, unit: str) -> timedelta:
    return timedelta(**{unit: value})


def _resolve_lookback_timedelta(sched: models.ScheduledQuery) -> timedelta:
    lookback_value = getattr(sched, "lookback_value", None) or sched.days_back or 1
    lookback_unit = getattr(sched, "lookback_unit", None) or "days"
    return _convert_to_timedelta(lookback_value, lookback_unit)


def _next_run_after(sched: models.ScheduledQuery) -> datetime:
    return datetime.utcnow() + _convert_to_timedelta(
        sched.interval_value or sched.interval_minutes,
        sched.interval_unit or "minutes",
    )


# ── Tick: enfileira schedules vencidos ───────────────────────────────


@celery_app.task(
    name=T_SCHED_DISPATCH_DUE,
    bind=True,
    acks_late=True,
    time_limit=120,
    soft_time_limit=90,
)
def dispatch_due_scheduled_queries(self) -> dict:
    """Beat dispara a cada 60s. Varre o DB e enfileira tasks por schedule.

    Returns:
        dict com contadores para observabilidade via Flower.
    """
    dispatched = 0
    skipped = 0
    now = datetime.utcnow()

    with database.SessionLocal() as db:
        sched_repo = repository.ScheduledQueryRepository(db)
        for sched in sched_repo.list():
            if sched.next_run > now:
                skipped += 1
                continue
            # enfileira na fila DEDICADA de query (não mais
            # collect.bulk — fim do noisy-neighbor com a ingestão). A task marca
            # o next_run/saúde. (O TICK em si segue leve em collect.bulk.)
            run_scheduled_query.apply_async(
                kwargs={"sched_id": sched.id},
                queue=Q_QUERY,
            )
            dispatched += 1

    logger.info(
        "scheduler-tick: dispatched=%d skipped=%d total=%d",
        dispatched, skipped, dispatched + skipped,
    )
    return {"dispatched": dispatched, "skipped": skipped}


# ── Execução de um schedule ──────────────────────────────────────────


@celery_app.task(
    name=T_SCHED_RUN,
    bind=True,
    autoretry_for=(ConnectionError, TimeoutError, OSError),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=3,
    acks_late=True,
    time_limit=15 * 60,
    soft_time_limit=12 * 60,
)
def run_scheduled_query(self, sched_id: int) -> None:
    """Executa um ``ScheduledQuery`` — mesma semântica do ``_execute_schedule`` legado."""
    with TASK_DURATION.labels(stream="scheduled_query", queue=Q_QUERY).time():
        with database.SessionLocal() as db:
            sched_repo = repository.ScheduledQueryRepository(db)
            sched = sched_repo.get(sched_id)
            if sched is None:
                logger.warning("scheduler: schedule id=%s não encontrado", sched_id)
                return
            try:
                _execute_schedule(db, sched)
            except SoftTimeLimitExceeded:
                logger.error("scheduler: soft-timeout sched=%s", sched_id)
                raise
            except Exception as exc:
                # Erro inesperado fora do per-integração (que já é tratado em
                # _execute_schedule): avança next_run mas registra a falha na SAÚDE
                # (não "parece rodar").
                logger.exception(
                    "scheduler: falha executando sched=%s: %s", sched_id, exc
                )
                try:
                    sched_repo.update_run_outcome(
                        sched,
                        next_run=_next_run_after(sched),
                        last_run_at=datetime.utcnow(),
                        success=False,
                        last_error=str(exc),
                    )
                except Exception:
                    logger.exception("scheduler: falha ao registrar saúde do schedule")
                raise


def _execute_schedule(
    db: Session,
    sched: models.ScheduledQuery,
    actor_user_id: Optional[int] = None,
) -> None:
    """Lógica extraída de ``services/scheduler.py::_execute_schedule``.

    Preservada 1:1 (incluindo ``actor_user_id`` para que o endpoint
    ``POST /schedules`` possa executar imediatamente após criar um
    schedule e atribuir a autoria ao admin que disparou).
    """
    sched_repo = repository.ScheduledQueryRepository(db)
    query_repo = repository.PredefinedQueryRepository(db)
    integration_repo = repository.IntegrationRepository(db)
    email_repo = repository.EmailRepository(db)
    results_repo = repository.SearchResultRepository(db)
    history = HistoryService(db)

    q = query_repo.get(sched.query_id)
    if not q:
        logger.warning(
            "scheduler: query %d não encontrada p/ schedule %d",
            sched.query_id, sched.id,
        )
        sched_repo.update_run_outcome(
            sched, next_run=_next_run_after(sched), last_run_at=datetime.utcnow(),
            success=False, last_error=f"predefined query {sched.query_id} ausente",
        )
        return

    now = datetime.utcnow().replace(microsecond=0)

    # idempotência (acks_late): se um run terminal recente já
    # existe (re-entrega de task em voo), não duplica SearchResult+e-mail+alerta.
    interval_td = _convert_to_timedelta(
        sched.interval_value or sched.interval_minutes, sched.interval_unit or "minutes"
    )
    guard_since = now - (interval_td / 2)
    if results_repo.has_recent_terminal_run(sched.id, guard_since):
        logger.info(
            "scheduler: sched=%d já executado na última meia-cadência — skip idempotente",
            sched.id,
        )
        sched_repo.update_run_outcome(
            sched, next_run=_next_run_after(sched), last_run_at=now, success=True,
        )
        return

    from_ts = (now - _resolve_lookback_timedelta(sched)).strftime("%Y-%m-%dT%H:%M:%SZ")
    to_ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    # a lista de destinatários NÃO é resolvida aqui
    # (global). É resolvida POR INTEGRAÇÃO, escopada à org da integração, dentro
    # de _run_query_for_integration — senão o resultado do tenant X vaza para
    # e-mails de outros tenants.
    notify_on_results = bool(getattr(sched, "notify_on_results", False))

    client_ids = [int(x) for x in sched.client_ids.split(",") if x.strip()]
    # Fail-closed: backfilla o org_id do schedule a partir da 1ª integração (p/ o
    # alerta de scheduled query rotear por-tenant em vez de GLOBAL).
    _ensure_schedule_org(db, sched, integration_repo, client_ids)

    statuses: list[str] = []
    for cid in client_ids:
        statuses.append(
            _run_query_for_integration(
                db=db,
                integration_id=cid,
                sched=sched,
                query_def=q,
                from_ts=from_ts,
                to_ts=to_ts,
                email_repo=email_repo,
                notify_on_results=notify_on_results,
                actor_user_id=actor_user_id,
                integration_repo=integration_repo,
                results_repo=results_repo,
                history=history,
            )
        )
        # Pequena folga entre integrações — evita burst de rate limit.
        time.sleep(1)

    # ``next_run`` avança sempre (evita hot-loop), mas a SAÚDE
    # distingue sucesso de falha — um schedule morto fica VISÍVEL (status=failing)
    # em vez de "parecer rodar". Sucesso = ao menos uma fonte respondeu.
    answered = any(s == "answered" for s in statuses)
    if answered:
        sched_repo.update_run_outcome(
            sched, next_run=_next_run_after(sched), last_run_at=now, success=True,
        )
    else:
        failed = [s for s in statuses if s == "failed"]
        last_error = (
            f"{len(failed)}/{len(statuses)} fonte(s) falharam"
            if failed
            else "nenhuma integração-alvo com capability de query"
        )
        sched_repo.update_run_outcome(
            sched, next_run=_next_run_after(sched), last_run_at=now, success=False,
            last_error=last_error,
        )


def _ensure_schedule_org(
    db: Session,
    sched: models.ScheduledQuery,
    integration_repo: repository.IntegrationRepository,
    client_ids: list[int],
) -> None:
    """Backfilla ``ScheduledQuery.organization_id`` da 1ª integração-alvo (uma vez).

    Schedules legados não têm org_id; sem ele o alerta de scheduled query roteia
    GLOBAL (vaza tenant). O CRUD novo já seta no create; isto cobre os legados."""
    if getattr(sched, "organization_id", None) is not None or not client_ids:
        return
    first = integration_repo.get(client_ids[0])
    if first is not None and first.organization_id is not None:
        sched.organization_id = first.organization_id
        sched.updated_at = datetime.utcnow()
        db.commit()


def _run_query_for_integration(
    *,
    db: Session,
    integration_id: int,
    sched: models.ScheduledQuery,
    query_def: models.PredefinedQuery,
    from_ts: str,
    to_ts: str,
    email_repo: repository.EmailRepository,
    notify_on_results: bool,
    actor_user_id: Optional[int],
    integration_repo: repository.IntegrationRepository,
    results_repo: repository.SearchResultRepository,
    history: HistoryService,
) -> str:
    """Executa a query agendada numa fonte via o ponto canônico
    ``get_provider(integration).run_query()``. O provider resolve
    creds/region/tenant/token-sharing-parent internamente
    (``SophosProvider._credential_holder``) e levanta se mal-configurado. Devolve
    ``answered`` | ``failed`` | ``skipped`` para a saúde do schedule."""
    integration = integration_repo.get(integration_id)
    if integration is None:
        logger.warning("scheduler: integração %d não encontrada; pulando", integration_id)
        return "skipped"

    # Gate por capability (não por ``platform ==``): a fonte precisa declarar
    # ``query:<dialect>``. Sem isso (ex.: vendor só-coleta) → skip silencioso.
    qc = integration_query_capability(integration)
    if qc is None:
        logger.warning(
            "scheduler: integração %d (%s) sem capability de query; pulando",
            integration_id, integration.platform,
        )
        return "skipped"

    # destinatários ESCOPADOS à org desta integração.
    emails = (
        [e.email for e in email_repo.list_for_org(integration.organization_id)]
        if notify_on_results
        else []
    )

    search_id = uuid4().hex
    record: Optional[models.SearchResult] = None
    try:
        record = results_repo.add_run(
            integration.id,
            search_id,
            query_def.statement,
            query_def.table,
            from_ts,
            to_ts,
            "submitted",
            schedule_id=sched.id,
            user_id=actor_user_id,
            platform=integration.platform,
            engine="query",
            language=qc.dialect,
            ocsf_mapping_version=qc.ocsf_mapping_version,
            organization_id=integration.organization_id,
        )
        history.add_entry(
            integration.id,
            "schedule_query",
            f"provider://{integration.platform}/run_query",
            json.dumps({
                "statement": query_def.statement, "from": from_ts,
                "to": to_ts, "dialect": qc.dialect,
            }),
            "submitted",
            user_id=actor_user_id,
        )

        provider = get_provider(integration)
        result = provider.run_query(query_def.statement, from_ts, to_ts)
        items = list(getattr(result, "items", []) or [])

        results_repo.update_result(
            record,
            "finished",
            json.dumps(items, default=str),
            result_count=len(items),
            error_message=None,
        )

        if items and emails:
            send_email(
                emails,
                f"Resultado para {query_def.title}",
                f"Encontrados {len(items)} itens para a integração {integration.name}",
            )

        if items:
            # Detection de 1ª classe é a FONTE DA VERDADE
            # (durável, org-scoped, severidade configurável, dedup por janela) —
            # substitui o alerta best-effort como registro. org_id ausente NÃO gera
            # Detection (fail-closed: nunca um alerta sem tenant).
            if integration.organization_id is not None:
                try:
                    repository.DetectionRepository(db).record(
                        organization_id=integration.organization_id,
                        source="scheduled_query",
                        dedup_key=f"sched:{sched.id}:integ:{integration.id}",
                        severity_id=settings.QUERY_DETECTION_DEFAULT_SEVERITY_ID,
                        source_query_id=getattr(query_def, "id", None),
                        integration_id=integration.id,
                        dialect=qc.dialect,
                        rule_name=query_def.title,
                        search_result_id=record.id if record is not None else None,
                        suppression_window_seconds=settings.QUERY_DETECTION_SUPPRESSION_SECONDS,
                    )
                except Exception:
                    logger.exception(
                        "scheduler: falha ao registrar Detection sched=%d", sched.id
                    )
            # O syslog segue como ENTREGA (não-fonte) — best-effort.
            try:
                _dispatch_scheduled_query_alert(
                    integration=integration,
                    sched=sched,
                    query_def=query_def,
                    items=items,
                    from_ts=from_ts,
                    to_ts=to_ts,
                    record=record,
                )
            except Exception:
                # Alerta é best-effort — falha aqui não invalida o SearchResult.
                logger.exception(
                    "scheduler: falha ao despachar alerta sched=%d", sched.id
                )
        return "answered"

    except Exception as exc:
        logger.exception(
            "scheduler: falha integração=%s sched=%d: %s",
            getattr(integration, "name", integration_id), sched.id, exc,
        )
        if record is not None:
            try:
                results_repo.mark_failed(record, str(exc))
            except Exception:
                logger.exception("scheduler: falha ao persistir erro")
        return "failed"


def _dispatch_scheduled_query_alert(
    *,
    integration: models.Integration,
    sched: models.ScheduledQuery,
    query_def: models.PredefinedQuery,
    items: list,
    from_ts: str,
    to_ts: str,
    record: Optional[models.SearchResult],
) -> None:
    """Despacha alerta syslog (Critical) quando uma scheduled query retorna resultados."""
    from .normalize.envelope import EnvelopeContext, build_envelope
    from .normalize.ocsf import SEVERITY_ID
    from .pipeline import _enqueue_dispatch

    # customer_id do envelope = Organization.id interno (não mais IRIS).
    org = integration.organization
    customer_id = getattr(org, "id", None) if org is not None else None
    customer_name = getattr(org, "name", None) if org is not None else None

    ctx = EnvelopeContext(
        vendor="centralops",
        integration_id=integration.id,
        customer_id=customer_id,
        customer_name=customer_name,
        stream="scheduled_query",
        event_type="centralops.scheduled_query.match",
        mapping_version_id=None,
    )

    normalized = {
        "severity_id": SEVERITY_ID["critical"],  # 5 → syslog crit (PRI=130)
        "message": (
            f"Scheduled query '{query_def.title}' encontrou {len(items)} "
            f"resultado(s) para {integration.name}"
        ),
        "metadata": {
            "schedule_id": sched.id,
            "query_id": query_def.id,
            "query_title": query_def.title,
            "items_count": len(items),
            "from": from_ts,
            "to": to_ts,
            "search_result_id": record.id if record is not None else None,
        },
    }

    # Cap nos items pra não estourar payload do Wazuh.
    raw: dict = {"items": items[:50], "items_truncated": len(items) > 50}

    vendor_msg_id = (
        f"sched-{sched.id}-{record.id}" if record is not None else f"sched-{sched.id}"
    )
    envelope = build_envelope(raw, normalized, ctx, vendor_msg_id=vendor_msg_id)

    # Funnel through the shared helper so ALL producers inherit
    # routing. With no
    # matching routes the batch follows the configured vendor-neutral fallback
    # (``Destination.is_default``) or, absent one, lands in the DLQ as ``unrouted``
    # (no hardcoded wazuh-default). NOTE: this envelope carries
    # organization_id=None, so under routing it matches GLOBAL routes only
    # (tenant-scoped routing of scheduled-query results would need org_id set on
    # the EnvelopeContext — future).
    _enqueue_dispatch([envelope])


# ── Retention diária ─────────────────────────────────────────────────


@celery_app.task(
    name=T_SCHED_PRUNE_RESULTS,
    bind=True,
    acks_late=True,
    time_limit=600,
)
def prune_search_result_retention(self) -> int:
    """Poda ``SearchResult`` expirados (substitui a chamada no loop legado)."""
    with database.SessionLocal() as db:
        deleted = SearchResultRetentionService(db).prune_expired_entries()
    logger.info("scheduler-retention: deletados=%d", deleted or 0)
    return int(deleted or 0)
