"""Router de PredefinedQuery (SQL statements reutilizáveis para XDR Query).

Segurança:
- POST / PUT / DELETE: requerem MAPPING_WRITE (engineer+).
- GET: requer MAPPING_READ (todos os papéis autenticados).
- client_ids no payload são validados contra o tenant scope do usuário:
  admin bypassa; non-admin só pode referenciar integrações da própria org.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..api import schemas
from ..core import tenant
from ..core.auth import Permission, require_permission
from ..core.errors import ApiError
from ..db import models, repository, database

router = APIRouter(prefix="/queries", tags=["queries"])


def get_repo(db: Session = Depends(database.get_session)) -> repository.PredefinedQueryRepository:
    return repository.PredefinedQueryRepository(db)


def get_schedule_repo(db: Session = Depends(database.get_session)) -> repository.ScheduledQueryRepository:
    return repository.ScheduledQueryRepository(db)


def get_integration_repo(db: Session = Depends(database.get_session)) -> repository.IntegrationRepository:
    return repository.IntegrationRepository(db)


def _db_to_schema(q: models.PredefinedQuery) -> schemas.PredefinedQueryRead:
    ids = [int(x) for x in q.client_ids.split(',')] if q.client_ids else []
    return schemas.PredefinedQueryRead(
        id=q.id,
        title=q.title,
        description=q.description,
        statement=q.statement,
        table=q.table,
        client_ids=ids,
        organization_id=q.organization_id,
    )


def _validate_client_ids_tenant(
    client_ids: list[int],
    current_user: models.AppUser,
    integration_repo: repository.IntegrationRepository,
) -> None:
    """Valida que todos os client_ids pertencem ao tenant do usuário.

    Admin bypassa a validação. Non-admin recebe 403 se tentar referenciar
    uma integração de outra organização.
    """
    if tenant.has_global_scope(current_user):
        return
    for cid in client_ids:
        integration = integration_repo.get(cid)
        if integration is None:
            # Integração inexistente — deixa o handler do endpoint decidir
            continue
        tenant.require_subtree_access(current_user, integration.organization_id)


@router.post("/", response_model=schemas.PredefinedQueryRead)
def create_query(
    data: schemas.PredefinedQueryCreate,
    repo: repository.PredefinedQueryRepository = Depends(get_repo),
    integration_repo: repository.IntegrationRepository = Depends(get_integration_repo),
    # Requer MAPPING_WRITE (engineer+).
    current_user: models.AppUser = Depends(require_permission(Permission.QUERY_SAVE)),
):
    # Valida tenant scope dos client_ids fornecidos
    if data.client_ids:
        _validate_client_ids_tenant(data.client_ids, current_user, integration_repo)

    # Carimba a org dona: escopado herda a própria; global direciona (body) ou
    # deriva das integrações referenciadas. Fecha o leak de queries cross-tenant.
    candidate_orgs = [
        integ.organization_id
        for cid in (data.client_ids or [])
        if (integ := integration_repo.get(cid)) is not None
    ]
    org_id = tenant.resolve_owner_org_id(
        current_user,
        explicit_org_id=data.organization_id,
        candidate_org_ids=candidate_orgs,
    )

    db_query = models.PredefinedQuery(
        title=data.title,
        description=data.description,
        statement=data.statement,
        table=data.table,
        client_ids=','.join(str(cid) for cid in (data.client_ids or [])) if data.client_ids else None,
        organization_id=org_id,
    )
    q = repo.add(db_query)
    return _db_to_schema(q)


@router.get("/", response_model=list[schemas.PredefinedQueryRead])
def list_queries(
    repo: repository.PredefinedQueryRepository = Depends(get_repo),
    # Requer MAPPING_READ (todos os papéis autenticados na matriz atual).
    current_user: models.AppUser = Depends(require_permission(Permission.MAPPING_READ)),
):
    # Escopo de tenant: usuário escopado vê só as queries da própria org.
    scoped = tenant.accessible_org_ids(current_user, repo.db)
    return [_db_to_schema(q) for q in repo.list(scoped)]


@router.get("/{query_id}", response_model=schemas.PredefinedQueryRead)
def get_query(
    query_id: int,
    repo: repository.PredefinedQueryRepository = Depends(get_repo),
    # Requer MAPPING_READ.
    current_user: models.AppUser = Depends(require_permission(Permission.MAPPING_READ)),
):
    q = repo.get(query_id)
    # 404 (não 403) quando fora do escopo: não vaza existência cross-tenant.
    if not q or not tenant.can_access_organization(current_user, q.organization_id):
        raise ApiError(
            "query.not_found",
            404,
            messages={
                "pt": "Query não encontrada.",
                "en": "Query not found.",
                "es": "Consulta no encontrada.",
            },
        )
    return _db_to_schema(q)


@router.put("/{query_id}", response_model=schemas.PredefinedQueryRead)
def update_query(
    query_id: int,
    data: schemas.PredefinedQueryUpdate,
    repo: repository.PredefinedQueryRepository = Depends(get_repo),
    integration_repo: repository.IntegrationRepository = Depends(get_integration_repo),
    # Requer MAPPING_WRITE.
    current_user: models.AppUser = Depends(require_permission(Permission.QUERY_SAVE)),
):
    q = repo.get(query_id)
    # Bloqueia edição cross-tenant do recurso (404 = não vaza existência).
    if not q or not tenant.can_access_organization(current_user, q.organization_id):
        raise ApiError(
            "query.not_found",
            404,
            messages={
                "pt": "Query não encontrada.",
                "en": "Query not found.",
                "es": "Consulta no encontrada.",
            },
        )

    # Valida tenant scope dos client_ids atualizados
    if data.client_ids is not None:
        _validate_client_ids_tenant(data.client_ids, current_user, integration_repo)

    client_ids = None
    if data.client_ids is not None:
        client_ids = ','.join(str(cid) for cid in data.client_ids)
    updated = repo.update(
        q,
        title=data.title,
        description=data.description,
        statement=data.statement,
        table=data.table,
        client_ids=client_ids,
    )
    return _db_to_schema(updated)


@router.delete("/{query_id}")
def delete_query(
    query_id: int,
    repo: repository.PredefinedQueryRepository = Depends(get_repo),
    sched_repo: repository.ScheduledQueryRepository = Depends(get_schedule_repo),
    # Gerir query salva exige QUERY_SAVE.
    current_user: models.AppUser = Depends(require_permission(Permission.QUERY_SAVE)),
):
    q = repo.get(query_id)
    # Bloqueia delete cross-tenant (404 = não vaza existência).
    if not q or not tenant.can_access_organization(current_user, q.organization_id):
        raise ApiError(
            "query.not_found",
            404,
            messages={
                "pt": "Query não encontrada.",
                "en": "Query not found.",
                "es": "Consulta no encontrada.",
            },
        )
    if sched_repo.list_by_query_id(query_id):
        raise ApiError(
            "query.linked_to_active_schedule",
            409,
            messages={
                "pt": "Query vinculada a um agendamento ativo. Remova o agendamento antes de excluir a query.",
                "en": "Query linked to an active schedule. Remove the schedule before deleting the query.",
                "es": "Consulta vinculada a una programación activa. Elimine la programación antes de eliminar la consulta.",
            },
        )
    repo.delete(q)
    return {"detail": "Query deleted"}
