"""Ganchos de fim de ciclo: a fonte só esquece o que entregou DEPOIS do cursor gravado.

``on_cycle_committed`` é onde uma fonte de fila (Pub/Sub) dá o ack. Se o
pipeline o chamasse antes de gravar o cursor — ou num ciclo que falhou —, a fila
apagaria eventos que o CentralOps ainda podia perder. ``on_cycle_aborted`` é a
contraparte (nack) e nunca pode mascarar o erro original.

Mesmo método de ``test_adr0015_claim_conservation_behavior.py``: o ciclo REAL
(``pipeline._run_collection_once``) roda contra dublês, e o que se afirma é a
ORDEM de eventos numa fita — nada é lido do ``.py``.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import asyncio
import contextlib
from typing import Any, List, Optional, Tuple

import pytest

from backend.app.collectors import pipeline
from backend.app.collectors.state import dedupe


class _Tape:
    def __init__(self) -> None:
        self.events: List[Tuple[str, Any]] = []

    def names(self) -> List[str]:
        return [name for name, _ in self.events]


class _FakeRedis:
    async def aclose(self) -> None:
        return None


class _FakeIntegration:
    id = 42
    is_active = True
    kind = "tenant"
    platform = "fakequeue"
    organization_id = 7
    organization = None
    data_geography = None


class _FakeDb:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def scalar(self, *a, **k):
        return _FakeIntegration()

    def expunge(self, *a, **k):
        return None


class _FakeConfig:
    collector_batch_size = 100
    collector_batch_flush_seconds = 1e9
    effective_dedupe_ttl_seconds = 60
    rate_limits_by_vendor: dict = {}
    domain_concurrency_limits: dict = {}


class _FakeApplied:
    output: dict = {}
    reduced_raw = None
    consumed_paths: tuple = ()
    ingest_fallback_targets: tuple = ()


class _FakeEngine:
    def apply(self, *a, **k):
        return _FakeApplied()


class _FakeRegistration:
    refresh_fn = None
    collector_cls: Any = None


def _make_collector_cls(tape: _Tape, *, boom_in_collect=False, boom_in_commit=False, boom_in_abort=False):
    class _QueueCollector:
        event_type = "fake.event"

        def __init__(self, ctx):
            self.ctx = ctx

        async def collect(self):
            yield {"id": "m0"}
            if boom_in_collect:
                raise RuntimeError("fonte caiu no meio do ciclo")
            yield {"id": "m1"}

        def extract_message_id(self, raw):
            return raw["id"]

        @staticmethod
        def watermark_at(cursor):
            return None

        async def on_cycle_committed(self):
            tape.events.append(("committed", None))
            if boom_in_commit:
                raise RuntimeError("ack falhou")

        async def on_cycle_aborted(self):
            tape.events.append(("aborted", None))
            if boom_in_abort:
                raise RuntimeError("nack falhou")

    return _QueueCollector


@pytest.fixture()
def harness(monkeypatch):
    tape = _Tape()

    class _CursorStore:
        def __init__(self, *a, **k):
            pass

        async def load(self, *a, **k):
            return {}

        async def save(self, integration_id, stream, cursor, *, error=None, **kw):
            tape.events.append(("save_error" if error else "save_ok", error))

    async def _claim(redis, integration_id, msg_id, **kw):
        return True

    async def _release_many(redis, integration_id, ids):
        return 0

    def _dispatch(batch, routes=None, **kw):
        tape.events.append(("dispatch", len(batch)))

    monkeypatch.setattr("backend.app.collectors.celery_app.get_worker_redis", lambda: _FakeRedis())
    monkeypatch.setattr(pipeline.database, "SessionLocal", lambda: _FakeDb())
    monkeypatch.setattr(pipeline, "_load_routes_for_org", lambda oid: [])
    monkeypatch.setattr(
        "backend.app.collectors.inflight.runtime.load_inflight_rules_for_org",
        lambda oid: pipeline._EMPTY_RULESET,
    )
    monkeypatch.setattr(pipeline, "registry_has", lambda p, s: True)
    monkeypatch.setattr(pipeline, "_headers_for", lambda *a, **k: {})
    monkeypatch.setattr(pipeline, "_load_collection_filters", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "get_collector_config", _acoro(_FakeConfig()))
    monkeypatch.setattr(pipeline, "get_or_refresh_token", _acoro("tok"))
    monkeypatch.setattr(pipeline, "_load_current_mapping", lambda *a, **k: (1, [], "v2"))
    monkeypatch.setattr(pipeline, "CursorStore", _CursorStore)
    monkeypatch.setattr(pipeline, "RedisRateLimiter", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "DomainLimiter", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_aiohttp_session", _null_session)
    monkeypatch.setattr(pipeline, "default_engine", _FakeEngine())
    monkeypatch.setattr(
        pipeline, "build_envelope",
        lambda raw, out, ctx, vendor_msg_id=None: {
            "_centralops": {"event_id": vendor_msg_id, "organization_id": 7, "customer_id": 7}
        },
    )
    monkeypatch.setattr(pipeline, "likely_no_session", lambda oid: True)
    monkeypatch.setattr(pipeline, "_record_source_ingested", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.drift, "should_capture", lambda *a, **k: False)
    monkeypatch.setattr(pipeline.sample_reservoir, "push", lambda *a, **k: _noop_coro())
    monkeypatch.setattr(pipeline, "claim", _claim)
    monkeypatch.setattr(dedupe, "release_many", _release_many)
    monkeypatch.setattr(pipeline, "_enqueue_dispatch", _dispatch)

    def run(**collector_kw) -> Optional[BaseException]:
        reg = _FakeRegistration()
        reg.collector_cls = _make_collector_cls(tape, **collector_kw)
        monkeypatch.setattr(pipeline, "registry_get", lambda p, s: reg)
        try:
            asyncio.run(pipeline._run_collection_once(integration_id=42, stream="events"))
            return None
        except Exception as exc:  # noqa: BLE001 — o teste inspeciona a fita
            return exc

    return tape, run


def _acoro(value):
    async def _f(*a, **k):
        return value
    return _f


async def _noop_coro():
    return None


@contextlib.asynccontextmanager
async def _null_session():
    yield None


def test_commit_hook_runs_once_after_dispatch_and_cursor_save(harness):
    tape, run = harness
    assert run() is None
    names = tape.names()
    assert "dispatch" in names and "save_ok" in names and "committed" in names, names
    assert names.index("dispatch") < names.index("save_ok") < names.index("committed"), (
        "a fonte confirmou antes de o cursor estar gravado — falha entre os dois perde o evento"
    )
    assert names.count("committed") == 1 and "aborted" not in names


def test_failed_cycle_hands_back_and_never_commits(harness):
    tape, run = harness
    exc = run(boom_in_collect=True)
    assert isinstance(exc, RuntimeError) and "fonte caiu" in str(exc)
    names = tape.names()
    assert "aborted" in names and "committed" not in names, names
    assert names.index("save_error") < names.index("aborted")


def test_broken_ack_does_not_turn_a_saved_cycle_into_an_error(harness):
    """O cursor já está no banco: propagar regravaria o cursor ANTERIOR por cima."""
    tape, run = harness
    assert run(boom_in_commit=True) is None
    names = tape.names()
    assert names.count("save_ok") == 1 and "save_error" not in names, names
    assert "aborted" not in names


def test_broken_nack_never_masks_the_original_error(harness):
    tape, run = harness
    exc = run(boom_in_collect=True, boom_in_abort=True)
    assert isinstance(exc, RuntimeError) and "fonte caiu" in str(exc), exc
    assert "aborted" in tape.names()


def test_collector_without_hooks_is_untouched():
    """Os coletores de polling não declaram nada: o default do BaseCollector é no-op."""
    from backend.app.collectors.base import BaseCollector

    assert asyncio.run(BaseCollector.on_cycle_committed(object())) is None
    assert asyncio.run(BaseCollector.on_cycle_aborted(object())) is None


@pytest.mark.asyncio
async def test_notify_helpers_tolerate_a_cycle_that_died_before_the_collector():
    assert await pipeline._notify_cycle_committed(None, 1, "s") is None
    assert await pipeline._notify_cycle_aborted(None, 1, "s") is None
