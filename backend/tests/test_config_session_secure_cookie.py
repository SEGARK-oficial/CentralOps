"""SESSION_SECURE_COOKIE: string vazia = "não configurado", nunca bool_parsing.

Regressão do incidente jul/2026: o compose interpolava
``SESSION_SECURE_COOKIE=${SESSION_SECURE_COOKIE:-}`` nos serviços de collector;
sem a var no .env chegava ``''`` no Settings e o boot caía com
``ValidationError: bool_parsing`` — todos os collectors em crash-loop, zero
coleta, sem erro visível na UI. Cobre o validator ``empty_secure_cookie_means_unset``
(mode="before") + a interação com ``enforce_secure_cookie_in_production``.

Todos os casos passam kwargs explícitos: em pydantic-settings kwargs do init
vencem env vars, então os testes são determinísticos mesmo com env de CI sujo.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.app.core.config import Settings

_TEST_KEY = "test-master-key-for-centralops-suite-12345"
_PG_URL = "postgresql+psycopg://user:pass@db:5432/centralops"


def _settings(**overrides):
    """Constrói Settings com mínimos válidos; ``overrides`` ajusta o caso sob teste."""
    base = dict(
        _env_file=None,  # ignora .env do disco
        APP_MASTER_KEY=_TEST_KEY,
    )
    base.update(overrides)
    return Settings(**base)


def test_empty_string_in_production_resolves_true() -> None:
    """``''`` em produção vira True (default seguro) — o cenário do crash-loop."""
    s = _settings(APP_ENV="production", DATABASE_URL=_PG_URL, SESSION_SECURE_COOKIE="")
    assert s.SESSION_SECURE_COOKIE is True


def test_whitespace_string_in_production_resolves_true() -> None:
    s = _settings(APP_ENV="production", DATABASE_URL=_PG_URL, SESSION_SECURE_COOKIE="   ")
    assert s.SESSION_SECURE_COOKIE is True


def test_empty_string_outside_production_resolves_false() -> None:
    s = _settings(APP_ENV="development", SESSION_SECURE_COOKIE="")
    assert s.SESSION_SECURE_COOKIE is False


def test_explicit_false_in_production_still_rejected() -> None:
    """O before-validator não afrouxa o enforcement: False explícito em prod segue 422."""
    with pytest.raises(ValidationError, match="SESSION_SECURE_COOKIE"):
        _settings(APP_ENV="production", DATABASE_URL=_PG_URL, SESSION_SECURE_COOKIE="0")


def test_explicit_values_pass_through_unchanged() -> None:
    assert (
        _settings(APP_ENV="production", DATABASE_URL=_PG_URL, SESSION_SECURE_COOKIE="1").SESSION_SECURE_COOKIE
        is True
    )
    assert _settings(APP_ENV="development", SESSION_SECURE_COOKIE="0").SESSION_SECURE_COOKIE is False
