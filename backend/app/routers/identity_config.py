"""Config de identidade/SSO (Microsoft Entra) operada pela UI.

GET/PUT admin-only da tabela singleton ``identity_config``. O ``client_secret``
é cifrado no banco e NUNCA devolvido em claro (só a flag ``*_configured``).
``POST /test`` valida a config obtendo um token ``client_credentials`` — prova
que tenant/client/secret estão corretos.
``POST /sync`` dispara sync de usuarios via Graph.
``GET /sync-status`` retorna estado do ultimo sync.
"""

from __future__ import annotations

import json
import logging

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..api import schemas
from ..core import auth as app_auth
from ..core import identity_config
from ..core import tenant
from ..core.config import settings as _settings
from ..core.crypto import encrypt
from ..core.errors import ApiError
from ..db import database, models, repository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/identity/config", tags=["identity"])


def _require_platform_admin(
    user: models.AppUser = Depends(app_auth.require_admin_user),
) -> models.AppUser:
    """Identidade/SSO é configuração de PLATAFORMA (singleton, sem org) — um
    admin-de-org não pode ler nem alterar: mudaria o SSO/
    sync de TODOS os tenants. Só admin de escopo global."""
    tenant.require_global_scope(user)
    return user


def _to_read(db: Session, row: models.IdentityConfig | None) -> schemas.IdentityConfigRead:
    snap = identity_config.load(db)
    # Parseia o JSON bruto do summary; fallback None se malformado
    summary_dict = None
    if snap.entra_last_sync_summary:
        try:
            summary_dict = json.loads(snap.entra_last_sync_summary)
        except (json.JSONDecodeError, TypeError):
            summary_dict = None
    return schemas.IdentityConfigRead(
        entra_enabled=snap.entra_enabled,
        entra_tenant_id=snap.entra_tenant_id,
        entra_client_id=snap.entra_client_id,
        entra_client_secret_configured=bool(snap.entra_client_secret),
        entra_redirect_uri=snap.entra_redirect_uri,
        entra_authority=snap.entra_authority,
        entra_scopes=snap.entra_scopes,
        entra_role_map=snap.entra_role_map,
        entra_default_role=snap.entra_default_role,
        entra_default_is_global=snap.entra_default_is_global,
        entra_jit_provisioning=snap.entra_jit_provisioning,
        entra_allowed_email_domains=snap.entra_allowed_email_domains,
        entra_button_label=snap.entra_button_label,
        entra_post_login_redirect=snap.entra_post_login_redirect,
        is_persisted=snap.is_persisted,
        updated_at=row.updated_at if row else None,
        entra_sync_enabled=snap.entra_sync_enabled,
        entra_sync_deprovision=snap.entra_sync_deprovision,
        entra_last_sync_at=snap.entra_last_sync_at,
        entra_last_sync_status=snap.entra_last_sync_status,
        entra_last_sync_summary=summary_dict,
    )


@router.get("", response_model=schemas.IdentityConfigRead)
def get_identity_config(
    _: models.AppUser = Depends(_require_platform_admin),
    db: Session = Depends(database.get_session),
):
    return _to_read(db, repository.IdentityConfigRepository(db).get())


@router.put("", response_model=schemas.IdentityConfigRead)
def update_identity_config(
    payload: schemas.IdentityConfigUpdate,
    _: models.AppUser = Depends(_require_platform_admin),
    db: Session = Depends(database.get_session),
):
    data = payload.model_dump(exclude_unset=True)
    # Secret: só grava se enviado não-vazio; cifra antes de persistir.
    secret = data.pop("entra_client_secret", None)
    if secret is not None and str(secret).strip():
        data["entra_client_secret"] = encrypt(str(secret).strip())

    row = repository.IdentityConfigRepository(db).update(**data)
    logger.info("identity_config: atualizado por admin")
    return _to_read(db, row)


def _entra_sync_lock_active() -> bool:
    """Verifica se o lock Redis do sync de usuarios Entra esta ativo.

    Best-effort: retorna False em qualquer falha de conexao.
    """
    try:
        import redis as _redis_sync

        client = _redis_sync.Redis.from_url(
            _settings.REDIS_URL or "redis://localhost:6379/0",
            decode_responses=True,
        )
        try:
            return bool(client.exists("sync:entra:users"))
        finally:
            client.close()
    except Exception:  # noqa: BLE001
        return False


@router.post("/sync", response_model=schemas.EntraSyncTriggerResult, status_code=202)
def trigger_entra_sync(
    _: models.AppUser = Depends(_require_platform_admin),
    db: Session = Depends(database.get_session),
) -> schemas.EntraSyncTriggerResult:
    """Dispara o sync de usuarios do Entra de forma assincrona via Celery.

    Retorna 202 quando a task e enfileirada.
    Retorna 429 se outro sync ja esta em andamento (lock Redis ativo).
    Retorna 503 se o broker Celery estiver indisponivel.
    """
    # Verifica lock antes de despachar
    if _entra_sync_lock_active():
        raise ApiError(
            "identity.entra_sync_in_progress",
            429,
            messages={
                "pt": "Sync de usuários Entra já em andamento.",
                "en": "Entra user sync already in progress.",
                "es": "La sincronización de usuarios de Entra ya está en curso.",
            },
        )

    # Verifica se sync esta habilitado na config
    cfg = identity_config.load(db)
    if not cfg.entra_sync_enabled:
        return schemas.EntraSyncTriggerResult(
            queued=False,
            message="sync desabilitado na configuracao",
            lock_active=False,
        )

    # Despacha a task — import local para evitar circular
    try:
        from ..collectors.entra_sync_tasks import sync_entra_users
        sync_entra_users.delay()
    except Exception as exc:  # noqa: BLE001
        logger.error("trigger_entra_sync: falha ao despachar task: %s", exc)
        raise ApiError(
            "identity.entra_sync_dispatch_failed",
            503,
            messages={
                "pt": "Falha ao enfileirar sync — broker indisponível.",
                "en": "Failed to queue sync — broker unavailable.",
                "es": "Fallo al encolar la sincronización — broker no disponible.",
            },
        ) from exc

    return schemas.EntraSyncTriggerResult(
        queued=True,
        message="Sync de usuarios Entra disparado",
        lock_active=False,
    )


@router.get("/sync-status", response_model=schemas.EntraSyncStatus)
def get_entra_sync_status(
    _: models.AppUser = Depends(_require_platform_admin),
    db: Session = Depends(database.get_session),
) -> schemas.EntraSyncStatus:
    """Retorna o estado do ultimo sync de usuarios Entra.

    Sempre retorna 200 — mesmo sem sync anterior (campos None).
    """
    row = repository.IdentityConfigRepository(db).get()
    lock_active = _entra_sync_lock_active()

    if row is None:
        return schemas.EntraSyncStatus(lock_active=lock_active)

    # Deserializa o summary JSON para EntraSyncSummary; fallback None se malformado
    summary_obj: schemas.EntraSyncSummary | None = None
    if row.entra_last_sync_summary:
        try:
            raw = json.loads(row.entra_last_sync_summary)
            summary_obj = schemas.EntraSyncSummary(
                created=raw.get("created", 0),
                updated=raw.get("updated", 0),
                deactivated=raw.get("deactivated", 0),
                errors=raw.get("errors", []),
                started_at=raw.get("started_at"),
                finished_at=raw.get("finished_at"),
            )
        except (json.JSONDecodeError, TypeError, Exception):  # noqa: BLE001
            summary_obj = None

    return schemas.EntraSyncStatus(
        last_sync_at=row.entra_last_sync_at,
        last_sync_status=row.entra_last_sync_status,
        last_sync_summary=summary_obj,
        lock_active=lock_active,
    )


@router.post("/test", response_model=schemas.IdentityConnectionTestResult)
def test_identity_connection(
    _: models.AppUser = Depends(_require_platform_admin),
    db: Session = Depends(database.get_session),
):
    """Valida a config atual obtendo um token client_credentials no Entra."""
    cfg = identity_config.load(db)
    if not (cfg.entra_tenant_id and cfg.entra_client_id and cfg.entra_client_secret):
        return schemas.IdentityConnectionTestResult(
            ok=False, detail="Preencha tenant, client e secret antes de testar."
        )

    authority = f"{cfg.entra_authority.rstrip('/')}/{cfg.entra_tenant_id}"
    token_url = f"{authority}/oauth2/v2.0/token"
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": cfg.entra_client_id,
                    "client_secret": cfg.entra_client_secret,
                    "scope": "https://graph.microsoft.com/.default",
                },
            )
    except httpx.HTTPError as exc:
        return schemas.IdentityConnectionTestResult(
            ok=False, detail=f"Falha de rede ao contatar o Entra: {exc}"
        )

    if resp.status_code == 200:
        return schemas.IdentityConnectionTestResult(
            ok=True, detail="Credenciais válidas — token obtido com sucesso."
        )
    try:
        body = resp.json()
        err = body.get("error_description") or body.get("error") or resp.text[:200]
    except Exception:  # pragma: no cover
        err = resp.text[:200]
    return schemas.IdentityConnectionTestResult(
        ok=False, detail=f"Entra recusou ({resp.status_code}): {str(err).splitlines()[0][:200]}"
    )
