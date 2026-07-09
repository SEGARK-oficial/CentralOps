import smtplib
from email.message import EmailMessage
from typing import List
import logging
from pathlib import Path

from ..core.crypto import decrypt
from ..db import database, repository

logger = logging.getLogger(__name__)

def send_email(
    recipients: List[str],
    subject: str,
    body: str,
    *,
    raise_on_error: bool = False,
) -> None:
    if not recipients:
        if raise_on_error:
            raise ValueError("Nenhum destinatario configurado para envio de email.")
        return

    db = next(database.get_session())
    cfg_repo = repository.EmailConfigRepository(db)
    cfg = cfg_repo.get()
    db.close()
    if not cfg:
        logger.warning("No email configuration found")
        if raise_on_error:
            raise ValueError("Nenhuma configuração de email foi encontrada.")
        return

    if not cfg.smtp_host:
        if raise_on_error:
            raise ValueError("SMTP Host não configurado.")
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.sender or "noreply@example.com"
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)

    # try to add a simple HTML alternative using a template
    try:
        template_path = Path(__file__).resolve().parent.parent / "templates" / "email.html"
        html_template = template_path.read_text(encoding="utf-8")
        html_body = (
            html_template.replace("{{ subject }}", subject).replace("{{ message }}", body)
        )
        msg.add_alternative(html_body, subtype="html")
    except Exception as e:  # pragma: no cover - template errors shouldn't break email
        logger.error(f"Failed to render email template: {e}")

    try:
        smtp_password = decrypt(cfg.smtp_password) if cfg.smtp_password else None
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port) as smtp:
            if cfg.use_tls:
                smtp.starttls()
            if cfg.smtp_user and smtp_password:
                smtp.login(cfg.smtp_user, smtp_password)
            smtp.send_message(msg)
    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        if raise_on_error:
            raise
