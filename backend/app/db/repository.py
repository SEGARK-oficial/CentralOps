from __future__ import annotations

import json
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload
from datetime import datetime, timedelta

from ..core.crypto import encrypt
from ..services import integration_secrets
from . import hierarchy, models

_UNSET = object()


# ── Organization ──────────────────────────────────────────────────────

class OrganizationRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def add(self, org: models.Organization) -> models.Organization:
        self.db.add(org)
        self.db.commit()
        self.db.refresh(org)
        return org

    def get(self, org_id: int) -> models.Organization | None:
        return self.db.query(models.Organization).filter(models.Organization.id == org_id).first()

    def get_by_slug(self, slug: str) -> models.Organization | None:
        return self.db.query(models.Organization).filter(models.Organization.slug == slug).first()

    def get_by_name(self, name: str) -> models.Organization | None:
        return self.db.query(models.Organization).filter(models.Organization.name == name).first()

    def list(
        self,
        *,
        include_inactive: bool = False,
        organization_ids: list[int] | None = None,
        name_query: str | None = None,
        status: str | None = None,
        auto_managed: bool | None = None,
        external_provider: str | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> list[models.Organization]:
        """List organizations com filtros e paginação opcionais.

        ``include_inactive`` é mantido para compat — quando ``status`` é passado,
        ele tem precedência. Filtros novos:

        - ``name_query``: substring case-insensitive em ``name`` ou ``slug``.
        - ``status``: ``"active"|"inactive"|"all"``. ``None`` → comportamento
          legado (mesma semântica de ``include_inactive``).
        - ``auto_managed``: ``True``/``False`` filtra; ``None`` ignora.
        - ``external_provider``: igualdade exata.
        - ``offset``/``limit``: paginação. ``limit=None`` retorna tudo.
        """
        # Eager-load p/ evitar N+1 no _serialize: customer_mappings (lido p/ o
        # iris_customer_id) e integrations (integration_count). Uma
        # IN-query agregada por relação em vez de 1 lazy-load por org.
        q = self.db.query(models.Organization).options(
            selectinload(models.Organization.customer_mappings),
            selectinload(models.Organization.integrations),
        )
        if organization_ids is not None:
            q = q.filter(models.Organization.id.in_(organization_ids))

        # Status precedence: explicit `status` overrides include_inactive.
        if status is not None:
            normalized_status = status.lower()
            if normalized_status == "active":
                q = q.filter(models.Organization.is_active == True)  # noqa: E712
            elif normalized_status == "inactive":
                q = q.filter(models.Organization.is_active == False)  # noqa: E712
            # "all" → no filter
        elif not include_inactive:
            q = q.filter(models.Organization.is_active == True)  # noqa: E712

        if auto_managed is not None:
            q = q.filter(models.Organization.auto_managed == auto_managed)

        if external_provider:
            q = q.filter(models.Organization.external_provider == external_provider)

        if name_query:
            needle = f"%{name_query.strip()}%"
            q = q.filter(
                or_(
                    func.lower(models.Organization.name).like(func.lower(needle)),
                    func.lower(models.Organization.slug).like(func.lower(needle)),
                )
            )

        q = q.order_by(models.Organization.name.asc())
        if offset:
            q = q.offset(offset)
        if limit is not None:
            q = q.limit(limit)
        return q.all()

    def count(
        self,
        *,
        include_inactive: bool = False,
        organization_ids: list[int] | None = None,
        name_query: str | None = None,
        status: str | None = None,
        auto_managed: bool | None = None,
        external_provider: str | None = None,
    ) -> int:
        """Conta organizations aplicando os mesmos filtros de ``list``.
        Não aplica offset/limit — usado para o total da paginação."""
        q = self.db.query(func.count(models.Organization.id))
        if organization_ids is not None:
            q = q.filter(models.Organization.id.in_(organization_ids))
        if status is not None:
            normalized_status = status.lower()
            if normalized_status == "active":
                q = q.filter(models.Organization.is_active == True)  # noqa: E712
            elif normalized_status == "inactive":
                q = q.filter(models.Organization.is_active == False)  # noqa: E712
        elif not include_inactive:
            q = q.filter(models.Organization.is_active == True)  # noqa: E712
        if auto_managed is not None:
            q = q.filter(models.Organization.auto_managed == auto_managed)
        if external_provider:
            q = q.filter(models.Organization.external_provider == external_provider)
        if name_query:
            needle = f"%{name_query.strip()}%"
            q = q.filter(
                or_(
                    func.lower(models.Organization.name).like(func.lower(needle)),
                    func.lower(models.Organization.slug).like(func.lower(needle)),
                )
            )
        return q.scalar() or 0

    def update(self, org: models.Organization, **kwargs) -> models.Organization:
        for key, value in kwargs.items():
            if value is not _UNSET and hasattr(org, key):
                setattr(org, key, value)
        org.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(org)
        return org

    def delete(self, org: models.Organization) -> None:
        self.db.delete(org)
        self.db.commit()

    # ── Sophos Partner Mode — auto-onboarding helpers ─────────────────

    @staticmethod
    def _slugify(value: str) -> str:
        """Cheap deterministic slug. Mirrors api.schemas.OrganizationCreate.normalize_slug."""
        import re

        slug = re.sub(r"[^a-z0-9-]", "-", (value or "").strip().lower()).strip("-")
        return slug or "tenant"

    def find_by_external_id(
        self,
        provider: str,
        external_id: str,
    ) -> models.Organization | None:
        """Look up an Organization auto-created (or linked) by a previous Partner sync."""
        if not provider or not external_id:
            return None
        return (
            self.db.query(models.Organization)
            .filter(
                models.Organization.external_provider == provider,
                models.Organization.external_id == external_id,
            )
            .first()
        )

    def find_by_iris_customer_id(
        self,
        iris_customer_id: int,
    ) -> models.Organization | None:
        """Resolve the Organization mapped to an IrisDFIR customer.

        resolve via ``destination_customer_mappings`` (kind='iris') —
        a fonte da verdade — e NÃO mais a coluna deprecada
        ``Organization.iris_customer_id``. Usado pela API interna de resolução de
        tenant (um alerta vindo do IRIS/Wazuh carrega o customer id externo).
        """
        if iris_customer_id is None:
            return None
        mapping = (
            self.db.query(models.DestinationCustomerMapping)
            .filter(
                models.DestinationCustomerMapping.destination_kind == "iris",
                models.DestinationCustomerMapping.external_customer_id
                == str(iris_customer_id),
            )
            .first()
        )
        if mapping is None:
            return None
        return self.get(mapping.organization_id)

    def _allocate_unique_slug(self, base: str) -> str:
        """Return a slug guaranteed to be unique by appending ``-N`` if needed."""
        candidate = self._slugify(base)
        suffix = 1
        while self.get_by_slug(candidate) is not None:
            suffix += 1
            candidate = f"{self._slugify(base)}-{suffix}"
            if suffix > 1000:
                # Defensive — a thousand collisions on the same name is pathological.
                raise RuntimeError(
                    f"Could not allocate unique slug after 1000 attempts for base={base!r}"
                )
        return candidate

    def _allocate_unique_name(self, base: str) -> str:
        """Mirror of slug uniqueness for ``Organization.name`` (also unique)."""
        candidate = (base or "").strip() or "tenant"
        suffix = 1
        while self.get_by_name(candidate) is not None:
            suffix += 1
            candidate = f"{(base or '').strip()} ({suffix})"
            if suffix > 1000:
                raise RuntimeError(
                    f"Could not allocate unique name after 1000 attempts for base={base!r}"
                )
        return candidate

    def create_from_sophos_tenant(
        self,
        tenant_dto: dict,
        partner_integration_id: int,
    ) -> models.Organization:
        """Auto-create an Organization from a Sophos ``/partner/v1/tenants`` item.

        Idempotent against ``(external_provider, external_id)`` — if the row
        already exists, the caller should use :meth:`find_by_external_id`.
        Slug + name collisions are resolved with numeric suffixes so that
        Sophos accounts named identically to existing Organizations don't
        crash the sync.

        este é caminho EXCLUSIVO do EE (partner-sync). NÃO aplica a
        trava ``max_organizations`` do tier Starter (single-tenant) — e nem
        deveria: o isolamento de tier é garantido na EMISSÃO da licença (um
        deploy Starter não recebe o artefato EE nem licença MSSP). O teto MSSP
        por-tenant é ``PartnerProgram.max_child_orgs``, aplicado no quota guard
        do EE, não aqui.
        """
        external_id = (tenant_dto.get("external_id") or tenant_dto.get("id") or "").strip()
        if not external_id:
            raise ValueError("Sophos tenant payload missing external_id/id")
        raw_name = (tenant_dto.get("name") or external_id).strip() or external_id
        org = models.Organization(
            name=self._allocate_unique_name(raw_name),
            slug=self._allocate_unique_slug(raw_name),
            description=f"Auto-created from Sophos Partner sync (external_id={external_id})",
            is_active=True,
            external_provider="sophos",
            external_id=external_id,
            auto_managed=True,
            partner_integration_id=partner_integration_id,
        )
        self.db.add(org)
        self.db.flush()  # garante org.id para a materialização da hierarquia
        # materializa a aresta org→org (pai = reseller dono do
        # partner_integration) + closure; marca o pai como reseller. Idempotente.
        hierarchy.assign_on_create(self.db, org)
        self.db.commit()
        self.db.refresh(org)
        return org

    def link_existing_organization(
        self,
        org_id: int,
        tenant_dto: dict,
        partner_integration_id: int,
    ) -> models.Organization | None:
        """Attach a Sophos external identity to an Organization that was created manually.

        Useful when the admin set up an Organization before enabling Partner Mode
        and now wants the sync to include it. Does not flip ``auto_managed`` —
        manual ownership is preserved.
        """
        org = self.get(org_id)
        if org is None:
            return None
        external_id = (tenant_dto.get("external_id") or tenant_dto.get("id") or "").strip()
        if not external_id:
            raise ValueError("Sophos tenant payload missing external_id/id")
        org.external_provider = "sophos"
        org.external_id = external_id
        org.partner_integration_id = partner_integration_id
        org.updated_at = datetime.utcnow()
        self.db.flush()
        # agora que esta org tem um partner_integration, re-materializa
        # sua posição na árvore (passa a ser filha do reseller).
        hierarchy.assign_on_create(self.db, org)
        self.db.commit()
        self.db.refresh(org)
        return org


# ── Destination customer mappings ──────────────────────────

class DestinationCustomerMappingRepository:
    """CRUD do mapeamento Organization → customer id externo por destino IR/SOAR.

    Fonte da verdade do "external customer id" (IRIS/TheHive/SOAR), resolvido só
    na borda do connector — fora do hot path de entrega.
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    def get(
        self, organization_id: int, destination_kind: str
    ) -> models.DestinationCustomerMapping | None:
        return (
            self.db.query(models.DestinationCustomerMapping)
            .filter(
                models.DestinationCustomerMapping.organization_id == organization_id,
                models.DestinationCustomerMapping.destination_kind == destination_kind,
            )
            .first()
        )

    def get_external_id(
        self, organization_id: int, destination_kind: str
    ) -> str | None:
        mapping = self.get(organization_id, destination_kind)
        return mapping.external_customer_id if mapping else None

    def list_for_org(
        self, organization_id: int
    ) -> list[models.DestinationCustomerMapping]:
        return (
            self.db.query(models.DestinationCustomerMapping)
            .filter(
                models.DestinationCustomerMapping.organization_id == organization_id
            )
            .order_by(models.DestinationCustomerMapping.destination_kind.asc())
            .all()
        )

    def set(
        self,
        organization_id: int,
        destination_kind: str,
        external_customer_id: str,
    ) -> models.DestinationCustomerMapping:
        """Upsert idempotente do mapping (organization_id, destination_kind).

        Race-safe: dois workers provisionando a MESMA org concorrentemente
        colidem na UniqueConstraint (org+kind); o segundo captura o
        ``IntegrityError``, faz rollback e re-lê (read-after-conflict) →
        atualiza a linha existente em vez de quebrar o run do partner sync.
        """
        mapping = self.get(organization_id, destination_kind)
        if mapping is None:
            mapping = models.DestinationCustomerMapping(
                organization_id=organization_id,
                destination_kind=destination_kind,
                external_customer_id=str(external_customer_id),
            )
            self.db.add(mapping)
            try:
                self.db.commit()
            except IntegrityError:
                # Outra transação inseriu o mesmo (org, kind) entre o get e o
                # commit — re-lê e atualiza (idempotente sob concorrência).
                self.db.rollback()
                mapping = self.get(organization_id, destination_kind)
                if mapping is None:  # pragma: no cover - colisão por outro motivo
                    raise
                mapping.external_customer_id = str(external_customer_id)
                mapping.updated_at = datetime.utcnow()
                self.db.commit()
        else:
            mapping.external_customer_id = str(external_customer_id)
            mapping.updated_at = datetime.utcnow()
            self.db.commit()
        self.db.refresh(mapping)
        return mapping

    def find_organization_id(
        self, destination_kind: str, external_customer_id: str
    ) -> int | None:
        mapping = (
            self.db.query(models.DestinationCustomerMapping)
            .filter(
                models.DestinationCustomerMapping.destination_kind == destination_kind,
                models.DestinationCustomerMapping.external_customer_id
                == str(external_customer_id),
            )
            .first()
        )
        return mapping.organization_id if mapping else None


# ── Integration ───────────────────────────────────────────────────────

class IntegrationRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def add(self, integration: models.Integration) -> models.Integration:
        # Guarda de race condition: rejeita criação de integração se a organização
        # já foi marcada como inactive (ex: delete-request em andamento).
        # Previne integração órfã quando request_data_deletion e add() correm
        # concorrentemente na mesma org.
        if integration.organization_id is not None:
            from fastapi import HTTPException

            org = self.db.get(models.Organization, integration.organization_id)
            if org is not None and not org.is_active:
                raise HTTPException(
                    status_code=409,
                    detail="Organization is inactive — cannot create integration",
                )
        self.db.add(integration)
        self.db.commit()
        self.db.refresh(integration)
        return integration

    def get(self, integration_id: int) -> models.Integration | None:
        return self.db.query(models.Integration).filter(models.Integration.id == integration_id).first()

    def list(
        self,
        organization_id: int | None = None,
        platform: str | None = None,
        include_inactive: bool = False,
        organization_ids: list[int] | None = None,
        *,
        name: str | None = None,
        kind: str | None = None,
        status: str | None = None,
        region: str | None = None,
        data_geography: str | None = None,
        page: int | None = None,
        size: int | None = None,
    ) -> list[models.Integration]:
        """Listagem com filtros + paginação opcional.

        Filtros novos:
          * ``name``: substring case-insensitive em ``integrations.name``.
          * ``kind``: ``"tenant"|"partner"|"organization"`` ou None/'all'.
          * ``status``: ``"active"|"inactive"`` ou None/'all'. Se informado,
             tem precedência sobre ``include_inactive``.
          * ``region`` / ``data_geography``: substrings case-insensitive.
          * ``page`` (1-based) + ``size`` para paginação. Se ambos forem
             informados aplicamos OFFSET/LIMIT após a ordenação. Se apenas
             ``size`` vier, retornamos os primeiros N itens (page=1).

        Compat: defaults preservam comportamento antigo (todos ativos da org).
        """
        q = self.db.query(models.Integration)
        if organization_id is not None:
            q = q.filter(models.Integration.organization_id == organization_id)
        if organization_ids is not None:
            q = q.filter(models.Integration.organization_id.in_(organization_ids))
        if platform:
            q = q.filter(models.Integration.platform == platform)

        # Status: explicit overrides include_inactive when provided.
        if status:
            normalized_status = status.strip().lower()
            if normalized_status == "active":
                q = q.filter(models.Integration.is_active.is_(True))
            elif normalized_status == "inactive":
                q = q.filter(models.Integration.is_active.is_(False))
            # 'all' → no filter
        elif not include_inactive:
            q = q.filter(models.Integration.is_active.is_(True))

        if kind:
            normalized_kind = kind.strip().lower()
            if normalized_kind in ("tenant", "partner", "organization"):
                q = q.filter(models.Integration.kind == normalized_kind)
            # 'all' / unknown → no filter

        if name:
            needle = name.strip()
            if needle:
                q = q.filter(func.lower(models.Integration.name).contains(needle.lower()))

        if region:
            needle_region = region.strip()
            if needle_region:
                q = q.filter(
                    func.lower(func.coalesce(models.Integration.region, "")).contains(
                        needle_region.lower()
                    )
                )

        if data_geography:
            needle_geo = data_geography.strip()
            if needle_geo:
                q = q.filter(
                    func.lower(func.coalesce(models.Integration.data_geography, "")).contains(
                        needle_geo.lower()
                    )
                )

        q = q.order_by(models.Integration.name.asc())

        if size is not None and size > 0:
            effective_page = page if (page is not None and page > 0) else 1
            q = q.offset((effective_page - 1) * size).limit(size)

        return q.all()

    def update(self, integration: models.Integration, **kwargs) -> models.Integration:
        for key, value in kwargs.items():
            if value is not _UNSET and hasattr(integration, key):
                setattr(integration, key, value)
        integration.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(integration)
        return integration

    def record_auth_state(
        self,
        integration: models.Integration,
        *,
        auth_status: str,
        last_error: str | None,
    ) -> models.Integration:
        now = datetime.utcnow()
        integration.auth_status = auth_status
        integration.last_checked_at = now
        integration.last_error = last_error
        if auth_status in {"healthy", "degraded"}:
            integration.last_successful_check_at = now
        integration.updated_at = now
        self.db.commit()
        self.db.refresh(integration)
        return integration

    def update_integration_tokens(
        self,
        integration_id: int,
        *,
        access_token: str,
        refresh_token: str,
        region: str,
        tenant_id: str,
    ) -> models.Integration:
        """Persiste tokens OAuth (store ``integration_credentials``) + region/tenant.

        ``access_token``/``refresh_token`` chegam em PLAINTEXT — o
        ``write_secret`` cifra (Vault-aware). region/tenant_id continuam colunas.
        """
        integration = self.db.get(models.Integration, integration_id)
        if integration is None:
            raise ValueError(f"Integration {integration_id} not found")
        integration_secrets.write_secret(integration, "access_token", access_token)
        integration_secrets.write_secret(integration, "refresh_token", refresh_token)
        integration.region = region
        integration.tenant_id = tenant_id
        integration.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(integration)
        return integration

    def update_tokens(
        self,
        integration: models.Integration,
        *,
        access_token: str,
        refresh_token: str,
    ) -> models.Integration:
        """Rotaciona tokens OAuth no store ``integration_credentials``.

        ``integration`` é o credential holder (parent em Partner mode) já atado a
        ``self.db``; os tokens chegam em PLAINTEXT (``write_secret`` cifra)."""
        integration_secrets.write_secret(integration, "access_token", access_token)
        integration_secrets.write_secret(integration, "refresh_token", refresh_token)
        integration.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(integration)
        return integration

    def count_active(self, organization_id: int) -> int:
        """Retorna o número de integrações ativas para uma organização.

        Usado pelo limite por organização (MAX_INTEGRATIONS_PER_ORG).
        Query escalar — não carrega objetos ORM.
        """
        from sqlalchemy import func
        return (
            self.db.query(func.count(models.Integration.id))
            .filter(
                models.Integration.organization_id == organization_id,
                models.Integration.is_active.is_(True),
            )
            .scalar()
            or 0
        )

    def soft_delete(self, integration: models.Integration) -> models.Integration:
        integration.is_active = False
        integration.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(integration)
        return integration

    def delete(self, integration: models.Integration) -> None:
        self.db.delete(integration)
        self.db.commit()

    # ── Sophos Partner Mode — hierarchy helpers ───────────────────────

    def list_children(
        self,
        parent_id: int,
        *,
        include_inactive: bool = False,
    ) -> list[models.Integration]:
        """Return all child integrations of a Partner/Organization integration.

        Used by ``GET /integrations/{id}/discovered-tenants`` and the cascade
        soft-delete flow.
        """
        q = self.db.query(models.Integration).filter(
            models.Integration.parent_integration_id == parent_id
        )
        if not include_inactive:
            q = q.filter(models.Integration.is_active.is_(True))
        return q.order_by(models.Integration.name.asc()).all()

    def count_active_children(self, parent_id: int) -> int:
        return (
            self.db.query(func.count(models.Integration.id))
            .filter(
                models.Integration.parent_integration_id == parent_id,
                models.Integration.is_active.is_(True),
            )
            .scalar()
            or 0
        )

    def get_credential_source(
        self, integration: models.Integration
    ) -> models.Integration | None:
        """Retorna a integration que carrega credenciais OAuth para esta.

        Para children gerenciadas via Partner sync (``kind in ("tenant","organization")``
        com ``parent_integration_id`` populado), as credenciais (``client_id``,
        ``client_secret``, ``access_token``, ``refresh_token``) vivem no parent —
        o child só tem ``tenant_id``/``region`` próprios. Esta função resolve a
        fonte correta para fluxos como o scheduler.

        Retorna ``None`` se o parent não existir ou estiver inativo (caller
        decide pular o child com warning).
        """
        if integration.parent_integration_id is None:
            return integration
        parent = self.get(integration.parent_integration_id)
        if parent is None or not parent.is_active:
            return None
        return parent

    def has_resolvable_credentials(
        self, integration: models.Integration
    ) -> tuple[bool, str | None]:
        """Valida se a integração consegue obter um access_token válido.

        Considera o caso Partner-managed (child cuja credencial OAuth vive
        no parent). Verifica os pré-requisitos para um refresh bem-sucedido:
        ``region`` OU ``api_host`` no child (XDRQueryService resolve URL
        a partir de qualquer um deles) + ``tenant_id`` próprio +
        ``client_id``/``client_secret`` no credential source (próprio ou parent).

        Não checa ``access_token`` — esse é refrescado on-demand pelo
        ``TokenManager`` quando ausente.

        Retorna ``(ok, error)``. ``error`` é uma string descritiva quando
        ``ok=False``, suitable para mostrar ao operador.
        """
        if not integration.region and not integration.api_host:
            hint = (
                "Re-run sync_sophos_partner to restore api_host/region from the "
                "Sophos /partner/v1/tenants payload."
                if integration.parent_integration_id
                else "Re-authenticate."
            )
            return False, (
                f"Integration '{integration.name}' missing region and api_host. {hint}"
            )
        if not integration.tenant_id:
            return False, f"Integration '{integration.name}' missing tenant_id. Re-authenticate."
        source = self.get_credential_source(integration)
        if source is None:
            return False, (
                f"Integration '{integration.name}' is Partner-managed but parent "
                f"(id={integration.parent_integration_id}) is missing or inactive."
            )
        if not source.client_id or not integration_secrets.has_secret(source, "client_secret"):
            return False, (
                f"Integration '{integration.name}' (credential source '{source.name}') "
                "missing OAuth client_id/client_secret. Re-authenticate."
            )
        return True, None

    def find_partner_by_external_id(
        self,
        platform: str,
        external_id: str,
    ) -> models.Integration | None:
        """Locate the Partner integration that owns ``external_id`` (Sophos partner UUID)."""
        if not external_id:
            return None
        return (
            self.db.query(models.Integration)
            .filter(
                models.Integration.platform == platform,
                models.Integration.kind.in_(("partner", "organization")),
                models.Integration.external_id == external_id,
            )
            .first()
        )

    def find_child_by_external_id(
        self,
        parent_id: int,
        tenant_external_id: str,
    ) -> models.Integration | None:
        """Idempotency probe used by the Partner sync — given a parent and a
        Sophos tenant UUID, return the existing child or None.
        """
        if not tenant_external_id:
            return None
        return (
            self.db.query(models.Integration)
            .filter(
                models.Integration.parent_integration_id == parent_id,
                models.Integration.external_id == tenant_external_id,
            )
            .first()
        )

    def create_managed_child(
        self,
        *,
        parent: models.Integration,
        organization: models.Organization,
        name: str,
        external_id: str,
        region: str | None = None,
        data_geography: str | None = None,
        api_host: str | None = None,
    ) -> models.Integration:
        """Create a child Integration auto-managed by a Partner sync.

        Children intentionally have ``client_id``/``client_secret`` set to NULL —
        OAuth credentials live on the parent. The provider layer reads from
        ``parent`` when servicing tenant-scoped API calls.

        ``api_host`` is the verbatim hostname Sophos returned in
        ``/partner/v1/tenants`` (e.g. ``api-eu03.central.sophos.com``).
        Stored as the source of truth for outbound calls so collectors don't
        have to derive ``f"api-{region}..."`` from a geo code.
        """
        child = models.Integration(
            organization_id=organization.id,
            name=name,
            platform=parent.platform,
            kind="tenant",
            parent_integration_id=parent.id,
            external_id=external_id,
            id_type="tenant",
            region=region,
            data_geography=data_geography,
            api_host=api_host,
            tenant_id=external_id,  # Legacy column kept for retrocompat with provider headers.
            auto_managed=True,
            auth_status="unknown",
            is_active=True,
        )
        self.db.add(child)
        self.db.commit()
        self.db.refresh(child)
        return child

    def soft_delete_cascade(self, parent: models.Integration) -> int:
        """Soft-delete the parent and all active children.

        Returns the number of integrations deactivated (parent + children).
        Idempotent: rows already inactive are left alone.
        """
        now = datetime.utcnow()
        affected = 0
        for child in self.list_children(parent.id, include_inactive=False):
            child.is_active = False
            child.updated_at = now
            # Mirror to the linked Organization (auto-managed) — sync takes care
            # of the org slug & name; here we only flip the flag.
            org = self.db.get(models.Organization, child.organization_id)
            if org is not None and org.auto_managed and org.is_active:
                org.is_active = False
                org.updated_at = now
            affected += 1
        if parent.is_active:
            parent.is_active = False
            parent.updated_at = now
            affected += 1
        self.db.commit()
        return affected


# ── Integration Tenant Selection ──────────────────────────────────────

class IntegrationTenantSelectionRepository:
    """Persistência das seleções de tenants Sophos (Partner / Organization).

    Cada row representa um tenant descoberto sob um Partner com seu estado
    de seleção (``pending`` / ``approved`` / ``excluded``) + snapshots usados
    pela UI sem refazer chamada ao Sophos.
    """

    VALID_STATES = ("pending", "approved", "excluded")

    def __init__(self, db: Session) -> None:
        self.db = db

    def find(
        self,
        parent_id: int,
        external_id: str,
    ) -> models.IntegrationTenantSelection | None:
        if not external_id:
            return None
        return (
            self.db.query(models.IntegrationTenantSelection)
            .filter(
                models.IntegrationTenantSelection.parent_integration_id == parent_id,
                models.IntegrationTenantSelection.external_id == external_id,
            )
            .first()
        )

    def list(
        self,
        parent_id: int,
        *,
        state: str | None = None,
        search: str | None = None,
        geography: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[models.IntegrationTenantSelection]:
        q = self.db.query(models.IntegrationTenantSelection).filter(
            models.IntegrationTenantSelection.parent_integration_id == parent_id
        )
        if state is not None:
            q = q.filter(models.IntegrationTenantSelection.state == state)
        if geography:
            q = q.filter(
                func.lower(
                    func.coalesce(models.IntegrationTenantSelection.data_geography_snapshot, "")
                ) == geography.strip().lower()
            )
        if search:
            needle = search.strip().lower()
            if needle:
                q = q.filter(
                    or_(
                        func.lower(
                            func.coalesce(models.IntegrationTenantSelection.name_snapshot, "")
                        ).contains(needle),
                        func.lower(
                            func.coalesce(models.IntegrationTenantSelection.external_id, "")
                        ).contains(needle),
                    )
                )
        # Determinismo de ordenação: nome (snapshot) asc, id asc como tiebreaker.
        q = q.order_by(
            models.IntegrationTenantSelection.name_snapshot.asc(),
            models.IntegrationTenantSelection.id.asc(),
        )
        if offset:
            q = q.offset(offset)
        if limit is not None:
            q = q.limit(limit)
        return q.all()

    def count(
        self,
        parent_id: int,
        *,
        state: str | None = None,
        search: str | None = None,
        geography: str | None = None,
    ) -> int:
        q = self.db.query(func.count(models.IntegrationTenantSelection.id)).filter(
            models.IntegrationTenantSelection.parent_integration_id == parent_id
        )
        if state is not None:
            q = q.filter(models.IntegrationTenantSelection.state == state)
        if geography:
            q = q.filter(
                func.lower(
                    func.coalesce(models.IntegrationTenantSelection.data_geography_snapshot, "")
                ) == geography.strip().lower()
            )
        if search:
            needle = search.strip().lower()
            if needle:
                q = q.filter(
                    or_(
                        func.lower(
                            func.coalesce(models.IntegrationTenantSelection.name_snapshot, "")
                        ).contains(needle),
                        func.lower(
                            func.coalesce(models.IntegrationTenantSelection.external_id, "")
                        ).contains(needle),
                    )
                )
        return q.scalar() or 0

    def list_external_ids(
        self,
        parent_id: int,
        *,
        state: str | None = None,
    ) -> set[str]:
        """Conjunto de external_ids — usado para detectar drift no sync."""
        q = self.db.query(models.IntegrationTenantSelection.external_id).filter(
            models.IntegrationTenantSelection.parent_integration_id == parent_id
        )
        if state is not None:
            q = q.filter(models.IntegrationTenantSelection.state == state)
        return {row[0] for row in q.all() if row[0]}

    def upsert_snapshot(
        self,
        *,
        parent_id: int,
        external_id: str,
        name_snapshot: str | None = None,
        region_snapshot: str | None = None,
        data_geography_snapshot: str | None = None,
        api_host_snapshot: str | None = None,
        last_seen_at: datetime | None = None,
        default_state: str = "pending",
    ) -> tuple[models.IntegrationTenantSelection, bool]:
        """Idempotent: cria ou atualiza snapshots do tenant descoberto.

        Retorna ``(row, created)`` — ``created=True`` quando a row foi criada
        nesta chamada (sync usa essa info pra contar tenants novos).
        Não muda ``state`` quando já existe — só os snapshots e ``last_seen_at``.
        """
        if default_state not in self.VALID_STATES:
            raise ValueError(f"invalid state: {default_state!r}")
        if not external_id:
            raise ValueError("external_id is required")

        seen_at = last_seen_at or datetime.utcnow()
        row = self.find(parent_id, external_id)
        if row is None:
            row = models.IntegrationTenantSelection(
                parent_integration_id=parent_id,
                external_id=external_id,
                state=default_state,
                name_snapshot=name_snapshot,
                region_snapshot=region_snapshot,
                data_geography_snapshot=data_geography_snapshot,
                api_host_snapshot=api_host_snapshot,
                last_seen_at=seen_at,
            )
            self.db.add(row)
            self.db.commit()
            self.db.refresh(row)
            return row, True

        changed = False
        if name_snapshot and row.name_snapshot != name_snapshot:
            row.name_snapshot = name_snapshot
            changed = True
        if region_snapshot and row.region_snapshot != region_snapshot:
            row.region_snapshot = region_snapshot
            changed = True
        if data_geography_snapshot and row.data_geography_snapshot != data_geography_snapshot:
            row.data_geography_snapshot = data_geography_snapshot
            changed = True
        if api_host_snapshot and row.api_host_snapshot != api_host_snapshot:
            row.api_host_snapshot = api_host_snapshot
            changed = True
        if seen_at and row.last_seen_at != seen_at:
            row.last_seen_at = seen_at
            changed = True
        if changed:
            row.updated_at = datetime.utcnow()
            self.db.commit()
            self.db.refresh(row)
        return row, False

    def set_state(
        self,
        *,
        parent_id: int,
        external_ids: list[str],
        state: str,
        decided_by_user_id: int | None,
    ) -> list[models.IntegrationTenantSelection]:
        """Bulk update do state. Retorna as rows atualizadas (existentes apenas)."""
        if state not in self.VALID_STATES:
            raise ValueError(f"invalid state: {state!r}")
        external_ids = [e for e in external_ids if e]
        if not external_ids:
            return []
        rows = (
            self.db.query(models.IntegrationTenantSelection)
            .filter(
                models.IntegrationTenantSelection.parent_integration_id == parent_id,
                models.IntegrationTenantSelection.external_id.in_(external_ids),
            )
            .all()
        )
        now = datetime.utcnow()
        for row in rows:
            row.state = state
            row.decided_by_user_id = decided_by_user_id
            row.decided_at = now
            row.updated_at = now
        self.db.commit()
        for row in rows:
            self.db.refresh(row)
        return rows


# ── Integration Health ────────────────────────────────────────────────

class IntegrationHealthRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def add(self, check: models.IntegrationHealthCheck) -> models.IntegrationHealthCheck:
        self.db.add(check)
        self.db.commit()
        self.db.refresh(check)
        return check

    def get_latest(self, integration_id: int) -> models.IntegrationHealthCheck | None:
        return (
            self.db.query(models.IntegrationHealthCheck)
            .filter(models.IntegrationHealthCheck.integration_id == integration_id)
            .order_by(models.IntegrationHealthCheck.checked_at.desc())
            .first()
        )

    def list_for_integration(self, integration_id: int, limit: int = 50) -> list[models.IntegrationHealthCheck]:
        return (
            self.db.query(models.IntegrationHealthCheck)
            .filter(models.IntegrationHealthCheck.integration_id == integration_id)
            .order_by(models.IntegrationHealthCheck.checked_at.desc())
            .limit(limit)
            .all()
        )

    def get_latest_before(
        self,
        integration_id: int,
        checked_before: datetime,
    ) -> models.IntegrationHealthCheck | None:
        return (
            self.db.query(models.IntegrationHealthCheck)
            .filter(models.IntegrationHealthCheck.integration_id == integration_id)
            .filter(models.IntegrationHealthCheck.checked_at < checked_before)
            .order_by(models.IntegrationHealthCheck.checked_at.desc())
            .first()
        )

    def get_latest_bulk(
        self, integration_ids: list[int]
    ) -> dict[int, models.IntegrationHealthCheck]:
        """Return the most recent check per integration_id in a single query."""
        if not integration_ids:
            return {}
        max_ids_stmt = (
            select(func.max(models.IntegrationHealthCheck.id))
            .where(models.IntegrationHealthCheck.integration_id.in_(integration_ids))
            .group_by(models.IntegrationHealthCheck.integration_id)
        )
        rows = (
            self.db.query(models.IntegrationHealthCheck)
            .filter(models.IntegrationHealthCheck.id.in_(max_ids_stmt))
            .all()
        )
        return {row.integration_id: row for row in rows}

    def get_latest_before_bulk(
        self,
        integration_ids: list[int],
        checked_before: datetime,
    ) -> dict[int, models.IntegrationHealthCheck]:
        """Return the most recent check before checked_before per integration_id."""
        if not integration_ids:
            return {}
        max_ids_stmt = (
            select(func.max(models.IntegrationHealthCheck.id))
            .where(models.IntegrationHealthCheck.integration_id.in_(integration_ids))
            .where(models.IntegrationHealthCheck.checked_at < checked_before)
            .group_by(models.IntegrationHealthCheck.integration_id)
        )
        rows = (
            self.db.query(models.IntegrationHealthCheck)
            .filter(models.IntegrationHealthCheck.id.in_(max_ids_stmt))
            .all()
        )
        return {row.integration_id: row for row in rows}


class CollectionStateRepository:
    """Persistência do cursor/checkpoint por (integration, stream).

    Usado pelo ``collectors.state.cursor.CursorStore`` como fonte da verdade
    quando o Redis não tem o hot cursor (cold start, restart sem AOF, etc.).
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, integration_id: int, stream: str) -> models.CollectionState | None:
        return (
            self.db.query(models.CollectionState)
            .filter(models.CollectionState.integration_id == integration_id)
            .filter(models.CollectionState.stream == stream)
            .first()
        )

    def list_for_integration(
        self, integration_id: int
    ) -> list[models.CollectionState]:
        return (
            self.db.query(models.CollectionState)
            .filter(models.CollectionState.integration_id == integration_id)
            .order_by(models.CollectionState.stream.asc())
            .all()
        )

    def upsert(
        self,
        *,
        integration_id: int,
        stream: str,
        cursor: str | None,
        events_collected: int,
        error: str | None = None,
    ) -> models.CollectionState:
        now = datetime.utcnow()
        row = self.get(integration_id, stream)
        if row is None:
            row = models.CollectionState(
                integration_id=integration_id,
                stream=stream,
                cursor=cursor,
                last_attempt_at=now,
                last_success_at=now if not error else None,
                last_error=error,
                consecutive_failures=0 if not error else 1,
                events_collected_total=events_collected,
            )
            self.db.add(row)
        else:
            row.cursor = cursor
            row.last_attempt_at = now
            if error:
                row.last_error = error
                row.consecutive_failures += 1
            else:
                row.last_success_at = now
                row.last_error = None
                row.consecutive_failures = 0
            row.events_collected_total = (row.events_collected_total or 0) + events_collected
            row.updated_at = now
        self.db.commit()
        self.db.refresh(row)
        return row


class UserRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def count(self) -> int:
        return self.db.query(models.AppUser).count()

    def add(self, user: models.AppUser) -> models.AppUser:
        self.db.add(user)
        self.db.commit()
        self.db.refresh(user)
        return user

    def get(self, user_id: int) -> models.AppUser | None:
        return self.db.query(models.AppUser).filter(models.AppUser.id == user_id).first()

    def get_by_username(self, username: str) -> models.AppUser | None:
        return (
            self.db.query(models.AppUser)
            .filter(models.AppUser.username == username)
            .first()
        )

    def get_by_email(self, email: str) -> models.AppUser | None:
        return (
            self.db.query(models.AppUser)
            .filter(models.AppUser.email == email)
            .first()
        )

    def get_by_external_subject(
        self, auth_provider: str, external_subject: str
    ) -> models.AppUser | None:
        """Resolve uma conta federada pelo (provider, subject) do IdP.

        Usado no OIDC/SCIM pra casar o token do Entra com a conta
        local. Já incluído aqui pra fechar o contrato de identidade.
        """
        return (
            self.db.query(models.AppUser)
            .filter(models.AppUser.auth_provider == auth_provider)
            .filter(models.AppUser.external_subject == external_subject)
            .first()
        )

    def get_by_uuid(self, user_uuid: str) -> models.AppUser | None:
        return (
            self.db.query(models.AppUser)
            .filter(models.AppUser.uuid == user_uuid)
            .first()
        )

    def list(self) -> list[models.AppUser]:
        return self.db.query(models.AppUser).order_by(models.AppUser.username.asc()).all()

    def update(
        self,
        user: models.AppUser,
        *,
        username: str | None = None,
        display_name: str | None = None,
        password_hash: str | None = None,
        email: str | None | object = _UNSET,
        organization_id: int | None | object = _UNSET,
        role: str | None = None,
        is_global: bool | None = None,
        auth_provider: str | None = None,
        external_subject: str | None | object = _UNSET,
        is_active: bool | None = None,
    ) -> models.AppUser:
        if username is not None:
            user.username = username
        if display_name is not None:
            user.display_name = display_name
        if password_hash is not None:
            user.password_hash = password_hash
        # ``email`` / ``external_subject`` usam _UNSET porque ``None`` é um
        # valor válido (limpar o campo), distinto de "não mexer".
        if email is not _UNSET:
            user.email = email
        if organization_id is not _UNSET:
            user.organization_id = organization_id
        if role is not None:
            user.role = role
        if is_global is not None:
            user.is_global = is_global
        if auth_provider is not None:
            user.auth_provider = auth_provider
        if external_subject is not _UNSET:
            user.external_subject = external_subject
        if is_active is not None:
            user.is_active = is_active
        user.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(user)
        return user

    def delete(self, user: models.AppUser) -> None:
        self.db.delete(user)
        self.db.commit()

    def mark_login(self, user: models.AppUser) -> models.AppUser:
        user.last_login_at = datetime.utcnow()
        user.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(user)
        return user


class UserSessionRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def add(self, session: models.UserSession) -> models.UserSession:
        self.db.add(session)
        self.db.commit()
        self.db.refresh(session)
        return session

    def get_active_by_token_hash(self, token_hash: str) -> models.UserSession | None:
        now = datetime.utcnow()
        return (
            self.db.query(models.UserSession)
            .filter(models.UserSession.token_hash == token_hash)
            .filter(models.UserSession.revoked_at.is_(None))
            .filter(models.UserSession.expires_at > now)
            .first()
        )

    def revoke(self, session: models.UserSession) -> models.UserSession:
        session.revoked_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(session)
        return session

    def revoke_all_for_user(self, user_id: int) -> None:
        active_sessions = (
            self.db.query(models.UserSession)
            .filter(models.UserSession.user_id == user_id)
            .filter(models.UserSession.revoked_at.is_(None))
            .all()
        )
        now = datetime.utcnow()
        for session in active_sessions:
            session.revoked_at = now
        self.db.commit()

    def revoke_all_for_user_except(
        self, user_id: int, keep_session_id: int | None
    ) -> int:
        """Revoga todas as sessões ativas do usuário, exceto ``keep_session_id``.

        Usado pelo self-service ("sair das outras sessões" e troca de senha):
        o usuário permanece logado no dispositivo atual enquanto todas as demais
        sessões são invalidadas. ``keep_session_id=None`` (ex.: chamada via PAT,
        sem sessão de browser) revoga TODAS. Retorna o número de sessões
        revogadas."""
        query = (
            self.db.query(models.UserSession)
            .filter(models.UserSession.user_id == user_id)
            .filter(models.UserSession.revoked_at.is_(None))
        )
        if keep_session_id is not None:
            query = query.filter(models.UserSession.id != keep_session_id)
        active_sessions = query.all()
        now = datetime.utcnow()
        for session in active_sessions:
            session.revoked_at = now
        self.db.commit()
        return len(active_sessions)

    def touch(self, session: models.UserSession, ttl_hours: int) -> models.UserSession:
        now = datetime.utcnow()
        session.last_seen_at = now

        extension_threshold = now + timedelta(hours=max(ttl_hours // 2, 1))
        if session.expires_at <= extension_threshold:
            session.expires_at = now + timedelta(hours=ttl_hours)

        self.db.commit()
        self.db.refresh(session)
        return session

    def delete_expired(self) -> None:
        now = datetime.utcnow()
        expired = (
            self.db.query(models.UserSession)
            .filter(
                (models.UserSession.expires_at <= now)
                | (models.UserSession.revoked_at.is_not(None))
            )
            .all()
        )
        for session in expired:
            self.db.delete(session)
        self.db.commit()


class OidcAuthStateRepository:
    """Persistência efêmera de state/nonce/PKCE do fluxo OIDC."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self,
        *,
        state: str,
        nonce: str,
        code_verifier: str,
        ttl_seconds: int,
        redirect_to: str | None = None,
    ) -> models.OidcAuthState:
        now = datetime.utcnow()
        row = models.OidcAuthState(
            state=state,
            nonce=nonce,
            code_verifier=code_verifier,
            redirect_to=redirect_to,
            created_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def consume(self, state: str) -> tuple[str, str, str | None] | None:
        """Uso único: retorna ``(nonce, code_verifier, redirect_to)`` se o state
        existe e não expirou, sempre removendo a linha encontrada. ``None`` se
        inexistente ou expirado. Valores são lidos antes do delete (a instância
        fica detached após o commit)."""
        row = (
            self.db.query(models.OidcAuthState)
            .filter(models.OidcAuthState.state == state)
            .first()
        )
        if row is None:
            return None
        result = (row.nonce, row.code_verifier, row.redirect_to)
        expired = row.expires_at <= datetime.utcnow()
        self.db.delete(row)
        self.db.commit()
        return None if expired else result

    def delete_expired(self) -> int:
        now = datetime.utcnow()
        rows = (
            self.db.query(models.OidcAuthState)
            .filter(models.OidcAuthState.expires_at <= now)
            .all()
        )
        for row in rows:
            self.db.delete(row)
        self.db.commit()
        return len(rows)


class AuditLogRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def add(self, log: models.AuditLog) -> models.AuditLog:
        self.db.add(log)
        self.db.commit()
        self.db.refresh(log)
        return log

    def delete_older_than(self, cutoff: datetime) -> int:
        deleted = (
            self.db.query(models.AuditLog)
            .filter(models.AuditLog.created_at < cutoff)
            .delete(synchronize_session=False)
        )
        self.db.commit()
        return deleted

    def list(
        self,
        *,
        username: str | None = None,
        ip_address: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int = 500,
        viewer: models.AppUser | None = None,
        include_all: bool = False,
    ) -> list[models.AuditLog]:
        query = (
            self.db.query(models.AuditLog)
            .order_by(models.AuditLog.created_at.desc(), models.AuditLog.id.desc())
        )

        if viewer and not include_all:
            visibility_filters = []
            if getattr(viewer, "id", None) is not None:
                visibility_filters.append(models.AuditLog.user_id == viewer.id)
            normalized_username = (getattr(viewer, "username", "") or "").strip().lower()
            if normalized_username:
                visibility_filters.append(
                    (models.AuditLog.user_id.is_(None))
                    & (func.lower(models.AuditLog.username) == normalized_username)
                )
            if visibility_filters:
                query = query.filter(or_(*visibility_filters))

        if username:
            query = query.filter(func.lower(models.AuditLog.username).contains(username.strip().lower()))
        if ip_address:
            query = query.filter(models.AuditLog.ip_address.contains(ip_address.strip()))
        if date_from:
            query = query.filter(models.AuditLog.created_at >= date_from)
        if date_to:
            query = query.filter(models.AuditLog.created_at <= date_to)

        if limit > 0:
            query = query.limit(limit)
        return query.all()


class SearchResultRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def delete_older_than(self, cutoff: datetime) -> int:
        deleted = (
            self.db.query(models.SearchResult)
            .filter(models.SearchResult.created_at < cutoff)
            .delete(synchronize_session=False)
        )
        self.db.commit()
        return deleted

    def add_run(
        self,
        integration_id: int,
        search_id: str,
        statement: str,
        table: str,
        from_ts: str,
        to_ts: str,
        status: str,
        schedule_id: int | None = None,
        user_id: int | None = None,
        result_count: int | None = None,
        error_message: str | None = None,
        *,
        platform: str | None = None,
        engine: str | None = None,
        language: str | None = None,
        ocsf_mapping_version: str | None = None,
        organization_id: int | None = None,
        query_job_id: int | None = None,
    ) -> models.SearchResult:
        # metadados de vendor/dialeto/OCSF (antes nunca populados) +
        # org_id fail-closed + link ao QueryJob. ``engine``/``language`` mantêm o
        # default do model quando não informados (compat com call-sites legados).
        kwargs: dict = dict(
            integration_id=integration_id,
            user_id=user_id,
            search_id=search_id,
            statement=statement,
            table=table,
            from_ts=from_ts,
            to_ts=to_ts,
            status=status,
            schedule_id=schedule_id,
            result_count=result_count,
            error_message=error_message,
            platform=platform,
            ocsf_mapping_version=ocsf_mapping_version,
            organization_id=organization_id,
            query_job_id=query_job_id,
        )
        if engine is not None:
            kwargs["engine"] = engine
        if language is not None:
            kwargs["language"] = language
        run = models.SearchResult(**kwargs)
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        return run

    @staticmethod
    def _org_scope_clause(viewer: "models.AppUser | None"):
        """Cláusula de isolamento por org (fecha o leak de org-reassignment:
        ``user_id`` casa mas a org do usuário mudou). Tenant-scoped vê só a própria org
        (+ NULL legado pré-tenancy); admin/global (SOC) veem tudo (``None`` ⇒ sem filtro)."""
        if viewer is None:
            return None
        if getattr(viewer, "role", None) == "admin" or getattr(viewer, "is_global", False):
            return None
        org_id = getattr(viewer, "organization_id", None)
        return or_(
            models.SearchResult.organization_id == org_id,
            models.SearchResult.organization_id.is_(None),
        )

    def list(
        self,
        integration_id: int | None = None,
        schedule_id: int | None = None,
        viewer: models.AppUser | None = None,
    ) -> list[models.SearchResult]:
        q = self.db.query(models.SearchResult)
        if integration_id is not None:
            q = q.filter(models.SearchResult.integration_id == integration_id)
        if schedule_id is not None:
            q = q.filter(models.SearchResult.schedule_id == schedule_id)
        # user_id restringe ao próprio resultado — EXCETO admin/global (SOC vê os da
        # org); o org-scope abaixo fecha o isolamento por tenant.
        if viewer and getattr(viewer, "role", None) != "admin" and not getattr(viewer, "is_global", False):
            q = q.filter(models.SearchResult.user_id == viewer.id)
        _org = self._org_scope_clause(viewer)
        if _org is not None:
            q = q.filter(_org)
        return q.order_by(models.SearchResult.created_at.desc(), models.SearchResult.id.desc()).all()

    def has_recent_terminal_run(
        self, schedule_id: int, since: datetime
    ) -> bool:
        """True se já há um ``SearchResult`` TERMINAL recente (>= ``since``) p/ o schedule.

        Guarda de idempotência: com ``acks_late``, a re-entrega de um
        ``run_scheduled_query`` em voo (worker morto pós-commit, pré-ack) duplicaria
        SearchResult+e-mail+alerta. Como a janela é ancorada em ``now`` (recência), a
        dedupe é por "houve run terminal deste schedule na última meia-cadência" — pega
        a re-entrega (que ocorre em segundos/minutos) sem falsos positivos entre ticks."""
        return (
            self.db.query(models.SearchResult.id)
            .filter(
                models.SearchResult.schedule_id == schedule_id,
                models.SearchResult.status.in_(("finished", "completed", "partial")),
                models.SearchResult.created_at >= since,
            )
            .first()
            is not None
        )

    def get_by_search_id(
        self,
        search_id: str,
        viewer: models.AppUser | None = None,
    ) -> models.SearchResult | None:
        query = self.db.query(models.SearchResult).filter(models.SearchResult.search_id == search_id)
        if viewer and getattr(viewer, "role", None) != "admin" and not getattr(viewer, "is_global", False):
            query = query.filter(models.SearchResult.user_id == viewer.id)
        _org = self._org_scope_clause(viewer)
        if _org is not None:
            query = query.filter(_org)
        return query.first()

    def update_result(
        self,
        search_result: models.SearchResult,
        status: str,
        result_json: str,
        *,
        result_count: int | None = None,
        error_message: str | None = None,
    ) -> models.SearchResult:
        search_result.status = status
        search_result.result_json = result_json
        search_result.result_count = result_count
        search_result.error_message = error_message
        self.db.commit()
        self.db.refresh(search_result)
        return search_result

    def update_status(
        self, search_result: models.SearchResult, status: str
    ) -> models.SearchResult:
        search_result.status = status
        self.db.commit()
        self.db.refresh(search_result)
        return search_result

    def mark_failed(
        self,
        search_result: models.SearchResult,
        error_message: str,
        *,
        status: str = "failed",
    ) -> models.SearchResult:
        search_result.status = status
        search_result.error_message = error_message
        search_result.result_count = 0
        self.db.commit()
        self.db.refresh(search_result)
        return search_result


class QueryJobRepository:
    """Job store durável de query federada ao vivo.

    Toda leitura é org-scoped por construção (``organization_id`` obrigatório no
    create; ``get``/``list`` filtram por org) — fail-closed."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self,
        *,
        job_id: str,
        organization_id: int,
        dialect: str,
        statement: str,
        from_ts: str,
        to_ts: str,
        integration_ids: list[int],
        user_id: int | None = None,
        allow_partial_results: bool = False,
        spec_kind: str = "passthrough",
        original_statement: str | None = None,
    ) -> models.QueryJob:
        job = models.QueryJob(
            job_id=job_id,
            organization_id=organization_id,
            user_id=user_id,
            dialect=dialect,
            statement=statement,
            spec_kind=spec_kind,
            original_statement=original_statement,
            from_ts=from_ts,
            to_ts=to_ts,
            integration_ids=json.dumps(list(integration_ids)),
            allow_partial_results=allow_partial_results,
            status="submitted",
        )
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)
        return job

    def get(
        self, job_id: str, organization_ids: list[int] | None = None
    ) -> models.QueryJob | None:
        """Resolve por uuid público. ``organization_ids=None`` ⇒ escopo global
        (admin/SOC); lista vazia ⇒ nada; senão filtra estritamente por org."""
        q = self.db.query(models.QueryJob).filter(models.QueryJob.job_id == job_id)
        if organization_ids is not None:
            if not organization_ids:
                return None
            q = q.filter(models.QueryJob.organization_id.in_(organization_ids))
        return q.first()

    def get_by_pk(self, pk: int) -> models.QueryJob | None:
        return self.db.query(models.QueryJob).filter(models.QueryJob.id == pk).first()

    def list_for_org(
        self, organization_ids: list[int] | None, limit: int = 50
    ) -> list[models.QueryJob]:
        q = self.db.query(models.QueryJob)
        if organization_ids is not None:
            if not organization_ids:
                return []
            q = q.filter(models.QueryJob.organization_id.in_(organization_ids))
        return (
            q.order_by(models.QueryJob.created_at.desc(), models.QueryJob.id.desc())
            .limit(limit)
            .all()
        )

    def mark_running(self, job: models.QueryJob) -> models.QueryJob:
        job.status = "running"
        self.db.commit()
        self.db.refresh(job)
        return job

    def finish(
        self,
        job: models.QueryJob,
        *,
        status: str,
        per_source: dict | None = None,
        total_results: int | None = None,
        error_message: str | None = None,
    ) -> models.QueryJob:
        job.status = status
        if per_source is not None:
            job.per_source = json.dumps(per_source)
        if total_results is not None:
            job.total_results = total_results
        if error_message is not None:
            job.error_message = error_message
        job.finished_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(job)
        return job


class DetectionRepository:
    """Alertas de detecção de 1ª classe.

    ``record`` faz dedup LÓGICO por ``(organization_id, dedup_key)``: um match
    repetido dentro da janela de supressão BUMPA ``count``/``last_seen`` em vez de
    criar um novo alerta (anti-spam). Leituras são org-scoped fail-closed."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def record(
        self,
        *,
        organization_id: int,
        source: str,
        dedup_key: str,
        severity_id: int = 4,
        source_query_id: int | None = None,
        integration_id: int | None = None,
        dialect: str | None = None,
        rule_id: str | None = None,
        rule_name: str | None = None,
        search_result_id: int | None = None,
        ocsf_ref: str | None = None,
        suppression_window_seconds: int = 3600,
    ) -> models.Detection:
        now = datetime.utcnow()
        existing = (
            self.db.query(models.Detection)
            .filter(
                models.Detection.organization_id == organization_id,
                models.Detection.dedup_key == dedup_key,
                models.Detection.status != "closed",
            )
            .order_by(models.Detection.id.desc())
            .first()
        )
        if (
            existing is not None
            and existing.last_seen is not None
            and existing.last_seen >= now - timedelta(seconds=existing.suppression_window_seconds or 0)
        ):
            existing.count = (existing.count or 0) + 1
            existing.last_seen = now
            existing.updated_at = now
            if search_result_id is not None:
                existing.search_result_id = search_result_id
            self.db.commit()
            self.db.refresh(existing)
            return existing
        det = models.Detection(
            organization_id=organization_id,
            source=source,
            dedup_key=dedup_key,
            severity_id=severity_id,
            source_query_id=source_query_id,
            integration_id=integration_id,
            dialect=dialect,
            rule_id=rule_id,
            rule_name=rule_name,
            search_result_id=search_result_id,
            ocsf_ref=ocsf_ref,
            suppression_window_seconds=suppression_window_seconds,
            first_seen=now,
            last_seen=now,
            count=1,
            status="open",
        )
        self.db.add(det)
        self.db.commit()
        self.db.refresh(det)
        return det

    def get(
        self, detection_id: int, organization_ids: list[int] | None = None
    ) -> models.Detection | None:
        q = self.db.query(models.Detection).filter(models.Detection.id == detection_id)
        if organization_ids is not None:
            if not organization_ids:
                return None
            q = q.filter(models.Detection.organization_id.in_(organization_ids))
        return q.first()

    def list_for_org(
        self,
        organization_ids: list[int] | None,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[models.Detection]:
        q = self.db.query(models.Detection)
        if organization_ids is not None:
            if not organization_ids:
                return []
            q = q.filter(models.Detection.organization_id.in_(organization_ids))
        if status:
            q = q.filter(models.Detection.status == status)
        return (
            q.order_by(models.Detection.created_at.desc(), models.Detection.id.desc())
            .limit(limit)
            .all()
        )

    def set_status(self, detection: models.Detection, status: str) -> models.Detection:
        detection.status = status
        detection.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(detection)
        return detection


class CorrelationRuleRepository:
    """Regras de correlação cross-source. Org-scoped fail-closed."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def add(self, rule: models.CorrelationRule) -> models.CorrelationRule:
        self.db.add(rule)
        self.db.commit()
        self.db.refresh(rule)
        return rule

    def get(
        self, rule_id: int, organization_ids: list[int] | None = None
    ) -> models.CorrelationRule | None:
        q = self.db.query(models.CorrelationRule).filter(models.CorrelationRule.id == rule_id)
        if organization_ids is not None:
            if not organization_ids:
                return None
            q = q.filter(models.CorrelationRule.organization_id.in_(organization_ids))
        return q.first()

    def list_for_org(
        self, organization_ids: list[int] | None, limit: int = 200
    ) -> list[models.CorrelationRule]:
        q = self.db.query(models.CorrelationRule)
        if organization_ids is not None:
            if not organization_ids:
                return []
            q = q.filter(models.CorrelationRule.organization_id.in_(organization_ids))
        return q.order_by(models.CorrelationRule.id.desc()).limit(limit).all()

    def list_enabled_for_org(
        self, organization_id: int, limit: int = 500
    ) -> list[models.CorrelationRule]:
        # ``limit`` é um teto DURO de avaliação (defesa em profundidade vs. fan-out
        # ilimitado no finish do job) — o controle primário é o cap na criação.
        return (
            self.db.query(models.CorrelationRule)
            .filter(
                models.CorrelationRule.organization_id == organization_id,
                models.CorrelationRule.enabled.is_(True),
            )
            .limit(max(1, limit))
            .all()
        )

    def list_inflight_for_org(
        self, organization_id: int, limit: int
    ) -> list[models.CorrelationRule]:
        """Regras em modo ``inflight`` da org (ADR-0015 Fase 1).

        Método SEPARADO de ``list_enabled_for_org`` de propósito: aquele é o
        caminho batch/EE e não pode mudar de comportamento. Ordenado por ``id``
        para que a compilação seja determinística entre workers — duas réplicas
        avaliando regras em ordens diferentes produziriam Detections com
        ``first_seen`` divergente sob concorrência.

        ``limit`` é teto DURO de avaliação por ciclo; ``max(0, ...)`` e não
        ``max(1, ...)`` porque 0 é o kill-switch de ambiente.
        """
        return (
            self.db.query(models.CorrelationRule)
            .filter(
                models.CorrelationRule.organization_id == organization_id,
                models.CorrelationRule.enabled.is_(True),
                models.CorrelationRule.eval_mode == "inflight",
            )
            .order_by(models.CorrelationRule.id.asc())
            .limit(max(0, limit))
            .all()
        )

    def count_inflight_for_org(self, organization_id: int) -> int:
        """Quantas regras EM VOO habilitadas a org tem, sem o teto por ciclo.

        Existe para tornar visível o truncamento: ``list_inflight_for_org``
        aplica ``INFLIGHT_MAX_RULES_PER_CYCLE`` com ``order_by(id ASC)``, então
        acima do teto as regras descartadas são as mais RECENTES. Comparar este
        total com o teto é a única forma de o operador saber que a regra que ele
        acabou de criar não está sendo avaliada.
        """
        return (
            self.db.query(models.CorrelationRule)
            .filter(
                models.CorrelationRule.organization_id == organization_id,
                models.CorrelationRule.enabled.is_(True),
                models.CorrelationRule.eval_mode == "inflight",
            )
            .count()
        )

    def count_enabled_for_org(self, organization_id: int) -> int:
        """Quantas regras habilitadas a org tem, INDEPENDENTE de ``eval_mode``.

        Existe só para o diagnóstico: quando nenhuma regra em voo é carregada,
        a diferença entre "a org não tem regra" e "a org tem 12 regras, todas em
        modo batch" é a resposta ao ticket de suporte mais provável desta fase.
        """
        return (
            self.db.query(models.CorrelationRule)
            .filter(
                models.CorrelationRule.organization_id == organization_id,
                models.CorrelationRule.enabled.is_(True),
            )
            .count()
        )

    def count_for_org(self, organization_id: int) -> int:
        return (
            self.db.query(func.count(models.CorrelationRule.id))
            .filter(models.CorrelationRule.organization_id == organization_id)
            .scalar()
            or 0
        )

    def update(self, rule: models.CorrelationRule, **fields) -> models.CorrelationRule:
        for key, value in fields.items():
            if value is not None and hasattr(rule, key):
                setattr(rule, key, value)
        rule.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(rule)
        return rule

    def delete(self, rule: models.CorrelationRule) -> None:
        self.db.delete(rule)
        self.db.commit()


class PredefinedQueryRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def add(self, query: models.PredefinedQuery) -> models.PredefinedQuery:
        self.db.add(query)
        self.db.commit()
        self.db.refresh(query)
        return query

    def get(self, query_id: int) -> models.PredefinedQuery | None:
        return (
            self.db.query(models.PredefinedQuery)
            .filter(models.PredefinedQuery.id == query_id)
            .first()
        )

    def list(
        self, scoped_org_ids: list[int] | None = None
    ) -> list[models.PredefinedQuery]:
        """Lista queries salvas, opcionalmente escopadas por org.

        ``scoped_org_ids=None`` → sem filtro (escopo global). ``[]`` → nenhuma.
        ``[id, ...]`` → só as orgs informadas (linhas org NULL ficam de fora,
        fail-closed para usuário escopado).
        """
        q = self.db.query(models.PredefinedQuery)
        if scoped_org_ids is not None:
            q = q.filter(models.PredefinedQuery.organization_id.in_(scoped_org_ids))
        return q.all()

    def update(
        self,
        query: models.PredefinedQuery,
        *,
        title: str | None = None,
        description: str | None = None,
        statement: str | None = None,
        table: str | None = None,
        client_ids: str | None = None,
    ) -> models.PredefinedQuery:
        if title is not None:
            query.title = title
        if description is not None:
            query.description = description
        if statement is not None:
            query.statement = statement
        if table is not None:
            query.table = table
        if client_ids is not None:
            query.client_ids = client_ids
        self.db.commit()
        self.db.refresh(query)
        return query

    def delete(self, query: models.PredefinedQuery) -> None:
        self.db.delete(query)
        self.db.commit()


class ScheduledQueryRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def add(self, sched: models.ScheduledQuery) -> models.ScheduledQuery:
        self.db.add(sched)
        self.db.commit()
        self.db.refresh(sched)
        return sched

    def get(self, sched_id: int) -> models.ScheduledQuery | None:
        return (
            self.db.query(models.ScheduledQuery)
            .filter(models.ScheduledQuery.id == sched_id)
            .first()
        )

    def list(
        self, scoped_org_ids: list[int] | None = None
    ) -> list[models.ScheduledQuery]:
        """Lista agendamentos, opcionalmente escopados por org.

        ``scoped_org_ids=None`` → sem filtro (global). ``[]`` → nenhum.
        ``[id, ...]`` → só as orgs informadas (org NULL fica de fora).
        """
        q = self.db.query(models.ScheduledQuery)
        if scoped_org_ids is not None:
            q = q.filter(models.ScheduledQuery.organization_id.in_(scoped_org_ids))
        return q.order_by(
            models.ScheduledQuery.next_run.asc(), models.ScheduledQuery.id.desc()
        ).all()

    def list_by_query_id(self, query_id: int) -> list[models.ScheduledQuery]:
        return (
            self.db.query(models.ScheduledQuery)
            .filter(models.ScheduledQuery.query_id == query_id)
            .order_by(models.ScheduledQuery.next_run.asc(), models.ScheduledQuery.id.desc())
            .all()
        )

    def delete(self, sched: models.ScheduledQuery) -> None:
        self.db.delete(sched)
        self.db.commit()

    def update_next_run(
        self,
        sched: models.ScheduledQuery,
        next_run: datetime,
        *,
        last_run_at: datetime | None = None,
    ) -> models.ScheduledQuery:
        sched.next_run = next_run
        sched.last_run_at = last_run_at
        sched.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(sched)
        return sched

    def update_run_outcome(
        self,
        sched: models.ScheduledQuery,
        *,
        next_run: datetime,
        last_run_at: datetime,
        success: bool,
        last_error: str | None = None,
        failing_threshold: int = 5,
    ) -> models.ScheduledQuery:
        """Avança ``next_run`` + atualiza o estado de SAÚDE.

        Sucesso reseta (``healthy``, 0 falhas, sem erro); falha incrementa
        ``consecutive_failures`` e marca ``degraded`` (→ ``failing`` a partir de
        ``failing_threshold``), preservando ``last_error`` — um schedule morto fica
        VISÍVEL em vez de "parecer rodar"."""
        sched.next_run = next_run
        sched.last_run_at = last_run_at
        if success:
            sched.consecutive_failures = 0
            sched.last_error = None
            sched.status = "healthy"
        else:
            sched.consecutive_failures = (sched.consecutive_failures or 0) + 1
            sched.last_error = (last_error or "")[:1000]
            sched.last_error_at = datetime.utcnow()
            sched.status = (
                "failing" if sched.consecutive_failures >= failing_threshold else "degraded"
            )
        sched.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(sched)
        return sched


class EmailRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def add(self, email: models.NotificationEmail) -> models.NotificationEmail:
        self.db.add(email)
        self.db.commit()
        self.db.refresh(email)
        return email

    def list(self) -> list[models.NotificationEmail]:
        return self.db.query(models.NotificationEmail).all()

    def list_for_org(self, organization_id: int | None) -> list[models.NotificationEmail]:
        """Destinatários ESCOPADOS por org.

        Escopo ESTRITO: só e-mails cujo ``organization_id`` casa exatamente. NÃO
        inclui os de org NULL (sistema) — incluí-los reintroduziria o leak
        cross-tenant (um e-mail NULL receberia resultado de TODA org). Use para
        resolver destinatários de resultado de scheduled query por integração.
        """
        return (
            self.db.query(models.NotificationEmail)
            .filter(models.NotificationEmail.organization_id == organization_id)
            .all()
        )

    def delete(self, email: models.NotificationEmail) -> None:
        self.db.delete(email)
        self.db.commit()

    def get(self, email_id: int) -> models.NotificationEmail | None:
        return (
            self.db.query(models.NotificationEmail)
            .filter(models.NotificationEmail.id == email_id)
            .first()
        )


class EmailConfigRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self) -> models.EmailConfig | None:
        return self.db.query(models.EmailConfig).first()

    def update(self, **kwargs) -> models.EmailConfig:
        clear_smtp_password = bool(kwargs.pop("clear_smtp_password", False))
        smtp_password = kwargs.pop("smtp_password", _UNSET)
        config = self.get()
        if not config:
            create_kwargs = {key: value for key, value in kwargs.items() if value is not None}
            if smtp_password not in (_UNSET, None, ""):
                create_kwargs["smtp_password"] = encrypt(str(smtp_password))
            config = models.EmailConfig(**create_kwargs)
            self.db.add(config)
        else:
            for k, v in kwargs.items():
                if v is not None:
                    setattr(config, k, v)
            if clear_smtp_password:
                config.smtp_password = None
            elif smtp_password not in (_UNSET, None, ""):
                config.smtp_password = encrypt(str(smtp_password))
        self.db.commit()
        self.db.refresh(config)
        return config


class ActionRunRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self,
        action_type: str,
        client_ids: str,
        payload: str,
        status: str = "pending",
        result_summary: str | None = None,
    ) -> models.ActionRun:
        run = models.ActionRun(
            action_type=action_type,
            client_ids=client_ids,
            payload=payload,
            status=status,
            result_summary=result_summary,
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        return run

    def get(self, run_id: int) -> models.ActionRun | None:
        return (
            self.db.query(models.ActionRun)
            .filter(models.ActionRun.id == run_id)
            .first()
        )

    def list(self, action_type: str | None = None) -> list[models.ActionRun]:
        q = self.db.query(models.ActionRun).order_by(models.ActionRun.created_at.desc())
        if action_type:
            q = q.filter(models.ActionRun.action_type == action_type)
        return q.all()

    def update_result(
        self,
        run: models.ActionRun,
        status: str,
        result_summary: str,
    ) -> models.ActionRun:
        run.status = status
        run.result_summary = result_summary
        self.db.commit()
        self.db.refresh(run)
        return run


# ── Collector Config ──────────────────────────────────────────────────

class CollectorConfigRepository:
    """CRUD da tabela singleton ``collector_config`` (linha id=1).

    Espelha o padrão de ``EmailConfigRepository`` mas sem campos
    cifrados. Os mapas JSON (``domain_concurrency_limits``,
    ``rate_limits_by_vendor``) são serializados via ``json.dumps`` na
    persistência e deserializados na leitura via ``as_snapshot()``.
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self) -> models.CollectorConfig | None:
        return self.db.query(models.CollectorConfig).filter_by(id=1).first()

    def get_or_create(self, **defaults) -> models.CollectorConfig:
        row = self.get()
        if row is not None:
            return row
        row = models.CollectorConfig(id=1, **defaults)
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def update(self, **kwargs) -> models.CollectorConfig:
        """Partial update. Usa sentinel ``_UNSET`` pra distinguir "campo
        não enviado" de "campo enviado como None". Serializa mapas via
        ``json.dumps`` antes de persistir.
        """
        import json

        row = self.get()
        if row is None:
            # Cria singleton com defaults do model
            row = models.CollectorConfig(id=1)
            self.db.add(row)

        for key, value in kwargs.items():
            if value is _UNSET:
                continue
            if not hasattr(row, key):
                continue
            if key in ("domain_concurrency_limits", "rate_limits_by_vendor") and isinstance(value, (dict, list)):
                setattr(row, key, json.dumps(value, separators=(",", ":")))
            else:
                setattr(row, key, value)

        row.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(row)
        return row


class IdentityConfigRepository:
    """CRUD da tabela singleton ``identity_config`` (linha id=1).

    Espelha ``CollectorConfigRepository``. Os campos JSON (``entra_role_map``,
    ``entra_allowed_email_domains``) são serializados na persistência. O
    ``entra_client_secret`` chega aqui **já cifrado** (o router cifra); este
    repo nunca lida com o secret em claro.
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self) -> models.IdentityConfig | None:
        return self.db.query(models.IdentityConfig).filter_by(id=1).first()

    def get_or_create(self, **defaults) -> models.IdentityConfig:
        row = self.get()
        if row is not None:
            return row
        row = models.IdentityConfig(id=1, **defaults)
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def update(self, **kwargs) -> models.IdentityConfig:
        """Partial update. ``_UNSET`` distingue "não enviado" de "None".
        Serializa os campos JSON antes de persistir."""
        import json

        row = self.get()
        if row is None:
            row = models.IdentityConfig(id=1)
            self.db.add(row)

        for key, value in kwargs.items():
            if value is _UNSET:
                continue
            if not hasattr(row, key):
                continue
            if key in ("entra_role_map", "entra_allowed_email_domains", "entra_last_sync_summary") \
                    and isinstance(value, (dict, list)):
                setattr(row, key, json.dumps(value, separators=(",", ":")))
            else:
                setattr(row, key, value)

        row.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(row)
        return row


# ── Destination ───────────────────────────────────────────────────────


class DestinationRepository:
    """CRUD for the ``destinations`` table.

    Multi-tenant scoping (S1): ``list`` and ``count`` accept an
    ``org_id`` and filter to rows where
    ``organization_id == org_id OR organization_id IS NULL`` — global
    destinations are visible to every tenant (e.g. wazuh-default).
    Global-scope callers (admin without a tenant) pass ``global_scope=True``
    to see every row unfiltered.

    ``secret_ref`` storage: this repo stores whatever the caller passes
    (already-encrypted ciphertext). The router is responsible for
    calling ``get_default_backend().encrypt(hec_token)`` BEFORE calling
    ``add`` or ``update``.
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    # ── helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _compute_version(
        config: dict,
        delivery: dict | None,
        secret_ref: str | None = None,
    ) -> str:
        """Delegate to the registry helper so there is one source of truth."""
        from ..collectors.output.destinations.registry import compute_config_version

        # secret_ref change (e.g. re-key) must also bump the version so
        # destination_cache rebuilds with the new credential.
        effective_delivery = dict(delivery or {})
        if secret_ref is not None:
            # Fold a short hint into delivery so a token rotation bumps
            # config_version without leaking the full ciphertext elsewhere.
            effective_delivery["_secret_ref_hint"] = secret_ref[:8]
        return compute_config_version(config, effective_delivery)

    @staticmethod
    def _json_serialize(value: dict) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    # ── audit (append-only destination audit trail) ─
    # Fields that must NEVER appear in an audit snapshot, in clear.
    _AUDIT_SECRET_KEYS = frozenset({"hec_token", "secret_ref", "token", "secret"})

    @classmethod
    def _scrub_snapshot(cls, value):
        """Recursively redact sensitive fields. The snapshot NEVER carries the
        secret in clear — only the booleanized presence (``has_secret``)."""
        if isinstance(value, dict):
            return {
                k: ("[REDACTED]" if k.lower() in cls._AUDIT_SECRET_KEYS else cls._scrub_snapshot(v))
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [cls._scrub_snapshot(v) for v in value]
        return value

    @classmethod
    def snapshot(cls, row: models.Destination) -> dict:
        """Scrubbed destination state as a plain dict (for audit).

        CRITICAL: ``secret_ref`` is reduced to ``has_secret: bool`` — the
        ciphertext (let alone the plaintext) NEVER enters the audit trail.
        ``config``/``delivery`` are deep-scrubbed in case a kind ever folds a
        token-like key into them.
        """
        return {
            "id": str(row.id),
            "name": str(row.name),
            "kind": str(row.kind),
            "enabled": bool(row.enabled),
            "config": cls._scrub_snapshot(json.loads(str(row.config or "{}"))),
            "delivery": cls._scrub_snapshot(json.loads(str(row.delivery or "{}"))),
            "config_version": str(row.config_version or ""),
            "has_secret": row.secret_ref is not None,
            "data_residency": (
                str(row.data_residency) if row.data_residency is not None else None
            ),
            "organization_id": (
                int(row.organization_id) if row.organization_id is not None else None
            ),
        }

    def _audit(self, row: models.Destination, action: str, actor: str | None) -> None:
        """Append a DestinationAuditLog row with a scrubbed snapshot.

        Added to the SAME session as the mutation so audit + write commit
        atomically. Append-only by convention (never UPDATE/DELETE)."""
        self.db.add(
            models.DestinationAuditLog(
                destination_id=str(row.id),
                action=action,
                actor=actor,
                organization_id=row.organization_id,
                snapshot=self._json_serialize(self.snapshot(row)),
            )
        )

    # ── write operations ───────────────────────────────────────────────

    def add(
        self,
        *,
        name: str,
        kind: str,
        config: dict,
        delivery: dict | None = None,
        secret_ref: str | None = None,
        organization_id: int | None = None,
        enabled: bool = True,
        data_residency: str | None = None,
        actor: str | None = None,
    ) -> models.Destination:
        """Persist a new Destination. ``config_version`` is computed here.

        Records a ``create`` audit row (scrubbed snapshot) in the same
        transaction."""
        effective_delivery = delivery or {}
        version = self._compute_version(config, effective_delivery, secret_ref)
        row = models.Destination(
            name=name,
            kind=kind,
            config=self._json_serialize(config),
            delivery=self._json_serialize(effective_delivery),
            secret_ref=secret_ref,
            config_version=version,
            organization_id=organization_id,
            enabled=enabled,
            data_residency=data_residency,
        )
        self.db.add(row)
        self.db.flush()  # assign id before snapshot
        self._audit(row, "create", actor)
        self.db.commit()
        self.db.refresh(row)
        return row

    def update(
        self,
        destination_id: str,
        *,
        name: object = _UNSET,
        config: object = _UNSET,
        delivery: object = _UNSET,
        enabled: object = _UNSET,
        secret_ref: object = _UNSET,
        organization_id: object = _UNSET,
        data_residency: object = _UNSET,
        actor: str | None = None,
    ) -> models.Destination | None:
        """Partial update. Returns None if the destination does not exist.

        ``config_version`` is recomputed whenever config, delivery, or
        secret_ref changes — any of the three alone is enough to trigger
        the cache rebuild.
        """
        row = self.get(destination_id)
        if row is None:
            return None

        if name is not _UNSET:
            row.name = name  # type: ignore[assignment]
        if enabled is not _UNSET:
            row.enabled = enabled  # type: ignore[assignment]
        if organization_id is not _UNSET:
            row.organization_id = organization_id  # type: ignore[assignment]
        if data_residency is not _UNSET:
            row.data_residency = data_residency  # type: ignore[assignment]

        # Deserialize current values for version recomputation.
        current_config: dict = json.loads(row.config or "{}")
        current_delivery: dict = json.loads(row.delivery or "{}")
        current_secret_ref: str | None = row.secret_ref

        new_config = current_config
        new_delivery = current_delivery
        new_secret_ref = current_secret_ref
        version_dirty = False

        if config is not _UNSET and config is not None:
            new_config = config  # type: ignore[assignment]
            row.config = self._json_serialize(new_config)
            version_dirty = True
        if delivery is not _UNSET and delivery is not None:
            new_delivery = delivery  # type: ignore[assignment]
            row.delivery = self._json_serialize(new_delivery)
            version_dirty = True
        if secret_ref is not _UNSET:
            # secret_ref=None is a valid value (clear the credential)
            new_secret_ref = secret_ref  # type: ignore[assignment]
            row.secret_ref = new_secret_ref
            version_dirty = True

        if version_dirty:
            row.config_version = self._compute_version(
                new_config, new_delivery, new_secret_ref
            )

        row.updated_at = datetime.utcnow()
        self.db.flush()
        self._audit(row, "update", actor)
        self.db.commit()
        self.db.refresh(row)
        return row

    def delete(self, destination_id: str, *, actor: str | None = None) -> bool:
        """Hard delete. Returns True if the row existed.

        Records a ``delete`` audit row (scrubbed snapshot captured BEFORE the
        row is removed) in the same transaction."""
        row = self.get(destination_id)
        if row is None:
            return False
        # Audit BEFORE delete so the final snapshot is captured.
        self._audit(row, "delete", actor)
        self.db.delete(row)
        self.db.commit()
        return True

    # ── read operations ────────────────────────────────────────────────

    def get(self, destination_id: str) -> models.Destination | None:
        return (
            self.db.query(models.Destination)
            .filter(models.Destination.id == destination_id)
            .first()
        )

    def list(
        self,
        org_id: int | None,
        *,
        include_disabled: bool = False,
        offset: int = 0,
        limit: int = 50,
        global_scope: bool = False,
    ) -> list[models.Destination]:
        """Return destinations visible to ``org_id``.

        ``global_scope=True`` (admin without tenant): returns all rows
        unfiltered by org. Otherwise applies S1 filter:
        ``organization_id == org_id OR organization_id IS NULL``.
        """
        q = self.db.query(models.Destination)
        if not global_scope:
            if org_id is not None:
                q = q.filter(
                    or_(
                        models.Destination.organization_id == org_id,
                        models.Destination.organization_id.is_(None),
                    )
                )
            else:
                # org_id is None AND not global_scope → user has no org,
                # sees only global destinations (organization_id IS NULL).
                q = q.filter(models.Destination.organization_id.is_(None))
        if not include_disabled:
            q = q.filter(models.Destination.enabled.is_(True))
        q = q.order_by(models.Destination.name.asc())
        if offset:
            q = q.offset(offset)
        q = q.limit(limit)
        return q.all()

    def count(
        self,
        org_id: int | None,
        *,
        global_scope: bool = False,
    ) -> int:
        q = self.db.query(func.count(models.Destination.id))
        if not global_scope:
            if org_id is not None:
                q = q.filter(
                    or_(
                        models.Destination.organization_id == org_id,
                        models.Destination.organization_id.is_(None),
                    )
                )
            else:
                q = q.filter(models.Destination.organization_id.is_(None))
        return q.scalar() or 0

    def by_kind(self, kind: str) -> list[models.Destination]:
        return (
            self.db.query(models.Destination)
            .filter(models.Destination.kind == kind)
            .order_by(models.Destination.name.asc())
            .all()
        )

    @staticmethod
    def _dlq_org_scope(q, org_id: int | None, global_scope: bool):
        """Row-level org filter (review LOW): DLQ rows on a GLOBAL destination
        (e.g. wazuh-default) aggregate events from all tenants, so a non-global
        caller must only see their org's (or NULL) rows — mirrors RouteRepository.
        Global callers see everything (default, preserves existing behavior)."""
        if global_scope or org_id is None:
            return q
        return q.filter(
            or_(
                models.DestinationDeadLetter.organization_id == org_id,
                models.DestinationDeadLetter.organization_id.is_(None),
            )
        )

    def dlq_stats(
        self, destination_id: str, *, org_id: int | None = None, global_scope: bool = True
    ) -> dict:
        """DLQ counters for a destination.

        Returns ``{dlq_total, dlq_24h, last_dlq_at}`` from the forensic
        ``destination_dlq`` table, optionally row-org-scoped.
        """
        from datetime import datetime, timedelta

        cutoff = datetime.utcnow() - timedelta(hours=24)
        base = self._dlq_org_scope(
            self.db.query(models.DestinationDeadLetter).filter(
                models.DestinationDeadLetter.destination_id == destination_id
            ),
            org_id,
            global_scope,
        )
        total = base.count()
        last24 = base.filter(
            models.DestinationDeadLetter.created_at >= cutoff
        ).count()
        last_row = base.order_by(
            models.DestinationDeadLetter.created_at.desc()
        ).first()
        return {
            "dlq_total": total,
            "dlq_24h": last24,
            "last_dlq_at": last_row.created_at if last_row else None,
        }

    def list_dlq(
        self,
        destination_id: str,
        *,
        offset: int = 0,
        limit: int = 50,
        org_id: int | None = None,
        global_scope: bool = True,
    ) -> list[models.DestinationDeadLetter]:
        """DLQ rows for a destination, newest first. The
        ``payload`` column carries the rejected event for inspection."""
        q = self._dlq_org_scope(
            self.db.query(models.DestinationDeadLetter).filter(
                models.DestinationDeadLetter.destination_id == destination_id
            ),
            org_id,
            global_scope,
        )
        return (
            q.order_by(models.DestinationDeadLetter.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

    def dlq_error_kind_counts(
        self, destination_id: str, *, org_id: int | None = None, global_scope: bool = True
    ) -> dict:
        """{error_kind: count} for a destination — the DLQ breakdown by reason."""
        q = self._dlq_org_scope(
            self.db.query(
                models.DestinationDeadLetter.error_kind,
                func.count(models.DestinationDeadLetter.id),
            ).filter(models.DestinationDeadLetter.destination_id == destination_id),
            org_id,
            global_scope,
        )
        rows = q.group_by(models.DestinationDeadLetter.error_kind).all()
        return {str(k): int(c) for k, c in rows}

    def list_dlq_for_reprocess(
        self,
        destination_id: str,
        *,
        event_ids: list[str] | None = None,
        org_id: int | None = None,
        global_scope: bool = True,
    ) -> list[models.DestinationDeadLetter]:
        """Return DLQ rows for a destination, optionally filtered by event_ids.

        Used exclusively by the drain/reprocess path.  No pagination — the
        caller (Celery task) processes the full set in one invocation; the
        endpoint caps the trigger via the 500-row org-scoped bound.
        """
        q = self._dlq_org_scope(
            self.db.query(models.DestinationDeadLetter).filter(
                models.DestinationDeadLetter.destination_id == destination_id
            ),
            org_id,
            global_scope,
        )
        if event_ids:
            q = q.filter(models.DestinationDeadLetter.event_id.in_(event_ids))
        return q.order_by(models.DestinationDeadLetter.created_at.asc()).all()

    def delete_dlq_entry(self, dlq_id: str) -> bool:
        """Hard-delete a single DLQ row by primary key after successful redelivery.

        Returns True when the row was found and deleted, False when already gone
        (idempotent — concurrent reprocess tasks converge safely).
        """
        row = self.db.get(models.DestinationDeadLetter, dlq_id)
        if row is None:
            return False
        self.db.delete(row)
        self.db.commit()
        return True

    def update_dlq_error(self, dlq_id: str, *, error_kind: str, error_detail: str) -> bool:
        """Update the error fields on a DLQ row after a failed re-delivery attempt.

        Keeps the row in the DLQ (operator can retry again later) but captures
        the latest failure reason for triage.
        Returns True when the row was found and updated.
        """
        row = self.db.get(models.DestinationDeadLetter, dlq_id)
        if row is None:
            return False
        row.error_kind = error_kind  # type: ignore[assignment]
        row.error_detail = error_detail  # type: ignore[assignment]
        self.db.commit()
        return True

    # ── S5: credential lifecycle ──────────────────────────────────────

    def rotate_credential(
        self,
        destination_id: str,
        *,
        new_secret_ref: str,
        expires_at: datetime | None = None,
    ) -> models.Destination | None:
        """Re-encrypt and bump ``secret_version``.

        Sets ``secret_created_at`` on first rotation (when NULL), updates
        ``secret_rotated_at`` unconditionally, and optionally sets
        ``secret_expires_at``.  Clears ``secret_revoked_at`` so a re-keyed
        credential is no longer considered revoked.

        Returns the refreshed row, or None if not found.
        """
        row = self.get(destination_id)
        if row is None:
            return None

        now = datetime.utcnow()
        row.secret_ref = new_secret_ref  # type: ignore[assignment]
        row.secret_version = (int(row.secret_version or 1)) + 1  # type: ignore[assignment]
        if row.secret_created_at is None:  # type: ignore[union-attr]
            row.secret_created_at = now  # type: ignore[assignment]
        row.secret_rotated_at = now  # type: ignore[assignment]
        if expires_at is not None:
            row.secret_expires_at = expires_at  # type: ignore[assignment]
        row.secret_revoked_at = None  # type: ignore[assignment]  # re-key clears revoke

        # Recompute config_version so destination_cache rebuilds.
        current_config: dict = json.loads(row.config or "{}")
        current_delivery: dict = json.loads(row.delivery or "{}")
        row.config_version = self._compute_version(  # type: ignore[assignment]
            current_config, current_delivery, new_secret_ref
        )
        row.updated_at = now  # type: ignore[assignment]
        self.db.commit()
        self.db.refresh(row)
        return row

    def revoke_credential(
        self,
        destination_id: str,
    ) -> models.Destination | None:
        """Clear ``secret_ref``, disable the destination, and set ``secret_revoked_at``.

        A revoked destination must be re-keyed via ``rotate_credential`` before
        it can be re-enabled.  Returns the refreshed row, or None if not found.
        """
        row = self.get(destination_id)
        if row is None:
            return None

        now = datetime.utcnow()
        row.secret_ref = None  # type: ignore[assignment]
        row.enabled = False  # type: ignore[assignment]
        row.secret_revoked_at = now  # type: ignore[assignment]
        row.updated_at = now  # type: ignore[assignment]
        self.db.commit()
        self.db.refresh(row)
        return row

    # ── S6: credential access audit ───────────────────────────────────

    def log_credential_access(
        self,
        destination_id: str,
        *,
        actor: str | None,
        action: str,
        organization_id: int | None = None,
        detail: str | None = None,
    ) -> models.CredentialAccessLog:
        """Append a credential access record (append-only — never updated).

        ``action`` must be one of: decrypt | test | rotate | revoke.
        ``detail`` is optional metadata (e.g. secret_version after rotation).
        Never include plaintext credentials here.
        """
        entry = models.CredentialAccessLog(
            destination_id=destination_id,
            actor=actor,
            action=action,
            organization_id=organization_id,
            detail=detail,
        )
        self.db.add(entry)
        self.db.commit()
        self.db.refresh(entry)
        return entry

    def list_credential_access_log(
        self,
        destination_id: str,
        *,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[int, list[models.CredentialAccessLog]]:
        """Return (total, entries) for the credential audit trail of a destination.

        Newest first.  No org-scoping: the caller (router) already asserts
        visibility via ``_assert_visible`` before reaching here.
        """
        q = (
            self.db.query(models.CredentialAccessLog)
            .filter(models.CredentialAccessLog.destination_id == destination_id)
        )
        total = q.count()
        entries = (
            q.order_by(models.CredentialAccessLog.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        return total, entries

    # ── destination CRUD audit trail ────────────────

    def audit_trail(
        self,
        destination_id: str,
        *,
        limit: int = 50,
    ) -> list[models.DestinationAuditLog]:
        """Append-only CRUD audit trail for a destination, newest first.

        No org-scoping here: the caller (router) already asserts visibility
        via ``_assert_visible`` before reaching this method."""
        return (
            self.db.query(models.DestinationAuditLog)
            .filter(models.DestinationAuditLog.destination_id == destination_id)
            .order_by(models.DestinationAuditLog.created_at.desc())
            .limit(limit)
            .all()
        )

    def get_audit(self, audit_id: str) -> models.DestinationAuditLog | None:
        return (
            self.db.query(models.DestinationAuditLog)
            .filter(models.DestinationAuditLog.id == audit_id)
            .first()
        )


class RouteRepository:
    """CRUD for the ``routes`` table + append-only ``route_audit_log``.
    Org-scoped exactly like DestinationRepository: ``list`` filters to
    ``organization_id == org_id OR IS NULL`` (global routes visible to every
    tenant); ``global_scope=True`` callers see everything.

    Every mutation writes a RouteAuditLog row (with the full route snapshot) in
    the SAME transaction — create+audit are atomic. The audit table is
    append-only by convention (never UPDATE/DELETE)."""

    def __init__(self, db: Session) -> None:
        self.db = db

    # ── serialization ──────────────────────────────────────────────────

    @staticmethod
    def _dumps(value) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def snapshot(row: models.Route) -> dict:
        """Full route state as a plain dict (for audit + rollback)."""
        return {
            "id": str(row.id),
            "name": str(row.name),
            "priority": int(row.priority),
            "condition": json.loads(str(row.condition or "{}")),
            "action": str(row.action),
            "destination_ids": json.loads(str(row.destination_ids or "[]")),
            "is_final": bool(row.is_final),
            "canary_percent": int(row.canary_percent),
            "transform_ref": row.transform_ref,
            "pii_redaction": (
                json.loads(str(row.pii_redaction))
                if getattr(row, "pii_redaction", None)
                else None
            ),
            "protect_detection": bool(row.protect_detection),
            "sample_percent": int(row.sample_percent),
            "suppress_key": row.suppress_key,
            "suppress_allow": int(row.suppress_allow),
            "suppress_window_s": int(row.suppress_window_s),
            "drop_raw": bool(getattr(row, "drop_raw", False) or False),
            "enabled": bool(row.enabled),
            "organization_id": int(row.organization_id) if row.organization_id is not None else None,
        }

    def _audit(self, row: models.Route, action: str, actor: str | None) -> None:
        self.db.add(
            models.RouteAuditLog(
                route_id=str(row.id),
                action=action,
                actor=actor,
                organization_id=row.organization_id,
                snapshot=self._dumps(self.snapshot(row)),
            )
        )

    # ── write ──────────────────────────────────────────────────────────

    def add(
        self,
        *,
        name: str,
        condition: dict,
        destination_ids: list,
        action: str = "route",
        is_final: bool = True,
        priority: int = 100,
        enabled: bool = True,
        canary_percent: int = 100,
        transform_ref: str | None = None,
        pii_redaction: object = None,
        # ── redução de volume (ADR-0015) ────────────────────────────
        # Defaults ESPELHAM ``models.Route``: protect_detection=True é o
        # fail-safe (protege por default, opt-out explícito); os demais
        # default pra "sem redução" (byte-idêntico).
        protect_detection: bool = True,
        sample_percent: int = 100,
        suppress_key: str | None = None,
        suppress_allow: int = 0,
        suppress_window_s: int = 30,
        drop_raw: bool = False,
        organization_id: int | None = None,
        actor: str | None = None,
    ) -> models.Route:
        row = models.Route(
            name=name,
            priority=priority,
            condition=self._dumps(condition),
            action=action,
            destination_ids=self._dumps(destination_ids),
            is_final=is_final,
            canary_percent=canary_percent,
            transform_ref=transform_ref,
            pii_redaction=self._dumps(pii_redaction) if pii_redaction else None,
            protect_detection=protect_detection,
            sample_percent=sample_percent,
            suppress_key=suppress_key,
            suppress_allow=suppress_allow,
            suppress_window_s=suppress_window_s,
            drop_raw=drop_raw,
            enabled=enabled,
            organization_id=organization_id,
        )
        self.db.add(row)
        self.db.flush()  # assign id before snapshot
        self._audit(row, "created", actor)
        self.db.commit()
        self.db.refresh(row)
        return row

    def update(
        self,
        route_id: str,
        *,
        name: object = _UNSET,
        priority: object = _UNSET,
        condition: object = _UNSET,
        action: object = _UNSET,
        destination_ids: object = _UNSET,
        is_final: object = _UNSET,
        canary_percent: object = _UNSET,
        transform_ref: object = _UNSET,
        pii_redaction: object = _UNSET,
        # ── redução de volume (ADR-0015) ────────────────────────────
        # ``_UNSET`` (não passado) = mantém o valor atual da row — inclui
        # ``protect_detection``: ausência NUNCA rebaixa o fail-safe pra
        # False. ``suppress_key`` aceita ``None`` EXPLÍCITO (distinto de
        # ``_UNSET``) pra limpar a chave — o caller (router) é quem faz
        # essa distinção via ``model_fields_set``.
        protect_detection: object = _UNSET,
        sample_percent: object = _UNSET,
        suppress_key: object = _UNSET,
        suppress_allow: object = _UNSET,
        suppress_window_s: object = _UNSET,
        drop_raw: object = _UNSET,
        enabled: object = _UNSET,
        organization_id: object = _UNSET,
        actor: str | None = None,
        audit_action: str = "updated",
    ) -> models.Route | None:
        row = self.get(route_id)
        if row is None:
            return None
        if name is not _UNSET:
            row.name = name  # type: ignore[assignment]
        if priority is not _UNSET:
            row.priority = priority  # type: ignore[assignment]
        if condition is not _UNSET:
            row.condition = self._dumps(condition)  # type: ignore[assignment]
        if action is not _UNSET:
            row.action = action  # type: ignore[assignment]
        if destination_ids is not _UNSET:
            row.destination_ids = self._dumps(destination_ids)  # type: ignore[assignment]
        if is_final is not _UNSET:
            row.is_final = is_final  # type: ignore[assignment]
        if canary_percent is not _UNSET:
            row.canary_percent = canary_percent  # type: ignore[assignment]
        if transform_ref is not _UNSET:
            row.transform_ref = transform_ref  # type: ignore[assignment]
        if pii_redaction is not _UNSET:
            # dict/list → JSON; None/falsy → NULL (limpa a redação).
            row.pii_redaction = (  # type: ignore[assignment]
                self._dumps(pii_redaction) if pii_redaction else None
            )
        if protect_detection is not _UNSET:
            row.protect_detection = protect_detection  # type: ignore[assignment]
        if sample_percent is not _UNSET:
            row.sample_percent = sample_percent  # type: ignore[assignment]
        if suppress_key is not _UNSET:
            row.suppress_key = suppress_key  # type: ignore[assignment]
        if suppress_allow is not _UNSET:
            row.suppress_allow = suppress_allow  # type: ignore[assignment]
        if suppress_window_s is not _UNSET:
            row.suppress_window_s = suppress_window_s  # type: ignore[assignment]
        if drop_raw is not _UNSET:
            row.drop_raw = drop_raw  # type: ignore[assignment]
        if enabled is not _UNSET:
            row.enabled = enabled  # type: ignore[assignment]
        if organization_id is not _UNSET:
            row.organization_id = organization_id  # type: ignore[assignment]
        self.db.flush()
        self._audit(row, audit_action, actor)
        self.db.commit()
        self.db.refresh(row)
        return row

    def delete(self, route_id: str, *, actor: str | None = None) -> bool:
        row = self.get(route_id)
        if row is None:
            return False
        # Audit BEFORE delete so the snapshot (for rollback) is captured.
        self._audit(row, "deleted", actor)
        self.db.delete(row)
        self.db.commit()
        return True

    # ── read ───────────────────────────────────────────────────────────

    def get(self, route_id: str) -> models.Route | None:
        return self.db.query(models.Route).filter(models.Route.id == route_id).first()

    def _scope(self, q, org_id: int | None, global_scope: bool):
        if global_scope:
            return q
        if org_id is not None:
            return q.filter(
                or_(
                    models.Route.organization_id == org_id,
                    models.Route.organization_id.is_(None),
                )
            )
        return q.filter(models.Route.organization_id.is_(None))

    def list(
        self,
        org_id: int | None,
        *,
        include_disabled: bool = True,
        global_scope: bool = False,
        offset: int = 0,
        limit: int = 200,
    ) -> list[models.Route]:
        q = self.db.query(models.Route)
        q = self._scope(q, org_id, global_scope)
        if not include_disabled:
            q = q.filter(models.Route.enabled.is_(True))
        q = q.order_by(models.Route.priority.asc(), models.Route.id.asc())
        return q.offset(offset).limit(limit).all()

    def list_enabled_for_org(self, org_id: int | None) -> list[models.Route]:
        """Enabled routes visible to ``org_id`` (org-scoped or global), ordered by
        priority — the set the routing engine evaluates for that tenant."""
        q = self.db.query(models.Route).filter(models.Route.enabled.is_(True))
        q = self._scope(q, org_id, global_scope=False)
        return q.order_by(models.Route.priority.asc(), models.Route.id.asc()).all()

    def count(self, org_id: int | None, *, global_scope: bool = False) -> int:
        q = self.db.query(func.count(models.Route.id))
        q = self._scope(q, org_id, global_scope)
        return q.scalar() or 0

    def audit_trail(self, route_id: str, *, limit: int = 50) -> list[models.RouteAuditLog]:
        return (
            self.db.query(models.RouteAuditLog)
            .filter(models.RouteAuditLog.route_id == route_id)
            .order_by(models.RouteAuditLog.created_at.desc())
            .limit(limit)
            .all()
        )

    def get_audit(self, audit_id: str) -> models.RouteAuditLog | None:
        return (
            self.db.query(models.RouteAuditLog)
            .filter(models.RouteAuditLog.id == audit_id)
            .first()
        )

    def reorder_routes(
        self,
        route_ids: list[str],
        *,
        org_id: int | None,
        global_scope: bool,
        actor: str | None = None,
        priority_step: int = 10,
    ) -> list[models.Route]:
        """Atomically reassign priorities to the given ordered list of route_ids.

        Priorities are assigned as ``priority_step * (1-based position)``, so
        a step of 10 yields 10, 20, 30 … leaving gaps for future inserts.

        Org-scope contract (S1 anti-enumeration):
          - Non-global callers: only routes whose ``organization_id == org_id``
            OR ``organization_id IS NULL`` are acceptable targets.  Any id that
            resolves to a row outside that scope raises ``PermissionError``.
          - Global-scope callers may reorder any route.

        The entire operation is a single commit — either ALL priorities are
        updated and audit rows written, or NONE are (atomicity via flush +
        single commit at the end).

        Returns the updated rows in the requested order.
        """
        updated: list[models.Route] = []
        for position, route_id in enumerate(route_ids, start=1):
            row = self.get(route_id)
            if row is None:
                raise ValueError(f"Route {route_id!r} not found")
            if not global_scope:
                row_org = row.organization_id
                if row_org is not None and row_org != org_id:
                    raise PermissionError(
                        f"Route {route_id!r} is outside the caller's organization scope"
                    )
            new_priority = priority_step * position
            row.priority = new_priority  # type: ignore[assignment]
            self.db.flush()
            self._audit(row, "reorder", actor)
            updated.append(row)
        self.db.commit()
        for row in updated:
            self.db.refresh(row)
        return updated
