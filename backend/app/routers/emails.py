from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..api import schemas
from ..core import auth as app_auth
from ..core import tenant
from ..core.errors import ApiError
from ..db import models, repository, database
from ..services.emailer import send_email

router = APIRouter(prefix="/emails", tags=["emails"])


def get_repo(db: Session = Depends(database.get_session)) -> repository.EmailRepository:
    return repository.EmailRepository(db)


def get_cfg_repo(db: Session = Depends(database.get_session)) -> repository.EmailConfigRepository:
    return repository.EmailConfigRepository(db)


def _serialize_email_config(cfg: models.EmailConfig) -> schemas.EmailConfigRead:
    return schemas.EmailConfigRead(
        id=cfg.id,
        smtp_host=cfg.smtp_host,
        smtp_port=cfg.smtp_port,
        smtp_user=cfg.smtp_user,
        use_tls=cfg.use_tls,
        sender=cfg.sender,
        smtp_password_configured=bool(cfg.smtp_password),
    )


@router.post("/", response_model=schemas.NotificationEmailRead)
def create_email(
    data: schemas.NotificationEmailCreate,
    repo: repository.EmailRepository = Depends(get_repo),
    current_user: models.AppUser = Depends(app_auth.require_admin_user),
):
    # carimba a org do destinatário. Explícito no body
    # (admin global direcionando) ou herdado da org do admin. Resultado de
    # scheduled query é entregue só a e-mails da MESMA org (fecha o leak).
    org_id = data.organization_id if data.organization_id is not None else current_user.organization_id
    # admin escopado não carimba destinatário em outra org.
    if org_id is not None:
        tenant.require_subtree_access(current_user, org_id, repo.db)
    elif not tenant.has_global_scope(current_user):
        # Destinatário GLOBAL (org NULL) recebe notificações de toda a plataforma —
        # só admin global pode criar.
        tenant.require_global_scope(current_user)
    email = models.NotificationEmail(email=data.email, organization_id=org_id)
    return repo.add(email)


@router.get("/", response_model=list[schemas.NotificationEmailRead])
def list_emails(
    repo: repository.EmailRepository = Depends(get_repo),
    current_user: models.AppUser = Depends(app_auth.require_admin_user),
):
    # admin escopado vê só destinatários da própria subárvore
    # (globais org NULL ficam ocultos — são de plataforma). Global vê todos.
    rows = repo.list()
    org_ids = tenant.accessible_org_ids(current_user, repo.db)
    if org_ids is not None:
        rows = [r for r in rows if r.organization_id in org_ids]
    return rows


@router.delete("/{email_id}", status_code=204)
def delete_email(
    email_id: int,
    repo: repository.EmailRepository = Depends(get_repo),
    current_user: models.AppUser = Depends(app_auth.require_admin_user),
):
    email = repo.get(email_id)
    if email is not None:
        # escopado só deleta destinatário da própria subárvore;
        # destinatário GLOBAL (org NULL) só por admin global.
        if email.organization_id is not None:
            tenant.require_subtree_access(current_user, email.organization_id, repo.db)
        else:
            tenant.require_global_scope(current_user)
    if not email:
        raise ApiError(
            "email.not_found",
            404,
            messages={
                "pt": "E-mail não encontrado.",
                "en": "Email not found.",
                "es": "Correo electrónico no encontrado.",
            },
        )
    repo.delete(email)
    return None


@router.get("/config", response_model=schemas.EmailConfigRead)
def get_email_config(
    repo: repository.EmailConfigRepository = Depends(get_cfg_repo),
    _: models.AppUser = Depends(app_auth.require_admin_user),
):
    cfg = repo.get()
    if not cfg:
        cfg = repo.update(smtp_host="localhost", smtp_port=25, sender="noreply@example.com")
    return _serialize_email_config(cfg)


@router.put("/config", response_model=schemas.EmailConfigRead)
def update_email_config(
    data: schemas.EmailConfigUpdate,
    repo: repository.EmailConfigRepository = Depends(get_cfg_repo),
    current_user: models.AppUser = Depends(app_auth.require_admin_user),
):
    # SMTP é configuração de PLATAFORMA (singleton, sem org) — só admin global:
    # alterá-la muda o envio de e-mail de todos os tenants.
    tenant.require_global_scope(current_user)
    cfg = repo.update(**data.model_dump(exclude_unset=True))
    return _serialize_email_config(cfg)


@router.post("/test")
def send_test_email(
    repo: repository.EmailRepository = Depends(get_repo),
    current_user: models.AppUser = Depends(app_auth.require_admin_user),
):
    # o teste envia só aos destinatários no escopo do caller
    # (um admin escopado não dispara e-mail p/ destinatários de outras orgs).
    rows = repo.list()
    org_ids = tenant.accessible_org_ids(current_user, repo.db)
    if org_ids is not None:
        rows = [r for r in rows if r.organization_id in org_ids]
    recipients = [e.email for e in rows]
    if not recipients:
        raise ApiError(
            "email.no_recipients",
            400,
            messages={
                "pt": "Cadastre ao menos um destinatario antes de enviar o teste de email.",
                "en": "Register at least one recipient before sending the test email.",
                "es": "Registre al menos un destinatario antes de enviar el correo de prueba.",
            },
        )

    try:
        send_email(
            recipients,
            "Teste de email",
            "Esta \u00e9 uma mensagem de teste. Sua configura\u00e7\u00e3o de email parece correta!",
            raise_on_error=True,
        )
    except ValueError as exc:
        raise ApiError(
            "email.send_invalid_config",
            400,
            messages={
                "pt": "{error}",
                "en": "{error}",
                "es": "{error}",
            },
            params={"error": str(exc)},
        ) from exc
    except Exception as exc:
        raise ApiError(
            "email.send_failed",
            502,
            messages={
                "pt": "Falha ao enviar email de teste: {error}",
                "en": "Failed to send test email: {error}",
                "es": "Error al enviar el correo de prueba: {error}",
            },
            params={"error": str(exc)},
        ) from exc

    return {"detail": f"Email de teste enviado para {len(recipients)} destinatario(s)."}
