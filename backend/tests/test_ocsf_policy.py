"""Per-org OCSF enforcement policy (decide + resolve).

The ``decide`` matrix is the load-bearing invariant: tag_and_pass NEVER drops
security data, valid/out_of_scope always pass, only quarantine/fail_closed drop an
in-scope invalid event. ``resolve_enforcement_mode`` reads the per-org row with a
fail-safe fallback to the global default.

Imports use ``backend.app.*`` (compiled .so dual-root gotcha).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.collectors import ocsf_policy as P
from backend.app.core.config import settings
from backend.app.db import database, models
from backend.app.db.database import Base


# ── decide() — exhaustive matrix ──────────────────────────────────────────────

@pytest.mark.parametrize(
    "valid,in_scope,mode,expected",
    [
        # valid always passes, regardless of mode
        (True, True, P.MODE_TAG_AND_PASS, P.ACTION_PASS),
        (True, True, P.MODE_QUARANTINE, P.ACTION_PASS),
        (True, True, P.MODE_FAIL_CLOSED, P.ACTION_PASS),
        # out-of-scope always passes (graceful — we don't judge unvendored classes)
        (False, False, P.MODE_QUARANTINE, P.ACTION_PASS),
        (False, False, P.MODE_FAIL_CLOSED, P.ACTION_PASS),
        # invalid in-scope follows the mode
        (False, True, P.MODE_TAG_AND_PASS, P.ACTION_PASS),   # NEVER drops
        (False, True, P.MODE_QUARANTINE, P.ACTION_QUARANTINE),
        (False, True, P.MODE_FAIL_CLOSED, P.ACTION_DROP),
        (False, True, None, P.ACTION_PASS),                  # unset → safe pass
        (False, True, "garbage", P.ACTION_PASS),             # unknown → safe pass
    ],
)
def test_decide_matrix(valid, in_scope, mode, expected) -> None:
    assert P.decide(valid=valid, in_scope=in_scope, mode=mode) == expected


def test_tag_and_pass_never_drops_any_invalid() -> None:
    """The core safety invariant: in tag_and_pass, no invalid event is ever dropped."""
    for in_scope in (True, False):
        assert P.decide(valid=False, in_scope=in_scope, mode=P.MODE_TAG_AND_PASS) == P.ACTION_PASS


def test_enforcement_modes_match_config_validator() -> None:
    """ocsf_policy.ENFORCEMENT_MODES must equal the set config.py validates
    OCSF_DEFAULT_ENFORCEMENT against (kept in sync by hand → guarded here)."""
    from backend.app.core.config import Settings

    # a valid mode is accepted; an invalid one fails fast at construction
    for mode in P.ENFORCEMENT_MODES:
        assert Settings(OCSF_DEFAULT_ENFORCEMENT=mode).OCSF_DEFAULT_ENFORCEMENT == mode
    with pytest.raises(Exception):
        Settings(OCSF_DEFAULT_ENFORCEMENT="not_a_mode")


# ── resolve_enforcement_mode() ────────────────────────────────────────────────

@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    original = database.SessionLocal
    database.SessionLocal = Session  # type: ignore[assignment]
    yield Session
    database.SessionLocal = original  # type: ignore[assignment]
    Base.metadata.drop_all(bind=engine)


def _make_org(Session, name: str = "Org") -> int:
    with Session() as db:
        org = models.Organization(name=name, slug=name.lower().replace(" ", "-"))
        db.add(org)
        db.commit()
        db.refresh(org)
        return org.id


def test_resolve_none_org_returns_global_default(db_session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "OCSF_DEFAULT_ENFORCEMENT", "quarantine")
    assert P.resolve_enforcement_mode(None) == "quarantine"


def test_resolve_org_without_policy_returns_global_default(db_session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "OCSF_DEFAULT_ENFORCEMENT", "tag_and_pass")
    org_id = _make_org(db_session)
    assert P.resolve_enforcement_mode(org_id) == "tag_and_pass"


def test_resolve_org_with_policy_row_wins_over_global(db_session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "OCSF_DEFAULT_ENFORCEMENT", "tag_and_pass")
    org_id = _make_org(db_session)
    with db_session() as db:
        db.add(models.OrganizationOcsfPolicy(organization_id=org_id, enforcement_mode="fail_closed"))
        db.commit()
    assert P.resolve_enforcement_mode(org_id) == "fail_closed"


def test_resolve_invalid_row_mode_falls_back_to_global(db_session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "OCSF_DEFAULT_ENFORCEMENT", "quarantine")
    org_id = _make_org(db_session)
    with db_session() as db:
        db.add(models.OrganizationOcsfPolicy(organization_id=org_id, enforcement_mode="corrupt"))
        db.commit()
    assert P.resolve_enforcement_mode(org_id) == "quarantine"


def test_resolve_is_failsafe_on_bad_global(monkeypatch) -> None:
    # even a corrupt global default must resolve to a safe mode, never crash the hook
    monkeypatch.setattr(settings, "OCSF_DEFAULT_ENFORCEMENT", "corrupt")
    assert P.resolve_enforcement_mode(None) == P.MODE_TAG_AND_PASS
