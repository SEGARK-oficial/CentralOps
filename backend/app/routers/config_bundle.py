"""Config-as-code export/import for routing + destinations (GitOps).

Endpoints (admin-only, org-scoped):

- GET  /api/collectors/config/export
    Returns a versioned bundle JSON with all destinations (DestinationRead,
    NO secret_ref) and routes (RouteRead) visible to the caller's org.
    Safe to store in Git / CI — no credentials leak out.

- POST /api/collectors/config/import?dry_run=true|false
    Applies the bundle idempotently and transactionally.
    dry_run=true (default): validates + computes diff (created/updated/unchanged)
    without persisting anything.
    dry_run=false (apply): runs the same logic but commits to the DB.

Secret handling
───────────────
Destinations with ``has_secret=true`` in the bundle carry a placeholder
``secret_ref`` value (``"__SECRET_PLACEHOLDER__"``). On import these are
handled as follows:

  * If a ``secrets`` mapping is supplied in the request body
    (``{"dest_name": "plaintext-token", ...}``), the router encrypts each
    value via ``get_default_backend().encrypt()`` and stores it.
  * Otherwise, if the destination already exists in the DB (upsert path)
    its existing ``secret_ref`` is preserved unchanged.
  * If the destination is new AND no secret is provided, the destination is
    created without a credential — the caller must patch it separately.

RBAC
────
All endpoints require admin authentication (same as destinations/routes).
Non-global users are scoped to their own organization; global admins
export/import across the full fleet when ``?org_id=`` is provided.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..api.schemas_destinations import DestinationRead
from ..api.schemas_routes import RouteRead
from ..core import auth as app_auth
from ..core.errors import ApiError
from ..core.tenant import has_global_scope
from ..db import database, models, repository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/collectors/config", tags=["config-bundle"])

# Bundle format version — bump whenever the schema changes in a
# backwards-incompatible way.
BUNDLE_VERSION = "1.0"

# Placeholder written into ``secret_ref`` in exported bundles.
# Import code recognises this sentinel and substitutes the real credential
# from the ``secrets`` map in the request, or preserves the existing one.
_SECRET_PLACEHOLDER = "__SECRET_PLACEHOLDER__"


# ── Bundle schemas ─────────────────────────────────────────────────────


class ConfigBundle(BaseModel):
    """Versioned, secret-free snapshot of all destinations + routes for an org.

    Safe for storage in Git.  ``secret_ref`` fields inside
    ``destinations[*]`` are NEVER populated (``has_secret`` signals that a
    credential exists so the importer knows to look it up or supply one).
    """

    version: str = Field(..., description="Bundle format version (e.g. '1.0')")
    exported_at: datetime = Field(..., description="UTC timestamp of the export")
    organization_id: Optional[int] = Field(
        None,
        description=(
            "Organization the bundle belongs to. "
            "None = global (admin fleet-wide export)."
        ),
    )
    destinations: List[DestinationRead] = Field(default_factory=list)
    routes: List[RouteRead] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=False)


class ImportRequest(BaseModel):
    """Request body for POST /collectors/config/import.

    ``bundle`` — the ConfigBundle produced by the export endpoint (or
    maintained in a Git repository).

    ``secrets`` — optional mapping of destination *name* → plaintext secret
    (e.g. HEC token). The router encrypts each value before storing.
    Destinations in the bundle that have ``has_secret=true`` but are absent
    from this map keep their existing credential when updating, or are
    created without a credential when inserting.

    ``dry_run`` — when True (default) the endpoint validates the bundle,
    computes the diff, and returns it without touching the DB. Set to False
    to apply.
    """

    bundle: ConfigBundle
    secrets: Dict[str, str] = Field(
        default_factory=dict,
        description="dest_name → plaintext credential; encrypted before storage",
    )
    dry_run: bool = Field(
        default=True,
        description="Validate + diff only; set False to persist",
    )


class DestinationDiff(BaseModel):
    name: str
    status: str  # "created" | "updated" | "unchanged"
    id: Optional[str] = None


class RouteDiff(BaseModel):
    name: str
    status: str  # "created" | "updated" | "unchanged"
    id: Optional[str] = None


class ImportResult(BaseModel):
    dry_run: bool
    destinations: List[DestinationDiff] = Field(default_factory=list)
    routes: List[RouteDiff] = Field(default_factory=list)


# ── Dependency helpers ─────────────────────────────────────────────────


def _get_dest_repo(
    db: Session = Depends(database.get_session),
) -> repository.DestinationRepository:
    return repository.DestinationRepository(db)


def _get_route_repo(
    db: Session = Depends(database.get_session),
) -> repository.RouteRepository:
    return repository.RouteRepository(db)


def _resolve_scope(
    user: models.AppUser,
) -> tuple[bool, Optional[int]]:
    """Return (is_global, org_id)."""
    is_global = has_global_scope(user)
    raw_org = user.organization_id
    org_id: Optional[int] = int(raw_org) if raw_org is not None else None  # type: ignore[arg-type]
    return is_global, (org_id if not is_global else None)


# ── Serialisation helpers (mirror destinations.py / routes.py) ─────────


def _dest_row_to_read(row: models.Destination) -> DestinationRead:
    import json

    config: Dict[str, Any] = json.loads(str(row.config or "{}"))
    delivery: Dict[str, Any] = json.loads(str(row.delivery or "{}"))
    return DestinationRead(
        id=str(row.id),
        name=str(row.name),
        kind=str(row.kind),
        enabled=bool(row.enabled),
        config=config,
        delivery=delivery,
        config_version=str(row.config_version or ""),
        organization_id=int(row.organization_id) if row.organization_id is not None else None,
        created_at=row.created_at,  # type: ignore[arg-type]
        updated_at=row.updated_at,  # type: ignore[arg-type]
        has_secret=row.secret_ref is not None,
    )


def _route_row_to_read(row: models.Route) -> RouteRead:
    import json

    return RouteRead(
        id=str(row.id),
        name=str(row.name),
        priority=int(row.priority),
        condition=json.loads(str(row.condition or "{}")),
        action=str(row.action),
        destination_ids=json.loads(str(row.destination_ids or "[]")),
        is_final=bool(row.is_final),
        canary_percent=int(row.canary_percent),
        # ADR-0015: alavancas de redução. Sem estas linhas o export emitiria os
        # DEFAULTS do schema em vez dos valores da rota — pior que omitir, porque
        # um restore desconfiguraria a economia em silêncio e reporia
        # ``protect_detection`` para o default, descartando um opt-out consciente.
        protect_detection=bool(row.protect_detection),
        drop_raw=bool(row.drop_raw),
        sample_percent=int(row.sample_percent),
        suppress_key=row.suppress_key,
        suppress_allow=int(row.suppress_allow),
        suppress_window_s=int(row.suppress_window_s),
        transform_ref=row.transform_ref,  # type: ignore[arg-type]
        pii_redaction=(
            json.loads(str(row.pii_redaction))
            if getattr(row, "pii_redaction", None)
            else None
        ),
        enabled=bool(row.enabled),
        organization_id=int(row.organization_id) if row.organization_id is not None else None,
        created_at=row.created_at,  # type: ignore[arg-type]
        updated_at=row.updated_at,  # type: ignore[arg-type]
    )


def _dest_by_name(
    repo: repository.DestinationRepository,
    name: str,
    org_id: Optional[int],
    *,
    global_scope: bool,
) -> models.Destination | None:
    """Lookup a destination by name within the caller's scope."""
    rows = repo.list(org_id, global_scope=global_scope, include_disabled=True, limit=1000)
    for row in rows:
        if str(row.name) == name:
            return row
    return None


def _route_by_name(
    repo: repository.RouteRepository,
    name: str,
    org_id: Optional[int],
    *,
    global_scope: bool,
) -> models.Route | None:
    """Lookup a route by name within the caller's scope."""
    rows = repo.list(org_id, global_scope=global_scope, include_disabled=True, limit=1000)
    for row in rows:
        if str(row.name) == name:
            return row
    return None


def _validate_bundle_org_scope(
    bundle: ConfigBundle,
    is_global: bool,
    caller_org: Optional[int],
) -> None:
    """Reject a bundle whose org does not match the caller's scope."""
    if is_global:
        return  # global admins may import any bundle
    bundle_org = bundle.organization_id
    if bundle_org is not None and bundle_org != caller_org:
        raise ApiError(
            "config_bundle.org_mismatch",
            status.HTTP_403_FORBIDDEN,
            messages={
                "pt": (
                    "organization_id {bundle_org} do bundle não corresponde à "
                    "organização do solicitante {caller_org}"
                ),
                "en": (
                    "Bundle organization_id {bundle_org} does not match "
                    "the caller's organization {caller_org}"
                ),
                "es": (
                    "El organization_id {bundle_org} del paquete no coincide con "
                    "la organización del solicitante {caller_org}"
                ),
            },
            params={"bundle_org": bundle_org, "caller_org": caller_org},
        )


# ── Route condition validation (reuse routing engine) ─────────────────


def _validate_route_read(route: RouteRead) -> None:
    """Validate a RouteRead from the bundle against the routing engine.

    Raises ApiError (422) on any violation so the whole import is
    rejected before touching the DB.
    """
    from ..collectors.routing import (
        validate_condition,
        validate_pii_redaction,
        validate_suppress_key,
    )

    try:
        validate_condition(route.condition)
    except Exception as exc:
        raise ApiError(
            "config_bundle.invalid_route_condition",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            messages={
                "pt": "Rota {route_name!r}: condição inválida — {error}",
                "en": "Route {route_name!r}: invalid condition — {error}",
                "es": "Ruta {route_name!r}: condición inválida — {error}",
            },
            params={"route_name": route.name, "error": str(exc)},
        ) from exc

    if route.pii_redaction is not None:
        try:
            validate_pii_redaction(route.pii_redaction)
        except Exception as exc:
            raise ApiError(
                "config_bundle.invalid_route_pii_redaction",
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                messages={
                    "pt": "Rota {route_name!r}: pii_redaction inválido — {error}",
                    "en": "Route {route_name!r}: invalid pii_redaction — {error}",
                    "es": "Ruta {route_name!r}: pii_redaction inválido — {error}",
                },
                params={"route_name": route.name, "error": str(exc)},
            ) from exc

    # A chave de supressão usa a MESMA allowlist da condição. Sem esta checagem um
    # bundle (de seed, de outro ambiente ou anterior à validação) ressuscitava uma
    # assinatura degenerada, que agrupa tráfego demais e descarta em silêncio.
    # Ausente/None/vazia = supressão desligada e continua válida — quem trata isso
    # é o próprio ``validate_suppress_key``, por isso não há guarda de ``if`` aqui.
    try:
        validate_suppress_key(route.suppress_key)
    except Exception as exc:
        raise ApiError(
            "config_bundle.invalid_route_suppress_key",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            messages={
                "pt": "Rota {route_name!r}: suppress_key inválida — {error}",
                "en": "Route {route_name!r}: invalid suppress_key — {error}",
                "es": "Ruta {route_name!r}: suppress_key no válida — {error}",
            },
            params={"route_name": route.name, "error": str(exc)},
        ) from exc

    if route.action == "route" and not route.destination_ids:
        raise ApiError(
            "config_bundle.route_requires_destination",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            messages={
                "pt": "Rota {route_name!r}: ação 'route' requer ao menos um destination_id",
                "en": "Route {route_name!r}: action 'route' requires at least one destination_id",
                "es": "Ruta {route_name!r}: la acción 'route' requiere al menos un destination_id",
            },
            params={"route_name": route.name},
        )
    if route.action == "drop" and route.destination_ids:
        raise ApiError(
            "config_bundle.drop_must_not_have_destinations",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            messages={
                "pt": "Rota {route_name!r}: ação 'drop' não deve ter destination_ids",
                "en": "Route {route_name!r}: action 'drop' must not have destination_ids",
                "es": "Ruta {route_name!r}: la acción 'drop' no debe tener destination_ids",
            },
            params={"route_name": route.name},
        )


# ── GET /collectors/config/export ─────────────────────────────────────


@router.get("/export", response_model=ConfigBundle)
def export_config(
    user: models.AppUser = Depends(app_auth.require_admin_user),
    dest_repo: repository.DestinationRepository = Depends(_get_dest_repo),
    route_repo: repository.RouteRepository = Depends(_get_route_repo),
) -> ConfigBundle:
    """Export all destinations + routes for the caller's org as a bundle.

    **Secret safety**: ``secret_ref`` is NEVER included in the response.
    Each destination carries ``has_secret: bool`` so importers know whether
    to supply a credential on import.

    Global admins export the full fleet (all orgs).  Org-scoped admins
    export only their organisation's rows (plus global/NULL-org rows).
    """
    is_global, caller_org = _resolve_scope(user)

    dest_rows = dest_repo.list(
        caller_org,
        include_disabled=True,
        global_scope=is_global,
        limit=1000,
    )
    route_rows = route_repo.list(
        caller_org,
        include_disabled=True,
        global_scope=is_global,
        limit=1000,
    )

    bundle = ConfigBundle(
        version=BUNDLE_VERSION,
        exported_at=datetime.now(tz=timezone.utc),
        organization_id=caller_org,
        destinations=[_dest_row_to_read(r) for r in dest_rows],
        routes=[_route_row_to_read(r) for r in route_rows],
    )

    logger.info(
        "config_bundle: export org_id=%s dests=%d routes=%d by=%s",
        caller_org,
        len(bundle.destinations),
        len(bundle.routes),
        user.username,
    )
    return bundle


# ── POST /collectors/config/import ────────────────────────────────────


@router.post("/import", response_model=ImportResult)
def import_config(
    payload: ImportRequest,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    dest_repo: repository.DestinationRepository = Depends(_get_dest_repo),
    route_repo: repository.RouteRepository = Depends(_get_route_repo),
) -> ImportResult:
    """Apply (or dry-run) a config bundle into the DB.

    **Idempotent**: destinations and routes are matched by *name* within
    the org.  Rows that exist and are byte-identical to the bundle are
    marked *unchanged*; differing rows are *updated*; absent rows are
    *created*.

    **Transactional**: all mutations run inside a single session.  If any
    validation fails the entire import is rejected before touching the DB
    (even in apply mode).

    **dry_run=true** (default): validate + diff only.  Nothing is persisted.
    **dry_run=false**: validate + diff + commit.
    """
    bundle = payload.bundle
    is_global, caller_org = _resolve_scope(user)

    _validate_bundle_org_scope(bundle, is_global, caller_org)

    # Effective org for new rows: prefer bundle's org_id over caller's
    # (allows a global admin to import a bundle for a specific org).
    effective_org: Optional[int]
    if is_global:
        effective_org = bundle.organization_id
    else:
        effective_org = caller_org

    # ── Validate all routes against the routing engine BEFORE touching DB ──
    for route in bundle.routes:
        _validate_route_read(route)

    # ── Resolve secrets map (encrypt plaintext values) ─────────────────
    encrypted_secrets: Dict[str, str] = {}
    if payload.secrets:
        from ..core.secrets import get_default_backend

        backend = get_default_backend()
        for dest_name, plaintext in payload.secrets.items():
            try:
                encrypted_secrets[dest_name] = backend.encrypt(plaintext)
            except Exception as exc:
                # NÃO interpolar str(exc) no detail devolvido ao cliente: o erro
                # do backend de KMS pode carregar topologia do Vault (path/policy/
                # mount). Só o tipo. (O VaultTransitBackend já sanitiza na origem;
                # isto é defesa em profundidade p/ qualquer backend futuro.)
                raise ApiError(
                    "config_bundle.secret_encryption_failed",
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    messages={
                        "pt": (
                            "Falha ao criptografar o segredo do destino {dest_name!r} "
                            "({error_type})."
                        ),
                        "en": (
                            "Failed to encrypt secret for destination {dest_name!r} "
                            "({error_type})."
                        ),
                        "es": (
                            "Fallo al cifrar el secreto del destino {dest_name!r} "
                            "({error_type})."
                        ),
                    },
                    params={"dest_name": dest_name, "error_type": type(exc).__name__},
                ) from exc

    # ── Process destinations ────────────────────────────────────────────
    dest_diffs: List[DestinationDiff] = []
    # Map name → db row for later route ID resolution.
    name_to_dest_id: Dict[str, str] = {}

    for dest in bundle.destinations:
        existing = _dest_by_name(dest_repo, dest.name, effective_org, global_scope=is_global)

        # Resolve secret_ref for this destination.
        new_secret_ref: str | None
        if dest.name in encrypted_secrets:
            new_secret_ref = encrypted_secrets[dest.name]
        elif existing is not None:
            # Preserve existing credential on update (secret stays in DB).
            new_secret_ref = existing.secret_ref  # type: ignore[assignment]
        else:
            new_secret_ref = None  # new dest, no secret supplied

        if existing is None:
            # CREATE
            if not payload.dry_run:
                row = dest_repo.add(
                    name=dest.name,
                    kind=dest.kind,
                    config=dest.config,
                    delivery=dest.delivery,
                    secret_ref=new_secret_ref,
                    organization_id=effective_org,
                    enabled=dest.enabled,
                )
                name_to_dest_id[dest.name] = str(row.id)
            dest_diffs.append(DestinationDiff(name=dest.name, status="created"))
        else:
            name_to_dest_id[dest.name] = str(existing.id)
            # Detect actual changes (config/delivery/enabled/kind).
            import json

            existing_config = json.loads(str(existing.config or "{}"))
            existing_delivery = json.loads(str(existing.delivery or "{}"))
            changed = (
                existing_config != dest.config
                or existing_delivery != dest.delivery
                or bool(existing.enabled) != dest.enabled
                or str(existing.kind) != dest.kind
                or (dest.name in encrypted_secrets)  # secret rotation counts
            )
            if changed:
                if not payload.dry_run:
                    dest_repo.update(
                        str(existing.id),
                        config=dest.config,
                        delivery=dest.delivery,
                        enabled=dest.enabled,
                        secret_ref=new_secret_ref if dest.name in encrypted_secrets else repository._UNSET,
                    )
                dest_diffs.append(
                    DestinationDiff(name=dest.name, status="updated", id=str(existing.id))
                )
            else:
                dest_diffs.append(
                    DestinationDiff(name=dest.name, status="unchanged", id=str(existing.id))
                )

    # ── Process routes ──────────────────────────────────────────────────
    route_diffs: List[RouteDiff] = []

    for route in bundle.routes:
        existing_route = _route_by_name(
            route_repo, route.name, effective_org, global_scope=is_global
        )

        # Re-map destination_ids: if the bundle references a dest by the
        # exported id that has been re-created (new id), resolve via name.
        # The name_to_dest_id map covers dests that were just created above.
        resolved_dest_ids: List[str] = []
        for did in route.destination_ids:
            if did == "wazuh-default":
                resolved_dest_ids.append(did)
                continue
            # If the id is already known in the repo use it as-is (round-trip
            # idempotence); otherwise try the name map from this import batch.
            if dest_repo.get(did) is not None:
                resolved_dest_ids.append(did)
            else:
                # Look up by ID in name_to_dest_id values (reverse map).
                id_by_name = {v: k for k, v in name_to_dest_id.items()}
                if did in id_by_name:
                    resolved_dest_ids.append(did)
                else:
                    # Could not resolve — keep original id (validator will
                    # catch missing dests on apply via FK / route logic).
                    resolved_dest_ids.append(did)

        if existing_route is None:
            # CREATE
            if not payload.dry_run:
                row = route_repo.add(
                    name=route.name,
                    condition=route.condition,
                    destination_ids=resolved_dest_ids,
                    action=route.action,
                    is_final=route.is_final,
                    priority=route.priority,
                    enabled=route.enabled,
                    canary_percent=route.canary_percent,
                    # ADR-0015 — alavancas de redução preservadas no import.
                    protect_detection=route.protect_detection,
                    drop_raw=route.drop_raw,
                    sample_percent=route.sample_percent,
                    suppress_key=route.suppress_key,
                    suppress_allow=route.suppress_allow,
                    suppress_window_s=route.suppress_window_s,
                    transform_ref=route.transform_ref,
                    pii_redaction=route.pii_redaction,
                    organization_id=effective_org,
                    actor=str(user.username),
                )
            route_diffs.append(RouteDiff(name=route.name, status="created"))
        else:
            # Detect changes.
            import json

            existing_condition = json.loads(str(existing_route.condition or "{}"))
            existing_dest_ids = json.loads(str(existing_route.destination_ids or "[]"))
            existing_pii = (
                json.loads(str(existing_route.pii_redaction))
                if getattr(existing_route, "pii_redaction", None)
                else None
            )
            changed = (
                existing_condition != route.condition
                or existing_dest_ids != resolved_dest_ids
                or str(existing_route.action) != route.action
                or bool(existing_route.is_final) != route.is_final
                or int(existing_route.priority) != route.priority
                or bool(existing_route.enabled) != route.enabled
                or int(existing_route.canary_percent) != route.canary_percent
                # ADR-0015 — sem estas comparações, mudar SÓ uma alavanca de
                # redução no bundle seria detectado como "sem drift" e o import
                # viraria no-op silencioso.
                or bool(existing_route.protect_detection) != route.protect_detection
                or bool(existing_route.drop_raw) != route.drop_raw
                or int(existing_route.sample_percent) != route.sample_percent
                or existing_route.suppress_key != route.suppress_key
                or int(existing_route.suppress_allow) != route.suppress_allow
                or int(existing_route.suppress_window_s) != route.suppress_window_s
                or existing_route.transform_ref != route.transform_ref
                or existing_pii != route.pii_redaction
            )
            if changed:
                if not payload.dry_run:
                    route_repo.update(
                        str(existing_route.id),
                        condition=route.condition,
                        destination_ids=resolved_dest_ids,
                        action=route.action,
                        is_final=route.is_final,
                        priority=route.priority,
                        enabled=route.enabled,
                        canary_percent=route.canary_percent,
                        # ADR-0015 — alavancas de redução preservadas no update.
                        protect_detection=route.protect_detection,
                        drop_raw=route.drop_raw,
                        sample_percent=route.sample_percent,
                        suppress_key=route.suppress_key,
                        suppress_allow=route.suppress_allow,
                        suppress_window_s=route.suppress_window_s,
                        transform_ref=route.transform_ref,
                        pii_redaction=route.pii_redaction,
                        actor=str(user.username),
                        audit_action="imported",
                    )
                route_diffs.append(
                    RouteDiff(
                        name=route.name,
                        status="updated",
                        id=str(existing_route.id),
                    )
                )
            else:
                route_diffs.append(
                    RouteDiff(
                        name=route.name,
                        status="unchanged",
                        id=str(existing_route.id),
                    )
                )

    logger.info(
        "config_bundle: import dry_run=%s org_id=%s dests=%s routes=%s by=%s",
        payload.dry_run,
        effective_org,
        [(d.name, d.status) for d in dest_diffs],
        [(r.name, r.status) for r in route_diffs],
        user.username,
    )

    return ImportResult(
        dry_run=payload.dry_run,
        destinations=dest_diffs,
        routes=route_diffs,
    )
