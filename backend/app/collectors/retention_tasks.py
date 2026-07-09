"""Tasks Celery de purge de dados por retenção configurável.

Cada task é idempotente e segura para re-execução. Rodam diariamente
às 3am UTC via Beat schedule. A task ``prune_all`` é o wrapper que
despacha todas as limpezas em sequência.

Estratégia de deleção por organização:
- Busca config de retenção; se não existir, usa defaults.
- Calcula cutoff = now() - retention_days.
- DELETE em lote via SQLAlchemy (sem ORM overhead).
- Retorna contagem total de linhas removidas.

Garantias:
- DELETE em UnknownField por ``organization_id``: o drift
  é escopado por tenant, então o right-to-erasure apaga o inferido da org.
- AuditLog com user_id=NULL (eventos de sistema) jamais são tocados.
- Em caso de erro parcial, a task loga e propaga (Celery cuida do retry).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from celery import shared_task
from sqlalchemy import delete
from sqlalchemy.orm import Session

from ..db import database, models

logger = logging.getLogger(__name__)

# Defaults usados quando a org não tem config de retenção.
_DEFAULT_QUARANTINE_DAYS = 7
_DEFAULT_DRIFT_DAYS = 90
_DEFAULT_HISTORY_DAYS = 30
_DEFAULT_SEARCH_RESULT_DAYS = 7
_DEFAULT_AUDIT_LOG_DAYS = 365

# Diretório onde o audit master de deleção é gravado.
_DELETION_AUDIT_DIR = Path(
    os.environ.get("CENTRALOPS_DELETION_AUDIT_DIR", "/var/log/centralops/data-deletion")
)


def _get_retention_days(
    db: Session, organization_id: int, field: str, default: int
) -> int:
    """Retorna o valor de retenção configurado ou o default."""
    config = (
        db.query(models.OrganizationRetentionConfig)
        .filter(
            models.OrganizationRetentionConfig.organization_id == organization_id
        )
        .first()
    )
    if config is None:
        return default
    return int(getattr(config, field, default))


def _integration_ids_for_org(db: Session, organization_id: int) -> list[int]:
    """Lista IDs de todas as integrações de uma organização."""
    rows = (
        db.query(models.Integration.id)
        .filter(models.Integration.organization_id == organization_id)
        .all()
    )
    return [r.id for r in rows]


def _user_ids_for_org(db: Session, organization_id: int) -> list[int]:
    """Lista IDs de todos os usuários de uma organização."""
    rows = (
        db.query(models.AppUser.id)
        .filter(models.AppUser.organization_id == organization_id)
        .all()
    )
    return [r.id for r in rows]


@shared_task(bind=True, queue="maintenance")
def prune_expired_quarantine(self: Any) -> dict[str, int]:
    """Deleta QuarantineEvent expirados conforme retenção de cada org.

    Para cada organização:
    1. Lê config de retenção (default 7d se não houver).
    2. cutoff = now - retention_days.
    3. DELETE FROM quarantine_events WHERE created_at < cutoff
       AND integration_id IN (integrations da org).
    """
    totals: dict[str, int] = {}

    with database.SessionLocal() as db:
        orgs = db.query(models.Organization).filter(
            models.Organization.is_active.is_(True)
        ).all()

        for org in orgs:
            retention_days = _get_retention_days(
                db, org.id, "quarantine_retention_days", _DEFAULT_QUARANTINE_DAYS
            )
            cutoff = datetime.utcnow() - timedelta(days=retention_days)
            integration_ids = _integration_ids_for_org(db, org.id)

            if not integration_ids:
                continue

            result = db.execute(
                delete(models.QuarantineEvent).where(
                    models.QuarantineEvent.integration_id.in_(integration_ids),
                    models.QuarantineEvent.created_at < cutoff,
                )
            )
            deleted = result.rowcount
            if deleted:
                totals[str(org.id)] = deleted
                logger.info(
                    "purge quarantine concluído",
                    extra={
                        "event": "retention.quarantine_purge",
                        "org_id": org.id,
                        "deleted": deleted,
                        "cutoff": cutoff.isoformat(),
                    },
                )

        db.commit()

    return totals


@shared_task(bind=True, queue="maintenance")
def prune_expired_drift(self: Any) -> dict[str, int]:
    """Deleta UnknownField expirados conforme retenção de cada org.

    UnknownField AGORA tem ``organization_id`` — a purga é por
    tenant EXATO (não mais aproximação por vendor compartilhado). last_seen é
    o timestamp de referência.
    """
    totals: dict[str, int] = {}

    with database.SessionLocal() as db:
        orgs = db.query(models.Organization).filter(
            models.Organization.is_active.is_(True)
        ).all()

        for org in orgs:
            retention_days = _get_retention_days(
                db, org.id, "drift_retention_days", _DEFAULT_DRIFT_DAYS
            )
            cutoff = datetime.utcnow() - timedelta(days=retention_days)

            result = db.execute(
                delete(models.UnknownField).where(
                    models.UnknownField.organization_id == org.id,
                    models.UnknownField.last_seen < cutoff,
                )
            )
            deleted = result.rowcount
            if deleted:
                totals[str(org.id)] = deleted
                logger.info(
                    "purge drift concluído",
                    extra={
                        "event": "retention.drift_purge",
                        "org_id": org.id,
                        "deleted": deleted,
                    },
                )

        db.commit()

    return totals


@shared_task(bind=True, queue="maintenance")
def prune_expired_history(self: Any) -> dict[str, int]:
    """Deleta History expirada por organização.

    Filtra por integration_id IN (integrações da org).
    """
    totals: dict[str, int] = {}

    with database.SessionLocal() as db:
        orgs = db.query(models.Organization).filter(
            models.Organization.is_active.is_(True)
        ).all()

        for org in orgs:
            retention_days = _get_retention_days(
                db, org.id, "history_retention_days", _DEFAULT_HISTORY_DAYS
            )
            cutoff = datetime.utcnow() - timedelta(days=retention_days)
            integration_ids = _integration_ids_for_org(db, org.id)

            if not integration_ids:
                continue

            result = db.execute(
                delete(models.History).where(
                    models.History.integration_id.in_(integration_ids),
                    models.History.timestamp < cutoff,
                )
            )
            deleted = result.rowcount
            if deleted:
                totals[str(org.id)] = deleted
                logger.info(
                    "purge history concluído",
                    extra={
                        "event": "retention.history_purge",
                        "org_id": org.id,
                        "deleted": deleted,
                        "cutoff": cutoff.isoformat(),
                    },
                )

        db.commit()

    return totals


@shared_task(bind=True, queue="maintenance")
def prune_expired_audit_logs(self: Any) -> dict[str, int]:
    """Deleta AuditLog expirado por organização.

    Vincula AuditLog à org via user_id → AppUser.organization_id.
    Entradas com user_id=NULL (eventos de sistema) NUNCA são tocadas.
    """
    totals: dict[str, int] = {}

    with database.SessionLocal() as db:
        orgs = db.query(models.Organization).filter(
            models.Organization.is_active.is_(True)
        ).all()

        for org in orgs:
            retention_days = _get_retention_days(
                db, org.id, "audit_log_retention_days", _DEFAULT_AUDIT_LOG_DAYS
            )
            cutoff = datetime.utcnow() - timedelta(days=retention_days)
            user_ids = _user_ids_for_org(db, org.id)

            if not user_ids:
                continue

            # Somente entradas com user_id explícito (não NULL).
            result = db.execute(
                delete(models.AuditLog).where(
                    models.AuditLog.user_id.in_(user_ids),
                    models.AuditLog.created_at < cutoff,
                )
            )
            deleted = result.rowcount
            if deleted:
                totals[str(org.id)] = deleted
                logger.info(
                    "purge audit logs concluído",
                    extra={
                        "event": "retention.audit_log_purge",
                        "org_id": org.id,
                        "deleted": deleted,
                        "cutoff": cutoff.isoformat(),
                    },
                )

        db.commit()

    return totals


@shared_task(bind=True, queue="maintenance")
def prune_expired_search_results(self: Any) -> dict[str, int]:
    """Deleta SearchResult expirados conforme retenção de cada org.

    O campo ``search_result_retention_days`` existe em
    ``OrganizationRetentionConfig`` mas não havia task de purge correspondente.
    Essa ausência causava crescimento ilimitado de search_results no banco.

    Filtra por integration_id IN (integrações da org).
    SearchResult sem integration_id (user_id-only) não é purgado por esta task —
    serão cobertos pela task de purge de usuários quando implementada.
    """
    totals: dict[str, int] = {}

    with database.SessionLocal() as db:
        orgs = db.query(models.Organization).filter(
            models.Organization.is_active.is_(True)
        ).all()

        for org in orgs:
            retention_days = _get_retention_days(
                db, org.id, "search_result_retention_days", _DEFAULT_SEARCH_RESULT_DAYS
            )
            cutoff = datetime.utcnow() - timedelta(days=retention_days)
            integration_ids = _integration_ids_for_org(db, org.id)

            if not integration_ids:
                continue

            result = db.execute(
                delete(models.SearchResult).where(
                    models.SearchResult.integration_id.in_(integration_ids),
                    models.SearchResult.created_at < cutoff,
                )
            )
            deleted = result.rowcount
            if deleted:
                totals[str(org.id)] = deleted
                logger.info(
                    "purge search results concluído",
                    extra={
                        "event": "retention.search_result_purge",
                        "org_id": org.id,
                        "deleted": deleted,
                        "cutoff": cutoff.isoformat(),
                    },
                )

        db.commit()

    return totals


@shared_task(bind=True, queue="maintenance")
def enforce_destination_retention(self: Any) -> dict[str, int]:
    """Expira dados nos destinos de ARMAZENAMENTO (tiering) por
    ``delivery.retention_days``.

    Para cada destino habilitado com ``retention_days > 0`` cujo kind declara a
    capability ``retention`` (hoje: ``s3``/object-store, o tier "cold"), chama
    ``prune_expired(retention_days)`` — apaga os objetos anteriores ao corte. É o
    enforcement que faltava: ``tier``/``retention_days`` deixam de ser só
    metadado. Destinos sem a capability (SIEM como Sentinel, barramento como
    Kafka) têm retenção PRÓPRIA no lado deles e são pulados (no-op documentado).

    Best-effort por destino (não propaga exceção — um destino quebrado não
    derruba os demais). Retorna ``{destination_id: nº_expirado}`` (-1 = falhou).
    """
    import asyncio

    from ..collectors.output.destinations import registry as _dest_registry
    from ..collectors.output.destinations.registry import DestinationConfig
    from ..core.secrets import get_default_backend

    results: dict[str, int] = {}

    # Snapshot dos campos necessários DENTRO da sessão (evita DetachedInstanceError
    # ao acessar atributos ORM depois do close + do asyncio.run por destino).
    candidates: list[dict[str, Any]] = []
    with database.SessionLocal() as db:
        for row in (
            db.query(models.Destination)
            .filter(models.Destination.enabled.is_(True))
            .all()
        ):
            kind = str(row.kind)
            if not _dest_registry.has(kind):
                continue
            if "retention" not in _dest_registry.get(kind).capabilities:
                continue
            delivery_raw = str(row.delivery or "{}")
            try:
                retention_days = int(json.loads(delivery_raw).get("retention_days", 0) or 0)
            except (ValueError, TypeError):
                retention_days = 0
            if retention_days <= 0:
                continue
            candidates.append(
                {
                    "id": str(row.id),
                    "kind": kind,
                    "retention_days": retention_days,
                    "config": str(row.config or "{}"),
                    "delivery": delivery_raw,
                    "secret_ref": str(row.secret_ref) if row.secret_ref else None,
                    "config_version": str(row.config_version or ""),
                    "name": str(row.name),
                    "organization_id": (
                        int(row.organization_id)
                        if row.organization_id is not None
                        else None
                    ),
                }
            )

    if not candidates:
        logger.info("destination_retention: nenhum destino com retention enforce-ável")
        return results

    secrets_backend = get_default_backend()

    for snap in candidates:
        dest_id = snap["id"]
        try:
            cfg = DestinationConfig(
                destination_id=dest_id,
                kind=snap["kind"],
                config=json.loads(snap["config"]),
                delivery=json.loads(snap["delivery"]),
                secret_ref=snap["secret_ref"],
                config_version=snap["config_version"],
                name=snap["name"],
                organization_id=snap["organization_id"],
            )
            connector = _dest_registry.build(cfg, secrets_backend)
            try:
                count = asyncio.run(connector.prune_expired(snap["retention_days"]))
                results[dest_id] = int(count or 0)
                logger.info(
                    "destination_retention: dest=%s kind=%s retention_days=%d expirados=%d",
                    dest_id,
                    snap["kind"],
                    snap["retention_days"],
                    results[dest_id],
                )
            finally:
                try:
                    asyncio.run(connector.close())
                except Exception:
                    pass
        except Exception as exc:
            results[dest_id] = -1
            logger.warning(
                "destination_retention: falha dest=%s kind=%s: %s",
                dest_id,
                snap["kind"],
                exc,
                exc_info=True,
            )

    return results


@shared_task(bind=True, queue="maintenance")
def prune_all(self: Any) -> dict[str, dict[str, int]]:
    """Wrapper que executa todos os prune_* em sequência.

    Retorna dict {task_name: {org_id: count}} para rastreabilidade.
    """
    results: dict[str, dict[str, int]] = {}

    tasks = [
        ("quarantine", prune_expired_quarantine),
        ("drift", prune_expired_drift),
        ("history", prune_expired_history),
        ("search_results", prune_expired_search_results),
        ("audit_logs", prune_expired_audit_logs),
        # tiering: expira objetos em destinos de armazenamento (S3) por
        # retention_days. Roda no mesmo ciclo diário.
        ("destination_retention", enforce_destination_retention),
    ]

    for name, task_fn in tasks:
        try:
            results[name] = task_fn.run()
        except Exception as exc:
            logger.error(
                "prune subtask falhou",
                extra={"event": "retention.subtask_error", "task": name, "error": str(exc)},
            )
            results[name] = {"error": str(exc)}  # type: ignore[assignment]

    logger.info("prune_all concluído", extra={"event": "retention.prune_all_done", "results": str(results)})
    return results


# ── Executor do right-to-delete ───────────────────────────────────────


@shared_task(bind=True, queue="maintenance.high", max_retries=2)
def execute_data_deletion(self: Any, job_id: str) -> dict[str, Any]:
    """Executa purge total de dados de uma organização (LGPD/GDPR).

    Ordem de deleção (dependente → pai):
    1. SearchResult, History, ActionRun (via integration_id da org).
    2. QuarantineEvent (via integration_id).
    3. UnknownField (via organization_id).
    4. CollectionState (via integration_id).
    5. BackfillJob (via integration_id).
    6. MappingAuditLog (via integration_id da org).
    7. Integration (integrações da org).
    8. AppUser (usuários da org) — hard delete, após audit.
    9. Organization (a própria org).
    10. Redis — cursores, token cache, dedupe, pipeline health.
    11. Wazuh Indexer — best-effort; falha → status='partial'.

    Audit master gravado em arquivo JSON imutável mesmo após deleção do DB.
    """
    with database.SessionLocal() as db:
        job = db.get(models.DataDeletionJob, job_id)
        if job is None:
            logger.error("execute_data_deletion: job_id=%s não encontrado", job_id)
            return {"error": "job not found"}

        job.status = "running"
        job.started_at = datetime.utcnow()
        db.commit()

        org_id = job.organization_id
        org_slug = job.organization_slug
        rows: dict[str, int] = {}

        try:
            integration_ids = _integration_ids_for_org(db, org_id)
            user_ids = _user_ids_for_org(db, org_id)

            # 1. SearchResult
            if integration_ids:
                r = db.execute(
                    delete(models.SearchResult).where(
                        models.SearchResult.integration_id.in_(integration_ids)
                    )
                )
                rows["search_results"] = r.rowcount

            # History
            if integration_ids:
                r = db.execute(
                    delete(models.History).where(
                        models.History.integration_id.in_(integration_ids)
                    )
                )
                rows["history"] = r.rowcount

            # ActionRun — usa client_ids JSON; melhor-esforço por integration_id
            # (ActionRun não tem FK direta; pulamos deleção automática por ora).
            rows["action_runs"] = 0  # sem FK direta, skip

            # 2. QuarantineEvent
            if integration_ids:
                r = db.execute(
                    delete(models.QuarantineEvent).where(
                        models.QuarantineEvent.integration_id.in_(integration_ids)
                    )
                )
                rows["quarantine_events"] = r.rowcount

            # 3. UnknownField — agora escopado por tenant, então
            # o right-to-erasure (LGPD/GDPR) apaga o drift inferido da org.
            r = db.execute(
                delete(models.UnknownField).where(
                    models.UnknownField.organization_id == org_id
                )
            )
            rows["unknown_fields"] = r.rowcount

            # 4. CollectionState
            if integration_ids:
                r = db.execute(
                    delete(models.CollectionState).where(
                        models.CollectionState.integration_id.in_(integration_ids)
                    )
                )
                rows["collection_state"] = r.rowcount

            # 5. BackfillJob
            if integration_ids:
                r = db.execute(
                    delete(models.BackfillJob).where(
                        models.BackfillJob.integration_id.in_(integration_ids)
                    )
                )
                rows["backfill_jobs"] = r.rowcount

            # 6. MappingAuditLog (via integration_id)
            if integration_ids:
                r = db.execute(
                    delete(models.MappingAuditLog).where(
                        models.MappingAuditLog.integration_id.in_(integration_ids)
                    )
                )
                rows["mapping_audit_log"] = r.rowcount

            # 7. Integration
            r = db.execute(
                delete(models.Integration).where(
                    models.Integration.organization_id == org_id
                )
            )
            rows["integrations"] = r.rowcount

            # 8. AppUser — hard delete após audit
            if user_ids:
                r = db.execute(
                    delete(models.AppUser).where(
                        models.AppUser.id.in_(user_ids)
                    )
                )
                rows["app_users"] = r.rowcount

            # 9. Organization
            r = db.execute(
                delete(models.Organization).where(
                    models.Organization.id == org_id
                )
            )
            rows["organizations"] = r.rowcount

            db.commit()

        except Exception as exc:
            db.rollback()
            # após rollback, a sessão atual está inválida/expirada.
            # Usar a mesma sessão para gravar status="failed" causaria erro silencioso
            # e deixaria o job preso em "running". Abrimos NOVA sessão para persistir
            # o estado de falha de forma independente.
            with database.SessionLocal() as fail_db:
                failed_job = fail_db.get(models.DataDeletionJob, job_id)
                if failed_job is not None:
                    failed_job.status = "failed"
                    failed_job.last_error = str(exc)[:2000]
                    failed_job.finished_at = datetime.utcnow()
                    fail_db.commit()
            logger.error(
                "execute_data_deletion FALHOU job_id=%s org_id=%s: %s",
                job_id,
                org_id,
                exc,
                exc_info=True,
            )
            raise self.retry(exc=exc, countdown=30)

        # 10. Redis purge.
        redis_error: str | None = None
        try:
            _purge_redis_for_integrations(integration_ids)
        except Exception as exc:
            redis_error = str(exc)
            logger.warning(
                "execute_data_deletion: Redis purge parcial job=%s: %s",
                job_id,
                exc,
            )

        # 11. Destination erasure — best-effort, async via asyncio.run.
        # Itera sobre destinos da org que suportam erasure (capability "erasure"):
        # - DLQ path: erase(event_ids) — apaga eventos rastreados na DLQ.
        # - Query path: erase(filter={"organization_id": org_id}) para destinos com
        #   capability "erasure_by_query" (ex. elastic_bulk) — apaga dados ENTREGUES
        #   via _delete_by_query, cobrindo o purge LGPD completo da org no cluster.
        # Gated: só executa se houver destinos configurados para a org.
        destination_erasure_partial = False
        erasure_outcomes: list[dict] = []
        try:
            _erasure_outcomes, destination_erasure_partial = _run_destination_erasure(
                org_id, job_id
            )
            erasure_outcomes = _erasure_outcomes
        except Exception as exc:
            destination_erasure_partial = True
            logger.warning(
                "execute_data_deletion: erasure de destinos falhou parcialmente job=%s: %s",
                job_id,
                exc,
            )

        # Determina status final.
        final_status = "completed"
        if redis_error or destination_erasure_partial:
            final_status = "partial"

        # Atualiza job (pode não existir mais no DB se a org foi deletada junto
        # com seus jobs via cascade, mas DataDeletionJob não tem FK cascade).
        with database.SessionLocal() as db2:
            job2 = db2.get(models.DataDeletionJob, job_id)
            if job2:
                job2.status = final_status
                job2.rows_deleted = json.dumps(rows, separators=(",", ":"))
                job2.finished_at = datetime.utcnow()
                db2.commit()

        # Audit master em arquivo (sobrevive à deleção do DB).
        _write_master_audit(
            job_id=job_id,
            org_id=org_id,
            org_slug=org_slug,
            rows=rows,
            status=final_status,
            redis_error=redis_error,
            erasure_outcomes=erasure_outcomes,
        )

        logger.info(
            "execute_data_deletion concluído job=%s org=%s status=%s rows=%s",
            job_id,
            org_id,
            final_status,
            rows,
        )
        return {"job_id": job_id, "status": final_status, "rows_deleted": rows}


def _purge_redis_for_integrations(integration_ids: list[int]) -> None:
    """Deleta chaves Redis relacionadas às integrações purgadas.

    Padrões deletados por integration_id:
    - collection:cursor:{id}:*
    - oauth_token:{id}
    - oauth_token:{id}:*
    - dedupe:{id}:*
    - pipeline_health:{id}
    """
    if not integration_ids:
        return

    try:
        import redis as redis_sync

        from ..core.config import settings

        redis_url = settings.REDIS_URL or "redis://localhost:6379/0"
        r = redis_sync.from_url(redis_url, decode_responses=True)

        for int_id in integration_ids:
            patterns = [
                f"collection:cursor:{int_id}:*",
                f"oauth_token:{int_id}",
                f"oauth_token:{int_id}:*",
                f"dedupe:{int_id}:*",
                f"pipeline_health:{int_id}",
            ]
            for pattern in patterns:
                # r.keys() é O(N) bloqueante em todo keyspace.
                # Em prod com muitas chaves de dedupe, trava o Redis para todos os
                # clientes. scan_iter é incremental (cursor-based) e não bloqueia.
                batch: list[str] = []
                for key in r.scan_iter(match=pattern, count=100):
                    batch.append(key)
                    if len(batch) >= 500:
                        r.delete(*batch)
                        batch = []
                if batch:
                    r.delete(*batch)
    except Exception as exc:
        # Redis pode estar offline — não bloqueia o purge principal.
        raise RuntimeError(f"Redis purge falhou: {exc}") from exc


def _write_master_audit(
    *,
    job_id: str,
    org_id: int,
    org_slug: str,
    rows: dict[str, int],
    status: str,
    redis_error: str | None,
    erasure_outcomes: list | None = None,
) -> None:
    """Grava audit master imutável em arquivo JSON.

    Path: /var/log/centralops/data-deletion/{job_id}.json
    Este arquivo sobrevive à deleção do DB — é a trilha de auditoria
    exigida pela LGPD/GDPR mesmo após o purge.
    """
    try:
        _DELETION_AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        audit_path = _DELETION_AUDIT_DIR / f"{job_id}.json"
        payload = {
            "job_id": job_id,
            "organization_id": org_id,
            "organization_slug": org_slug,
            "status": status,
            "rows_deleted": rows,
            "redis_error": redis_error,
            "destination_erasure": erasure_outcomes or [],
            "finished_at": datetime.utcnow().isoformat(),
        }
        with open(audit_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        logger.info("audit master gravado em %s", audit_path)
    except Exception as exc:
        logger.error(
            "Falha ao gravar audit master job=%s: %s", job_id, exc
        )


def _run_destination_erasure(
    org_id: int,
    job_id: str,
) -> tuple[list[dict], bool]:
    """Erasa eventos da org de destinos com capability ``erasure``.

    Retorna (outcomes, partial_flag):
    - outcomes: lista de dicts {destination_id, erased, failed, detail}
    - partial_flag: True se qualquer destino falhou parcialmente

    Implementação:
    1. Carrega destinos da org com capability "erasure" via registry.
    2. Colhe event_ids do DLQ da org (rastreados no DB).
    3. Para cada destino, chama erase() com asyncio.run() (Celery task é síncrona):
       a. event_ids (DLQ path) — sempre passado, mesmo que vazio.
       b. filter={"organization_id": org_id} para destinos com capability
          "erasure_by_query" (ex. elastic_bulk) — cobre dados ENTREGUES via
          _delete_by_query, garantindo purge LGPD completo mesmo sem DLQ.
    4. Loga outcome (best-effort: não propaga exceção por destino).

    Gated: sem destinos com erasure → retorna imediatamente.
    Destinos sem ``erasure_by_query`` mas sem DLQ events → skip seguro (vacuous ok).
    """
    import asyncio

    from ..collectors.output.destinations import registry as _dest_registry
    from ..collectors.output.destinations.registry import DestinationConfig
    from ..core.secrets import get_default_backend

    outcomes: list[dict] = []
    any_partial = False

    # Carrega destinos da org que suportam erasure.
    with database.SessionLocal() as db:
        dest_rows = (
            db.query(models.Destination)
            .filter(
                models.Destination.organization_id == org_id,
                models.Destination.enabled.is_(True),
            )
            .all()
        )
        # Filtra só os que têm capability "erasure" no registry.
        erasure_dests = [
            row
            for row in dest_rows
            if (
                _dest_registry.has(str(row.kind))
                and "erasure" in _dest_registry.get(str(row.kind)).capabilities
            )
        ]

        if not erasure_dests:
            logger.info(
                "destination_erasure: nenhum destino com capability=erasure para org=%s",
                org_id,
            )
            return outcomes, False

        # Colhe event_ids do DLQ da org (eventos rastreados pelo dispatcher).
        dlq_rows = (
            db.query(models.DestinationDeadLetter.event_id)
            .filter(models.DestinationDeadLetter.organization_id == org_id)
            .distinct()
            .all()
        )
        event_ids: list[str] = [str(r.event_id) for r in dlq_rows]

    # Classifica destinos: erasure_by_query suporta filtro de org (dados entregues).
    org_filter = {"organization_id": org_id}

    secrets_backend = get_default_backend()

    for dest_row in erasure_dests:
        dest_id = str(dest_row.id)
        kind = str(dest_row.kind)
        supports_query_erasure = (
            _dest_registry.has(kind)
            and "erasure_by_query" in _dest_registry.get(kind).capabilities
        )

        # Para destinos sem erasure_by_query e sem DLQ events: vacuous ok, pula.
        if not supports_query_erasure and not event_ids:
            logger.info(
                "destination_erasure: sem DLQ events e sem erasure_by_query, "
                "pulando dest=%s org=%s",
                dest_id,
                org_id,
            )
            continue

        try:
            cfg = DestinationConfig(
                destination_id=dest_id,
                kind=kind,
                config=json.loads(str(dest_row.config or "{}")),
                delivery=json.loads(str(dest_row.delivery or "{}")),
                secret_ref=str(dest_row.secret_ref) if dest_row.secret_ref else None,
                config_version=str(dest_row.config_version or ""),
                name=str(dest_row.name),
                organization_id=org_id,
            )
            connector = _dest_registry.build(cfg, secrets_backend)
            try:
                # Destinos com erasure_by_query: passa filter para cobrir dados
                # entregues via _delete_by_query (além do DLQ path).
                # Destinos sem erasure_by_query: apenas DLQ path (event_ids).
                if supports_query_erasure:
                    result = asyncio.run(
                        connector.erase(event_ids, filter=org_filter)
                    )
                else:
                    result = asyncio.run(connector.erase(event_ids))
                outcome: dict = {
                    "destination_id": dest_id,
                    "erased": result.erased,
                    "failed": result.failed,
                    "detail": result.detail,
                }
                if result.failed:
                    any_partial = True
                    logger.warning(
                        "destination_erasure: partial job=%s dest=%s erased=%d failed=%d",
                        job_id,
                        dest_id,
                        len(result.erased),
                        len(result.failed),
                    )
                else:
                    logger.info(
                        "destination_erasure: ok job=%s dest=%s erased=%d",
                        job_id,
                        dest_id,
                        len(result.erased),
                    )
            finally:
                # Best-effort close — ignore errors.
                try:
                    asyncio.run(connector.close())
                except Exception:
                    pass
        except Exception as exc:
            any_partial = True
            outcome = {
                "destination_id": dest_id,
                "erased": [],
                "failed": event_ids or [f"org:{org_id}"],
                "detail": f"erro ao construir/executar conector: {exc}",
            }
            logger.warning(
                "destination_erasure: erro no destino job=%s dest=%s: %s",
                job_id,
                dest_id,
                exc,
            )
        outcomes.append(outcome)

    return outcomes, any_partial
