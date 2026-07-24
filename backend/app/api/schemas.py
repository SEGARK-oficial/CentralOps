import logging
from datetime import datetime
from typing import Optional, List, Any, Dict, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator
import re

from ..core.url_policy import normalize_service_url
from ..core.config import settings

logger = logging.getLogger(__name__)


# 4 papéis RBAC + "user" legado (migrado para "viewer" na DB)
UserRole = Literal["viewer", "operator", "engineer", "admin", "user"]
ScheduleTimeUnit = Literal["minutes", "hours", "days", "weeks"]
PlatformType = Literal["sophos", "wazuh"]
# Sophos hierarchy: "tenant" (single cred per integration — default & legacy),
# "partner" (Partner Account that owns N tenants), "organization" (Org tier).
IntegrationKind = Literal["partner", "organization", "tenant"]
# "enterprise_required": edição Community não tem o dispatcher de partner-sync
# registrado (feature paga) — sinaliza honestamente em vez de fingir "ok".
# "license_required": o artefato EE ESTÁ presente, mas a licença ativa não concede a
# feature (ausente/expirada pós-carência/plano sem multi_tenant) — o seam EE recusou
# via ee_hooks.LicenseRequiredError. Distinto de enterprise_required (artefato ausente).
TenantSyncStatus = Literal["ok", "partial", "error", "enterprise_required", "license_required"]
DiscoveredTenantStatus = Literal["new", "linked", "stale"]
# Estado de seleção do tenant Sophos descoberto. ``stale`` é sintético — só
# aparece em respostas (tenant na tabela mas sumiu do último discover).
TenantSelectionState = Literal["pending", "approved", "excluded"]
SophosTenantUiState = Literal["pending", "approved", "excluded", "stale"]


def _normalize_optional_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


# ── Organization ──────────────────────────────────────────────────────

class OrganizationCreate(BaseModel):
    name: str
    slug: Optional[str] = None
    description: Optional[str] = None

    @field_validator("name")
    @classmethod
    def strip_org_name(cls, v: str) -> str:
        value = v.strip()
        if not value:
            raise ValueError("name must not be empty")
        return value

    @field_validator("slug")
    @classmethod
    def normalize_slug(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return re.sub(r"[^a-z0-9-]", "-", v.strip().lower()).strip("-") or None


class OrganizationRead(BaseModel):
    id: int
    name: str
    slug: str
    description: Optional[str] = None
    is_active: bool = True
    integration_count: int = 0
    # Sophos Partner auto-onboarding fields.
    external_provider: Optional[str] = None
    external_id: Optional[str] = None
    auto_managed: bool = False
    iris_customer_id: Optional[int] = None
    partner_integration_id: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class OrganizationUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


# ── Bulk operations (bulk deactivate) ─────────────────────────


class BulkDeactivateOrganizationsRequest(BaseModel):
    """Payload do POST /api/organizations/bulk/deactivate.

    O cap de 500 IDs evita query gigante e segura o tempo do request.
    """

    ids: List[int] = Field(..., min_length=1, max_length=500)


class BulkDeactivateOrganizationsError(BaseModel):
    id: int
    reason: str


class BulkDeactivateOrganizationsResult(BaseModel):
    """Resposta do bulk deactivate. Idempotente: ``deactivated`` conta apenas
    transições efetivas (orgs que estavam ativas e foram desativadas)."""

    processed: int
    deactivated: int
    errors: List[BulkDeactivateOrganizationsError] = Field(default_factory=list)


# ── Retention config ─────────────────────────────────────────

_RETENTION_DAYS_MIN = 1
_RETENTION_DAYS_MAX = 3650  # 10 anos


class OrganizationRetentionConfigRead(BaseModel):
    """Leitura da configuração de retenção por organização."""

    organization_id: int
    quarantine_retention_days: int = 7
    drift_retention_days: int = 90
    history_retention_days: int = 30
    search_result_retention_days: int = 7
    audit_log_retention_days: int = 365
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class OrganizationRetentionConfigUpdate(BaseModel):
    """Atualização parcial da configuração de retenção."""

    quarantine_retention_days: Optional[int] = Field(
        default=None, ge=_RETENTION_DAYS_MIN, le=_RETENTION_DAYS_MAX
    )
    drift_retention_days: Optional[int] = Field(
        default=None, ge=_RETENTION_DAYS_MIN, le=_RETENTION_DAYS_MAX
    )
    history_retention_days: Optional[int] = Field(
        default=None, ge=_RETENTION_DAYS_MIN, le=_RETENTION_DAYS_MAX
    )
    search_result_retention_days: Optional[int] = Field(
        default=None, ge=_RETENTION_DAYS_MIN, le=_RETENTION_DAYS_MAX
    )
    audit_log_retention_days: Optional[int] = Field(
        default=None, ge=_RETENTION_DAYS_MIN, le=_RETENTION_DAYS_MAX
    )


# ── Data Deletion Job ────────────────────────────────────────


class DataDeletionRequest(BaseModel):
    """Payload para solicitar purge total de dados de uma organização.

    ``confirmation_text`` deve ser exatamente 'DELETAR {org_slug}'
    para evitar deleção acidental.
    """

    confirmation_text: str
    reason: Optional[str] = None

    @field_validator("confirmation_text")
    @classmethod
    def strip_confirmation(cls, v: str) -> str:
        return v.strip()


class DataDeletionJobRead(BaseModel):
    """Retorno da solicitação de purge."""

    id: str
    organization_id: int
    organization_slug: str
    requested_by_username: Optional[str] = None
    reason: Optional[str] = None
    status: str
    rows_deleted: Optional[str] = None
    last_error: Optional[str] = None
    requested_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    celery_task_id: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


# ── Integration ───────────────────────────────────────────────────────

class IntegrationCreate(BaseModel):
    organization_id: int
    name: str
    # ``platform`` é um string-key validado em runtime contra o registry
    # de plataformas (catálogo plugin-driven), NÃO um Literal hardcoded. Assim um
    # vendor novo fica criável só registrando sua PlatformRegistration — zero core.
    platform: str
    # Sophos hierarchy: omit (or set "tenant") for legacy single-tenant flow.
    # Set "partner" or "organization" to bootstrap auto-discovery of children.
    kind: Optional[IntegrationKind] = None

    # OAuth/client-credentials genérico (sophos, ninjaone, defender, …)
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    region: Optional[str] = None
    # ``base_url`` (ex.: NinjaOne) e ``tenant_id`` (ex.: Microsoft Defender) —
    # campos genéricos do capability model; cada vendor declara nos auth_fields.
    base_url: Optional[str] = None
    tenant_id: Optional[str] = None

    # Wazuh fields
    manager_url: Optional[str] = None
    indexer_url: Optional[str] = None
    manager_api_username: Optional[str] = Field(default=None, validation_alias=AliasChoices("manager_api_username", "api_username"))
    manager_api_password: Optional[str] = Field(default=None, validation_alias=AliasChoices("manager_api_password", "api_password"))
    indexer_username: Optional[str] = None
    indexer_password: Optional[str] = None
    verify_ssl: Optional[bool] = True

    # ``extra="allow"`` deixa o vendor declarar chaves de
    # credencial INÉDITAS (ex.: okta ``api_token``, cloudtrail ``secret_access_key``)
    # que o cliente posta por key no topo do body — sem editar este schema fixo.
    # _assign_credentials só consome as keys das ``auth_fields`` declaradas; extra
    # não-mapeado é ignorado (chave obrigatória ausente ⇒ 400 descritivo).
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    @field_validator("platform")
    @classmethod
    def normalize_platform(cls, v: str) -> str:
        value = (v or "").strip().lower()
        if not value:
            raise ValueError("platform must not be empty")
        return value

    @field_validator("name")
    @classmethod
    def strip_integration_name(cls, v: str) -> str:
        value = v.strip()
        if not value:
            raise ValueError("name must not be empty")
        return value

    @field_validator("kind", mode="before")
    @classmethod
    def normalise_kind(cls, v: Optional[str]) -> Optional[str]:
        """Treat empty string as None; surface as ``"tenant"`` only at the router."""
        if v is None:
            return None
        if not isinstance(v, str):
            return v
        normalized = v.strip().lower()
        if not normalized:
            return None
        return normalized

    @field_validator(
        "client_id",
        "client_secret",
        "region",
        "base_url",
        "tenant_id",
        "manager_api_username",
        "manager_api_password",
        "indexer_username",
        "indexer_password",
    )
    @classmethod
    def normalize_optional_integration_text(cls, v: Optional[str]) -> Optional[str]:
        return _normalize_optional_text(v)

    @field_validator("manager_url", "indexer_url")
    @classmethod
    def normalize_service_urls(cls, v: Optional[str]) -> Optional[str]:
        return normalize_service_url(v)

    @field_validator("verify_ssl")
    @classmethod
    def warn_insecure_ssl(cls, v: Optional[bool]) -> Optional[bool]:
        """Permite verify_ssl=False em QUALQUER ambiente — decisão explícita do usuário.
        Wazuh/soluções self-hosted rodam com certificado auto-assinado
        na maioria dos deploys reais; bloquear em produção tornava a conexão impossível.
        A flag "confiar no certificado" é opt-in por-integração (padrão do mercado:
        Cribl/Grafana/Splunk expõem "skip TLS verify" por conexão). O trade-off (MITM no
        hop collector→manager) fica registrado em WARNING para auditoria — o operador
        pode alertar sobre isso na observabilidade se quiser.
        """
        if v is False:
            logger.warning(
                "integração configurada com verify_ssl=False (APP_ENV=%s): certificado "
                "do serviço remoto NÃO será validado — escolha explícita do usuário "
                "(auto-assinado/self-hosted)", settings.APP_ENV,
            )
        return v


class IntegrationRead(BaseModel):
    id: int
    organization_id: int
    organization_name: Optional[str] = None
    name: str
    platform: str
    is_active: bool = True
    is_authenticated: bool = False
    auth_status: str = "unknown"
    last_checked_at: Optional[datetime] = None
    last_successful_check_at: Optional[datetime] = None
    last_error: Optional[str] = None

    # Hierarchy / Partner mode (Sophos)
    kind: str = "tenant"
    parent_integration_id: Optional[int] = None
    external_id: Optional[str] = None
    id_type: Optional[str] = None
    data_geography: Optional[str] = None
    last_tenant_sync_at: Optional[datetime] = None
    tenant_sync_status: Optional[str] = None
    auto_managed: bool = False
    # Populated only for ``kind == "partner"`` listings.
    children_count: Optional[int] = None

    # Sophos visible fields
    client_id: Optional[str] = None
    region: Optional[str] = None
    tenant_id: Optional[str] = None
    base_url: Optional[str] = None

    # Wazuh visible fields
    manager_url: Optional[str] = None
    indexer_url: Optional[str] = None
    manager_api_username: Optional[str] = None
    indexer_username: Optional[str] = None
    manager_api_password_configured: bool = False
    indexer_password_configured: bool = False
    verify_ssl: Optional[bool] = None

    # Capabilities
    capabilities: List[str] = Field(default_factory=list)

    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class IntegrationUpdate(BaseModel):
    name: Optional[str] = None
    is_active: Optional[bool] = None

    # OAuth/client-credentials genérico
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    region: Optional[str] = None
    base_url: Optional[str] = None
    tenant_id: Optional[str] = None

    # Wazuh fields
    manager_url: Optional[str] = None
    indexer_url: Optional[str] = None
    manager_api_username: Optional[str] = Field(default=None, validation_alias=AliasChoices("manager_api_username", "api_username"))
    manager_api_password: Optional[str] = Field(default=None, validation_alias=AliasChoices("manager_api_password", "api_password"))
    indexer_username: Optional[str] = None
    indexer_password: Optional[str] = None
    verify_ssl: Optional[bool] = None

    model_config = ConfigDict(populate_by_name=True)

    @field_validator(
        "name",
        "client_id",
        "client_secret",
        "region",
        "base_url",
        "tenant_id",
        "manager_api_username",
        "manager_api_password",
        "indexer_username",
        "indexer_password",
    )
    @classmethod
    def normalize_optional_update_text(cls, v: Optional[str]) -> Optional[str]:
        return _normalize_optional_text(v)

    @field_validator("manager_url", "indexer_url")
    @classmethod
    def normalize_update_urls(cls, v: Optional[str]) -> Optional[str]:
        return normalize_service_url(v)

    @field_validator("verify_ssl")
    @classmethod
    def warn_insecure_ssl_update(cls, v: Optional[bool]) -> Optional[bool]:
        """Permite verify_ssl=False em qualquer ambiente (decisão do usuário) + WARNING
        auditável — ver :meth:`IntegrationCreate.warn_insecure_ssl`."""
        if v is False:
            logger.warning(
                "integração atualizada com verify_ssl=False (APP_ENV=%s): certificado "
                "do serviço remoto NÃO será validado — escolha explícita do usuário",
                settings.APP_ENV,
            )
        return v


class IntegrationHealthRead(BaseModel):
    integration_id: int
    status: str
    details: Dict[str, Any] = Field(default_factory=dict)
    checked_at: Optional[datetime] = None
    manager_status: Optional[str] = None
    indexer_status: Optional[str] = None


class TestConnectionResponse(BaseModel):
    status: str
    details: Dict[str, Any] = Field(default_factory=dict)


# ── Sophos Partner Mode — sync & resolution schemas ───────────────────

class DiscoveredTenant(BaseModel):
    """A tenant discovered through ``GET /partner/v1/tenants`` (or org tier).

    ``status`` indicates the relationship to the local CentralOps state:
      * ``new``    — first time this tenant has been seen by the sync
      * ``linked`` — already has Organization + child Integration locally
      * ``stale``  — present locally but no longer returned by Sophos
    """

    external_id: str
    name: str
    region: Optional[str] = None
    data_geography: Optional[str] = None
    api_host: Optional[str] = None
    status: DiscoveredTenantStatus = "new"
    linked_organization_id: Optional[int] = None
    linked_integration_id: Optional[int] = None


class PartnerSyncResult(BaseModel):
    """Outcome of a single Partner ``sync_tenants`` run."""

    integration_id: int
    discovered: int = 0
    created: int = 0
    linked: int = 0
    deactivated: int = 0
    errors: List[str] = Field(default_factory=list)
    started_at: datetime
    finished_at: Optional[datetime] = None
    status: TenantSyncStatus = "ok"


class PartnerSyncStatus(BaseModel):
    """Lightweight polling endpoint reply for ``GET /sync-status``."""

    integration_id: int
    tenant_sync_status: Optional[str] = None
    last_tenant_sync_at: Optional[datetime] = None
    lock_active: bool = False


# ── Sophos Partner — tenant selection (opt-in per tenant) ─────

class SophosTenantListItem(BaseModel):
    """Linha exibida em ``GET /integrations/{id}/sophos-tenants``.

    Mescla descoberta ao vivo + seleção persistida + child Integration (quando
    materializado). Frontend não precisa fazer N joins — uma única lista.
    """

    external_id: str
    name: Optional[str] = None
    region: Optional[str] = None
    data_geography: Optional[str] = None
    api_host: Optional[str] = None
    selection_state: SophosTenantUiState = "pending"
    child_integration_id: Optional[int] = None
    decided_by_user_id: Optional[int] = None
    decided_by_username: Optional[str] = None
    decided_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None


class SophosTenantListResponse(BaseModel):
    """Resposta paginada do listing de tenants Sophos."""

    items: List[SophosTenantListItem] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    size: int = 200
    fetched_live: bool = False
    auto_approve_new_tenants: bool = False
    last_tenant_sync_at: Optional[datetime] = None
    tenant_sync_status: Optional[str] = None


class SelectTenantsRequest(BaseModel):
    """Body do ``POST /integrations/{id}/tenants/select``.

    Idempotente: chamadas repetidas com mesmo input geram zero efeito visível
    a partir da segunda. Limites: 1..500 external_ids por request.
    """

    external_ids: List[str] = Field(..., min_length=1, max_length=500)
    state: Literal["approved", "excluded"]

    @field_validator("external_ids")
    @classmethod
    def _strip_external_ids(cls, value: List[str]) -> List[str]:
        cleaned = [v.strip() for v in value if isinstance(v, str) and v.strip()]
        if not cleaned:
            raise ValueError("external_ids must contain at least one non-empty value")
        # Dedup preservando ordem.
        seen: set[str] = set()
        ordered: List[str] = []
        for v in cleaned:
            if v not in seen:
                seen.add(v)
                ordered.append(v)
        if len(ordered) > 500:
            raise ValueError("external_ids must contain at most 500 entries")
        return ordered


class SelectTenantsError(BaseModel):
    external_id: str
    reason: str


class SelectTenantsResponse(BaseModel):
    processed: int = 0
    materialized: int = 0  # children novos criados nesta chamada
    deactivated: int = 0  # children soft-deletados nesta chamada
    pending: int = 0  # selections que ficaram pendentes (não foi possível agir)
    errors: List[SelectTenantsError] = Field(default_factory=list)
    # Open-core: a materialização de tenants-filho é feature Enterprise.
    # Na Community as decisões são persistidas (processed) mas nenhum child é
    # materializado — este flag sinaliza isso ao cliente (sem 500, sem contagem falsa).
    enterprise_required: bool = False
    # EE presente mas a licença ativa NÃO concede a feature (multi_tenant): o applier
    # recusou via ee_hooks.LicenseRequiredError. Decisões persistidas, zero children.
    license_required: bool = False


class AutoApprovePolicyUpdate(BaseModel):
    auto_approve_new_tenants: bool


class AutoApprovePolicyResponse(BaseModel):
    integration_id: int
    auto_approve_new_tenants: bool
    updated_at: datetime


class TenantResolutionSophos(BaseModel):
    tenant_external_id: Optional[str] = None
    region: Optional[str] = None
    partner_integration_id: Optional[int] = None
    child_integration_id: Optional[int] = None
    is_active: bool = False


class TenantResolution(BaseModel):
    """Reply of the internal ``/api/internal/tenants/...`` endpoints.

    Stable contract for external tenant-resolution consumers.
    """

    organization_id: int
    organization_slug: str
    organization_name: Optional[str] = None
    iris_customer_id: Optional[int] = None
    is_active: bool = True
    sophos: Optional[TenantResolutionSophos] = None
    mcps_enabled: List[str] = Field(default_factory=list)


# ── Integration overview ─────────────────────────────────────────────

class IntegrationOverviewHealthRead(BaseModel):
    status: str
    details: Dict[str, Any] = Field(default_factory=dict)
    manager_status: Optional[str] = None
    indexer_status: Optional[str] = None


class IntegrationOverviewRead(BaseModel):
    integration: IntegrationRead
    health: Optional[IntegrationOverviewHealthRead] = None
    # Populated only for Sophos child tenants (kind="tenant" + parent_integration_id set).
    # None for all other platforms and for Partner/Organization roots.
    licensed_products: Optional[List[Dict[str, Any]]] = None


class AuthTokens(BaseModel):
    access_token: str
    refresh_token: str


class AuthStatusRead(BaseModel):
    setup_required: bool
    company_name: str
    company_portal_name: str
    # SSO (Microsoft Entra) — frontend mostra o botão quando habilitado.
    sso_enabled: bool = False
    sso_button_label: Optional[str] = None


# ── Identity / SSO config (operada pela UI) ──────────────────

_VALID_APP_ROLES = {"viewer", "operator", "engineer", "admin"}


class IdentityConfigRead(BaseModel):
    """Resposta da config de identidade. O ``client_secret`` NUNCA é devolvido
    em claro — só a flag ``entra_client_secret_configured``."""

    entra_enabled: bool = False
    entra_tenant_id: Optional[str] = None
    entra_client_id: Optional[str] = None
    entra_client_secret_configured: bool = False
    entra_redirect_uri: Optional[str] = None
    entra_authority: str = "https://login.microsoftonline.com"
    entra_scopes: str = "openid profile email"
    entra_role_map: Dict[str, str] = Field(default_factory=dict)
    entra_default_role: str = "viewer"
    entra_default_is_global: bool = False
    entra_jit_provisioning: bool = True
    entra_allowed_email_domains: List[str] = Field(default_factory=list)
    entra_button_label: str = "Entrar com Microsoft"
    entra_post_login_redirect: str = "/"
    is_persisted: bool = False
    updated_at: Optional[datetime] = None
    # campos de controle e status do Graph-sync (read-only para o cliente)
    entra_sync_enabled: bool = False
    entra_sync_deprovision: bool = True
    entra_last_sync_at: Optional[datetime] = None
    entra_last_sync_status: Optional[str] = None
    # Summary deserializado de JSON para Dict antes de devolver ao cliente
    entra_last_sync_summary: Optional[Dict[str, Any]] = None


class IdentityConfigUpdate(BaseModel):
    """Partial update. ``entra_client_secret`` só é gravado quando enviado
    não-vazio (mandar ausente/vazio preserva o secret atual)."""

    entra_enabled: Optional[bool] = None
    entra_tenant_id: Optional[str] = None
    entra_client_id: Optional[str] = None
    entra_client_secret: Optional[str] = None
    entra_redirect_uri: Optional[str] = None
    entra_authority: Optional[str] = None
    entra_scopes: Optional[str] = None
    entra_role_map: Optional[Dict[str, str]] = None
    entra_default_role: Optional[str] = None
    entra_default_is_global: Optional[bool] = None
    entra_jit_provisioning: Optional[bool] = None
    entra_allowed_email_domains: Optional[List[str]] = None
    entra_button_label: Optional[str] = None
    entra_post_login_redirect: Optional[str] = None
    # campos mutaveis via PUT; campos de status sao somente-leitura
    entra_sync_enabled: Optional[bool] = None
    entra_sync_deprovision: Optional[bool] = None

    @field_validator("entra_default_role")
    @classmethod
    def _check_default_role(cls, v):
        if v is not None and v not in _VALID_APP_ROLES:
            raise ValueError(f"entra_default_role deve ser um de {sorted(_VALID_APP_ROLES)}")
        return v

    @field_validator("entra_role_map")
    @classmethod
    def _check_role_map(cls, v):
        if v is None:
            return v
        for app_role, local_role in v.items():
            if local_role not in _VALID_APP_ROLES:
                raise ValueError(
                    f"entra_role_map['{app_role}']='{local_role}' inválido "
                    f"(use {sorted(_VALID_APP_ROLES)})"
                )
        return v

    @field_validator("entra_allowed_email_domains")
    @classmethod
    def _normalize_domains(cls, v):
        if v is None:
            return v
        return [d.strip().lower() for d in v if d and d.strip()]

    @field_validator("entra_client_id", "entra_tenant_id")
    @classmethod
    def _check_identifier(cls, v):
        # tenant_id/client_id são interpolados em literal OData no Graph
        # (servicePrincipals(appId='{client_id}')). Restringe a GUID/domínio
        # (sem aspas/espaços) para impedir OData injection.
        if v is None:
            return v
        val = v.strip()
        if not val:
            return None
        import re

        if not re.fullmatch(r"[A-Za-z0-9.\-]+", val):
            raise ValueError(
                "identificador inválido — use apenas letras, números, '.' e '-' "
                "(GUID ou domínio do tenant)"
            )
        return val

    @field_validator("entra_authority")
    @classmethod
    def _check_authority(cls, v):
        # Anti-SSRF / exfiltração de secret: o POST /test envia o client_secret
        # para {authority}/{tenant}/oauth2/v2.0/token. Como authority é gravável
        # via PUT, sem allowlist um admin (ou PAT comprometido com user.manage)
        # poderia apontar para host interno (169.254.169.254) ou externo e vazar
        # a credencial. Restringe a hosts de login Microsoft conhecidos + HTTPS.
        if v is None:
            return v
        val = v.strip()
        if not val:
            return None
        from urllib.parse import urlparse

        allowed_hosts = {
            "login.microsoftonline.com",
            "login.microsoftonline.us",
            "login.partner.microsoftonline.cn",
            "login.microsoftonline.de",
        }
        parsed = urlparse(val)
        if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
            raise ValueError(
                "entra_authority deve ser HTTPS e um host Microsoft conhecido "
                f"({sorted(allowed_hosts)})"
            )
        return f"https://{parsed.hostname}"


class IdentityConnectionTestResult(BaseModel):
    ok: bool
    detail: str


# ── schemas do Graph-sync de usuarios do Entra ──────────────

class EntraSyncTriggerResult(BaseModel):
    """Resposta do POST /identity/config/sync."""
    queued: bool
    message: str
    lock_active: bool


class EntraSyncSummary(BaseModel):
    """Resumo estruturado de um ciclo de sync (subset do dict interno da task)."""
    created: int = 0
    updated: int = 0
    deactivated: int = 0
    errors: List[str] = Field(default_factory=list)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class EntraSyncStatus(BaseModel):
    """Resposta do GET /identity/config/sync-status."""
    last_sync_at: Optional[datetime] = None
    last_sync_status: Optional[str] = None   # 'ok'|'error'|'running'|'never'|None
    last_sync_summary: Optional[EntraSyncSummary] = None
    lock_active: bool = False


class LoginRequest(BaseModel):
    username: str
    password: str

    @field_validator("username", "password")
    @classmethod
    def validate_required_login_fields(cls, v: str) -> str:
        value = v.strip()
        if not value:
            raise ValueError("field must not be empty")
        return value


class BootstrapAdminRequest(LoginRequest):
    display_name: Optional[str] = None

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        value = v.strip()
        return value or None


class SessionUserRead(BaseModel):
    id: str
    username: str
    email: Optional[str] = None
    display_name: Optional[str] = None
    # "local" (senha) ou "entra" (federado).
    auth_provider: str = "local"
    # Escopo global de leitura (vê todas as orgs sem ser admin).
    is_global: bool = False
    organization_id: Optional[int] = None
    organization_name: Optional[str] = None
    role: UserRole
    is_active: bool
    permissions: List[str] = Field(default_factory=list)
    # Preferência de idioma da UI: "pt"/"en"/"es" ou None
    # (seguir o navegador). O SPA usa isto como prioridade máxima de detecção.
    locale: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class LocaleUpdate(BaseModel):
    """Corpo de PUT /auth/me/locale — o idioma escolhido no seletor do SPA."""

    locale: str = Field(pattern="^(pt|en|es)$")


class LoginResponse(BaseModel):
    user: SessionUserRead
    expires_at: datetime


class UserCreate(BaseModel):
    username: str
    password: str
    email: Optional[str] = None
    display_name: Optional[str] = None
    organization_id: Optional[int] = None
    role: UserRole = "user"
    # Concede escopo global de leitura (analista de SOC interno). Só admins
    # chamam este endpoint, então não há risco de auto-elevação.
    is_global: bool = False

    @field_validator("username", "password")
    @classmethod
    def validate_required_user_fields(cls, v: str) -> str:
        value = v.strip()
        if not value:
            raise ValueError("field must not be empty")
        return value

    @field_validator("email")
    @classmethod
    def normalize_user_email(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        value = v.strip().lower()
        if not value:
            return None
        if "@" not in value:
            raise ValueError("invalid email")
        return value

    @field_validator("display_name")
    @classmethod
    def normalize_user_display_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        value = v.strip()
        return value or None


class UserUpdate(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None
    email: Optional[str] = None
    display_name: Optional[str] = None
    organization_id: Optional[int] = None
    role: Optional[UserRole] = None
    is_global: Optional[bool] = None
    is_active: Optional[bool] = None

    @field_validator("username", "password")
    @classmethod
    def validate_optional_user_field(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        value = v.strip()
        return value or None

    @field_validator("email")
    @classmethod
    def normalize_optional_email(cls, v: Optional[str]) -> Optional[str]:
        # ``None`` = campo não enviado; ``""`` = intenção de limpar o e-mail.
        # O router distingue via ``model_fields_set``.
        if v is None:
            return None
        value = v.strip().lower()
        if not value:
            return None
        if "@" not in value:
            raise ValueError("invalid email")
        return value

    @field_validator("display_name")
    @classmethod
    def normalize_optional_display_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        value = v.strip()
        return value or None


class UserRead(SessionUserRead):
    created_at: datetime
    updated_at: datetime
    last_login_at: Optional[datetime] = None


class AccountProfileRead(SessionUserRead):
    """Visão do próprio perfil para a página de conta (self-service).

    Superset de ``SessionUserRead`` com carimbos úteis ao usuário (desde quando
    a conta existe, último acesso). NUNCA inclui ``password_hash`` nem
    ``external_subject`` — só o que é seguro o dono ver sobre si mesmo."""

    created_at: datetime
    last_login_at: Optional[datetime] = None


class SelfProfileUpdate(BaseModel):
    """Corpo de ``PATCH /auth/me`` — o que o usuário pode alterar em SI MESMO.

    Lista de permissão EXPLÍCITA (defesa contra mass-assignment): só estes três
    campos existem. ``role``/``organization_id``/``is_global``/``is_active``/
    ``auth_provider`` NÃO têm representação aqui, então não há como um usuário
    se auto-elevar ou trocar de tenant por este endpoint. Trocar ``email`` é
    sensível e exige reautenticação (``current_password``) no router; contas
    federadas (``auth_provider != 'local'``) têm ``email`` derivado do IdP e o
    router recusa a alteração."""

    display_name: Optional[str] = None
    email: Optional[str] = None
    locale: Optional[str] = None
    # Reautenticação para mudanças sensíveis (troca de e-mail). Nunca é
    # persistido — só verificado contra o hash atual e descartado.
    current_password: Optional[str] = None

    @field_validator("display_name")
    @classmethod
    def normalize_self_display_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        value = v.strip()
        return value or None

    @field_validator("email")
    @classmethod
    def normalize_self_email(cls, v: Optional[str]) -> Optional[str]:
        # ``None`` = campo não enviado; ``""`` = intenção de limpar o e-mail.
        # O router distingue via ``model_fields_set``.
        if v is None:
            return None
        value = v.strip().lower()
        if not value:
            return None
        if "@" not in value:
            raise ValueError("invalid email")
        return value

    @field_validator("locale")
    @classmethod
    def validate_self_locale(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        if v not in ("pt", "en", "es"):
            raise ValueError("invalid locale")
        return v


class PasswordChange(BaseModel):
    """Corpo de ``POST /auth/me/password`` — troca de senha pelo próprio dono.

    Exige a senha ATUAL (reautenticação anti-sequestro-de-sessão) e uma senha
    nova; a força é validada no router (mesma política do cadastro)."""

    current_password: str
    new_password: str

    @field_validator("current_password", "new_password")
    @classmethod
    def require_non_empty(cls, v: str) -> str:
        value = (v or "").strip()
        if not value:
            raise ValueError("field must not be empty")
        return value


# ── Search / Query ────────────────────────────────────────────────────

class QueryRequest(BaseModel):
    """SQL query for the XDR Query API."""
    statement: str
    from_: str
    to: str


# ── History ───────────────────────────────────────────────────────────

class HistoryRead(BaseModel):
    id: int
    client_id: int | None = None
    operation: str
    endpoint: str
    payload: Optional[str] = None
    response_summary: Optional[str] = None
    timestamp: datetime

    model_config = ConfigDict(from_attributes=True)


class AuditLogRead(BaseModel):
    id: int
    user_id: int | None = None
    username: Optional[str] = None
    user_role: Optional[UserRole] = None
    action: str
    endpoint: str
    method: Optional[str] = None
    status_code: Optional[int] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    request_payload: Optional[str] = None
    detail: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SearchResultRead(BaseModel):
    id: int
    search_id: str
    client_id: int | None = None
    schedule_id: int | None = None
    status: str
    result_json: Optional[str] = None
    statement: str
    table: str
    from_ts: str
    to_ts: str
    engine: str = "query"
    language: str = "sql"
    error_message: Optional[str] = None
    result_count: Optional[int] = None
    created_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


# ── Predefined Queries ────────────────────────────────────────────────

class PredefinedQueryBase(BaseModel):
    title: str
    description: Optional[str] = None
    statement: str
    table: str = "xdr_data"
    client_ids: Optional[List[int]] = None
    # Auditoria multi-tenant: dono da query. None → o servidor resolve (org do
    # criador escopado, ou derivada dos client_ids p/ admin global). Admin global
    # pode direcionar explicitamente a uma org.
    organization_id: Optional[int] = None


class PredefinedQueryCreate(PredefinedQueryBase):
    pass


class PredefinedQueryUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    statement: Optional[str] = None
    table: Optional[str] = None
    client_ids: Optional[List[int]] = None


class PredefinedQueryRead(PredefinedQueryBase):
    id: int

    model_config = ConfigDict(from_attributes=True)


# ── Scheduled Queries ─────────────────────────────────────────────────

class ScheduledQueryBase(BaseModel):
    query_id: int
    client_ids: List[int]
    interval_value: int = Field(default=1, gt=0)
    interval_unit: ScheduleTimeUnit = "hours"
    lookback_value: int = Field(default=1, gt=0, validation_alias=AliasChoices("lookback_value", "days_back"))
    lookback_unit: ScheduleTimeUnit = "days"
    notify_on_results: bool = False

    @field_validator("interval_unit", "lookback_unit", mode="before")
    @classmethod
    def normalize_schedule_unit(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip().lower()
        return v

    @field_validator("client_ids")
    @classmethod
    def validate_client_ids(cls, v: List[int]) -> List[int]:
        normalized: List[int] = []
        seen: set[int] = set()

        for raw_client_id in v:
            client_id = int(raw_client_id)
            if client_id <= 0:
                raise ValueError("client_ids must contain positive integers")
            if client_id not in seen:
                seen.add(client_id)
                normalized.append(client_id)

        if not normalized:
            raise ValueError("at least one client must be selected")

        return normalized


class ScheduledQueryCreate(ScheduledQueryBase):
    pass


class ScheduledQueryRead(ScheduledQueryBase):
    id: int
    organization_id: Optional[int] = None
    days_back: int = 1
    query_title: Optional[str] = None
    next_run: datetime
    last_run_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


# ── Email ─────────────────────────────────────────────────────────────

class NotificationEmailBase(BaseModel):
    email: str


class NotificationEmailCreate(NotificationEmailBase):
    # org do destinatário. Admin org-scoped herda a
    # própria; admin global pode direcionar a uma org específica (None = sistema).
    organization_id: Optional[int] = None


class NotificationEmailRead(NotificationEmailBase):
    id: int
    organization_id: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)


class EmailConfigBase(BaseModel):
    smtp_host: str
    smtp_port: int = 25
    smtp_user: str | None = None
    use_tls: bool = False
    sender: str = "noreply@example.com"

    @field_validator("smtp_host", "smtp_user", "sender")
    @classmethod
    def normalize_email_config_text(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        value = v.strip()
        return value or None


class EmailConfigUpdate(BaseModel):
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_user: str | None = None
    smtp_password: str | None = None
    use_tls: bool | None = None
    sender: str | None = None
    clear_smtp_password: bool = False

    @field_validator("smtp_host", "smtp_user", "smtp_password", "sender")
    @classmethod
    def normalize_optional_email_config_text(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        value = v.strip()
        return value or None


class EmailConfigRead(EmailConfigBase):
    id: int
    smtp_password_configured: bool = False

    model_config = ConfigDict(from_attributes=True)


# ── Block Actions ─────────────────────────────────────────────────────

_SHA256_RE = re.compile(r"^[a-fA-F0-9]{64}$")


class BlockedAddressItem(BaseModel):
    item: str = Field(
        validation_alias=AliasChoices("item", "address"),
        serialization_alias="item",
    )
    comment: str = ""
    expireInDays: Optional[int] = None

    @field_validator("item")
    @classmethod
    def validate_address(cls, v: str) -> str:
        import ipaddress

        addr = v.strip()
        if not addr:
            raise ValueError("item must not be empty")
        try:
            if "-" in addr:
                start_raw, end_raw = [part.strip() for part in addr.split("-", 1)]
                if not start_raw or not end_raw:
                    raise ValueError("Invalid IP range format")
                start_ip = ipaddress.ip_address(start_raw)
                end_ip = ipaddress.ip_address(end_raw)
                if start_ip.version != end_ip.version:
                    raise ValueError("IP range must use same IP version on both sides")
                if int(start_ip) > int(end_ip):
                    raise ValueError("IP range start must be less than or equal to end")
            elif "/" in addr:
                ipaddress.ip_network(addr, strict=False)
            else:
                ipaddress.ip_address(addr)
        except ValueError:
            raise ValueError(f"Invalid IP address, CIDR or range: {addr}")
        return addr

    @field_validator("comment")
    @classmethod
    def validate_comment(cls, v: str) -> str:
        comment = v.strip()
        if len(comment) > 250:
            raise ValueError("comment must contain at most 250 characters")
        return comment

    @field_validator("expireInDays", mode="before")
    @classmethod
    def normalize_expiration_days(cls, v: Any) -> Optional[int]:
        if v is None or v == "":
            return None
        if isinstance(v, bool):
            raise ValueError("expireInDays must be an integer between 1 and 365")
        try:
            days = int(float(v))
        except (TypeError, ValueError):
            raise ValueError("expireInDays must be an integer between 1 and 365")
        if days < 1 or days > 365:
            raise ValueError("expireInDays must be between 1 and 365")
        return days


class BlockIPsBulkRequest(BaseModel):
    client_ids: List[int]
    items: List[BlockedAddressItem]

    @field_validator("client_ids")
    @classmethod
    def at_least_one_client(cls, v: List[int]) -> List[int]:
        if not v:
            raise ValueError("At least one client_id is required")
        return v

    @field_validator("items")
    @classmethod
    def at_least_one_ip(cls, v: List[BlockedAddressItem]) -> List[BlockedAddressItem]:
        if not v:
            raise ValueError("At least one item is required")
        return v


class BlockedHashItem(BaseModel):
    sha256: str
    comment: str = ""

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, v: str) -> str:
        v = v.strip().lower()
        if not _SHA256_RE.match(v):
            raise ValueError(f"Invalid SHA256 hash: {v}")
        return v


class BlockItemsBulkRequest(BaseModel):
    client_ids: List[int]
    items: List[BlockedHashItem]

    @field_validator("client_ids")
    @classmethod
    def at_least_one_client(cls, v: List[int]) -> List[int]:
        if not v:
            raise ValueError("At least one client_id is required")
        return v

    @field_validator("items")
    @classmethod
    def at_least_one_hash(cls, v: List[BlockedHashItem]) -> List[BlockedHashItem]:
        if not v:
            raise ValueError("At least one item is required")
        return v


class ClientActionResult(BaseModel):
    client_id: int
    client_name: str
    ok: List[Dict[str, Any]]
    failed: List[Dict[str, Any]]


class BulkActionResponse(BaseModel):
    action_id: int
    clients: List[ClientActionResult]


class ActionRunRead(BaseModel):
    id: int
    action_type: str
    client_ids: List[int]
    status: str
    result_summary: Optional[str] = None
    created_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


# ── Collector Multi-Tenant ────────────────────────────────────────────


class CollectorVendorRead(BaseModel):
    """Descoberta de vendors registrados no registry do collector."""

    platform: str
    stream: str
    queue: str
    task_name: str
    schedule_seconds: int


class PlatformStreamsResponse(BaseModel):
    """Map agregado ``platform → [streams]`` para auto-discovery no frontend.

    Eliminação do hardcode ``PLATFORM_STREAMS`` em ``BackfillForm`` etc.
    Derivado em runtime do registry — adicionar vendor novo é zero-edit
    no frontend.
    """

    platforms: Dict[str, List[str]]


class CollectionStateRead(BaseModel):
    """Estado persistido de coleta por (integration, stream)."""

    integration_id: int
    integration_name: Optional[str] = None
    organization_id: Optional[int] = None
    organization_name: Optional[str] = None
    platform: Optional[str] = None
    stream: str
    cursor: Optional[Dict[str, Any]] = None
    last_success_at: Optional[datetime] = None
    last_attempt_at: Optional[datetime] = None
    # Instante do FORNECEDOR até onde o cursor consumiu. É a ÚNICA medida de
    # atraso que não mente: ``last_success_at`` é reescrito com ``agora`` a cada
    # ciclo sem erro, mesmo quando o ciclo processou o dia anterior.
    #
    # ``None`` quando não medível (cursor não temporal, ou nada coletado ainda) —
    # a tela deve OMITIR a coluna nesse caso, nunca renderizar 0.
    watermark_at: Optional[datetime] = None
    # O último ciclo parou no teto de páginas ⇒ sobrou trabalho para o próximo.
    # Só junto com ``watermark_at`` atrasado é que caracteriza backlog: um stream
    # sem eventos mantém o watermark legitimamente parado.
    last_run_capped: bool = False
    last_error: Optional[str] = None
    consecutive_failures: int = 0
    events_collected_total: int = 0
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class CollectorSummary(BaseModel):
    """KPIs agregados exibidos no topo da página do collector."""

    integrations_tracked: int = 0
    vendors_registered: int = 0
    events_collected_total: int = 0
    integrations_with_errors: int = 0
    stale_minutes_max: Optional[int] = None
    per_platform: List[Dict[str, Any]] = Field(default_factory=list)


class CollectorTriggerResponse(BaseModel):
    """Retorno do endpoint manual trigger."""

    task_id: str
    queue: str
    integration_id: int
    stream: str


# ── Filtros de coleta (descarte empurrado para a origem) ───────────────


class CollectionFilterFieldRead(BaseModel):
    """Declaração de um filtro de coleta, como a UI a recebe.

    Serializa ``collectors.registry.CollectionFilterField``. É o que torna a tela
    plugin-driven: um vendor que declare filtros novos aparece renderizado sem
    tocar em router nem em frontend.

    ``default`` é sempre o valor que NÃO filtra nada — a UI usa isso para saber
    quando o controle está desligado e para oferecer "voltar ao padrão".
    ``warning_text`` precisa ser exibido ANTES de o operador ligar o filtro: o que
    é filtrado na origem nunca entra na plataforma (não aparece no drift, nem na
    captura ao vivo, e não fica disponível para uma rota futura).
    """

    key: str
    label: str
    type: Literal["int_range", "enum", "bool"]
    default: Any = None
    min: Optional[int] = None
    max: Optional[int] = None
    options: Optional[List[str]] = None
    help_text: Optional[str] = None
    warning_text: Optional[str] = None

    @classmethod
    def from_field(cls, field: Any) -> "CollectionFilterFieldRead":
        """Converte um ``CollectionFilterField`` do registry.

        Existe para o catálogo (``/providers/platforms``) e a leitura por
        integração compartilharem UMA serialização — duas cópias divergiriam e a
        UI passaria a ver contratos diferentes para o mesmo campo.
        """
        return cls(
            key=field.key,
            label=field.label,
            type=field.type,
            default=field.default,
            min=field.min,
            max=field.max,
            options=list(field.options) if field.options else None,
            help_text=field.help_text,
            warning_text=field.warning_text,
        )


class IntegrationCollectionFiltersRead(BaseModel):
    """Filtros de coleta de UMA integração: o que está gravado + o que é possível.

    ``filters`` tem o mesmo shape que o PUT aceita (``{stream: {key: valor}}``),
    então a tela edita o que leu e devolve. Traz só o que o coletor vai de fato
    aplicar: valor gravado que não valida mais contra o plugin é omitido, porque
    o coletor também o ignora — ecoá-lo mostraria uma redução que não acontece.

    ``available_filters`` é o schema efetivo da plataforma desta integração, para
    a tela não ter de cruzar com ``GET /providers/platforms``. Stream que não
    declara filtro não aparece.
    """

    integration_id: int
    platform: str
    filters: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    available_filters: Dict[str, List[CollectionFilterFieldRead]] = Field(default_factory=dict)


class IntegrationCollectionFiltersUpdate(BaseModel):
    """Body do PUT — SUBSTITUI toda a configuração de filtros da integração.

    Não é merge: stream ausente do corpo perde os filtros que tinha, e ``{}``
    limpa tudo. É o que faz "voltar ao padrão" funcionar sem um endpoint de
    delete separado.
    """

    filters: Dict[str, Dict[str, Any]] = Field(default_factory=dict)


# ── Collector Config (runtime settings gerenciáveis via UI) ────────────


DispatchMode = Literal["syslog", "jsonl", "both"]
# formato syslog. rfc3164 = Wazuh JSON_Decoder compatível (default).
# rfc5424 = legado para SIEMs que suportam STRUCTURED-DATA.
SyslogFormat = Literal["rfc3164", "rfc5424"]


def _validate_rate_limits(value: Dict[str, Dict[str, int]]) -> Dict[str, Dict[str, int]]:
    """Valida estrutura e ranges de ``rate_limits_by_vendor``.

    Formato esperado:
    ``{"<vendor>": {"per_second": N, "per_minute": N, "per_hour": N}}``
    Todos inteiros 0..100000. Pelo menos uma chave de janela é obrigatória.
    """
    if not isinstance(value, dict):
        raise ValueError("rate_limits_by_vendor deve ser um objeto")
    allowed = {"per_second", "per_minute", "per_hour", "per_day"}
    for vendor, limits in value.items():
        if not isinstance(limits, dict):
            raise ValueError(f"rate_limits_by_vendor.{vendor} deve ser objeto")
        if not limits:
            raise ValueError(f"rate_limits_by_vendor.{vendor} está vazio")
        for window, qty in limits.items():
            if window not in allowed:
                raise ValueError(
                    f"rate_limits_by_vendor.{vendor}.{window}: janela inválida "
                    f"(use per_second/per_minute/per_hour/per_day)"
                )
            if not isinstance(qty, int) or qty < 0 or qty > 100_000:
                raise ValueError(
                    f"rate_limits_by_vendor.{vendor}.{window} deve ser int 0..100000"
                )
    return value


def _validate_domain_limits(value: Dict[str, int]) -> Dict[str, int]:
    """Valida ``domain_concurrency_limits``: {"<vendor>": int 1..1000}."""
    if not isinstance(value, dict):
        raise ValueError("domain_concurrency_limits deve ser um objeto")
    for vendor, qty in value.items():
        if not isinstance(qty, int) or qty < 1 or qty > 1000:
            raise ValueError(
                f"domain_concurrency_limits.{vendor} deve ser int 1..1000"
            )
    return value


class CollectorConfigBase(BaseModel):
    """Campos comuns entre leitura e escrita (defaults de UI)."""

    # Destino Wazuh
    wazuh_syslog_host: Optional[str] = None
    wazuh_syslog_port: int = Field(default=514, ge=1, le=65535)
    wazuh_syslog_use_tls: bool = False
    wazuh_ca_bundle: Optional[str] = None
    wazuh_dispatch_mode: DispatchMode = "syslog"
    # rfc3164 é o novo default (Wazuh JSON_Decoder).
    # rfc5424 preservado para configs legadas — não quebra quem estava em prod.
    wazuh_syslog_format: SyslogFormat = "rfc3164"
    collector_jsonl_dir: str = "/var/log/centralops/collectors"

    # Batching / dedupe
    collector_batch_size: int = Field(default=200, ge=1, le=10_000)
    collector_batch_flush_seconds: int = Field(default=5, ge=1, le=600)
    # alinhado a ``collectors.config_loader.DEFAULT_DEDUPE_TTL_DAYS``.
    # Travado contra divergência em ``backend/tests/test_dedupe_ttl_invariant.py``.
    dedupe_ttl_days: int = Field(default=1, ge=1, le=365)
    # TTL CANÔNICO em segundos. Piso de 4h = 4x o visibility_timeout do broker
    # (ver state/dedupe.MIN_TTL_SECONDS e test_dedupe_ttl_invariant): abaixo
    # disso uma claim órfã expira antes de o broker desistir de redeliverar.
    # Existe porque `dias` não expressa 4h, e o TTL é a alavanca direta sobre o
    # keyspace do Redis (chaves ~= EPS x TTL). None = deriva de dedupe_ttl_days.
    dedupe_ttl_seconds: Optional[int] = Field(default=None, ge=14_400, le=2_678_400)

    # Mapas JSON
    domain_concurrency_limits: Dict[str, int] = Field(default_factory=dict)
    rate_limits_by_vendor: Dict[str, Dict[str, int]] = Field(default_factory=dict)

    @field_validator("domain_concurrency_limits")
    @classmethod
    def _check_domain_limits(cls, v):
        return _validate_domain_limits(v)

    @field_validator("rate_limits_by_vendor")
    @classmethod
    def _check_rate_limits(cls, v):
        return _validate_rate_limits(v)


class CollectorConfigUpdate(BaseModel):
    """Partial update — todos os campos opcionais."""

    wazuh_syslog_host: Optional[str] = None
    wazuh_syslog_port: Optional[int] = Field(default=None, ge=1, le=65535)
    wazuh_syslog_use_tls: Optional[bool] = None
    wazuh_ca_bundle: Optional[str] = None
    wazuh_dispatch_mode: Optional[DispatchMode] = None
    wazuh_syslog_format: Optional[SyslogFormat] = None
    collector_jsonl_dir: Optional[str] = None

    collector_batch_size: Optional[int] = Field(default=None, ge=1, le=10_000)
    collector_batch_flush_seconds: Optional[int] = Field(default=None, ge=1, le=600)
    dedupe_ttl_days: Optional[int] = Field(default=None, ge=1, le=365)

    domain_concurrency_limits: Optional[Dict[str, int]] = None
    rate_limits_by_vendor: Optional[Dict[str, Dict[str, int]]] = None

    @field_validator("domain_concurrency_limits")
    @classmethod
    def _check_domain_limits(cls, v):
        if v is None:
            return v
        return _validate_domain_limits(v)

    @field_validator("rate_limits_by_vendor")
    @classmethod
    def _check_rate_limits(cls, v):
        if v is None:
            return v
        return _validate_rate_limits(v)


class CollectorConfigRead(CollectorConfigBase):
    """Resposta da API. Inclui meta info (``is_persisted``, ``config_version``)."""

    id: int = 1
    is_persisted: bool = True
    config_version: str = ""
    updated_at: Optional[datetime] = None


class CollectorConfigTestResult(BaseModel):
    """Resultado de teste por componente (syslog ou jsonl)."""

    component: Literal["syslog", "jsonl"]
    status: Literal["healthy", "error", "skipped"]
    details: Dict[str, Any] = Field(default_factory=dict)


class CollectorConfigTestResponse(BaseModel):
    """Retorno do endpoint ``POST /api/collectors/config/test``."""

    mode: DispatchMode
    results: List[CollectorConfigTestResult] = Field(default_factory=list)


# ── Captura ao vivo / "listening" (sessões de captura) ─────────


class CaptureSessionStartRequest(BaseModel):
    """Inicia uma sessão de captura escopada (opcionalmente) a um vendor."""

    vendor: Optional[str] = None
    duration_seconds: int = Field(default=300, ge=1, le=3600)
    ring_size: int = Field(default=5000, ge=1, le=20000)


class CaptureSession(BaseModel):
    id: str
    organization_id: Optional[int] = None
    vendor: Optional[str] = None
    created_at: Optional[float] = None
    expires_at: Optional[float] = None
    status: str  # active | stopped | expired
    event_count: int = 0


class CaptureSessionList(BaseModel):
    count: int
    sessions: List[CaptureSession] = Field(default_factory=list)


class CaptureEvent(BaseModel):
    event: dict = Field(default_factory=dict)
    vendor: Optional[str] = None
    captured_at: Optional[float] = None


class CaptureEventList(BaseModel):
    count: int
    session_id: str
    events: List[CaptureEvent] = Field(default_factory=list)


# ── Personal Access Tokens (PAT) ──────────


class ApiTokenCreate(BaseModel):
    """Payload de criação de PAT.

    Owner XOR:
      - ``service_account_id = None`` → PAT pessoal (atrelado ao
        ``current_user`` que está chamando).
      - ``service_account_id = N`` → token de Service Account; user
        logado precisa de USER_MANAGE pra criar.

    ``expires_at = None`` exige ``is_eternal=True`` explicitamente
    (opt-in pra evitar credenciais infinitas por acidente).

    ``scopes``: lista de Permission. ``None`` ou ``[]`` = full inherit
    da role do owner (legacy).
    """

    name: str
    expires_at: Optional[datetime] = None
    is_eternal: bool = False
    service_account_id: Optional[int] = None
    scopes: Optional[List[str]] = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        value = v.strip()
        if not value:
            raise ValueError("name must not be empty")
        if len(value) > 100:
            raise ValueError("name must be at most 100 characters")
        return value


class ApiTokenRead(BaseModel):
    """Visão segura de um PAT (sem hash, sem raw token).

    Usado em listagem e responses não-sensíveis.
    """

    id: int
    name: str
    token_prefix: str
    expires_at: Optional[datetime] = None
    is_eternal: bool = False
    last_used_at: Optional[datetime] = None
    last_used_ip: Optional[str] = None
    use_count: int
    revoked_at: Optional[datetime] = None
    created_at: datetime
    # null se PAT pessoal, set se token de Service Account.
    user_id: Optional[int] = None
    service_account_id: Optional[int] = None
    # array de Permission scopes; vazio = full inherit.
    scopes: List[str] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class ApiTokenCreateResponse(BaseModel):
    """Response de POST /api/v1/tokens.

    O campo ``token`` é o **raw plaintext** — exibido **uma única vez**
    pra UI e nunca persistido em logs ou retornado novamente.
    """

    token: str
    api_token: ApiTokenRead


# ── Service Accounts (credencial machine-to-machine) ───────


class ServiceAccountCreate(BaseModel):
    """Payload de POST /api/v1/service-accounts.

    Apenas USER_MANAGE pode criar. ``role`` define o teto de permissões
    dos PATs vinculados a este SA.
    """

    name: str
    description: Optional[str] = None
    role: str = "viewer"
    organization_id: Optional[int] = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        value = v.strip()
        if not value:
            raise ValueError("name must not be empty")
        if len(value) > 100:
            raise ValueError("name must be at most 100 characters")
        # Limita a charset seguro pra evitar ambiguidade no audit log
        # (que prefixa "sa:<name>").
        if not all(c.isalnum() or c in "-_." for c in value):
            raise ValueError(
                "name must be alphanumeric (also allowed: '-', '_', '.')"
            )
        return value

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        from ..core.auth import VALID_ROLES

        if v not in VALID_ROLES:
            raise ValueError(
                f"role must be one of {sorted(VALID_ROLES)}"
            )
        return v


class ServiceAccountUpdate(BaseModel):
    """Payload de PATCH /api/v1/service-accounts/{id}.

    Tudo opcional — só campos enviados são atualizados. ``role`` muda
    o teto de permissões dos tokens existentes (não os revoga).
    ``is_active=False`` = desativa o SA (tokens param de funcionar).
    """

    description: Optional[str] = None
    role: Optional[str] = None
    organization_id: Optional[int] = None
    is_active: Optional[bool] = None

    @field_validator("role")
    @classmethod
    def validate_role_optional(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        from ..core.auth import VALID_ROLES

        if v not in VALID_ROLES:
            raise ValueError(
                f"role must be one of {sorted(VALID_ROLES)}"
            )
        return v


class ServiceAccountRead(BaseModel):
    """Visão pública de um Service Account."""

    id: int
    name: str
    description: Optional[str] = None
    role: str
    organization_id: Optional[int] = None
    is_active: bool
    created_by_user_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    # Quantos PATs ativos (não-revogados) o SA tem hoje. Útil pra UI
    # decidir se permite delete sem prompt extra.
    active_token_count: int = 0

    model_config = ConfigDict(from_attributes=True)
