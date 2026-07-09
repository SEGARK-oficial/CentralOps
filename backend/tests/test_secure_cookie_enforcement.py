"""Testes para enforcement de SESSION_SECURE_COOKIE em produção.

Verifica:
- APP_ENV=production + SESSION_SECURE_COOKIE=false → ValidationError ao instanciar Settings.
- APP_ENV=dev ou test permite SESSION_SECURE_COOKIE=false.
- APP_ENV=production + SESSION_SECURE_COOKIE=true → instancia com sucesso.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

# Importa a classe Settings (não a instância singleton)
from backend.app.core.config import Settings

# Chave válida para testes (>= 32 chars, não está na lista de inseguras)
_TEST_KEY = "test-master-key-for-centralops-suite-12345"
# Em produção o SQLite default é rejeitado pelo validator forbid_sqlite_in_production
# Os testes de cookie abaixo isolam a intenção passando um
# Postgres explícito — sem ele, a construção produção falharia por DATABASE_URL.
_PG_URL = "postgresql+psycopg://user:pass@db:5432/centralops"


# ── Testes: enforcement de cookie seguro ──────────────────────────────

def test_secure_cookie_required_in_production() -> None:
    """Produção com SESSION_SECURE_COOKIE=false deve falhar no startup."""
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            _env_file=None,  # ignora .env do disco
            APP_MASTER_KEY=_TEST_KEY,
            APP_ENV="production",
            DATABASE_URL=_PG_URL,
            SESSION_SECURE_COOKIE=False,
        )

    errors = exc_info.value.errors()
    # Deve haver ao menos um erro relacionado ao SESSION_SECURE_COOKIE
    error_fields = {err["loc"][0] for err in errors}
    assert "SESSION_SECURE_COOKIE" in error_fields, (
        f"Esperava erro em SESSION_SECURE_COOKIE, got: {errors}"
    )
    # Mensagem deve ser informativa
    messages = [err["msg"] for err in errors if err["loc"][0] == "SESSION_SECURE_COOKIE"]
    assert any("produção" in msg or "production" in msg.lower() for msg in messages), (
        f"Mensagem de erro deveria mencionar produção: {messages}"
    )


def test_secure_cookie_optional_in_dev() -> None:
    """Em desenvolvimento, SESSION_SECURE_COOKIE=false é permitido."""
    settings = Settings(
        _env_file=None,
        APP_MASTER_KEY=_TEST_KEY,
        APP_ENV="dev",
        SESSION_SECURE_COOKIE=False,
    )
    assert settings.SESSION_SECURE_COOKIE is False
    assert settings.APP_ENV == "dev"


def test_secure_cookie_optional_in_test() -> None:
    """Em test, SESSION_SECURE_COOKIE=false é permitido."""
    settings = Settings(
        _env_file=None,
        APP_MASTER_KEY=_TEST_KEY,
        APP_ENV="test",
        SESSION_SECURE_COOKIE=False,
    )
    assert settings.SESSION_SECURE_COOKIE is False
    assert settings.APP_ENV == "test"


def test_secure_cookie_optional_in_staging() -> None:
    """Em staging (não-produção), SESSION_SECURE_COOKIE=false é permitido."""
    settings = Settings(
        _env_file=None,
        APP_MASTER_KEY=_TEST_KEY,
        APP_ENV="staging",
        SESSION_SECURE_COOKIE=False,
    )
    assert settings.SESSION_SECURE_COOKIE is False


def test_secure_cookie_true_in_production_passes() -> None:
    """Produção com SESSION_SECURE_COOKIE=true é válido."""
    settings = Settings(
        _env_file=None,
        APP_MASTER_KEY=_TEST_KEY,
        APP_ENV="production",
        DATABASE_URL=_PG_URL,
        SESSION_SECURE_COOKIE=True,
    )
    assert settings.SESSION_SECURE_COOKIE is True
    assert settings.APP_ENV == "production"


def test_secure_cookie_default_is_false_and_requires_production_override() -> None:
    """Confirma que o default é False e que production exige configuração explícita."""
    # O default de SESSION_SECURE_COOKIE é False (ver config.py linha 51).
    # Em produção isso deve falhar — o operador deve configurar explicitamente.
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            APP_MASTER_KEY=_TEST_KEY,
            APP_ENV="production",
            DATABASE_URL=_PG_URL,
            # SESSION_SECURE_COOKIE omitido — usa default False → deve falhar
        )


@pytest.mark.parametrize("env", ["dev", "test", "staging", "local"])
def test_secure_cookie_false_allowed_in_non_production_envs(env: str) -> None:
    """Qualquer env que não seja 'production' permite SESSION_SECURE_COOKIE=false."""
    settings = Settings(
        _env_file=None,
        APP_MASTER_KEY=_TEST_KEY,
        APP_ENV=env,
        SESSION_SECURE_COOKIE=False,
    )
    assert settings.APP_ENV == env
    assert settings.SESSION_SECURE_COOKIE is False
