"""Seam tests: the EE partner-sync dispatcher extension point.

The Sophos partner tenant-discovery task moves to ``centralops_ee`` in the carve-out;
``providers.sophos.provider.on_created`` therefore dispatches it through this seam.
Unlike the scope/quota slots there is NO Community default — unregistered means the
call-site no-ops (Community has no partner auto-discovery). Covers registration
mechanics + the dispatcher-or-no-op call pattern the consumer uses.
"""
from __future__ import annotations

import pytest

from backend.app.core import ee_hooks


@pytest.fixture(autouse=True)
def _reset():
    ee_hooks.reset_partner_sync_dispatcher()
    yield
    ee_hooks.reset_partner_sync_dispatcher()


def test_unregistered_by_default():
    assert ee_hooks.get_partner_sync_dispatcher() is None


def test_register_then_get_returns_same_callable():
    fn = lambda integration_id: None  # noqa: E731
    ee_hooks.register_partner_sync_dispatcher(fn)
    assert ee_hooks.get_partner_sync_dispatcher() is fn


def test_reregister_same_callable_is_idempotent():
    fn = lambda integration_id: None  # noqa: E731
    ee_hooks.register_partner_sync_dispatcher(fn)
    ee_hooks.register_partner_sync_dispatcher(fn)  # must NOT raise
    assert ee_hooks.get_partner_sync_dispatcher() is fn


def test_conflicting_reregister_fails_loud():
    ee_hooks.register_partner_sync_dispatcher(lambda i: None)  # noqa: E731
    with pytest.raises(RuntimeError):
        ee_hooks.register_partner_sync_dispatcher(lambda i: None)  # noqa: E731


def test_reset_clears_registration():
    ee_hooks.register_partner_sync_dispatcher(lambda i: None)  # noqa: E731
    ee_hooks.reset_partner_sync_dispatcher()
    assert ee_hooks.get_partner_sync_dispatcher() is None


def test_dispatcher_or_noop_call_pattern():
    # Mirrors provider.on_created: dispatch only when a dispatcher is registered.
    calls = []

    # Community: no dispatcher -> the guarded call is a no-op.
    dispatch = ee_hooks.get_partner_sync_dispatcher()
    if dispatch is not None:  # pragma: no cover
        dispatch(1)
    assert calls == []

    # Enterprise: a registered dispatcher receives the integration id.
    ee_hooks.register_partner_sync_dispatcher(lambda integration_id: calls.append(integration_id))
    dispatch = ee_hooks.get_partner_sync_dispatcher()
    if dispatch is not None:
        dispatch(42)
    assert calls == [42]
