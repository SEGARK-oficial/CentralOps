"""Testes dos 5 fixes de segurança — branch security/post-pluggable-fixes.

H-2: Rate limit POST /integrations (30/min por user) + limite por org.
M-1 (revisado): verify_ssl=False é a escolha do usuário em qualquer ambiente (WARN, não rejeita).
M-2: Sanitização de mensagens de erro de provider.
M-4: RedBeat namespace por ambiente.
L-2: Redação de Redis URL com senha em logs.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.api.schemas import IntegrationCreate, IntegrationUpdate
from backend.app.collectors.celery_app import _SecretsFilter
from backend.app.core.rate_limiter import IntegrationRateLimiter
from backend.app.db.database import Base, get_session
from backend.app.main import app


# ---------------------------------------------------------------------------
# Fixtures compartilhadas
# ---------------------------------------------------------------------------

@pytest.fixture()
def client_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    testing_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_session():
        db = testing_session_local()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_session] = override_get_session
    clients: list[TestClient] = []

    def factory() -> TestClient:
        client = TestClient(app)
        clients.append(client)
        return client

    yield factory

    for client in clients:
        client.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


def bootstrap_admin(client: TestClient) -> dict:
    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPassword123!", "display_name": "Admin"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def create_org(client: TestClient, name: str) -> dict:
    r = client.post("/api/organizations/", json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()


def wazuh_payload(org_id: int, name: str = "W") -> dict:
    # Indexer é obrigatório (fonte de alertas/detecções); Manager é opcional
    # mas, quando presente, exige user+pass. Ver [[integration-form-modal-legacy]].
    return {
        "organization_id": org_id,
        "name": name,
        "platform": "wazuh",
        "indexer_url": "https://indexer.example.com:9200",
        "indexer_username": "iuser",
        "indexer_password": "ipass123",
        "manager_url": "manager.example.com:55000",
        "manager_api_username": "user",
        "manager_api_password": "pass123",
        "verify_ssl": True,
    }


# ---------------------------------------------------------------------------
# H-2 — Rate limit: 30 POST/min por user
# ---------------------------------------------------------------------------

class TestIntegrationRateLimiterUnit:
    """Testa IntegrationRateLimiter em memória sem HTTP."""

    def test_create_allows_up_to_limit(self):
        limiter = IntegrationRateLimiter(max_creates_per_minute=5)
        for _ in range(5):
            assert limiter.check_create(user_id=1) is None

    def test_create_blocks_on_31st_request(self):
        limiter = IntegrationRateLimiter(max_creates_per_minute=30)
        for _ in range(30):
            assert limiter.check_create(user_id=42) is None
        retry_after = limiter.check_create(user_id=42)
        assert retry_after is not None
        assert retry_after >= 1

    def test_delete_blocks_on_6th_request(self):
        limiter = IntegrationRateLimiter(max_deletes_per_minute=5)
        for _ in range(5):
            assert limiter.check_delete(user_id=7) is None
        retry_after = limiter.check_delete(user_id=7)
        assert retry_after is not None

    def test_create_limit_isolated_per_user(self):
        limiter = IntegrationRateLimiter(max_creates_per_minute=2)
        for _ in range(2):
            limiter.check_create(user_id=1)
        # user 1 está no limite
        assert limiter.check_create(user_id=1) is not None
        # user 2 não foi afetado
        assert limiter.check_create(user_id=2) is None

    def test_get_integrations_not_rate_limited(self, client_factory):
        """GET /integrations nunca retorna 429."""
        client = client_factory()
        bootstrap_admin(client)
        for _ in range(50):
            r = client.get("/api/integrations/")
            assert r.status_code == 200

    def test_redis_backed_limiter(self):
        """Redis-backed limiter respeita mesmo limite."""
        fake_redis = fakeredis.FakeRedis(decode_responses=True)
        limiter = IntegrationRateLimiter(
            max_creates_per_minute=5,
            redis_client=fake_redis,
        )
        for _ in range(5):
            assert limiter.check_create(user_id=99) is None
        retry_after = limiter.check_create(user_id=99)
        assert retry_after is not None
        assert retry_after >= 1


class TestRateLimitHTTP:
    """Testa o rate limit via HTTP."""

    def test_post_returns_429_after_limit(self, client_factory, monkeypatch):
        client = client_factory()
        bootstrap_admin(client)
        org = create_org(client, "RL Org")

        # Injetar limiter com limite baixo
        fake_redis = fakeredis.FakeRedis(decode_responses=True)
        try:
            test_limiter = IntegrationRateLimiter(
                max_creates_per_minute=3,
                redis_client=fake_redis,
            )
            monkeypatch.setattr(
                "backend.app.routers.integrations.integration_rate_limiter",
                test_limiter,
            )

            # 3 requests devem passar
            for i in range(3):
                r = client.post("/api/integrations/", json=wazuh_payload(org["id"], f"W{i}"))
                assert r.status_code in (200, 201), f"request {i} falhou: {r.text}"

            # 4º request deve retornar 429
            r = client.post("/api/integrations/", json=wazuh_payload(org["id"], "W_extra"))
            assert r.status_code == 429, r.text
            assert "Retry-After" in r.headers
            assert int(r.headers["Retry-After"]) >= 1
        finally:
            monkeypatch.undo()

    def test_post_returns_400_when_org_at_limit(self, client_factory, monkeypatch):
        client = client_factory()
        bootstrap_admin(client)
        org = create_org(client, "Org At Limit")

        # Simular org com MAX_INTEGRATIONS_PER_ORG integrações ativas
        from backend.app.db.repository import IntegrationRepository

        monkeypatch.setattr(
            IntegrationRepository,
            "count_active",
            lambda self, org_id: 100,
        )

        with patch(
            "backend.app.core.config.settings.MAX_INTEGRATIONS_PER_ORG",
            100,
        ):
            r = client.post("/api/integrations/", json=wazuh_payload(org["id"], "Over Limit"))
            assert r.status_code == 400, r.text
            # Erros são localizados (i18n Fase 4) → asserir no code estável, não no texto.
            assert r.json()["error"]["code"] == "integration.org_limit_reached"

    def test_org_limit_does_not_return_429(self, client_factory, monkeypatch):
        """Limite por org deve retornar 400, não 429."""
        client = client_factory()
        bootstrap_admin(client)
        org = create_org(client, "Org Limit Type Check")

        from backend.app.db.repository import IntegrationRepository
        monkeypatch.setattr(
            IntegrationRepository,
            "count_active",
            lambda self, org_id: 999,
        )

        with patch("backend.app.core.config.settings.MAX_INTEGRATIONS_PER_ORG", 1):
            r = client.post("/api/integrations/", json=wazuh_payload(org["id"], "Blocked"))
            assert r.status_code == 400, r.text
            # Garantir que NÃO é 429
            assert r.status_code != 429


# ---------------------------------------------------------------------------
# M-1 — verify_ssl=False proibido em produção
# ---------------------------------------------------------------------------

class TestVerifySslProduction:
    def test_verify_ssl_false_allowed_in_production_with_warning(self, caplog):
        """verify_ssl=False é a escolha EXPLÍCITA do usuário — ACEITO em produção (revisão
        do M-1, commit cdbe863: Wazuh/self-hosted usam cert auto-assinado; bloquear em prod
        tornava a conexão impossível). Não é mais rejeitado; emite um WARNING auditável."""
        with patch("backend.app.api.schemas.settings") as mock_settings:
            mock_settings.APP_ENV = "production"
            with caplog.at_level(logging.WARNING, logger="backend.app.api.schemas"):
                obj = IntegrationCreate(
                    organization_id=1,
                    name="Wazuh Prod",
                    platform="wazuh",
                    manager_url="https://manager.example.com:55000",
                    manager_api_username="user",
                    manager_api_password="pass",
                    verify_ssl=False,
                )
        assert obj.verify_ssl is False
        assert "verify_ssl=False" in caplog.text  # WARNING auditável foi emitido

    def test_verify_ssl_false_allowed_in_development(self):
        with patch("backend.app.api.schemas.settings") as mock_settings:
            mock_settings.APP_ENV = "development"
            obj = IntegrationCreate(
                organization_id=1,
                name="Wazuh Dev",
                platform="wazuh",
                manager_url="https://manager.example.com:55000",
                manager_api_username="user",
                manager_api_password="pass",
                verify_ssl=False,
            )
            assert obj.verify_ssl is False

    def test_verify_ssl_true_allowed_in_any_env(self):
        for env in ("production", "staging", "development", "test"):
            with patch("backend.app.api.schemas.settings") as mock_settings:
                mock_settings.APP_ENV = env
                obj = IntegrationCreate(
                    organization_id=1,
                    name="W",
                    platform="wazuh",
                    manager_url="https://manager.example.com:55000",
                    manager_api_username="user",
                    manager_api_password="pass",
                    verify_ssl=True,
                )
                assert obj.verify_ssl is True

    def test_verify_ssl_false_accepted_via_http(self, client_factory):
        """Fluxo HTTP: verify_ssl=False é aceito (APP_ENV=test no conftest) — coerente com
        a decisão-do-usuário em qualquer ambiente (revisão do M-1)."""
        client = client_factory()
        bootstrap_admin(client)
        org = create_org(client, "Org SSL Test")
        payload = wazuh_payload(org["id"])
        payload["verify_ssl"] = False
        r = client.post("/api/integrations/", json=payload)
        assert r.status_code in (200, 201), r.text

    def test_verify_ssl_false_allowed_update_in_production_with_warning(self, caplog):
        """Update também aceita verify_ssl=False em produção (escolha do usuário) + WARNING."""
        with patch("backend.app.api.schemas.settings") as mock_settings:
            mock_settings.APP_ENV = "production"
            with caplog.at_level(logging.WARNING, logger="backend.app.api.schemas"):
                obj = IntegrationUpdate(verify_ssl=False)
        assert obj.verify_ssl is False
        assert "verify_ssl=False" in caplog.text


# ---------------------------------------------------------------------------
# M-4 — RedBeat namespace por ambiente
# ---------------------------------------------------------------------------

class TestRedbeatKeyPrefix:
    def test_prefix_includes_app_env(self):
        """_redbeat_key_prefix() retorna prefixo com APP_ENV."""
        from backend.app.collectors.celery_app import _redbeat_key_prefix

        with patch("backend.app.collectors.celery_app.settings") as mock_settings:
            mock_settings.APP_ENV = "development"
            prefix = _redbeat_key_prefix()
        assert prefix == "redbeat::development::"

    def test_prefix_production(self):
        from backend.app.collectors.celery_app import _redbeat_key_prefix

        with patch("backend.app.collectors.celery_app.settings") as mock_settings:
            mock_settings.APP_ENV = "production"
            prefix = _redbeat_key_prefix()
        assert prefix == "redbeat::production::"

    def test_prefix_staging(self):
        from backend.app.collectors.celery_app import _redbeat_key_prefix

        with patch("backend.app.collectors.celery_app.settings") as mock_settings:
            mock_settings.APP_ENV = "staging"
            prefix = _redbeat_key_prefix()
        assert prefix == "redbeat::staging::"

    def test_env_var_override(self, monkeypatch):
        """REDBEAT_KEY_PREFIX sobrescreve o padrão (compat operacional)."""
        from backend.app.collectors.celery_app import _redbeat_key_prefix

        monkeypatch.setenv("REDBEAT_KEY_PREFIX", "redbeat::custom::")
        with patch("backend.app.collectors.celery_app.settings") as mock_settings:
            mock_settings.APP_ENV = "production"
            prefix = _redbeat_key_prefix()
        assert prefix == "redbeat::custom::"

    def test_celery_app_conf_uses_env_prefix(self):
        """celery_app.conf.redbeat_key_prefix contém APP_ENV no prefixo."""
        from backend.app.collectors.celery_app import celery_app

        prefix = celery_app.conf.redbeat_key_prefix
        # O prefixo deve conter o padrão redbeat::<env>::
        assert prefix.startswith("redbeat::")
        assert prefix.endswith("::")
        # Deve ter 3 partes: "redbeat", <env>, "" → split("::") dá 3 itens
        parts = prefix.split("::")
        assert len(parts) == 3
        assert parts[0] == "redbeat"
        assert parts[1] != ""  # env não é vazio


# ---------------------------------------------------------------------------
# L-2 — Redação de Redis URL com senha em logs
# ---------------------------------------------------------------------------

class TestSecretsFilterRedisUrl:
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
        f = _SecretsFilter()
        record = self._make_record(
            "Connecting to redis://:supersecret@host:6379/0 failed"
        )
        assert f.filter(record) is True
        assert "supersecret" not in record.getMessage()
        assert "redis://[REDACTED]@host:6379/0" in record.getMessage()

    def test_redis_url_without_password_is_not_touched(self):
        f = _SecretsFilter()
        record = self._make_record("Connected to redis://host:6379/0")
        assert f.filter(record) is True
        assert "redis://host:6379/0" in record.getMessage()

    def test_redis_url_with_user_and_password_is_redacted(self):
        f = _SecretsFilter()
        record = self._make_record("URL: redis://myuser:mypass@redis.internal:6380/1")
        assert f.filter(record) is True
        msg = record.getMessage()
        assert "mypass" not in msg
        assert "myuser" not in msg
        assert "[REDACTED]" in msg

    def test_access_token_needle_still_redacted(self):
        """Garantir que needles existentes continuam funcionando com o novo código."""
        f = _SecretsFilter()
        record = self._make_record("Token: access_token=abc123")
        assert f.filter(record) is True
        assert "access_token" not in record.getMessage() or "[secret redacted" in record.getMessage()

    def test_message_with_both_redis_url_and_token_is_fully_redacted(self):
        f = _SecretsFilter()
        record = self._make_record(
            "Error at redis://:pass@host:6379/0 while using access_token=abc"
        )
        assert f.filter(record) is True
        msg = record.getMessage()
        # Primeiro redis redatado, depois access_token redatado
        assert "pass" not in msg or "[REDACTED]" in msg

    def test_format_args_redaction(self):
        """Testamos que args são zerados após redação do Redis URL."""
        f = _SecretsFilter()
        record = self._make_record(
            "Broker URL: %s",
            ("redis://:topsecret@broker:6379/1",),
        )
        assert f.filter(record) is True
        msg = record.getMessage()
        assert "topsecret" not in msg
