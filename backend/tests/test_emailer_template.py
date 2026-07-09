import types
from email.message import EmailMessage
from unittest import mock

from backend.app.core.crypto import encrypt
from backend.app.services.emailer import send_email

class DummyCfg:
    smtp_host = "localhost"
    smtp_port = 25
    smtp_user = None
    smtp_password = None
    use_tls = False
    sender = "noreply@example.com"


def test_send_email_renders_html():
    dummy_session = mock.Mock()
    with mock.patch("backend.app.services.emailer.database.get_session", return_value=iter([dummy_session])), \
         mock.patch("backend.app.services.emailer.repository.EmailConfigRepository") as Repo, \
         mock.patch("smtplib.SMTP") as smtp_cls:
        Repo.return_value.get.return_value = DummyCfg()
        smtp_instance = smtp_cls.return_value.__enter__.return_value

        send_email(["x@example.com"], "Subj", "Msg")

        smtp_instance.send_message.assert_called()
        msg: EmailMessage = smtp_instance.send_message.call_args[0][0]
        html_part = msg.get_body(preferencelist=("html"))
        assert html_part is not None
        assert "Msg" in html_part.get_content()
        assert "<html" in html_part.get_content().lower()


def test_send_email_decrypts_stored_smtp_password_before_login():
    dummy_session = mock.Mock()
    encrypted_password = encrypt("smtp-secret")

    cfg = types.SimpleNamespace(
        smtp_host="localhost",
        smtp_port=25,
        smtp_user="mailer",
        smtp_password=encrypted_password,
        use_tls=False,
        sender="noreply@example.com",
    )

    with mock.patch("backend.app.services.emailer.database.get_session", return_value=iter([dummy_session])), \
         mock.patch("backend.app.services.emailer.repository.EmailConfigRepository") as Repo, \
         mock.patch("smtplib.SMTP") as smtp_cls:
        Repo.return_value.get.return_value = cfg
        smtp_instance = smtp_cls.return_value.__enter__.return_value

        send_email(["x@example.com"], "Subj", "Msg")

        smtp_instance.login.assert_called_once_with("mailer", "smtp-secret")
