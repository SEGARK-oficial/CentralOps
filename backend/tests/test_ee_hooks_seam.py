"""Seam tests: the EE scope-resolver extension point.

Proves the dependency-inversion seam WITHOUT DB fixtures: registration mechanics,
the conflict/idempotency contract, the test-reset hook, and that
``core.tenant.accessible_org_ids`` routes a scoped user through a registered
resolver (the EE override) while (a) preserving the in-Core default when none is
registered and (b) keeping the global-scope short-circuit edition-independent.

The behavior-equivalence of the *default* resolver to the pre-seam subtree logic is
covered by the existing suite (test_accessible_org_ids_h1 / _subtree_*),
which must remain green after this change.
"""
from __future__ import annotations

import types

import pytest

from backend.app.core import ee_hooks, tenant


def _user(role="viewer", is_global=False, organization_id=1, user_id=1):
    """Lightweight stand-in for models.AppUser (no DB). has_global_scope only reads
    role / is_global / organization_id; accessible_org_ids also reads id."""
    return types.SimpleNamespace(
        role=role, is_global=is_global, organization_id=organization_id, id=user_id
    )


@pytest.fixture(autouse=True)
def _reset_hooks():
    ee_hooks.reset_scope_resolver()
    ee_hooks.reset_quota_guard()
    yield
    ee_hooks.reset_scope_resolver()
    ee_hooks.reset_quota_guard()


# ── registry mechanics ────────────────────────────────────────────────────────

def test_unregistered_by_default():
    assert ee_hooks.get_scope_resolver() is None


def test_register_then_get_returns_same_callable():
    fn = lambda u, s: {7}  # noqa: E731
    ee_hooks.register_scope_resolver(fn)
    assert ee_hooks.get_scope_resolver() is fn


def test_reregister_same_callable_is_idempotent():
    fn = lambda u, s: {7}  # noqa: E731
    ee_hooks.register_scope_resolver(fn)
    ee_hooks.register_scope_resolver(fn)  # must NOT raise
    assert ee_hooks.get_scope_resolver() is fn


def test_conflicting_reregister_fails_loud():
    ee_hooks.register_scope_resolver(lambda u, s: {1})  # noqa: E731
    with pytest.raises(RuntimeError):
        ee_hooks.register_scope_resolver(lambda u, s: {2})  # noqa: E731


def test_reset_clears_registration():
    ee_hooks.register_scope_resolver(lambda u, s: {1})  # noqa: E731
    ee_hooks.reset_scope_resolver()
    assert ee_hooks.get_scope_resolver() is None


# ── tenant.accessible_org_ids routes through the seam ──────────────────────────

def test_registered_resolver_is_used_for_scoped_user():
    sentinel = {999}
    ee_hooks.register_scope_resolver(lambda u, s: sentinel)  # noqa: E731
    # scoped (non-global) user -> resolver consulted; the fake ignores the session
    assert tenant.accessible_org_ids(_user(), session=None) == sentinel


def test_global_user_short_circuits_before_resolver():
    calls = {"n": 0}

    def _resolver(u, s):
        calls["n"] += 1
        return {1}

    ee_hooks.register_scope_resolver(_resolver)
    admin = _user(role="admin", is_global=True, organization_id=None)
    assert tenant.accessible_org_ids(admin, session=None) is None
    assert calls["n"] == 0  # global short-circuits; resolver never called


def test_default_resolver_used_when_none_registered():
    # No EE registered -> Core default. A scoped user with no id and no org resolves
    # to the empty set WITHOUT touching OrgClosure (org=None branch), so no DB needed.
    u = _user(organization_id=None, user_id=None)
    assert tenant.accessible_org_ids(u, session=None) == set()


# ── quota guard slot ─────────────────────────────────────────────────
# The dispatch (guard or hierarchy.child_quota_exceeded) lives at the
# materialize_child call-site; the default (no-guard) path is covered by the
# existing test_partner_program_h3a suite staying green. Here we cover the registry
# mechanics + that the dispatcher form selects a registered guard over the default.

def test_quota_guard_unregistered_by_default():
    assert ee_hooks.get_quota_guard() is None


def test_quota_guard_register_then_get():
    fn = lambda db, org_id: True  # noqa: E731
    ee_hooks.register_quota_guard(fn)
    assert ee_hooks.get_quota_guard() is fn


def test_quota_guard_reregister_same_is_idempotent():
    fn = lambda db, org_id: True  # noqa: E731
    ee_hooks.register_quota_guard(fn)
    ee_hooks.register_quota_guard(fn)  # must NOT raise
    assert ee_hooks.get_quota_guard() is fn


def test_quota_guard_conflicting_reregister_fails_loud():
    ee_hooks.register_quota_guard(lambda db, org_id: True)  # noqa: E731
    with pytest.raises(RuntimeError):
        ee_hooks.register_quota_guard(lambda db, org_id: False)  # noqa: E731


def test_quota_guard_reset_clears():
    ee_hooks.register_quota_guard(lambda db, org_id: True)  # noqa: E731
    ee_hooks.reset_quota_guard()
    assert ee_hooks.get_quota_guard() is None


def test_dispatcher_form_prefers_registered_guard_over_default():
    # Mirrors the materialize_child call-site: (guard or default)(db, org_id).
    # A registered guard must be chosen and the default must NOT be evaluated.
    default_calls = {"n": 0}

    def _default(db, org_id):
        default_calls["n"] += 1
        return False

    ee_hooks.register_quota_guard(lambda db, org_id: True)  # noqa: E731
    quota_check = ee_hooks.get_quota_guard() or _default
    assert quota_check(None, 1) is True
    assert default_calls["n"] == 0  # registered guard wins; default not called


def test_dispatcher_form_falls_back_to_default_when_unregistered():
    default_calls = {"n": 0}

    def _default(db, org_id):
        default_calls["n"] += 1
        return False

    quota_check = ee_hooks.get_quota_guard() or _default  # none registered
    assert quota_check(None, 1) is False
    assert default_calls["n"] == 1  # default used
