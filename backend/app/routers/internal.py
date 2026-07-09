"""Service-to-service API for tenant context resolution.

**Authentication (dual-mode):**

  1. **Bearer com scope ``internal.tenant.read``** (preferido):
     - Header: ``Authorization: Bearer copsk_<...>``
     - Token deve ter scope ``internal.tenant.read``, ou ser de role
       que herda ele (operator/engineer/admin).

  2. **Shared key** (legacy fallback, deprecated):
     - Header: ``X-Internal-Api-Key: <CENTRALOPS_INTERNAL_API_KEY>``
     - Será removida em release futura. Migre para Bearer.

Se ambos vierem no mesmo request, **Bearer prevalece** — shared key é
ignorada (mesma lógica do ``get_current_user`` global).

NOT to be exposed to end users — registered without the cookie/session
``protected_api`` dependency in ``main.py``.

Endpoints resolvem um identificador de tenant no contexto per-tenant
completo necessário para chamadas downstream (external_id do vendor,
caminho de credenciais SIEM, etc.). A resposta é compacta e estável
para permitir caching curto (~60s) pelo consumidor.
"""

from __future__ import annotations

import hmac
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from ..api import schemas
from ..core import auth as app_auth
from ..core.auth import Permission
from ..core.config import settings
from ..core.errors import ApiError
from ..db import database, models, repository
from ..services.api_tokens import ApiTokenService, parse_scopes

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])


def _check_internal_api_key(
    x_internal_api_key: Optional[str] = Header(default=None, alias="X-Internal-Api-Key"),
) -> None:
    """Validate the legacy shared key.

    Returns silently on success. Raises 401 on mismatch and 503 if the
    deployment never set ``CENTRALOPS_INTERNAL_API_KEY`` (key flow disabled).
    """
    expected = (settings.CENTRALOPS_INTERNAL_API_KEY or "").strip()
    if not expected:
        # No key configured = endpoints disabled. Don't leak whether the
        # caller supplied a key — return a generic 503.
        raise ApiError(
            "internal.not_configured",
            status.HTTP_503_SERVICE_UNAVAILABLE,
            messages={
                "pt": "API interna não configurada neste deployment",
                "en": "Internal API not configured on this deployment",
                "es": "API interna no configurada en este despliegue",
            },
        )
    # Constant-time compare to avoid timing side-channel on key prefix.
    supplied = (x_internal_api_key or "").encode("utf-8")
    if not supplied or not hmac.compare_digest(supplied, expected.encode("utf-8")):
        # Constant message on both missing and mismatched key — don't help an
        # attacker enumerate.
        raise ApiError(
            "internal.invalid_api_key",
            status.HTTP_401_UNAUTHORIZED,
            messages={
                "pt": "X-Internal-Api-Key inválida ou ausente",
                "en": "Invalid or missing X-Internal-Api-Key",
                "es": "X-Internal-Api-Key inválida o ausente",
            },
        )


def _check_bearer_internal_scope(
    request: Request,
    db: Session,
) -> models.ApiToken | None:
    """Try to authenticate via Bearer token with ``internal.tenant.read`` scope.

    Returns the resolved ApiToken on success, or None if no Bearer header
    was provided. Raises 401 / 403 on present-but-invalid credentials —
    same fail-fast strategy as ``_resolve_bearer_user``.

    The owner (user or SA) must have ``Permission.INTERNAL_TENANT_READ``,
    AND the token's effective scopes must include it (token can restrict,
    not expand).
    """
    auth_header = request.headers.get("Authorization") or request.headers.get("authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return None

    raw_token = auth_header[len("Bearer "):].strip()
    from ..services.api_tokens import TOKEN_RAW_PREFIX
    if not raw_token.startswith(TOKEN_RAW_PREFIX):
        return None

    api_token = ApiTokenService(db).resolve_bearer(raw_token)
    if api_token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired API token",
            headers={"WWW-Authenticate": 'Bearer realm="centralops"'},
        )

    # Resolve owner role (user OR SA via shim).
    if api_token.service_account_id is not None:
        sa = api_token.service_account
        if sa is None or not sa.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Service account is inactive",
                headers={"WWW-Authenticate": 'Bearer realm="centralops"'},
            )
        owner_role = sa.role
    else:
        owner = api_token.user
        if owner is None or not owner.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User is inactive",
                headers={"WWW-Authenticate": 'Bearer realm="centralops"'},
            )
        owner_role = owner.role

    # Effective scopes: INTERSECTION(role, token.scopes).
    parsed = parse_scopes(api_token.scopes_json)
    token_scopes_param = parsed if parsed else None
    allowed = app_auth.effective_scopes(owner_role, token_scopes_param)
    if Permission.INTERNAL_TENANT_READ not in allowed:
        # Não expor o nome da permission constant no body — só log interno.
        logger.info(
            "internal: token lacks INTERNAL_TENANT_READ token_id=%s",
            api_token.id,
        )
        raise ApiError(
            "internal.insufficient_permissions",
            status.HTTP_403_FORBIDDEN,
            messages={
                "pt": "Permissões insuficientes",
                "en": "Insufficient permissions",
                "es": "Permisos insuficientes",
            },
        )

    return api_token


def _internal_auth_gate(
    request: Request,
    db: Session = Depends(database.get_session),
    x_internal_api_key: Optional[str] = Header(default=None, alias="X-Internal-Api-Key"),
) -> None:
    """Dual-mode auth gate: Bearer (preferred) OR shared key (deprecated).

    Bearer wins if both are present.
    """
    # 1. Try Bearer first — preferred path.
    api_token = _check_bearer_internal_scope(request, db)
    if api_token is not None:
        # Bearer authenticated successfully. Record usage best-effort.
        try:
            from ..services.audit import get_client_ip
            ApiTokenService(db).record_usage(api_token, ip_address=get_client_ip(request))
        except Exception as exc:  # pragma: no cover — best-effort
            logger.warning("Falha ao gravar uso de PAT id=%s no /internal: %s", api_token.id, exc)
        return

    # 2. Fall back to shared key (will raise 401/503 on miss).
    _check_internal_api_key(x_internal_api_key)


def _resolve_tenant(
    db: Session,
    organization: models.Organization,
) -> schemas.TenantResolution:
    """Build the ``TenantResolution`` payload from an Organization row.

    Pulls the most recent active Sophos child integration (Partner-managed
    or standalone tenant) so the caller has the canonical ``tenant_id``
    to target on downstream vendor calls.
    """
    sophos_block: Optional[schemas.TenantResolutionSophos] = None
    mcps_enabled: list[str] = []

    sophos_int = (
        db.query(models.Integration)
        .filter(
            models.Integration.organization_id == organization.id,
            models.Integration.platform == "sophos",
            models.Integration.is_active.is_(True),
        )
        # Prefer child integrations (Partner-managed); fall back to standalone tenants.
        .order_by(
            models.Integration.parent_integration_id.desc().nullslast()
            if hasattr(models.Integration.parent_integration_id, "desc")
            else models.Integration.parent_integration_id.desc(),
            models.Integration.id.asc(),
        )
        .first()
    )
    if sophos_int is not None:
        sophos_block = schemas.TenantResolutionSophos(
            tenant_external_id=sophos_int.external_id or sophos_int.tenant_id,
            region=sophos_int.region,
            partner_integration_id=sophos_int.parent_integration_id,
            child_integration_id=sophos_int.id,
            is_active=bool(sophos_int.is_active),
        )
        mcps_enabled.append("sophos")

    # Future-proof: surface enabled MCPs from any other active integrations.
    other_platforms = (
        db.query(models.Integration.platform)
        .filter(
            models.Integration.organization_id == organization.id,
            models.Integration.is_active.is_(True),
            models.Integration.platform != "sophos",
        )
        .distinct()
        .all()
    )
    for (platform,) in other_platforms:
        if platform and platform not in mcps_enabled:
            mcps_enabled.append(platform)

    # IRIS customer id vem do mapping (kind='iris'), não da coluna.
    _iris_ext = repository.DestinationCustomerMappingRepository(db).get_external_id(
        organization.id, "iris"
    )
    _iris_cid: int | None = (
        int(_iris_ext) if _iris_ext and _iris_ext.isdigit() else None
    )

    return schemas.TenantResolution(
        organization_id=organization.id,
        organization_slug=organization.slug,
        organization_name=organization.name,
        iris_customer_id=_iris_cid,
        is_active=bool(organization.is_active),
        sophos=sophos_block,
        mcps_enabled=sorted(mcps_enabled),
    )


@router.get(
    "/tenants/by-iris-customer/{iris_customer_id}",
    response_model=schemas.TenantResolution,
    dependencies=[Depends(_internal_auth_gate)],
)
def resolve_by_iris_customer(
    iris_customer_id: int,
    db: Session = Depends(database.get_session),
) -> schemas.TenantResolution:
    """Primary entry point — alert payload carries iris_customer_id."""
    org_repo = repository.OrganizationRepository(db)
    org = org_repo.find_by_iris_customer_id(iris_customer_id)
    if org is None:
        raise ApiError(
            "internal.tenant.not_found_by_iris_customer",
            status.HTTP_404_NOT_FOUND,
            messages={
                "pt": "Nenhuma organização mapeada para iris_customer_id={iris_customer_id}",
                "en": "No organization mapped to iris_customer_id={iris_customer_id}",
                "es": "Ninguna organización mapeada a iris_customer_id={iris_customer_id}",
            },
            params={"iris_customer_id": iris_customer_id},
        )
    return _resolve_tenant(db, org)


@router.get(
    "/tenants/by-sophos-tenant/{external_id}",
    response_model=schemas.TenantResolution,
    dependencies=[Depends(_internal_auth_gate)],
)
def resolve_by_sophos_tenant(
    external_id: str,
    db: Session = Depends(database.get_session),
) -> schemas.TenantResolution:
    """Reverse lookup used when a Sophos webhook arrives with a tenant UUID."""
    org_repo = repository.OrganizationRepository(db)
    org = org_repo.find_by_external_id("sophos", external_id)
    if org is None:
        raise ApiError(
            "internal.tenant.not_found_by_sophos_external_id",
            status.HTTP_404_NOT_FOUND,
            messages={
                "pt": "Nenhuma organização mapeada para sophos external_id={external_id}",
                "en": "No organization mapped to sophos external_id={external_id}",
                "es": "Ninguna organización mapeada a sophos external_id={external_id}",
            },
            params={"external_id": external_id},
        )
    return _resolve_tenant(db, org)


@router.get(
    "/tenants/{organization_id}",
    response_model=schemas.TenantResolution,
    dependencies=[Depends(_internal_auth_gate)],
)
def resolve_by_organization_id(
    organization_id: int,
    db: Session = Depends(database.get_session),
) -> schemas.TenantResolution:
    """Direct lookup by CentralOps Organization id — used when the caller
    already knows the local id (e.g. from a previous resolution it cached)."""
    org_repo = repository.OrganizationRepository(db)
    org = org_repo.get(organization_id)
    if org is None:
        raise ApiError(
            "internal.tenant.organization_not_found",
            status.HTTP_404_NOT_FOUND,
            messages={
                "pt": "Organização {organization_id} não encontrada",
                "en": "Organization {organization_id} not found",
                "es": "Organización {organization_id} no encontrada",
            },
            params={"organization_id": organization_id},
        )
    return _resolve_tenant(db, org)
