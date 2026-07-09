"""Admin-de-org escopado + anti-escalonamento.

Community é FLAT: o admin escopado delega só na PRÓPRIA org; a
delegação por subárvore é feature Enterprise.
Cobre:
  - ``has_global_scope``: admin + org + is_global=False → ESCOPADO; admin sem org
    ou is_global=True → global;
  - ``require_global_scope``: ação de plataforma bloqueia admin escopado;
  - ``enforce_admin_delegation_scope``: admin escopado não concede global, não cria
    usuário sem org, e só delega DENTRO da própria subárvore; admin global bypassa.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core import tenant
from backend.app.db import hierarchy, models
from backend.app.db.database import Base


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with Session() as s:
        yield s


def _org(session, name, **kw):
    o = models.Organization(name=name, slug=name.lower().replace(" ", "-"), **kw)
    session.add(o)
    session.flush()
    return o


def _user(**kw) -> models.AppUser:
    return models.AppUser(**kw)


@pytest.fixture()
def tree(session):
    r = _org(session, "Reseller")
    hierarchy.materialize_node(session, r.id, None)
    c1 = _org(session, "Client 1")
    hierarchy.materialize_node(session, c1.id, r.id)
    s = _org(session, "Sibling")
    hierarchy.materialize_node(session, s.id, None)
    session.flush()
    return r, c1, s


def _raises_403(fn):
    with pytest.raises(HTTPException) as exc:
        fn()
    assert exc.value.status_code == 403


# ── has_global_scope ──────────────────────────────────────────────────────────

def test_admin_scope_depends_on_org_and_is_global():
    assert tenant.has_global_scope(
        _user(role="admin", is_global=False, organization_id=5)
    ) is False  # admin-de-org ESCOPADO
    assert tenant.has_global_scope(
        _user(role="admin", is_global=False, organization_id=None)
    ) is True  # admin sem org = global de plataforma
    assert tenant.has_global_scope(
        _user(role="admin", is_global=True, organization_id=5)
    ) is True  # is_global explícito


# ── require_global_scope ──────────────────────────────────────────────────────

def test_require_global_scope_blocks_scoped_admin():
    _raises_403(lambda: tenant.require_global_scope(
        _user(role="admin", is_global=False, organization_id=5)
    ))
    tenant.require_global_scope(_user(role="admin", is_global=False, organization_id=None))


# ── enforce_admin_delegation_scope ────────────────────────────────────────────

def test_global_admin_bypasses_delegation(tree, session):
    g = _user(id=1, role="admin", is_global=False, organization_id=None)  # global
    tenant.enforce_admin_delegation_scope(
        g, target_org_id=None, target_is_global=True, session=session
    )


def test_scoped_admin_cannot_grant_global(tree, session):
    r, c1, s = tree
    actor = _user(id=2, role="admin", is_global=False, organization_id=r.id)
    _raises_403(lambda: tenant.enforce_admin_delegation_scope(
        actor, target_org_id=c1.id, target_is_global=True, session=session
    ))


def test_scoped_admin_cannot_create_orgless_user(tree, session):
    r, c1, s = tree
    actor = _user(id=3, role="admin", is_global=False, organization_id=r.id)
    _raises_403(lambda: tenant.enforce_admin_delegation_scope(
        actor, target_org_id=None, target_is_global=False, session=session
    ))


def test_scoped_admin_delegates_within_own_org_flat(tree, session):
    """Community (FLAT): a scoped admin delegates within its OWN org. Reaching
    a CHILD org (subtree delegation) is an Enterprise feature — denied in Community
    (the EE registers the subtree resolver that would allow it)."""
    r, c1, s = tree
    actor = _user(id=4, role="admin", is_global=False, organization_id=r.id)
    # Own org: allowed.
    tenant.enforce_admin_delegation_scope(
        actor, target_org_id=r.id, target_is_global=False, session=session
    )
    # Child org (subtree): denied in Community — Enterprise-only.
    _raises_403(lambda: tenant.enforce_admin_delegation_scope(
        actor, target_org_id=c1.id, target_is_global=False, session=session
    ))


def test_scoped_admin_cannot_delegate_outside_subtree(tree, session):
    r, c1, s = tree
    actor = _user(id=5, role="admin", is_global=False, organization_id=r.id)
    _raises_403(lambda: tenant.enforce_admin_delegation_scope(
        actor, target_org_id=s.id, target_is_global=False, session=session
    ))
