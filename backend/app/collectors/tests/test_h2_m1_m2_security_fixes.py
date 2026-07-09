"""Testes H-2 (rate limit), M-1 (verify_ssl), M-2 (error sanitization).

Testes unitários que NÃO importam backend.app.main nem backend.app.api.schemas
(ambos requerem Python 3.11+ por causa de StrEnum e int|None syntax).
Focam em camadas testáveis sem o HTTP stack completo.
"""
from __future__ import annotations

import logging
import os
import re
import time
from unittest.mock import MagicMock, patch

import fakeredis
import pytest


# ---------------------------------------------------------------------------
# H-2 — IntegrationRateLimiter (sem HTTP)
# ---------------------------------------------------------------------------

class TestIntegrationRateLimiterH2:
    """Testes unitários do IntegrationRateLimiter em memória e Redis."""

    def test_in_memory_create_allows_up_to_limit(self):
        from backend.app.core.rate_limiter import IntegrationRateLimiter
        limiter = IntegrationRateLimiter(max_creates_per_minute=5)
        for i in range(5):
            result = limiter.check_create(user_id=1)
            assert result is None, f"Request {i+1} should be allowed"

    def test_in_memory_create_blocks_on_limit_exceeded(self):
        from backend.app.core.rate_limiter import IntegrationRateLimiter
        limiter = IntegrationRateLimiter(max_creates_per_minute=30)
        for _ in range(30):
            assert limiter.check_create(user_id=42) is None
        retry_after = limiter.check_create(user_id=42)
        assert retry_after is not None
        assert retry_after >= 1

    def test_in_memory_delete_blocks_on_limit_exceeded(self):
        from backend.app.core.rate_limiter import IntegrationRateLimiter
        limiter = IntegrationRateLimiter(max_deletes_per_minute=5)
        for _ in range(5):
            assert limiter.check_delete(user_id=7) is None
        retry_after = limiter.check_delete(user_id=7)
        assert retry_after is not None
        assert retry_after >= 1

    def test_in_memory_limit_isolated_per_user(self):
        from backend.app.core.rate_limiter import IntegrationRateLimiter
        limiter = IntegrationRateLimiter(max_creates_per_minute=2)
        for _ in range(2):
            limiter.check_create(user_id=1)
        assert limiter.check_create(user_id=1) is not None
        assert limiter.check_create(user_id=2) is None

    def test_redis_backed_create_allows_up_to_limit(self):
        from backend.app.core.rate_limiter import IntegrationRateLimiter
        fake_redis = fakeredis.FakeRedis(decode_responses=True)
        limiter = IntegrationRateLimiter(max_creates_per_minute=5, redis_client=fake_redis)
        for i in range(5):
            result = limiter.check_create(user_id=99)
            assert result is None, f"Request {i+1} should be allowed"

    def test_redis_backed_create_blocks_on_limit_exceeded(self):
        from backend.app.core.rate_limiter import IntegrationRateLimiter
        fake_redis = fakeredis.FakeRedis(decode_responses=True)
        limiter = IntegrationRateLimiter(max_creates_per_minute=3, redis_client=fake_redis)
        for _ in range(3):
            assert limiter.check_create(user_id=55) is None
        retry_after = limiter.check_create(user_id=55)
        assert retry_after is not None
        assert retry_after >= 1

    def test_redis_backed_retry_after_header_value(self):
        from backend.app.core.rate_limiter import IntegrationRateLimiter
        fake_redis = fakeredis.FakeRedis(decode_responses=True)
        limiter = IntegrationRateLimiter(max_creates_per_minute=1, redis_client=fake_redis)
        limiter.check_create(user_id=1)
        retry_after = limiter.check_create(user_id=1)
        assert retry_after is not None
        assert 1 <= retry_after <= 60

    def test_redis_backed_delete_limit(self):
        from backend.app.core.rate_limiter import IntegrationRateLimiter
        fake_redis = fakeredis.FakeRedis(decode_responses=True)
        limiter = IntegrationRateLimiter(max_deletes_per_minute=5, redis_client=fake_redis)
        for _ in range(5):
            assert limiter.check_delete(user_id=10) is None
        assert limiter.check_delete(user_id=10) is not None

    def test_create_and_delete_are_independent_counters(self):
        from backend.app.core.rate_limiter import IntegrationRateLimiter
        fake_redis = fakeredis.FakeRedis(decode_responses=True)
        limiter = IntegrationRateLimiter(
            max_creates_per_minute=2,
            max_deletes_per_minute=2,
            redis_client=fake_redis,
        )
        for _ in range(2):
            limiter.check_create(user_id=1)
        assert limiter.check_create(user_id=1) is not None
        # Deletes devem estar livres
        assert limiter.check_delete(user_id=1) is None

    def test_module_singleton_exists(self):
        from backend.app.core.rate_limiter import integration_rate_limiter
        assert integration_rate_limiter is not None

    def test_router_imports_rate_limiter(self):
        """Verifica que integrations.py importa integration_rate_limiter."""
        router_path = os.path.join(
            os.path.dirname(__file__), "../../routers/integrations.py"
        )
        with open(router_path) as f:
            content = f.read()
        assert "integration_rate_limiter" in content
        assert "from ..core.rate_limiter import integration_rate_limiter" in content

    def test_router_has_rate_limit_check_on_post(self):
        """POST /integrations aplica check_create antes de processar."""
        router_path = os.path.join(
            os.path.dirname(__file__), "../../routers/integrations.py"
        )
        with open(router_path) as f:
            content = f.read()
        assert "check_create" in content
        assert "429" in content
        assert "Retry-After" in content

    def test_router_has_rate_limit_check_on_delete(self):
        """DELETE /integrations aplica check_delete."""
        router_path = os.path.join(
            os.path.dirname(__file__), "../../routers/integrations.py"
        )
        with open(router_path) as f:
            content = f.read()
        assert "check_delete" in content

    def test_settings_has_max_integrations_per_org(self):
        from backend.app.core.config import settings
        assert hasattr(settings, "MAX_INTEGRATIONS_PER_ORG")
        assert settings.MAX_INTEGRATIONS_PER_ORG == 100

    def test_repository_count_active_method_exists(self):
        from backend.app.db.repository import IntegrationRepository
        assert hasattr(IntegrationRepository, "count_active")
        assert callable(getattr(IntegrationRepository, "count_active"))

    def test_router_has_org_limit_check(self):
        """POST /integrations verifica limite por organização."""
        router_path = os.path.join(
            os.path.dirname(__file__), "../../routers/integrations.py"
        )
        with open(router_path) as f:
            content = f.read()
        assert "count_active" in content
        assert "MAX_INTEGRATIONS_PER_ORG" in content
        assert "maximum" in content.lower()


# ---------------------------------------------------------------------------
# M-1 (REVISADO) — verify_ssl é decisão EXPLÍCITA do usuário, em qualquer ambiente
# Wazuh/soluções self-hosted rodam com certificado auto-assinado na maioria dos
# deploys reais; o bloqueio em produção tornava a conexão impossível. O validator
# agora PERMITE verify_ssl=False em todo APP_ENV e emite WARNING auditável.
# Testa os validators REAIS (IntegrationCreate/IntegrationUpdate), não uma cópia.
# ---------------------------------------------------------------------------

class TestVerifySslUserChoice:
    def _create(self, **kw):
        from backend.app.api.schemas import IntegrationCreate
        return IntegrationCreate(
            organization_id=1, name="wz", platform="wazuh", **kw
        )

    def _update(self, **kw):
        from backend.app.api.schemas import IntegrationUpdate
        return IntegrationUpdate(**kw)

    def test_false_allowed_in_production_create(self, monkeypatch):
        """O usuário PODE optar por não validar o certificado mesmo em produção
        (flag "confiar no certificado" — self-hosted/auto-assinado)."""
        from backend.app.core.config import settings
        monkeypatch.setattr(settings, "APP_ENV", "production")
        assert self._create(verify_ssl=False).verify_ssl is False

    def test_false_allowed_in_production_update(self, monkeypatch):
        from backend.app.core.config import settings
        monkeypatch.setattr(settings, "APP_ENV", "production")
        assert self._update(verify_ssl=False).verify_ssl is False

    def test_false_allowed_in_all_envs(self, monkeypatch):
        from backend.app.core.config import settings
        for env in ("production", "staging", "development", "test"):
            monkeypatch.setattr(settings, "APP_ENV", env)
            assert self._create(verify_ssl=False).verify_ssl is False, env
            assert self._update(verify_ssl=False).verify_ssl is False, env

    def test_true_and_none_pass_through(self):
        assert self._create(verify_ssl=True).verify_ssl is True
        assert self._update(verify_ssl=None).verify_ssl is None

    def test_false_emits_audit_warning(self, monkeypatch, caplog):
        """A escolha insegura é auditável: WARNING no log quando setada."""
        import logging

        from backend.app.core.config import settings
        monkeypatch.setattr(settings, "APP_ENV", "production")
        with caplog.at_level(logging.WARNING, logger="backend.app.api.schemas"):
            self._create(verify_ssl=False)
        assert any("verify_ssl=False" in r.message for r in caplog.records)

    def test_settings_has_app_env(self):
        from backend.app.core.config import settings
        assert hasattr(settings, "APP_ENV")
        assert settings.APP_ENV != ""


# ---------------------------------------------------------------------------
# M-2 — _safe_provider_error helper
# ---------------------------------------------------------------------------

class TestSafeProviderErrorM2:
    def _safe_provider_error_logic(
        self,
        integration_name: str,
        integration_id: int,
        exc: Exception,
        *,
        logger: logging.Logger | None = None,
    ) -> str:
        """Lógica exata de _safe_provider_error (sem importar o módulo)."""
        _logger = logger or logging.getLogger("backend.app.routers.integrations")
        _logger.warning(
            "provider error integration_id=%s name=%r: %s",
            integration_id,
            integration_name,
            exc,
            exc_info=True,
        )
        return f"{integration_name}: falha ao consultar provedor"

    def test_returns_generic_message(self):
        exc = RuntimeError("SSL 10.0.0.1 SAN mismatch indexer-internal.corp.local")
        msg = self._safe_provider_error_logic("Wazuh Prod", 42, exc)
        assert msg == "Wazuh Prod: falha ao consultar provedor"
        assert "10.0.0" not in msg
        assert "indexer-internal" not in msg

    def test_logs_full_exception(self, caplog):
        internal = "192.168.1.50 TCP connection refused port 9200"
        exc = ConnectionError(internal)
        with caplog.at_level(logging.WARNING, logger="backend.app.routers.integrations"):
            self._safe_provider_error_logic("Test", 7, exc)
        assert internal in " ".join(caplog.messages)

    def test_integration_name_in_return(self):
        msg = self._safe_provider_error_logic("My SIEM", 1, Exception("details"))
        assert "My SIEM" in msg
        assert "falha ao consultar provedor" in msg

    def test_helper_function_in_router_file(self):
        router_path = os.path.join(
            os.path.dirname(__file__), "../../routers/integrations.py"
        )
        with open(router_path) as f:
            content = f.read()
        assert "_safe_provider_error" in content
        assert "falha ao consultar provedor" in content
        assert "exc_info=True" in content

    def test_unsafe_pattern_removed_from_integrations_router(self):
        """f"{integration.name}: {exc}" não deve aparecer em partial_errors.append."""
        router_path = os.path.join(
            os.path.dirname(__file__), "../../routers/integrations.py"
        )
        with open(router_path) as f:
            content = f.read()
        unsafe = re.findall(r'partial_errors\.append\(f"[^"]*\{exc\}"', content)
        assert len(unsafe) == 0, f"Padrão inseguro encontrado: {unsafe}"

    def test_unsafe_pattern_removed_from_dashboard(self):
        """dashboard.py também não deve expor exc diretamente."""
        dashboard_path = os.path.join(
            os.path.dirname(__file__), "../../routers/dashboard.py"
        )
        with open(dashboard_path) as f:
            content = f.read()
        unsafe = re.findall(r'partial_errors\.append\(f"[^"]*\{exc\}"', content)
        assert len(unsafe) == 0, f"Padrão inseguro em dashboard.py: {unsafe}"

    def test_dashboard_uses_generic_message(self):
        """dashboard.py deve usar mensagem genérica."""
        dashboard_path = os.path.join(
            os.path.dirname(__file__), "../../routers/dashboard.py"
        )
        with open(dashboard_path) as f:
            content = f.read()
        assert "falha ao consultar provedor" in content
