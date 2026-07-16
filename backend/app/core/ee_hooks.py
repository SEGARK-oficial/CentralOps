"""Extension-point seam for the Enterprise edition.

Dependency-inversion: the Community Core declares optional override *slots* with
safe defaults that live IN the Core; the Enterprise package (``centralops_ee``)
registers real implementations at activation. **The Core never imports the EE** —
the dependency arrow is always EE -> Core (golden rule).

Slots so far:
  * **scope resolver** — consumed by
    :func:`backend.app.core.tenant.accessible_org_ids`. The Community default lives
    in ``tenant.py`` and PRESERVES today's subtree behavior (app pré-lançamento).
  * **quota guard** — consumed at the child-materialization call-site (the
    EE's ``partner_sync`` ``materialize_child`` after the carve-out). The Community
    default ``db.hierarchy.child_quota_exceeded`` is now a no-op ``False`` (no reseller
    quota in Community); the EE registers the real ``PartnerProgram`` quota.
  * **extra task modules** — consumed by
    ``collectors.celery_app._build_include``. Lets the EE add Celery task modules to
    the worker's ``include[]`` so its ``@shared_task`` names register (e.g. the
    partner-sync tasks once they move to ``centralops_ee``). Empty in
    Community → ``include[]`` unchanged.
  * **beat entries** — consumed by
    ``collectors.beat_schedule.build_schedule``. Lets the EE contribute periodic-task
    entries to the Beat schedule. Empty in Community → schedule unchanged.
  * **partner-sync dispatcher** — consumed by
    ``providers.sophos.provider.on_created``. Lets the EE dispatch the async partner
    tenant-discovery task. NO Community default (the task moves to ``centralops_ee`` in
    the carve-out) → unregistered means the call-site no-ops + logs.
  * **tenant-selection applier** — consumed by
    ``routers.integrations.select_tenants`` + the ``bulk-approve`` CLI. Lets the EE
    materialize/deactivate child tenants synchronously (truthful counts). NO Community
    default → unregistered means the selection state is persisted but no child is
    materialized (``enterprise_required``).

The two collection slots exist so the Celery worker/beat have a registration
seam (the worker never calls :func:`backend.app.core.edition.activate_enterprise`): the
EE registers task modules / beat entries in its worker/beat bootstrap *before* it
imports ``app.collectors.celery_app`` / ``beat_schedule``, so the extras are present at
construction/build time.

Both slots follow the SAME pattern: an optional override (``get_*`` returns ``None``
when unregistered) + the default kept at/near the call-site. A later carve-out
flips the Community defaults (FLAT scope / no-quota) and the EE
registers the real implementations here — without touching the ~21 router call-sites
nor the ``materialize_child`` body (they resolve ``tenant.X`` / ``ee_hooks.X``
per-call).

Intentionally dependency-light (no db/models import at runtime) to avoid import
cycles; the model types are referenced only under ``TYPE_CHECKING``.
"""
from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:  # avoid runtime import cycle and any db dependency
    from sqlalchemy.orm import Session

    from ..db import models

class LicenseRequiredError(RuntimeError):
    """Signal from an EE seam implementation: the EE artifact IS present but the
    ACTIVE LICENSE does not grant the feature the seam delivers (absent license,
    expired past the grace window, or a plan without the feature).

    Part of the seam CONTRACT (defined here so the Core can catch it precisely
    without ever importing the EE — the dependency arrow stays EE -> Core): a
    registered ``partner_sync_dispatcher`` / ``tenant_selection_applier`` MAY raise
    it instead of acting; Core call-sites translate it into the ``license_required``
    signal (``TenantSyncStatus`` / ``SelectTenantsResponse.license_required``),
    distinct from ``enterprise_required`` (= the EE artifact is ABSENT, Community).
    Never raised by the Core itself.
    """

    def __init__(self, feature: str, message: Optional[str] = None) -> None:
        self.feature = feature
        super().__init__(
            message
            or f"license_required: the active license does not grant feature {feature!r}"
        )


# Maps a scoped (non-global) user to the set of organization ids it may access,
# or ``None`` for "all orgs". Mirrors the contract of tenant.accessible_org_ids:
#   None     -> no filter (global scope)
#   set()    -> sees no org (scoped without organization_id)
#   set(...) -> the visible orgs
ScopeResolver = Callable[["models.AppUser", "Session"], "Optional[set[int]]"]

_lock = threading.Lock()
_scope_resolver: Optional[ScopeResolver] = None


def register_scope_resolver(fn: ScopeResolver) -> None:
    """Register the org-scope resolver. Called ONCE at boot/activation, before the
    first request (single-threaded). Idempotent when the SAME callable is
    re-registered; raises ``RuntimeError`` on a *conflicting* re-register so a silent
    double-wire fails loud (anti-drift, mirrors GitLab's ``Override`` guard). Test
    suites that construct the app per-module must call :func:`reset_scope_resolver`.
    """
    global _scope_resolver
    with _lock:
        if _scope_resolver is not None and _scope_resolver is not fn:
            raise RuntimeError(
                "scope resolver already registered; refusing conflicting re-register "
                "(call reset_scope_resolver() first in tests)"
            )
        _scope_resolver = fn


def get_scope_resolver() -> Optional[ScopeResolver]:
    """Return the registered resolver, or ``None`` when none is registered (the Core
    then falls back to its in-Core default)."""
    return _scope_resolver


def reset_scope_resolver() -> None:
    """Clear the registered resolver. For test fixtures only."""
    global _scope_resolver
    with _lock:
        _scope_resolver = None


# ── quota guard ─────────────────────────────────────────────────────────────────
# Returns True when the child-org quota is exceeded (a child must NOT be created).
# Default (no EE) is provided at the call-site (db.hierarchy.child_quota_exceeded
# today; no-op False after the open-core carve-out). Mirrors the scope-resolver slot.
QuotaGuard = Callable[["Session", "Optional[int]"], bool]

_quota_guard: Optional[QuotaGuard] = None


def register_quota_guard(fn: QuotaGuard) -> None:
    """Register the child-org quota guard. Same contract as
    :func:`register_scope_resolver`: called once at boot/activation, idempotent on the
    same callable, fail-loud on a conflicting re-register. In the worker, the EE
    registers this via the ``worker_process_init`` signal (the worker never calls
    ``activate()``)."""
    global _quota_guard
    with _lock:
        if _quota_guard is not None and _quota_guard is not fn:
            raise RuntimeError(
                "quota guard already registered; refusing conflicting re-register "
                "(call reset_quota_guard() first in tests)"
            )
        _quota_guard = fn


def get_quota_guard() -> Optional[QuotaGuard]:
    """Return the registered quota guard, or ``None`` (caller uses its default)."""
    return _quota_guard


def reset_quota_guard() -> None:
    """Clear the registered quota guard. For test fixtures only."""
    global _quota_guard
    with _lock:
        _quota_guard = None


# ── extra Celery task modules ────────────────────────────────────────────────────
# Dotted module paths the EE wants the Celery worker to import so its ``@shared_task``
# names register on the worker. ``celery_app._build_include()`` appends these to the
# Core ``include[]`` at construction. Empty in Community (behavior-preserving). The EE
# registers them in its worker/beat bootstrap BEFORE importing ``celery_app``.
_extra_task_modules: tuple[str, ...] = ()


def register_extra_task_modules(modules: Iterable[str]) -> None:
    """Register EE Celery task-module dotted paths. Same boot-time, fail-loud-on-
    conflict contract as the override slots: idempotent when an EQUAL set of modules
    is re-registered, ``RuntimeError`` on a conflicting one. Validates that every entry
    is a non-empty ``str`` so a malformed plugin fails loud instead of silently
    dropping tasks."""
    global _extra_task_modules
    mods = tuple(modules)
    if not all(isinstance(m, str) and m for m in mods):
        raise TypeError("extra task modules must be non-empty dotted-path strings")
    with _lock:
        if _extra_task_modules and _extra_task_modules != mods:
            raise RuntimeError(
                "extra task modules already registered; refusing conflicting "
                "re-register (call reset_extra_task_modules() first in tests)"
            )
        _extra_task_modules = mods


def get_extra_task_modules() -> tuple[str, ...]:
    """Return the registered EE task-module dotted paths (``()`` when none)."""
    return _extra_task_modules


def reset_extra_task_modules() -> None:
    """Clear the registered extra task modules. For test fixtures only."""
    global _extra_task_modules
    with _lock:
        _extra_task_modules = ()


# ── extra Celery beat entries ────────────────────────────────────────────────────
# Periodic-task entries (name -> entry dict) the EE contributes to the Beat schedule
# (e.g. the Sophos partner sync once it moves to ``centralops_ee``). Core's
# ``beat_schedule.build_schedule()`` merges these over its static entries. Empty in
# Community (behavior-preserving). The EE registers them in its beat bootstrap BEFORE
# importing ``beat_schedule``.
_beat_entries: dict[str, Any] = {}


def register_beat_entries(entries: Mapping[str, Any]) -> None:
    """Register EE Beat schedule entries. Same fail-loud-on-conflict contract;
    idempotent on an EQUAL mapping. Validates the argument is a mapping keyed by
    ``str`` (entry names)."""
    global _beat_entries
    if not isinstance(entries, Mapping) or not all(
        isinstance(k, str) for k in entries
    ):
        raise TypeError("beat entries must be a mapping keyed by entry-name strings")
    new = dict(entries)
    with _lock:
        if _beat_entries and _beat_entries != new:
            raise RuntimeError(
                "beat entries already registered; refusing conflicting re-register "
                "(call reset_beat_entries() first in tests)"
            )
        _beat_entries = new


def get_beat_entries() -> dict[str, Any]:
    """Return a COPY of the registered EE beat entries (``{}`` when none) so callers
    can merge without mutating the registry."""
    return dict(_beat_entries)


def reset_beat_entries() -> None:
    """Clear the registered beat entries. For test fixtures only."""
    global _beat_entries
    with _lock:
        _beat_entries = {}


# ── partner-sync dispatcher ──────────────────────────────────────────────────────
# Dispatches the async Sophos partner-tenant discovery for a partner/organization
# integration. Consumed by ``providers.sophos.provider.on_created`` (and any other
# "a partner integration was created" trigger). The Community Core has NO default
# (the auto-discovery task lives in ``centralops_ee`` after the carve-out): when no
# dispatcher is registered the call-site no-ops + logs. The EE registers a dispatcher
# that does ``sync_sophos_partner.delay(integration_id)`` — in the API process (via
# ``activate``) AND on the Celery worker (via its own entrypoint, since the worker
# never calls ``activate``). Mirrors the scope/quota override slots.
# License contract: a registered dispatcher MAY raise :class:`LicenseRequiredError`
# instead of dispatching (EE present, license without the feature) — call-sites
# translate it into the ``license_required`` signal instead of a 5xx.
PartnerSyncDispatcher = Callable[[int], None]

_partner_sync_dispatcher: Optional[PartnerSyncDispatcher] = None


def register_partner_sync_dispatcher(fn: PartnerSyncDispatcher) -> None:
    """Register the partner-sync dispatcher. Same boot-time, fail-loud-on-conflict
    contract as the override slots: idempotent on the SAME callable, ``RuntimeError``
    on a conflicting re-register. Register a module-level singleton (not a per-call
    closure) so registering in both ``activate`` and the worker entrypoint stays
    idempotent."""
    global _partner_sync_dispatcher
    with _lock:
        if _partner_sync_dispatcher is not None and _partner_sync_dispatcher is not fn:
            raise RuntimeError(
                "partner sync dispatcher already registered; refusing conflicting "
                "re-register (call reset_partner_sync_dispatcher() first in tests)"
            )
        _partner_sync_dispatcher = fn


def get_partner_sync_dispatcher() -> Optional[PartnerSyncDispatcher]:
    """Return the registered dispatcher, or ``None`` (Community: the caller no-ops)."""
    return _partner_sync_dispatcher


def reset_partner_sync_dispatcher() -> None:
    """Clear the registered partner-sync dispatcher. For test fixtures only."""
    global _partner_sync_dispatcher
    with _lock:
        _partner_sync_dispatcher = None


# ── tenant-selection applier ─────────────────────────────────────────────────────
# Applies approve/exclude tenant selections SYNCHRONOUSLY (materialize/deactivate the
# child orgs) and returns truthful counts. Consumed by ``routers.integrations.
# select_tenants`` and the ``bulk-approve --apply`` CLI. Reseller child-tenant
# management is an Enterprise feature, so there is NO Community default — when no
# applier is registered the call-site persists the selection state (Community) and
# reports ``enterprise_required`` WITHOUT materializing (no fake counts, no 500). The
# EE registers an applier in ``activate`` that reuses its ``materialize_child`` /
# ``deactivate_child``. Contract:
#   applier(session, partner_integration, selections, state) -> dict with int keys
#   "materialized"/"deactivated"/"pending" and "errors": list of
#   {"external_id": str, "reason": str}.
# License contract: a registered applier MAY raise :class:`LicenseRequiredError`
# BEFORE materializing (EE present, license without the feature) — call-sites keep
# the persisted selections and report ``license_required`` instead of a 5xx.
TenantSelectionApplier = Callable[..., "dict[str, Any]"]

_tenant_selection_applier: Optional[TenantSelectionApplier] = None


def register_tenant_selection_applier(fn: TenantSelectionApplier) -> None:
    """Register the tenant-selection applier. Same boot-time, fail-loud-on-conflict
    contract as the other slots (idempotent on the SAME callable, ``RuntimeError`` on a
    conflicting re-register)."""
    global _tenant_selection_applier
    with _lock:
        if (
            _tenant_selection_applier is not None
            and _tenant_selection_applier is not fn
        ):
            raise RuntimeError(
                "tenant selection applier already registered; refusing conflicting "
                "re-register (call reset_tenant_selection_applier() first in tests)"
            )
        _tenant_selection_applier = fn


def get_tenant_selection_applier() -> Optional[TenantSelectionApplier]:
    """Return the registered applier, or ``None`` (Community: no materialization)."""
    return _tenant_selection_applier


def reset_tenant_selection_applier() -> None:
    """Clear the registered tenant-selection applier. For test fixtures only."""
    global _tenant_selection_applier
    with _lock:
        _tenant_selection_applier = None


# ── cost pricer ──────────────────────────────────────────────────────────────────
# Turns metered VOLUME into MONEY. The Community core meters bytes/events (in vs out)
# and exposes raw volume + a unitless ratio at ``GET /collectors/cost-summary``; pricing
# (US$/GB tables, savings-in-US$, per-org cost policy) is an Enterprise feature, so there
# is NO Community default — when no pricer is registered the endpoint omits the USD block
# (ratio + volume only). The EE registers a pricer in ``activate`` that prices a row.
# Mirrors the other override slots (fail-loud on conflicting re-register; the Core never
# imports the EE). Contract:
#   pricer(organization_id, destination_id, gigabytes) -> {"usd": float, "currency": str}
CostPricer = Callable[[int, "Optional[str]", float], "dict[str, Any]"]

_cost_pricer: Optional[CostPricer] = None


def register_cost_pricer(fn: CostPricer) -> None:
    """Register the cost pricer. Same boot-time, fail-loud-on-conflict
    contract as the other slots: idempotent on the SAME callable, ``RuntimeError`` on a
    conflicting re-register. Register a module-level singleton so registering in both
    ``activate`` and a worker entrypoint stays idempotent."""
    global _cost_pricer
    with _lock:
        if _cost_pricer is not None and _cost_pricer is not fn:
            raise RuntimeError(
                "cost pricer already registered; refusing conflicting re-register "
                "(call reset_cost_pricer() first in tests)"
            )
        _cost_pricer = fn


def get_cost_pricer() -> Optional[CostPricer]:
    """Return the registered cost pricer, or ``None`` (Community: the cost-summary
    endpoint returns volume + ratio only, no USD)."""
    return _cost_pricer


def reset_cost_pricer() -> None:
    """Clear the registered cost pricer. For test fixtures only."""
    global _cost_pricer
    with _lock:
        _cost_pricer = None


# ── hierarchy materializer ───────────────────────────────────────────────────────
# Materializes ONE org node's hierarchy given its parent id: sets parent/root/depth
# and (re)writes the ``org_closure`` edges. The closure table + subtree depth are an
# Enterprise concern (the ``OrgClosure`` model moved to ``centralops_ee``), so
# the Community default is FLAT — ``db.hierarchy.materialize_node`` sets ``root_id=self,
# depth=0`` and writes NO closure (Community is single-tenant / flat). The EE registers
# the real subtree materializer here (writes closure, derives root/depth from parent).
# Consumed by ``db.hierarchy.materialize_node`` — so BOTH runtime org creation
# (``assign_on_create``) and the boot ``backfill_hierarchy`` route to the right impl.
# Mirrors the other override slots (fail-loud on conflict; the Core never imports EE).
HierarchyMaterializer = Callable[["Session", "models.Organization", "Optional[int]"], None]

_hierarchy_materializer: Optional[HierarchyMaterializer] = None


def register_hierarchy_materializer(fn: HierarchyMaterializer) -> None:
    """Register the subtree hierarchy materializer.
    Same boot-time, fail-loud-on-conflict contract as the other slots: idempotent on the
    SAME callable, ``RuntimeError`` on a conflicting re-register. Register a module-level
    singleton so registering in both ``activate`` and the worker entrypoint is idempotent."""
    global _hierarchy_materializer
    with _lock:
        if _hierarchy_materializer is not None and _hierarchy_materializer is not fn:
            raise RuntimeError(
                "hierarchy materializer already registered; refusing conflicting "
                "re-register (call reset_hierarchy_materializer() first in tests)"
            )
        _hierarchy_materializer = fn


def get_hierarchy_materializer() -> Optional[HierarchyMaterializer]:
    """Return the registered materializer, or ``None`` (Community: FLAT default in
    ``db.hierarchy.materialize_node`` — root=self, depth=0, no closure)."""
    return _hierarchy_materializer


def reset_hierarchy_materializer() -> None:
    """Clear the registered hierarchy materializer. For test fixtures only."""
    global _hierarchy_materializer
    with _lock:
        _hierarchy_materializer = None
