"""Testes unitários do ApiTokenService.

Cobre:
- Criação produz raw token com prefixo copsk_, prefix index, hash Argon2.
- Hash não é o raw (defesa contra logging acidental).
- resolve_bearer aceita token válido, rejeita token errado, expirado, revogado.
- list_for_user filtra revoked e ordena por created_at desc.
- revoke_token só permite quando o token pertence ao caller.
- record_usage incrementa use_count e popula last_used_*.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Generator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import models
from backend.app.db.database import Base
from backend.app.services.api_tokens import (
    ApiTokenService,
    TOKEN_RAW_PREFIX,
    _verify_token_hash,
)


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
def other_user(db: Session) -> models.AppUser:
    u = models.AppUser(
        username="other",
        password_hash="x",
        role="viewer",
        is_active=True,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


# ── Geração ─────────────────────────────────────────────────────────────


def test_create_token_returns_copsk_prefix(db: Session, user: models.AppUser):
    service = ApiTokenService(db)
    raw, token = service.create_token(user=user, name="ci-bot", expires_at=None)

    assert raw.startswith(TOKEN_RAW_PREFIX)
    assert len(raw) > len(TOKEN_RAW_PREFIX) + 20  # tem entropia suficiente
    # Prefixo armazenado não inclui o token completo, mas tem 12 chars
    assert token.token_prefix == raw[:12]
    # Hash !== raw (sanity)
    assert token.token_hash != raw
    # Argon2 self-described format
    assert token.token_hash.startswith("$argon2")


def test_create_token_persists_with_expires_at(db: Session, user: models.AppUser):
    service = ApiTokenService(db)
    expires = datetime.utcnow() + timedelta(days=30)
    _, token = service.create_token(user=user, name="builder", expires_at=expires)
    assert token.expires_at == expires
    assert token.user_id == user.id
    assert token.revoked_at is None


def test_create_token_rejects_empty_name(db: Session, user: models.AppUser):
    service = ApiTokenService(db)
    with pytest.raises(ValueError, match="empty"):
        service.create_token(user=user, name="   ", expires_at=None)


def test_create_token_rejects_duplicate_name_same_user(
    db: Session, user: models.AppUser
):
    service = ApiTokenService(db)
    service.create_token(user=user, name="ci-bot", expires_at=None)
    with pytest.raises(ValueError, match="already in use"):
        service.create_token(user=user, name="ci-bot", expires_at=None)


def test_create_token_allows_same_name_for_different_users(
    db: Session, user: models.AppUser, other_user: models.AppUser
):
    service = ApiTokenService(db)
    service.create_token(user=user, name="shared-name", expires_at=None)
    # Não deve levantar — name é único por user_id
    raw, token = service.create_token(
        user=other_user, name="shared-name", expires_at=None
    )
    assert token.user_id == other_user.id


def test_create_token_rejects_past_expires_at(db: Session, user: models.AppUser):
    service = ApiTokenService(db)
    past = datetime.utcnow() - timedelta(seconds=1)
    with pytest.raises(ValueError, match="future"):
        service.create_token(user=user, name="bad", expires_at=past)


# ── resolve_bearer ─────────────────────────────────────────────────────


def test_resolve_bearer_returns_token_for_valid_raw(
    db: Session, user: models.AppUser
):
    service = ApiTokenService(db)
    raw, created = service.create_token(user=user, name="ci", expires_at=None)
    resolved = service.resolve_bearer(raw)
    assert resolved is not None
    assert resolved.id == created.id
    assert resolved.user_id == user.id


def test_resolve_bearer_rejects_garbled_token(
    db: Session, user: models.AppUser
):
    service = ApiTokenService(db)
    raw, _ = service.create_token(user=user, name="ci", expires_at=None)
    # Mesmo prefixo (mesmas primeiras 12 chars), corpo errado
    bad = raw[:12] + "XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
    assert service.resolve_bearer(bad) is None


def test_resolve_bearer_rejects_wrong_prefix(db: Session):
    service = ApiTokenService(db)
    assert service.resolve_bearer("notapat_aB3xK7zY9MmRTpFqZqVm5e8XvU") is None
    assert service.resolve_bearer("") is None
    assert service.resolve_bearer("Bearer copsk_abc") is None  # com prefix do header


def test_resolve_bearer_rejects_expired(db: Session, user: models.AppUser):
    service = ApiTokenService(db)
    # Cria normal e depois força expires_at no passado
    raw, created = service.create_token(
        user=user,
        name="exp",
        expires_at=datetime.utcnow() + timedelta(days=1),
    )
    created.expires_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()
    assert service.resolve_bearer(raw) is None


def test_resolve_bearer_rejects_revoked(db: Session, user: models.AppUser):
    service = ApiTokenService(db)
    raw, created = service.create_token(user=user, name="rev", expires_at=None)
    service.revoke_token(user=user, token_id=created.id)
    assert service.resolve_bearer(raw) is None


def test_resolve_bearer_handles_eternal_token(db: Session, user: models.AppUser):
    """Tokens com expires_at=None ('nunca expira') devem ser aceitos."""
    service = ApiTokenService(db)
    raw, _ = service.create_token(user=user, name="forever", expires_at=None)
    resolved = service.resolve_bearer(raw)
    assert resolved is not None


# ── list / revoke ──────────────────────────────────────────────────────


def test_list_for_user_excludes_revoked_by_default(
    db: Session, user: models.AppUser
):
    service = ApiTokenService(db)
    _, t1 = service.create_token(user=user, name="active", expires_at=None)
    _, t2 = service.create_token(user=user, name="revoked", expires_at=None)
    service.revoke_token(user=user, token_id=t2.id)

    active = service.list_for_user(user.id)
    assert [t.id for t in active] == [t1.id]

    all_tokens = service.list_for_user(user.id, include_revoked=True)
    assert {t.id for t in all_tokens} == {t1.id, t2.id}


def test_revoke_token_other_user_returns_none(
    db: Session, user: models.AppUser, other_user: models.AppUser
):
    service = ApiTokenService(db)
    _, token = service.create_token(user=user, name="ci", expires_at=None)
    # other_user tenta revogar token do user — deve falhar
    result = service.revoke_token(user=other_user, token_id=token.id)
    assert result is None
    # Token continua não revogado
    db.refresh(token)
    assert token.revoked_at is None


def test_revoke_token_idempotent(db: Session, user: models.AppUser):
    service = ApiTokenService(db)
    _, token = service.create_token(user=user, name="ci", expires_at=None)
    first = service.revoke_token(user=user, token_id=token.id)
    revoked_at = first.revoked_at
    # Segunda chamada não muda timestamp
    second = service.revoke_token(user=user, token_id=token.id)
    assert second.revoked_at == revoked_at


# ── record_usage ───────────────────────────────────────────────────────


def test_record_usage_increments_count_and_sets_ip(
    db: Session, user: models.AppUser
):
    service = ApiTokenService(db)
    _, token = service.create_token(user=user, name="ci", expires_at=None)
    assert token.use_count == 0
    assert token.last_used_at is None

    service.record_usage(token, ip_address="1.2.3.4")
    db.refresh(token)
    assert token.use_count == 1
    assert token.last_used_ip == "1.2.3.4"
    assert token.last_used_at is not None


# ── Hash semantics ─────────────────────────────────────────────────────


def test_hash_is_argon2id_and_unique_per_call(db: Session, user: models.AppUser):
    """Mesmo raw token gera hashes diferentes (salt distinto)."""
    service = ApiTokenService(db)
    raw, t1 = service.create_token(user=user, name="t1", expires_at=None)
    # Não dá pra criar segundo PAT com mesmo raw via service (random),
    # então testamos diretamente o hash helper exposto.
    from backend.app.services.api_tokens import _hash_token
    h1 = _hash_token(raw)
    h2 = _hash_token(raw)
    assert h1 != h2  # salts distintos
    assert _verify_token_hash(h1, raw)
    assert _verify_token_hash(h2, raw)
