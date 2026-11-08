"""ADR-0018 Onda 3a — coleta só coleta, e o processor sem ciclo de coleta.

Três decisões, cada uma travada aqui por comportamento (o ciclo e o estágio
REAIS rodam contra dublês; nada é lido do ``.py``):

1. **Coleta só coleta.** Integração em modo ``raw`` grava o evento cru no tópico
   raw com ``acks=all`` e só então avança o cursor. Não deduplica, não normaliza
   e não roteia (``test_collector_stage_only_produces_raw``). Broker fora ⇒ o
   cursor NÃO anda e a fonte não confirma.
2. **Processor sem ciclo.** O lote do raw vai direto ao ``StreamProcessor``: sem
   token, sem cursor, sem coletor instanciado. Integração inativa devolve False
   (o processor manda à DLQ, nunca commita mudo); falha solta as claims.
3. **Corte por integração.** ``PIPELINE_RAW_INTEGRATIONS`` só vale com Kafka; o
   resto segue no ciclo inline de sempre.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import asyncio
import contextlib
import json
import types
from typing import Any, List, Optional, Tuple

import pytest

from backend.app.collectors import pipeline
from backend.app.collectors import registry as registry_module
from backend.app.collectors.dataplane import processor as proc
from backend.app.collectors.dataplane import raw as raw_mod
from backend.app.collectors.state import dedupe
from backend.app.core.config import settings


# ── Dublês ───────────────────────────────────────────────────────────────────


class _Tape:
    def __init__(self) -> None:
        self.events: List[Tuple[str, Any]] = []

    def names(self) -> List[str]:
        return [name for name, _ in self.events]

    def of(self, name: str) -> List[Any]:
        return [payload for n, payload in self.events if n == name]


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
    effective_dedupe_ttl_seconds = 60
    rate_limits_by_vendor: dict = {}
    domain_concurrency_limits: dict = {}


class _FakeApplied:
    output: dict = {}
    reduced_raw = None
    consumed_paths: tuple = ()
    ingest_fallback_targets: tuple = ()


class _FakeEngine:
    def __init__(self, tape: _Tape) -> None:
        self.tape = tape

    def apply(self, *a, **k):
        self.tape.events.append(("normalize", None))
        return _FakeApplied()


class _FakeRegistration:
    refresh_fn = None
    collector_cls: Any = None


def _make_collector_cls(tape: _Tape, *, n: int = 3, instantiable: bool = True):
    class _Collector:
        event_type = "fake.event"

        def __init__(self, ctx):
            if not instantiable:
                raise AssertionError("o processor instanciou um coletor")
            tape.events.append(("collector_init", None))
            self.ctx = ctx

        async def collect(self):
            for i in range(n):
                yield {"id": f"m{i}", "n": i}
            self.ctx.cursor = {"pos": n}

        def extract_message_id(self, raw):
            return raw.get("id", "")

        @staticmethod
        def watermark_at(cursor):
            return None

        async def on_cycle_committed(self):
            tape.events.append(("committed", None))

        async def on_cycle_aborted(self):
            tape.events.append(("aborted", None))

    return _Collector


def _acoro(value):
    async def _f(*a, **k):
        return value

    return _f


async def _noop_coro():
    return None


@contextlib.asynccontextmanager
async def _null_session():
    yield None


@pytest.fixture()
def harness(monkeypatch):
    """Ciclo e estágio reais contra dublês; tudo o que importa cai na fita."""
    tape = _Tape()
    integration = _FakeIntegration()
    reg = _FakeRegistration()
    reg.collector_cls = _make_collector_cls(tape)

    class _CursorStore:
        def __init__(self, *a, **k):
            pass

        async def load(self, *a, **k):
            tape.events.append(("cursor_load", None))
            return {"pos": 0}

        async def save(self, integration_id, stream, cursor, *, error=None, **kw):
            tape.events.append(
                ("save_error" if error else "save_ok", {"cursor": cursor, "error": error, **kw})
            )

    async def _claim(redis, integration_id, msg_id, **kw):
        tape.events.append(("claim", (msg_id, kw.get("pending_set"))))
        return True

    async def _release_many(redis, integration_id, ids):
        tape.events.append(("release", sorted(ids)))
        return len(ids)

    async def _recover(redis, integration_id, pending_set):
        tape.events.append(("recover", pending_set))
        return 0

    def _dispatch(batch, routes=None, **kw):
        tape.events.append(("dispatch", [e["_centralops"]["event_id"] for e in batch]))

    async def _produce(integration_id, stream, events, ids=None):
        tape.events.append(("produce", (integration_id, stream, [e["id"] for e in events], ids)))
        return len(events)

    async def _token(*a, **k):
        tape.events.append(("token", None))
        return "tok"

    def _routes(oid):
        tape.events.append(("routes", oid))
        return []

    monkeypatch.setattr("backend.app.collectors.celery_app.get_worker_redis", lambda: _FakeRedis())
    monkeypatch.setattr(pipeline.database, "SessionLocal", lambda: _FakeDb(integration))
    monkeypatch.setattr(pipeline, "_load_routes_for_org", _routes)
    monkeypatch.setattr(
        "backend.app.collectors.inflight.runtime.load_inflight_rules_for_org",
        lambda oid: pipeline._EMPTY_RULESET,
    )
    monkeypatch.setattr(pipeline, "registry_has", lambda p, s: True)
    monkeypatch.setattr(pipeline, "registry_get", lambda p, s: reg)
    monkeypatch.setattr(pipeline, "_headers_for", lambda *a, **k: {})
    monkeypatch.setattr(pipeline, "_load_collection_filters", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "get_collector_config", _acoro(_FakeConfig()))
    monkeypatch.setattr(pipeline, "get_or_refresh_token", _token)
    monkeypatch.setattr(pipeline, "_load_current_mapping", lambda *a, **k: (1, [], "v2"))
    monkeypatch.setattr(pipeline, "CursorStore", _CursorStore)
    monkeypatch.setattr(pipeline, "RedisRateLimiter", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "DomainLimiter", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_aiohttp_session", _null_session)
    monkeypatch.setattr(pipeline, "default_engine", _FakeEngine(tape))
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
    monkeypatch.setattr(pipeline, "recover_orphan_claims", _recover)
    monkeypatch.setattr(dedupe, "release_many", _release_many)
    monkeypatch.setattr(pipeline, "_enqueue_dispatch", _dispatch)
    monkeypatch.setattr(raw_mod, "produce_raw_async", _produce)
    monkeypatch.setattr(settings, "EVENT_DATAPLANE", "kafka")
    monkeypatch.setattr(settings, "PIPELINE_RAW_INTEGRATIONS", "")

    def cycle() -> Optional[BaseException]:
        try:
            asyncio.run(pipeline._run_collection_once(integration_id=42, stream="events"))
            return None
        except Exception as exc:  # noqa: BLE001 — o teste inspeciona a fita
            return exc

    return types.SimpleNamespace(
        tape=tape, cycle=cycle, reg=reg, integration=integration, monkeypatch=monkeypatch
    )


# ── 3. Corte por integração ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("dataplane", "spec", "expected"),
    [
        ("kafka", "", "inline"),
        ("kafka", "*", "raw"),
        ("kafka", "40, 42", "raw"),
        ("kafka", "40,43", "inline"),
        ("kafka", "abc,42x", "inline"),
        # Sem broker não há tópico raw: a lista é ignorada (e avisada no log).
        ("celery", "*", "inline"),
        ("celery", "42", "inline"),
    ],
)
def test_pipeline_mode_is_per_integration_and_needs_kafka(monkeypatch, dataplane, spec, expected):
    monkeypatch.setattr(settings, "EVENT_DATAPLANE", dataplane)
    monkeypatch.setattr(settings, "PIPELINE_RAW_INTEGRATIONS", spec)
    assert pipeline.pipeline_mode_for(42) == expected


def test_inline_mode_still_processes_in_the_cycle(harness):
    """Fora da lista, nada muda: o ciclo deduplica, normaliza e despacha."""
    assert harness.cycle() is None
    names = harness.tape.names()
    assert "produce" not in names
    assert harness.tape.of("dispatch") == [["m0", "m1"], ["m2"]]
    assert names.index("dispatch") < names.index("save_ok") < names.index("committed")


# ── 1. Coleta só coleta ──────────────────────────────────────────────────────


def test_collector_stage_only_produces_raw(harness):
    harness.monkeypatch.setattr(settings, "PIPELINE_RAW_INTEGRATIONS", "42")
    assert harness.cycle() is None
    tape = harness.tape
    names = tape.names()

    # Gravou no raw, em lotes do tamanho configurado, com o id do coletor.
    assert tape.of("produce") == [
        (42, "events", ["m0", "m1"], ["m0", "m1"]),
        (42, "events", ["m2"], ["m2"]),
    ]
    # Nada de processamento no ciclo de coleta.
    for forbidden in ("claim", "normalize", "dispatch", "routes", "recover"):
        assert forbidden not in names, f"o ciclo de coleta fez {forbidden!r}"
    # O cursor só anda DEPOIS do último ack do raw, e a fonte só confirma depois.
    assert names.index("produce") < names.index("save_ok") < names.index("committed")
    assert len([n for n in names if n == "produce"]) == 2
    assert names.index("save_ok") > max(i for i, n in enumerate(names) if n == "produce")
    saved = tape.of("save_ok")[0]
    assert saved["cursor"] == {"pos": 3} and saved["events_collected"] == 3


def test_raw_collect_with_the_broker_down_keeps_the_cursor(harness):
    """Sem o ack do raw nada foi gravado: o cursor fica onde estava e a fonte de
    fila é avisada para não confirmar. O erro sobe para o retry do Celery."""
    harness.monkeypatch.setattr(settings, "PIPELINE_RAW_INTEGRATIONS", "*")

    async def _broker_down(*a, **k):
        raise ConnectionError("broker fora")

    harness.monkeypatch.setattr(raw_mod, "produce_raw_async", _broker_down)
    exc = harness.cycle()
    assert isinstance(exc, ConnectionError)
    names = harness.tape.names()
    assert "save_ok" not in names and "committed" not in names, names
    assert names.index("save_error") < names.index("aborted")
    assert harness.tape.of("save_error")[0]["cursor"] == {"pos": 0}


def test_raw_collect_skips_inactive_integration_without_touching_the_source(harness):
    harness.monkeypatch.setattr(settings, "PIPELINE_RAW_INTEGRATIONS", "*")
    harness.integration.is_active = False
    assert harness.cycle() is None
    assert harness.tape.names() == []


# ── 2. Processor sem ciclo de coleta ─────────────────────────────────────────


def _process(events, *, ids=None, scope="raw-p3"):
    return asyncio.run(pipeline.process_raw_events(42, "events", events, ids=ids, scope=scope))


def test_processor_runs_the_stage_without_a_collection_cycle(harness):
    harness.reg.collector_cls = _make_collector_cls(harness.tape, instantiable=False)
    events = [{"id": "p0"}, {"id": "p1"}, {"id": "p2"}]
    # ids gravados pelo coletor pull vencem; "" cai no extract_message_id da classe.
    assert _process(events, ids=["c0", "", "c2"]) is True

    tape = harness.tape
    names = tape.names()
    for forbidden in ("token", "cursor_load", "save_ok", "save_error", "collector_init", "committed"):
        assert forbidden not in names, f"o processor fez {forbidden!r}"
    assert [m for m, _ in tape.of("claim")] == ["c0", "p1", "c2"]
    assert tape.of("dispatch") == [["c0", "p1"], ["c2"]]
    # Claims escopadas pela partição: a recuperação de órfãs não toca outra.
    pending = dedupe.pending_set_key(42, "events:raw-p3")
    assert tape.of("recover") == [pending]
    assert {ps for _, ps in tape.of("claim")} == {pending}


def test_processor_reports_inactive_integration_instead_of_consuming(harness):
    harness.integration.is_active = False
    assert _process([{"id": "x"}]) is False
    assert "dispatch" not in harness.tape.names()


def test_processor_failure_releases_the_claims_and_raises(harness):
    """Hand-off que falha: as claims tomadas são soltas (o processor relê o lote
    e o dedupe não pode descartá-lo como duplicado) e o erro sobe para o seek."""

    def _dispatch_down(batch, routes=None, **kw):
        raise ConnectionError("data-plane fora")

    harness.monkeypatch.setattr(pipeline, "_enqueue_dispatch", _dispatch_down)
    with pytest.raises(ConnectionError):
        _process([{"id": "a"}, {"id": "b"}])
    assert harness.tape.of("release") == [["a", "b"]]


# ── Registro raw carrega os ids do coletor ───────────────────────────────────


def test_raw_record_roundtrips_ids_and_rejects_misaligned_ones():
    value = raw_mod.encode_raw(5, "s", [{"a": 1}, {"a": 2}], received_at=0.0, ids=["x", "y"])
    assert raw_mod.decode_raw(value)["ids"] == ["x", "y"]
    # Sem ids (push): o registro continua igual ao da Onda 2.
    assert "ids" not in json.loads(raw_mod.encode_raw(5, "s", [{"a": 1}], received_at=0.0))

    bad = json.dumps({"integration_id": 5, "stream": "s", "events": [{}, {}], "ids": ["x"]})
    with pytest.raises(ValueError):
        raw_mod.decode_raw(bad.encode())


def test_raw_frames_keep_ids_aligned_when_split():
    events = [{"pad": "x" * 400, "i": i} for i in range(8)]
    ids = [f"id{i}" for i in range(8)]
    frames = list(raw_mod._raw_frames(5, "s", events, 1200, 0.0, ids))
    assert len(frames) > 1
    for sub, value in frames:
        doc = raw_mod.decode_raw(value)
        assert doc["ids"] == [f"id{e['i']}" for e in sub]


def test_group_records_pads_missing_ids():
    recs = [
        types.SimpleNamespace(offset=0, value=raw_mod.encode_raw(1, "t", [{"o": 0}], received_at=0.0)),
        types.SimpleNamespace(
            offset=1, value=raw_mod.encode_raw(1, "t", [{"o": 1}, {"o": 2}], received_at=0.0, ids=["a", "b"])
        ),
    ]
    (group,) = list(proc._group_records(recs))
    assert len(group.events) == len(group.ids) == 3
    assert group.ids == ["", "a", "b"]


# ── O processor deriva o id sem instanciar coletor ───────────────────────────

_REGISTRATIONS = list(registry_module.all_registrations())


@pytest.mark.parametrize(
    "reg", _REGISTRATIONS, ids=[f"{r.platform}/{r.stream}" for r in _REGISTRATIONS]
)
def test_extract_message_id_does_not_need_the_collector_context(reg):
    """``process_raw_events`` chama ``extract_message_id`` numa instância SEM
    ``__init__`` (sem ``ctx``) para eventos push e registros sem ``ids``. Um
    coletor cujo id dependa de estado da instância quebraria ali — este teste
    pega antes, sem ler fonte (vale também na imagem compilada)."""
    instance = object.__new__(reg.collector_cls)
    sample = {"id": "1", "eventId": "1", "uuid": "1", "_ingest": {"id": "1"}}
    try:
        instance.extract_message_id(sample)
    except AttributeError as exc:
        assert type(instance).__name__ not in str(exc), (
            f"{reg.platform}/{reg.stream}: extract_message_id depende do estado da "
            f"instância ({exc}) — o processor não tem coletor instanciado"
        )
    except Exception:  # noqa: BLE001 — formato de evento é problema do vendor, não daqui
        pass


# ══ Onda 3b — configuração por snapshot com last-known-good ══════════════════
#
# O processor lê a configuração por um snapshot em memória: dentro do TTL não
# vai ao Postgres; com o banco fora, serve a última versão boa até o teto de
# idade. Erro que NÃO é indisponibilidade (rota que não compila) continua
# falhando — o last-known-good não pode mascarar configuração quebrada.

from sqlalchemy import exc as sa_exc  # noqa: E402

from backend.app.collectors import config_snapshot  # noqa: E402


def _db_down() -> Exception:
    return sa_exc.OperationalError("SELECT 1", {}, ConnectionRefusedError("postgres fora"))


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_snapshot_serves_from_memory_inside_the_ttl():
    clock = _Clock()
    cache = config_snapshot.SnapshotCache(30, 900, clock=clock)
    loads = []
    assert cache.get("routes", 7, lambda: loads.append(1) or "v1") == "v1"
    clock.now += 29
    assert cache.get("routes", 7, lambda: loads.append(1) or "v2") == "v1"
    clock.now += 2
    assert cache.get("routes", 7, lambda: loads.append(1) or "v2") == "v2"
    assert len(loads) == 2


def test_snapshot_serves_last_known_good_while_the_database_is_down(monkeypatch):
    from backend.app.collectors import metrics

    stale = []
    monkeypatch.setattr(
        metrics, "CONFIG_SNAPSHOT_STALE",
        types.SimpleNamespace(
            labels=lambda **kw: types.SimpleNamespace(inc=lambda: stale.append(kw["kind"]))
        ),
    )
    clock = _Clock()
    cache = config_snapshot.SnapshotCache(30, 900, clock=clock)
    cache.get("routes", 7, lambda: "boa")

    def _down():
        raise _db_down()

    clock.now += 300  # 5 min de Postgres fora: a meta do ADR
    assert cache.get("routes", 7, _down) == "boa"
    assert stale == ["routes"]
    clock.now += 601  # passou do teto de idade: para de inventar
    with pytest.raises(sa_exc.OperationalError):
        cache.get("routes", 7, _down)


def test_snapshot_never_masks_invalid_configuration():
    """Rota que não compila é decisão da Onda 0 (retém a entrega). Uma versão
    boa anterior em memória NÃO pode passar por cima disso."""
    clock = _Clock()
    cache = config_snapshot.SnapshotCache(30, 900, clock=clock)
    cache.get("routes", 7, lambda: "boa")
    clock.now += 60

    def _invalid():
        raise pipeline.RoutingUnavailable("rota 9 da org 7 inválida: ValueError")

    with pytest.raises(pipeline.RoutingUnavailable):
        cache.get("routes", 7, _invalid)


def test_snapshot_without_a_good_version_keeps_the_old_outcome():
    """Sem versão boa: carga fail-closed levanta (lote fica no Kafka); carga
    fail-safe volta ao valor degradado de antes desta onda."""
    token = config_snapshot.ACTIVE.set(config_snapshot.SnapshotCache(30, 900))
    try:
        def _down():
            raise _db_down()

        with pytest.raises(sa_exc.OperationalError):
            config_snapshot.read("routes", 7, _down)
        assert config_snapshot.read("residency", frozenset(), _down, lambda: {}) == {}
    finally:
        config_snapshot.ACTIVE.reset(token)


def test_store_outage_is_recognized_through_the_exception_chain():
    try:
        try:
            raise _db_down()
        except Exception as exc:
            raise pipeline.RoutingUnavailable("rotas da org 7 indisponíveis") from exc
    except pipeline.RoutingUnavailable as wrapped:
        assert config_snapshot.is_store_outage(wrapped)
    assert not config_snapshot.is_store_outage(ValueError("mapping corrompido"))


def test_inline_cycle_reads_without_a_snapshot(harness):
    """O ciclo inline não ganha cache: lê a cada ciclo, como sempre."""
    assert config_snapshot.ACTIVE.get() is None
    assert harness.cycle() is None
    assert harness.cycle() is None
    assert harness.tape.of("routes") == [7, 7]


def test_processor_survives_postgres_outage(harness):
    tape, mp = harness.tape, harness.monkeypatch
    clock = _Clock()
    mp.setattr(
        "backend.app.collectors.inflight.runtime.load_inflight_rules_for_org",
        lambda oid, **kw: pipeline._EMPTY_RULESET,
    )
    mp.setattr(
        "backend.app.collectors.enrich.runtime.load_policy_for_org",
        lambda oid, **kw: None,
    )
    calls = {"mapping": 0, "integration": 0}
    real_view = pipeline._load_cycle_integration

    def _mapping(*a, **k):
        calls["mapping"] += 1
        return (1, [], "v2")

    def _integration(iid, stream):
        calls["integration"] += 1
        return real_view(iid, stream)

    mp.setattr(pipeline, "_load_current_mapping", _mapping)
    mp.setattr(pipeline, "_load_cycle_integration", _integration)

    async def _batch(n):
        token = config_snapshot.ACTIVE.set(cache)
        try:
            return await pipeline.process_raw_events(
                42, "events", [{"id": f"b{n}-{i}"} for i in range(2)], scope="raw-p0"
            )
        finally:
            config_snapshot.ACTIVE.reset(token)

    cache = config_snapshot.SnapshotCache(30, 900, clock=clock)

    # Banco no ar: carrega uma vez e serve da memória dentro do TTL.
    assert asyncio.run(_batch(1)) is True
    clock.now += 10
    assert asyncio.run(_batch(2)) is True
    assert tape.of("routes") == [7] and calls == {"mapping": 1, "integration": 1}

    # Postgres cai por 5 minutos: os lotes seguem com a última versão boa.
    def _routes_down(oid):
        tape.events.append(("routes_down", oid))
        try:
            raise _db_down()
        except Exception as exc:
            raise pipeline.RoutingUnavailable(f"rotas da org {oid} indisponíveis") from exc

    def _down(*a, **k):
        raise _db_down()

    mp.setattr(pipeline, "_load_routes_for_org", _routes_down)
    mp.setattr(pipeline, "_load_current_mapping", _down)
    mp.setattr(pipeline, "_load_cycle_integration", _down)
    for minute in range(1, 6):
        clock.now += 60
        assert asyncio.run(_batch(10 + minute)) is True, f"lote perdido no minuto {minute}"
    delivered = [eid for batch in tape.of("dispatch") for eid in batch]
    assert len(delivered) == 7 * 2 and len(set(delivered)) == 14
    assert len(tape.of("routes_down")) == 5  # tentou o banco a cada TTL vencido

    # Fora além do teto de idade: o lote FALHA (fica no Kafka), não segue com
    # configuração velha demais.
    clock.now += 900
    with pytest.raises(Exception) as excinfo:
        asyncio.run(_batch(99))
    assert config_snapshot.is_store_outage(excinfo.value)
    assert not any(eid.startswith("b99") for b in tape.of("dispatch") for eid in b)


@pytest.mark.source_only  # lê o .py; na imagem Cython o fonte não existe
def test_destination_maps_go_through_the_snapshot():
    """Residência de dados, anti-loop Wazuh e fallback eram fail-safe para
    VAZIO: com o banco fora, a proteção sumia em silêncio. No processor eles
    passam pelo snapshot — e a versão estrita é a que alimenta o cache."""
    import inspect

    src = inspect.getsource(pipeline._enqueue_routed)
    for kind, fn in (
        ("destination_residency", "_load_destination_residency"),
        ("wazuh_loop_destinations", "_load_wazuh_loop_destination_ids"),
        ("fallback_destination", "_load_fallback_destination_id"),
    ):
        assert f'config_snapshot.read(\n        "{kind}"' in src, kind
        assert f"{fn}(" in src and "strict=True" in src, fn


def test_processor_activates_the_snapshot(monkeypatch):
    """O processor liga o snapshot antes de consumir — sem ele, nada desta onda vale."""
    import threading

    import aiokafka

    seen = {}

    class _Consumer:
        def __init__(self, *a, **k):
            pass

        async def start(self):
            seen["active"] = config_snapshot.ACTIVE.get()
            raise RuntimeError("para aqui")

        async def stop(self):
            pass

    monkeypatch.setattr(aiokafka, "AIOKafkaConsumer", _Consumer)
    monkeypatch.setattr(raw_mod, "_ensure_raw_topic", lambda: None)
    with pytest.raises(RuntimeError):
        asyncio.run(proc.run_raw_processor(stop_event=threading.Event()))
    active = seen["active"]
    assert isinstance(active, config_snapshot.SnapshotCache)
    assert active.ttl == settings.CONFIG_SNAPSHOT_TTL_SECONDS
    assert active.max_stale == settings.CONFIG_SNAPSHOT_MAX_STALE_SECONDS
