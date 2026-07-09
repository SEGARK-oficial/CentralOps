"""REST endpoints for Destinations CRUD.

Endpoints (all admin-only):

- ``GET  /api/collectors/destinations``              — list (org-scoped)
- ``POST /api/collectors/destinations``              — create
- ``GET  /api/collectors/destinations/destination-types`` — catalog
- ``GET  /api/collectors/destinations/{id}``         — get by id
- ``PUT  /api/collectors/destinations/{id}``         — partial update
- ``DELETE /api/collectors/destinations/{id}``       — hard delete
- ``POST /api/collectors/destinations/{id}/test``    — live probe

Security invariants:
  - ``hec_token`` / ``secret_ref`` NEVER appear in responses, logs, or audit.
  - Non-global users only see/mutate destinations scoped to their org.
  - /test builds an ephemeral Destination via registry.build() + close() —
    it NEVER calls destination_cache.get_destination().
  - Multi-destination dispatch is GA: configuring an
    enabled destination here gets an auto-created ``{} -> [dest]`` route, so it
    receives events on the next collection cycle. Routing is GA (single model):
    edit/add ``routes`` to refine selection (first-match, catch-all -> wazuh-default).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..api.schemas_destinations import (
    CredentialAccessEntry,
    CredentialAuditResponse,
    CredentialRotateRequest,
    CredentialRotateResponse,
    CredentialRevokeResponse,
    DestinationAuditEntry,
    DestinationAuditResponse,
    DestinationCreate,
    DestinationDlqEntry,
    DestinationDlqResponse,
    DestinationHealthBatchResponse,
    DestinationHealthItem,
    DestinationHealthResponse,
    DestinationLineageResponse,
    DestinationMetricsResponse,
    DestinationRead,
    DestinationTapResponse,
    DestinationShadowRequest,
    DestinationShadowResponse,
    DestinationTestResponse,
    DestinationTypeRead,
    DestinationUpdate,
    DlqReprocessRequest,
    DlqReprocessResponse,
    EventLineageResponse,
    LineageEntry,
)
from ..collectors.output.destinations import registry as _registry
from ..collectors.output.destinations.registry import DestinationConfig
from ..core import auth as app_auth
from ..core.config import settings
from ..core.errors import ApiError

_RWIN = int(getattr(settings, "OBS_RATE_WINDOW_MINUTES", 5) or 5)  # janela da média móvel de taxa (EPS/bytes)
from ..core.secrets import get_default_backend
from ..core.tenant import has_global_scope
from ..db import database, models, repository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/collectors/destinations", tags=["destinations"])

# Separate router for the org-level lineage query endpoint (GET /collectors/lineage/{event_id}).
# Registered in main.py alongside ``router``.
lineage_router = APIRouter(prefix="/collectors/lineage", tags=["destinations"])

# Fields that must never appear in audit logs.
_AUDIT_SCRUB = frozenset({"hec_token", "secret_ref", "token"})


# ── Dependency helpers ────────────────────────────────────────────────


def _get_repo(db: Session = Depends(database.get_session)) -> repository.DestinationRepository:
    return repository.DestinationRepository(db)


# ── Serialization helpers ─────────────────────────────────────────────


def _row_to_read(row: models.Destination) -> DestinationRead:
    """Convert an ORM row to DestinationRead. Secrets are never included."""
    # Explicit str() casts work around the SQLAlchemy Column[T] mypy inference
    # limitation — the same pattern seen in collector_config.py (pre-existing).
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
        data_residency=str(row.data_residency) if row.data_residency is not None else None,
        is_default=bool(row.is_default),
    )


def _apply_is_default(db, row: models.Destination, org_id: Optional[int], value: bool) -> None:
    """vendor-neutro: garante no MÁX. 1 destino default (catch-all) por org.

    ``value=True`` → limpa o flag dos demais da MESMA org (escopo NULL = global) e
    marca este; ``value=False`` → só desmarca este. Mesma sessão do create/update."""
    from sqlalchemy import update as _update

    if value:
        db.execute(
            _update(models.Destination)
            .where(
                models.Destination.organization_id.is_(None)
                if org_id is None
                else models.Destination.organization_id == org_id,
                models.Destination.id != row.id,
                models.Destination.is_default.is_(True),
            )
            .values(is_default=False)
        )
    row.is_default = bool(value)
    db.commit()


def _scrub_for_audit(data: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively redact sensitive fields from audit payloads."""
    result: Dict[str, Any] = {}
    for key, value in data.items():
        if key.lower() in _AUDIT_SCRUB:
            result[key] = "[REDACTED]"
        elif isinstance(value, dict):
            result[key] = _scrub_for_audit(value)
        else:
            result[key] = value
    return result


def _resolve_scope(user: models.AppUser) -> tuple[bool, Optional[int]]:
    """Return (global_scope, org_id) for the requesting user."""
    is_global = has_global_scope(user)
    raw_org = user.organization_id
    org_id: Optional[int] = int(raw_org) if raw_org is not None else None  # type: ignore[arg-type]
    return is_global, org_id if not is_global else None


def _assert_visible(
    row: models.Destination | None,
    user: models.AppUser,
) -> models.Destination:
    """Return row if visible to user, else raise 404 (anti-enumeration).

    Global-scope users see everything. Tenant-scoped users see rows where
    organization_id == their org OR organization_id IS NULL (global rows).
    """
    if row is None:
        raise ApiError(
            "destination.not_found",
            status.HTTP_404_NOT_FOUND,
            messages={
                "pt": "Destino não encontrado.",
                "en": "Destination not found.",
                "es": "Destino no encontrado.",
            },
        )
    is_global, org_id = _resolve_scope(user)
    if is_global:
        return row
    # Row is visible if it is global or belongs to the user's org.
    if row.organization_id is not None and row.organization_id != org_id:
        raise ApiError(
            "destination.not_found",
            status.HTTP_404_NOT_FOUND,
            messages={
                "pt": "Destino não encontrado.",
                "en": "Destination not found.",
                "es": "Destino no encontrado.",
            },
        )
    return row


def _assert_mutable(
    row: models.Destination | None,
    user: models.AppUser,
) -> models.Destination:
    """``_assert_visible`` + trava de ESCRITA: linha GLOBAL
    (``organization_id`` NULL) é infraestrutura COMPARTILHADA entre todas as orgs
    (ex.: o catch-all ``wazuh-default``). Um admin-de-org a enxerga (roteia para
    ela), mas mutá-la — editar, deletar, girar credencial, reprocessar DLQ —
    afetaria TODOS os tenants; só admin de PLATAFORMA (escopo global) pode."""
    row = _assert_visible(row, user)
    is_global, _ = _resolve_scope(user)
    if not is_global and row.organization_id is None:
        raise ApiError(
            "destination.global_requires_platform_admin",
            status.HTTP_403_FORBIDDEN,
            messages={
                "pt": "Destino global (compartilhado) só pode ser alterado por um administrador de plataforma.",
                "en": "A global (shared) destination can only be changed by a platform administrator.",
                "es": "Un destino global (compartido) solo puede ser modificado por un administrador de plataforma.",
            },
        )
    return row


# ── GET /destination-types ────────────────────────────────────────────
# NOTE: This route MUST be declared before /{id} to avoid FastAPI
# treating "destination-types" as a path parameter.


@router.get("/destination-types", response_model=List[DestinationTypeRead])
def get_destination_types(
    _: models.AppUser = Depends(app_auth.require_admin_user),
) -> List[DestinationTypeRead]:
    """Return the catalog of registered destination kinds.

    Consumed by the UI to render destination-type selectors without
    hard-coding kind names on the frontend.
    """
    return [DestinationTypeRead(**entry) for entry in _registry.describe_all()]


# ── POST "" ───────────────────────────────────────────────────────────


@router.post("", response_model=DestinationRead, status_code=status.HTTP_201_CREATED)
def create_destination(
    payload: DestinationCreate,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    repo: repository.DestinationRepository = Depends(_get_repo),
) -> DestinationRead:
    """Create a new Destination.

    If ``hec_token`` is provided it is encrypted via the default secrets
    backend before storage. The plaintext token is never persisted or
    logged.
    """
    secret_ref: str | None = None
    if payload.hec_token:
        secret_ref = get_default_backend().encrypt(payload.hec_token)

    # Determine effective org: non-global users cannot create destinations
    # outside their own org.
    is_global, caller_org = _resolve_scope(user)
    org_id = payload.organization_id
    if not is_global and org_id is not None and org_id != caller_org:
        raise ApiError(
            "destination.cross_org_create_denied",
            status.HTTP_403_FORBIDDEN,
            messages={
                "pt": "Não é possível criar destino para uma organização diferente",
                "en": "Cannot create destination for a different organization",
                "es": "No es posible crear un destino para una organización diferente",
            },
        )
    if not is_global and org_id is None and caller_org is not None:
        # Default to caller's org when not explicitly set
        org_id = caller_org

    try:
        row = repo.add(
            name=payload.name,
            kind=payload.kind,
            config=payload.config,
            delivery=payload.delivery,
            secret_ref=secret_ref,
            organization_id=org_id,
            enabled=payload.enabled,
            data_residency=payload.data_residency,
            actor=user.username,  # type: ignore[arg-type]
        )
    except IntegrityError:
        raise ApiError(
            "destination.name_conflict",
            status.HTTP_409_CONFLICT,
            messages={
                "pt": "Já existe um destino chamado {name!r}",
                "en": "A destination named {name!r} already exists",
                "es": "Ya existe un destino llamado {name!r}",
            },
            params={"name": payload.name},
        )

    # broadcast por default via uma rota auto-criada
    # ``{} → [dest]`` (clone+continue) — modelo Cribl/Vector "tudo é rota". O
    # destino recebe todos os eventos do seu escopo, de forma VISÍVEL e editável
    # em /routes; o operador refina (condição/severity/labels) ou exclui depois.
    # ``is_final=False`` para o evento continuar até o catch-all wazuh-default.
    # Mesma sessão/transação que o destino → atômico (rollback junto em falha).
    # ``auto_route=false`` no payload pula isto (roteamento explícito puro).
    if getattr(payload, "auto_route", True):
        repository.RouteRepository(repo.db).add(
            name=f"Broadcast → {row.name}",
            condition={},
            destination_ids=[str(row.id)],
            action="route",
            is_final=False,
            priority=100,
            organization_id=org_id,
            actor=user.username,  # type: ignore[arg-type]
        )

    # vendor-neutro: marca como fallback (catch-all) da org se pedido.
    if getattr(payload, "is_default", False):
        _apply_is_default(repo.db, row, org_id, True)

    audit_data = _scrub_for_audit(payload.model_dump())
    logger.info(
        "destinations: created id=%s name=%r kind=%s org=%s by user=%s",
        row.id,
        row.name,
        row.kind,
        org_id,
        user.username,
        extra={"audit": audit_data},
    )
    return _row_to_read(row)


# ── GET "" ────────────────────────────────────────────────────────────


@router.get("", response_model=List[DestinationRead])
def list_destinations(
    org_id: Optional[int] = Query(default=None, description="Filter by org (global admin only)"),
    include_disabled: bool = Query(default=False),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    user: models.AppUser = Depends(app_auth.require_admin_user),
    repo: repository.DestinationRepository = Depends(_get_repo),
) -> List[DestinationRead]:
    """List destinations visible to the caller.

    Non-global users: see only their org's destinations + global rows.
    Global users: ``?org_id`` scopes to a specific org; absent → all rows.
    """
    is_global, caller_org = _resolve_scope(user)

    if is_global:
        effective_org = org_id  # None = show all; int = filter
        effective_global = org_id is None
    else:
        effective_org = caller_org
        effective_global = False

    rows = repo.list(
        effective_org,
        include_disabled=include_disabled,
        offset=offset,
        limit=limit,
        global_scope=effective_global,
    )
    return [_row_to_read(r) for r in rows]


# ── GET /health (BATCH) ──────────────
# NOTE: MUST be declared BEFORE /{destination_id} so the single literal
# segment "health" is matched by THIS handler and not captured as an id by
# the /{destination_id} route. FastAPI resolves by declaration order; the
# test ``test_batch_health_route_does_not_collide_with_id`` guards this.


@router.get("/health", response_model=DestinationHealthBatchResponse)
async def destinations_health_batch(
    include_disabled: bool = Query(default=True),
    user: models.AppUser = Depends(app_auth.require_admin_user),
    repo: repository.DestinationRepository = Depends(_get_repo),
) -> DestinationHealthBatchResponse:
    """Batch health for EVERY destination visible to the caller — one call feeds
    the UI's destinations list (health badge + EPS/bytes per row) without the N+1
    of hitting ``/{id}/health`` per destination.

    Same org-scope/visibility as ``list_destinations`` (non-global users see only
    their org's destinations + global rows). Each item has the same shape as the
    single ``GET /{id}/health`` (status/breaker_state/dlq/eps/bytes) plus
    name/kind. ``include_disabled`` defaults True so disabled destinations still
    surface a ``disabled`` badge in the UI. Admin-only.
    """
    is_global, caller_org = _resolve_scope(user)

    if is_global:
        effective_org: Optional[int] = None
        effective_global = True
    else:
        effective_org = caller_org
        effective_global = False

    rows = repo.list(
        effective_org,
        include_disabled=include_disabled,
        offset=0,
        limit=200,
        global_scope=effective_global,
    )

    async def _safe_item(row: models.Destination) -> DestinationHealthItem:
        # Isolamento por item: a saúde de UM destino (Redis/breaker indisponível)
        # nunca derruba o lote inteiro — degrada esse item para "unknown".
        try:
            health = await _compute_destination_health(
                row, org_id=caller_org, global_scope=is_global, repo=repo
            )
            return DestinationHealthItem(**health)
        except Exception:  # pragma: no cover — caminho defensivo
            logger.warning(
                "health em lote falhou p/ destino %s — degradando p/ unknown",
                getattr(row, "id", "?"),
                exc_info=True,
            )
            return DestinationHealthItem(
                destination_id=str(row.id),
                name=row.name,
                kind=row.kind,
                status="unknown",
                enabled=bool(row.enabled),
            )

    # Paraleliza as leituras (cada item faz I/O Redis): evita o N+1 serial.
    items: List[DestinationHealthItem] = list(
        await asyncio.gather(*[_safe_item(row) for row in rows])
    )
    return DestinationHealthBatchResponse(total=len(items), items=items)


# ── GET /{id} ─────────────────────────────────────────────────────────


@router.get("/{destination_id}", response_model=DestinationRead)
def get_destination(
    destination_id: str,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    repo: repository.DestinationRepository = Depends(_get_repo),
) -> DestinationRead:
    """Fetch a single destination by ID.

    Returns 404 (not 403) when the destination belongs to a different
    tenant — anti-enumeration pattern.
    """
    row = _assert_visible(repo.get(destination_id), user)
    return _row_to_read(row)


# ── PUT /{id} ─────────────────────────────────────────────────────────

@router.put("/{destination_id}", response_model=DestinationRead)
def update_destination(
    destination_id: str,
    payload: DestinationUpdate,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    repo: repository.DestinationRepository = Depends(_get_repo),
) -> DestinationRead:
    """Partial update. Only provided fields are mutated.

    ``hec_token`` in the request triggers re-encryption of ``secret_ref``.
    ``config_version`` is recomputed automatically when config, delivery,
    or secret_ref changes.
    """
    # Visibility check first (anti-enumeration) + write-guard: linha GLOBAL
    # (compartilhada) só é editável por admin de plataforma.
    _assert_mutable(repo.get(destination_id), user)

    # Reassign de org: admin escopado não pode mover o destino para outra org
    # (mesma regra do create — cross_org_create_denied).
    _is_global_upd, _caller_org_upd = _resolve_scope(user)
    if (
        payload.organization_id is not None
        and not _is_global_upd
        and payload.organization_id != _caller_org_upd
    ):
        raise ApiError(
            "destination.cross_org_update_denied",
            status.HTTP_403_FORBIDDEN,
            messages={
                "pt": "Não é possível mover o destino para outra organização",
                "en": "Cannot move the destination to another organization",
                "es": "No es posible mover el destino a otra organización",
            },
        )

    # Re-encrypt if a new token is provided.
    new_secret_ref: object = repository._UNSET
    if payload.hec_token is not None:
        new_secret_ref = get_default_backend().encrypt(payload.hec_token)

    # Validate config against kind's schema when config is being updated.
    if payload.config is not None:
        # We need the current kind to validate the new config.
        current_row = repo.get(destination_id)
        if current_row is not None:
            try:
                reg = _registry.get(str(current_row.kind))  # type: ignore[arg-type]
                reg.config_schema(**payload.config)
            except Exception as exc:
                raise ApiError(
                    "destination.config_invalid",
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    messages={
                        "pt": "config inválida para kind={kind!r}: {error}",
                        "en": "config is invalid for kind={kind!r}: {error}",
                        "es": "config no válida para kind={kind!r}: {error}",
                    },
                    params={"kind": current_row.kind, "error": str(exc)},
                )

    try:
        updated = repo.update(
            destination_id,
            name=payload.name if payload.name is not None else repository._UNSET,
            config=payload.config if payload.config is not None else repository._UNSET,
            delivery=payload.delivery if payload.delivery is not None else repository._UNSET,
            enabled=payload.enabled if payload.enabled is not None else repository._UNSET,
            secret_ref=new_secret_ref,
            organization_id=(
                payload.organization_id
                if payload.organization_id is not None
                else repository._UNSET
            ),
            data_residency=(
                payload.data_residency
                if payload.data_residency is not None
                else repository._UNSET
            ),
            actor=user.username,  # type: ignore[arg-type]
        )
    except IntegrityError:
        raise ApiError(
            "destination.name_conflict",
            status.HTTP_409_CONFLICT,
            messages={
                "pt": "Já existe um destino chamado {name!r}",
                "en": "A destination named {name!r} already exists",
                "es": "Ya existe un destino llamado {name!r}",
            },
            params={"name": payload.name},
        )

    if updated is None:
        raise ApiError(
            "destination.not_found",
            status.HTTP_404_NOT_FOUND,
            messages={
                "pt": "Destino não encontrado.",
                "en": "Destination not found.",
                "es": "Destino no encontrado.",
            },
        )

    # vendor-neutro: marca/desmarca este destino como fallback (catch-all),
    # com enforce de 1 default por org.
    if payload.is_default is not None:
        _apply_is_default(
            repo.db,
            updated,
            int(updated.organization_id) if updated.organization_id is not None else None,
            payload.is_default,
        )

    # a entrega ao wazuh-default agora lê a própria config da
    # linha ``destinations`` (kind syslog_rfc3164, via dispatch_batch_to_destination),
    # então a edição em /destinations já tem efeito direto — sem projeção p/
    # CollectorConfig.wazuh_* (a lane dedicada + a projeção foram removidas).

    audit_data = _scrub_for_audit(payload.model_dump(exclude_unset=True))
    logger.info(
        "destinations: updated id=%s by user=%s",
        destination_id,
        user.username,
        extra={"audit": audit_data},
    )
    return _row_to_read(updated)


# ── DELETE /{id} ──────────────────────────────────────────────────────


@router.delete("/{destination_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_destination(
    destination_id: str,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    repo: repository.DestinationRepository = Depends(_get_repo),
) -> None:
    """Hard-delete a destination.

    Returns 404 (not 403) for cross-tenant IDs (anti-enumeration).
    """
    row = repo.get(destination_id)
    _assert_mutable(row, user)
    repo.delete(destination_id, actor=user.username)  # type: ignore[arg-type]
    logger.info(
        "destinations: deleted id=%s name=%r by user=%s",
        destination_id,
        row.name if row else "?",
        user.username,
    )


# ── POST /{id}/test ───────────────────────────────────────────────────


@router.post("/{destination_id}/test", response_model=DestinationTestResponse)
async def test_destination(
    destination_id: str,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    repo: repository.DestinationRepository = Depends(_get_repo),
) -> DestinationTestResponse:
    """Run a live connectivity probe against a destination.

    IMPORTANT: builds an ephemeral Destination via ``registry.build()``
    and closes it in a ``finally`` block. Never uses
    ``destination_cache.get_destination()`` — that would pollute the
    production singleton cache.

    The ``secret_ref`` is decrypted ONLY here, held in memory for the
    duration of the probe, then discarded. It is never logged.
    """
    row = _assert_visible(repo.get(destination_id), user)

    config_dict: Dict[str, Any] = json.loads(str(row.config or "{}"))
    delivery_dict: Dict[str, Any] = json.loads(str(row.delivery or "{}"))
    row_secret_ref: Optional[str] = str(row.secret_ref) if row.secret_ref else None  # type: ignore[arg-type]
    row_id = str(row.id)  # type: ignore[arg-type]
    row_kind = str(row.kind)  # type: ignore[arg-type]
    row_name = str(row.name)  # type: ignore[arg-type]
    row_version = str(row.config_version or "")
    row_org_id: Optional[int] = int(row.organization_id) if row.organization_id is not None else None  # type: ignore[arg-type]

    # Decrypt secret only for the probe — plaintext not stored or logged.
    decrypted_token: Optional[str] = None
    if row_secret_ref:
        try:
            decrypted_token = get_default_backend().decrypt(row_secret_ref)
        except Exception as exc:
            logger.warning(
                "destinations/test: failed to decrypt secret_ref for id=%s: %s",
                destination_id,
                type(exc).__name__,
            )
            return DestinationTestResponse(
                ok=False,
                detail="Failed to decrypt credential — check secrets backend configuration",
            )
        # audit every successful decrypt (credential was accessed).
        repo.log_credential_access(
            destination_id,
            actor=str(user.username),  # type: ignore[arg-type]
            action="test",
            organization_id=row_org_id,
        )

    dest_cfg = DestinationConfig(
        destination_id=row_id,
        kind=row_kind,
        config=config_dict,
        delivery=delivery_dict,
        secret_ref=row_secret_ref,
        config_version=row_version,
        name=row_name,
        organization_id=row_org_id,
    )

    # Inject the decrypted backend so the factory can resolve the token.
    # We pass a thin adapter that only exposes decrypt() with the
    # already-resolved plaintext — avoids passing the real backend with
    # its full key material.
    class _OneTimeSecrets:
        """Single-use secrets shim: decrypt returns the pre-resolved token."""

        def decrypt(self, _ref: str) -> str:  # noqa: D102
            return decrypted_token or ""

        def encrypt(self, plaintext: str) -> str:  # noqa: D102
            raise NotImplementedError("encrypt not available in test context")

    secrets_shim = _OneTimeSecrets() if decrypted_token is not None else None

    dest = _registry.build(dest_cfg, secrets_shim)
    try:
        result = await dest.test()
    finally:
        await dest.close()

    logger.info(
        "destinations/test: id=%s kind=%s ok=%s by user=%s",
        destination_id,
        row.kind,
        result.ok,
        user.username,
    )
    return DestinationTestResponse(
        ok=result.ok,
        detail=result.detail,
        latency_ms=result.latency_ms,
    )


# ── POST /{id}/shadow (format preview, NO delivery) ──────────────


def _synthetic_envelope() -> Dict[str, Any]:
    """Minimal canonical envelope used when the caller provides no sample."""
    return {
        "_centralops": {
            "event_id": "shadow-preview",
            "organization_id": None,
            "vendor": "centralops",
            "schema_version": 1,
        },
        "normalized": {
            "class_uid": 1001,
            "message": "shadow preview event",
            "severity_id": 1,
        },
        "raw": {"preview": True},
    }


def _preview_wire(wire: Any, *, limit: int = 4096) -> str:
    """Render the formatted wire as a (truncated) string preview."""
    if isinstance(wire, (bytes, bytearray)):
        text = wire.decode("utf-8", errors="replace")
    elif isinstance(wire, str):
        text = wire
    else:
        try:
            text = json.dumps(wire, ensure_ascii=False, sort_keys=True)
        except Exception:
            text = repr(wire)
    return text if len(text) <= limit else text[:limit] + "…[truncated]"


@router.post("/{destination_id}/shadow", response_model=DestinationShadowResponse)
async def shadow_destination(
    destination_id: str,
    payload: DestinationShadowRequest = DestinationShadowRequest(),
    user: models.AppUser = Depends(app_auth.require_admin_user),
    repo: repository.DestinationRepository = Depends(_get_repo),
) -> DestinationShadowResponse:
    """Shadow/preview: format a sample event the way this destination would
    emit it on the wire, WITHOUT delivering and WITHOUT decrypting the credential.

    Builds an ephemeral Destination via ``registry.build()`` with NO secrets
    backend (``format()`` is pure and never touches the token), closes it in a
    ``finally``. No socket is opened; the sink is never contacted. Org-scoped +
    admin-only. Useful to validate the wire format before a real cutover.
    """
    import time as _time

    row = _assert_visible(repo.get(destination_id), user)

    config_dict: Dict[str, Any] = json.loads(str(row.config or "{}"))
    delivery_dict: Dict[str, Any] = json.loads(str(row.delivery or "{}"))

    # Build WITHOUT secrets — format is pure; never decrypt for a preview.
    dest_cfg = DestinationConfig(
        destination_id=str(row.id),
        kind=str(row.kind),
        config=config_dict,
        delivery=delivery_dict,
        secret_ref=None,
        config_version=str(row.config_version or ""),
        name=str(row.name),
        organization_id=int(row.organization_id) if row.organization_id is not None else None,
    )

    sample = payload.sample if payload.sample is not None else _synthetic_envelope()

    dest = _registry.build(dest_cfg, None)
    try:
        started = _time.monotonic()
        try:
            wire = dest.format(sample)
        except NotImplementedError:
            return DestinationShadowResponse(
                ok=False,
                detail=f"kind={row.kind!r} does not expose a decoupled formatter",
            )
        except Exception as exc:
            return DestinationShadowResponse(
                ok=False, detail=f"format failed: {type(exc).__name__}: {exc}"
            )
        latency_ms = (_time.monotonic() - started) * 1000.0
    finally:
        await dest.close()

    logger.info(
        "destinations/shadow: id=%s kind=%s ok=True by user=%s (no delivery)",
        destination_id,
        row.kind,
        user.username,
    )
    return DestinationShadowResponse(
        ok=True,
        count=1,
        formatted_preview=_preview_wire(wire),
        latency_ms=latency_ms,
    )


# ── GET /{id}/health (UI KPI cards) ─────────────────


async def _read_breaker_state(destination_id: str) -> str:
    """Best-effort breaker state from Redis: closed | open | half_open | unknown."""
    try:
        from ..collectors import circuit_breaker
        from ..collectors.celery_app import get_worker_redis

        redis = get_worker_redis()
        try:
            if await redis.exists(circuit_breaker._key_open(destination_id)):
                if await redis.exists(circuit_breaker._key_probe(destination_id)):
                    return "half_open"
                return "open"
            return "closed"
        finally:
            try:
                await redis.aclose()
            except Exception:  # pragma: no cover
                pass
    except Exception:
        return "unknown"


async def _compute_destination_health(
    row: models.Destination,
    *,
    org_id: Optional[int],
    global_scope: bool,
    repo: repository.DestinationRepository,
) -> Dict[str, Any]:
    """Compute the health snapshot for ONE destination (DB DLQ counters + Redis
    breaker state + native-store EPS/bytes), as a plain dict.

    Single source of truth shared by ``GET /{id}/health`` and the batch
    ``GET /health`` endpoint — keeps the derived-``status`` logic and the EPS/
    bytes window identical across both (no duplication). The caller is
    responsible for the org-scope/visibility check (``_assert_visible``); the
    DLQ counters are row-org-scoped via ``org_id``/``global_scope``.
    """
    destination_id = str(row.id)
    enabled = bool(row.enabled)

    stats = repo.dlq_stats(destination_id, org_id=org_id, global_scope=global_scope)
    breaker_state = await _read_breaker_state(destination_id)

    # EPS rolling-window (eventos entregues/s na última 1h), do store nativo.
    from ..collectors import observability_store as _obs

    eps_1h = await asyncio.to_thread(
        _obs.read_window_rate, "dest", destination_id, "sent", minutes=_RWIN
    )
    bytes_1h = await asyncio.to_thread(
        _obs.read_window_total, "dest", destination_id, "bytes", minutes=_RWIN
    )

    if not enabled:
        derived = "disabled"
    elif breaker_state == "open":
        derived = "unhealthy"
    elif stats["dlq_24h"] > 0:
        derived = "degraded"
    elif breaker_state == "unknown":
        # Breaker store unreachable → can't claim "healthy" (don't show a green
        # badge for an unverifiable state). breaker_state is still returned raw.
        derived = "unknown"
    else:
        derived = "healthy"

    return {
        "destination_id": destination_id,
        "name": str(row.name),
        "kind": str(row.kind),
        "status": derived,
        "enabled": enabled,
        "breaker_state": breaker_state,
        "dlq_total": stats["dlq_total"],
        "dlq_24h": stats["dlq_24h"],
        "last_dlq_at": stats["last_dlq_at"],
        # computados do store nativo (não mais null). bytes_per_min = bytes
        # entregues na última hora / 60 (0 até haver dado de bytes registrado).
        "eps": round(eps_1h, 4),
        "bytes_per_min": round(bytes_1h / float(_RWIN), 2) if bytes_1h else None,
    }


@router.get("/{destination_id}/health", response_model=DestinationHealthResponse)
async def destination_health(
    destination_id: str,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    repo: repository.DestinationRepository = Depends(_get_repo),
) -> DestinationHealthResponse:
    """Health snapshot for a destination (DB DLQ counters + Redis breaker state).

    Org-scoped + admin-only. Returns 404 for unknown/cross-tenant ids
    (anti-enumeration). ``eps`` é COMPUTADO da janela de
    1h do store nativo (padrão AxoSyslog ``eps_last_*``), não mais null.
    """
    row = _assert_visible(repo.get(destination_id), user)
    is_global, caller_org = _resolve_scope(user)
    health = await _compute_destination_health(
        row, org_id=caller_org, global_scope=is_global, repo=repo
    )
    return DestinationHealthResponse(
        destination_id=health["destination_id"],
        status=health["status"],
        enabled=health["enabled"],
        breaker_state=health["breaker_state"],
        dlq_total=health["dlq_total"],
        dlq_24h=health["dlq_24h"],
        last_dlq_at=health["last_dlq_at"],
        eps=health["eps"],
        bytes_per_min=health["bytes_per_min"],
    )


# ── GET /{id}/dlq (DLQ drill-in to rejected payload) ─


@router.get("/{destination_id}/dlq", response_model=DestinationDlqResponse)
def destination_dlq(
    destination_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    user: models.AppUser = Depends(app_auth.require_admin_user),
    repo: repository.DestinationRepository = Depends(_get_repo),
) -> DestinationDlqResponse:
    """Dead-letter entries for a destination with the rejected payload (drill-in)
    + a breakdown by error_kind. Org-scoped + admin-only."""
    _assert_visible(repo.get(destination_id), user)

    is_global, caller_org = _resolve_scope(user)
    scope = {"org_id": caller_org, "global_scope": is_global}
    stats = repo.dlq_stats(destination_id, **scope)
    by_kind = repo.dlq_error_kind_counts(destination_id, **scope)
    rows = repo.list_dlq(destination_id, offset=offset, limit=limit, **scope)

    # Redação no READ: o payload armazenado fica íntegro (forense/reprocess),
    # mas a resposta da API mascara segredos por nome — mesmo contrato do tap,
    # evitando expor credenciais brutas no drill-in.
    from ..collectors.audit_buffer import _redact

    entries: List[DestinationDlqEntry] = []
    for r in rows:
        payload: Optional[Dict[str, Any]] = None
        if r.payload:
            try:
                payload = _redact(json.loads(str(r.payload)))
            except (TypeError, ValueError):
                payload = None
        entries.append(
            DestinationDlqEntry(
                id=str(r.id),
                event_id=str(r.event_id),
                error_kind=str(r.error_kind),
                error_detail=str(r.error_detail) if r.error_detail is not None else None,
                payload=payload,
                organization_id=int(r.organization_id) if r.organization_id is not None else None,
                created_at=r.created_at,  # type: ignore[arg-type]
            )
        )

    return DestinationDlqResponse(
        destination_id=destination_id,
        total=stats["dlq_total"],
        by_error_kind=by_kind,
        entries=entries,
    )


# ── POST /{id}/dlq/reprocess (drain DLQ back to dest) ─


@router.post("/{destination_id}/dlq/reprocess", response_model=DlqReprocessResponse)
def destination_dlq_reprocess(
    destination_id: str,
    payload: DlqReprocessRequest,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    repo: repository.DestinationRepository = Depends(_get_repo),
) -> DlqReprocessResponse:
    """Re-queue dead-lettered events for re-delivery to the destination.

    ``event_ids`` is optional: when absent, ALL DLQ entries for the destination
    are drained.  The actual re-delivery runs in a Celery worker
    (``drain_destination_dlq``) — this endpoint returns immediately with the
    queued task id and the count of rows that will be attempted.

    Org-scoped + admin-only (same RBAC as GET /{id}/dlq).
    """
    # Write-guard: reprocessar a DLQ de um destino GLOBAL re-despacha eventos de
    # TODAS as orgs — só admin de plataforma.
    _assert_mutable(repo.get(destination_id), user)

    is_global, caller_org = _resolve_scope(user)
    scope = {"org_id": caller_org, "global_scope": is_global}

    # Resolve the rows that will be targeted so we can return an accurate
    # count and pass only validated event_ids to the task.
    event_ids: Optional[List[str]] = payload.event_ids or None
    rows = repo.list_dlq_for_reprocess(destination_id, event_ids=event_ids, **scope)
    queued = len(rows)

    if queued == 0:
        # Nothing to drain — return immediately without spawning a task.
        return DlqReprocessResponse(
            destination_id=destination_id,
            task_id="",
            queued=0,
        )

    # Only pass the event_ids that actually exist in the scoped DLQ
    # (prevents the task from re-querying a broader set on resume).
    scoped_event_ids = [str(r.event_id) for r in rows]

    from ..collectors.tasks import drain_destination_dlq

    result = drain_destination_dlq.apply_async(
        kwargs={
            "destination_id": destination_id,
            "event_ids": scoped_event_ids,
            "org_id": caller_org,
            "global_scope": is_global,
        },
        queue="dispatch.dlq",
    )
    task_id: str = str(result.id) if result is not None else ""

    logger.info(
        "destinations: DLQ reprocess enqueued destination_id=%s queued=%d "
        "task_id=%s by=%s",
        destination_id,
        queued,
        task_id,
        user.username,
    )
    return DlqReprocessResponse(
        destination_id=destination_id,
        task_id=task_id,
        queued=queued,
    )


# ── GET /{id}/metrics (EPS/bytes/latency + DB summary) ─


@router.get("/{destination_id}/metrics", response_model=DestinationMetricsResponse)
async def destination_metrics(
    destination_id: str,
    range_minutes: int = Query(default=60, ge=5, le=1440),
    step_seconds: int = Query(default=60, ge=15, le=3600),
    user: models.AppUser = Depends(app_auth.require_admin_user),
    repo: repository.DestinationRepository = Depends(_get_repo),
) -> DestinationMetricsResponse:
    """Observability for a destination — served from the NATIVE store (Redis
    rollups), so the UI's own dashboards work WITHOUT any external Prometheus.
    Returns per-minute time-series (events/rejected/latency) + the DB DLQ summary
    + the live breaker state. Org-scoped + admin-only."""
    _assert_visible(repo.get(destination_id), user)

    from ..collectors import observability_store as obs

    is_global, caller_org = _resolve_scope(user)
    scope = {"org_id": caller_org, "global_scope": is_global}
    stats = repo.dlq_stats(destination_id, **scope)
    by_kind = repo.dlq_error_kind_counts(destination_id, **scope)
    breaker_state = await _read_breaker_state(destination_id)

    series = await asyncio.to_thread(
        obs.read_series,
        "dest",
        destination_id,
        ["sent", "rejected", "latency_avg"],
        minutes=range_minutes,
    )
    gauges = await asyncio.to_thread(
        obs.read_gauges, "dest", destination_id, ["queue_depth", "backpressure_state"]
    )

    return DestinationMetricsResponse(
        destination_id=destination_id,
        available=True,  # native store always present (series may be empty)
        reason=None,
        series=series,
        gauges=gauges,
        dlq_total=stats["dlq_total"],
        dlq_24h=stats["dlq_24h"],
        by_error_kind=by_kind,
        breaker_state=breaker_state,
    )


# ── GET /{id}/tap (live data-tap of what's flowing) ──


@router.get("/{destination_id}/tap", response_model=DestinationTapResponse)
async def destination_tap(
    destination_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    user: models.AppUser = Depends(app_auth.require_admin_user),
    repo: repository.DestinationRepository = Depends(_get_repo),
) -> DestinationTapResponse:
    """Most-recent redacted envelopes that flowed to this destination (live
    data-tap, Axoflow-style). Org-scoped + admin-only; a non-global caller only
    sees their own org's (+ NULL) events even on a shared/global destination."""
    _assert_visible(repo.get(destination_id), user)

    from ..collectors import observability_store as obs

    entries = await asyncio.to_thread(obs.read_tap, destination_id, limit=limit)

    is_global, caller_org = _resolve_scope(user)
    if not is_global and caller_org is not None:
        entries = [
            e
            for e in entries
            if (e.get("_centralops") or {}).get("organization_id") in (caller_org, None)
        ]

    return DestinationTapResponse(destination_id=destination_id, entries=entries)


# ── POST /{id}/credential/rotate ────────────────────────────────


@router.post("/{destination_id}/credential/rotate", response_model=CredentialRotateResponse)
def rotate_credential(
    destination_id: str,
    payload: CredentialRotateRequest,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    repo: repository.DestinationRepository = Depends(_get_repo),
) -> CredentialRotateResponse:
    """Rotate the credential for a destination (credential lifecycle).

    Re-encrypts the new plaintext secret, bumps ``secret_version``, and
    records ``secret_rotated_at``.  Clears any prior revocation state.

    Org-scoped + admin-only.
    """
    # Write-guard: girar credencial de destino GLOBAL muda o segredo compartilhado.
    row = _assert_mutable(repo.get(destination_id), user)
    is_global, caller_org = _resolve_scope(user)
    org_id: Optional[int] = (
        int(row.organization_id) if row.organization_id is not None else None  # type: ignore[arg-type]
    )

    new_secret_ref = get_default_backend().encrypt(payload.new_secret)

    updated = repo.rotate_credential(
        destination_id,
        new_secret_ref=new_secret_ref,
        expires_at=payload.expires_at,
    )
    if updated is None:
        raise ApiError(
            "destination.not_found",
            status.HTTP_404_NOT_FOUND,
            messages={
                "pt": "Destino não encontrado.",
                "en": "Destination not found.",
                "es": "Destino no encontrado.",
            },
        )

    # audit the rotation
    repo.log_credential_access(
        destination_id,
        actor=str(user.username),  # type: ignore[arg-type]
        action="rotate",
        organization_id=org_id if not is_global else (caller_org or org_id),
        detail=json.dumps(
            {"secret_version": int(updated.secret_version or 1)},  # type: ignore[arg-type]
            separators=(",", ":"),
        ),
    )

    logger.info(
        "destinations/credential/rotate: id=%s version=%s by user=%s",
        destination_id,
        updated.secret_version,
        user.username,
    )
    return CredentialRotateResponse(
        destination_id=destination_id,
        secret_version=int(updated.secret_version or 1),  # type: ignore[arg-type]
        secret_rotated_at=updated.secret_rotated_at,  # type: ignore[arg-type]
        secret_expires_at=updated.secret_expires_at,  # type: ignore[arg-type]
        has_secret=True,
    )


# ── POST /{id}/credential/revoke ────────────────────────────────


@router.post("/{destination_id}/credential/revoke", response_model=CredentialRevokeResponse)
def revoke_credential(
    destination_id: str,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    repo: repository.DestinationRepository = Depends(_get_repo),
) -> CredentialRevokeResponse:
    """Revoke the credential for a destination (credential lifecycle).

    Clears ``secret_ref``, sets ``enabled=False``, and records
    ``secret_revoked_at``.  The destination will not receive events until
    re-keyed via ``/credential/rotate``.

    Org-scoped + admin-only.
    """
    # Write-guard: revogar credencial de destino GLOBAL desliga a entrega de TODOS.
    row = _assert_mutable(repo.get(destination_id), user)
    is_global, caller_org = _resolve_scope(user)
    org_id: Optional[int] = (
        int(row.organization_id) if row.organization_id is not None else None  # type: ignore[arg-type]
    )

    updated = repo.revoke_credential(destination_id)
    if updated is None:
        raise ApiError(
            "destination.not_found",
            status.HTTP_404_NOT_FOUND,
            messages={
                "pt": "Destino não encontrado.",
                "en": "Destination not found.",
                "es": "Destino no encontrado.",
            },
        )

    # audit the revocation
    repo.log_credential_access(
        destination_id,
        actor=str(user.username),  # type: ignore[arg-type]
        action="revoke",
        organization_id=org_id if not is_global else (caller_org or org_id),
    )

    logger.info(
        "destinations/credential/revoke: id=%s by user=%s",
        destination_id,
        user.username,
    )
    return CredentialRevokeResponse(
        destination_id=destination_id,
        enabled=False,
        secret_revoked_at=updated.secret_revoked_at,  # type: ignore[arg-type]
        has_secret=False,
    )


# ── GET /{id}/credential/audit ──────────────────────────────────


@router.get("/{destination_id}/credential/audit", response_model=CredentialAuditResponse)
def credential_audit(
    destination_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    user: models.AppUser = Depends(app_auth.require_admin_user),
    repo: repository.DestinationRepository = Depends(_get_repo),
) -> CredentialAuditResponse:
    """Return the credential access audit trail for a destination.

    Includes decrypt (from /test), rotate, and revoke events — newest first.
    Org-scoped + admin-only.
    """
    _assert_visible(repo.get(destination_id), user)

    total, rows = repo.list_credential_access_log(
        destination_id, offset=offset, limit=limit
    )
    entries = [
        CredentialAccessEntry(
            id=str(r.id),
            destination_id=str(r.destination_id),
            actor=str(r.actor) if r.actor is not None else None,
            action=str(r.action),
            organization_id=int(r.organization_id) if r.organization_id is not None else None,  # type: ignore[arg-type]
            detail=str(r.detail) if r.detail is not None else None,
            created_at=r.created_at,  # type: ignore[arg-type]
        )
        for r in rows  # type: ignore[attr-defined]
    ]
    return CredentialAuditResponse(
        destination_id=destination_id,
        total=total,
        entries=entries,
    )


# ── GET /{id}/audit (destination CRUD audit trail) ─


@router.get("/{destination_id}/audit", response_model=DestinationAuditResponse)
def destination_audit(
    destination_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    user: models.AppUser = Depends(app_auth.require_admin_user),
    repo: repository.DestinationRepository = Depends(_get_repo),
) -> DestinationAuditResponse:
    """Return the append-only CRUD audit trail for a destination.

    One entry per create/update/delete — newest first. Each ``snapshot`` is
    scrubbed: it carries ``has_secret: bool``, never the secret in clear.
    Admin-only + org-scoped via ``_assert_visible`` (cross-tenant → 404).
    """
    _assert_visible(repo.get(destination_id), user)

    rows = repo.audit_trail(destination_id, limit=limit)
    entries = [
        DestinationAuditEntry(
            id=str(r.id),
            destination_id=str(r.destination_id),
            action=str(r.action),
            actor=str(r.actor) if r.actor is not None else None,
            snapshot=json.loads(str(r.snapshot or "{}")),
            created_at=r.created_at,  # type: ignore[arg-type]
        )
        for r in rows
    ]
    return DestinationAuditResponse(
        destination_id=destination_id,
        total=len(entries),
        entries=entries,
    )


# ── GET /{id}/lineage (event lineage per dest) ─


@router.get("/{destination_id}/lineage", response_model=DestinationLineageResponse)
async def destination_lineage(
    destination_id: str,
    event_id: str = Query(..., min_length=1, description="Event ID to look up"),
    user: models.AppUser = Depends(app_auth.require_admin_user),
    repo: repository.DestinationRepository = Depends(_get_repo),
) -> DestinationLineageResponse:
    """Lineage for a specific event at this destination.

    Returns the positive delivery record(s) for ``event_id`` at
    ``destination_id`` within the lineage retention window.

    Gated on ``LINEAGE_ENABLED``: returns an empty entries list (not 503)
    when lineage is disabled — the endpoint is always routable, the data
    is just absent when the feature flag is off.

    Org-scoped + admin-only.  Non-global users may only query events for
    their own org.  Returns 404 for unknown/cross-tenant destination ids
    (anti-enumeration).

    Retention note: lineage is a Redis store with TTL (default 7 days).
    It is NOT a compliance archive — use the JSONL/Elasticsearch sink for
    long-term evidence.
    """
    row = _assert_visible(repo.get(destination_id), user)

    if not settings.LINEAGE_ENABLED:
        return DestinationLineageResponse(
            destination_id=destination_id,
            event_id=event_id,
            entries=[],
        )

    is_global, caller_org = _resolve_scope(user)
    row_org_id: Optional[int] = (
        int(row.organization_id) if row.organization_id is not None else None  # type: ignore[arg-type]
    )

    # Org-scope resolution: non-global users query their own org.
    # Global users use the destination's org (or explicitly provided org_id
    # via query param — for now we default to the destination's org).
    effective_org: Optional[int] = caller_org if not is_global else row_org_id
    if effective_org is None:
        # Global destination with no org assignment AND global admin: no lineage
        # without an org_id (the key requires one — anti-cross-tenant invariant).
        return DestinationLineageResponse(
            destination_id=destination_id,
            event_id=event_id,
            entries=[],
        )

    from ..collectors.output.lineage import query_lineage

    raw = await asyncio.to_thread(query_lineage, effective_org, event_id)

    # Filter to this specific destination (the lineage key is per-org+event_id
    # and may contain entries from multiple destinations in future).
    entries = [
        LineageEntry(**e)
        for e in raw
        if e.get("destination_id") == destination_id
    ]

    return DestinationLineageResponse(
        destination_id=destination_id,
        event_id=event_id,
        entries=entries,
    )


# ── GET /collectors/lineage/{event_id} (admin, org-scoped) ────────────


@lineage_router.get("/{event_id}", response_model=EventLineageResponse)
async def event_lineage(
    event_id: str,
    org_id: Optional[int] = Query(
        default=None,
        description=(
            "Org to query lineage for.  Required for global admins; "
            "non-global users always query their own org."
        ),
    ),
    user: models.AppUser = Depends(app_auth.require_admin_user),
) -> EventLineageResponse:
    """Org-scoped lineage for a specific event — all destinations that received it.

    Returns the union of positive delivery records for ``event_id`` across
    all destinations within the caller's org scope.

    Non-global users always query their own org (``org_id`` param is
    ignored).  Global admins must supply ``?org_id=...`` explicitly;
    without it a 400 is returned (no cross-tenant query without explicit
    scope).

    Gated on ``LINEAGE_ENABLED``: returns an empty entries list when
    the feature is off.

    Retention note: lineage is a Redis store with TTL (default 7 days).
    It is NOT a compliance archive.
    """
    is_global, _ = _resolve_scope(user)
    # Raw org from the user row (not nulled out by _resolve_scope for global users).
    raw_org: Optional[int] = (
        int(user.organization_id) if user.organization_id is not None else None  # type: ignore[arg-type]
    )

    # Effective org resolution:
    # - Non-global users: always their own org (org_id param ignored).
    # - Admins with own org set: default to their org; ?org_id overrides if
    #   they are truly global (no own org) or need to query a different org.
    # - Pure global admin (no org + global): must provide ?org_id=.
    if not is_global:
        if raw_org is None:
            raise ApiError(
                "lineage.user_without_organization",
                status.HTTP_403_FORBIDDEN,
                messages={
                    "pt": "Usuário sem organização — não é possível consultar a linhagem",
                    "en": "User has no organization — cannot query lineage",
                    "es": "El usuario no tiene organización — no es posible consultar el linaje",
                },
            )
        effective_org: int = raw_org
    else:
        # Global admin path.
        if org_id is not None:
            effective_org = org_id
        elif raw_org is not None:
            # Admin scoped to an org (is_global because role=admin): use their org.
            effective_org = raw_org
        else:
            raise ApiError(
                "lineage.org_id_required",
                status.HTTP_400_BAD_REQUEST,
                messages={
                    "pt": "Administrador global deve informar ?org_id= para delimitar a consulta de linhagem",
                    "en": "Global admin must supply ?org_id= to scope the lineage query",
                    "es": "El administrador global debe indicar ?org_id= para delimitar la consulta de linaje",
                },
            )

    if not settings.LINEAGE_ENABLED:
        return EventLineageResponse(
            event_id=event_id,
            organization_id=effective_org,
            entries=[],
        )

    from ..collectors.output.lineage import query_lineage

    raw = await asyncio.to_thread(query_lineage, effective_org, event_id)
    entries = [LineageEntry(**e) for e in raw]

    return EventLineageResponse(
        event_id=event_id,
        organization_id=effective_org,
        entries=entries,
    )
