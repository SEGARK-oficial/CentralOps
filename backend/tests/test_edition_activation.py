"""Tests for the EE runtime activation seam (open-core).

Exercises edition.activate_enterprise WITHOUT importing main.py: a fake
``centralops_ee`` module is injected into sys.modules to prove the discovery hook,
and its absence proves the Community no-op. Mirrors how main.py wires it after the
core routers.
"""
from __future__ import annotations

import sys
import types

import pytest

from backend.app.core import edition


def test_returns_false_when_ee_absent(monkeypatch):
    # Ensure no (real or leaked-fake) centralops_ee is importable.
    monkeypatch.delitem(sys.modules, "centralops_ee", raising=False)
    assert edition.activate_enterprise(object()) is False


def test_calls_ee_activate_when_present(monkeypatch):
    captured = {}
    fake = types.ModuleType("centralops_ee")
    fake.activate = lambda app: captured.setdefault("app", app)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "centralops_ee", fake)

    app = object()
    assert edition.activate_enterprise(app) is True
    assert captured["app"] is app


def test_propagates_error_from_ee_activate(monkeypatch):
    # A present-but-broken EE must fail loud (not silently degrade to Community).
    fake = types.ModuleType("centralops_ee")

    def _boom(app):
        raise RuntimeError("ee misconfigured")

    fake.activate = _boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "centralops_ee", fake)

    with pytest.raises(RuntimeError, match="ee misconfigured"):
        edition.activate_enterprise(object())
