"""Tests for the daily housekeeping task that marks expired PATs as revoked."""

from __future__ import annotations

import os
from datetime import datetime, timedelta

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import database as _db_module
from backend.app.db import models as _models  # noqa: F401  registers tables
from backend.app.db.database import Base


@pytest.fixture
def fresh_db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(_db_module, "SessionLocal", SessionLocal)
    yield engine
    Base.metadata.drop_all(bind=engine)


def _make_user(db, *, username="alice", role="admin"):
    user = _models.AppUser(
        username=username,
        display_name=username.title(),
        password_hash="x",  # not used in this test
        role=role,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_token(
    db,
    user,
    *,
    name,
    expires_at,
    revoked_at=None,
):
    token = _models.ApiToken(
        user_id=user.id,
        name=name,
        token_hash=f"hash-{name}",
        token_prefix=f"copsk_{name}"[:12],
        scopes_json=None,
        expires_at=expires_at,
        is_eternal=expires_at is None,
        revoked_at=revoked_at,
    )
    db.add(token)
    db.commit()
    db.refresh(token)
    return token


def _run_task():
    from backend.app.collectors.api_tokens_housekeeping import mark_expired_api_tokens

    return mark_expired_api_tokens.run()  # type: ignore[attr-defined]


def test_marks_expired_token_as_revoked(fresh_db):
    SessionLocal = sessionmaker(bind=fresh_db)
    with SessionLocal() as db:
        user = _make_user(db)
        past = datetime.utcnow() - timedelta(days=1)
        tok = _make_token(db, user, name="oldie", expires_at=past)

    result = _run_task()
    assert result["affected"] == 1

    with SessionLocal() as db:
        refreshed = db.execute(
            select(_models.ApiToken).where(_models.ApiToken.id == tok.id)
        ).scalar_one()
        # Heuristic for "auto-expired": revoked_at equals expires_at.
        # Manual revocations set revoked_at != expires_at (usually < expires).
        assert refreshed.revoked_at == tok.expires_at


def test_does_not_touch_token_with_future_expiry(fresh_db):
    SessionLocal = sessionmaker(bind=fresh_db)
    with SessionLocal() as db:
        user = _make_user(db, username="bob")
        future = datetime.utcnow() + timedelta(days=10)
        tok = _make_token(db, user, name="future", expires_at=future)

    result = _run_task()
    assert result["affected"] == 0

    with SessionLocal() as db:
        refreshed = db.execute(
            select(_models.ApiToken).where(_models.ApiToken.id == tok.id)
        ).scalar_one()
        assert refreshed.revoked_at is None


def test_does_not_touch_eternal_token_without_expires_at(fresh_db):
    SessionLocal = sessionmaker(bind=fresh_db)
    with SessionLocal() as db:
        user = _make_user(db, username="carol")
        tok = _make_token(db, user, name="eternal", expires_at=None)

    result = _run_task()
    assert result["affected"] == 0

    with SessionLocal() as db:
        refreshed = db.execute(
            select(_models.ApiToken).where(_models.ApiToken.id == tok.id)
        ).scalar_one()
        assert refreshed.revoked_at is None


def test_does_not_re_revoke_already_revoked_token(fresh_db):
    SessionLocal = sessionmaker(bind=fresh_db)
    earlier_revocation = datetime(2026, 1, 1, 12, 0, 0)
    with SessionLocal() as db:
        user = _make_user(db, username="dave")
        past = datetime.utcnow() - timedelta(days=1)
        tok = _make_token(
            db,
            user,
            name="old-and-revoked",
            expires_at=past,
            revoked_at=earlier_revocation,
        )

    result = _run_task()
    assert result["affected"] == 0  # WHERE revoked_at IS NULL filters this out

    with SessionLocal() as db:
        refreshed = db.execute(
            select(_models.ApiToken).where(_models.ApiToken.id == tok.id)
        ).scalar_one()
        # Original revoked_at preserved — not overwritten by job.
        assert refreshed.revoked_at == earlier_revocation


def test_idempotent_when_run_twice(fresh_db):
    SessionLocal = sessionmaker(bind=fresh_db)
    with SessionLocal() as db:
        user = _make_user(db, username="eve")
        past = datetime.utcnow() - timedelta(hours=1)
        _make_token(db, user, name="t1", expires_at=past)
        _make_token(db, user, name="t2", expires_at=past)

    first = _run_task()
    second = _run_task()
    assert first["affected"] == 2
    assert second["affected"] == 0


def test_swallows_exceptions_and_returns_error_flag(fresh_db, monkeypatch):
    """Failure inside the task must not crash the worker — beat retries
    on the next schedule."""
    from backend.app.collectors import api_tokens_housekeeping

    class _BoomSession:
        def __enter__(self):
            raise RuntimeError("simulated DB outage")

        def __exit__(self, *_a):
            return False

    monkeypatch.setattr(
        api_tokens_housekeeping.database, "SessionLocal", lambda: _BoomSession()
    )
    result = _run_task()
    assert result["affected"] == 0
    assert result.get("error") is True
