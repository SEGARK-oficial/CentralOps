"""Centralised token lifecycle management.

``TokenManager`` authenticates / refreshes Sophos OAuth2 tokens and
**persists** the new credentials back to the database so that other
components (scheduler, block-actions) always work with valid tokens.
"""

from __future__ import annotations

import logging
from typing import Dict

from sqlalchemy.orm import Session

from ..db import models
from ..db.repository import IntegrationRepository
from . import integration_secrets
from .auth import SophosAuthService

logger = logging.getLogger(__name__)


class TokenManager:
    """Ensures an integration has a valid token and returns ready-to-use headers."""

    @staticmethod
    def _build_headers(access_token: str, tenant_id: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "X-Tenant-ID": tenant_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @staticmethod
    def _resolve_credential_source(
        integration: models.Integration,
        db: Session,
        credential_source: models.Integration | None,
    ) -> models.Integration:
        """Resolve the integration holding OAuth credentials.

        Auto-detects Partner-managed children (``parent_integration_id``
        populated) and resolves to the parent. Caller can override by
        passing ``credential_source`` explicitly.

        Raises ``RuntimeError`` when the parent is missing or inactive —
        same error surface as legacy "missing credentials" path.
        """
        if credential_source is not None:
            return credential_source
        if integration.parent_integration_id is None:
            return integration
        source = IntegrationRepository(db).get_credential_source(integration)
        if source is None:
            raise RuntimeError(
                f"Integration '{integration.name}' (id={integration.id}) is a "
                f"Partner-managed child but parent (id={integration.parent_integration_id}) "
                "is missing or inactive."
            )
        return source

    @classmethod
    def ensure_valid_token(
        cls,
        integration: models.Integration,
        db: Session,
        *,
        credential_source: models.Integration | None = None,
    ) -> Dict[str, str]:
        """Return API headers for *integration*, refreshing the token if needed.

        ``credential_source`` is the integration that carries OAuth credentials
        (``client_id``, ``client_secret``, ``access_token``, ``refresh_token``).
        For Partner-managed children it's the parent; for standalone tenants
        it's the integration itself. Defaults to ``integration`` for retro-compat.

        ``tenant_id`` is always read from *integration* (each tenant has its own).
        """
        if not integration.tenant_id:
            raise RuntimeError(
                f"Integration '{integration.name}' (id={integration.id}) is missing tenant_id. "
                "Please re-authenticate the integration."
            )

        source = cls._resolve_credential_source(integration, db, credential_source)

        normalized_client_id = (source.client_id or "").strip()
        if not normalized_client_id:
            raise RuntimeError(
                f"Credential source '{source.name}' (id={source.id}) has invalid client_id."
            )

        # Secrets vêm do store integration_credentials, sempre do
        # holder `source` (parent em Partner mode). read_secret já devolve None
        # quando ausente/revogado e funciona com a row detached (lazy=selectin).
        client_secret = integration_secrets.read_secret(source, "client_secret")
        access_token = integration_secrets.read_secret(source, "access_token")
        refresh_token = integration_secrets.read_secret(source, "refresh_token")

        if access_token:
            return cls._build_headers(access_token, integration.tenant_id)

        auth = SophosAuthService(normalized_client_id, client_secret)
        repo = IntegrationRepository(db)

        if refresh_token:
            try:
                logger.info("TokenManager: refreshing token for credential source %s", source.name)
                tokens = auth.refresh(refresh_token)
                new_access = tokens["access_token"]
                new_refresh = tokens.get("refresh_token", refresh_token)
                # IMPORTANT: persist on the SOURCE, not on the child integration.
                repo.update_tokens(
                    source,
                    access_token=new_access,
                    refresh_token=new_refresh,
                )
                return cls._build_headers(new_access, integration.tenant_id)
            except Exception:
                logger.warning(
                    "TokenManager: refresh_token failed for credential source %s, trying full auth",
                    source.name,
                )

        try:
            logger.info("TokenManager: full re-authentication for credential source %s", source.name)
            tokens = auth.authenticate()
            new_access = tokens["access_token"]
            new_refresh = tokens.get("refresh_token", "")
            # discover_region_and_tenant only makes sense for standalone (kind="tenant" without parent).
            # For Partner-managed children, region/tenant_id are already set; don't overwrite.
            if source.id == integration.id:
                region, tenant_id = auth.discover_region_and_tenant(new_access)
                repo.update_integration_tokens(
                    integration_id=integration.id,
                    access_token=new_access,
                    refresh_token=new_refresh,
                    region=region,
                    tenant_id=tenant_id,
                )
                return cls._build_headers(new_access, tenant_id)
            # Partner-managed: persist tokens on the source (parent) only.
            repo.update_tokens(
                source,
                access_token=new_access,
                refresh_token=new_refresh,
            )
            return cls._build_headers(new_access, integration.tenant_id)
        except Exception as exc:
            raise RuntimeError(
                f"TokenManager: could not authenticate '{source.name}': {exc}"
            ) from exc

    @classmethod
    def refresh_after_401(
        cls,
        integration: models.Integration,
        db: Session,
        *,
        credential_source: models.Integration | None = None,
    ) -> Dict[str, str]:
        """Force a fresh token after receiving a 401 from the API."""
        source = cls._resolve_credential_source(integration, db, credential_source)

        normalized_client_id = (source.client_id or "").strip()
        if not normalized_client_id:
            raise RuntimeError(
                f"Credential source '{source.name}' (id={source.id}) has invalid client_id."
            )

        client_secret = integration_secrets.read_secret(source, "client_secret")
        auth = SophosAuthService(normalized_client_id, client_secret)
        repo = IntegrationRepository(db)

        try:
            logger.info(
                "TokenManager: forced re-auth after 401 for credential source %s", source.name
            )
            tokens = auth.authenticate()
            new_access = tokens["access_token"]
            new_refresh = tokens.get("refresh_token", "")
            if source.id == integration.id:
                region, tenant_id = auth.discover_region_and_tenant(new_access)
                repo.update_integration_tokens(
                    integration_id=integration.id,
                    access_token=new_access,
                    refresh_token=new_refresh,
                    region=region,
                    tenant_id=tenant_id,
                )
                return cls._build_headers(new_access, tenant_id)
            repo.update_tokens(
                source,
                access_token=new_access,
                refresh_token=new_refresh,
            )
            return cls._build_headers(new_access, integration.tenant_id)
        except Exception as exc:
            raise RuntimeError(
                f"TokenManager: forced re-auth failed for '{source.name}': {exc}"
            ) from exc
