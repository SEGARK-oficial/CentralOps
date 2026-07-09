"""REST endpoints para gestão de backfill de janela histórica (RF2.4).

Endpoints:

- ``POST /api/integrations/{integration_id}/backfill``
      Cria job de backfill para uma janela histórica e streams informados.
      Despacha task Celery na fila ``collect.backfill``.

- ``GET /api/integrations/{integration_id}/backfill-jobs``
      Lista jobs de backfill paginados, com filtro opcional por status.

- ``GET /api/backfill-jobs/{job_id}``
      Detalhe de um job específico (verifica multi-tenant).

- ``POST /api/backfill-jobs/{job_id}/cancel``
      Cancela job pendente ou em execução. Tenta revogar task Celery;
      o worker checa o status a cada iteração e sai limpo.

Restrições:
- Janela máxima: 90 dias.
- ``from_ts`` não pode ser mais de 90 dias no passado do momento do request.
- ``streams`` deve conter ao menos um stream registrado para o vendor
  da integration.
- Multi-tenant: non-admin só acessa integrations da própria organização.
- Auditoria: cada criação/cancelamento grava em ``MappingAuditLog``
  com ações ``backfill_requested`` / ``backfill_cancelled``.
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import datetime
from typing import Any, List, Optional

from celery.result import AsyncResult
from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..core import auth as app_auth
from ..core import tenant
from ..core.errors import ApiError
from ..db import database, models

logger = logging.getLogger(__name__)

router = APIRouter(tags=["backfill"])

# Janela máxima em dias (configurável futuramente via CollectorConfig).
_MAX_WINDOW_DAYS = 90

# Observabilidade (RF2.4): um job que fica "pending" além deste limiar quase
# sempre significa que NENHUM worker está consumindo a fila collect.backfill
# (sintoma "backfill nunca é realizado"). Surfaçado por job (stalled) e no
# endpoint de diagnóstico abaixo.
_STALL_THRESHOLD_SECONDS = 120
# Um job "running" por muito mais que o task_time_limit (15min) pode estar
# travado ou ter perdido o worker (acks_late re-enfileira).
_RUNNING_STALL_SECONDS = 30 * 60
# Backlog de pending acima disto = saudável mas saturado (escalar o worker).
_PENDING_BACKLOG_WATERMARK = 500
# Inspeção de workers: cache curto p/ não re-broadcastar a cada GET, e um
# timeout DURO num thread dedicado p/ um broker black-holed não travar a
# request pelo connect-timeout do SO (~127s) — a janela de replies (2s) NÃO
# cobre o connect.
_INSPECT_TTL_SECONDS = 10.0
_INSPECT_HARD_TIMEOUT = 6.0
_inspect_cache: dict[str, Any] = {"ts": 0.0, "value": None}
_inspect_executor = ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="backfill-inspect"
)


# ── Schemas ────────────────────────────────────────────────────────────


class CreateBackfillJobRequest(BaseModel):
    streams: List[str] = Field(..., min_length=1)  # ex: ["alerts", "cases"]
    from_ts: datetime
    to_ts: datetime

    @model_validator(mode="after")
    def validate_window(self) -> "CreateBackfillJobRequest":
        """Valida janela: from < to, janela máxima 90 dias."""
        if self.to_ts <= self.from_ts:
            raise ValueError("to_ts deve ser maior que from_ts")
        if (self.to_ts - self.from_ts).days > _MAX_WINDOW_DAYS:
            raise ValueError(f"Janela máxima de backfill é {_MAX_WINDOW_DAYS} dias")
        return self


class BackfillJobRead(BaseModel):
    id: str
    integration_id: int
    streams: List[str]
    from_ts: datetime
    to_ts: datetime
    status: str
    events_collected: int
    events_dispatched: int
    progress_pct: int
    requested_by_user_id: Optional[int] = None
    requested_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    last_error: Optional[str] = None
    cancelled_at: Optional[datetime] = None
    celery_task_id: Optional[str] = None
    # Observabilidade derivada (não persistida): True quando o job está parado
    # além do esperado (pending sem worker, ou running por tempo demais).
    # ATENÇÃO: populado SOMENTE via _serialize_job() — um model_validate(orm)
    # direto deixaria ambos no default (False/None), pulando a heurística.
    stalled: bool = False
    stall_reason: Optional[str] = None

    model_config = {"from_attributes": True}


class BackfillJobsListResponse(BaseModel):
    total: int
    items: List[BackfillJobRead]
    limit: int
    offset: int


class BackfillDiagnostics(BaseModel):
    """Diagnóstico operacional do subsistema de backfill (admin global).

    Responde diretamente à pergunta "por que o backfill nunca roda?": inspeciona
    os workers Celery vivos e quais filas consomem, e cruza com os jobs pending.
    """

    broker_reachable: bool
    workers_online: List[str]
    backfill_queue_consumers: List[str]
    pending_jobs: int
    running_jobs: int
    oldest_pending_age_seconds: Optional[int] = None
    healthy: bool
    diagnosis: str


# ── Helpers ────────────────────────────────────────────────────────────


def _compute_stall(job: models.BackfillJob, now: datetime) -> tuple[bool, Optional[str]]:
    """Heurística de "job parado" para observabilidade (pull-based, sem beat).

    pending além do limiar → quase sempre não há consumidor da fila
    collect.backfill; running por tempo demais → possível trava/worker perdido.
    """
    if job.status == "pending" and job.requested_at is not None:
        age = (now - job.requested_at).total_seconds()
        if age > _STALL_THRESHOLD_SECONDS:
            return True, (
                f"Job 'pending' há {int(age)}s sem iniciar. Causa provável: nenhum "
                "worker consome a fila 'collect.backfill'. Cheque "
                "GET /api/backfill-jobs/diagnostics e o serviço worker-bulk "
                "(deve incluir -Q ...,collect.backfill)."
            )
    if job.status == "running" and job.started_at is not None:
        running_for = (now - job.started_at).total_seconds()
        if running_for > _RUNNING_STALL_SECONDS:
            return True, (
                f"Job 'running' há {int(running_for)}s. Pode estar travado ou o worker "
                "foi perdido (acks_late re-enfileira). Verifique os logs do worker."
            )
    return False, None


def _serialize_job(
    job: models.BackfillJob, *, now: Optional[datetime] = None
) -> BackfillJobRead:
    """Converte modelo ORM em schema de leitura (+ flag de stall derivada)."""
    try:
        streams = json.loads(job.streams)
    except (TypeError, ValueError):
        streams = []
    stalled, stall_reason = _compute_stall(job, now or datetime.utcnow())
    return BackfillJobRead(
        id=job.id,
        integration_id=job.integration_id,
        streams=streams,
        from_ts=job.from_ts,
        to_ts=job.to_ts,
        status=job.status,
        events_collected=job.events_collected,
        events_dispatched=job.events_dispatched,
        progress_pct=job.progress_pct,
        requested_by_user_id=job.requested_by_user_id,
        requested_at=job.requested_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        last_error=job.last_error,
        cancelled_at=job.cancelled_at,
        celery_task_id=job.celery_task_id,
        stalled=stalled,
        stall_reason=stall_reason,
    )


def _do_inspect(timeout: float) -> tuple[List[str], List[str]]:
    """Uma ÚNICA chamada broadcast (active_queues): descobre online + consumidores.

    Um worker sempre consome ≥1 fila, então responder a ``active_queues`` já
    prova que está vivo — dispensa um ``ping()`` separado, que dobraria a janela
    de espera por replies.
    """
    from ..collectors.celery_app import celery_app

    inspector = celery_app.control.inspect(timeout=timeout)
    active_queues = inspector.active_queues() or {}
    workers_online = sorted(active_queues.keys())
    consumers = sorted(
        worker
        for worker, queues in active_queues.items()
        if any((q or {}).get("name") == "collect.backfill" for q in (queues or []))
    )
    return workers_online, consumers


def _inspect_backfill_workers(timeout: float = 2.0) -> tuple[List[str], List[str]]:
    """Descobre os workers vivos e quais consomem ``collect.backfill``.

    Resultado cacheado por ``_INSPECT_TTL_SECONDS`` p/ não re-broadcastar a cada
    GET (cada broadcast espera a janela de replies). A chamada roda num thread
    dedicado com timeout DURO: o broadcast espera ``timeout`` por replies, mas um
    broker black-holed pode travar o connect no TCP do SO (~127s) — o
    hard-timeout protege a thread da request.

    Levanta (Timeout/erro) se o broker estiver inacessível — o chamador trata
    como ``broker_reachable=False``. NB: um ``ImportError`` de ``celery_app``
    mal-configurado também sobe aqui e é absorvido como broker_reachable=False.
    """
    now = time.monotonic()
    cached = _inspect_cache.get("value")
    if cached is not None and (now - _inspect_cache["ts"]) < _INSPECT_TTL_SECONDS:
        return cached  # type: ignore[return-value]

    future = _inspect_executor.submit(_do_inspect, timeout)
    try:
        result = future.result(timeout=_INSPECT_HARD_TIMEOUT)
    except FuturesTimeout as exc:
        # Não bloqueia a request além do hard-timeout; a thread órfã (no máx. 1,
        # pois o executor é single-worker) morre quando o connect do SO desistir.
        raise TimeoutError(
            "inspeção de workers Celery excedeu o tempo limite"
        ) from exc

    _inspect_cache["value"] = result
    _inspect_cache["ts"] = now
    return result


def _get_integration_or_404(
    db: Session,
    integration_id: int,
    user: models.AppUser,
) -> models.Integration:
    """Carrega integration e valida acesso multi-tenant."""
    integration = db.get(models.Integration, integration_id)
    if integration is None:
        raise ApiError(
            "backfill.integration_not_found",
            404,
            messages={
                "pt": "Integration não encontrada",
                "en": "Integration not found",
                "es": "Integration no encontrada",
            },
        )
    tenant.require_subtree_access(user, integration.organization_id)
    return integration


def _audit_backfill(
    db: Session,
    *,
    action: str,
    user: models.AppUser,
    integration_id: int,
    job_id: str,
    streams: List[str],
    from_ts: datetime,
    to_ts: datetime,
) -> None:
    """Grava entrada de auditoria no MappingAuditLog (ação de operação)."""
    detail_payload = json.dumps(
        {
            "job_id": job_id,
            "integration_id": integration_id,
            "streams": streams,
            "from_ts": from_ts.isoformat(),
            "to_ts": to_ts.isoformat(),
        },
        separators=(",", ":"),
    )
    db.add(
        models.MappingAuditLog(
            integration_id=integration_id,
            action=action,
            user_id=user.id,
            username=user.username,
            user_role=user.role,
            detail=detail_payload,
        )
    )


# ── Endpoints ──────────────────────────────────────────────────────────


@router.post(
    "/integrations/{integration_id}/backfill",
    response_model=BackfillJobRead,
    status_code=status.HTTP_201_CREATED,
)
def create_backfill_job(
    integration_id: int,
    payload: CreateBackfillJobRequest,
    db: Session = Depends(database.get_session),
    user: models.AppUser = Depends(
        app_auth.require_permission(app_auth.Permission.INTEGRATION_WRITE)
    ),
) -> BackfillJobRead:
    """Cria job de backfill para a integration indicada.

    Valida:
    - Acesso multi-tenant à integration.
    - ``from_ts`` não mais de 90 dias no passado.
    - Streams existem no registry para o vendor da integration.
    - Janela máxima de 90 dias (validado pelo schema).

    Despacha task Celery na fila ``collect.backfill`` de forma assíncrona.
    """
    integration = _get_integration_or_404(db, integration_id, user)

    # Backfill só faz sentido para integrações tenant-scoped.
    # Parents MSSP (capability discover:children) são agregadores sem streams
    # próprios — backfill deles geraria dados fantasma e vazamento de scope.
    # gateado por capability, sem ``if kind in``.
    # gate VALIDADO por constante — um typo no literal cru
    # virava fail-OPEN (parent backfilled → scope leak). integration_has_capability
    # valida a key (typo → ValueError) antes de checar.
    from ..collectors.registry import integration_has_capability
    from ..collectors.capabilities import CAP_DISCOVER_CHILDREN
    if integration_has_capability(integration, CAP_DISCOVER_CHILDREN):
        logger.info(
            "backfill: ignorando integration parent MSSP (discover:children) integration_id=%s",
            integration_id,
        )
        raise ApiError(
            "backfill.parent_not_supported",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            messages={
                "pt": (
                    "Backfill não suportado para integrations parent (MSSP). "
                    "Somente integrations kind='tenant' possuem streams coletáveis."
                ),
                "en": (
                    "Backfill is not supported for parent (MSSP) integrations. "
                    "Only kind='tenant' integrations have collectable streams."
                ),
                "es": (
                    "Backfill no soportado para integrations parent (MSSP). "
                    "Solo las integrations kind='tenant' tienen streams recolectables."
                ),
            },
        )

    # Valida que from_ts não está mais de 90 dias no passado.
    now = datetime.utcnow()
    # Remove tzinfo para comparar com datetime naive (padrão do projeto).
    from_ts_naive = payload.from_ts.replace(tzinfo=None)
    to_ts_naive = payload.to_ts.replace(tzinfo=None)
    if (now - from_ts_naive).days > _MAX_WINDOW_DAYS:
        raise ApiError(
            "backfill.from_ts_too_old",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            messages={
                "pt": "from_ts não pode ser mais de {n} dias no passado",
                "en": "from_ts cannot be more than {n} days in the past",
                "es": "from_ts no puede tener más de {n} días en el pasado",
            },
            params={"n": _MAX_WINDOW_DAYS},
        )

    # Valida que os streams informados existem no registry para o vendor.
    from ..collectors import registry as collector_registry

    supported = set(collector_registry.supported_streams(integration.platform))
    invalid_streams = [s for s in payload.streams if s not in supported]
    if invalid_streams:
        raise ApiError(
            "backfill.unsupported_streams",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            messages={
                "pt": "Streams não suportados para '{platform}': {invalid}. Disponíveis: {available}",
                "en": "Unsupported streams for '{platform}': {invalid}. Available: {available}",
                "es": "Streams no soportados para '{platform}': {invalid}. Disponibles: {available}",
            },
            params={
                "platform": integration.platform,
                "invalid": invalid_streams,
                "available": sorted(supported),
            },
        )

    # Cria o job com status="pending".
    job = models.BackfillJob(
        integration_id=integration_id,
        streams=json.dumps(payload.streams, separators=(",", ":")),
        from_ts=from_ts_naive,
        to_ts=to_ts_naive,
        status="pending",
        requested_by_user_id=user.id,
        requested_at=now,
    )
    db.add(job)
    db.flush()  # garante job.id

    # Despacha task Celery na fila dedicada de backfill.
    # Import tardio evita ciclo de importação app ↔ collectors.
    from ..collectors.backfill_tasks import collect_backfill_job

    result = collect_backfill_job.apply_async(
        kwargs={"job_id": job.id},
        queue="collect.backfill",
    )
    job.celery_task_id = result.id

    # Auditoria antes do commit.
    _audit_backfill(
        db,
        action="backfill_requested",
        user=user,
        integration_id=integration_id,
        job_id=job.id,
        streams=payload.streams,
        from_ts=from_ts_naive,
        to_ts=to_ts_naive,
    )

    db.commit()
    db.refresh(job)

    logger.info(
        "backfill: job criado job_id=%s integration_id=%s streams=%s celery_task=%s",
        job.id, integration_id, payload.streams, result.id,
    )
    return _serialize_job(job)


@router.get(
    "/integrations/{integration_id}/backfill-jobs",
    response_model=BackfillJobsListResponse,
)
def list_backfill_jobs(
    integration_id: int,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    status_filter: Optional[str] = Query(None, alias="status"),
    db: Session = Depends(database.get_session),
    user: models.AppUser = Depends(
        app_auth.require_permission(app_auth.Permission.INTEGRATION_READ)
    ),
) -> BackfillJobsListResponse:
    """Lista jobs de backfill da integration, paginados e filtráveis por status."""
    _get_integration_or_404(db, integration_id, user)

    q = db.query(models.BackfillJob).filter(
        models.BackfillJob.integration_id == integration_id
    )
    if status_filter:
        q = q.filter(models.BackfillJob.status == status_filter)

    total = q.count()
    rows = (
        q.order_by(models.BackfillJob.requested_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    return BackfillJobsListResponse(
        total=total,
        items=[_serialize_job(r) for r in rows],
        limit=limit,
        offset=offset,
    )


@router.get(
    "/backfill-jobs/diagnostics",
    response_model=BackfillDiagnostics,
)
def backfill_diagnostics(
    db: Session = Depends(database.get_session),
    user: models.AppUser = Depends(app_auth.require_admin_user),
) -> BackfillDiagnostics:
    """Diagnóstico do subsistema de backfill (admin global).

    Cruza o estado dos workers Celery (vivos + filas consumidas) com os jobs
    ``pending`` para apontar a causa de "backfill nunca roda" — tipicamente
    nenhum worker consumindo ``collect.backfill``.

    NB: declarado ANTES de ``/backfill-jobs/{job_id}`` de propósito — senão
    'diagnostics' seria capturado como ``job_id``.
    """
    now = datetime.utcnow()
    pending_jobs = (
        db.query(func.count(models.BackfillJob.id))
        .filter(models.BackfillJob.status == "pending")
        .scalar()
        or 0
    )
    running_jobs = (
        db.query(func.count(models.BackfillJob.id))
        .filter(models.BackfillJob.status == "running")
        .scalar()
        or 0
    )
    oldest_pending = (
        db.query(func.min(models.BackfillJob.requested_at))
        .filter(models.BackfillJob.status == "pending")
        .scalar()
    )
    oldest_age = (
        int((now - oldest_pending).total_seconds()) if oldest_pending else None
    )

    try:
        workers_online, consumers = _inspect_backfill_workers()
        broker_reachable = True
    except Exception as exc:  # noqa: BLE001 — diagnóstico nunca derruba a request
        logger.warning(
            "backfill diagnostics: falha ao inspecionar workers Celery: %r", exc
        )
        workers_online, consumers, broker_reachable = [], [], False

    if not broker_reachable:
        healthy = False
        diagnosis = (
            "Não foi possível inspecionar os workers (broker Celery inacessível ou "
            "nenhum worker respondeu no timeout). Verifique o Redis/broker e os "
            "processos worker."
        )
    elif not workers_online:
        healthy = False
        diagnosis = (
            "Nenhum worker Celery respondeu ao ping — os workers podem estar "
            "offline ou apontando para outro broker."
        )
    elif not consumers:
        healthy = False
        diagnosis = (
            f"{len(workers_online)} worker(s) online, mas NENHUM consome a fila "
            "'collect.backfill' — jobs ficam 'pending' indefinidamente. Configure o "
            "worker-bulk para incluir a fila (compose: -Q collect.bulk,collect.backfill; "
            "helm: workers-deployment args)."
        )
    elif oldest_age is not None and oldest_age > _STALL_THRESHOLD_SECONDS:
        healthy = False
        diagnosis = (
            f"{len(consumers)} consumidor(es) da fila ativo(s), mas {pending_jobs} "
            f"job(s) 'pending' (mais antigo há {oldest_age}s). O consumidor pode estar "
            "saturado ou errando — verifique os logs do worker-bulk."
        )
    elif pending_jobs > _PENDING_BACKLOG_WATERMARK:
        healthy = False
        diagnosis = (
            f"{len(consumers)} consumidor(es) ativo(s) e os jobs estão avançando, "
            f"porém o backlog está alto ({pending_jobs} 'pending'). Considere escalar "
            "o worker-bulk para drenar a fila mais rápido."
        )
    else:
        healthy = True
        diagnosis = (
            f"OK — {len(consumers)} worker(s) consumindo 'collect.backfill' "
            f"({pending_jobs} pending, {running_jobs} running)."
        )

    return BackfillDiagnostics(
        broker_reachable=broker_reachable,
        workers_online=workers_online,
        backfill_queue_consumers=consumers,
        pending_jobs=pending_jobs,
        running_jobs=running_jobs,
        oldest_pending_age_seconds=oldest_age,
        healthy=healthy,
        diagnosis=diagnosis,
    )


@router.get(
    "/backfill-jobs/{job_id}",
    response_model=BackfillJobRead,
)
def get_backfill_job(
    job_id: str,
    db: Session = Depends(database.get_session),
    user: models.AppUser = Depends(
        app_auth.require_permission(app_auth.Permission.INTEGRATION_READ)
    ),
) -> BackfillJobRead:
    """Retorna detalhe de um job de backfill, validando acesso multi-tenant."""
    job = db.get(models.BackfillJob, job_id)
    if job is None:
        raise ApiError(
            "backfill.job_not_found",
            404,
            messages={
                "pt": "BackfillJob não encontrado",
                "en": "BackfillJob not found",
                "es": "BackfillJob no encontrado",
            },
        )

    # Valida multi-tenant via integration.
    integration = db.get(models.Integration, job.integration_id)
    if integration is None:
        raise ApiError(
            "backfill.job_integration_not_found",
            404,
            messages={
                "pt": "Integration do job não encontrada",
                "en": "Job's integration not found",
                "es": "Integration del job no encontrada",
            },
        )
    tenant.require_subtree_access(user, integration.organization_id)

    return _serialize_job(job)


@router.post(
    "/backfill-jobs/{job_id}/cancel",
    response_model=BackfillJobRead,
)
def cancel_backfill_job(
    job_id: str,
    db: Session = Depends(database.get_session),
    user: models.AppUser = Depends(
        app_auth.require_permission(app_auth.Permission.INTEGRATION_WRITE)
    ),
) -> BackfillJobRead:
    """Cancela um job de backfill pendente ou em execução.

    - Jobs com status ``completed`` ou ``failed`` não podem ser cancelados (400).
    - Tenta revogar a task Celery via ``AsyncResult.revoke(terminate=False)``.
      O worker verifica o status a cada iteração de página e sai limpo se
      ``status == 'cancelled'``.
    - Grava auditoria com ação ``backfill_cancelled``.
    """
    job = db.get(models.BackfillJob, job_id)
    if job is None:
        raise ApiError(
            "backfill.job_not_found",
            404,
            messages={
                "pt": "BackfillJob não encontrado",
                "en": "BackfillJob not found",
                "es": "BackfillJob no encontrado",
            },
        )

    # Valida acesso multi-tenant.
    integration = db.get(models.Integration, job.integration_id)
    if integration is None:
        raise ApiError(
            "backfill.job_integration_not_found",
            404,
            messages={
                "pt": "Integration do job não encontrada",
                "en": "Job's integration not found",
                "es": "Integration del job no encontrada",
            },
        )
    tenant.require_subtree_access(user, integration.organization_id)

    # Não cancela jobs já terminados.
    if job.status in {"completed", "failed"}:
        raise ApiError(
            "backfill.job_not_cancellable",
            status.HTTP_400_BAD_REQUEST,
            messages={
                "pt": "Job com status '{status}' não pode ser cancelado",
                "en": "Job with status '{status}' cannot be cancelled",
                "es": "El job con estado '{status}' no se puede cancelar",
            },
            params={"status": job.status},
        )
    if job.status == "cancelled":
        raise ApiError(
            "backfill.job_already_cancelled",
            status.HTTP_400_BAD_REQUEST,
            messages={
                "pt": "Job já está cancelado",
                "en": "Job is already cancelled",
                "es": "El job ya está cancelado",
            },
        )

    now = datetime.utcnow()
    job.status = "cancelled"
    job.cancelled_at = now

    # Tenta revogar task Celery (não-bloqueante; falha é tolerada).
    if job.celery_task_id:
        try:
            from ..collectors.celery_app import celery_app

            AsyncResult(job.celery_task_id, app=celery_app).revoke(terminate=False)
            logger.info(
                "backfill: revoke enviado celery_task_id=%s job_id=%s",
                job.celery_task_id, job_id,
            )
        except Exception:
            # Revoke é best-effort: o worker vai checar o status no DB.
            logger.warning(
                "backfill: falha ao revogar task celery_task_id=%s job_id=%s",
                job.celery_task_id, job_id, exc_info=True,
            )

    # Auditoria.
    try:
        streams = json.loads(job.streams)
    except (TypeError, ValueError):
        streams = []

    _audit_backfill(
        db,
        action="backfill_cancelled",
        user=user,
        integration_id=job.integration_id,
        job_id=job.id,
        streams=streams,
        from_ts=job.from_ts,
        to_ts=job.to_ts,
    )

    db.commit()
    db.refresh(job)

    logger.info(
        "backfill: job cancelado job_id=%s por user=%s",
        job_id, user.username,
    )
    return _serialize_job(job)
