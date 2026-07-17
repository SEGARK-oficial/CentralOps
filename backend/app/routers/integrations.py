"""Integration CRUD + provider-powered operations router.

Handles creation, health checks, and provider-powered operations
for any registered provider (Sophos, Wazuh, etc.).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..api import schemas
from ..core import auth as app_auth
from ..core import ee_hooks
from ..core import tenant
from ..core.errors import ApiError
from ..core.rate_limiter import integration_rate_limiter
from ..db import database, models, repository
from ..providers.errors import (
    ProviderConfigurationError,
    ProviderError,
    ProviderNotFoundError,
)
from ..collectors.registry import (
    all_platforms,
    get_platform,
    get_provider,
    integration_capabilities,
    integration_has_capability,
)
from ..collectors.capabilities import (
    CAP_DISCOVER_CHILDREN,
    CAP_LICENSING_LIST,
    validate_capability,
)
from ..services import integration_secrets
from ..core.accept_version import resolve_api_version
from ..schemas.health import HealthResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/integrations", tags=["integrations"])


# ── Dependencies ──────────────────────────────────────────────────────

def get_repo(db: Session = Depends(database.get_session)) -> repository.IntegrationRepository:
    return repository.IntegrationRepository(db)


def get_org_repo(db: Session = Depends(database.get_session)) -> repository.OrganizationRepository:
    return repository.OrganizationRepository(db)


def get_health_repo(db: Session = Depends(database.get_session)) -> repository.IntegrationHealthRepository:
    return repository.IntegrationHealthRepository(db)


# ── Helpers ───────────────────────────────────────────────────────────

def _serialize(
    integration: models.Integration,
    current_user: models.AppUser,
) -> schemas.IntegrationRead:
    # Verifica permissão secret.read via ROLE_PERMISSIONS (sem levantar HTTP)
    effective_role = "viewer" if current_user.role == "user" else current_user.role
    can_view_secrets = app_auth.Permission.SECRET_READ in app_auth.ROLE_PERMISSIONS.get(effective_role, frozenset())

    result = schemas.IntegrationRead(
        id=integration.id,
        organization_id=integration.organization_id,
        organization_name=integration.organization.name if integration.organization else None,
        name=integration.name,
        platform=integration.platform,
        is_active=integration.is_active,
        is_authenticated=integration.is_authenticated,
        auth_status=integration.auth_status,
        last_checked_at=integration.last_checked_at,
        last_successful_check_at=integration.last_successful_check_at,
        last_error=integration.last_error,
        # Sophos Partner Mode metadata.
        kind=integration.kind or "tenant",
        parent_integration_id=integration.parent_integration_id,
        external_id=integration.external_id,
        id_type=integration.id_type,
        data_geography=integration.data_geography,
        last_tenant_sync_at=integration.last_tenant_sync_at,
        tenant_sync_status=integration.tenant_sync_status,
        auto_managed=bool(integration.auto_managed),
        created_at=integration.created_at,
        updated_at=integration.updated_at,
    )

    # instancia o provider UMA vez e reusa as
    # capabilities — antes ``get_provider`` era chamado 2× por linha (gate de
    # children_count + campo capabilities). Vendor sem provider rico ⇒ vazio.
    try:
        from ..collectors.registry import get_provider as _gp
        _caps = frozenset(_gp(integration).capabilities())
    except Exception:  # noqa: BLE001
        _caps = frozenset()
    result.capabilities = sorted(_caps)

    # Surface children_count só para parents MSSP — gateado pela capability
    # "discover:children" (sem ``if kind in``). Evita N+1 em
    # listagens regulares (tenants/genéricos não têm a capability).
    if CAP_DISCOVER_CHILDREN in _caps:
        try:
            from sqlalchemy import func as _func
            from ..db.database import SessionLocal as _Session

            with _Session() as _db:
                result.children_count = (
                    _db.query(_func.count(models.Integration.id))
                    .filter(
                        models.Integration.parent_integration_id == integration.id,
                        models.Integration.is_active.is_(True),
                    )
                    .scalar()
                    or 0
                )
        except Exception:  # noqa: BLE001
            result.children_count = None

    if integration.platform == "sophos":
        result.client_id = integration.client_id if can_view_secrets else None
        result.region = integration.region
        result.tenant_id = integration.tenant_id if can_view_secrets else None
    elif integration.platform == "wazuh":
        if can_view_secrets:
            result.manager_url = integration.manager_url
            result.indexer_url = integration.indexer_url
            # usernames vêm do store (sem fallback api_username legado).
            result.manager_api_username = integration_secrets.read_secret(integration, "manager_api_username")
            result.indexer_username = integration_secrets.read_secret(integration, "indexer_username")

        result.manager_api_password_configured = integration.manager_credentials_configured
        result.indexer_password_configured = integration.indexer_credentials_configured
        result.verify_ssl = integration.verify_ssl if can_view_secrets else None
    else:
        # Vendors genéricos (ninjaone, defender, …): expõe os campos comuns do
        # capability model. ``client_secret`` nunca sai em claro.
        if can_view_secrets:
            result.client_id = integration.client_id
            result.base_url = integration.base_url
            result.tenant_id = integration.tenant_id
            result.region = integration.region

    # ``result.capabilities`` já foi resolvido acima (provider instanciado 1×).
    return result


def _integration_last_error(details: Dict[str, Any]) -> str | None:
    messages: list[str] = []

    for key, value in details.items():
        if isinstance(value, dict):
            status_value = str(value.get("status", "")).strip().lower()
            if status_value in {"", "healthy", "not_configured"}:
                continue
            message = value.get("message") or value.get("error") or status_value
            messages.append(f"{key}: {message}")
        elif key == "message" and value:
            messages.append(str(value))

    if not messages:
        return None
    return "; ".join(messages)[:1000]


def _record_health_state(
    db: Session,
    integration_id: int,
    result,
) -> None:
    now = datetime.utcnow()
    last_error = _integration_last_error(result.details)
    update_values: Dict[str, Any] = {
        "auth_status": result.status,
        "last_checked_at": now,
        "last_error": last_error,
        "updated_at": now,
    }
    if result.status in {"healthy", "degraded"}:
        update_values["last_successful_check_at"] = now
    db.query(models.Integration).filter(models.Integration.id == integration_id).update(update_values)
    db.commit()


def _get_integration_or_404(
    repo: repository.IntegrationRepository,
    integration_id: int,
) -> models.Integration:
    integration = repo.get(integration_id)
    if not integration:
        raise ApiError(
            "integration.not_found",
            404,
            messages={
                "pt": "Integração não encontrada.",
                "en": "Integration not found.",
                "es": "Integración no encontrada.",
            },
        )
    return integration


def _ensure_integration_access(
    current_user: models.AppUser,
    integration: models.Integration,
    *,
    require_active: bool = False,
) -> None:
    tenant.require_subtree_access(current_user, integration.organization_id)
    if require_active and not integration.is_active:
        raise ApiError(
            "integration.inactive",
            409,
            messages={
                "pt": "A integração está inativa.",
                "en": "Integration is inactive.",
                "es": "La integración está inactiva.",
            },
        )


def _ensure_capability(provider, capability: str) -> None:
    validate_capability(capability)  # typo → ValueError, não fail-open
    if capability in provider.capabilities():
        return
    raise ApiError(
        "integration.capability_unsupported",
        409,
        messages={
            "pt": "A integração não suporta {capability}.",
            "en": "Integration does not support {capability}.",
            "es": "La integración no soporta {capability}.",
        },
        params={"capability": capability},
    )


def _provider_error_payload(exc: ProviderError, *, integration_id: int) -> Dict[str, Any]:
    return {"error": exc.to_payload(integration_id=integration_id)}


def _safe_provider_error(integration: models.Integration, exc: Exception) -> str:
    """Retorna mensagem genérica para o usuário e loga o erro completo.

    Evita vazar IPs internos, hostnames, certificate SANs ou qualquer detalhe
    técnico de infraestrutura que aparece nos repr/str de exceções de provider.
    """
    logger.warning(
        "provider error integration_id=%s name=%r: %s",
        integration.id,
        integration.name,
        exc,
        exc_info=True,
    )
    return f"{integration.name}: falha ao consultar provedor"


def _provider_error_response(
    exc: ProviderError,
    *,
    integration: models.Integration,
    operation: str,
) -> JSONResponse:
    logger.warning(
        "Provider operation failed integration=%s provider=%s operation=%s code=%s details=%s",
        integration.id,
        integration.platform,
        operation,
        exc.code,
        exc.details,
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=_provider_error_payload(exc, integration_id=integration.id),
    )


def _collection_timestamps(
    db: Session,
    integration_id: int,
) -> tuple[Any, Any]:
    """Return (last_collection_at, last_success_at) from CollectionState rows.

    Takes the maximum across all streams for the integration.
    Returns (None, None) if no state rows exist yet.
    """
    from ..db.repository import CollectionStateRepository

    rows = CollectionStateRepository(db).list_for_integration(integration_id)
    if not rows:
        return None, None

    last_collection_at = max(
        (row.last_attempt_at for row in rows if row.last_attempt_at),
        default=None,
    )
    last_success_at = max(
        (row.last_success_at for row in rows if row.last_success_at),
        default=None,
    )
    return last_collection_at, last_success_at


def _component_status(details: Dict[str, Any], key: str) -> str | None:
    component = details.get(key)
    if isinstance(component, dict):
        status = component.get("status")
        if status not in (None, ""):
            return str(status)
    return None


def _serialize_health_result(result) -> Dict[str, Any]:
    details = result.details if isinstance(result.details, dict) else {}
    return {
        "status": result.status,
        "details": details,
        "manager_status": _component_status(details, "manager"),
        "indexer_status": _component_status(details, "indexer"),
    }


# ── Collector hooks (fire-and-forget) ────────────────────────────────

def _trigger_initial_collection(integration_id: int, platform: str) -> None:
    """Enfileira tasks de coleta one-shot para cada stream da plataforma.

    Usa countdown=5s para garantir que o DB já commitou antes da task rodar.
    Falha silenciosa: se Celery/Redis estiver down, loga error e segue —
    a integração continua existindo; a coleta ocorrerá no próximo tick do Beat.
    """
    try:
        from ..collectors.queues import Q_PRIORITY, Q_BULK
        from ..collectors.registry import iter_for_platform
        from ..collectors.tasks import (
            collect_vendor_logs_priority,
            collect_vendor_logs_bulk,
        )

        _TASK_BY_QUEUE = {
            Q_PRIORITY: collect_vendor_logs_priority,
            Q_BULK: collect_vendor_logs_bulk,
        }

        dispatched = 0
        for reg in iter_for_platform(platform):
            task_fn = _TASK_BY_QUEUE.get(reg.queue, collect_vendor_logs_bulk)
            task_fn.apply_async(
                args=[integration_id, reg.stream],
                countdown=5,
                queue=reg.queue,
            )
            dispatched += 1
            logger.info(
                "integration.on_create: task one-shot enfileirada "
                "integration_id=%s stream=%s queue=%s",
                integration_id,
                reg.stream,
                reg.queue,
            )

        if dispatched == 0:
            logger.warning(
                "integration.on_create: nenhum stream para platform=%r "
                "(integration_id=%s) — verifique o registry",
                platform,
                integration_id,
            )
    except Exception:
        logger.error(
            "integration.on_create: falha ao enfileirar coleta inicial "
            "para integration_id=%s — coleta ocorrerá no próximo tick do Beat",
            integration_id,
            exc_info=True,
        )


def _register_in_beat(integration_id: int) -> None:
    """Registra a integração no RedBeat scheduler. Fire-and-forget."""
    try:
        from ..collectors.scheduler import register_integration_in_beat
        register_integration_in_beat(integration_id)
    except Exception:
        logger.error(
            "integration.on_create: falha ao registrar integration_id=%s "
            "no RedBeat — será reconciliado no próximo boot do Beat",
            integration_id,
            exc_info=True,
        )


def _deregister_from_beat(integration_id: int) -> None:
    """Remove a integração do RedBeat scheduler. Fire-and-forget."""
    try:
        from ..collectors.scheduler import deregister_integration_from_beat
        deregister_integration_from_beat(integration_id)
    except Exception:
        logger.error(
            "integration.on_delete: falha ao remover integration_id=%s "
            "do RedBeat — entry pode persistir até o próximo boot do Beat",
            integration_id,
            exc_info=True,
        )


# ── CRUD endpoints ────────────────────────────────────────────────────

@router.get("/platforms")
def list_platforms():
    """List all supported integration platforms (catálogo plugin-driven).

    Lê o registry de plataformas — não uma lista hardcoded. Para o
    catálogo rico (display_name/category/auth_fields) use ``GET /providers/platforms``."""
    return {"platforms": [p.platform for p in all_platforms()]}


# Colunas reais do model Integration — a atribuição genérica de credencial só
# seta atributos que são colunas de fato (evita perder silenciosamente um
# auth_field sem coluna dedicada; esses migram p/ integration_credentials).
_INTEGRATION_COLUMNS = {c.name for c in models.Integration.__table__.columns}


def _assign_credentials(
    integration: models.Integration,
    data: schemas.IntegrationCreate,
    plat_reg,
    kind: str,
) -> None:
    """Mapeia os auth_fields declarados pelo vendor → store/colunas do Integration.

    Plugin-driven: a ``PlatformRegistration.auth_fields`` É o schema de
    credencial. ``type=secret`` vai para o store ``integration_credentials`` via
    ``write_secret`` (cifra Vault-aware); os demais (``key`` casa 1:1 com a coluna)
    são gravados na coluna (``bool`` coagido). SEM branch por plataforma — vendor
    novo = registrar auth_fields. Obrigatório ausente ⇒ 400."""
    if kind != "tenant" and getattr(data, "region", None):
        raise ApiError(
            "integration.region_not_allowed",
            400,
            messages={
                "pt": "region não deve ser informado para kind=partner|organization (descoberto por filho).",
                "en": "region must not be supplied for kind=partner|organization (discovered per child).",
                "es": "region no debe informarse para kind=partner|organization (descubierto por hijo).",
            },
        )
    # resolve o valor pela key declarada — campo fixo do
    # schema OU ``model_extra`` (chave inédita postada por vendor plugin-driven,
    # ex.: api_token/secret_access_key). Sem isto um vendor com key nova recebia
    # None → required → 400 permanente (okta/cloudtrail não eram criáveis).
    _extra = getattr(data, "model_extra", None) or {}

    def _field_value(key: str):
        v = getattr(data, key, None)
        return _extra.get(key) if v is None else v

    missing: list[str] = []
    config_extra: dict = {}  # campos não-coluna/não-secret → config_json
    for field in plat_reg.auth_fields:
        value = _field_value(field.key)
        if isinstance(value, str):
            value = value.strip() or None
        if field.required and value in (None, ""):
            missing.append(field.label)
            continue
        if value is None:
            continue
        if field.type == "secret":
            # TODO segredo vai para o store vendor-neutro
            # ``integration_credentials`` (funciona até p/ creds exóticas sem coluna
            # dedicada). sophos/wazuh migraram — não há mais coluna legada.
            integration_secrets.write_secret(integration, field.key, str(value))
        elif field.key in _INTEGRATION_COLUMNS:
            if field.type == "bool":
                setattr(integration, field.key, bool(value))
            else:
                setattr(integration, field.key, value)
        else:
            # config não-secreta sem coluna dedicada (ex.: lake
            # layout/prefix/source) — antes era descartada silenciosamente.
            config_extra[field.key] = bool(value) if field.type == "bool" else value
    if config_extra:
        import json as _json
        integration.config_json = _json.dumps(config_extra)
    if missing:
        raise ApiError(
            "integration.missing_required_fields",
            400,
            messages={
                "pt": "{platform} exige: {fields}.",
                "en": "{platform} requires: {fields}.",
                "es": "{platform} requiere: {fields}.",
            },
            params={"platform": plat_reg.display_name, "fields": ", ".join(missing)},
        )
    # Wazuh: Manager parcial proibido no create. Se manager_url foi fornecido,
    # manager_api_username + manager_api_password também devem estar presentes.
    # A regra é específica do wazuh — não interfere em outros vendors.
    if plat_reg.platform == "wazuh":
        manager_url_val = _field_value("manager_url")
        if isinstance(manager_url_val, str):
            manager_url_val = manager_url_val.strip() or None
        if manager_url_val:
            has_mgr_user = bool(_field_value("manager_api_username"))
            has_mgr_pass = bool(_field_value("manager_api_password"))
            if not has_mgr_user or not has_mgr_pass:
                raise ApiError(
                    "integration.wazuh_manager_incomplete",
                    400,
                    messages={
                        "pt": "manager_url do Wazuh exige manager_api_username e manager_api_password.",
                        "en": "Wazuh manager_url requires manager_api_username and manager_api_password.",
                        "es": "manager_url de Wazuh requiere manager_api_username y manager_api_password.",
                    },
                )
    # ``id_type`` espelha o kind (compat legada Sophos — metadata benigna).
    integration.id_type = kind


@router.post("/", response_model=schemas.IntegrationRead)
def create_integration(
    data: schemas.IntegrationCreate,
    db: Session = Depends(database.get_session),
    repo: repository.IntegrationRepository = Depends(get_repo),
    org_repo: repository.OrganizationRepository = Depends(get_org_repo),
    current_user: models.AppUser = Depends(app_auth.require_permission(app_auth.Permission.INTEGRATION_WRITE)),
):
    # rate limit por usuário — 30 POST/min
    retry_after = integration_rate_limiter.check_create(current_user.id)
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail="Too many integration creation requests. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    org = org_repo.get(data.organization_id)
    if not org:
        raise ApiError(
            "organization.not_found",
            404,
            messages={
                "pt": "Organização não encontrada.",
                "en": "Organization not found.",
                "es": "Organización no encontrada.",
            },
        )
    if not org.is_active:
        raise ApiError(
            "organization.inactive",
            409,
            messages={
                "pt": "A organização está inativa.",
                "en": "Organization is inactive.",
                "es": "La organización está inactiva.",
            },
        )

    # limite por organização — impede proliferação irrestrita de integrações
    from ..core.config import settings as _settings
    active_count = repo.count_active(data.organization_id)
    if active_count >= _settings.MAX_INTEGRATIONS_PER_ORG:
        raise ApiError(
            "integration.org_limit_reached",
            400,
            messages={
                "pt": (
                    "A organização atingiu o máximo de {max} integrações ativas. "
                    "Desative integrações não usadas antes de criar novas."
                ),
                "en": (
                    "Organization has reached the maximum of {max} active integrations. "
                    "Deactivate unused integrations before creating new ones."
                ),
                "es": (
                    "La organización alcanzó el máximo de {max} integraciones activas. "
                    "Desactive integraciones no usadas antes de crear nuevas."
                ),
            },
            params={"max": _settings.MAX_INTEGRATIONS_PER_ORG},
        )

    plat_reg = get_platform(data.platform)
    if plat_reg is None:
        raise ApiError(
            "integration.platform_unsupported",
            400,
            messages={
                "pt": "Plataforma não suportada: {platform}.",
                "en": "Unsupported platform: {platform}.",
                "es": "Plataforma no soportada: {platform}.",
            },
            params={"platform": data.platform},
        )

    # variantes-card (ex.: "sophos_partner") mapeiam para a
    # plataforma-base + kind. ``Integration.platform`` é persistida na BASE
    # ("sophos") — collectors/providers/downstream continuam inalterados; o
    # ``platform`` da registration existe só no catálogo/galeria.
    effective_platform = plat_reg.base_platform or data.platform

    # Resolve effective kind. Variante (partner/organization) pina o kind; senão
    # ``data.kind`` (default "tenant"). Partner/Organization destravam a
    # auto-descoberta — só p/ plataformas que DECLARAM ``discover:children``
    # (não mais keyed por 'sophos': CrowdStrike Flight Control / Defender
    # Lighthouse reusam o mesmo fluxo MSSP só registrando essa capability).
    requested_kind = plat_reg.variant or (data.kind or "tenant")
    if requested_kind not in ("partner", "organization", "tenant"):
        raise ApiError(
            "integration.kind_unsupported",
            400,
            messages={
                "pt": "Kind não suportado: {kind}.",
                "en": "Unsupported kind: {kind}.",
                "es": "Kind no soportado: {kind}.",
            },
            params={"kind": requested_kind},
        )
    if requested_kind != "tenant" and CAP_DISCOVER_CHILDREN not in plat_reg.capabilities:
        raise ApiError(
            "integration.kind_requires_discovery",
            400,
            messages={
                "pt": "kind=partner|organization exige uma plataforma que suporte descoberta de filhos.",
                "en": "kind=partner|organization requires a platform that supports child discovery.",
                "es": "kind=partner|organization requiere una plataforma que soporte el descubrimiento de hijos.",
            },
        )

    integration = models.Integration(
        organization_id=data.organization_id,
        name=data.name,
        platform=effective_platform,
        kind=requested_kind,
    )

    # Atribuição genérica de credenciais (plugin-driven) — sem branch por vendor.
    _assign_credentials(integration, data, plat_reg, requested_kind)

    integration = repo.add(integration)

    # ── Hook on-create do provider (capability-gated) ───────────
    # Partner/Organization disparam a descoberta assíncrona de tenants via
    # ``provider.on_created()`` (substitui o branch sophos-específico). Eles NÃO
    # rodam test_connection síncrono (pode demorar com muitos tenants) — a UI
    # acompanha por /sync-status. Filhos + RedBeat saem da task de sync.
    # gate PURO por capability — sem o AND kind-literal
    # que excluía um MSSP genérico (ex.: CrowdStrike Flight Control) que declara
    # discover:children sob outro kind (era fail-silent: on_created nunca disparava).
    if CAP_DISCOVER_CHILDREN in plat_reg.capabilities:
        try:
            provider = get_provider(integration)
            provider.on_created()
            logger.info(
                "integration.on_create: on_created() disparado integration_id=%s kind=%s",
                integration.id, requested_kind,
            )
        except ee_hooks.LicenseRequiredError as exc:
            # EE presente mas a licença não concede a feature: a CRIAÇÃO segue
            # permitida (teaser by-design, paridade com Community), só a descoberta
            # assíncrona é recusada. Persiste o sinal p/ a UI (polling /sync-status).
            logger.warning(
                "integration.on_create: descoberta recusada por licença (feature=%s) "
                "integration_id=%s", exc.feature, integration.id,
            )
            _persist_refused_sync_status(repo, integration, "license_required")
        except Exception:
            logger.error(
                "integration.on_create: on_created() falhou integration_id=%s — "
                "use POST /integrations/{id}/sync-tenants para retry",
                integration.id, exc_info=True,
            )
        return _serialize(integration, current_user)

    # ── Validação imediata de credenciais (best-effort, plugin-driven) ────
    # Plataformas com BaseProvider rico validam creds na criação (Sophos também
    # descobre region/tenant aqui). Vendor só-coleta (sem provider_factory) é
    # ignorado — ``get_provider`` levanta ValueError e seguimos sem bloquear.
    try:
        provider = get_provider(integration)
        result = provider.test_connection()
        _record_health_state(db, integration.id, result)
        repository.IntegrationHealthRepository(db).add(models.IntegrationHealthCheck(
            integration_id=integration.id,
            status=result.status,
            details=json.dumps(result.details),
        ))
        if result.status == "healthy":
            db.refresh(integration)
    except ValueError:
        pass  # plataforma só-catálogo/coleta — sem provider rico p/ validar
    except Exception as exc:
        logger.warning("Auto-connect falhou para integration %s: %s", integration.id, exc)

    # ── Hook on-create: primeira coleta + registro no RedBeat ─────────
    # Fire-and-forget: falhas aqui não devem bloquear a resposta 201.
    try:
        # coleta usa a plataforma BASE (effective_platform),
        # não a variante (ex.: sophos_partner) — collector é registrado pela base.
        _trigger_initial_collection(integration.id, effective_platform)
    except Exception:
        logger.error(
            "integration.on_create: _trigger_initial_collection levantou inesperadamente "
            "integration_id=%s — ignorado",
            integration.id,
            exc_info=True,
        )
    try:
        _register_in_beat(integration.id)
    except Exception:
        logger.error(
            "integration.on_create: _register_in_beat levantou inesperadamente "
            "integration_id=%s — ignorado",
            integration.id,
            exc_info=True,
        )

    return _serialize(integration, current_user)


# ── Sophos Partner Mode endpoints ────────────────────────────────────


def _sync_lock_active(integration_id: int) -> bool:
    """Probe the Redis lock used by ``sync_sophos_partner``. Best-effort:
    returns False on any Redis failure so the UI can keep polling."""
    try:
        import redis as _redis_sync

        from ..core.config import settings as _settings

        client = _redis_sync.Redis.from_url(
            _settings.REDIS_URL or "redis://localhost:6379/0",
            decode_responses=True,
        )
        try:
            return bool(client.exists(f"sync:partner:{integration_id}"))
        finally:
            client.close()
    except Exception:  # noqa: BLE001
        return False


def _persist_refused_sync_status(
    repo: repository.IntegrationRepository,
    integration: models.Integration,
    status_value: str,
) -> None:
    """Persiste em ``tenant_sync_status`` o MOTIVO de um sync recusado
    (``enterprise_required`` | ``license_required``) — sem tocar
    ``last_tenant_sync_at`` (nenhum sync rodou). Best-effort: uma falha de commit
    não derruba a resposta (o body já carrega o mesmo sinal)."""
    try:
        integration.tenant_sync_status = status_value
        integration.updated_at = datetime.utcnow()
        repo.db.commit()
    except Exception:  # noqa: BLE001
        repo.db.rollback()
        logger.warning(
            "sync-tenants: falha ao persistir tenant_sync_status=%s integration_id=%s",
            status_value, integration.id, exc_info=True,
        )


@router.post("/{integration_id}/sync-tenants", response_model=schemas.PartnerSyncResult)
def sync_partner_tenants(
    integration_id: int,
    repo: repository.IntegrationRepository = Depends(get_repo),
    current_user: models.AppUser = Depends(
        app_auth.require_permission(app_auth.Permission.INTEGRATION_WRITE)
    ),
) -> schemas.PartnerSyncResult:
    """Force-sync tenants for a Partner / Organization integration.

    Returns ``202`` semantics via ``PartnerSyncResult`` with ``status="ok"``
    when the task is dispatched. Honours the per-integration lock — concurrent
    callers receive ``429``.
    """
    integration = _get_integration_or_404(repo, integration_id)
    _ensure_integration_access(current_user, integration, require_active=True)
    # gate por capability discover:children (não kind).
    if not integration_has_capability(integration, CAP_DISCOVER_CHILDREN):
        # Não expor o enum interno de tipos no body — só registrar no log.
        logger.info(
            "sync-tenants: rejected integration_id=%s kind=%s",
            integration_id, integration.kind,
        )
        raise ApiError(
            "integration.sync_not_supported",
            409,
            messages={
                "pt": "sync-tenants não é suportado para este tipo de integração.",
                "en": "sync-tenants not supported for this integration type.",
                "es": "sync-tenants no es compatible con este tipo de integración.",
            },
        )
    # Edição: sem partner_sync_dispatcher registrado (Community), provider.on_created()
    # é no-op — não reportar um "ok" enganoso. Sinaliza enterprise_required (paridade
    # com select_tenants), sem 500 e sem disparar nada. A trava nem é relevante aqui.
    if ee_hooks.get_partner_sync_dispatcher() is None:
        logger.info(
            "sync-tenants: enterprise feature não disponível (Community) integration_id=%s",
            integration_id,
        )
        # Persiste o motivo da recusa em tenant_sync_status — o polling de
        # /sync-status deixa de devolver null para sempre e a UI ganha o sinal.
        _persist_refused_sync_status(repo, integration, "enterprise_required")
        return schemas.PartnerSyncResult(
            integration_id=integration_id,
            started_at=datetime.utcnow(),
            status="enterprise_required",
        )
    if _sync_lock_active(integration_id):
        raise ApiError(
            "integration.sync_in_progress",
            429,
            messages={
                "pt": "Já há uma sincronização de parceiro em andamento para esta integração.",
                "en": "A partner sync is already in progress for this integration.",
                "es": "Ya hay una sincronización de partner en curso para esta integración.",
            },
        )
    try:
        from ..core.config import settings as _settings

        # dispatch VENDOR-NEUTRO via hook do provider —
        # provider.on_created() dispara a descoberta de children (Sophos despacha
        # sync_sophos_partner; outro MSSP faz o seu) sem acoplar o router ao task.
        provider = get_provider(integration)
        provider.on_created()
    except ee_hooks.LicenseRequiredError as exc:
        # EE presente, mas a licença ativa não concede a feature (multi_tenant):
        # o dispatcher do EE recusou. Sinal DISTINTO de enterprise_required
        # (artefato ausente) — sem 500, nada foi disparado. Nunca logar o token.
        logger.warning(
            "sync-tenants: recusado por licença (feature=%s) integration_id=%s",
            exc.feature, integration_id,
        )
        _persist_refused_sync_status(repo, integration, "license_required")
        return schemas.PartnerSyncResult(
            integration_id=integration_id,
            started_at=datetime.utcnow(),
            status="license_required",
        )
    except Exception as exc:  # noqa: BLE001
        # Causa típica: Celery broker (Redis) inacessível ou
        # ``settings.CELERY_BROKER_URL`` apontando pra host errado.
        # Mensagens detalhadas (tipo de exceção, URL do broker) ficam SÓ
        # no log do servidor — a imagem é pública, não queremos confirmar
        # stack interno ou expor endpoint do broker via body 503.
        broker = _settings.CELERY_BROKER_URL or _settings.REDIS_URL or "<unset>"
        logger.exception(
            "sync-tenants: falha ao despachar sync_sophos_partner "
            "integration_id=%s broker_set=%s exc_type=%s",
            integration_id,
            "yes" if broker != "<unset>" else "no",
            type(exc).__name__,
        )
        raise ApiError(
            "integration.sync_dispatch_failed",
            503,
            messages={
                "pt": "Falha ao despachar a sincronização de parceiro; verifique os logs do servidor.",
                "en": "Partner sync dispatch failed; check server logs.",
                "es": "Fallo al despachar la sincronización de partner; verifique los logs del servidor.",
            },
        ) from exc
    return schemas.PartnerSyncResult(
        integration_id=integration_id,
        started_at=datetime.utcnow(),
        status="ok",
    )


@router.get("/{integration_id}/sync-status", response_model=schemas.PartnerSyncStatus)
def get_partner_sync_status(
    integration_id: int,
    repo: repository.IntegrationRepository = Depends(get_repo),
    current_user: models.AppUser = Depends(app_auth.require_authenticated_user),
) -> schemas.PartnerSyncStatus:
    integration = _get_integration_or_404(repo, integration_id)
    _ensure_integration_access(current_user, integration)
    return schemas.PartnerSyncStatus(
        integration_id=integration_id,
        tenant_sync_status=integration.tenant_sync_status,
        last_tenant_sync_at=integration.last_tenant_sync_at,
        lock_active=_sync_lock_active(integration_id),
    )


@router.get(
    "/{integration_id}/discovered-tenants",
    response_model=List[schemas.DiscoveredTenant],
)
def list_discovered_tenants(
    integration_id: int,
    include_inactive: bool = Query(default=False),
    repo: repository.IntegrationRepository = Depends(get_repo),
    current_user: models.AppUser = Depends(app_auth.require_authenticated_user),
) -> List[schemas.DiscoveredTenant]:
    """Return the children Integrations of a Partner, presented as
    ``DiscoveredTenant`` rows so the frontend has a uniform shape across
    "fresh from Sophos" and "already linked" states.
    """
    integration = _get_integration_or_404(repo, integration_id)
    _ensure_integration_access(current_user, integration)
    # gate por capability discover:children (não kind).
    if not integration_has_capability(integration, CAP_DISCOVER_CHILDREN):
        raise ApiError(
            "integration.not_mssp_parent.discovered_tenants",
            409,
            messages={
                "pt": "discovered-tenants só é válido para integrações-pai MSSP (kind={kind!r}).",
                "en": "discovered-tenants is only valid for MSSP parent integrations (got kind={kind!r}).",
                "es": "discovered-tenants solo es válido para integraciones padre MSSP (kind={kind!r}).",
            },
            params={"kind": integration.kind},
        )
    children = repo.list_children(integration_id, include_inactive=include_inactive)
    return [
        schemas.DiscoveredTenant(
            external_id=child.external_id or child.tenant_id or "",
            name=child.name,
            region=child.region,
            data_geography=child.data_geography,
            api_host=None,
            status="linked" if child.is_active else "stale",
            linked_organization_id=child.organization_id,
            linked_integration_id=child.id,
        )
        for child in children
    ]


# ── Sophos Partner — tenant selection (opt-in per tenant) ─────


def _sophos_discover_cache_key(integration_id: int) -> str:
    return f"discover:partner:{integration_id}"


def _sophos_get_redis():
    """Sync Redis client used pelo cache de 5min do discover. None = sem cache."""
    try:
        import redis as _redis_sync

        from ..core.config import settings as _settings

        return _redis_sync.Redis.from_url(
            _settings.REDIS_URL or "redis://localhost:6379/0",
            decode_responses=True,
        )
    except Exception:  # noqa: BLE001
        return None


def _ensure_partner(integration: models.Integration) -> None:
    # gate por capability discover:children (não kind).
    if not integration_has_capability(integration, CAP_DISCOVER_CHILDREN):
        raise ApiError(
            "integration.not_mssp_parent",
            409,
            messages={
                "pt": "endpoint só é válido para integrações-pai MSSP (kind={kind!r}).",
                "en": "endpoint is only valid for MSSP parent integrations (got kind={kind!r}).",
                "es": "el endpoint solo es válido para integraciones padre MSSP (kind={kind!r}).",
            },
            params={"kind": integration.kind},
        )


def _serialize_sophos_tenant_item(
    selection: models.IntegrationTenantSelection,
    *,
    discovered_ids: set[str] | None,
    children_by_external_id: dict[str, models.Integration],
    decided_by_user: models.AppUser | None,
) -> schemas.SophosTenantListItem:
    """Calcula ``selection_state`` exposto na UI (incl. ``stale``)."""
    state = selection.state
    # ``stale`` quando aprovado localmente mas sumiu do último discover.
    if (
        state == "approved"
        and discovered_ids is not None
        and selection.external_id not in discovered_ids
    ):
        state = "stale"
    child = children_by_external_id.get(selection.external_id)
    return schemas.SophosTenantListItem(
        external_id=selection.external_id,
        name=selection.name_snapshot,
        region=selection.region_snapshot,
        data_geography=selection.data_geography_snapshot,
        api_host=selection.api_host_snapshot,
        selection_state=state,
        child_integration_id=child.id if child is not None else None,
        decided_by_user_id=selection.decided_by_user_id,
        decided_by_username=(
            decided_by_user.username if decided_by_user is not None else None
        ),
        decided_at=selection.decided_at,
        last_seen_at=selection.last_seen_at,
    )


def _sophos_tenant_payload_normalize(tenant_dto: dict) -> dict | None:
    """Extrai a tupla canônica do payload do Sophos. Retorna None pra entradas inválidas."""
    external_id = (tenant_dto.get("id") or tenant_dto.get("external_id") or "").strip()
    if not external_id:
        return None
    raw_name = (tenant_dto.get("name") or external_id).strip() or external_id
    api_host_url = (tenant_dto.get("apiHost") or "").strip()
    api_host: str | None = None
    region_slug: str | None = None
    if api_host_url:
        host = api_host_url
        for prefix in ("https://", "http://"):
            if host.startswith(prefix):
                host = host[len(prefix):]
        api_host = host.strip("/") or None
        if api_host and api_host.startswith("api-") and api_host.endswith(".central.sophos.com"):
            region_slug = api_host[len("api-"):-len(".central.sophos.com")]
    if not region_slug:
        region_slug = (tenant_dto.get("dataRegion") or tenant_dto.get("region") or "").strip() or None
    geography = (tenant_dto.get("dataGeography") or "").strip() or None
    return {
        "external_id": external_id,
        "name": raw_name,
        "region": region_slug,
        "data_geography": geography,
        "api_host": api_host,
    }


@router.get(
    "/{integration_id}/sophos-tenants",
    response_model=schemas.SophosTenantListResponse,
)
def list_sophos_tenants(
    integration_id: int,
    refresh: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=10, ge=1, le=1000),
    state: str | None = Query(
        default=None,
        description="Filter por estado: pending|approved|excluded|stale|all",
    ),
    search: str | None = Query(default=None, max_length=200),
    geography: str | None = Query(default=None, max_length=20),
    repo: repository.IntegrationRepository = Depends(get_repo),
    db: Session = Depends(database.get_session),
    current_user: models.AppUser = Depends(
        app_auth.require_permission(app_auth.Permission.INTEGRATION_READ)
    ),
) -> schemas.SophosTenantListResponse:
    """List Sophos Partner tenants merged com seleções e children locais.

    ``refresh=False`` (default): retorna do snapshot local (rápido,
    serve da última ``sync_sophos_partner`` rodada). ``refresh=True``: força
    nova chamada ``provider.discover_tenants()`` (10-30s pra centenas de tenants),
    atualiza snapshots em ``integration_tenant_selections`` e retorna fresh.
    Cache Redis 5min em ``discover:partner:{id}`` evita abuse.
    """
    integration = _get_integration_or_404(repo, integration_id)
    _ensure_integration_access(current_user, integration)
    _ensure_partner(integration)

    sel_repo = repository.IntegrationTenantSelectionRepository(db)

    # Discovered IDs do último refresh (ou cache, ou desta chamada).
    discovered_ids: set[str] | None = None
    fetched_live = False

    if refresh:
        # Cache 5min — proteção contra polling abusivo do frontend.
        redis_client = _sophos_get_redis()
        cached_payload: list[dict] | None = None
        cache_key = _sophos_discover_cache_key(integration_id)
        if redis_client is not None:
            try:
                cached_raw = redis_client.get(cache_key)
                if cached_raw:
                    cached_payload = json.loads(cached_raw)
            except Exception:  # noqa: BLE001
                cached_payload = None

        if cached_payload is None:
            # Fresh fetch — chama o provider direto. Bloqueia o request,
            # acceitável: operador apertou refresh consciente.
            try:
                provider = get_provider(integration)
            except (ProviderNotFoundError, ProviderConfigurationError) as exc:
                raise ApiError(
                    "integration.provider_unavailable",
                    409,
                    messages={
                        "pt": "{error}",
                        "en": "{error}",
                        "es": "{error}",
                    },
                    params={"error": str(exc)},
                ) from exc
            try:
                cached_payload = list(provider.discover_tenants())
            except ProviderError as exc:
                raise ApiError(
                    "integration.discover_tenants_failed",
                    502,
                    messages={
                        "pt": "falha ao descobrir tenants: {error}",
                        "en": "discover_tenants failed: {error}",
                        "es": "fallo al descubrir tenants: {error}",
                    },
                    params={"error": str(exc)},
                ) from exc
            finally:
                try:
                    provider.close()
                except Exception:  # noqa: BLE001
                    pass
            if redis_client is not None:
                try:
                    redis_client.setex(cache_key, 300, json.dumps(cached_payload))
                except Exception:  # noqa: BLE001
                    pass
            fetched_live = True

        # Atualiza snapshots — preserva ``state`` quando já existir.
        discovered_ids = set()
        auto_approve = bool(integration.auto_approve_new_tenants)
        default_state = "approved" if auto_approve else "pending"
        for tenant_dto in cached_payload or []:
            normalized = _sophos_tenant_payload_normalize(tenant_dto)
            if normalized is None:
                continue
            discovered_ids.add(normalized["external_id"])
            sel_repo.upsert_snapshot(
                parent_id=integration.id,
                external_id=normalized["external_id"],
                name_snapshot=normalized["name"],
                region_snapshot=normalized["region"],
                data_geography_snapshot=normalized["data_geography"],
                api_host_snapshot=normalized["api_host"],
                last_seen_at=datetime.utcnow(),
                default_state=default_state,
            )
        if redis_client is not None:
            try:
                redis_client.close()
            except Exception:  # noqa: BLE001
                pass

    # Filtro de state. ``stale`` é sintético — vamos calcular post-fetch.
    state_filter: str | None = None
    keep_only_stale = False
    if state and state != "all":
        if state == "stale":
            keep_only_stale = True
            state_filter = "approved"  # stale = approved + drifted
        elif state in ("pending", "approved", "excluded"):
            state_filter = state
        else:
            raise ApiError(
                "integration.invalid_state_filter",
                400,
                messages={
                    "pt": "filtro de state inválido: {state!r}",
                    "en": "invalid state filter: {state!r}",
                    "es": "filtro de state inválido: {state!r}",
                },
                params={"state": state},
            )

    total = sel_repo.count(integration.id, state=state_filter, search=search, geography=geography)
    offset = (page - 1) * size
    selections = sel_repo.list(
        integration.id, state=state_filter, search=search, geography=geography, limit=size, offset=offset
    )

    # Children por external_id pra resposta.
    children = repo.list_children(integration.id, include_inactive=True)
    children_by_external_id: dict[str, models.Integration] = {}
    for child in children:
        ext = (child.external_id or child.tenant_id or "").strip()
        if ext:
            children_by_external_id.setdefault(ext, child)

    # Decided-by usernames em batch — N+1 evitado.
    decided_user_ids = {s.decided_by_user_id for s in selections if s.decided_by_user_id}
    users_map: dict[int, models.AppUser] = {}
    if decided_user_ids:
        for u in (
            db.query(models.AppUser)
            .filter(models.AppUser.id.in_(decided_user_ids))
            .all()
        ):
            users_map[u.id] = u

    items = [
        _serialize_sophos_tenant_item(
            sel,
            discovered_ids=discovered_ids,
            children_by_external_id=children_by_external_id,
            decided_by_user=users_map.get(sel.decided_by_user_id) if sel.decided_by_user_id else None,
        )
        for sel in selections
    ]
    if keep_only_stale:
        items = [it for it in items if it.selection_state == "stale"]
        total = len(items)

    return schemas.SophosTenantListResponse(
        items=items,
        total=total,
        page=page,
        size=size,
        fetched_live=fetched_live,
        auto_approve_new_tenants=bool(integration.auto_approve_new_tenants),
        last_tenant_sync_at=integration.last_tenant_sync_at,
        tenant_sync_status=integration.tenant_sync_status,
    )


@router.post(
    "/{integration_id}/tenants/select",
    response_model=schemas.SelectTenantsResponse,
)
def select_tenants(
    integration_id: int,
    body: schemas.SelectTenantsRequest,
    repo: repository.IntegrationRepository = Depends(get_repo),
    db: Session = Depends(database.get_session),
    current_user: models.AppUser = Depends(
        app_auth.require_permission(app_auth.Permission.INTEGRATION_WRITE)
    ),
) -> schemas.SelectTenantsResponse:
    """Bulk approve/exclude de tenants Sophos descobertos.

    Idempotente. ``approved`` materializa Org+child se ainda não existir.
    ``excluded`` soft-deactiva o child (deactivate, não dropa) preservando
    histórico. Tenants ainda não descobertos retornam erro estruturado, sem 500.
    """
    integration = _get_integration_or_404(repo, integration_id)
    _ensure_integration_access(current_user, integration, require_active=True)
    _ensure_partner(integration)

    sel_repo = repository.IntegrationTenantSelectionRepository(db)

    response = schemas.SelectTenantsResponse()

    # Pré-validação: tenants não descobertos não podem ser aprovados/excluídos.
    existing = {
        s.external_id: s
        for s in (
            db.query(models.IntegrationTenantSelection)
            .filter(
                models.IntegrationTenantSelection.parent_integration_id == integration.id,
                models.IntegrationTenantSelection.external_id.in_(body.external_ids),
            )
            .all()
        )
    }
    valid_ids: list[str] = []
    for ext_id in body.external_ids:
        if ext_id not in existing:
            response.errors.append(
                schemas.SelectTenantsError(
                    external_id=ext_id,
                    reason="tenant not discovered yet — run sync first",
                )
            )
            continue
        valid_ids.append(ext_id)

    if not valid_ids:
        return response

    # 1) Persiste o novo state no DB pra todas as rows válidas.
    updated = sel_repo.set_state(
        parent_id=integration.id,
        external_ids=valid_ids,
        state=body.state,
        decided_by_user_id=current_user.id,
    )
    response.processed = len(updated)

    # 2) Materialização/desativação dos children — feature Enterprise (gestão de
    # tenants-filho de reseller). O pacote Enterprise registra um applier que faz
    # o trabalho SÍNCRONO e devolve contagens verdadeiras; na Community não há applier,
    # então as decisões ficam persistidas (passo 1) mas nenhum child é materializado.
    applier = ee_hooks.get_tenant_selection_applier()
    if applier is None:
        response.enterprise_required = True
        response.pending += len(updated)
        return response

    try:
        result = applier(db, integration, updated, body.state)
    except ee_hooks.LicenseRequiredError as exc:
        # EE presente, mas a licença ativa não concede a feature: o applier recusou
        # ANTES de materializar. Paridade com o branch Community — decisões ficam
        # persistidas (passo 1), zero children, sinal license_required (distinto).
        logger.warning(
            "select-tenants: recusado por licença (feature=%s) integration_id=%s",
            exc.feature, integration_id,
        )
        response.license_required = True
        response.pending += len(updated)
        return response
    response.materialized += int(result.get("materialized", 0))
    response.deactivated += int(result.get("deactivated", 0))
    response.pending += int(result.get("pending", 0))
    for err in result.get("errors", []):
        response.errors.append(
            schemas.SelectTenantsError(
                external_id=err["external_id"], reason=err["reason"]
            )
        )
    return response


@router.patch(
    "/{integration_id}/auto-approve-policy",
    response_model=schemas.AutoApprovePolicyResponse,
)
def update_auto_approve_policy(
    integration_id: int,
    body: schemas.AutoApprovePolicyUpdate,
    repo: repository.IntegrationRepository = Depends(get_repo),
    db: Session = Depends(database.get_session),
    current_user: models.AppUser = Depends(
        app_auth.require_permission(app_auth.Permission.INTEGRATION_WRITE)
    ),
) -> schemas.AutoApprovePolicyResponse:
    """Atualiza ``auto_approve_new_tenants`` do Partner. Próximo sync usa o novo valor."""
    integration = _get_integration_or_404(repo, integration_id)
    _ensure_integration_access(current_user, integration, require_active=True)
    _ensure_partner(integration)

    integration.auto_approve_new_tenants = body.auto_approve_new_tenants
    integration.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(integration)

    return schemas.AutoApprovePolicyResponse(
        integration_id=integration.id,
        auto_approve_new_tenants=bool(integration.auto_approve_new_tenants),
        updated_at=integration.updated_at,
    )


# ── Bulk operations ───────────────────────────────────────────────────

_BULK_DEACTIVATE_LIMIT = 500


class BulkDeactivateRequest(BaseModel):
    """Body do POST /integrations/bulk/deactivate.

    ``ids``: lista de integration_ids (1..500). Duplicatas são deduplicadas
    antes do processamento, preservando ordem para idempotência do audit log.
    """

    ids: List[int] = Field(..., min_length=1, max_length=_BULK_DEACTIVATE_LIMIT)


class BulkDeactivateError(BaseModel):
    id: int
    reason: str


class BulkDeactivateResponse(BaseModel):
    processed: int
    deactivated: int
    errors: List[BulkDeactivateError]


@router.post("/bulk/deactivate", response_model=BulkDeactivateResponse)
def bulk_deactivate_integrations(
    body: BulkDeactivateRequest,
    db: Session = Depends(database.get_session),
    repo: repository.IntegrationRepository = Depends(get_repo),
    current_user: models.AppUser = Depends(
        app_auth.require_permission(app_auth.Permission.INTEGRATION_WRITE)
    ),
) -> BulkDeactivateResponse:
    """Bulk soft-deactivate de integrações.

    Comportamento:
      * Itera ``ids``, marca ``is_active=False`` em cada um.
      * Já-inativa → erro ``already_inactive`` (idempotente, NÃO 500).
      * Não encontrada / fora do tenant scope → erro ``not_found``
        (não enumera tenants).
      * Partner/Organization → bloqueio explícito: a request inteira é
        rejeitada com 422 antes de qualquer mutation, indicando que o
        fluxo Partner cascade (DELETE /{id}?force=true) deve ser usado.
      * Para cada deactivated, deregistra do RedBeat fire-and-forget.
      * Grava audit log por sucesso (action=``bulk_deactivate_integration``).

    Retorna ``{processed, deactivated, errors}``.
    """
    # Dedupe preservando ordem.
    seen: set[int] = set()
    unique_ids = [i for i in body.ids if not (i in seen or seen.add(i))]  # type: ignore[func-returns-value]

    allowed_org_ids = tenant.accessible_org_ids(current_user, db)

    # Pre-flight: rejeita a request se algum dos IDs aponta pra Partner/Organization.
    # NUNCA aceitar Partner em bulk — UI tem
    # que mandar pelo cascade-delete modal individual.
    partner_blockers: list[int] = []
    for integration_id in unique_ids:
        integration = repo.get(integration_id)
        if integration is None:
            continue
        # Tenant scoping: ids fora do escopo permitido são tratados como
        # not_found (sem revelar existência) — cai no loop principal.
        if allowed_org_ids is not None and integration.organization_id not in allowed_org_ids:
            continue
        # Parents MSSP (capability discover:children) nunca entram em bulk —
        # exigem o cascade-delete modal individual.
        if integration_has_capability(integration, CAP_DISCOVER_CHILDREN):
            partner_blockers.append(integration_id)
    if partner_blockers:
        # Mensagem terse: o swagger documenta o fluxo alternativo correto.
        # Não queremos documentar o flag ``?force=true`` em body de erro
        # numa imagem pública.
        raise ApiError(
            "integration.partner_bulk_unsupported",
            422,
            messages={
                "pt": "Integrações Partner não são suportadas em operação em massa.",
                "en": "Partner integrations not supported in bulk operation.",
                "es": "Las integraciones Partner no son compatibles con la operación masiva.",
            },
            params={"partner_ids": partner_blockers},
        )

    errors: list[BulkDeactivateError] = []
    deactivated_count = 0

    for integration_id in unique_ids:
        try:
            integration = repo.get(integration_id)
            if integration is None:
                errors.append(BulkDeactivateError(id=integration_id, reason="not_found"))
                continue
            if allowed_org_ids is not None and integration.organization_id not in allowed_org_ids:
                # Não revela existência — mesmo erro de ID inexistente.
                errors.append(BulkDeactivateError(id=integration_id, reason="not_found"))
                continue
            if not integration.is_active:
                errors.append(
                    BulkDeactivateError(id=integration_id, reason="already_inactive")
                )
                continue

            repo.soft_delete(integration)
            _deregister_from_beat(integration_id)
            deactivated_count += 1

            # Audit log (best-effort — falha aqui não reverte a deactivation).
            try:
                db.add(
                    models.AuditLog(
                        user_id=current_user.id,
                        username=current_user.username,
                        user_role=current_user.role,
                        action="bulk_deactivate_integration",
                        endpoint="/api/integrations/bulk/deactivate",
                        method="POST",
                        status_code=200,
                        detail=(
                            f"Integration {integration_id} ({integration.name}) "
                            f"soft-deactivated via bulk endpoint"
                        ),
                    )
                )
                db.commit()
            except Exception:  # noqa: BLE001
                logger.exception(
                    "bulk_deactivate.audit_log_failed integration_id=%s",
                    integration_id,
                )
                db.rollback()
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            logger.exception(
                "bulk_deactivate.unexpected_error integration_id=%s", integration_id
            )
            errors.append(
                BulkDeactivateError(id=integration_id, reason=f"error: {exc}"[:200])
            )

    return BulkDeactivateResponse(
        processed=len(unique_ids),
        deactivated=deactivated_count,
        errors=errors,
    )


@router.get("/", response_model=list[schemas.IntegrationRead])
def list_integrations(
    organization_id: int | None = None,
    platform: str | None = None,
    include_inactive: bool = Query(default=False),
    name: str | None = Query(
        default=None,
        description="Substring case-insensitive em integration.name.",
    ),
    kind: str | None = Query(
        default=None,
        description="Filtra por kind: 'tenant'|'partner'|'organization'|'all'.",
    ),
    status: str | None = Query(
        default=None,
        description="Filtra por status: 'active'|'inactive'|'all'. "
        "Override do default include_inactive=False.",
    ),
    region: str | None = Query(
        default=None,
        description="Substring case-insensitive em integration.region.",
    ),
    data_geography: str | None = Query(
        default=None,
        description="Substring case-insensitive em integration.data_geography.",
    ),
    page: int | None = Query(default=None, ge=1, description="Página 1-based."),
    size: int | None = Query(
        default=None,
        ge=1,
        le=200,
        description="Tamanho da página. Default sem paginação retorna todos. "
        "Recomendado: 50.",
    ),
    repo: repository.IntegrationRepository = Depends(get_repo),
    current_user: models.AppUser = Depends(app_auth.require_authenticated_user),
):
    if organization_id is not None:
        tenant.require_subtree_access(current_user, organization_id)

    allowed_org_ids = tenant.accessible_org_ids(current_user, repo.db)

    # 'inactive'/'all' status só passam se o caller for admin — caso contrário,
    # cai no comportamento padrão de listar apenas ativas.
    effective_status = status
    if status and status.strip().lower() in ("inactive", "all") and not tenant.is_admin(current_user):
        effective_status = "active"

    integrations = repo.list(
        organization_id=organization_id,
        platform=platform,
        include_inactive=include_inactive and tenant.is_admin(current_user),
        organization_ids=allowed_org_ids,
        name=name,
        kind=kind,
        status=effective_status,
        region=region,
        data_geography=data_geography,
        page=page,
        size=size,
    )
    return [_serialize(i, current_user) for i in integrations]


@router.get("/{integration_id}", response_model=schemas.IntegrationRead)
def get_integration(
    integration_id: int,
    repo: repository.IntegrationRepository = Depends(get_repo),
    current_user: models.AppUser = Depends(app_auth.require_authenticated_user),
):
    integration = _get_integration_or_404(repo, integration_id)
    _ensure_integration_access(current_user, integration)
    if not integration.is_active and not tenant.is_admin(current_user):
        raise ApiError(
            "integration.inactive",
            403,
            messages={
                "pt": "A integração está inativa.",
                "en": "Integration is inactive.",
                "es": "La integración está inactiva.",
            },
        )
    return _serialize(integration, current_user)


@router.put("/{integration_id}", response_model=schemas.IntegrationRead)
def update_integration(
    integration_id: int,
    data: schemas.IntegrationUpdate,
    repo: repository.IntegrationRepository = Depends(get_repo),
    current_user: models.AppUser = Depends(app_auth.require_permission(app_auth.Permission.INTEGRATION_WRITE)),
):
    integration = _get_integration_or_404(repo, integration_id)
    # Escopo de tenant: consistente com os ~15 endpoints irmãos de escrita. Sem
    # isto, se INTEGRATION_WRITE fosse concedida a papel não-admin/custom, um
    # usuário escopado editaria/rotacionaria credenciais de integração de outra org.
    _ensure_integration_access(current_user, integration)

    payload = data.model_dump(exclude_unset=True)
    update_kwargs: Dict[str, Any] = {}

    if "name" in payload:
        update_kwargs["name"] = payload["name"]
    if "is_active" in payload:
        update_kwargs["is_active"] = payload["is_active"]

    if integration.platform == "sophos":
        final_client_id = payload["client_id"] if "client_id" in payload else integration.client_id
        final_client_secret_configured = (
            bool(payload["client_secret"]) if "client_secret" in payload
            else integration_secrets.has_secret(integration, "client_secret")
        )

        if not final_client_id or not final_client_secret_configured:
            raise ApiError(
                "integration.sophos_credentials_required",
                400,
                messages={
                    "pt": "A integração Sophos exige client_id e client_secret.",
                    "en": "Sophos integration requires client_id and client_secret.",
                    "es": "La integración Sophos requiere client_id y client_secret.",
                },
            )

        if "client_id" in payload:
            update_kwargs["client_id"] = payload["client_id"]
        if payload.get("client_secret"):
            # rotaciona client_secret no store e invalida os tokens
            # (revoga access/refresh — recunhados no próximo reauth). Validação acima
            # garante que client_secret vazio já caiu em 400.
            integration_secrets.write_secret(integration, "client_secret", payload["client_secret"])
            integration_secrets.revoke_secret(integration, "access_token")
            integration_secrets.revoke_secret(integration, "refresh_token")
            update_kwargs["tenant_id"] = None
        if "region" in payload:
            update_kwargs["region"] = payload["region"]
        if "client_id" in payload or "client_secret" in payload:
            update_kwargs["auth_status"] = "unknown"
            update_kwargs["last_error"] = None

    elif integration.platform == "wazuh":
        # configured-check vem do store (sem fallback api_* legado).
        # Indexer é OBRIGATÓRIO (fonte de detecções/consultas — wazuh-alerts-*).
        # Manager é OPCIONAL (saúde + inventário de agentes) — mas se fornecido,
        # username + password são exigidos (proibido Manager parcial).
        final_indexer_url = payload["indexer_url"] if "indexer_url" in payload else integration.indexer_url
        final_indexer_username = (
            bool(payload["indexer_username"]) if "indexer_username" in payload
            else integration_secrets.has_secret(integration, "indexer_username")
        )
        final_indexer_password = (
            bool(payload["indexer_password"]) if "indexer_password" in payload
            else integration_secrets.has_secret(integration, "indexer_password")
        )
        final_manager_url = payload["manager_url"] if "manager_url" in payload else integration.manager_url
        final_manager_username = (
            bool(payload["manager_api_username"]) if "manager_api_username" in payload
            else integration_secrets.has_secret(integration, "manager_api_username")
        )
        final_manager_password = (
            bool(payload["manager_api_password"]) if "manager_api_password" in payload
            else integration_secrets.has_secret(integration, "manager_api_password")
        )

        if not final_indexer_url or not final_indexer_username or not final_indexer_password:
            raise ApiError(
                "integration.wazuh_indexer_required",
                400,
                messages={
                    "pt": "A integração Wazuh exige indexer_url, indexer_username e indexer_password.",
                    "en": "Wazuh integration requires indexer_url, indexer_username and indexer_password.",
                    "es": "La integración Wazuh requiere indexer_url, indexer_username y indexer_password.",
                },
            )
        if final_manager_url and (not final_manager_username or not final_manager_password):
            raise ApiError(
                "integration.wazuh_manager_incomplete",
                400,
                messages={
                    "pt": "manager_url do Wazuh exige manager_api_username e manager_api_password.",
                    "en": "Wazuh manager_url requires manager_api_username and manager_api_password.",
                    "es": "manager_url de Wazuh requiere manager_api_username y manager_api_password.",
                },
            )

        if "indexer_url" in payload:
            update_kwargs["indexer_url"] = payload["indexer_url"]
        if "manager_url" in payload:
            update_kwargs["manager_url"] = payload["manager_url"]
            if not payload["manager_url"]:
                # Limpar a URL do manager revoga suas credenciais no store.
                integration_secrets.revoke_secret(integration, "manager_api_username")
                integration_secrets.revoke_secret(integration, "manager_api_password")
        if "verify_ssl" in payload:
            update_kwargs["verify_ssl"] = payload["verify_ssl"]
        # as 4 credenciais vivem no store integration_credentials.
        # Valor truthy ⇒ rotate (write_secret); vazio/None ⇒ revoke.
        for _logical in ("indexer_username", "indexer_password", "manager_api_username", "manager_api_password"):
            if _logical in payload:
                if payload[_logical]:
                    integration_secrets.write_secret(integration, _logical, payload[_logical])
                else:
                    integration_secrets.revoke_secret(integration, _logical)

        if any(field in payload for field in ("manager_url", "manager_api_username", "manager_api_password", "indexer_url", "indexer_username", "indexer_password", "verify_ssl")):
            update_kwargs["auth_status"] = "unknown"
            update_kwargs["last_error"] = None

    else:
        # Vendor-neutro (ninjaone/defender/…): config em colunas, secret no store
        # (rotate via write_secret).
        for key in ("client_id", "base_url", "tenant_id"):
            if key in payload:
                update_kwargs[key] = payload[key]
        if payload.get("client_secret"):
            integration_secrets.write_secret(integration, "client_secret", payload["client_secret"])
        if any(k in payload for k in ("client_id", "client_secret", "base_url", "tenant_id")):
            update_kwargs["auth_status"] = "unknown"
            update_kwargs["last_error"] = None

    integration = repo.update(integration, **update_kwargs)
    return _serialize(integration, current_user)


@router.delete("/{integration_id}")
def delete_integration(
    integration_id: int,
    purge: bool = Query(default=False),
    force: bool = Query(default=False),
    repo: repository.IntegrationRepository = Depends(get_repo),
    current_user: models.AppUser = Depends(app_auth.require_permission(app_auth.Permission.INTEGRATION_WRITE)),
):
    # rate limit por usuário — 5 DELETE/min
    retry_after = integration_rate_limiter.check_delete(current_user.id)
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail="Too many integration deletion requests. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    integration = _get_integration_or_404(repo, integration_id)

    # Sophos Partner Mode: deleting a Partner with active children is a
    # double-edged operation. Default behaviour: 409 + list of children so
    # the UI can show a confirmation modal. ``?force=true`` cascades the
    # soft-delete down to children and (auto-managed) Organizations.
    # Parent MSSP (capability discover:children) com filhos ativos → 409 + lista
    # (ou cascade com ?force=true).
    if integration_has_capability(integration, CAP_DISCOVER_CHILDREN):
        active_children = repo.list_children(integration_id, include_inactive=False)
        if active_children and not force:
            raise ApiError(
                "integration.partner_has_active_children",
                409,
                messages={
                    "pt": (
                        "A integração Partner tem {count} tenant(s) filho(s) ativo(s). "
                        "Refaça a requisição com ?force=true para desativá-los em cascata "
                        "(o histórico é preservado)."
                    ),
                    "en": (
                        "Partner integration has {count} active child tenant(s). "
                        "Re-issue the request with ?force=true to soft-delete them all "
                        "(history is preserved)."
                    ),
                    "es": (
                        "La integración Partner tiene {count} tenant(s) hijo(s) activo(s). "
                        "Vuelva a enviar la solicitud con ?force=true para desactivarlos en cascada "
                        "(el historial se conserva)."
                    ),
                },
                params={
                    "count": len(active_children),
                    "children": [
                        {
                            "id": c.id,
                            "name": c.name,
                            "external_id": c.external_id,
                            "organization_id": c.organization_id,
                        }
                        for c in active_children
                    ],
                },
            )
        if force:
            # Soft-delete cascade — preserves rows + history. ``purge`` is
            # rejected to avoid accidentally hard-deleting a partner tree.
            if purge:
                raise ApiError(
                    "integration.partner_purge_not_supported",
                    400,
                    messages={
                        "pt": "purge=true não é suportado para integrações Partner; use force=true (desativação em cascata).",
                        "en": "purge=true is not supported for Partner integrations; use force=true (soft-delete cascade).",
                        "es": "purge=true no es compatible con integraciones Partner; use force=true (desactivación en cascada).",
                    },
                )
            affected = repo.soft_delete_cascade(integration)
            for child in repo.list_children(integration_id, include_inactive=True):
                _deregister_from_beat(child.id)
            _deregister_from_beat(integration_id)
            return {
                "detail": f"Partner soft-deleted with cascade ({affected} integrations deactivated)",
                "affected": affected,
            }

    if purge:
        repo.delete(integration)
    else:
        repo.soft_delete(integration)

    # ── Hook on-delete/disable: remove entries do RedBeat ────────────
    # Fire-and-forget: falha no Redis não reverte a deleção do banco.
    _deregister_from_beat(integration_id)

    if purge:
        return {"detail": "Integration deleted permanently"}
    return {"detail": "Integration deactivated"}


# ── Test Connection ───────────────────────────────────────────────────

@router.post("/{integration_id}/test-connection", response_model=schemas.TestConnectionResponse)
def test_connection(
    integration_id: int,
    db: Session = Depends(database.get_session),
    repo: repository.IntegrationRepository = Depends(get_repo),
    health_repo: repository.IntegrationHealthRepository = Depends(get_health_repo),
    _: models.AppUser = Depends(app_auth.require_permission(app_auth.Permission.INTEGRATION_WRITE)),
):
    integration = _get_integration_or_404(repo, integration_id)

    provider = get_provider(integration)
    try:
        result = provider.test_connection()
        _record_health_state(db, integration.id, result)

        # Persist health check
        health_repo.add(models.IntegrationHealthCheck(
            integration_id=integration.id,
            status=result.status,
            details=json.dumps(result.details),
        ))

        return schemas.TestConnectionResponse(status=result.status, details=result.details)
    finally:
        provider.close()


# ── Health Check ──────────────────────────────────────────────────────

@router.get("/{integration_id}/health")
def get_health(
    integration_id: int,
    response: Response,
    db: Session = Depends(database.get_session),
    repo: repository.IntegrationRepository = Depends(get_repo),
    current_user: models.AppUser = Depends(app_auth.require_authenticated_user),
    api_version: int = Depends(resolve_api_version),
):
    """Health check para uma integração.

    Suporta dois shapes via header ``Accept``:
    - Padrão (sem header ou ``*/*``): retorna ``HealthResponse`` v2.
    - ``Accept: application/vnd.centralops.v1+json``: retorna shape v1 legado
      com header ``X-API-Deprecation`` indicando remoção na próxima release.
    """
    from sqlalchemy.orm import selectinload as _selectinload

    integration = (
        db.query(models.Integration)
        .options(_selectinload(models.Integration.organization))
        .filter(models.Integration.id == integration_id)
        .first()
    )
    if not integration:
        raise ApiError(
            "integration.not_found",
            404,
            messages={
                "pt": "Integração não encontrada.",
                "en": "Integration not found.",
                "es": "Integración no encontrada.",
            },
        )
    _ensure_integration_access(current_user, integration, require_active=True)

    last_collection_at, last_success_at = _collection_timestamps(db, integration_id)

    db.expunge_all()
    db.close()

    provider = get_provider(integration)
    try:
        result = provider.health_check()

        with database.SessionLocal() as write_db:
            now = datetime.utcnow()
            last_error = _integration_last_error(result.details)
            update_values: Dict[str, Any] = {
                "auth_status": result.status,
                "last_checked_at": now,
                "last_error": last_error,
                "updated_at": now,
            }
            if result.status in {"healthy", "degraded"}:
                update_values["last_successful_check_at"] = now
            write_db.query(models.Integration).filter(
                models.Integration.id == integration_id
            ).update(update_values)
            write_db.add(models.IntegrationHealthCheck(
                integration_id=integration_id,
                status=result.status,
                details=json.dumps(result.details),
            ))
            write_db.commit()

        # ── v1 backwards compat ───────────────────────────────────────────
        if api_version == 1:
            response.headers["X-API-Deprecation"] = (
                "HealthSchema v1 sera removido na proxima release; migrar para v2"
            )
            return schemas.IntegrationHealthRead(
                integration_id=integration_id,
                status=result.status,
                details=result.details,
                manager_status=_component_status(result.details, "manager"),
                indexer_status=_component_status(result.details, "indexer"),
            )

        # ── v2 data-driven ────────────────────────────────────────────────
        try:
            metrics = provider.get_health_metrics()
        except Exception:
            logger.warning(
                "get_health_metrics falhou para integration=%s — retornando metrics=[]",
                integration_id,
                exc_info=True,
            )
            metrics = []

        return HealthResponse(
            platform=integration.platform,
            last_collection_at=last_collection_at,
            last_success_at=last_success_at,
            metrics=metrics,
        )
    finally:
        provider.close()


# ── Overview (unified summary for an integration) ─────────────────────

@router.get("/{integration_id}/overview", response_model=schemas.IntegrationOverviewRead)
def get_integration_overview(
    integration_id: int,
    db: Session = Depends(database.get_session),
    repo: repository.IntegrationRepository = Depends(get_repo),
    health_repo: repository.IntegrationHealthRepository = Depends(get_health_repo),
    current_user: models.AppUser = Depends(app_auth.require_authenticated_user),
):
    integration = _get_integration_or_404(repo, integration_id)
    _ensure_integration_access(current_user, integration, require_active=True)

    overview: Dict[str, Any] = {
        "integration": _serialize(integration, current_user).model_dump(),
    }

    provider = get_provider(integration)
    try:
        # Health
        try:
            health = provider.health_check()
            _record_health_state(db, integration.id, health)
            overview["health"] = _serialize_health_result(health)
        except Exception:
            overview["health"] = {
                "status": "error",
                "details": {"message": "Health check failed"},
                "manager_status": None,
                "indexer_status": None,
            }

        # Licensed products — gateado pela capability licensing:list.
        # SophosProvider só a declara p/ child tenant (kind=tenant com
        # parent) — encapsula o ``platform=='sophos' and kind=='tenant' and parent``.
        if CAP_LICENSING_LIST in provider.capabilities():
            try:
                from ..providers.sophos.licensing import fetch_licenses

                overview["licensed_products"] = fetch_licenses(integration)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "sophos:licenses: failed to fetch for integration %s: %s",
                    integration.id,
                    exc,
                )
                overview["licensed_products"] = None

        return overview
    finally:
        provider.close()
