"""Organization CRUD router — inclui endpoints de retenção,
right-to-delete e bulk operations."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..api import schemas
from ..core import auth as app_auth
from ..core import edition
from ..core import tenant
from ..core.errors import ApiError
from ..db import database, hierarchy, models, repository

logger = logging.getLogger(__name__)

# Chave do advisory lock que serializa a verificação do teto de orgs do tier
# (distinta da chave de migração 0x0C0DE004 em database.py).
_ORG_LIMIT_ADVISORY_LOCK_KEY = 0x0C0DE0D6

router = APIRouter(prefix="/organizations", tags=["organizations"])


def get_repo(db: Session = Depends(database.get_session)) -> repository.OrganizationRepository:
    return repository.OrganizationRepository(db)


def _generate_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", name.strip().lower()).strip("-")


def _iris_customer_id_of(org: models.Organization) -> Optional[int]:
    """Lê o IRIS customer id do mapping (kind='iris'), não mais da
    coluna deprecada. ``None`` se não houver mapping. Mantém o campo
    ``iris_customer_id`` na API estável durante a transição."""
    for mapping in (getattr(org, "customer_mappings", None) or []):
        if mapping.destination_kind == "iris":
            try:
                return int(mapping.external_customer_id)
            except (TypeError, ValueError):
                return None
    return None


def _serialize(org: models.Organization) -> schemas.OrganizationRead:
    return schemas.OrganizationRead(
        id=org.id,
        name=org.name,
        slug=org.slug,
        description=org.description,
        is_active=org.is_active,
        integration_count=len(org.integrations) if org.integrations else 0,
        # Sophos Partner Mode + IRIS linkage.
        external_provider=org.external_provider,
        external_id=org.external_id,
        auto_managed=bool(org.auto_managed),
        iris_customer_id=_iris_customer_id_of(org),
        partner_integration_id=org.partner_integration_id,
        created_at=org.created_at,
        updated_at=org.updated_at,
    )


def _parse_tristate(value: Optional[str], param: str) -> Optional[bool]:
    """Parse query strings ``"true"|"false"|"all"`` em ``Optional[bool]``.

    ``None`` ou ``"all"`` → ``None`` (sem filtro). Aceita case-insensitive.
    Retorna 422 quando o valor não é reconhecido.
    """
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in ("", "all"):
        return None
    if normalized in ("true", "1", "yes"):
        return True
    if normalized in ("false", "0", "no"):
        return False
    raise ApiError(
        "org.invalid_tristate_value",
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        messages={
            "pt": "Valor inválido para {param!r}: esperado 'true', 'false' ou 'all'.",
            "en": "Invalid value for {param!r}: expected 'true', 'false' or 'all'.",
            "es": "Valor inválido para {param!r}: se esperaba 'true', 'false' o 'all'.",
        },
        params={"param": param},
    )


@router.post("/", response_model=schemas.OrganizationRead)
def create_organization(
    data: schemas.OrganizationCreate,
    repo: repository.OrganizationRepository = Depends(get_repo),
    current_user: models.AppUser = Depends(app_auth.require_admin_user),
):
    # Criar Organization é ação de PLATAFORMA: só admin GLOBAL.
    # (Flag OFF ⇒ todo admin é global ⇒ no-op. Um admin-de-org não cria orgs.)
    tenant.require_global_scope(current_user)

    # Trava single-tenant: o tier (via licença) pode limitar o nº de orgs
    # (Starter = 1 org). Fail-closed-to-Community: sem licença/claim → max_organizations
    # é None → SEM teto (o core AGPL é irrestrito; a trava é uma feature do tier pago).
    org_limit = edition.max_organizations()
    if org_limit is not None:
        # Lock transaction-scoped serializa contagem→inserção contra POSTs
        # concorrentes: sob READ COMMITTED dois requests poderiam ambos ler
        # count<limit e ambos inserir, furando a trava. Auto-liberado no
        # commit de repo.add() (a seção crítica termina na inserção durável).
        # Postgres-only; SQLite (testes) é single-writer e dispensa o lock.
        if repo.db.get_bind().dialect.name == "postgresql":
            repo.db.execute(
                text("SELECT pg_advisory_xact_lock(:k)"),
                {"k": _ORG_LIMIT_ADVISORY_LOCK_KEY},
            )
        # count() conta apenas orgs ATIVAS (include_inactive=False): a trava é
        # "1 tenant ativo", não "1 org já criada na história" — uma org
        # soft-deletada NÃO consome a vaga (senão o cliente fica travado p/ sempre).
        if repo.count() >= org_limit:
            raise ApiError(
                "org.plan_limit_reached",
                403,
                messages={
                    "pt": (
                        "Limite de organizações do plano atingido ({limit}). "
                        "Faça upgrade do plano para adicionar mais organizações."
                    ),
                    "en": (
                        "Plan organization limit reached ({limit}). "
                        "Upgrade your plan to add more organizations."
                    ),
                    "es": (
                        "Límite de organizaciones del plan alcanzado ({limit}). "
                        "Actualice su plan para agregar más organizaciones."
                    ),
                },
                params={"limit": org_limit},
            )
    slug = data.slug or _generate_slug(data.name)

    if repo.get_by_name(data.name):
        raise ApiError(
            "org.name_already_exists",
            409,
            messages={
                "pt": "Já existe uma organização com esse nome.",
                "en": "Organization name already exists.",
                "es": "Ya existe una organización con ese nombre.",
            },
        )
    if repo.get_by_slug(slug):
        raise ApiError(
            "org.slug_already_exists",
            409,
            messages={
                "pt": "Já existe uma organização com esse slug.",
                "en": "Organization slug already exists.",
                "es": "Ya existe una organización con ese slug.",
            },
        )

    org = models.Organization(
        name=data.name,
        slug=slug,
        description=data.description,
    )
    try:
        org = repo.add(org)
    except IntegrityError:
        repo.db.rollback()
        raise ApiError(
            "org.already_exists",
            409,
            messages={
                "pt": "Organização já existe.",
                "en": "Organization already exists.",
                "es": "La organización ya existe.",
            },
        )

    # Org criada manualmente é raiz sob a plataforma (sem parent):
    # root_id=self, depth=0, kind=customer + self-pair na closure. Idempotente.
    hierarchy.assign_on_create(repo.db, org)
    repo.db.commit()

    return _serialize(org)


@router.get("/", response_model=list[schemas.OrganizationRead])
def list_organizations(
    response: Response,
    include_inactive: bool = Query(
        default=False,
        description="DEPRECATED — use `status` em vez disso. Mantido por compat.",
    ),
    name: Optional[str] = Query(
        default=None,
        description="Substring case-insensitive em name ou slug.",
    ),
    status: Optional[str] = Query(
        default=None,
        description="'active' (default), 'inactive' ou 'all'. Sobrepõe include_inactive.",
    ),
    auto_managed: Optional[str] = Query(
        default=None,
        description="'true', 'false' ou 'all'. Default: 'all' (sem filtro).",
    ),
    external_provider: Optional[str] = Query(
        default=None,
        description="Filtra por external_provider (igualdade exata, ex: 'sophos').",
    ),
    page: int = Query(default=1, ge=1, description="1-indexed page number."),
    size: int = Query(
        default=50,
        ge=1,
        le=200,
        description="Itens por página. Cap 200.",
    ),
    repo: repository.OrganizationRepository = Depends(get_repo),
    current_user: models.AppUser = Depends(app_auth.require_authenticated_user),
):
    """Lista organizações com filtros e paginação.

    Compat: chamadas antigas sem query params recebem comportamento idêntico
    (apenas orgs ativas, ordenado por nome). Headers ``X-Total-Count``,
    ``X-Page``, ``X-Size`` expostos pra paginação simples lado-cliente.
    """
    auto_managed_filter = _parse_tristate(auto_managed, "auto_managed")

    # Resolve status final. Se nada foi dito, usar comportamento legado.
    effective_status: Optional[str] = None
    if status is not None:
        normalized = status.strip().lower()
        if normalized not in ("active", "inactive", "all", ""):
            raise ApiError(
                "org.invalid_status_value",
                422,
                messages={
                    "pt": "Valor inválido para 'status': esperado 'active', 'inactive' ou 'all'.",
                    "en": "Invalid value for 'status': expected 'active', 'inactive' or 'all'.",
                    "es": "Valor inválido para 'status': se esperaba 'active', 'inactive' o 'all'.",
                },
            )
        effective_status = normalized or None

    # Permissão: include_inactive / status='inactive' / status='all' exigem admin.
    is_admin = tenant.is_admin(current_user)
    if not is_admin:
        if effective_status in ("inactive", "all"):
            # Operadores não-admin só veem ativas.
            effective_status = "active"
        else:
            include_inactive = False

    org_ids = tenant.accessible_org_ids(current_user, repo.db)

    list_kwargs = dict(
        include_inactive=include_inactive,
        organization_ids=org_ids,
        name_query=name,
        status=effective_status,
        auto_managed=auto_managed_filter,
        external_provider=external_provider.strip() if external_provider else None,
    )

    total = repo.count(**list_kwargs)  # type: ignore[arg-type]
    offset = (page - 1) * size
    rows = repo.list(offset=offset, limit=size, **list_kwargs)  # type: ignore[arg-type]

    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Page"] = str(page)
    response.headers["X-Size"] = str(size)

    return [_serialize(org) for org in rows]


@router.post(
    "/bulk/deactivate",
    response_model=schemas.BulkDeactivateOrganizationsResult,
)
def bulk_deactivate_organizations(
    payload: schemas.BulkDeactivateOrganizationsRequest,
    db: Session = Depends(database.get_session),
    current_user: models.AppUser = Depends(app_auth.require_admin_user),
) -> schemas.BulkDeactivateOrganizationsResult:
    """Soft-deactivate em lote de organizações (até 500 IDs/request).

    Política:
    - Idempotente: org já inactive → conta em ``processed`` mas não em
      ``deactivated`` e não gera erro.
    - Bloqueia ``auto_managed=True``: caem em ``errors`` com motivo claro.
      Operador deve usar o fluxo Sophos Partner pra mexer nessas orgs.
    - IDs inexistentes caem em ``errors`` (404 lógico, não derruba o request).
    - Cada transição grava AuditLog para rastreabilidade.

    Permissão: ``require_admin_user`` (mesma do DELETE individual).
    """
    repo = repository.OrganizationRepository(db)

    # De-dup IDs preservando ordem para resposta determinística.
    seen: set[int] = set()
    unique_ids: list[int] = []
    for raw in payload.ids:
        if raw not in seen:
            seen.add(raw)
            unique_ids.append(raw)

    deactivated_count = 0
    errors: list[schemas.BulkDeactivateOrganizationsError] = []
    audit_entries: list[models.AuditLog] = []

    for org_id in unique_ids:
        org = repo.get(org_id)
        if org is None:
            errors.append(
                schemas.BulkDeactivateOrganizationsError(
                    id=org_id, reason="organization not found"
                )
            )
            continue
        if org.auto_managed:
            errors.append(
                schemas.BulkDeactivateOrganizationsError(
                    id=org_id,
                    reason="auto_managed organization (Sophos)",
                )
            )
            continue
        if not org.is_active:
            # Idempotente — conta como processado, sem audit entry duplicado.
            continue

        org.is_active = False
        org.updated_at = datetime.utcnow()
        deactivated_count += 1
        audit_entries.append(
            models.AuditLog(
                user_id=current_user.id,
                username=current_user.username,
                user_role=current_user.role,
                action="bulk_deactivate_organization",
                endpoint="/api/organizations/bulk/deactivate",
                method="POST",
                status_code=200,
                detail=(
                    f"Org {org_id} ({org.slug}) desativada via bulk operation."
                ),
            )
        )

    for entry in audit_entries:
        db.add(entry)
    db.commit()

    logger.info(
        "bulk_deactivate_organizations processed=%s deactivated=%s errors=%s by=%s",
        len(unique_ids),
        deactivated_count,
        len(errors),
        current_user.username,
    )

    return schemas.BulkDeactivateOrganizationsResult(
        processed=len(unique_ids),
        deactivated=deactivated_count,
        errors=errors,
    )


@router.get("/{org_id}", response_model=schemas.OrganizationRead)
def get_organization(
    org_id: int,
    repo: repository.OrganizationRepository = Depends(get_repo),
    current_user: models.AppUser = Depends(app_auth.require_authenticated_user),
):
    org = repo.get(org_id)
    if not org:
        raise ApiError(
            "org.not_found",
            404,
            messages={
                "pt": "Organização não encontrada.",
                "en": "Organization not found.",
                "es": "Organización no encontrada.",
            },
        )
    tenant.require_subtree_access(current_user, org.id)
    if not org.is_active and not tenant.is_admin(current_user):
        raise ApiError(
            "org.inactive",
            403,
            messages={
                "pt": "Organização está inativa.",
                "en": "Organization is inactive.",
                "es": "La organización está inactiva.",
            },
        )
    return _serialize(org)


@router.put("/{org_id}", response_model=schemas.OrganizationRead)
def update_organization(
    org_id: int,
    data: schemas.OrganizationUpdate,
    repo: repository.OrganizationRepository = Depends(get_repo),
    _: models.AppUser = Depends(app_auth.require_admin_user),
):
    org = repo.get(org_id)
    if not org:
        raise ApiError(
            "org.not_found",
            404,
            messages={
                "pt": "Organização não encontrada.",
                "en": "Organization not found.",
                "es": "Organización no encontrada.",
            },
        )

    if data.name and data.name != org.name:
        existing = repo.get_by_name(data.name)
        if existing and existing.id != org.id:
            raise ApiError(
                "org.name_already_exists",
                409,
                messages={
                    "pt": "Já existe uma organização com esse nome.",
                    "en": "Organization name already exists.",
                    "es": "Ya existe una organización con ese nombre.",
                },
            )

    update_kwargs = {}
    if data.name is not None:
        update_kwargs["name"] = data.name
    if data.description is not None:
        update_kwargs["description"] = data.description
    if data.is_active is not None:
        update_kwargs["is_active"] = data.is_active

    org = repo.update(org, **update_kwargs)
    return _serialize(org)


@router.delete("/{org_id}")
def delete_organization(
    org_id: int,
    repo: repository.OrganizationRepository = Depends(get_repo),
    current_user: models.AppUser = Depends(app_auth.require_admin_user),
):
    # Deletar Organization é ação de PLATAFORMA: só admin GLOBAL.
    tenant.require_global_scope(current_user)
    org = repo.get(org_id)
    if not org:
        raise ApiError(
            "org.not_found",
            404,
            messages={
                "pt": "Organização não encontrada.",
                "en": "Organization not found.",
                "es": "Organización no encontrada.",
            },
        )
    repo.delete(org)
    return {"detail": "Organization deleted"}


# ── IRIS DFIR linkage (manual sync para Orgs não-Partner) ───


@router.post("/{org_id}/sync-iris-customer", response_model=schemas.OrganizationRead)
def sync_iris_customer(
    org_id: int,
    force: bool = Query(default=False, description="Re-vincula mesmo se já mapeada"),
    repo: repository.OrganizationRepository = Depends(get_repo),
    current_user: models.AppUser = Depends(
        app_auth.require_permission(app_auth.Permission.ORG_MANAGE)
    ),
):
    """Cria (ou encontra) customer no IRIS DFIR e grava o mapping
    ``destination_customer_mappings`` (kind='iris').

    Ação OPCIONAL e auditável (não gateia a entrega de eventos — o envelope usa
    o ``Organization.id`` interno). Útil para vincular Organizations ao IRIS
    quando o stack de SOC usa IRIS DFIR como destino/IR tool.

    Idempotente:
      - Se já existe mapping IRIS → 409 (use ``?force=true`` pra re-vincular).
      - ``IrisClient.add_customer`` é idempotente no servidor: customer com o
        mesmo nome já existente é retornado em vez de duplicar.

    Falha modes:
      - 503 quando ``DFIR_IRIS_URL``/``DFIR_IRIS_API_KEY`` não configurados.
      - 502 quando IRIS API rejeita ou timeout.
    """
    from ..services.iris_client import (
        IrisApiError,
        IrisClient,
        IrisConfigurationError,
    )

    org = repo.get(org_id)
    if not org:
        raise ApiError(
            "org.not_found",
            404,
            messages={
                "pt": "Organização não encontrada.",
                "en": "Organization not found.",
                "es": "Organización no encontrada.",
            },
        )
    tenant.require_subtree_access(current_user, org_id)  # isolamento multi-tenant

    dcm_repo = repository.DestinationCustomerMappingRepository(repo.db)
    _existing_iris = dcm_repo.get_external_id(org.id, "iris")
    if _existing_iris and not force:
        raise ApiError(
            "org.iris_already_mapped",
            409,
            messages={
                "pt": (
                    "Organização já mapeada ao IRIS customer={customer_id}. "
                    "Use ?force=true pra re-vincular."
                ),
                "en": (
                    "Organization already mapped to IRIS customer={customer_id}. "
                    "Use ?force=true to re-link."
                ),
                "es": (
                    "La organización ya está vinculada al cliente IRIS={customer_id}. "
                    "Use ?force=true para volver a vincular."
                ),
            },
            params={"customer_id": _existing_iris},
        )

    iris = IrisClient()
    try:
        iris._ensure_configured()  # noqa: SLF001 — early check pra 503 limpo
    except IrisConfigurationError as exc:
        raise ApiError(
            "org.iris_not_configured",
            503,
            messages={
                "pt": "IRIS DFIR não configurado neste deployment: {error}",
                "en": "IRIS DFIR is not configured in this deployment: {error}",
                "es": "IRIS DFIR no está configurado en este despliegue: {error}",
            },
            params={"error": str(exc)},
        ) from exc

    try:
        payload = iris.add_customer(
            name=org.name,
            description=f"Linked from CentralOps Organization id={org.id}",
        )
        customer_id = iris.extract_customer_id(payload)
    except IrisApiError as exc:
        raise ApiError(
            "org.iris_sync_failed",
            502,
            messages={
                "pt": "Falha ao sincronizar customer no IRIS: {error}",
                "en": "Failed to sync customer in IRIS: {error}",
                "es": "Error al sincronizar el cliente en IRIS: {error}",
            },
            params={"error": str(exc)},
        ) from exc
    finally:
        iris.close()

    if customer_id is None:
        raise ApiError(
            "org.iris_invalid_customer_id",
            502,
            messages={
                "pt": "IRIS API não retornou customer_id válido (payload={payload}).",
                "en": "IRIS API did not return a valid customer_id (payload={payload}).",
                "es": "La API de IRIS no devolvió un customer_id válido (payload={payload}).",
            },
            params={"payload": repr(payload)},
        )

    dcm_repo.set(org.id, "iris", customer_id)
    logger.info(
        "iris_customer_linked",
        extra={
            "event": "organization.iris_customer_linked",
            "organization_id": org.id,
            "iris_customer_id": customer_id,
            "force": force,
        },
    )
    repo.db.refresh(org)
    return _serialize(org)


@router.get("/{org_id}/customer-mappings")
def list_customer_mappings(
    org_id: int,
    repo: repository.OrganizationRepository = Depends(get_repo),
    current_user: models.AppUser = Depends(
        app_auth.require_permission(app_auth.Permission.ORG_MANAGE)
    ),
):
    """Lista o mapeamento Organization → customer id externo por
    destino (IRIS/TheHive/SOAR). É a API de BORDA — um connector de IR/SOAR (ou
    o script Wazuh→IRIS) resolve o id externo a partir do ``Organization.id``
    interno aqui, em vez de o id externo viajar no hot path do envelope.
    """
    org = repo.get(org_id)
    if not org:
        raise ApiError(
            "org.not_found",
            404,
            messages={
                "pt": "Organização não encontrada.",
                "en": "Organization not found.",
                "es": "Organización no encontrada.",
            },
        )
    tenant.require_subtree_access(current_user, org_id)  # isolamento multi-tenant
    mappings = repository.DestinationCustomerMappingRepository(repo.db).list_for_org(org_id)
    return {
        "organization_id": org_id,
        "mappings": [
            {
                "destination_kind": m.destination_kind,
                "external_customer_id": m.external_customer_id,
                "updated_at": m.updated_at,
            }
            for m in mappings
        ],
    }


# ── Retenção ─────────────────────────────────────────────────


def _default_retention_read(org_id: int) -> schemas.OrganizationRetentionConfigRead:
    """Retorna defaults quando nenhum config existe para a organização."""
    return schemas.OrganizationRetentionConfigRead(
        organization_id=org_id,
        quarantine_retention_days=7,
        drift_retention_days=90,
        history_retention_days=30,
        search_result_retention_days=7,
        audit_log_retention_days=365,
    )


@router.get(
    "/{org_id}/retention",
    response_model=schemas.OrganizationRetentionConfigRead,
)
def get_retention_config(
    org_id: int,
    db: Session = Depends(database.get_session),
    current_user: models.AppUser = Depends(
        app_auth.require_permission(app_auth.Permission.INTEGRATION_READ)
    ),
) -> schemas.OrganizationRetentionConfigRead:
    """Retorna configuração de retenção da organização.

    Qualquer usuário com acesso à org pode ler.
    Defaults são retornados se ainda não foi configurado.
    """
    # Valida existência da org e acesso do tenant.
    repo = repository.OrganizationRepository(db)
    org = repo.get(org_id)
    if not org:
        raise ApiError(
            "org.not_found",
            404,
            messages={
                "pt": "Organização não encontrada.",
                "en": "Organization not found.",
                "es": "Organización no encontrada.",
            },
        )
    tenant.require_subtree_access(current_user, org_id)

    config = (
        db.query(models.OrganizationRetentionConfig)
        .filter(models.OrganizationRetentionConfig.organization_id == org_id)
        .first()
    )
    if config is None:
        return _default_retention_read(org_id)

    return schemas.OrganizationRetentionConfigRead(
        organization_id=config.organization_id,
        quarantine_retention_days=config.quarantine_retention_days,
        drift_retention_days=config.drift_retention_days,
        history_retention_days=config.history_retention_days,
        search_result_retention_days=config.search_result_retention_days,
        audit_log_retention_days=config.audit_log_retention_days,
        created_at=config.created_at,
        updated_at=config.updated_at,
    )


@router.put(
    "/{org_id}/retention",
    response_model=schemas.OrganizationRetentionConfigRead,
)
def update_retention_config(
    org_id: int,
    data: schemas.OrganizationRetentionConfigUpdate,
    db: Session = Depends(database.get_session),
    current_user: models.AppUser = Depends(
        app_auth.require_permission(app_auth.Permission.ORG_MANAGE)
    ),
) -> schemas.OrganizationRetentionConfigRead:
    """Atualiza configuração de retenção (ORG_MANAGE — admin only).

    Cria o registro se não existir. Registra audit log.
    """
    repo = repository.OrganizationRepository(db)
    org = repo.get(org_id)
    if not org:
        raise ApiError(
            "org.not_found",
            404,
            messages={
                "pt": "Organização não encontrada.",
                "en": "Organization not found.",
                "es": "Organización no encontrada.",
            },
        )

    # Upsert do config de retenção.
    config = (
        db.query(models.OrganizationRetentionConfig)
        .filter(models.OrganizationRetentionConfig.organization_id == org_id)
        .first()
    )
    if config is None:
        config = models.OrganizationRetentionConfig(organization_id=org_id)
        db.add(config)

    # Aplica apenas campos enviados.
    if data.quarantine_retention_days is not None:
        config.quarantine_retention_days = data.quarantine_retention_days
    if data.drift_retention_days is not None:
        config.drift_retention_days = data.drift_retention_days
    if data.history_retention_days is not None:
        config.history_retention_days = data.history_retention_days
    if data.search_result_retention_days is not None:
        config.search_result_retention_days = data.search_result_retention_days
    if data.audit_log_retention_days is not None:
        config.audit_log_retention_days = data.audit_log_retention_days

    config.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(config)

    # Audit log.
    audit = models.AuditLog(
        user_id=current_user.id,
        username=current_user.username,
        user_role=current_user.role,
        action="update_retention_config",
        endpoint=f"/api/organizations/{org_id}/retention",
        method="PUT",
        status_code=200,
        detail=(
            f"Configuração de retenção atualizada para org={org_id} "
            f"({org.slug})"
        ),
    )
    db.add(audit)
    db.commit()

    logger.info(
        "retention_config_updated org_id=%s by user=%s",
        org_id,
        current_user.username,
    )

    return schemas.OrganizationRetentionConfigRead(
        organization_id=config.organization_id,
        quarantine_retention_days=config.quarantine_retention_days,
        drift_retention_days=config.drift_retention_days,
        history_retention_days=config.history_retention_days,
        search_result_retention_days=config.search_result_retention_days,
        audit_log_retention_days=config.audit_log_retention_days,
        created_at=config.created_at,
        updated_at=config.updated_at,
    )


# ── Right-to-delete ──────────────────────────────────────────


@router.delete(
    "/{org_id}/data",
    response_model=schemas.DataDeletionJobRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def request_data_deletion(
    org_id: int,
    payload: schemas.DataDeletionRequest,
    db: Session = Depends(database.get_session),
    current_user: models.AppUser = Depends(
        app_auth.require_permission(app_auth.Permission.ORG_MANAGE)
    ),
) -> schemas.DataDeletionJobRead:
    """Solicita purge total de dados da organização (LGPD/GDPR).

    Operação irreversível. Cria DataDeletionJob com status='pending' e
    dispara task Celery na fila 'maintenance.high'. O texto de confirmação
    deve ser exatamente 'DELETAR {org_slug}' para prevenir acidentes.

    O Wazuh Indexer purge é best-effort: se indisponível, o job fica
    como 'partial' mas o restante do purge conclui normalmente.
    """
    repo = repository.OrganizationRepository(db)
    org = repo.get(org_id)
    if not org:
        raise ApiError(
            "org.not_found",
            404,
            messages={
                "pt": "Organização não encontrada.",
                "en": "Organization not found.",
                "es": "Organización no encontrada.",
            },
        )

    # Validação da confirmação obrigatória.
    expected = f"DELETAR {org.slug}"
    if payload.confirmation_text != expected:
        raise ApiError(
            "org.deletion_confirmation_mismatch",
            status.HTTP_400_BAD_REQUEST,
            messages={
                "pt": (
                    "confirmation_text deve ser exatamente '{expected}' "
                    "para confirmar a deleção da organização '{org_name}'."
                ),
                "en": (
                    "confirmation_text must be exactly '{expected}' "
                    "to confirm deletion of organization '{org_name}'."
                ),
                "es": (
                    "confirmation_text debe ser exactamente '{expected}' "
                    "para confirmar la eliminación de la organización '{org_name}'."
                ),
            },
            params={"expected": expected, "org_name": org.name},
        )

    # Marca organização como inativa ANTES de qualquer dispatch Celery.
    # Isso fecha a janela de race condition: qualquer tentativa concorrente de
    # criar Integration nesta org será rejeitada pelo IntegrationRepository.add()
    # com HTTP 409 após este commit.
    org.is_active = False
    org.updated_at = datetime.utcnow()

    # Cria o job de deleção.
    job = models.DataDeletionJob(
        organization_id=org_id,
        organization_slug=org.slug,
        requested_by_user_id=current_user.id,
        requested_by_username=current_user.username,
        reason=payload.reason,
        status="pending",
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    # Audit log antes de despachar — garante rastreabilidade mesmo se Celery falhar.
    audit = models.AuditLog(
        user_id=current_user.id,
        username=current_user.username,
        user_role=current_user.role,
        action="request_data_deletion",
        endpoint=f"/api/organizations/{org_id}/data",
        method="DELETE",
        status_code=202,
        detail=(
            f"Solicitação de purge para org={org_id} ({org.slug}). "
            f"Job={job.id}. Org marcada inactive. "
            f"Motivo: {payload.reason or 'não informado'}"
        ),
    )
    db.add(audit)
    db.commit()

    # Despacha task Celery de forma lazy (import local para evitar ciclo
    # de dependência entre routers e módulo de collectors no startup).
    try:
        from ..collectors.retention_tasks import execute_data_deletion  # type: ignore[attr-defined]

        celery_result = execute_data_deletion.apply_async(
            args=(job.id,),
            queue="maintenance.high",
        )
        job.celery_task_id = celery_result.id
        db.commit()
        db.refresh(job)
    except Exception as exc:  # pragma: no cover — Celery pode estar offline
        logger.warning(
            "execute_data_deletion dispatch falhou job_id=%s: %s",
            job.id,
            exc,
        )

    logger.info(
        "data_deletion_requested org_id=%s slug=%s job_id=%s by=%s",
        org_id,
        org.slug,
        job.id,
        current_user.username,
    )

    return schemas.DataDeletionJobRead(
        id=job.id,
        organization_id=job.organization_id,
        organization_slug=job.organization_slug,
        requested_by_username=job.requested_by_username,
        reason=job.reason,
        status=job.status,
        rows_deleted=job.rows_deleted,
        last_error=job.last_error,
        requested_at=job.requested_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        celery_task_id=job.celery_task_id,
    )
