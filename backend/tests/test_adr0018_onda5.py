"""ADR-0018 Onda 5 — limpeza: nenhum caminho antigo sobrevive.

O evento viaja só pelo Kafka. Saíram, e cada saída é travada aqui:

1. **A lane Celery de entrega** (``EVENT_DATAPLANE=celery``,
   ``dispatch_to_destination``/``dispatch_to_dlq`` e as filas
   ``dispatch.destination.N``), junto com a válvula E6 que media a fundura
   dessas filas. O roteamento produz sempre no tópico ``deliver``
   (``test_no_celery_dataplane``).
2. **A flag de modo de pipeline** (``PIPELINE_RAW_INTEGRATIONS``): todo ciclo de
   coleta só coleta — grava no raw e avança o cursor. Fonte push não tem ciclo.
3. **O dedupe Redis por evento** (claim com TTL, conjunto de pendentes,
   recuperação de órfãs e o TTL configurável na tela): o dedupe é a janela em
   memória da partição do raw.
4. **A lista Redis do push**: ``/api/ingest`` e o receptor syslog gravam no raw.

O backfill continua passando pelo processor, marcado como histórico no registro
do raw — sem o ciclo inline, a marca é o que impede o batimento falso da
ausência.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import asyncio
import inspect
import json
import pathlib
import re
import types

import pytest

from backend.app.collectors import pipeline
from backend.app.collectors import registry as registry_module
from backend.app.collectors.dataplane import processor as proc
from backend.app.collectors.dataplane import raw as raw_mod
from backend.app.collectors.state import dedupe
from backend.app.core.config import Settings, settings

_REPO = pathlib.Path(__file__).resolve().parents[2]
_APP = _REPO / "backend" / "app"


# ── 1. Sem lane Celery de entrega ───────────────────────────────────────────


def test_no_celery_dataplane():
    """O Celery fica no control-plane: nenhuma fila, rota ou task carrega lote de
    evento, e não há chave de configuração para religar o caminho antigo."""
    from backend.app.collectors import queues, tasks
    from backend.app.collectors.celery_app import celery_app

    queue_names = {q.name for q in celery_app.conf.task_queues}
    assert not {q for q in queue_names if q.startswith("dispatch.destination")}
    assert "dispatch.dlq" in queue_names  # reenvio da DLQ pedido pela UI
    routes = celery_app.conf.task_routes or {}
    assert "collectors.dispatch_to_destination" not in routes
    for gone in ("dispatch_to_destination", "dispatch_to_dlq"):
        assert not hasattr(tasks, gone)
        assert f"collectors.{gone}" not in celery_app.tasks
    for gone in ("Q_DISPATCH_DESTINATION", "dispatch_dest_shard_queue", "all_dispatch_dest_queues"):
        assert not hasattr(queues, gone)

    fields = Settings.model_fields
    for gone in (
        "EVENT_DATAPLANE",
        "PIPELINE_RAW_INTEGRATIONS",
        "BACKPRESSURE_E6_ENABLED",
        "DISPATCH_QUEUE_CEILING",
        "DEDUPE_TTL_DAYS",
    ):
        assert gone not in fields, gone


def test_routed_batch_always_goes_to_the_deliver_topic(monkeypatch):
    """Sem flag de transporte: o sub-lote de cada destino vira um produce no
    ``deliver``, nunca uma task Celery."""
    produced = []

    monkeypatch.setattr(
        "backend.app.collectors.dataplane.produce_delivery",
        lambda dest, events, tp=None: produced.append((dest, [e["_centralops"]["event_id"] for e in events])),
    )
    monkeypatch.setattr(pipeline, "_load_destination_residency", lambda *a, **k: {})
    monkeypatch.setattr(pipeline, "_load_wazuh_loop_destination_ids", lambda *a, **k: frozenset())
    monkeypatch.setattr(pipeline, "_load_fallback_destination_id", lambda *a, **k: "dest-1")

    batch = [
        {"_centralops": {"event_id": f"e{i}", "organization_id": 7, "vendor": "x"}, "normalized": {}}
        for i in range(3)
    ]
    pipeline._enqueue_routed(batch, [])
    assert produced == [("dest-1", ["e0", "e1", "e2"])]


def test_retired_delivery_keys_are_ignored_not_rejected():
    """``backpressure``/``queue_ceiling`` mediam a fila Celery que saiu. Linha antiga
    com elas não vira 422 nem perde o resto da política."""
    from backend.app.collectors.output.delivery_config import DeliveryConfig, parse_delivery

    cfg = parse_delivery(
        "syslog_rfc3164",
        {"backpressure": "drop_newest", "queue_ceiling": 5, "ordered": True, "timeout_ms": 1234},
    )
    assert cfg.ordered is True and cfg.timeout_ms == 1234
    assert "backpressure" not in DeliveryConfig.model_fields
    assert "queue_ceiling" not in DeliveryConfig.model_fields


# ── 2. Coleta só coleta, sempre ─────────────────────────────────────────────


class _FakeRedis:
    async def aclose(self) -> None:
        return None


class _FakeIntegration:
    id = 42
    is_active = True
    kind = "tenant"
    platform = "fakevendor"
    organization_id = 7
    organization = None
    data_geography = None


class _FakeDb:
    def __init__(self, integration):
        self._integration = integration

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def scalar(self, *a, **k):
        return self._integration

    def expunge(self, *a, **k):
        return None


class _FakeConfig:
    collector_batch_size = 2
    collector_batch_flush_seconds = 1e9
    rate_limits_by_vendor: dict = {}
    domain_concurrency_limits: dict = {}


@pytest.fixture()
def cycle(monkeypatch):
    """O ciclo REAL contra dublês. O estágio de processamento é proibido: se o
    ciclo o construir, o teste quebra."""
    tape: list = []

    class _Collector:
        event_type = "fake.event"

        def __init__(self, ctx):
            self.ctx = ctx

        async def collect(self):
            for i in range(3):
                yield {"id": f"m{i}"}
            self.ctx.cursor = {"pos": 3}

        def extract_message_id(self, raw):
            return raw["id"]

        @staticmethod
        def watermark_at(cursor):
            return None

        async def on_cycle_committed(self):
            tape.append(("committed", None))

        async def on_cycle_aborted(self):
            tape.append(("aborted", None))

    reg = types.SimpleNamespace(refresh_fn=None, collector_cls=_Collector)

    class _CursorStore:
        def __init__(self, *a, **k):
            pass

        async def load(self, *a, **k):
            return {"pos": 0}

        async def save(self, integration_id, stream, cursor, *, error=None, **kw):
            tape.append(("save_error" if error else "save_ok", cursor))

    async def _produce(integration_id, stream, events, ids=None, historical=False):
        tape.append(("produce", ([e["id"] for e in events], ids, historical)))
        return len(events)

    async def _token(*a, **k):
        tape.append(("token", None))
        return "tok"

    class _NoProcessor:
        def __init__(self, *a, **k):
            raise AssertionError("o ciclo de coleta construiu o estágio de processamento")

    import contextlib

    @contextlib.asynccontextmanager
    async def _null_session():
        yield None

    async def _config(*a, **k):
        return _FakeConfig()

    monkeypatch.setattr("backend.app.collectors.celery_app.get_worker_redis", lambda: _FakeRedis())
    monkeypatch.setattr(pipeline.database, "SessionLocal", lambda: _FakeDb(_FakeIntegration()))
    monkeypatch.setattr(pipeline, "registry_has", lambda p, s: True)
    monkeypatch.setattr(pipeline, "registry_get", lambda p, s: reg)
    monkeypatch.setattr(pipeline, "_headers_for", lambda *a, **k: {})
    monkeypatch.setattr(pipeline, "_load_collection_filters", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "get_collector_config", _config)
    monkeypatch.setattr(pipeline, "get_or_refresh_token", _token)
    monkeypatch.setattr(pipeline, "CursorStore", _CursorStore)
    monkeypatch.setattr(pipeline, "RedisRateLimiter", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "DomainLimiter", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_aiohttp_session", _null_session)
    monkeypatch.setattr(pipeline, "StreamProcessor", _NoProcessor)
    monkeypatch.setattr(raw_mod, "produce_raw_async", _produce)

    def run():
        asyncio.run(pipeline._run_collection_once(integration_id=42, stream="events"))

    return types.SimpleNamespace(tape=tape, run=run, monkeypatch=monkeypatch)


def test_every_collection_cycle_only_produces_raw(cycle):
    """Sem flag de modo: TODA integração grava no raw e só então avança o cursor."""
    cycle.run()
    names = [n for n, _ in cycle.tape]
    produced = [p for n, p in cycle.tape if n == "produce"]
    assert produced == [(["m0", "m1"], ["m0", "m1"], False), (["m2"], ["m2"], False)]
    assert names.index("produce") < names.index("save_ok") < names.index("committed")
    assert ("save_ok", {"pos": 3}) in cycle.tape


def test_push_source_has_no_collection_cycle(cycle):
    """Fonte push não tem o que buscar: o ciclo nem pede token, nem toca o cursor."""
    cycle.monkeypatch.setattr(pipeline, "registry_is_push", lambda platform: True)
    cycle.run()
    assert cycle.tape == []


def test_pipeline_mode_flag_is_gone():
    for gone in ("pipeline_mode_for", "PIPELINE_MODE_RAW", "PIPELINE_MODE_INLINE", "_collect_to_raw"):
        assert not hasattr(pipeline, gone), gone


def test_push_platforms_get_no_beat_entry(monkeypatch):
    """O agendador não cria entry para fonte push — e apaga a que sobrou de quando
    o ciclo drenava a lista Redis."""
    from backend.app.collectors import scheduler

    integ = types.SimpleNamespace(id=5, is_active=True, kind="tenant", platform="fortinet_fortigate")

    class _Q:
        def filter(self, *a, **k):
            return self

        def first(self):
            return integ

    class _Db:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def query(self, *a, **k):
            return _Q()

    deregistered = []
    saved = []

    class _Entry:
        def __init__(self, *a, **k):
            pass

        def save(self):
            saved.append(True)

    monkeypatch.setattr(scheduler.database, "SessionLocal", lambda: _Db())
    monkeypatch.setattr(scheduler, "_deregister_integration_from_beat_unsafe", deregistered.append)
    monkeypatch.setattr("redbeat.RedBeatSchedulerEntry", _Entry)
    scheduler._register_integration_in_beat_unsafe(5)
    assert deregistered == [5]
    assert saved == []


# ── 3. Dedupe só na janela em memória ──────────────────────────────────────


def test_no_per_event_redis_dedupe():
    for gone in ("claim", "release", "release_many", "settle_pending", "recover_orphan_claims",
                 "pending_set_key", "clamp_ttl_seconds", "DEFAULT_TTL_SECONDS"):
        assert not hasattr(dedupe, gone), gone
    # A janela é obrigatória: o estágio não tem mais o caminho "claim no Redis".
    params = inspect.signature(pipeline.StreamProcessor).parameters
    assert "pending_set" not in params
    assert params["dedupe"].default is inspect.Parameter.empty
    assert not hasattr(pipeline.StreamProcessor, "recover_orphan_claims")
    assert inspect.signature(pipeline.process_raw_events).parameters["dedupe"].kind is (
        inspect.Parameter.KEYWORD_ONLY
    )


def test_collector_config_no_longer_exposes_a_dedupe_ttl():
    from pydantic import ValidationError

    from backend.app.api import schemas
    from backend.app.collectors.config_loader import CollectorConfigSnapshot

    assert "dedupe_ttl_seconds" not in schemas.CollectorConfigRead.model_fields
    assert "dedupe_ttl_days" not in schemas.CollectorConfigRead.model_fields
    with pytest.raises(ValidationError):
        schemas.CollectorConfigUpdate(dedupe_ttl_seconds=14_400)
    assert not hasattr(CollectorConfigSnapshot, "effective_dedupe_ttl_seconds")


# ── 4. Push direto no raw ───────────────────────────────────────────────────


def test_push_has_no_redis_list():
    import importlib.util

    assert importlib.util.find_spec("backend.app.collectors.ingest_buffer") is None
    from backend.app.collectors.vendors import push_ingest

    assert not hasattr(push_ingest, "PushBufferCollector")


def test_syslog_receiver_writes_to_raw(monkeypatch):
    from backend.app.syslog import server as srv

    written = []

    async def _produce(integration_id, stream, events, ids=None, historical=False):
        written.append((integration_id, stream, len(events)))
        return len(events)

    monkeypatch.setattr(raw_mod, "produce_raw_async", _produce)
    assert "redis" not in inspect.signature(srv.Receiver).parameters
    r = srv.Receiver(sources=srv.SourceTable([]), registry_has=lambda p, s: True,
                     batch_max=10, flush_ms=10, max_line=1024)
    r._queues[(9, "traffic", "fortinet_fortigate")] = [{"a": 1}, {"a": 2}]
    assert asyncio.run(r.flush()) == 2
    assert written == [(9, "traffic", 2)]


# ── Backfill: histórico marcado no raw ──────────────────────────────────────


def test_raw_record_carries_the_historical_mark():
    live = raw_mod.decode_raw(raw_mod.encode_raw(1, "s", [{"a": 1}], ids=["x"]))
    past = raw_mod.decode_raw(raw_mod.encode_raw(1, "s", [{"a": 1}], ids=["x"], historical=True))
    assert "historical" not in live
    assert past["historical"] is True


def test_processor_keeps_historical_batches_apart_and_passes_the_mark(monkeypatch):
    """Registros consecutivos do mesmo stream só se juntam com a mesma marca: um
    lote de backfill nunca contamina o ao vivo (nem o contrário)."""
    msgs = [
        types.SimpleNamespace(offset=o, value=raw_mod.encode_raw(1, "s", [{"o": o}], ids=[str(o)], historical=h))
        for o, h in ((10, False), (11, True), (12, True), (13, False))
    ]
    groups = list(proc._group_records(msgs))
    assert [(g.first_offset, g.last_offset, g.historical) for g in groups] == [
        (10, 10, False), (11, 12, True), (13, 13, False)
    ]

    seen = []

    async def _process(iid, stream, events, *, dedupe, ids=None, historical=False):
        seen.append((iid, stream, len(events), historical))
        return True

    monkeypatch.setattr(pipeline, "process_raw_events", _process)
    asyncio.run(proc.process_group(1, "s", [{"a": 1}], 0, ids=["x"], historical=True))
    assert seen == [(1, "s", 1, True)]


def test_backfill_produces_historical_raw_records(monkeypatch):
    """O backfill só busca e grava no raw com a marca de histórico; nunca
    processa no worker de backfill."""
    from datetime import datetime, timezone

    from backend.app.collectors import backfill_tasks

    produced = []

    class _Collector:
        event_type = "fake.event"

        def __init__(self, ctx):
            self.ctx = ctx

        async def collect(self):
            yield {"id": "b1"}
            self.ctx.cursor = {"pos": 1}

        def extract_message_id(self, raw):
            return raw["id"]

    reg = types.SimpleNamespace(refresh_fn=None, collector_cls=_Collector)
    view = types.SimpleNamespace(integration=None, platform="fakevendor", organization_id=7)

    async def _produce(integration_id, stream, events, ids=None, historical=False):
        produced.append((ids, historical))
        return len(events)

    async def _config(*a, **k):
        return _FakeConfig()

    async def _token(*a, **k):
        return "tok"

    import contextlib

    @contextlib.asynccontextmanager
    async def _null_session():
        yield None

    class _NoProcessor:
        def __init__(self, *a, **k):
            raise AssertionError("o backfill construiu o estágio de processamento")

    monkeypatch.setattr("backend.app.collectors.celery_app.get_worker_redis", lambda: _FakeRedis())
    monkeypatch.setattr(pipeline, "_load_cycle_integration", lambda *a: view)
    monkeypatch.setattr(pipeline, "_registration_for", lambda *a: reg)
    monkeypatch.setattr(pipeline, "get_or_refresh_token", _token)
    monkeypatch.setattr(pipeline, "_headers_for", lambda *a, **k: {})
    monkeypatch.setattr(pipeline, "get_collector_config", _config)
    monkeypatch.setattr(pipeline, "RedisRateLimiter", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "DomainLimiter", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_aiohttp_session", _null_session)
    monkeypatch.setattr(pipeline, "_load_collection_filters", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "StreamProcessor", _NoProcessor)
    monkeypatch.setattr(raw_mod, "produce_raw_async", _produce)

    out = asyncio.run(backfill_tasks.run_backfill_collection_once(
        42, "events",
        datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 2, tzinfo=timezone.utc),
    ))
    assert produced == [(["b1"], True)]
    assert out == {"cursor": {"pos": 1}, "events_collected": 1, "events_dispatched": 1}


# ── Fonte: nada do caminho antigo fica no código nem no deploy ──────────────


_GONE_IN_CODE = re.compile(
    r"\b(EVENT_DATAPLANE|PIPELINE_RAW_INTEGRATIONS|dispatch_to_destination|dispatch_to_dlq|"
    r"ingest_buffer|PushBufferCollector|pipeline_mode_for|BACKPRESSURE_E6_ENABLED|"
    r"DISPATCH_QUEUE_CEILING|load_shedder|resolve_dispatch_plan|DEDUPE_TTL_DAYS)\b"
)


@pytest.mark.source_only  # lê o .py; na imagem Cython o fonte não existe
def test_no_old_path_left_in_the_backend_source():
    hits = []
    for path in _APP.rglob("*.py"):
        if "/tests/" in str(path):
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _GONE_IN_CODE.search(line):
                hits.append(f"{path.relative_to(_REPO)}:{n}: {line.strip()}")
    assert hits == []


@pytest.mark.source_only  # lê compose/Helm do repositório
def test_no_old_path_left_in_the_deploy():
    targets = [
        _REPO / "compose" / "docker-compose.yml",
        _REPO / "compose" / "docker-compose.ee.yml",
        _REPO / "compose" / ".env.example",
        *sorted((_REPO / "kubernetes" / "helm" / "centralops" / "templates").glob("*.yaml")),
        _REPO / "kubernetes" / "helm" / "centralops" / "values.yaml",
    ]
    pattern = re.compile(r"EVENT_DATAPLANE|eventDataplane|PIPELINE_RAW_INTEGRATIONS|rawIntegrations|"
                         r"dispatch\.destination\.[0-9]|\"dispatch\.destination\"|dispatch\.destination,")
    hits = []
    for path in targets:
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if pattern.search(line):
                hits.append(f"{path.relative_to(_REPO)}:{n}: {line.strip()}")
    assert hits == []
