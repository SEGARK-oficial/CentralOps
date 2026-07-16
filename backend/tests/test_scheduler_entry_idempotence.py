"""Registro de entries RedBeat é idempotente DE VERDADE (não re-salva idênticas).

Regressão do incidente jul/2026: ``entry.save()`` numa entry existente recalcula
o score do zset — para entries que nunca rodaram (sem meta), reagenda para
``now + intervalo``. Como o boot-sync roda a cada (re)start do Beat, re-salvar
sempre virava INANIÇÃO sob restart-loop: streams com intervalo maior que o
uptime do Beat (sophos cases 3min / detections 5min) eram empurradas
eternamente, enquanto alerts (1min) disparava. Cobre ``_existing_entry_matches``
e o skip do save em ``_register_integration_in_beat_unsafe``.
"""

from __future__ import annotations

import sys
import types
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.collectors import scheduler
from backend.app.db import models
from backend.app.db.database import Base

_INTERVAL = timedelta(minutes=3)
_REG = SimpleNamespace(
    beat_key="sophos-cases",
    stream="cases",
    task_name="collectors.collect_vendor_logs_priority",
    queue="collect.priority",
    schedule=_INTERVAL,
)
_EXPIRES = max(30, int(_INTERVAL.total_seconds()) - 5)  # 175


class _FakeExisting:
    """Entry decodificada do Redis, com a forma que o matcher inspeciona."""

    def __init__(self, *, task=None, run_every=_INTERVAL, args=None, options=None, schedule=None):
        self.task = task if task is not None else _REG.task_name
        self.schedule = schedule if schedule is not None else SimpleNamespace(run_every=run_every)
        self.args = args if args is not None else [7, "cases"]
        self.options = options if options is not None else {"queue": _REG.queue, "expires": _EXPIRES}


def _stub_redbeat(monkeypatch, from_key_impl):
    """Injeta um módulo ``redbeat`` fake (independente de o pacote estar instalado)."""
    mod = types.ModuleType("redbeat")

    class RedBeatSchedulerEntry:
        saved = []

        def __init__(self, **kwargs):
            self._kwargs = kwargs

        def save(self):
            RedBeatSchedulerEntry.saved.append(self._kwargs)

        from_key = staticmethod(from_key_impl)

    mod.RedBeatSchedulerEntry = RedBeatSchedulerEntry
    monkeypatch.setitem(sys.modules, "redbeat", mod)
    return RedBeatSchedulerEntry


# ── matcher ─────────────────────────────────────────────────────────────


def test_matcher_false_when_entry_absent(monkeypatch):
    def from_key(key, app=None):
        raise KeyError(key)

    _stub_redbeat(monkeypatch, from_key)
    assert scheduler._existing_entry_matches("k", _REG, 7, _EXPIRES, None) is False


def test_matcher_false_on_read_error(monkeypatch):
    def from_key(key, app=None):
        raise RuntimeError("corrompida")

    _stub_redbeat(monkeypatch, from_key)
    assert scheduler._existing_entry_matches("k", _REG, 7, _EXPIRES, None) is False


def test_matcher_true_when_identical(monkeypatch):
    _stub_redbeat(monkeypatch, lambda key, app=None: _FakeExisting())
    assert scheduler._existing_entry_matches("k", _REG, 7, _EXPIRES, None) is True


@pytest.mark.parametrize(
    "existing",
    [
        _FakeExisting(task="outra.task"),
        _FakeExisting(run_every=timedelta(minutes=5)),          # schedule mudou
        _FakeExisting(args=[7, "alerts"]),                      # stream de outra entry
        _FakeExisting(args=[8, "cases"]),                       # outra integração
        _FakeExisting(options={"queue": "collect.bulk", "expires": _EXPIRES}),
        _FakeExisting(options={"queue": _REG.queue, "expires": 999}),
        _FakeExisting(schedule=SimpleNamespace()),              # crontab: sem run_every
    ],
)
def test_matcher_false_when_definition_differs(monkeypatch, existing):
    _stub_redbeat(monkeypatch, lambda key, app=None: existing)
    assert scheduler._existing_entry_matches("k", _REG, 7, _EXPIRES, None) is False


# ── loop de registro ────────────────────────────────────────────────────


@pytest.fixture()
def db_session_local(monkeypatch):
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(eng)
    factory = sessionmaker(bind=eng)
    monkeypatch.setattr(scheduler.database, "SessionLocal", factory)
    s = factory()
    org = models.Organization(name="Acme", slug="acme")
    s.add(org)
    s.commit()
    s.refresh(org)
    integ = models.Integration(
        name="i", organization_id=org.id, kind="tenant", platform="sophos", is_active=True
    )
    s.add(integ)
    s.commit()
    s.refresh(integ)
    yield s, integ
    s.close()
    eng.dispose()


def test_register_skips_save_when_entry_identical(monkeypatch, db_session_local):
    _, integ = db_session_local
    reg = SimpleNamespace(**{**vars(_REG)})
    monkeypatch.setattr(scheduler, "iter_for_platform", lambda platform: [reg])

    existing = _FakeExisting(args=[integ.id, "cases"])
    entry_cls = _stub_redbeat(monkeypatch, lambda key, app=None: existing)
    entry_cls.saved = []

    scheduler._register_integration_in_beat_unsafe(integ.id)
    assert entry_cls.saved == [], "entry idêntica não deve ser re-salva (preserva agenda/meta)"


def test_register_saves_when_entry_absent(monkeypatch, db_session_local):
    _, integ = db_session_local
    reg = SimpleNamespace(**{**vars(_REG)})
    monkeypatch.setattr(scheduler, "iter_for_platform", lambda platform: [reg])

    def from_key(key, app=None):
        raise KeyError(key)

    entry_cls = _stub_redbeat(monkeypatch, from_key)
    entry_cls.saved = []

    scheduler._register_integration_in_beat_unsafe(integ.id)
    assert len(entry_cls.saved) == 1
    saved = entry_cls.saved[0]
    assert saved["args"] == (integ.id, "cases")
    assert saved["options"] == {"queue": _REG.queue, "expires": _EXPIRES}


def test_register_saves_when_definition_changed(monkeypatch, db_session_local):
    _, integ = db_session_local
    reg = SimpleNamespace(**{**vars(_REG)})
    monkeypatch.setattr(scheduler, "iter_for_platform", lambda platform: [reg])

    existing = _FakeExisting(args=[integ.id, "cases"], run_every=timedelta(minutes=10))
    entry_cls = _stub_redbeat(monkeypatch, lambda key, app=None: existing)
    entry_cls.saved = []

    scheduler._register_integration_in_beat_unsafe(integ.id)
    assert len(entry_cls.saved) == 1, "definição mudou → re-salvar"
