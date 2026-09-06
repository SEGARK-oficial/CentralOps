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
            shape = _resolve_finding_shape(query_def)
            severity_id = _resolve_query_severity(query_def)
            rows_cap = int(settings.QUERY_FINDING_MAX_ROWS_PER_RUN)
            base_key = f"sched:{sched.id}:integ:{integration.id}"
            # fingerprint da linha → activity_id do evento 2004 dela (1 Create,
            # 2 Update). Vem do BANCO: só a Detection sabe se a linha é nova.
            row_activities: dict[str, int] = {}
            # Detection de 1ª classe é a FONTE DA VERDADE
            # (durável, org-scoped, severidade da query, dedup por CONTEÚDO) —
            # substitui o alerta best-effort como registro. org_id ausente NÃO gera
            # Detection (fail-closed: nunca um alerta sem tenant).
            if integration.organization_id is not None:
                try:
                    row_activities = _record_query_detections(
                        db=db,
                        integration=integration,
                        sched=sched,
                        query_def=query_def,
                        items=items,
                        record=record,
                        dialect=qc.dialect,
                        shape=shape,
                        severity_id=severity_id,
                        rows_cap=rows_cap,
                        base_key=base_key,
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
                    severity_id=severity_id,
                    finding_shape=shape,
                    row_activities=row_activities,
                    rows_cap=rows_cap,
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


FINDING_SHAPES: frozenset[str] = frozenset({"summary", "per_row", "both"})
DEFAULT_FINDING_SHAPE = "both"


def _resolve_finding_shape(query_def: object) -> str:
    """``PredefinedQuery.finding_shape`` com fallback: valor fora do enum (linha
    legada, NULL, typo de migração manual) cai em ``both`` em vez de calar."""
    value = getattr(query_def, "finding_shape", None)
    return value if value in FINDING_SHAPES else DEFAULT_FINDING_SHAPE


def _resolve_query_severity(query_def: object) -> int:
    """Severidade ÚNICA da query: Detection, 1006 e 2004 saem com este valor.

    Antes a Detection gravava ``QUERY_DETECTION_DEFAULT_SEVERITY_ID`` (4) e os
    eventos saíam com Critical (5) fixo — a mesma execução dizia duas
    severidades, e toda hunt virava level 12 no Wazuh. A coluna
    ``PredefinedQuery.severity_id`` é validada na escrita; aqui só se defende
    de linha legada/NULL e de valor fora do enum, que derrubaria o evento no
    gate estrutural.
    """
    from .normalize.ocsf.classes import SEVERITY_ID, is_valid_severity_id

    for candidate in (
        getattr(query_def, "severity_id", None),
        settings.QUERY_DETECTION_DEFAULT_SEVERITY_ID,
    ):
        if isinstance(candidate, int) and not isinstance(candidate, bool) and is_valid_severity_id(candidate):
            return candidate
    return SEVERITY_ID["high"]


def _suppression_seconds(sched: object) -> int:
    """Janela de supressão da Detection de scheduled query.

    O default global (3600 s) é MENOR que a deriva real entre execuções de um
    schedule horário (60 a 61 min, medido em produção): a linha repetida caía
    fora da janela por segundos e virava Detection nova a cada run. A janela
    passa a ser ao menos DUAS cadências, então a mesma linha em runs seguidos
    bumpa ``count`` e o run pulado ainda fecha o alerta.
    """
    base = int(settings.QUERY_DETECTION_SUPPRESSION_SECONDS)
    try:
        value = getattr(sched, "interval_value", None) or getattr(sched, "interval_minutes", None) or 0
        unit = getattr(sched, "interval_unit", None) or "minutes"
        interval = _convert_to_timedelta(int(value), str(unit)).total_seconds() if value else 0
    except Exception:  # noqa: BLE001 — schedule malformado não pode derrubar o run
        interval = 0
    return max(base, int(2 * interval))


def _summary_event_id(sched: models.ScheduledQuery, record: Optional[models.SearchResult]) -> str:
    return f"sched-{sched.id}-{record.id}-finding" if record is not None else f"sched-{sched.id}-finding"


def _row_event_id(
    sched: models.ScheduledQuery, record: Optional[models.SearchResult], fingerprint: str
) -> str:
    base = f"sched-{sched.id}-{record.id}" if record is not None else f"sched-{sched.id}"
    return f"{base}-row-{fingerprint}"


def _record_query_detections(
    *,
    db: Session,
    integration: models.Integration,
    sched: models.ScheduledQuery,
    query_def: models.PredefinedQuery,
    items: list,
    record: Optional[models.SearchResult],
    dialect: Optional[str],
    shape: str,
    severity_id: int,
    rows_cap: int,
    base_key: str,
) -> dict[str, int]:
    """Grava as Detections do run e devolve ``fingerprint → activity_id``.

    ``summary``: UMA Detection por run, chave ``sched:{s}:integ:{i}`` (o
    contrato antigo), ``ocsf_ref`` = id do evento-resumo.

    ``per_row``/``both``: uma Detection POR LINHA, chave
    ``sched:{s}:integ:{i}:{fingerprint}``. A linha vista de novo dentro da
    janela BUMPA a existente (``count`` > 1 → o evento dela sai como Update);
    a linha nova nasce com ``count`` 1 (→ Create). ``ocsf_ref`` = id do evento
    2004 daquela linha, para ir do banco ao alerta e do alerta ao banco.

    Acima de ``rows_cap`` não há Detection nem evento — o excedente é declarado
    no evento-resumo e em cada evento por linha (``rows_over_cap``).
    """
    from .normalize.ocsf.query_rows import evidence_fingerprint, row_to_evidence

    repo = repository.DetectionRepository(db)
    common = dict(
        organization_id=integration.organization_id,
        source="scheduled_query",
        severity_id=severity_id,
        source_query_id=getattr(query_def, "id", None),
        integration_id=integration.id,
        dialect=dialect,
        rule_name=query_def.title,
        search_result_id=record.id if record is not None else None,
        suppression_window_seconds=_suppression_seconds(sched),
    )
    if shape == "summary":
        repo.record(dedup_key=base_key, ocsf_ref=_summary_event_id(sched, record), **common)
        return {}

    activities: dict[str, int] = {}
    for row in items[:rows_cap]:
        evidence, _aggregates = row_to_evidence(row)
        fingerprint = evidence_fingerprint(evidence, row)
        if fingerprint in activities:
            # Duas linhas do MESMO run com a mesma identidade (a hunt agrupou
            # por cmdline, por exemplo): uma Detection, um evento.
            continue
        det = repo.record(
            dedup_key=f"{base_key}:{fingerprint}",
            ocsf_ref=_row_event_id(sched, record, fingerprint),
            **common,
        )
        activities[fingerprint] = 2 if int(getattr(det, "count", 1) or 1) > 1 else 1
    if len(items) > rows_cap:
        logger.warning(
            "scheduler: sched=%d integ=%d devolveu %d linhas; só as %d primeiras "
            "viram Detection/evento próprio (QUERY_FINDING_MAX_ROWS_PER_RUN). O "
            "excedente está declarado no evento-resumo.",
            sched.id, integration.id, len(items), rows_cap,
        )
    return activities


def _dispatch_scheduled_query_alert(
    *,
    integration: models.Integration,
    sched: models.ScheduledQuery,
    query_def: models.PredefinedQuery,
    items: list,
    from_ts: str,
    to_ts: str,
    record: Optional[models.SearchResult],
    severity_id: Optional[int] = None,
    finding_shape: Optional[str] = None,
    row_activities: Optional[dict[str, int]] = None,
    rows_cap: Optional[int] = None,
) -> list[dict]:
    """Despacha os eventos de uma scheduled query que retornou resultados.

    Saem, pela mesma ``_enqueue_dispatch`` que todo produtor usa:

    1. **Scheduled Job Activity (1006)** — "o job rodou e devolveu N linhas".
       As linhas vão em ``raw.items`` (cortado por BYTES), e o ``raw`` é a
       primeira coisa que some no caminho (``drop_raw``, ``payload="ocsf"``).
    2. **Detection Finding (2004) resumo** — a tabela inteira mapeada em OCSF
       dentro de ``normalized.evidences[]``, onde nada a alcança. Sai quando
       ``finding_shape`` é ``summary`` ou ``both``.
    3. **Detection Finding (2004) por linha** — um evento por linha, com
       ``device``/``actor``/``process`` no nível da classe. É o que um destino
       que achata JSON (Wazuh) consegue indexar: o ``evidences[]`` do resumo
       chega lá como UMA string. Sai quando ``finding_shape`` é ``per_row`` ou
       ``both``, até ``rows_cap`` linhas.

    Os três carregam a MESMA ``severity_id`` que a Detection gravou. Devolve o
    lote despachado (para teste e para quem precisar dos ids).
    """
    from .normalize.envelope import EnvelopeContext, build_envelope
    from .normalize.ocsf.classes import CLASS_UID_SCHEDULED_JOB_ACTIVITY
    from .normalize.ocsf.query_rows import cap_rows_by_bytes
    from .pipeline import _enqueue_dispatch

    severity = severity_id if severity_id is not None else _resolve_query_severity(query_def)
    shape = finding_shape if finding_shape in FINDING_SHAPES else _resolve_finding_shape(query_def)
    cap = int(rows_cap) if rows_cap is not None else int(settings.QUERY_FINDING_MAX_ROWS_PER_RUN)

    # customer_id do envelope = Organization.id interno (não mais IRIS).
    org = integration.organization
    customer_id = getattr(org, "id", None) if org is not None else None
    customer_name = getattr(org, "name", None) if org is not None else None
    # Slug: rótulo estável do tenant no destino. Ver
    # ``EnvelopeContext.organization_slug``.
    organization_slug = getattr(org, "slug", None) if org is not None else None

    ctx = EnvelopeContext(
        vendor="centralops",
        integration_id=integration.id,
        customer_id=customer_id,
        customer_name=customer_name,
        organization_slug=organization_slug,
        stream="scheduled_query",
        event_type="centralops.scheduled_query.match",
        mapping_version_id=None,
        platform=integration.platform,
        # Sem isto o evento sai com organization_id=None e o roteador casa
        # SOMENTE rotas globais: uma rota criada pelo próprio tenant nunca
        # recebia o resultado da scheduled query dele.
        organization_id=getattr(integration, "organization_id", None),
        data_geography=getattr(integration, "data_geography", None),
    )

    agora_ms = int(datetime.utcnow().timestamp() * 1000)
    activity_id = 6  # Start — o OCSF 1.8 não tem "Run" nesta classe.
    base_key = f"sched:{sched.id}:integ:{integration.id}"

    unmapped: dict = {
        "schedule_id": sched.id,
        "query_id": query_def.id,
        "query_title": query_def.title,
        "items_count": len(items),
        "from": from_ts,
        "to": to_ts,
        "search_result_id": record.id if record is not None else None,
        "integration_id": integration.id,
        "integration_name": integration.name,
        "platform": integration.platform,
        "organization_id": getattr(integration, "organization_id", None),
        "dialect": getattr(record, "language", None),
        "ocsf_mapping_version": getattr(record, "ocsf_mapping_version", None),
        "table": query_def.table,
        "finding_shape": shape,
        # Chave da FAMÍLIA de Detections deste (schedule, integração); as
        # Detections por linha acrescentam o fingerprint a ela.
        "dedup_key": base_key,
    }
    # O statement inteiro (4 KiB numa hunt real, com a lista de IOCs) só sai
    # quando o operador pediu: ``query_id`` e ``search_result_id`` já levam ao
    # SQL, e repeti-lo a cada run era volume no fio para zero informação nova.
    if settings.QUERY_EVENT_INCLUDE_STATEMENT:
        unmapped["statement"] = query_def.statement

    normalized = {
        # ── identidade OCSF ──────────────────────────────────────────────
        "class_uid": CLASS_UID_SCHEDULED_JOB_ACTIVITY,
        "category_uid": 1,
        "activity_id": activity_id,
        # type_uid = class_uid * 100 + activity_id, como manda a spec.
        "type_uid": CLASS_UID_SCHEDULED_JOB_ACTIVITY * 100 + activity_id,
        # Milissegundos. Segundos aqui seria erro de 1000x, que este repo já
        # pagou uma vez em 16 mappings.
        "time": agora_ms,
        "status_id": 1,  # Success: a consulta rodou e devolveu resultado.
        "severity_id": severity,
        "metadata": {
            "version": "1.8.0",
            "product": {"name": "CentralOps", "vendor_name": "CentralOps"},
            "logged_time": agora_ms,
        },
        # ── obrigatórios da classe ───────────────────────────────────────
        "device": {
            "hostname": ctx.collector_host,
            "type_id": 0,  # Unknown: é o coletor, não um ativo do cliente.
        },
        "job": {
            "name": query_def.title,
            "desc": getattr(query_def, "description", None) or query_def.title,
        },
        "message": (
            f"Scheduled query '{query_def.title}' encontrou {len(items)} "
            f"resultado(s) para {integration.name}"
        ),
        # Sob ``unmapped`` porque não são campos da classe 1006: é onde o OCSF
        # manda pôr o que é específico do produto.
        "unmapped": unmapped,
    }

    # ``raw.items`` cortado por BYTES, não por contagem: 50 linhas com cmdline
    # longa passam do OS_MAXSTR do Wazuh e o JSON chega cortado no meio.
    raw_items, raw_truncated = cap_rows_by_bytes(items, int(settings.QUERY_RAW_ITEMS_MAX_BYTES))
    raw: dict = {
        "items": raw_items,
        "items_truncated": raw_truncated,
        "items_included": len(raw_items),
        "items_total": len(items),
    }

    vendor_msg_id = (
        f"sched-{sched.id}-{record.id}" if record is not None else f"sched-{sched.id}"
    )
    batch: list[dict] = [build_envelope(raw, normalized, ctx, vendor_msg_id=vendor_msg_id)]

    if shape in ("summary", "both"):
        batch.append(
            _build_scheduled_query_finding(
                integration=integration,
                sched=sched,
                query_def=query_def,
                items=items,
                ctx=ctx,
                occurred_ms=agora_ms,
                base_unmapped=unmapped,
                record=record,
                severity_id=severity,
                rows_cap=cap,
            )
        )
    if shape in ("per_row", "both"):
        batch.extend(
            _build_scheduled_query_row_findings(
                sched=sched,
                query_def=query_def,
                items=items,
                ctx=ctx,
                occurred_ms=agora_ms,
                base_unmapped=unmapped,
                record=record,
                severity_id=severity,
                rows_cap=cap,
                row_activities=row_activities or {},
            )
        )

    # Funnel through the shared helper so ALL producers inherit routing. With
    # no matching routes the batch follows the configured vendor-neutral
    # fallback (``Destination.is_default``) or, absent one, lands in the DLQ as
    # ``unrouted``. O envelope carrega ``organization_id`` da integração, então
    # rota criada pelo tenant casa o evento dele.
    _enqueue_dispatch(batch)
    return batch


def _build_scheduled_query_row_findings(
    *,
    sched: models.ScheduledQuery,
    query_def: models.PredefinedQuery,
    items: list,
    ctx,
    occurred_ms: int,
    base_unmapped: dict,
    record: Optional[models.SearchResult],
    severity_id: int,
    rows_cap: int,
    row_activities: dict[str, int],
) -> list[dict]:
    """Um envelope 2004 por linha (até ``rows_cap``), sem repetir a identidade
    dentro do mesmo run: duas linhas com o mesmo fingerprint viram um evento."""
    from dataclasses import replace

    from .normalize.envelope import build_envelope
    from .normalize.ocsf.query_rows import (
        build_row_finding_normalized,
        evidence_fingerprint,
        row_to_evidence,
    )

    base_key = str(base_unmapped["dedup_key"])
    # ``finding_info.uid`` da linha = a ``dedup_key`` da Detection dela, SEM o
    # id do run: é a identidade que atravessa execuções (o Update do run
    # seguinte e o Close da triagem apontam para o mesmo achado). O run fica
    # em ``unmapped.search_result_id``.
    finding_uid_base = base_key
    row_ctx = replace(ctx, event_type="centralops.scheduled_query.finding")
    inherited = {k: v for k, v in base_unmapped.items() if k != "statement"}

    rows = items[:rows_cap]
    envelopes: list[dict] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        # A identidade decide o ``activity_id`` (Create/Update) ANTES de montar
        # o evento; o builder recalcula o mesmo digest (é puro) e o devolve.
        evidence, _aggregates = row_to_evidence(row)
        fingerprint = evidence_fingerprint(evidence, row)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        normalized, fingerprint = build_row_finding_normalized(
            row=row,
            finding_uid_base=finding_uid_base,
            title=query_def.title,
            description=getattr(query_def, "description", None),
            severity_id=severity_id,
            query_id=getattr(query_def, "id", None),
            occurred_ms=occurred_ms,
            row_index=index,
            rows_total=len(items),
            rows_emitted=len(rows),
            activity_id=row_activities.get(fingerprint, 1),
            unmapped=inherited,
        )
        normalized["unmapped"]["dedup_key"] = f"{base_key}:{fingerprint}"
        envelopes.append(
            build_envelope(
                {}, normalized, row_ctx, vendor_msg_id=_row_event_id(sched, record, fingerprint)
            )
        )
    return envelopes


def _build_scheduled_query_finding(
    *,
    integration: models.Integration,
    sched: models.ScheduledQuery,
    query_def: models.PredefinedQuery,
    items: list,
    ctx,
    occurred_ms: int,
    base_unmapped: dict,
    record: Optional[models.SearchResult],
    severity_id: Optional[int] = None,
    rows_cap: Optional[int] = None,
) -> dict:
    """Envelope do ``Detection Finding`` (2004) com as linhas do resultado.

    Reusa o ``EnvelopeContext`` do 1006 — mesmo tenant, mesma integração, mesmo
    stream — trocando apenas o ``event_type``, para que uma rota já existente
    por ``stream``/``organization_id`` continue casando os dois eventos e quem
    quiser separá-los tenha por onde.
    """
    from dataclasses import replace

    from .normalize.envelope import build_envelope
    from .normalize.ocsf.query_rows import build_finding_normalized

    dedup_key = base_unmapped.get("dedup_key")
    # ``uid`` do achado: a chave de dedup da Detection durável mais o id do run.
    # Duas execuções do mesmo schedule são achados distintos e precisam de uids
    # distintos, senão o consumidor colapsa os dois no mesmo registro.
    finding_uid = (
        f"{dedup_key}:{record.id}" if record is not None and dedup_key else str(dedup_key)
    )

    # O statement sai do ``unmapped`` do achado. Ele já viaja inteiro no 1006 e
    # no ``SearchResult``, e uma hunt real passa de 4 KiB — repeti-lo aqui é o
    # dobro do texto no fio para zero informação nova, comendo a margem que
    # existe para as evidências dentro do OS_MAXSTR do Wazuh. ``query_id`` e
    # ``search_result_id`` continuam no evento: quem precisar do SQL chega nele.
    finding_unmapped = {k: v for k, v in base_unmapped.items() if k != "statement"}

    if rows_cap is not None:
        # O resumo declara o que os eventos por linha NÃO cobriram.
        finding_unmapped["rows_total"] = len(items)
        finding_unmapped["rows_emitted"] = min(len(items), int(rows_cap))
        finding_unmapped["rows_over_cap"] = max(0, len(items) - int(rows_cap))

    normalized = build_finding_normalized(
        rows=items,
        finding_uid=finding_uid,
        title=query_def.title,
        description=getattr(query_def, "description", None),
        severity_id=(
            severity_id if severity_id is not None else _resolve_query_severity(query_def)
        ),
        query_id=getattr(query_def, "id", None),
        occurred_ms=occurred_ms,
        unmapped=finding_unmapped,
    )

    finding_ctx = replace(ctx, event_type="centralops.scheduled_query.finding")
    # ``vendor_msg_id`` distinto do 1006: os dois saem da mesma execução, e um
    # id compartilhado faria o dedup do destino descartar o segundo como
    # repetição do primeiro — justamente o que carrega a tabela.
    vendor_msg_id = (
        f"sched-{sched.id}-{record.id}-finding"
        if record is not None
        else f"sched-{sched.id}-finding"
    )
    # ``raw`` vazio de propósito: as linhas já estão mapeadas no ``normalized``,
    # e repetir os mesmos itens no bruto dobraria o evento no fio sem acrescentar
    # informação. O bruto continua saindo no 1006, para quem o consome.
    return build_envelope({}, normalized, finding_ctx, vendor_msg_id=vendor_msg_id)


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
