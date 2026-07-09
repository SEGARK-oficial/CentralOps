"""Testes M-4 (RedBeat namespace) e L-2 (SecretsFilter Redis URL).

Estes testes rodam no ambiente de collectors que tem Python 3.9 compat.
Não importam backend.app.main nem nada que dependa de StrEnum (Python 3.11+).
"""
from __future__ import annotations

import logging
import os
from unittest.mock import MagicMock, patch


class TestRedbeatKeyPrefixM4:
    """M-4: RedBeat namespace isolado por APP_ENV."""

    def test_prefix_development(self):
        from backend.app.collectors.celery_app import _redbeat_key_prefix

        mock_settings = MagicMock()
        mock_settings.APP_ENV = "development"
        with patch("backend.app.collectors.celery_app.settings", mock_settings):
            prefix = _redbeat_key_prefix()
        assert prefix == "redbeat::development::"

    def test_prefix_production(self):
        from backend.app.collectors.celery_app import _redbeat_key_prefix

        mock_settings = MagicMock()
        mock_settings.APP_ENV = "production"
        with patch("backend.app.collectors.celery_app.settings", mock_settings):
            prefix = _redbeat_key_prefix()
        assert prefix == "redbeat::production::"

    def test_prefix_staging(self):
        from backend.app.collectors.celery_app import _redbeat_key_prefix

        mock_settings = MagicMock()
        mock_settings.APP_ENV = "staging"
        with patch("backend.app.collectors.celery_app.settings", mock_settings):
            prefix = _redbeat_key_prefix()
        assert prefix == "redbeat::staging::"

    def test_prefix_test_env(self):
        from backend.app.collectors.celery_app import _redbeat_key_prefix

        mock_settings = MagicMock()
        mock_settings.APP_ENV = "test"
        with patch("backend.app.collectors.celery_app.settings", mock_settings):
            prefix = _redbeat_key_prefix()
        assert prefix == "redbeat::test::"

    def test_env_var_override_takes_priority(self, monkeypatch):
        monkeypatch.setenv("REDBEAT_KEY_PREFIX", "redbeat::custom::")
        from backend.app.collectors.celery_app import _redbeat_key_prefix

        mock_settings = MagicMock()
        mock_settings.APP_ENV = "production"
        with patch("backend.app.collectors.celery_app.settings", mock_settings):
            prefix = _redbeat_key_prefix()
        assert prefix == "redbeat::custom::"

    def test_env_var_empty_falls_back_to_app_env(self, monkeypatch):
        monkeypatch.delenv("REDBEAT_KEY_PREFIX", raising=False)
        from backend.app.collectors.celery_app import _redbeat_key_prefix

        mock_settings = MagicMock()
        mock_settings.APP_ENV = "staging"
        with patch("backend.app.collectors.celery_app.settings", mock_settings):
            prefix = _redbeat_key_prefix()
        assert prefix == "redbeat::staging::"

    def test_celery_app_conf_uses_env_prefix(self):
        from backend.app.collectors.celery_app import celery_app

        prefix = celery_app.conf.redbeat_key_prefix
        # Deve conter o padrão redbeat::<env>::
        assert prefix.startswith("redbeat::")
        assert prefix.endswith("::")
        parts = prefix.split("::")
        assert len(parts) == 3, f"Expected 3 parts, got: {parts}"
        assert parts[0] == "redbeat"
        assert parts[1] != "", "APP_ENV part must not be empty"

    def test_prefix_is_isolated_from_legacy_redbeat(self):
        """O novo prefixo NÃO é 'redbeat::' sem env — garante isolamento."""
        from backend.app.collectors.celery_app import celery_app

        prefix = celery_app.conf.redbeat_key_prefix
        assert prefix != "redbeat::", (
            "Prefixo não deve ser 'redbeat::' sem ambiente — isso causaria "
            "colisão com entries legadas"
        )


class TestSecretsFilterRedisUrlL2:
    """L-2: SecretsFilter deve redatar Redis URLs com senha em logs."""

    def _make_record(self, msg: str, args: tuple = ()) -> logging.LogRecord:
        record = logging.LogRecord(
            name="test",
            level=logging.WARNING,
            pathname="",
            lineno=0,
            msg=msg,
            args=args,
            exc_info=None,
        )
        return record

    def test_redis_url_with_password_is_redacted(self):
        from backend.app.collectors.celery_app import _SecretsFilter

        f = _SecretsFilter()
        record = self._make_record(
            "Connecting to redis://:supersecret@host:6379/0 failed"
        )
        assert f.filter(record) is True
        msg = record.getMessage()
        assert "supersecret" not in msg
        assert "redis://[REDACTED]@host:6379/0" in msg

    def test_redis_url_without_password_is_not_touched(self):
        from backend.app.collectors.celery_app import _SecretsFilter

        f = _SecretsFilter()
        record = self._make_record("Connected to redis://host:6379/0")
        assert f.filter(record) is True
        assert "redis://host:6379/0" in record.getMessage()

    def test_redis_url_with_user_and_password_is_redacted(self):
        from backend.app.collectors.celery_app import _SecretsFilter

        f = _SecretsFilter()
        record = self._make_record("URL: redis://myuser:mypass@redis.internal:6380/1")
        assert f.filter(record) is True
        msg = record.getMessage()
        assert "mypass" not in msg
        assert "myuser" not in msg
        assert "[REDACTED]" in msg

    def test_host_preserved_after_redaction(self):
        from backend.app.collectors.celery_app import _SecretsFilter

        f = _SecretsFilter()
        record = self._make_record(
            "redis://:pass123@redis.corp.local:6379/0"
        )
        f.filter(record)
        msg = record.getMessage()
        # Host e porta devem permanecer (úteis para debug)
        assert "redis.corp.local:6379/0" in msg
        assert "pass123" not in msg

    def test_access_token_needle_still_works(self):
        from backend.app.collectors.celery_app import _SecretsFilter

        f = _SecretsFilter()
        record = self._make_record("bearer access_token=abc123")
        assert f.filter(record) is True
        msg = record.getMessage()
        assert "[secret redacted" in msg

    def test_client_secret_needle_still_works(self):
        from backend.app.collectors.celery_app import _SecretsFilter

        f = _SecretsFilter()
        record = self._make_record("client_secret=mysecretvalue")
        assert f.filter(record) is True
        assert "[secret redacted" in record.getMessage()

    def test_format_string_with_redis_url_arg(self):
        """Verifica redação quando msg usa %s e args separados."""
        from backend.app.collectors.celery_app import _SecretsFilter

        f = _SecretsFilter()
        record = self._make_record(
            "Broker URL: %s",
            ("redis://:topsecret@broker:6379/1",),
        )
        assert f.filter(record) is True
        msg = record.getMessage()
        assert "topsecret" not in msg

    def test_message_without_redis_is_untouched(self):
        from backend.app.collectors.celery_app import _SecretsFilter

        f = _SecretsFilter()
        original_msg = "Worker started successfully on port 8080"
        record = self._make_record(original_msg)
        f.filter(record)
        assert record.getMessage() == original_msg

    def test_multiple_redis_urls_in_message(self):
        """Ambos os patterns redatados se mensagem contiver múltiplas URLs."""
        from backend.app.collectors.celery_app import _SecretsFilter

        f = _SecretsFilter()
        record = self._make_record(
            "primary=redis://:secret1@host1:6379/0 replica=redis://:secret2@host2:6379/0"
        )
        f.filter(record)
        msg = record.getMessage()
        assert "secret1" not in msg
        assert "secret2" not in msg
        assert "[REDACTED]" in msg
