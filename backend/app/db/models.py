from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text as _sa_text,
)
from sqlalchemy.orm import relationship
from datetime import datetime
from uuid import uuid4

from .database import Base


# ── Multi-integration domain models ──────────────────────────────────

class Organization(Base):
    """A customer/company that can have multiple integrations."""
    __tablename__ = "organizations"
    __table_args__ = (
        UniqueConstraint(
            "external_provider", "external_id",
            name="uq_organization_provider_external",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)
    slug = Column(String, unique=True, nullable=False, index=True)
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Sophos Partner auto-onboarding (kind=partner spawns child Organizations)
    external_provider = Column(String, nullable=True, index=True)  # "sophos"
    external_id = Column(String, nullable=True, index=True)  # Sophos tenant UUID
    auto_managed = Column(Boolean, nullable=False, default=False, server_default=_sa_text("false"))
    iris_customer_id = Column(Integer, nullable=True, index=True)
    partner_integration_id = Column(
        Integer,
        ForeignKey(
            "integrations.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_organizations_partner_integration",
        ),
        nullable=True,
        index=True,
    )

    # ── hierarquia de tenants (árvore) ─────────────────────────
    # Aresta EXPLÍCITA org→org (substitui o 2-hop via partner_integration_id como
    # FONTE da verdade da hierarquia; partner_integration_id segue vivo só p/
    # herança de creds). NULL = raiz sob a plataforma. RESTRICT: não deleta um pai
    # com filhos (reparent/delete primeiro) — invariante anti-órfão.
    parent_organization_id = Column(
        Integer,
        ForeignKey(
            "organizations.id",
            ondelete="RESTRICT",
            use_alter=True,
            name="fk_organizations_parent",
        ),
        nullable=True,
        index=True,
    )
    # root_id denormalizado = raiz da subárvore (= si mesmo p/ raízes). Eixo de
    # subtree-scoping O(1) no caso 1-nível (``WHERE root_id = :r``) e de
    # sharding/residência. SET NULL no delete p/ não auto-bloquear a
    # deleção de uma raiz-folha (que referencia a si mesma). Populado pelo serviço
    # ``db.hierarchy``; NULL é lido como "sou minha própria raiz".
    root_id = Column(
        Integer,
        ForeignKey(
            "organizations.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_organizations_root",
        ),
        nullable=True,
        index=True,
    )
    # Guard de profundidade da árvore (0 = raiz).
    depth = Column(Integer, nullable=False, default=0, server_default=_sa_text("0"))
    # 'customer' (folha, default) | 'reseller' (ramo com filhos). Conceder
    # 'reseller' é privilégio do dono da plataforma, não self-service.
    kind = Column(
        String,
        nullable=False,
        default="customer",
        server_default="customer",
        index=True,
    )

    integrations = relationship(
        "Integration",
        back_populates="organization",
        cascade="all, delete-orphan",
        foreign_keys="Integration.organization_id",
    )
    users = relationship("AppUser", back_populates="organization")
    retention_config = relationship(
        "OrganizationRetentionConfig",
        back_populates="organization",
        uselist=False,
        cascade="all, delete-orphan",
    )
    partner_integration = relationship(
        "Integration",
        foreign_keys=[partner_integration_id],
        post_update=True,
    )
    customer_mappings = relationship(
        "DestinationCustomerMapping",
        back_populates="organization",
        cascade="all, delete-orphan",
    )


# ── tenant-hierarchy tables carved out to the Enterprise package ─────────
# ``OrgClosure`` (org_closure), ``OrgRoleBinding`` (org_role_bindings) and
# ``PartnerProgram`` (partner_programs) are the NUCLEAR multi-tenant/reseller feature.
# Under the open-core split they live ONLY in ``centralops_ee.models`` (on the separate
# ``BaseEE`` MetaData). The Community artifact must NOT ship them — the ``org_closure``
# etc. tables are created by ``centralops_ee.migrate`` on Enterprise deploys. The
# hierarchy COLUMNS on ``Organization`` (parent_organization_id/root_id/depth/kind) stay
# here, inert in Community (FLAT: root=self, depth=0) — see
# ``db.hierarchy.materialize_node``.


class DestinationCustomerMapping(Base):
    """mapeia uma Organization ao seu "customer id" EXTERNO num
    destino/ferramenta de IR/SOAR (IRIS, TheHive, Splunk SOAR, Cortex XSOAR).

    Tira a identidade do IRIS do HOT PATH de entrega: o envelope usa o
    ``Organization.id`` INTERNO; o id externo vive aqui e é resolvido APENAS na
    BORDA do connector daquele destino (não no core do pipeline). Generaliza o
    antigo ``Organization.iris_customer_id`` (agora deprecado) para qualquer
    sistema downstream — "external customer id por destino".

    Unicidade por (organization_id, destination_kind): uma org tem no máximo um
    customer id por sistema externo. ``external_customer_id`` é String para
    cobrir IDs não-inteiros (TheHive/Cortex usam strings; IRIS usa inteiro,
    armazenado como string).
    """

    __tablename__ = "destination_customer_mappings"
    __table_args__ = (
        # Uma org tem no máximo um customer id por sistema externo.
        UniqueConstraint(
            "organization_id",
            "destination_kind",
            name="uq_dest_customer_org_kind",
        ),
        # ...e um customer id externo pertence a no máximo UMA org por destino —
        # torna a resolução inversa (external_id → org) INEQUÍVOCA (sem cruzar
        # tenant por colisão de id).
        UniqueConstraint(
            "destination_kind",
            "external_customer_id",
            name="uq_dest_customer_kind_extid",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # "iris" | "thehive" | "splunk_soar" | "cortex_xsoar" | ...
    destination_kind = Column(String, nullable=False, index=True)
    external_customer_id = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    organization = relationship("Organization", back_populates="customer_mappings")


class Integration(Base):
    """A connection to a security platform (Sophos, Wazuh, etc.) within an organization."""
    __tablename__ = "integrations"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String, nullable=False)
    platform = Column(String, nullable=False)  # "sophos" | "wazuh"
    is_active = Column(Boolean, nullable=False, default=True)

    # Hierarchy / Partner mode (Sophos Partner spawns child Integrations)
    # kind: "tenant" (default — single-cred per integration), "partner", "organization"
    kind = Column(String, nullable=False, default="tenant", server_default="tenant", index=True)
    parent_integration_id = Column(
        Integer,
        ForeignKey("integrations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # External identity returned by /whoami/v1 (partner_id when kind=partner, tenant_id when kind=tenant)
    external_id = Column(String, nullable=True, index=True)
    id_type = Column(String, nullable=True)  # echo of Sophos /whoami idType
    data_geography = Column(String, nullable=True)  # Sophos field (US, EU, ...)
    last_tenant_sync_at = Column(DateTime, nullable=True)  # only for kind=partner
    tenant_sync_status = Column(String, nullable=True)  # "ok" | "partial" | "error"
    auto_managed = Column(Boolean, nullable=False, default=False, server_default=_sa_text("false"))
    # Sophos Partner — política de descoberta de novos tenants. Só relevante
    # quando ``kind in ("partner", "organization")``. ``False`` (default seguro)
    # quer dizer: sync vai gravar tenants novos como ``state='pending'`` e o
    # operador escolhe quais habilitar via UI. ``True`` materializa cada
    # tenant descoberto automaticamente (comportamento legado).
    #
    # ATENÇÃO: usar ``server_default=text("false")`` em vez de literal string
    # ``"false"``. SQLAlchemy passa string-literal como TEXT em SQLite — o
    # storage retorna o literal ``'false'`` (Python truthy!) em vez do
    # boolean coerced. ``text()`` força SQL keyword sem aspas, então o
    # SQLite trata como TRUE/FALSE token e o ORM converte direito.
    auto_approve_new_tenants = Column(
        Boolean, nullable=True, default=False, server_default=_sa_text("false")
    )

    # Sophos-specific fields
    client_id = Column(String, nullable=True)
    client_secret = Column(String, nullable=True)
    region = Column(String, nullable=True)
    # Generic vendor base URL (ex.: NinjaOne ``https://app.ninjarmm.com``). Sophos
    # deriva o host via ``api_host``/``region``; vendors novos declaram ``base_url``
    # nos auth_fields do capability model. ``tenant_id`` (abaixo) também
    # é reutilizado de forma genérica (ex.: Microsoft Defender / Azure tenant).
    base_url = Column(String, nullable=True)
    # Source of truth for the API hostname. When populated, collectors and
    # services use it verbatim (``f"https://{api_host}/..."``) instead of
    # deriving via ``f"api-{region}.central.sophos.com"``. Necessary because
    # Sophos ``/partner/v1/tenants`` returns ``apiHost`` (e.g.
    # ``https://api-eu03.central.sophos.com``) while ``dataRegion`` is a
    # geo code (``EU``/``US``) — deriving from the latter produced
    # ``api-EU.central.sophos.com`` which doesn't exist in DNS.
    api_host = Column(String, nullable=True)
    tenant_id = Column(String, nullable=True)  # legacy — superseded by external_id when kind=tenant
    access_token = Column(String, nullable=True)
    refresh_token = Column(String, nullable=True)
    # config NÃO-secreta de vendor que não cabe em coluna fixa (JSON).
    # Ex.: lake = {layout, prefix, source}. auth_fields não-coluna/não-secret caem
    # aqui no create (antes eram descartados silenciosamente). Nunca guarda segredo.
    config_json = Column(Text, nullable=True)

    # Wazuh-specific fields
    manager_url = Column(String, nullable=True)  # e.g. https://wazuh-manager:55000
    indexer_url = Column(String, nullable=True)  # e.g. https://wazuh-indexer:9200
    manager_api_username = Column(String, nullable=True)  # encrypted
    manager_api_password = Column(String, nullable=True)  # encrypted
    indexer_username = Column(String, nullable=True)  # encrypted
    indexer_password = Column(String, nullable=True)  # encrypted
    # Legacy shared credentials kept temporarily for backward-compatible migration.
    api_username = Column(String, nullable=True)  # encrypted
    api_password = Column(String, nullable=True)  # encrypted
    verify_ssl = Column(Boolean, nullable=True, default=True)
    auth_status = Column(String, nullable=False, default="unknown")
    last_checked_at = Column(DateTime, nullable=True)
    last_successful_check_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    organization = relationship(
        "Organization",
        back_populates="integrations",
        foreign_keys=[organization_id],
    )
    health_checks = relationship("IntegrationHealthCheck", back_populates="integration", cascade="all, delete-orphan")
    # Sem ``cascade="all, delete-orphan"`` em ``search_results`` e
    # ``histories`` — a política de FK é ``SET NULL`` (preservação
    # forense). Cascade ORM removeria as rows antes do DB aplicar a
    # rule, anulando o intent (vide ``docs/runbooks/fk-cascade-policy.md``).
    search_results = relationship("SearchResult", back_populates="integration", foreign_keys="SearchResult.integration_id")
    histories = relationship("History", back_populates="integration")
    parent = relationship(
        "Integration",
        remote_side="Integration.id",
        foreign_keys=[parent_integration_id],
        backref="children",
    )
    # storage de segredo vendor-neutro. ``lazy="selectin"`` carrega
    # os segredos JUNTO da integração — sobrevive a ``db.expunge`` (providers leem
    # creds com a row detached). cascade delete-orphan: apagar a integração apaga
    # os segredos. Vendors legados (sophos/wazuh) ainda usam as colunas.
    credentials = relationship(
        "IntegrationCredential",
        back_populates="integration",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    @property
    def is_authenticated(self) -> bool:
        return self.auth_status in {"healthy", "degraded"}

    def _has_active_secret(self, logical_name: str) -> bool:
        """Presence-check de um segredo ATIVO no store ``integration_credentials``.

        Inline (sem importar ``services.integration_secrets``) para não criar o
        ciclo models ↔ services. Lê da relationship ``credentials`` (lazy=selectin),
        então funciona mesmo com a row detached."""
        return any(
            c.logical_name == logical_name and c.revoked_at is None
            for c in (self.credentials or ())
        )

    @property
    def manager_credentials_configured(self) -> bool:
        # deriva do store (corte limpo — sem fallback api_* legado).
        return self._has_active_secret("manager_api_username") and self._has_active_secret("manager_api_password")

    @property
    def indexer_credentials_configured(self) -> bool:
        return self._has_active_secret("indexer_username") and self._has_active_secret("indexer_password")


class IntegrationCredential(Base):
    """Segredo de credencial de integração, vendor-neutro.

    Um segredo LÓGICO por linha (``logical_name`` único por integração):
    ``client_secret``, ``access_token``, ``refresh_token``, ou creds exóticas
    (``aws_secret_access_key``, ``gcp_sa_json``, …). O ciphertext fica em
    ``secret_ref`` (via ``core.crypto.encrypt`` — Vault-aware quando
    ``KMS_PROVIDER=vault_transit``; a master key nunca toca o processo). Espelha o
    lifecycle de credencial de ``Destination`` (``secret_version``/``rotated_at``/
    ``revoked_at``) — habilita rotação/revogação/auditoria por segredo.

    Vendors OAuth genéricos (ninjaone/defender/…) usam esta tabela. Sophos/Wazuh
    seguem nas colunas batizadas (quando o código é reescrito)."""

    __tablename__ = "integration_credentials"
    __table_args__ = (
        UniqueConstraint(
            "integration_id", "logical_name", name="uq_integration_credential"
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    integration_id = Column(
        Integer,
        ForeignKey("integrations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Nome lógico do segredo (= auth_field.key): "client_secret" | "access_token" | …
    logical_name = Column(String, nullable=False)
    # Ciphertext inline (mesmo formato/backend de Destination.secret_ref).
    secret_ref = Column(String, nullable=False)
    # Versão da master key do KMS (rotação Transit) — opcional, p/ re-encrypt.
    key_version = Column(String, nullable=True)
    # Lifecycle (espelha Destination): incrementa a cada rotate; revoga sem apagar.
    secret_version = Column(Integer, nullable=False, default=1, server_default=_sa_text("1"))
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    rotated_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)

    integration = relationship("Integration", back_populates="credentials")


class CollectionState(Base):
    """Cursor/checkpoint por (integration, stream) para coleta incremental.

    Fonte da verdade persistente do estado da coleta. Redis guarda o hot
    path (``collection:cursor:{integration_id}:{stream}``) para leitura de
    baixa latência; esta tabela garante retomada idempotente após restart
    do Redis ou do próprio worker (RF02, RNF01).
    """

    __tablename__ = "collection_state"
    __table_args__ = (
        UniqueConstraint(
            "integration_id", "stream", name="uq_collection_state_int_stream"
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    integration_id = Column(
        Integer,
        ForeignKey("integrations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # "alerts" | "detections" | "incidents" | "activities" — semântica do
    # cursor é opaca ao banco; cada collector interpreta.
    stream = Column(String, nullable=False)
    cursor = Column(Text, nullable=True)  # JSON string
    last_success_at = Column(DateTime, nullable=True)
    last_attempt_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    consecutive_failures = Column(Integer, nullable=False, default=0)
    events_collected_total = Column(Integer, nullable=False, default=0)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class IntegrationHealthCheck(Base):
    """Periodic health/status snapshot for an integration."""
    __tablename__ = "integration_health_checks"

    id = Column(Integer, primary_key=True, index=True)
    integration_id = Column(
        Integer,
        ForeignKey("integrations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status = Column(String, nullable=False)  # "healthy" | "degraded" | "error" | "unknown"
    details = Column(Text, nullable=True)  # JSON with provider-specific info
    checked_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    integration = relationship("Integration", back_populates="health_checks")


class IntegrationTenantSelection(Base):
    """Estado de seleção de cada tenant descoberto sob um Partner/Organization Sophos.

    Roda 1:1 com cada tenant retornado em ``GET /partner/v1/tenants``. O sync
    materializa apenas children cujo ``state='approved'``. Estados:

      * ``pending``  — descoberto mas ainda não aprovado pelo operador.
      * ``approved`` — child Integration deve existir/permanecer ativo.
      * ``excluded`` — operador escolheu não monitorar; child fica
        ``is_active=False`` (soft-delete) preservando histórico.

    Snapshots (``name_snapshot``, ``region_snapshot``, ``api_host_snapshot``,
    ``data_geography_snapshot``) servem pra UI exibir o tenant mesmo quando
    o último sync é antigo, sem refazer chamada ao Sophos. ``last_seen_at``
    indica a última vez que o tenant apareceu no payload do partner — quando
    fica defasado e o tenant não está mais no payload, a UI mostra ``stale``.
    """

    __tablename__ = "integration_tenant_selections"
    __table_args__ = (
        UniqueConstraint(
            "parent_integration_id", "external_id",
            name="uq_tenant_selection_parent_external",
        ),
        Index(
            "ix_tenant_selection_parent_state",
            "parent_integration_id", "state",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    parent_integration_id = Column(
        Integer,
        ForeignKey("integrations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    external_id = Column(String, nullable=False)  # UUID Sophos do tenant
    state = Column(String, nullable=False)  # 'pending' | 'approved' | 'excluded'

    decided_by_user_id = Column(
        Integer,
        ForeignKey("app_users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    decided_at = Column(DateTime, nullable=True)

    # Snapshots do último sync — UI exibe sem refetch ao Sophos.
    name_snapshot = Column(String, nullable=True)
    region_snapshot = Column(String, nullable=True)
    data_geography_snapshot = Column(String, nullable=True)
    api_host_snapshot = Column(String, nullable=True)
    last_seen_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class AppUser(Base):
    __tablename__ = "app_users"
    __table_args__ = (
        # Garante 1 conta por sujeito de IdP externo (o ``oid``/``sub`` do
        # Entra). Contas locais têm ``external_subject=NULL`` e múltiplos NULL
        # não conflitam (Postgres e SQLite), então N contas locais convivem.
        UniqueConstraint(
            "auth_provider", "external_subject",
            name="uq_app_users_provider_subject",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(String, unique=True, nullable=False, index=True, default=lambda: str(uuid4()))
    username = Column(String, unique=True, nullable=False, index=True)
    # E-mail corporativo. Nullable porque contas locais legadas não têm; passa
    # a ser o identificador casado com o claim do IdP (OIDC/SCIM).
    email = Column(String, nullable=True, unique=True, index=True)
    display_name = Column(String, nullable=True)
    # Origem da identidade: "local" (senha) ou "entra" (federado).
    auth_provider = Column(String, nullable=False, default="local", server_default="local")
    # Subject imutável no IdP externo (Entra ``oid``/``sub``). NULL p/ locais.
    external_subject = Column(String, nullable=True, index=True)
    # Nullable: contas federadas (auth_provider != "local") não têm senha.
    password_hash = Column(String, nullable=True)
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    role = Column(String, nullable=False, default="user")
    # Escopo global de leitura: quando True, o usuário enxerga TODAS as
    # organizations com as permissões da sua role (analista de SOC interno),
    # sem ser admin. Ver core/tenant.has_global_scope.
    is_global = Column(Boolean, nullable=False, default=False, server_default=_sa_text("false"))
    is_active = Column(Boolean, nullable=False, default=True)
    # Preferência de idioma da UI: "pt"/"en"/"es". NULL = seguir
    # o Accept-Language do navegador. Sincronizada pelo seletor de idioma do SPA.
    locale = Column(String, nullable=True)
    last_login_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    sessions = relationship("UserSession", back_populates="user", cascade="all, delete-orphan")
    organization = relationship("Organization", back_populates="users")
    histories = relationship("History", back_populates="user")
    audit_logs = relationship("AuditLog", back_populates="user")
    search_results = relationship("SearchResult", back_populates="user")
    api_tokens = relationship(
        "ApiToken",
        back_populates="user",
        cascade="all, delete-orphan",
        foreign_keys="ApiToken.user_id",
    )


class UserSession(Base):
    __tablename__ = "user_sessions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer,
        ForeignKey("app_users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash = Column(String, unique=True, nullable=False, index=True)
    user_agent = Column(String, nullable=True)
    expires_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime, nullable=True)
    last_seen_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("AppUser", back_populates="sessions")


class OidcAuthState(Base):
    """Estado efêmero do fluxo OIDC (state/nonce/PKCE) entre /sso/login e
    /sso/callback. Uso único, TTL curto; consumido no callback e limpo por
    housekeeping. Ver core/oidc.py e routers/sso.py."""

    __tablename__ = "oidc_auth_states"

    id = Column(Integer, primary_key=True, index=True)
    state = Column(String, unique=True, nullable=False, index=True)
    nonce = Column(String, nullable=False)
    code_verifier = Column(String, nullable=False)
    redirect_to = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)


class IdentityConfig(Base):
    """Singleton (id=1) de configuração de identidade/SSO operada pela UI.

    Mesmo padrão de ``CollectorConfig``: o ``.env`` (ENTRA_*) é só seed da
    primeira subida; depois o admin edita pela UI (/config → Identidade & SSO)
    e o backend lê via ``core.identity_config``. O ``client_secret`` é
    armazenado **cifrado** (``core.crypto.encrypt``) e nunca devolvido em claro.
    """

    __tablename__ = "identity_config"

    id = Column(Integer, primary_key=True)

    # ── Microsoft Entra — OIDC ──────────────────────────────────────
    entra_enabled = Column(Boolean, nullable=False, default=False)
    entra_tenant_id = Column(String, nullable=True)
    entra_client_id = Column(String, nullable=True)
    entra_client_secret = Column(String, nullable=True)  # cifrado (enc::...)
    entra_redirect_uri = Column(String, nullable=True)
    entra_authority = Column(
        String, nullable=False, default="https://login.microsoftonline.com"
    )
    entra_scopes = Column(String, nullable=False, default="openid profile email")
    # JSON serializado (compat SQLite/Postgres).
    entra_role_map = Column(Text, nullable=False, default="{}")
    entra_default_role = Column(String, nullable=False, default="viewer")
    entra_default_is_global = Column(Boolean, nullable=False, default=False)
    entra_jit_provisioning = Column(Boolean, nullable=False, default=True)
    entra_allowed_email_domains = Column(Text, nullable=False, default="[]")  # JSON list
    entra_button_label = Column(
        String, nullable=False, default="Entrar com Microsoft"
    )
    entra_post_login_redirect = Column(String, nullable=False, default="/")

    # ── Graph-sync de usuarios ────────────────────────────────
    # Toggle de sync periodico via Graph. Falso por padrao (opt-in).
    entra_sync_enabled = Column(Boolean, nullable=False, default=False, server_default=_sa_text("false"))
    # Quando True, desativa contas que saem do App Registration.
    entra_sync_deprovision = Column(Boolean, nullable=False, default=True, server_default=_sa_text("true"))
    # Timestamp e status do ultimo sync. Nulos ate o primeiro sync.
    entra_last_sync_at = Column(DateTime, nullable=True)
    entra_last_sync_status = Column(String, nullable=True)   # 'ok'|'error'|'running'|'never'
    entra_last_sync_summary = Column(Text, nullable=True)    # JSON serializado do dict de resultado

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


class LicenseConfig(Base):
    """Singleton (id=1) da licença Enterprise ATIVADA pela UI.

    O token assinado (EdDSA) é persistido **cifrado** (``core.crypto.encrypt``) e é a
    FONTE DA VERDADE lida pelo resolver de edição (``core.edition`` → DB-first, com
    fallback p/ env/arquivo). Por-DEPLOY (não por-org), mesmo padrão de
    ``IdentityConfig``. A chave PRIVADA nunca vive aqui — só o keyring público verifica.
    """

    __tablename__ = "license_config"

    id = Column(Integer, primary_key=True)
    # Token JWT assinado, cifrado em repouso (enc::... / kmsenc::...). NULL = sem licença.
    license_token = Column(Text, nullable=True)
    activated_by = Column(String, nullable=True)   # admin que ativou (e-mail/username)
    activated_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


class History(Base):
    __tablename__ = "history"

    id = Column(Integer, primary_key=True, index=True)
    integration_id = Column(
        Integer,
        ForeignKey("integrations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    user_id = Column(
        Integer,
        ForeignKey("app_users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    operation = Column(String, nullable=False)
    endpoint = Column(String, nullable=False)
    payload = Column(Text)
    response_summary = Column(Text)
    timestamp = Column(DateTime, default=datetime.utcnow)

    integration = relationship("Integration", back_populates="histories")
    user = relationship("AppUser", back_populates="histories")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer,
        ForeignKey("app_users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    username = Column(String, nullable=True)
    user_role = Column(String, nullable=True)
    action = Column(String, nullable=False)
    endpoint = Column(String, nullable=False)
    method = Column(String, nullable=True)
    status_code = Column(Integer, nullable=True)
    ip_address = Column(String, nullable=True)
    user_agent = Column(Text, nullable=True)
    request_payload = Column(Text, nullable=True)
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("AppUser", back_populates="audit_logs")


class SearchResult(Base):
    __tablename__ = "search_results"

    id = Column(Integer, primary_key=True, index=True)
    search_id = Column(String, unique=True, nullable=False)
    integration_id = Column(
        Integer,
        ForeignKey("integrations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    user_id = Column(
        Integer,
        ForeignKey("app_users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    schedule_id = Column(
        Integer,
        ForeignKey("scheduled_queries.id", ondelete="SET NULL"),
        nullable=True,
    )
    platform = Column(String, nullable=True)  # "sophos" | "wazuh"
    statement = Column(Text, nullable=False)
    table = Column(String, nullable=False)
    from_ts = Column(String, nullable=False)
    to_ts = Column(String, nullable=False)
    status = Column(String, nullable=False)
    result_json = Column(Text)
    engine = Column(String, nullable=False, default="query")
    language = Column(String, nullable=False, default="sql")
    error_message = Column(Text, nullable=True)
    result_count = Column(Integer, nullable=True)
    # index=True: retenção por data faz DELETE WHERE created_at < cutoff. O índice
    # nomeia ``ix_search_results_created_at``
    # — o MESMO que a migração lightweight cria em DBs existentes (consistência
    # model↔schema; guarda ``test_autogenerate_is_clean``).
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    # Versão do mapping OCSF aplicado ao resultado (anti-drift; populada de
    # ``QueryCapability.ocsf_mapping_version`` no writer).
    ocsf_mapping_version = Column(String, nullable=True)
    # org_id fail-closed: toda linha de resultado carrega a org
    # (derivada da integração no insert). nullable p/ linhas legadas.
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # Liga a linha (por-fonte) ao ``QueryJob`` federado pai.
    query_job_id = Column(
        Integer,
        ForeignKey("query_jobs.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    integration = relationship("Integration", back_populates="search_results", foreign_keys=[integration_id])
    user = relationship("AppUser", back_populates="search_results")
    query_job = relationship("QueryJob", back_populates="results")


class QueryJob(Base):
    """Job store durável de uma query federada ao vivo.

    Pai de N ``SearchResult`` (1 por fonte consultada). Submete-se via
    ``POST /query-jobs`` (retorna ``job_id`` e LIBERA o request) e poll-a via
    ``GET /query-jobs/{job_id}`` — o trabalho roda num worker da fila DEDICADA
    ``collect.query`` (nunca na ``collect.bulk`` da ingestão).

    ``organization_id`` é fail-closed: todo job carrega a org; o
    fan-out nunca cruza tenant. Estado: ``submitted`` → ``running`` →
    ``finished`` | ``partial`` | ``failed``."""

    __tablename__ = "query_jobs"

    id = Column(Integer, primary_key=True, index=True)
    # uuid público opaco (o que vaza na URL/poll — não o PK sequencial).
    job_id = Column(String, unique=True, nullable=False, index=True)
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(
        Integer,
        ForeignKey("app_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    dialect = Column(String, nullable=False)            # ex.: "opensearch_dsl"
    statement = Column(Text, nullable=False)            # nativo executado (pós-tradução)
    # forma do statement submetido (passthrough|sigma) + o original
    # ANTES da tradução central (provenance/debug de hunts ad-hoc Sigma; o nativo
    # executado fica em ``statement``). nullable p/ rows legadas.
    spec_kind = Column(String, nullable=True, default="passthrough")
    original_statement = Column(Text, nullable=True)
    from_ts = Column(String, nullable=False)
    to_ts = Column(String, nullable=False)
    # JSON list dos integration_id alvo (fan-out). Mesmo org (validado no submit).
    integration_ids = Column(Text, nullable=False)
    allow_partial_results = Column(Boolean, nullable=False, default=False)
    status = Column(String, nullable=False, default="submitted")
    total_results = Column(Integer, nullable=False, default=0)
    # JSON {integration_id: {status, count, error, partial}} — facets por fonte.
    per_source = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    finished_at = Column(DateTime, nullable=True)

    results = relationship(
        "SearchResult", back_populates="query_job", foreign_keys="SearchResult.query_job_id"
    )


class Detection(Base):
    """Alerta de detecção de 1ª classe.

    Substitui o alerta best-effort syslog Critical FIXO de scheduled query por um
    registro DURÁVEL, org-scoped (fail-closed), com severidade
    configurável (não mais fixa em Critical) e dedup por janela de supressão.
    Populado por ambos os caminhos: ``scheduled_query`` e ``correlation``.

    Dedup LÓGICO por ``(organization_id, dedup_key)``: um match repetido DENTRO da
    ``suppression_window_seconds`` BUMPA ``count``+``last_seen`` em vez de criar um
    novo alerta (anti-spam). Sem UniqueConstraint — após a janela, um novo alerta é
    legítimo."""

    __tablename__ = "detections"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    # "scheduled_query" | "live_query" | "correlation" | "inflight"
    # ``inflight`` (ADR-0015 Fase 1) = classificação single-event no pipeline de
    # ingestão, emitida ANTES de o dado chegar ao SIEM. Difere de ``correlation``
    # (multi-evento, ao final de uma busca federada) na origem e na latência.
    source = Column(String, nullable=False)
    source_query_id = Column(
        Integer, ForeignKey("predefined_queries.id", ondelete="SET NULL"), nullable=True
    )
    integration_id = Column(
        Integer, ForeignKey("integrations.id", ondelete="SET NULL"), nullable=True
    )
    dialect = Column(String, nullable=True)
    rule_id = Column(String, nullable=True)
    rule_name = Column(String, nullable=True)
    # OCSF severity_id (0..6/99). Default high (4) — configurável, NÃO fixo em
    # Critical (5) como era o alerta legado (severidade derivada de regra).
    severity_id = Column(Integer, nullable=False, default=4)
    status = Column(String, nullable=False, default="open")  # open | ack | closed
    dedup_key = Column(String, nullable=False)
    suppression_window_seconds = Column(Integer, nullable=False, default=3600)
    first_seen = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_seen = Column(DateTime, default=datetime.utcnow, nullable=False)
    count = Column(Integer, nullable=False, default=1)
    # Link ao evento normalizado / SearchResult que originou a detecção.
    search_result_id = Column(
        Integer, ForeignKey("search_results.id", ondelete="SET NULL"), nullable=True
    )
    ocsf_ref = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("ix_detections_org_dedup", "organization_id", "dedup_key"),
        Index("ix_detections_org_created", "organization_id", "created_at"),
    )


class CorrelationRule(Base):
    """Regra de correlação cross-source.

    Avalia os resultados (cross-source) de uma query federada e emite um
    ``Detection(source="correlation")`` de 1ª classe quando o padrão dispara.
    org-scoped fail-closed. MVP = tipo ``threshold`` ("≥ ``min_count`` eventos
    casando ``where``, agrupados por ``group_by_field``, dentro de
    ``window_seconds``"); ``rule_type`` deixa espaço p/ sequence/aggregation."""

    __tablename__ = "correlation_rules"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    # OCSF severity_id da Detection emitida (configurável, não fixo).
    severity_id = Column(Integer, nullable=False, default=4)
    rule_type = Column(String, nullable=False, default="threshold")  # threshold | (futuro)
    # ADR-0015 Fase 1 — DISCRIMINADOR DE EXECUÇÃO. "batch" (default,
    # preserva o comportamento de toda regra existente) = avaliada pelo
    # CorrelationService ao final de uma busca federada. "inflight" = avaliada
    # POR EVENTO no pipeline de ingestão, antes de o dado chegar ao SIEM.
    #
    # Em modo "inflight" os campos de agregação são IGNORADOS — ``rule_type``,
    # ``min_count``, ``window_seconds`` e ``timestamp_field`` não têm sentido
    # sobre um único evento — e ``group_by_field`` muda de papel: deixa de
    # agrupar para virar o SELETOR DA CHAVE DE DEDUP da Detection emitida
    # (NULL ⇒ uma Detection por regra por janela de supressão). ``where_json``
    # aceita ``in``/``nin``/``exists`` SÓ neste modo.
    eval_mode = Column(String, nullable=False, default="batch")  # batch | inflight
    # ── threshold ────────────────────────────────────────────────────────
    # Campo (dotted path) p/ agrupar (ex.: "agent.name", "host", "data.srcip").
    group_by_field = Column(String, nullable=True)
    # Mínimo de eventos no grupo (dentro da janela) p/ disparar.
    min_count = Column(Integer, nullable=False, default=5)
    # Janela deslizante (s) sobre ``timestamp_field``; 0 ⇒ conta todos do grupo.
    window_seconds = Column(Integer, nullable=False, default=300)
    # Campo de timestamp do evento (dotted path) p/ a janela deslizante.
    timestamp_field = Column(String, nullable=True)
    # JSON list de filtros {field, op, value} (op: eq|ne|contains|gt|lt|gte|lte).
    where_json = Column(Text, nullable=True)
    # Janela de supressão da Detection emitida (anti-spam; herda do dedup).
    suppression_window_seconds = Column(Integer, nullable=False, default=3600)
    created_by_user_id = Column(
        Integer, ForeignKey("app_users.id", ondelete="SET NULL"), nullable=True,
    )
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class PredefinedQuery(Base):
    __tablename__ = "predefined_queries"

    id = Column(Integer, primary_key=True, index=True)
    # title NÃO é mais unique GLOBAL (colidia entre tenants) — a
    # unicidade é por (organization_id, title) via __table_args__ abaixo.
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    statement = Column(Text, nullable=False)
    table = Column(String, nullable=False, default="xdr_data")
    client_ids = Column(Text, nullable=True)  # comma separated ids (→ query_target, futuro)
    # dialeto do statement (↔ QueryCapability.dialect) + forma do
    # statement (passthrough | sigma | ocsf_queryspec). nullable p/ rows legadas.
    dialect = Column(String, nullable=True)
    spec_kind = Column(String, nullable=True, default="passthrough")
    # Auditoria multi-tenant: dono do recurso. Usuário escopado (não-global)
    # só vê/edita queries da própria org; NULL = global (visível apenas a
    # admin/is_global). Nullable p/ reconciliar create_all com a migração leve.
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    __table_args__ = (
        UniqueConstraint("organization_id", "title", name="uq_predefined_queries_org_title"),
    )


class ScheduledQuery(Base):
    __tablename__ = "scheduled_queries"

    id = Column(Integer, primary_key=True, index=True)
    query_id = Column(
        Integer,
        ForeignKey("predefined_queries.id", ondelete="CASCADE"),
        nullable=False,
    )
    client_ids = Column(Text, nullable=False)
    interval_minutes = Column(Integer, nullable=False)
    interval_value = Column(Integer, nullable=True)
    interval_unit = Column(String, nullable=True)
    days_back = Column(Integer, nullable=False, default=1)
    lookback_value = Column(Integer, nullable=False, default=1)
    lookback_unit = Column(String, nullable=False, default="days")
    notify_on_results = Column(Boolean, nullable=False, default=False)
    # index=True: o tick varre next_run<=now a cada 60s — sem índice
    # é full scan. ``ix_scheduled_queries_next_run`` (mesmo nome da migração lightweight).
    next_run = Column(DateTime, nullable=False, index=True)
    last_run_at = Column(DateTime, nullable=True)
    # ── org fail-closed + estado de saúde do schedule ────────────────────
    # CONVERGÊNCIA: UM único
    # ``organization_id`` serve aos dois — escopo de leitura/delete (não-global só
    # vê os da própria org) E fail-closed do alerta (sem org → roteia GLOBAL).
    # ``SET NULL``: org deletada não cascateia o schedule.
    # nullable: linhas antigas viram org NULL (visível só a admin/is_global);
    # backfillado da 1ª integração no run e setado no create.
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Saúde: hoje ``next_run`` avançava INCONDICIONAL → schedule morto (token expirado,
    # 429 crônico) "pulava" e PARECIA rodar. Agora o sucesso reseta; a falha incrementa
    # ``consecutive_failures`` + status (healthy/degraded/failing) — dead schedule VISÍVEL.
    consecutive_failures = Column(Integer, nullable=True, default=0)
    last_error = Column(Text, nullable=True)
    last_error_at = Column(DateTime, nullable=True)
    status = Column(String, nullable=True, default="healthy")  # healthy|degraded|failing
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class NotificationEmail(Base):
    __tablename__ = "notification_emails"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, nullable=False)
    # destinatário é ESCOPADO por org. Resultado de
    # scheduled query do tenant X só vai para e-mails do tenant X — sem isto a
    # lista era GLOBAL e vazava contagem/nome de integração entre tenants.
    # nullable: e-mail "de sistema" (teste/auth global) não recebe resultado de
    # query org-específica (escopo estrito fecha o leak).
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )


class EmailConfig(Base):
    __tablename__ = "email_config"

    id = Column(Integer, primary_key=True, index=True)
    smtp_host = Column(String, nullable=False, default="localhost")
    smtp_port = Column(Integer, nullable=False, default=25)
    smtp_user = Column(String, nullable=True)
    smtp_password = Column(String, nullable=True)
    use_tls = Column(Boolean, nullable=False, default=False)
    sender = Column(String, nullable=False, default="noreply@example.com")


class ActionRun(Base):
    """Audit log for block-actions and other bulk operations."""
    __tablename__ = "action_runs"

    id = Column(Integer, primary_key=True, index=True)
    action_type = Column(String, nullable=False)  # "block_ip" | "block_item" | "query"
    client_ids = Column(Text, nullable=False)  # JSON array of client IDs
    payload = Column(Text, nullable=False)  # JSON of the original request
    status = Column(String, nullable=False, default="pending")  # "pending" | "completed" | "partial" | "failed"
    result_summary = Column(Text, nullable=True)  # JSON with per-client results
    created_at = Column(DateTime, default=datetime.utcnow)


# ── Threat Intel Middleware ─────────────────────────────────────────

class ThreatIntelConfig(Base):
    """Singleton global de configuração do Threat Intel. Linha id=1."""
    __tablename__ = "threat_intel_config"

    id = Column(Integer, primary_key=True)
    enabled = Column(Boolean, nullable=False, default=True)
    cache_ttl_days = Column(Integer, nullable=False, default=7)
    blacklist_update_interval_seconds = Column(Integer, nullable=False, default=3600)
    blacklist_confidence_minimum = Column(Integer, nullable=False, default=80)
    blacklist_limit = Column(Integer, nullable=False, default=10000)
    abuseipdb_max_age_days = Column(Integer, nullable=False, default=30)
    threat_score_critical = Column(Integer, nullable=False, default=80)
    threat_score_high = Column(Integer, nullable=False, default=40)
    otx_pulse_high = Column(Integer, nullable=False, default=5)
    external_timeout_seconds = Column(Integer, nullable=False, default=5)
    last_blacklist_refresh_at = Column(DateTime, nullable=True)
    last_blacklist_size = Column(Integer, nullable=True)
    last_blacklist_error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class ThreatIntelApiKey(Base):
    """Pool de chaves de API com rotação e tracking de cota."""
    __tablename__ = "threat_intel_api_keys"

    id = Column(Integer, primary_key=True, index=True)
    provider = Column(String, nullable=False, index=True)  # "abuseipdb" | "otx"
    label = Column(String, nullable=True)
    api_key = Column(String, nullable=False)  # encrypt() via core/crypto.py
    is_active = Column(Boolean, nullable=False, default=True)
    exhausted_until = Column(DateTime, nullable=True)
    last_used_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    requests_count = Column(Integer, nullable=False, default=0)
    exhausted_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class ThreatIntelToken(Base):
    """Bearer tokens dedicados (Graylog). Plaintext exibido apenas na criação."""
    __tablename__ = "threat_intel_tokens"

    id = Column(Integer, primary_key=True, index=True)
    label = Column(String, nullable=False)
    token_hash = Column(String, unique=True, nullable=False, index=True)  # sha256
    token_prefix = Column(String, nullable=False)  # primeiros 8 chars (referência UI)
    is_active = Column(Boolean, nullable=False, default=True)
    last_used_at = Column(DateTime, nullable=True)
    requests_count = Column(Integer, nullable=False, default=0)
    created_by = Column(
        Integer,
        ForeignKey("app_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    revoked_at = Column(DateTime, nullable=True)


class ThreatIntelQuery(Base):
    """Histórico de consultas para UI de auditoria. Pruned por retention."""
    __tablename__ = "threat_intel_queries"

    id = Column(Integer, primary_key=True, index=True)
    ip_address = Column(String, nullable=False, index=True)
    tier = Column(String, nullable=False)  # "tier0" | "tier1" | "tier2" | "private" | "disabled"
    threat_level = Column(String, nullable=False)  # CRITICAL | HIGH | LOW | SAFE
    otx_pulse_count = Column(Integer, nullable=True)
    abuse_score = Column(Integer, nullable=True)
    abuse_country = Column(String, nullable=True)
    abuse_usage_type = Column(String, nullable=True)
    response_time_ms = Column(Integer, nullable=True)
    quota_exceeded = Column(Boolean, nullable=False, default=False)
    token_id = Column(
        Integer,
        ForeignKey("threat_intel_tokens.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_ip = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


# ── Collector subsystem config ──────────────────────────────────────

class CollectorConfig(Base):
    """Singleton de configuração do subsistema Collector. Linha id=1.

    Migra do ``.env`` para o banco os parâmetros **runtime-mutáveis** do
    Collector. Workers leem via ``backend.app.collectors.config_loader``
    (cache Redis 30s + fallback a ``settings``). O ``.env`` permanece só
    como seed inicial (populado em ``database._run_lightweight_migrations``).

    Não migram: ``APP_MASTER_KEY``, ``DATABASE_URL``, ``REDIS_URL``,
    ``CELERY_BROKER_URL``, ``CELERY_RESULT_BACKEND`` — são lidas no
    import de ``celery_app.py``, antes de existir banco.

    Notas:
    - ``wazuh_syslog_use_tls``: Wazuh Manager vanilla **não** aceita
      Syslog-over-TLS (issue aberta desde 2021). Default ``False`` para
      ir direto em TCP 514; ``True`` apenas quando há stunnel/rsyslog
      intermediário ou SIEM alternativo (Graylog/Splunk) que aceita TLS.
    - ``wazuh_ca_bundle``: path dentro do container; só usado quando
      ``use_tls=True``. Não é secret, não é cifrado.
    - ``domain_concurrency_limits`` e ``rate_limits_by_vendor``: JSON
      serializado (``Text``) para compatibilidade SQLite/Postgres.
    """

    __tablename__ = "collector_config"

    id = Column(Integer, primary_key=True)

    # ── Destino Wazuh ───────────────────────────────────────────────
    wazuh_syslog_host = Column(String, nullable=True)
    wazuh_syslog_port = Column(Integer, nullable=False, default=514)
    wazuh_syslog_use_tls = Column(Boolean, nullable=False, default=False)
    wazuh_ca_bundle = Column(String, nullable=True)
    wazuh_dispatch_mode = Column(String, nullable=False, default="syslog")  # syslog|jsonl|both
    # formato Syslog. RFC 3164 é o novo default (Wazuh-compatible).
    # RFC 5424 mantido como legado para configs existentes em prod (preserva quem usa rfc5424).
    # Linhas existentes no banco recebem 'rfc5424' via ALTER TABLE (idempotente em migration).
    wazuh_syslog_format = Column(
        String, nullable=False, default="rfc3164", server_default="rfc3164"
    )
    collector_jsonl_dir = Column(
        String, nullable=False, default="/var/log/centralops/collectors"
    )

    # ── Batching / dedupe ───────────────────────────────────────────
    collector_batch_size = Column(Integer, nullable=False, default=200)
    collector_batch_flush_seconds = Column(Integer, nullable=False, default=5)
    # ADR-0015: alinhado a ``collectors.config_loader.DEFAULT_DEDUPE_TTL_DAYS``
    # (fonte canônica). O literal é repetido aqui de propósito — ``models`` é
    # importado POR ``config_loader``, então importar de volta seria circular.
    # A divergência entre os dois é travada por
    # ``backend/tests/test_dedupe_ttl_invariant.py``.
    #
    # Na prática este default é quase inerte: o seed de ``collector_config``
    # (singleton id=1) informa o campo explicitamente a partir do env. Ele só
    # valeria num INSERT que omitisse a coluna — e é exatamente aí que a
    # divergência silenciosa moraria.
    dedupe_ttl_days = Column(Integer, nullable=False, default=1)

    # ── JSON-serialized mappings ────────────────────────────────────
    domain_concurrency_limits = Column(Text, nullable=False, default="{}")
    rate_limits_by_vendor = Column(Text, nullable=False, default="{}")

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


# ── Saída desacoplada: destinos & rotas ──────────────────


class Destination(Base):
    """Destino de saída de primeira classe. 1:N.

    Diferente do ``CollectorConfig`` (singleton com batching/dedupe
    **globais**), há N destinos. A config específica do ``kind`` vive em
    ``config`` (JSON validado pelo ``config_schema`` do kind no registry);
    ``delivery`` carrega batch/retry/backpressure/queue; ``secret_ref``
    referencia a credencial no cofre (``core/secrets``) — **NUNCA** o
    segredo em claro. ``config_version`` = sha1(config+delivery) → o
    ``destination_cache`` recria o singleton granularmente quando muda.

    ``organization_id`` NULL = destino global; preenchido = escopado ao
    tenant (multi-tenant/MSSP). Mudanças são auditadas à parte
    (append-only, espelhando ``MappingAuditLog``).

    A migração lightweight materializa ``wazuh-default`` a partir das
    colunas ``wazuh_*`` do ``CollectorConfig`` → caminho Wazuh idêntico. O
    o roteamento por regra é o modelo único de despacho (GA, sem flag): rotas
    em ``routes`` selecionam destinos (first-match), com catch-all ->
    wazuh-default garantindo zero perda.
    """

    __tablename__ = "destinations"

    id = Column(String, primary_key=True, default=lambda: str(uuid4()))
    name = Column(String, nullable=False, unique=True)
    kind = Column(String, nullable=False, index=True)  # chave do DestinationRegistry
    enabled = Column(Boolean, nullable=False, default=True, server_default=_sa_text("true"))
    config = Column(Text, nullable=False, default="{}")  # JSON (config_schema do kind)
    secret_ref = Column(String, nullable=True)  # ref no cofre; nunca o segredo
    delivery = Column(Text, nullable=False, default="{}")  # JSON batch/retry/backpressure
    config_version = Column(String, nullable=False, default="")
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,  # NULL = global
        index=True,
    )
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
    # ── S5: ciclo de vida de credencial ──────────────────────────────
    # ``secret_version`` é incrementado a cada rotate; permite detectar
    # rotações concorrentes (optimistic lock parcial).
    # ``secret_created_at`` é populado na criação via POST /credential/rotate
    # (ou no ``add()`` quando um hec_token é fornecido na criação).
    # ``secret_rotated_at`` registra a última rotação.
    # ``secret_expires_at`` suporta TTL de credencial (futuro: job de aviso).
    # ``secret_revoked_at`` é setado por revoke; distingue expiração de revogação.
    secret_version = Column(Integer, nullable=False, default=1, server_default=_sa_text("1"))
    secret_created_at = Column(DateTime, nullable=True)
    secret_rotated_at = Column(DateTime, nullable=True)
    secret_expires_at = Column(DateTime, nullable=True)
    secret_revoked_at = Column(DateTime, nullable=True)
    # ── data residency ──────────────────────────
    # Localidade de armazenamento declarada pelo operador. Quando preenchido,
    # o engine de roteamento EXCLUI este destino de qualquer fan-out cujo
    # evento carrega uma ``data_geography`` INCOMPATÍVEL com este valor —
    # enforcement conservador (nunca perde dado silenciosamente; cai no
    # fallback wazuh-default se o fan-out ficar vazio).
    # Valores conhecidos (não-exaustivo): "EU" | "US" | "BR" | "global".
    # NULL = sem restrição de residência (comportamento default).
    data_residency = Column(String, nullable=True, index=True)
    # ── vendor-neutro: destino de FALLBACK (catch-all) ────────────
    # Quando True, este destino recebe os eventos que não casam NENHUMA rota
    # (substitui o ``wazuh-default`` hardcoded). No máximo UM default por org
    # (índice único parcial em _run_lightweight_migrations). NULL-org = default
    # GLOBAL (fallback de todas as orgs sem default próprio). Sem nenhum default
    # configurado, não-roteados vão à DLQ/quarentena (zero perda, vendor-neutro).
    is_default = Column(
        Boolean, nullable=False, default=False, server_default=_sa_text("false")
    )


class Route(Base):
    """Regra de roteamento. ``condition`` → destino(s).

    Avaliadas por ``priority`` (menor primeiro), first-match com
    ``is_final``: ON para no match (exclusivo); OFF clona e continua
    (fan-out). ``action='drop'`` descarta (cost-control). ``condition`` é
    label-driven (platform/stream/organization_id/event_type/severity_id/
    vendor) — JSON, sem regex frágil. ``destination_ids`` é um JSON array.

    Sem rotas, tudo vai para ``wazuh-default`` (back-compat).
    """

    __tablename__ = "routes"

    id = Column(String, primary_key=True, default=lambda: str(uuid4()))
    name = Column(String, nullable=False)
    priority = Column(Integer, nullable=False, default=100)
    condition = Column(Text, nullable=False, default="{}")  # JSON label-match
    action = Column(String, nullable=False, default="route")  # "route" | "drop"
    destination_ids = Column(Text, nullable=False, default="[]")  # JSON array (fan-out)
    is_final = Column(Boolean, nullable=False, default=True, server_default=_sa_text("true"))
    # Canary rollout 0-100. 100 = full; <100 aplica a rota só a essa
    # fração determinística (hash do event_id) dos eventos que casam — cutover
    # gradual SIEM→SIEM. O restante cai pra próxima rota.
    canary_percent = Column(Integer, nullable=False, default=100, server_default=_sa_text("100"))
    # fail-safe de detecção. TRUE (default) =
    # esta rota alimenta detecção e NUNCA é amostrada/agregada pelas alavancas de
    # redução, mesmo com REDUCTION_SAMPLE/AGGREGATE ligados. O operador faz opt-out
    # explícito (FALSE) nas rotas onde reduzir volume é seguro. Default-protege: a
    # redução exige decisão consciente por-rota, não o contrário.
    protect_detection = Column(
        Boolean, nullable=False, default=True, server_default=_sa_text("true")
    )
    # sampling estatístico de REDUÇÃO (0-100). 100 (default) = sem
    # amostragem (byte-idêntico). <100 = só essa fração determinística (consistent-hash
    # por event_id) chega aos destinos desta rota. NUNCA aplicado a rotas
    # protect_detection=True. Só tem efeito com REDUCTION_SAMPLE_ENABLED on.
    sample_percent = Column(Integer, nullable=False, default=100, server_default=_sa_text("100"))
    # suppression durável por assinatura (rate-limit Number-to-Allow).
    # suppress_key = CSV de labels p/ a assinatura (ex.: "src_ip,event_type"); NULL/vazio
    # = sem supressão. suppress_allow = quantos passam por janela (0 = desligado, default).
    # suppress_window_s = janela (s). Reduz ruído repetitivo sem perder a 1ª ocorrência.
    suppress_key = Column(Text, nullable=True)
    suppress_allow = Column(Integer, nullable=False, default=0, server_default=_sa_text("0"))
    suppress_window_s = Column(Integer, nullable=False, default=30, server_default=_sa_text("30"))
    transform_ref = Column(String, nullable=True)  # mapping/redação por rota (futuro)
    # redação de PII por rota (JSON declarativo:
    # {"version":1,"rules":[{path,action,...}]}). NULL = sem redação (default,
    # byte-idêntico). A mesma origem chega íntegra no lago e mascarada no SIEM.
    pii_redaction = Column(Text, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True, server_default=_sa_text("true"))
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


class RouteAuditLog(Base):
    """Append-only audit trail of route mutations.

    One row per create/update/delete of a Route, with the FULL route ``snapshot``
    (JSON) at that point. Powers (1) governance (who changed routing, when) and
    (2) granular rollback — restore a route to any prior snapshot. Append-only by
    convention: the app only INSERTs here; it never UPDATEs/DELETEs a row.

    ``route_id`` is a plain String (no FK) so the trail survives the route's
    deletion (forensics). ``organization_id`` CASCADE-deletes with the org.
    """

    __tablename__ = "route_audit_log"

    id = Column(String, primary_key=True, default=lambda: str(uuid4()))
    route_id = Column(String, nullable=False, index=True)
    action = Column(String, nullable=False)  # created | updated | deleted | rolled_back
    actor = Column(String, nullable=True)  # username; nullable for system actions
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    snapshot = Column(Text, nullable=False, default="{}")  # JSON of the route state
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class DestinationDeadLetter(Base):
    """Dead-letter queue for failed destination deliveries.

    Populated by the dispatcher when a delivery attempt is
    permanently rejected (non-retryable). Created now so the schema is
    stable; populated in a later PR.

    ``organization_id`` CASCADE-deletes with the owning Organization so
    data-erasure flows are honoured. ``destination_id`` is a plain String
    (no FK) because destinations can be deleted; the DLQ entry is kept for
    forensics even after the destination is removed.

    The ``(destination_id, event_id)`` UNIQUE constraint enforces E1 dedup at
    the DB level (review LOW): concurrent redeliveries can't create duplicate
    forensic rows — the loser of the race hits IntegrityError and is skipped.
    """

    __tablename__ = "destination_dlq"
    __table_args__ = (
        UniqueConstraint(
            "destination_id", "event_id", name="uq_dest_dlq_dest_event"
        ),
        # pruning/erase por tenant + tempo. Índice composto
        # ``(organization_id, created_at)`` torna eficiente o DELETE/listagem
        # ``WHERE organization_id = ? AND created_at < ?`` (retenção e erase-by-org)
        # sem o custo/lifecycle de particionamento declarativo — estas tabelas são
        # sinks de erro de baixo volume (o hot-path NÃO persiste eventos) e o
        # unique ``(destination_id, event_id)`` é um guard de dedup que RANGE-partition
        # por tempo quebraria (a chave de partição teria de entrar no unique).
        Index("ix_destination_dlq_org_created", "organization_id", "created_at"),
    )

    id = Column(String, primary_key=True, default=lambda: str(uuid4()))
    destination_id = Column(String, nullable=False, index=True)
    event_id = Column(String, nullable=False, index=True)
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    error_kind = Column(String, nullable=False)
    error_detail = Column(Text, nullable=True)
    payload = Column(Text, nullable=True)  # JSON-serialized original event
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class CredentialAccessLog(Base):
    """Append-only audit trail for credential-level access on Destinations (S6).

    Captures every decrypt, test, rotate, and revoke operation so operators
    can answer "who accessed or changed this credential, and when?".

    Design choices (mirroring RouteAuditLog):
      - ``destination_id`` is a plain String (no FK) so the trail survives
        the destination's deletion (forensics).
      - ``organization_id`` CASCADE-deletes with the owning Organization so
        data-erasure / GDPR flows are honoured.
      - ``actor`` is the username string; nullable for system/automated actions.
      - ``action`` is one of: decrypt | test | rotate | revoke.
      - ``detail`` is optional free-form JSON or short text (never contains
        the plaintext credential — only metadata like version numbers or
        IP address).
      - ``created_at`` is indexed for time-range audit queries.

    Append-only by convention: the app only INSERTs here; it never
    UPDATEs/DELETEs a row.
    """

    __tablename__ = "credential_access_log"

    id = Column(String, primary_key=True, default=lambda: str(uuid4()))
    destination_id = Column(String, nullable=False, index=True)
    actor = Column(String, nullable=True)  # username; nullable for system actions
    action = Column(String, nullable=False)  # decrypt | test | rotate | revoke
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    detail = Column(Text, nullable=True)  # free-form JSON metadata (never plaintext)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class DestinationAuditLog(Base):
    """Append-only audit trail of Destination CRUD mutations.

    One row per create/update/delete of a Destination, with the destination
    ``snapshot`` (JSON) at that point — powering enterprise governance ("who
    changed which destination, when"). Mirrors ``RouteAuditLog`` exactly.

    CRITICAL security invariant: the ``snapshot`` NEVER contains the secret
    (``secret_ref`` / ``hec_token`` / token) in clear — only ``has_secret:
    bool``. The repository scrubs sensitive fields before serializing.

    Design choices (mirroring RouteAuditLog / CredentialAccessLog):
      - ``destination_id`` is a plain String (no FK) so the trail survives the
        destination's deletion (forensics).
      - ``organization_id`` CASCADE-deletes with the owning Organization so
        data-erasure / GDPR flows are honoured.
      - ``actor`` is the username string; nullable for system actions.
      - ``action`` is one of: create | update | delete.
      - ``created_at`` is indexed for time-range audit queries.

    Append-only by convention: the app only INSERTs here; it never
    UPDATEs/DELETEs a row.
    """

    __tablename__ = "destination_audit_log"

    id = Column(String, primary_key=True, default=lambda: str(uuid4()))
    destination_id = Column(String, nullable=False, index=True)
    action = Column(String, nullable=False)  # create | update | delete
    actor = Column(String, nullable=True)  # username; nullable for system actions
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    snapshot = Column(Text, nullable=False, default="{}")  # JSON of the destination state (no secret)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


# ── Normalization subsystem ───────────


class MappingDefinition(Base):
    """Catálogo de mappings — uma linha por (vendor, event_type).

    O ponteiro ``current_version_id`` resolve qual ``MappingVersion`` é
    aplicada em runtime. Pode ser ``NULL`` enquanto o mapping ainda não
    teve versão criada (caso típico do seed inicial). Eventos que caem
    em ``(vendor, event_type)`` sem versão atual vão para quarentena.
    """

    __tablename__ = "mapping_definitions"
    __table_args__ = (
        UniqueConstraint("vendor", "event_type", name="uq_mapping_def_vendor_event"),
    )

    id = Column(String, primary_key=True, default=lambda: str(uuid4()))
    vendor = Column(String, nullable=False, index=True)
    event_type = Column(String, nullable=False)
    ocsf_class_uid = Column(Integer, nullable=False)
    description = Column(Text, nullable=True)
    # ``use_alter=True`` resolve cycle de FK entre ``mapping_definitions``
    # e ``mapping_versions`` (cada lado referencia o outro). Sem isso,
    # ``drop_all`` em testes emite SAWarning; em runtime não muda nada.
    current_version_id = Column(
        String,
        ForeignKey(
            "mapping_versions.id",
            use_alter=True,
            name="fk_mapping_def_current_version",
        ),
        nullable=True,
    )
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    versions = relationship(
        "MappingVersion",
        back_populates="definition",
        cascade="all, delete-orphan",
        foreign_keys="MappingVersion.definition_id",
    )
    current_version = relationship(
        "MappingVersion", foreign_keys=[current_version_id], post_update=True
    )


class MappingVersion(Base):
    """Versão imutável de um mapping. RF3.8 — cada save = nova linha.

    Não tem ``updated_at`` nem soft-delete: histórico é append-only para
    audit trail (RF5.4). Para "trocar" o mapping atual, atualiza-se
    ``MappingDefinition.current_version_id``; rollback = apontar de
    volta para uma versão anterior.

    ``rules`` guarda a DSL (JSON serializada) que será interpretada
    pelo engine de normalização (Sprint 2). ``dry_run_stats`` é cache
    do resultado da validação obrigatória no save (RF3.7).
    """

    __tablename__ = "mapping_versions"
    __table_args__ = (
        UniqueConstraint(
            "definition_id", "version_number",
            name="uq_mapping_version_def_num",
        ),
    )

    id = Column(String, primary_key=True, default=lambda: str(uuid4()))
    definition_id = Column(
        String,
        ForeignKey("mapping_definitions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_number = Column(Integer, nullable=False)
    rules = Column(Text, nullable=False)  # JSON serializada da DSL
    author_user_id = Column(
        Integer,
        ForeignKey("app_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    commit_message = Column(Text, nullable=False)  # obrigatório (RF5.3)
    diff_from_previous = Column(Text, nullable=True)  # JSON do diff
    dry_run_stats = Column(Text, nullable=True)  # JSON {ok, fail, novos campos}
    # resultado da validação OCSF no commit — JSON
    # {checked, valid, invalid, by_reason{...}, missing_required{class:[...]},
    # class_uid_declared_vs_emitted}. Espelha ``dry_run_stats``. Nullable p/ conciliar
    # create_all (DB novo) com a migração lightweight (versões legadas ficam NULL).
    ocsf_validation_stats = Column(Text, nullable=True)
    dsl_version = Column(Integer, nullable=False, default=2)  # DSL v2 (dict com preprocess+rules)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    definition = relationship(
        "MappingDefinition",
        back_populates="versions",
        foreign_keys=[definition_id],
    )


class OrganizationOcsfPolicy(Base):
    """Política de enforcement OCSF por organização.

    Uma linha por org. ``enforcement_mode`` decide o que o hook de validação OCSF
    (``pipeline.py``) faz com um evento estruturalmente inválido:

    - ``tag_and_pass`` — só etiqueta ``_centralops.ocsf_valid`` + métrica; despacha
      mesmo assim (default seguro no rollout; orgs existentes são backfilladas aqui).
    - ``quarantine`` — envia p/ quarentena (``ERROR_KIND_VALIDATE``), NÃO despacha;
      recuperável via reprocess. Default enterprise seguro na GA.
    - ``fail_closed`` — descarta sem quarentena (só p/ orgs que exigem explicitamente).

    Escopo de tenant: resolvido SEMPRE por ``organization_id`` do envelope —
    sem default cross-tenant. ``ocsf_version`` per-org NÃO existe: a versão
    alvo vem do global ``settings.OCSF_VALIDATION_VERSION``.
    """

    __tablename__ = "organization_ocsf_policy"

    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    enforcement_mode = Column(String, nullable=False, default="tag_and_pass")
    # TIMESTAMP, NÃO DATETIME (Postgres não tem DATETIME; gotcha de migração).
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


class UnknownField(Base):
    """Campos do raw que nenhum mapping consome (RF3.6).

    Populado pelo detector de drift (Sprint 3) com sampling 1:N. UI
    expõe via "Drift Explorer". ``status`` permite o operador
    marcar campo como ``ignored`` (esperado, não interessa) ou
    ``mapped`` (depois de criar regra para ele).
    """

    __tablename__ = "unknown_fields"
    __table_args__ = (
        # ``organization_id`` entra na unicidade — antes a
        # inferência de drift era por (vendor, event_type, field_path) SEM
        # tenant, vazando campos desconhecidos entre clientes do mesmo vendor.
        UniqueConstraint(
            "vendor", "event_type", "field_path", "organization_id",
            name="uq_unknown_field_vendor_event_org",
        ),
    )

    id = Column(String, primary_key=True, default=lambda: str(uuid4()))
    vendor = Column(String, nullable=False, index=True)
    event_type = Column(String, nullable=False)
    field_path = Column(String, nullable=False)  # ex: "alert.threat.details.hash"
    # escopo de tenant. Nullable no ORM para conciliar
    # create_all (DB novo) com a migração lightweight (backfill de rows
    # legadas); o isolamento de leitura é garantido pelo filtro por org.
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    sample_value = Column(Text, nullable=True)  # truncado em ~200 chars no insert
    sample_type = Column(String, nullable=True)  # "string|number|object|array|null|bool"
    occurrence_count = Column(Integer, nullable=False, default=1)
    first_seen = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_seen = Column(DateTime, default=datetime.utcnow, nullable=False)
    status = Column(String, nullable=False, default="new")  # "new|ignored|mapped"


class QuarantineEvent(Base):
    """Eventos que falharam parsing/normalização (RF2.6).

    Não devem ir para o Wazuh sem revisão. Retenção mínima 7 dias
    (``expires_at`` = +7d por default) para investigação. UI expõe
    listar/inspecionar/reprocessar/descartar.

    ``error_kind`` categoriza a falha:
    - ``parse``: payload do vendor não decodificou (raro);
    - ``map``: regra ``required`` do mapping resolveu para nada;
    - ``validate``: envelope produzido viola schema (ex: class_uid inválido);
    - ``missing_customer_id``: tenant não pôde ser resolvido (RF4.2).
    """

    __tablename__ = "quarantine_events"
    __table_args__ = (
        # eixo tenant+tempo para pruning/erase eficientes.
        # Antes desta migração a quarentena NÃO tinha coluna de tenant (só
        # integration_id) — gap real de isolamento multi-tenant. O índice composto
        # ``(organization_id, created_at)`` serve retenção, erase-by-org e listagem
        # escopada por org sem o custo de particionamento declarativo (sink de erro
        # de baixo volume; ver nota em DestinationDeadLetter).
        Index("ix_quarantine_events_org_created", "organization_id", "created_at"),
    )

    id = Column(String, primary_key=True, default=lambda: str(uuid4()))
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    integration_id = Column(
        Integer,
        ForeignKey("integrations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    vendor = Column(String, nullable=False, index=True)
    event_type = Column(String, nullable=True)  # null se falhou antes de classificar
    raw_payload = Column(Text, nullable=False)  # JSON serializado
    error_kind = Column(String, nullable=False)
    error_detail = Column(Text, nullable=True)
    mapping_version_id = Column(
        String,
        ForeignKey("mapping_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False)  # default = created_at + 7d
    reprocessed_at = Column(DateTime, nullable=True)


class BackfillJob(Base):
    """Job de backfill — coleta histórica controlada (RF2.4).

    Criado via POST /api/integrations/{id}/backfill com janela e streams.
    Worker dedicado em fila 'collect.backfill' executa de forma assíncrona,
    com cursor isolado do polling normal.

    Campos de status possíveis:
    - "pending"   : criado, aguardando execução pelo worker.
    - "running"   : worker ativo processando.
    - "completed" : concluído com sucesso.
    - "failed"    : erro durante execução (ver last_error).
    - "cancelled" : cancelado pelo operador antes de concluir.
    """

    __tablename__ = "backfill_jobs"
    __table_args__ = (
        # Permite listar jobs por integration ordenados por mais recente
        # primeiro sem full-table-scan.
        Index("idx_backfill_jobs_integration_ts", "integration_id", "requested_at"),
    )

    id = Column(String, primary_key=True, default=lambda: str(uuid4()))
    integration_id = Column(
        Integer,
        ForeignKey("integrations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    streams = Column(Text, nullable=False)  # JSON list de streams
    from_ts = Column(DateTime, nullable=False)
    to_ts = Column(DateTime, nullable=False)

    status = Column(String, nullable=False, default="pending")
    # "pending" | "running" | "completed" | "failed" | "cancelled"

    # Progresso
    events_collected = Column(Integer, nullable=False, default=0)
    events_dispatched = Column(Integer, nullable=False, default=0)
    current_cursor = Column(Text, nullable=True)  # JSON, isolado do polling
    progress_pct = Column(Integer, nullable=False, default=0)  # 0–100

    # Lifecycle
    requested_by_user_id = Column(
        Integer,
        ForeignKey("app_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    requested_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)

    # Rastreabilidade Celery
    celery_task_id = Column(String, nullable=True, index=True)


class OrganizationRetentionConfig(Base):
    """Política de retenção por organização (RNF7.2).

    Define janelas de retenção separadas para raw (forense) e normalizado
    (operacional). Job periódico (Celery beat) poda dados expirados.
    """

    __tablename__ = "organization_retention_config"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    # Quarentena (raw payload) — padrão 7 dias.
    quarantine_retention_days = Column(Integer, nullable=False, default=7)
    # Drift entries (UnknownField) — padrão 90 dias.
    drift_retention_days = Column(Integer, nullable=False, default=90)
    # History (audit de chamadas) — padrão 30 dias.
    history_retention_days = Column(Integer, nullable=False, default=30)
    # Search results — padrão 7 dias.
    search_result_retention_days = Column(Integer, nullable=False, default=7)
    # Audit logs gerais — padrão 365 (RNF4.4 audit imutável 1 ano).
    audit_log_retention_days = Column(Integer, nullable=False, default=365)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    organization = relationship(
        "Organization",
        back_populates="retention_config",
        uselist=False,
    )


class DataDeletionJob(Base):
    """Job de right-to-delete (RNF7.3 — LGPD/GDPR).

    Criado por DELETE /api/organizations/{id}/data. O executor Celery
    percorre as tabelas dependentes em ordem e atualiza status ao final.
    """

    __tablename__ = "data_deletion_jobs"

    id = Column(String, primary_key=True, default=lambda: str(uuid4()))
    # ``RESTRICT`` evita deletar Organization enquanto existir job de
    # right-to-delete em qualquer estado — preserva o snapshot pra trilha
    # forense LGPD/GDPR. Para deletar a org, opera-se primeiro o job
    # (concluir ou cancelar e arquivar).
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # Snapshot do slug para manter rastreabilidade após deleção da org.
    organization_slug = Column(String, nullable=False)
    requested_by_user_id = Column(
        Integer,
        ForeignKey("app_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    requested_by_username = Column(String, nullable=True)
    reason = Column(Text, nullable=True)
    # "pending" | "running" | "completed" | "partial" | "failed"
    status = Column(String, nullable=False, default="pending")
    # JSON com contagem de linhas deletadas por tabela.
    rows_deleted = Column(Text, nullable=True)
    last_error = Column(Text, nullable=True)
    requested_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    celery_task_id = Column(String, nullable=True)


class MappingAuditLog(Base):
    """Audit trail imutável de mudanças em mappings (RF5.4).

    Existe separado do ``AuditLog`` genérico do app para garantir
    retenção mínima de 1 ano (RNF4.4) e schema dedicado com diff
    estruturado. Append-only por convenção — sem coluna de update,
    sem endpoint de delete.

    Em uma futura fase de hardening, considerar exportar para storage
    WORM (S3 Object Lock) — Postgres permite ``DELETE`` privilegiado
    e portanto não atende WORM real.
    """

    __tablename__ = "mapping_audit_log"

    id = Column(String, primary_key=True, default=lambda: str(uuid4()))
    # Auditoria imutável — ao deletar parents (definition/version/integration/
    # user), preserva a row e nuca o FK para NULL. Snapshots em ``username``
    # / ``user_role`` mantêm contexto humano mesmo após a deleção do parent.
    mapping_definition_id = Column(
        String,
        ForeignKey("mapping_definitions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    mapping_version_id = Column(
        String,
        ForeignKey("mapping_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    # rastreabilidade por integração (discard_quarantine)
    integration_id = Column(
        Integer,
        ForeignKey("integrations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    action = Column(String, nullable=False)
    # "create_definition|create_version|set_current|rollback|ignore_field|
    #  mark_mapped|delete_field|discard_quarantine|create_from_drift"
    user_id = Column(
        Integer,
        ForeignKey("app_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    username = Column(String, nullable=True)
    user_role = Column(String, nullable=True)
    diff = Column(Text, nullable=True)  # JSON
    detail = Column(Text, nullable=True)  # commit_message ou contexto livre
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


# ── Service Accounts (credencial machine-to-machine) ───────


class ServiceAccount(Base):
    """Identidade non-human pra workloads M2M (workers, IASOC, scripts).

    Diferente de ``AppUser``:
      - Não loga em browser (não tem ``password_hash`` nem ``UserSession``).
      - Tem ``role`` própria (operador escolhe na criação) que é o teto
        de permissões dos PATs ligados a ela.
      - Tokens emitidos referenciam o SA via ``ApiToken.service_account_id``
        em vez de ``user_id`` (relação XOR — vide constraint).

    Audit trail: requests autenticados via PAT-de-SA gravam ``username``
    como ``"sa:<name>"`` no ``AuditLog`` (transformação no shim de
    auth, não persistida aqui). Isso preserva diferenciabilidade no log
    sem esticar o schema de ``app_users``.
    """

    __tablename__ = "service_accounts"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, unique=True, index=True)
    # Texto livre — pra que/por que existe (referência humana).
    description = Column(Text, nullable=True)
    # Role do mesmo conjunto que ``AppUser`` (viewer/operator/engineer/admin).
    # Funciona como teto: PATs ligados a este SA não podem escalar privilégio.
    role = Column(String, nullable=False, default="viewer")
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    is_active = Column(Boolean, nullable=False, default=True)
    created_by_user_id = Column(
        Integer,
        ForeignKey("app_users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    organization = relationship("Organization")
    created_by = relationship("AppUser", foreign_keys=[created_by_user_id])
    api_tokens = relationship(
        "ApiToken",
        back_populates="service_account",
        cascade="all, delete-orphan",
        foreign_keys="ApiToken.service_account_id",
    )


# ── Personal Access Tokens (PAT) ──────


class ApiToken(Base):
    """Token Bearer pra ``Authorization: Bearer copsk_<...>``.

    Cobre dois casos (XOR):
      1. **PAT pessoal** — ``user_id`` setado, ``service_account_id`` nulo.
         Token herda permissões da role do ``AppUser``.
      2. **Token de Service Account** — ``service_account_id``
         setado, ``user_id`` nulo. Token herda permissões da role do SA.

    Nunca os dois ao mesmo tempo, nunca nenhum dos dois.
    Garantido por ``CheckConstraint`` (DB) **e** validação de service
    layer (ApiTokenService) — defesa em profundidade porque CHECK em
    SQLite só é avaliada com `PRAGMA foreign_keys=ON` e expressões boolean
    funcionais.

    **Scopes:**
    - ``scopes_json = NULL`` ou ``[]`` → "full inherit" da role (legacy,
      mantém comportamento de tokens já emitidos).
    - ``scopes_json = ["mapping.read", ...]`` → permissões efetivas =
      INTERSEÇÃO(scopes_json, role.permissions). Operador não consegue
      ampliar privilégio adicionando scope que a role não tem.

    **is_eternal:**
    - Substitui o ``expires_at IS NULL`` semanticamente. Tokens criados
      sem expiração precisam ser explicitamente marcados ``is_eternal``,
      o que dispara warnings na UI e telemetria. Backwards: tokens
      antigos com ``expires_at IS NULL`` são tratados como eternos pela
      query do housekeeping.

    Diferenças intencionais frente a ``ThreatIntelToken`` (que usa SHA-256):
    PAT são alvo de alto valor (acesso ao app inteiro com a role do dono),
    enquanto threat-intel tokens são credenciais read-only de feed externo
    e baixo valor se vazadas. Por isso PATs usam Argon2id (~50 ms/verify),
    custo aceitável dado que a UI mantém cookie session — Bearer é só para
    integrações non-browser.

    Lookup: index único em ``token_prefix`` permite localizar o candidato
    em O(log N) e fazer apenas 1 ``argon2.verify`` por request, mesmo
    com milhares de tokens emitidos.
    """

    __tablename__ = "api_tokens"

    id = Column(Integer, primary_key=True, index=True)
    # XOR: exatamente um de user_id / service_account_id é NOT NULL.
    # Constraint enforced por CheckConstraint (DB) + ApiTokenService (app).
    user_id = Column(
        Integer,
        ForeignKey("app_users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    service_account_id = Column(
        Integer,
        ForeignKey("service_accounts.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    name = Column(String, nullable=False)  # rótulo humano (livre, único por owner)
    # Primeiros 12 chars do raw token (ex: "copsk_aB3xK7"); permite display
    # parcial na UI ("...e9f1") + lookup rápido antes do argon2.verify.
    token_prefix = Column(String, nullable=False, unique=True, index=True)
    # Hash Argon2id — formato $argon2id$v=19$...$... (encode próprio do argon2-cffi).
    token_hash = Column(String, nullable=False)
    # Optional. ``None`` ou ``is_eternal=True`` significa "nunca expira"
    # (UI mostra warning amarelo + housekeeping job alerta).
    expires_at = Column(DateTime, nullable=True)
    is_eternal = Column(Boolean, nullable=False, default=False)
    # JSON array de strings (ex: ["mapping.read","integration.read"]).
    # NULL ou "[]" = full inherit da role (legacy).
    # Valores devem ser membros de Permission enum — validado em service layer.
    scopes_json = Column(Text, nullable=True)
    last_used_at = Column(DateTime, nullable=True)
    last_used_ip = Column(String, nullable=True)
    # Counter atualizado no resolver Bearer (best-effort, fora do hot-path do auth).
    use_count = Column(Integer, nullable=False, default=0)
    revoked_at = Column(DateTime, nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("AppUser", back_populates="api_tokens", foreign_keys=[user_id])
    service_account = relationship(
        "ServiceAccount",
        back_populates="api_tokens",
        foreign_keys=[service_account_id],
    )

    __table_args__ = (
        # Nome único por dono — mas dono é "user OU SA". Mantemos o constraint
        # antigo (user_id, name) e adicionamos par equivalente para SA.
        # Scopo de unicidade pequeno o bastante pra duplicar não dói; alternativa
        # seria coalescer pra um único expression index, mas SQLite tem limites.
        UniqueConstraint("user_id", "name", name="uq_api_tokens_user_name"),
        UniqueConstraint(
            "service_account_id", "name",
            name="uq_api_tokens_sa_name",
        ),
        # XOR — exatamente um dos dois owners. Postgres aceita; SQLite >=3.3.0
        # aceita CHECK em CREATE TABLE (mas não via ALTER TABLE ADD CONSTRAINT).
        # Para tabelas existentes, a defesa é
        # via ApiTokenService.create() — vide service_layer comments.
        CheckConstraint(
            "(user_id IS NOT NULL AND service_account_id IS NULL) "
            "OR (user_id IS NULL AND service_account_id IS NOT NULL)",
            name="ck_api_tokens_owner_xor",
        ),
    )
