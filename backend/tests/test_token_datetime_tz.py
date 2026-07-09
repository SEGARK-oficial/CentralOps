"""Teste de regressão: bug naive/aware em expires_at de API tokens.

Garante que clientes enviando ISO 8601 com timezone (ex: "2030-01-01T00:00:00Z")
não causam TypeError ao criar tokens via service ou endpoint HTTP.

Bug: ApiTokenService.create_token comparava expires_at (aware, vindo do Pydantic)
com datetime.utcnow() (naive), levantando TypeError.

Fix: ensure_naive_utc() normaliza qualquer datetime aware para naive-UTC antes
de comparar ou persistir.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Generator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core.datetime_utils import ensure_naive_utc
from backend.app.db import models
from backend.app.db.database import Base, get_session
from backend.app.main import app
from backend.app.services.api_tokens import ApiTokenService

from fastapi.testclient import TestClient


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture()
def db() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def user(db: Session) -> models.AppUser:
    u = models.AppUser(
        username="tester",
        password_hash="x",
        role="admin",
        is_active=True,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture()
def http_setup() -> Generator[Any, None, None]:
    """TestClient com banco em memória + admin bootstrappado."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_session():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_session] = override_get_session
    client = TestClient(app)

    # Limpa rate limiter para evitar contaminação entre testes.
    from backend.app.core.rate_limiter import token_rate_limiter
    token_rate_limiter._windows.clear()

    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPassword123!"},
    )
    assert r.status_code == 200, r.text

    yield client

    client.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


# ── Testes do helper ensure_naive_utc ────────────────────────────────────


def test_ensure_naive_utc_retorna_none_se_none():
    assert ensure_naive_utc(None) is None


def test_ensure_naive_utc_preserva_naive():
    naive = datetime(2030, 1, 1, 12, 0, 0)
    result = ensure_naive_utc(naive)
    assert result == naive
    assert result.tzinfo is None


def test_ensure_naive_utc_converte_utc_aware_para_naive():
    aware = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    result = ensure_naive_utc(aware)
    assert result == datetime(2030, 1, 1, 12, 0, 0)
    assert result.tzinfo is None


def test_ensure_naive_utc_converte_offset_positivo_para_naive_utc():
    """Offset +03:00 deve subtrair 3h ao converter para UTC naive."""
    from datetime import timezone as tz_module
    tz_plus3 = tz_module(timedelta(hours=3))
    aware = datetime(2030, 1, 1, 15, 0, 0, tzinfo=tz_plus3)  # 15:00+03 = 12:00 UTC
    result = ensure_naive_utc(aware)
    assert result == datetime(2030, 1, 1, 12, 0, 0)
    assert result.tzinfo is None


# ── Testes de unidade do service ─────────────────────────────────────────


def test_create_token_com_expires_at_aware_nao_levanta_typeerror(
    db: Session, user: models.AppUser
):
    """Bug principal: expires_at aware não deve levantar TypeError."""
    service = ApiTokenService(db)
    expires_aware = datetime(2030, 1, 1, tzinfo=timezone.utc)
    # Antes do fix: TypeError: can't compare offset-naive and offset-aware datetimes
    raw, token = service.create_token(
        user=user, name="aware-token", expires_at=expires_aware
    )
    assert raw is not None
    assert token.expires_at is not None


def test_create_token_persiste_expires_at_naive(db: Session, user: models.AppUser):
    """expires_at aware do request deve ser armazenado como naive-UTC no banco."""
    service = ApiTokenService(db)
    expires_aware = datetime(2030, 6, 15, 10, 30, 0, tzinfo=timezone.utc)
    _, token = service.create_token(
        user=user, name="naive-persist", expires_at=expires_aware
    )
    # Banco deve guardar naive — sem tzinfo.
    assert token.expires_at is not None
    assert token.expires_at.tzinfo is None
    # Valor deve ser UTC equivalente.
    assert token.expires_at == datetime(2030, 6, 15, 10, 30, 0)


def test_create_token_rejeita_expires_at_aware_no_passado(
    db: Session, user: models.AppUser
):
    """expires_at aware no passado ainda deve ser rejeitado com ValueError."""
    service = ApiTokenService(db)
    passado_aware = datetime(2020, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="future"):
        service.create_token(user=user, name="past-aware", expires_at=passado_aware)


def test_create_token_com_expires_at_naive_continua_funcionando(
    db: Session, user: models.AppUser
):
    """Caminho original (naive) não deve ser afetado pelo fix."""
    service = ApiTokenService(db)
    expires_naive = datetime.utcnow() + timedelta(days=30)
    _, token = service.create_token(
        user=user, name="naive-orig", expires_at=expires_naive
    )
    assert token.expires_at == expires_naive
    assert token.expires_at.tzinfo is None


# ── Testes de endpoint HTTP ───────────────────────────────────────────────


def test_post_tokens_com_expires_at_iso_z_retorna_201(http_setup):
    """POST /api/v1/tokens com expires_at ISO+Z deve retornar 201, não 500."""
    client = http_setup
    r = client.post(
        "/api/v1/tokens",
        json={"name": "tz-aware-token", "expires_at": "2030-01-01T00:00:00Z"},
    )
    assert r.status_code == 201, r.text


def test_post_tokens_com_expires_at_offset_explicito_retorna_201(http_setup):
    """POST /api/v1/tokens com offset +00:00 explícito deve retornar 201."""
    client = http_setup
    r = client.post(
        "/api/v1/tokens",
        json={"name": "offset-token", "expires_at": "2030-06-15T10:30:00+00:00"},
    )
    assert r.status_code == 201, r.text


def test_post_tokens_com_expires_at_no_passado_retorna_400(http_setup):
    """POST /api/v1/tokens com expires_at passado (aware) deve retornar 400, não 500."""
    client = http_setup
    r = client.post(
        "/api/v1/tokens",
        json={"name": "past-tz", "expires_at": "2020-01-01T00:00:00Z"},
    )
    assert r.status_code == 400, r.text
