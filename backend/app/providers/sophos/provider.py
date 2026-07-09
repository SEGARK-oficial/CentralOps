"""Sophos Central provider — wraps existing auth, XDR Query, and Endpoint services.

Hierarchy support (Partner Mode):
  * ``kind=partner``  / ``kind=organization``  → orchestrator integration. Owns
    OAuth credentials but does NOT make tenant-scoped API calls. Used to enumerate
    tenants and bootstrap children.
  * ``kind=tenant`` with ``parent_integration_id`` set → child integration. Reads
    its OAuth tokens from the parent (lazy-loaded) and tags every call with
    ``X-Tenant-ID = self.external_id``. Tokens never live on the child row —
    refresh is parent-scoped and serialised across all children via a Redis lock
    (key ``sophos:partner:{parent_id}:reauth``).
  * ``kind=tenant`` with no parent → standalone integration (legacy). Behaves
    exactly like before: full self-managed OAuth, region/tenant discovery on
    first auth.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import object_session

from ...db import database as _db_module
from ...db.models import Integration
from ...services import integration_secrets
from ...services.auth import SophosAuthService
from ...services.xdr_query import AsyncXDRQueryService, XDRQueryService
from ..base import (
    BaseProvider,
    HealthResult,
    QueryResult,
)

logger = logging.getLogger(__name__)


class SophosProvider(BaseProvider):
    platform = "sophos"

    # Redis lock used to coalesce reauth across N children of the same Partner.
    # Without it, 50 children seeing 401 simultaneously would each hit the
    # Sophos token endpoint and likely trigger rate-limit responses.
    REAUTH_LOCK_TTL_SECONDS = 30
    REAUTH_LOCK_WAIT_SECONDS = 25
    REAUTH_LOCK_POLL_INTERVAL = 0.5

    def __init__(self, integration: Integration) -> None:
        super().__init__(integration)
        self._parent_loaded = False
        self._parent: Optional[Integration] = None

    # ── Hierarchy helpers ──────────────────────────────────────────────

    def _load_parent_lazy(self) -> Optional[Integration]:
        """Fetch and cache the parent integration on first access.

        Detached from the request session — the cached row is used for read-only
        attribute access. Refreshing tokens always opens a fresh ``SessionLocal``
        so we never mutate a stale ORM-bound object.
        """
        if self._parent_loaded:
            return self._parent
        parent_id = self.integration.parent_integration_id
        if parent_id:
            with _db_module.SessionLocal() as db:
                self._parent = db.get(Integration, parent_id)
                if self._parent is not None:
                    db.expunge(self._parent)
        self._parent_loaded = True
        return self._parent

    def _credential_holder(self) -> Integration:
        """Return the integration that owns OAuth client_id/client_secret.

        Children with a parent always read from the parent. Standalone tenants
        and Partner roots read from themselves.
        """
        if self.integration.kind == "tenant":
            parent = self._load_parent_lazy()
            if parent is not None:
                return parent
        return self.integration

    def _persist_tokens(
        self,
        holder: Integration,
        *,
        access: str,
        refresh: str,
        columns: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Persiste tokens OAuth rotacionados de ``holder`` no store
        ``integration_credentials`` (+ colunas não-secret opcionais).

        Escreve pela Session viva de ``holder`` quando atado; senão abre uma
        ``SessionLocal`` fresca sobre a row re-fetchada e ESPELHA os segredos no
        ``holder`` detached em memória. O espelho detached é seguro: a row não está
        em nenhuma Session, então nunca dá flush — evita o INSERT duplicado que a
        ``UniqueConstraint(integration_id, logical_name)`` rejeitaria se o objeto
        atado de um request fosse comitado depois. ``columns`` são updates de
        colunas não-secret (region/tenant_id/external_id/id_type/…)."""
        columns = columns or {}
        try:
            sess = object_session(holder)
        except Exception:  # noqa: BLE001 — holder não-ORM (fake de teste) ⇒ detached
            sess = None
        if sess is not None:
            for key, value in columns.items():
                setattr(holder, key, value)
            integration_secrets.write_secret(holder, "access_token", access)
            integration_secrets.write_secret(holder, "refresh_token", refresh)
            holder.updated_at = datetime.utcnow()
            sess.commit()
            return
        with _db_module.SessionLocal() as db:
            row = db.get(Integration, holder.id)
            if row is None:
                raise RuntimeError(
                    f"Integration {holder.id} disappeared during token persist"
                )
            for key, value in columns.items():
                setattr(row, key, value)
            integration_secrets.write_secret(row, "access_token", access)
            integration_secrets.write_secret(row, "refresh_token", refresh)
            row.updated_at = datetime.utcnow()
            db.commit()
        # Espelho em memória no holder detached (não flusha — seguro).
        for key, value in columns.items():
            setattr(holder, key, value)
        integration_secrets.write_secret(holder, "access_token", access)
        integration_secrets.write_secret(holder, "refresh_token", refresh)

    def _effective_tenant_external_id(self) -> str:
        """Identifier used in ``X-Tenant-ID`` for child/tenant integrations.

        Prefers the new ``external_id`` column; falls back to the legacy
        ``tenant_id`` column for backwards compatibility with rows migrated
        from the pre-Partner schema.
        """
        return (self.integration.external_id or self.integration.tenant_id or "").strip()

    # ── Auth helpers ───────────────────────────────────────────────────

    def _get_auth_service(self) -> SophosAuthService:
        holder = self._credential_holder()
        client_id = (holder.client_id or "").strip()
        if not client_id:
            raise RuntimeError(f"Integration '{holder.name}' has no client_id")
        client_secret = integration_secrets.read_secret(holder, "client_secret")
        if not client_secret:
            raise RuntimeError(f"Integration '{holder.name}' has no client_secret")
        return SophosAuthService(client_id, client_secret)

    def _build_headers(self, access_token: str) -> Dict[str, str]:
        tenant_id = self._effective_tenant_external_id()
        if not tenant_id:
            raise RuntimeError(
                f"Integration '{self.integration.name}' missing tenant_id/external_id"
            )
        return {
            "Authorization": f"Bearer {access_token}",
            "X-Tenant-ID": tenant_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _ensure_valid_token(self) -> Dict[str, str]:
        """Return API headers, refreshing token (parent or self) if needed."""
        if self.integration.kind in ("partner", "organization"):
            raise NotImplementedError(
                "Partner/Organization integrations do not issue tenant-scoped "
                "API calls — use a child tenant integration."
            )
        if not self._effective_tenant_external_id():
            raise RuntimeError(
                f"Integration '{self.integration.name}' missing tenant_id/external_id"
            )
        holder = self._credential_holder()
        if not (holder.client_id or "").strip():
            raise RuntimeError(f"Integration '{holder.name}' has invalid client_id")

        try:
            token = integration_secrets.read_secret(holder, "access_token")
        except Exception:  # noqa: BLE001 — corrupted secret = treat as missing
            token = None
        if token:
            return self._build_headers(token)
        return self._full_reauth()

    def _full_reauth(self) -> Dict[str, str]:
        """Refresh the OAuth token. Dispatches to the parent-locked path when
        this is a child integration, or to the self path otherwise.
        """
        parent = self._load_parent_lazy() if self.integration.kind == "tenant" else None
        if parent is not None:
            return self._full_reauth_parent_locked(parent)
        return self._full_reauth_self()

    def _full_reauth_self(self) -> Dict[str, str]:
        """Standalone tenant flow (legacy): re-auth using own credentials and
        rediscover region/tenant_id."""
        auth = self._get_auth_service()
        tokens = auth.authenticate()
        new_access = tokens["access_token"]
        new_refresh = tokens.get("refresh_token", "")
        region, tenant_id = auth.discover_region_and_tenant(new_access)

        cols: Dict[str, Any] = {"region": region, "tenant_id": tenant_id}
        if not self.integration.external_id:
            cols["external_id"] = tenant_id
            cols["id_type"] = "tenant"
        self._persist_tokens(
            self.integration, access=new_access, refresh=new_refresh, columns=cols
        )
        return self._build_headers(new_access)

    def _full_reauth_parent_locked(self, parent: Integration) -> Dict[str, str]:
        """Refresh the *parent's* token under a Redis lock so that 50 children
        seeing a 401 don't all hit the token endpoint at once.

        Behaviour matrix:
          * Lock acquired → we authenticate, persist on the parent row, return.
          * Lock contended → we wait up to ``REAUTH_LOCK_WAIT_SECONDS``, then
            re-read the parent's token from the DB. If the holder finished
            successfully, we use the freshly-written token. If we timeout, we
            fall through to a self-refresh as a last resort.
          * No Redis available → fall back to per-call refresh (no serialisation).
        """
        lock_key = f"sophos:partner:{parent.id}:reauth"
        lock_value = uuid.uuid4().hex
        client = self._get_redis_client()
        acquired = False

        try:
            if client is not None:
                acquired = bool(
                    client.set(lock_key, lock_value, nx=True, ex=self.REAUTH_LOCK_TTL_SECONDS)
                )
            else:
                # No Redis: skip serialisation. Acceptable in dev / single-worker.
                acquired = True

            if not acquired:
                # Wait for holder to finish.
                deadline = time.monotonic() + self.REAUTH_LOCK_WAIT_SECONDS
                while time.monotonic() < deadline:
                    time.sleep(self.REAUTH_LOCK_POLL_INTERVAL)
                    try:
                        if client is None or not client.exists(lock_key):
                            break
                    except Exception:  # noqa: BLE001
                        break
                # Re-read parent from DB to pick up the freshly-written token.
                with _db_module.SessionLocal() as db:
                    refreshed = db.get(Integration, parent.id)
                    if refreshed is not None:
                        try:
                            access = integration_secrets.read_secret(refreshed, "access_token")
                        except Exception:  # noqa: BLE001
                            access = None
                        if access:
                            # read_secret já materializou .credentials (selectin)
                            # → seguro reusar o parent detached para reads futuros.
                            db.expunge(refreshed)
                            self._parent = refreshed
                            return self._build_headers(access)
                # No usable token after waiting — fall through and refresh ourselves.
                logger.warning(
                    "sophos: parent %s reauth lock expired without usable token; refreshing manually",
                    parent.id,
                )

            # We hold the lock (or fell through). Do the refresh.
            auth = SophosAuthService(
                (parent.client_id or "").strip(),
                integration_secrets.read_secret(parent, "client_secret"),
            )
            tokens = auth.authenticate()
            new_access = tokens["access_token"]
            new_refresh = tokens.get("refresh_token", "")
            with _db_module.SessionLocal() as db:
                fresh_parent = db.get(Integration, parent.id)
                if fresh_parent is None:
                    raise RuntimeError(
                        f"Parent integration {parent.id} disappeared during reauth"
                    )
                integration_secrets.write_secret(fresh_parent, "access_token", new_access)
                integration_secrets.write_secret(fresh_parent, "refresh_token", new_refresh)
                fresh_parent.updated_at = datetime.utcnow()
                db.commit()
                db.refresh(fresh_parent)
                _ = fresh_parent.credentials  # materializa selectin antes do expunge
                db.expunge(fresh_parent)
                self._parent = fresh_parent
            return self._build_headers(new_access)
        finally:
            if acquired and client is not None:
                # Release only if we still own the lock — minimal CAS without Lua.
                try:
                    current = client.get(lock_key)
                    if current == lock_value:
                        client.delete(lock_key)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass

    @staticmethod
    def _get_redis_client():
        """Return a synchronous redis client or ``None`` if unavailable.

        We use the synchronous client because providers run inside Celery tasks
        (sync workers) and FastAPI sync handlers; pulling in asyncio for the
        lock would force re-architecting the call sites.
        """
        try:
            import redis as _redis_sync

            from ...core.config import settings

            redis_url = settings.REDIS_URL or "redis://localhost:6379/0"
            return _redis_sync.Redis.from_url(redis_url, decode_responses=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("sophos: Redis unavailable for reauth lock: %s", exc)
            return None

    def _on_401(self) -> Dict[str, str]:
        return self._full_reauth()

    # ── Provider interface ─────────────────────────────────────────────

    def capabilities(self) -> List[str]:
        # o router gateia ações MSSP (children_count, bulk-op,
        # delete-cascade, backfill) pela capability "discover:children" — não por
        # ``if integration.kind in (...)``. O branch por kind vive AQUI (provider).
        if self.integration.kind in ("partner", "organization"):
            return ["discover:children", "partner:sync_tenants", "auth:test"]
        caps = [
            "health:check",
        ]
        # a capability de query (``query:xdr_data_lake``) é DERIVADA da
        # ``query_capability()`` (None para partner/org, já tratado acima) —
        # substitui o legado ``investigations:run`` e alinha runtime↔catálogo.
        caps += self._query_capability_keys()
        # licensing:list: só child tenants (gerenciados por um partner) têm
        # licenças de produto. O router gateia a preview de licenças por isto.
        if self.integration.parent_integration_id is not None:
            caps.append("licensing:list")
        return caps

    def query_capability(self):
        """Sophos partner/organization NÃO rodam query (rejeitam ``run_query``); só
        o child tenant tem o contrato de XDR Data Lake. Caso contrário, herda o
        default (lê do catálogo via registry)."""
        if self.integration.kind in ("partner", "organization"):
            return None
        return super().query_capability()

    def on_created(self) -> None:
        """Partner/Organization → dispara a descoberta assíncrona de tenants.

        Substitui o branch ``if integration.kind in (...)`` que vivia no router
        (lifecycle de criação é responsabilidade do provider). Tenant
        comum (kind=tenant) não dispara nada aqui — a validação de creds/region
        roda via ``test_connection`` no fluxo genérico do router."""
        if self.integration.kind not in ("partner", "organization"):
            return
        # the Sophos partner tenant-discovery task lives in
        # centralops_ee. Dispatch through the ee_hooks seam — the EE registers a
        # dispatcher; in Community there is none, so the integration row is still
        # created but the async discovery is skipped (logged).
        from ...core import ee_hooks

        dispatch = ee_hooks.get_partner_sync_dispatcher()
        if dispatch is None:
            logger.info(
                "Sophos partner tenant discovery is an Enterprise feature; skipping "
                "async sync for integration_id=%s (Community edition)",
                self.integration.id,
            )
            return
        dispatch(self.integration.id)

    def discover_tenants(self) -> List[Dict[str, Any]]:
        """Enumerate tenants under a Partner/Organization integration.

        Side effects:
          * Authenticates (or re-authenticates) and persists tokens on this row.
          * Persists ``id_type`` and ``external_id`` (= partner/org UUID) so the
            sync task and audit log can identify which Sophos identity owns the
            children created from this listing.
        """
        if self.integration.kind not in ("partner", "organization"):
            raise NotImplementedError(
                "discover_tenants is only valid on partner/organization integrations"
            )
        auth = self._get_auth_service()
        tokens = auth.authenticate()
        access_token = tokens["access_token"]
        identity = auth.discover_identity(access_token)
        # Persist the auth + identity on the partner row.
        with _db_module.SessionLocal() as db:
            target = db.get(Integration, self.integration.id)
            if target is None:
                raise RuntimeError(
                    f"Integration {self.integration.id} disappeared during discover_tenants"
                )
            integration_secrets.write_secret(target, "access_token", access_token)
            integration_secrets.write_secret(target, "refresh_token", tokens.get("refresh_token", ""))
            target.id_type = identity.get("id_type") or target.id_type
            if identity.get("id"):
                target.external_id = identity["id"]
            target.updated_at = datetime.utcnow()
            db.commit()
        # Update the in-memory copy for the caller's convenience.
        self.integration.id_type = identity.get("id_type") or self.integration.id_type
        if identity.get("id"):
            self.integration.external_id = identity["id"]
        return [dict(item) for item in auth.discover_tenants(access_token, identity)]

    def test_connection(self) -> HealthResult:
        try:
            auth = self._get_auth_service()
            tokens = auth.authenticate()
            access_token = tokens["access_token"]

            if self.integration.kind in ("partner", "organization"):
                identity = auth.discover_identity(access_token)
                with _db_module.SessionLocal() as db:
                    target = db.get(Integration, self.integration.id)
                    if target is None:
                        raise RuntimeError(
                            f"Integration {self.integration.id} not found"
                        )
                    integration_secrets.write_secret(target, "access_token", access_token)
                    integration_secrets.write_secret(target, "refresh_token", tokens.get("refresh_token", ""))
                    target.id_type = identity.get("id_type") or target.id_type
                    if identity.get("id"):
                        target.external_id = identity["id"]
                    target.updated_at = datetime.utcnow()
                    db.commit()
                # Reflect back in memory.
                self.integration.id_type = identity.get("id_type") or self.integration.id_type
                if identity.get("id"):
                    self.integration.external_id = identity["id"]
                # Validate that the caller registered the right kind.
                actual = identity.get("id_type", "")
                if actual and actual != self.integration.kind:
                    return HealthResult(
                        status="error",
                        details={
                            "id_type": actual,
                            "registered_kind": self.integration.kind,
                            "message": (
                                f"Sophos returned idType='{actual}' but the integration was "
                                f"registered as kind='{self.integration.kind}'. Adjust and retry."
                            ),
                        },
                    )
                return HealthResult(
                    status="healthy",
                    details={
                        "id_type": actual,
                        "id": identity.get("id"),
                        "message": "Authentication successful (Partner/Organization)",
                    },
                )

            # Partner-managed child: token autenticado pelo parent. region/
            # tenant_id já vêm do payload /partner/v1/tenants — NUNCA chamar
            # discover_region_and_tenant aqui (whoami do partner retorna
            # dataRegion="" e id=<partner_uuid>, sobrescrevendo o child com
            # valores errados).
            if self.integration.parent_integration_id is not None:
                return HealthResult(
                    status="healthy",
                    details={
                        "region": self.integration.region,
                        "tenant_id": (
                            self.integration.external_id or self.integration.tenant_id
                        ),
                        "api_host": self.integration.api_host,
                        "id_type": self.integration.id_type or "tenant",
                        "kind": self.integration.kind,
                        "message": (
                            "Partner credentials authenticated. Tenant-scoped "
                            "metadata (region/tenant_id/api_host) preserved from "
                            "Partner sync — not overwritten by whoami."
                        ),
                    },
                )

            # Standalone tenant flow (legacy).
            region, tenant_id = auth.discover_region_and_tenant(access_token)
            cols: Dict[str, Any] = {"region": region, "tenant_id": tenant_id}
            if not self.integration.external_id:
                cols["external_id"] = tenant_id
                cols["id_type"] = "tenant"
            self._persist_tokens(
                self.integration,
                access=access_token,
                refresh=tokens.get("refresh_token", ""),
                columns=cols,
            )

            return HealthResult(
                status="healthy",
                details={
                    "region": region,
                    "tenant_id": tenant_id,
                    "id_type": "tenant",
                    "message": "Authentication successful",
                },
            )
        except Exception as exc:  # noqa: BLE001
            return HealthResult(
                status="error",
                details={"message": str(exc)},
            )

    def health_check(self) -> HealthResult:
        try:
            if self.integration.kind in ("partner", "organization"):
                # For Partner/Org we revalidate the OAuth credentials via a
                # short-lived authenticate() — does not require a tenant header.
                auth = self._get_auth_service()
                auth.authenticate()
                return HealthResult(
                    status="healthy",
                    details={
                        "id_type": self.integration.id_type,
                        "external_id": self.integration.external_id,
                        "kind": self.integration.kind,
                        "authenticated": True,
                    },
                )
            self._ensure_valid_token()
            return HealthResult(
                status="healthy",
                details={
                    "region": self.integration.region,
                    "tenant_id": self._effective_tenant_external_id(),
                    "kind": self.integration.kind,
                    "authenticated": True,
                },
            )
        except Exception as exc:  # noqa: BLE001
            return HealthResult(
                status="error",
                details={"message": str(exc)},
            )

    def get_health_metrics(self) -> List[Any]:  # List[HealthMetric]
        """Return v2 HealthMetric list for Sophos Central.

        For Partner/Organization integrations, the panel shows auth + identity;
        tenant-scoped metrics (region, tenant_id) are skipped because they
        don't apply.
        """
        from ...schemas.health import HealthMetric  # local import avoids circular

        metrics: List[HealthMetric] = []

        try:
            if self.integration.kind in ("partner", "organization"):
                auth = self._get_auth_service()
                auth.authenticate()
            else:
                self._ensure_valid_token()
            metrics.append(HealthMetric(
                id="api_status",
                label="API Sophos Central",
                value="authenticated",
                severity="ok",
                icon_id="shield-check",
                group="api",
                hint="Token OAuth válido",
            ))
        except Exception as exc:  # noqa: BLE001
            logger.warning("sophos health_metrics: auth failure: %s", exc)
            metrics.append(HealthMetric(
                id="api_status",
                label="API Sophos Central",
                value="error",
                severity="critical",
                icon_id="shield-alert",
                group="api",
                hint="Falha de autenticação. Veja logs para detalhes.",
            ))
            return metrics

        if self.integration.kind in ("partner", "organization"):
            external_id = self.integration.external_id or ""
            metrics.append(HealthMetric(
                id="partner_status",
                label="Identidade Sophos",
                value=(self.integration.id_type or "unknown").title(),
                severity="ok",
                icon_id="users",
                group="api",
                hint=f"ID: {external_id[:12] + '…' if len(external_id) > 12 else external_id}",
            ))
        else:
            region = self.integration.region or ""
            tenant_id = self._effective_tenant_external_id()
            if region and tenant_id:
                metrics.append(HealthMetric(
                    id="tenant_status",
                    label="Tenant",
                    value=tenant_id[:8] + "…" if len(tenant_id) > 8 else tenant_id,
                    severity="ok",
                    icon_id="cloud",
                    group="api",
                    hint=f"Região: {region}",
                ))
            else:
                metrics.append(HealthMetric(
                    id="tenant_status",
                    label="Tenant",
                    value="not_discovered",
                    severity="warn",
                    icon_id="cloud",
                    group="api",
                    hint="Execute 'Testar Conexão' para descobrir região e tenant",
                ))

        auth_status = self.integration.auth_status or "unknown"
        sev_map = {"healthy": "ok", "error": "critical", "unknown": "unknown"}
        metrics.append(HealthMetric(
            id="auth_status",
            label="Status de autenticação",
            value=auth_status,
            severity=sev_map.get(auth_status, "unknown"),
            icon_id="check" if auth_status == "healthy" else "info",
            group="api",
            hint="Estado registrado da última verificação de conectividade",
        ))

        return metrics

    # ── Tenant-scoped operations (rejected for Partner/Organization) ──

    def _reject_if_partner(self, op: str) -> None:
        if self.integration.kind in ("partner", "organization"):
            raise NotImplementedError(
                f"Operation '{op}' is not supported on Partner/Organization "
                f"integrations — use a child tenant integration."
            )

    def run_query(self, statement: str, from_ts: str, to_ts: str, **kwargs) -> QueryResult:
        self._reject_if_partner("run_query")
        headers = self._ensure_valid_token()
        service = XDRQueryService(
            region=self.integration.region,
            headers=headers,
            tenant_id=self._effective_tenant_external_id(),
            on_401=self._on_401,
        )
        try:
            run_data = service.run_query(statement, from_ts, to_ts)
            run_id = run_data.get("id", "")
            result = service.wait_and_fetch(run_id)
            items = result.get("items", [])
            return QueryResult(items=items, total=len(items), run_id=run_id)
        finally:
            service.close()

    async def run_query_async(self, statement: str, from_ts: str, to_ts: str, **kwargs) -> QueryResult:
        self._reject_if_partner("run_query_async")
        headers = self._ensure_valid_token()
        service = AsyncXDRQueryService(
            region=self.integration.region,
            headers=headers,
            tenant_id=self._effective_tenant_external_id(),
            on_401=self._on_401,
        )
        try:
            run_data = await service.run_query(statement, from_ts, to_ts)
            run_id = run_data.get("id", "")
            result = await service.wait_and_fetch(run_id)
            items = result.get("items", [])
            return QueryResult(items=items, total=len(items), run_id=run_id)
        finally:
            service.close()

    # ── Async worker-releasing ─────────────────────────────
    # Submete o run no XDR Data Lake e devolve o run_id SEM esperar (libera o
    # worker); o poll-task chama poll_query até finalizar. Reusa as primitivas
    # já separadas do XDRQueryService (run_query=submit, get_run_status, get_results).

    def submit_query(self, statement: str, from_ts: str, to_ts: str, **kwargs) -> str:
        self._reject_if_partner("submit_query")
        headers = self._ensure_valid_token()
        service = XDRQueryService(
            region=self.integration.region,
            headers=headers,
            tenant_id=self._effective_tenant_external_id(),
            on_401=self._on_401,
        )
        try:
            run_data = service.run_query(statement, from_ts, to_ts)
            run_id = run_data.get("id", "")
            if not run_id:
                raise RuntimeError("XDR Query: submit não retornou run_id")
            return run_id
        finally:
            service.close()

    def poll_query(self, run_id: str, **kwargs):
        self._reject_if_partner("poll_query")
        headers = self._ensure_valid_token()
        service = XDRQueryService(
            region=self.integration.region,
            headers=headers,
            tenant_id=self._effective_tenant_external_id(),
            on_401=self._on_401,
        )
        try:
            status_data = service.get_run_status(run_id)
            status = status_data.get("status", "")
            result = status_data.get("result", "")
            if status == "finished" and result == "succeeded":
                items = service.get_results(run_id)
                return "finished", QueryResult(items=items, total=len(items), run_id=run_id)
            if status in {"failed", "cancelled", "canceled"}:
                return "failed", None
            return "running", None
        finally:
            service.close()

