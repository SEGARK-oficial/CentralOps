"""Router de ScheduledQuery (agendamentos de XDR Query).

Segurança:
- Removido ``require_admin_user`` do router-level (era admin-only global).
- POST / PUT / DELETE: requerem MAPPING_WRITE (engineer+).
- GET list / GET /{id}/history: requerem MAPPING_READ.
- client_ids no payload são validados contra o tenant scope do usuário.
"""

import math
from datetime import datetime
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..api import schemas
from ..core import tenant
from ..core.auth import Permission, require_permission
from ..core.errors import ApiError
from ..db import models, repository, database
from ..services.search_results import SearchResultRetentionService
from ..services.scheduler import _convert_to_timedelta, _execute_schedule

# router sem dependencies globais — cada endpoint declara sua permissão.
router = APIRouter(
    prefix="/schedules",
    tags=["schedules"],
)


def get_repo(db: Session = Depends(database.get_session)) -> repository.ScheduledQueryRepository:
    return repository.ScheduledQueryRepository(db)


def get_query_repo(db: Session = Depends(database.get_session)) -> repository.PredefinedQueryRepository:
    return repository.PredefinedQueryRepository(db)


def get_integration_repo(db: Session = Depends(database.get_session)) -> repository.IntegrationRepository:
    return repository.IntegrationRepository(db)


def get_results_repo(db: Session = Depends(database.get_session)) -> repository.SearchResultRepository:
    SearchResultRetentionService(db).prune_expired_entries(force=True)
    return repository.SearchResultRepository(db)


def _parse_client_ids(raw_client_ids: str | None) -> list[int]:
    if not raw_client_ids:
        return []
    return [int(value) for value in raw_client_ids.split(",") if value.strip()]


def _serialize_schedule(
    sched: models.ScheduledQuery,
    *,
    query_title: str | None = None,
) -> schemas.ScheduledQueryRead:
    lookback_value = getattr(sched, "lookback_value", None) or sched.days_back or 1
    lookback_unit = getattr(sched, "lookback_unit", None) or "days"
    lookback_window = _convert_to_timedelta(lookback_value, lookback_unit)
    days_back = max(1, math.ceil(lookback_window.total_seconds() / 86400))

    return schemas.ScheduledQueryRead(
        id=sched.id,
        organization_id=sched.organization_id,
        query_id=sched.query_id,
        query_title=query_title,
        client_ids=_parse_client_ids(sched.client_ids),
        interval_value=sched.interval_value or sched.interval_minutes,
        interval_unit=sched.interval_unit or "minutes",
        lookback_value=lookback_value,
        lookback_unit=lookback_unit,
        notify_on_results=bool(getattr(sched, "notify_on_results", False)),
        days_back=days_back,
        next_run=sched.next_run,
        last_run_at=getattr(sched, "last_run_at", None),
        created_at=getattr(sched, "created_at", None),
        updated_at=getattr(sched, "updated_at", None),
    )


def _validate_schedule_client_ids(
    client_ids: list[int],
    current_user: models.AppUser,
    integration_repo: repository.IntegrationRepository,
) -> tuple[list[int], list[int]]:
    """Valida que todas as integrações existem, são Sophos e têm credenciais.

    Também valida tenant scope para non-admin.
    Retorna (missing_ids, invalid_ids) — vazio = tudo ok.
    """
    missing_ids: list[int] = []
    invalid_ids: list[int] = []
    for integration_id in client_ids:
        integration = integration_repo.get(integration_id)
        if not integration or integration.platform != "sophos":
            missing_ids.append(integration_id)
            continue

        # valida tenant scope
        tenant.require_subtree_access(current_user, integration.organization_id)

        ok, _ = integration_repo.has_resolvable_credentials(integration)
        if not ok:
            invalid_ids.append(integration_id)

    return missing_ids, invalid_ids


@router.post("/", response_model=schemas.ScheduledQueryRead)
def create_schedule(
    data: schemas.ScheduledQueryCreate,
    db: Session = Depends(database.get_session),
    repo: repository.ScheduledQueryRepository = Depends(get_repo),
    qrepo: repository.PredefinedQueryRepository = Depends(get_query_repo),
    integration_repo: repository.IntegrationRepository = Depends(get_integration_repo),
    # agendar uma query salva exige QUERY_SAVE (engineer+).
    current_user: models.AppUser = Depends(require_permission(Permission.QUERY_SAVE)),
):
    query = qrepo.get(data.query_id)
    if not query:
        raise ApiError(
            "query.not_found",
            404,
            messages={
                "pt": "Query não encontrada.",
                "en": "Query not found.",
                "es": "Consulta no encontrada.",
            },
        )

    missing_ids, invalid_ids = _validate_schedule_client_ids(
        data.client_ids, current_user, integration_repo
    )

    if missing_ids:
        missing_text = ", ".join(str(i) for i in missing_ids)
        raise ApiError(
            "schedule.sophos_integration_not_found",
            404,
            messages={
                "pt": "Integração(ões) Sophos não encontrada(s): {ids}",
                "en": "Sophos integration(s) not found: {ids}",
                "es": "Integración(es) Sophos no encontrada(s): {ids}",
            },
            params={"ids": missing_text},
        )
    if invalid_ids:
        invalid_text = ", ".join(str(i) for i in invalid_ids)
        raise ApiError(
            "schedule.integration_incomplete_auth",
            400,
            messages={
                "pt": "Integração(ões) sem autenticação completa para agendamento: {ids}",
                "en": "Integration(s) without complete authentication for scheduling: {ids}",
                "es": "Integración(es) sin autenticación completa para la programación: {ids}",
            },
            params={"ids": invalid_text},
        )

    minutes = int(
        _convert_to_timedelta(data.interval_value, data.interval_unit).total_seconds()
        / 60
    )
    lookback_days = max(
        1,
        math.ceil(
            _convert_to_timedelta(data.lookback_value, data.lookback_unit).total_seconds()
            / 86400
        ),
    )
    # Carimba a org dona: escopado herda a própria; global deriva das
    # integrações (client_ids). Habilita escopo de leitura/delete por tenant.
    candidate_orgs = [
        integ.organization_id
        for cid in data.client_ids
        if (integ := integration_repo.get(cid)) is not None
    ]
    org_id = tenant.resolve_owner_org_id(
        current_user, candidate_org_ids=candidate_orgs
    )

    sched = models.ScheduledQuery(
        query_id=data.query_id,
        organization_id=org_id,
        client_ids=",".join(str(c) for c in data.client_ids),
        interval_minutes=minutes,
        interval_value=data.interval_value,
        interval_unit=data.interval_unit,
        days_back=lookback_days,
        lookback_value=data.lookback_value,
        lookback_unit=data.lookback_unit,
        notify_on_results=data.notify_on_results,
        next_run=datetime.utcnow() + _convert_to_timedelta(data.interval_value, data.interval_unit),
    )
    created = repo.add(sched)
    _execute_schedule(db, created, actor_user_id=current_user.id)
    refreshed = repo.get(created.id) or created
    return _serialize_schedule(refreshed, query_title=query.title)


@router.get("/", response_model=list[schemas.ScheduledQueryRead])
def list_schedules(
    repo: repository.ScheduledQueryRepository = Depends(get_repo),
    qrepo: repository.PredefinedQueryRepository = Depends(get_query_repo),
    # requer MAPPING_READ.
    current_user: models.AppUser = Depends(require_permission(Permission.MAPPING_READ)),
):
    # Escopo de tenant: usuário escopado vê só agendamentos da própria org.
    scoped = tenant.accessible_org_ids(current_user, repo.db)
    query_titles = {query.id: query.title for query in qrepo.list(scoped)}
    return [
        _serialize_schedule(schedule, query_title=query_titles.get(schedule.query_id))
        for schedule in repo.list(scoped)
    ]


@router.delete("/{sched_id}")
def delete_schedule(
    sched_id: int,
    repo: repository.ScheduledQueryRepository = Depends(get_repo),
    # gerir agendamento de query exige QUERY_SAVE.
    current_user: models.AppUser = Depends(require_permission(Permission.QUERY_SAVE)),
):
    sched = repo.get(sched_id)
    # Bloqueia delete cross-tenant (era CRÍTICO: qualquer engineer escopado
    # deletava agendamento de outra org). 404 = não vaza existência.
    if not sched or not tenant.can_access_organization(current_user, sched.organization_id):
        raise ApiError(
            "schedule.not_found",
            404,
            messages={
                "pt": "Agendamento não encontrado.",
                "en": "Schedule not found.",
                "es": "Programación no encontrada.",
            },
        )
    repo.delete(sched)
    return {"detail": "Schedule deleted"}


@router.get("/{sched_id}/history", response_model=list[schemas.SearchResultRead])
def schedule_history(
    sched_id: int,
    results_repo: repository.SearchResultRepository = Depends(get_results_repo),
    repo: repository.ScheduledQueryRepository = Depends(get_repo),
    # requer MAPPING_READ.
    current_user: models.AppUser = Depends(require_permission(Permission.MAPPING_READ)),
):
    sched = repo.get(sched_id)
    # 404 fora do escopo (os resultados já são filtrados por viewer, mas isto
    # evita confirmar a existência de agendamento de outra org).
    if not sched or not tenant.can_access_organization(current_user, sched.organization_id):
        raise ApiError(
            "schedule.not_found",
            404,
            messages={
                "pt": "Agendamento não encontrado.",
                "en": "Schedule not found.",
                "es": "Programación no encontrada.",
            },
        )
    return results_repo.list(schedule_id=sched_id, viewer=current_user)
