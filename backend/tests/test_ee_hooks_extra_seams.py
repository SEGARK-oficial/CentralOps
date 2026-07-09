"""Seam tests: the EE Celery extension points.

Two collection slots let the Enterprise edition extend the Celery worker/beat
WITHOUT the Core importing the EE and without the worker calling ``activate()``:

  * ``register_extra_task_modules`` -> ``celery_app._build_include()`` appends them
  * ``register_beat_entries``       -> ``beat_schedule.build_schedule()`` merges them

Covers the registry mechanics (idempotency / fail-loud conflict / input validation /
reset) and that the two Core consumers actually pick up what was registered — while
staying empty/behavior-preserving in Community when nothing is registered.
"""
from __future__ import annotations

import pytest

from backend.app.collectors import beat_schedule, celery_app
from backend.app.core import ee_hooks


@pytest.fixture(autouse=True)
def _reset_extra_seams():
    ee_hooks.reset_extra_task_modules()
    ee_hooks.reset_beat_entries()
    yield
    ee_hooks.reset_extra_task_modules()
    ee_hooks.reset_beat_entries()


# ── extra task modules: registry mechanics ──────────────────────────────────────

def test_extra_task_modules_empty_by_default():
    assert ee_hooks.get_extra_task_modules() == ()


def test_register_then_get_returns_tuple():
    ee_hooks.register_extra_task_modules(["centralops_ee.tasks.partner_sync_tasks"])
    assert ee_hooks.get_extra_task_modules() == (
        "centralops_ee.tasks.partner_sync_tasks",
    )


def test_reregister_equal_modules_is_idempotent():
    mods = ["a.b", "c.d"]
    ee_hooks.register_extra_task_modules(mods)
    ee_hooks.register_extra_task_modules(list(mods))  # equal value -> no raise
    assert ee_hooks.get_extra_task_modules() == ("a.b", "c.d")


def test_conflicting_task_modules_reregister_fails_loud():
    ee_hooks.register_extra_task_modules(["a.b"])
    with pytest.raises(RuntimeError):
        ee_hooks.register_extra_task_modules(["a.b", "e.f"])


def test_non_string_task_module_rejected():
    with pytest.raises(TypeError):
        ee_hooks.register_extra_task_modules(["ok", 123])  # type: ignore[list-item]


def test_empty_string_task_module_rejected():
    with pytest.raises(TypeError):
        ee_hooks.register_extra_task_modules(["ok", ""])


def test_reset_clears_task_modules():
    ee_hooks.register_extra_task_modules(["a.b"])
    ee_hooks.reset_extra_task_modules()
    assert ee_hooks.get_extra_task_modules() == ()


# ── extra task modules: celery_app consumer ─────────────────────────────────────

def test_build_include_is_core_only_when_unregistered():
    # No EE registered -> include is exactly the Core modules (behavior-preserving).
    assert celery_app._build_include() == list(celery_app._MODULES_WITH_TASKS)


def test_build_include_appends_registered_modules():
    ee_hooks.register_extra_task_modules(["centralops_ee.tasks.partner_sync_tasks"])
    include = celery_app._build_include()
    # Core modules preserved, EE module appended at the end.
    assert include[: len(celery_app._MODULES_WITH_TASKS)] == list(
        celery_app._MODULES_WITH_TASKS
    )
    assert include[-1] == "centralops_ee.tasks.partner_sync_tasks"


# ── beat entries: registry mechanics ────────────────────────────────────────────

def test_beat_entries_empty_by_default():
    assert ee_hooks.get_beat_entries() == {}


def test_register_then_get_returns_copy():
    entry = {"sophos-partner-sync": {"task": "centralops_ee.tasks.x", "schedule": 60}}
    ee_hooks.register_beat_entries(entry)
    got = ee_hooks.get_beat_entries()
    assert got == entry
    # Mutating the returned dict must NOT corrupt the registry.
    got["injected"] = {"task": "evil"}
    assert "injected" not in ee_hooks.get_beat_entries()


def test_reregister_equal_beat_entries_is_idempotent():
    entry = {"x": {"task": "t", "schedule": 60}}
    ee_hooks.register_beat_entries(entry)
    ee_hooks.register_beat_entries(dict(entry))  # equal -> no raise
    assert ee_hooks.get_beat_entries() == entry


def test_conflicting_beat_entries_reregister_fails_loud():
    ee_hooks.register_beat_entries({"x": {"task": "t1"}})
    with pytest.raises(RuntimeError):
        ee_hooks.register_beat_entries({"x": {"task": "t2"}})


def test_non_mapping_beat_entries_rejected():
    with pytest.raises(TypeError):
        ee_hooks.register_beat_entries([("x", {})])  # type: ignore[arg-type]


def test_non_string_key_beat_entries_rejected():
    with pytest.raises(TypeError):
        ee_hooks.register_beat_entries({1: {"task": "t"}})  # type: ignore[dict-item]


def test_reset_clears_beat_entries():
    ee_hooks.register_beat_entries({"x": {"task": "t"}})
    ee_hooks.reset_beat_entries()
    assert ee_hooks.get_beat_entries() == {}


# ── beat entries: beat_schedule consumer ────────────────────────────────────────

def test_build_schedule_unchanged_when_unregistered():
    # No EE registered -> the EE-only entry name is absent; static entries present.
    schedule = beat_schedule.build_schedule()
    assert "scheduler-tick" in schedule  # a Core static entry
    assert "ee-only-probe" not in schedule


def test_build_schedule_merges_registered_entries():
    ee_hooks.register_beat_entries(
        {"ee-only-probe": {"task": "centralops_ee.tasks.probe", "schedule": 900}}
    )
    schedule = beat_schedule.build_schedule()
    # EE entry merged in AND the Core static entries are still there.
    assert schedule["ee-only-probe"]["task"] == "centralops_ee.tasks.probe"
    assert "scheduler-tick" in schedule
