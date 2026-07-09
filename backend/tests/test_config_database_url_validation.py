"""Fail-fast de DATABASE_URL (Postgres obrigatório em prod).

Verifica o validator ``forbid_sqlite_in_production`` (backend/app/core/config.py):
- APP_ENV=production + DATABASE_URL sqlite (qualquer variante) → ValidationError.
- APP_ENV=production + Postgres → instancia com sucesso.
- dev/test/staging/local + sqlite → permitido (default histórico).

Todos os casos passam ``DATABASE_URL`` explícito: em pydantic-settings kwargs do
init vencem env vars, então os testes são determinísticos mesmo se o ambiente de
CI exportar um DATABASE_URL próprio.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.app.core.config import Settings

_TEST_KEY = "test-master-key-for-centralops-suite-12345"
_PG_URL = "postgresql+psycopg://user:pass@db:5432/centralops"
_SQLITE_DEFAULT = "sqlite:///./data/sophos.db"


def _settings(**overrides):
    """Constrói Settings com mínimos válidos; ``overrides`` ajusta o caso sob teste."""
    base = dict(
        _env_file=None,  # ignora .env do disco
        APP_MASTER_KEY=_TEST_KEY,
        SESSION_SECURE_COOKIE=True,  # não colidir com o validator de cookie em prod
    )
    base.update(overrides)
    return Settings(**base)


def test_field_default_is_sqlite() -> None:
    """Documenta o default histórico do campo (a coisa que o fail-fast protege)."""
    assert str(Settings.model_fields["DATABASE_URL"].default).startswith("sqlite")


def test_sqlite_rejected_in_production() -> None:
    with pytest.raises(ValidationError) as exc_info:
        _settings(APP_ENV="production", DATABASE_URL=_SQLITE_DEFAULT)

    errors = exc_info.value.errors()
    error_fields = {err["loc"][0] for err in errors}
    assert "DATABASE_URL" in error_fields, f"Esperava erro em DATABASE_URL, got: {errors}"
    messages = [err["msg"] for err in errors if err["loc"][0] == "DATABASE_URL"]
    assert any("SQLite" in m or "sqlite" in m.lower() for m in messages), (
        f"Mensagem deveria mencionar SQLite: {messages}"
    )


def test_postgres_accepted_in_production() -> None:
    settings = _settings(APP_ENV="production", DATABASE_URL=_PG_URL)
    assert settings.DATABASE_URL == _PG_URL
    assert settings.APP_ENV == "production"


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///:memory:",
        "sqlite:////abs/path/centralops.db",
        "sqlite:///./data/sophos.db",
        "SQLite:///./data/sophos.db",  # case-insensitive (validator faz .lower())
    ],
)
def test_all_sqlite_variants_rejected_in_production(url: str) -> None:
    with pytest.raises(ValidationError):
        _settings(APP_ENV="production", DATABASE_URL=url)


@pytest.mark.parametrize("env", ["dev", "test", "staging", "local"])
def test_sqlite_allowed_in_non_production(env: str) -> None:
    settings = _settings(
        APP_ENV=env,
        SESSION_SECURE_COOKIE=False,  # permitido fora de produção
        DATABASE_URL=_SQLITE_DEFAULT,
    )
    assert settings.APP_ENV == env
    assert settings.DATABASE_URL.startswith("sqlite")


@pytest.mark.parametrize("env", ["dev", "test", "staging", "local"])
def test_postgres_allowed_in_non_production(env: str) -> None:
    settings = _settings(APP_ENV=env, SESSION_SECURE_COOKIE=False, DATABASE_URL=_PG_URL)
    assert settings.DATABASE_URL == _PG_URL
