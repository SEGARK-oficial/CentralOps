"""Captura ao vivo como TAP DE CICLO DE VIDA (não só de entrega).

BUG DE PRODUTO CORRIGIDO: o único ponto que gravava na captura ficava atrás da guarda
``if redis is not None and last_result is not None and accepted_total > 0`` do dispatch
— ou seja, só depois que um destino ACEITOU o lote. Tudo que era coletado mas NÃO
entregue (drop, sem rota, quarentena, sink fora do ar, breaker, suppress, sample) era
INVISÍVEL, e o operador via "capturei nada" sem distinguir "não houve tráfego" de
"morreu antes do tap".

Cobre:
  (i)   cada desfecho chega na captura com ``outcome`` (+ destination_id/detail);
  (ii)  evento NÃO entregue agora aparece (a regressão principal);
  (iii) ``action=drop`` credita ``bytes_saved{reason=drop}`` (antes: invisível);
  (iv)  ``organization_id`` como string "1" deixa de ser descartado em silêncio;
  (v)   filtro de vendor da sessão é case-insensitive;
  (vi)  best-effort: exceção na captura/metering NÃO quebra dispatch/roteamento.
"""

from __future__ import annotations

import os
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import fakeredis
import fakeredis.aioredis as fakeredis_aio
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.collectors import (
    capture_session as cs,
    dataplane,
    observability_store as obs,
    pipeline,
    tasks,
    tracing,
)
from backend.app.collectors.output._fastjson import dumps_bytes
from backend.app.collectors.output.base import DeliveryResult, RejectedEvent
from backend.app.collectors.reduction import metering
from backend.app.collectors.routing.engine import (
    CompiledRoute,
    SamplingConfig,
    route_batch,
)
from backend.app.core.config import settings
from backend.app.db import models
from backend.app.db.database import Base

_W = 180  # janela de leitura (min) = janela da /cost-summary


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_capture_cache():
    """O cache negativo de sessões é estado de MÓDULO — zera entre testes."""
    cs.reset_session_cache()
    yield
    cs.reset_session_cache()


@pytest.fixture
def sync_redis(monkeypatch):
    """fakeredis SÍNCRONO plugado no cliente do tap de captura (produtor/roteamento)."""
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(cs, "_sync_redis", lambda: r)
    return r


@pytest.fixture
def async_redis():
    return fakeredis_aio.FakeRedis(decode_responses=True)


def _env(event_id: str, *, org: object = 7, vendor: str = "sophos", **labels) -> dict:
    return {
        "_centralops": {
            "event_id": event_id,
            "organization_id": org,
            "vendor": vendor,
            **labels,
        },
        "normalized": {},
        "raw": {"x": 1},
    }


async def _start_session(async_redis, sync_redis, org=7, **kw) -> str:
    """Cria a sessão pelo engine async e replica o estado no fakeredis SÍNCRONO
    (os dois clientes são fakes independentes; em produção é o mesmo Redis)."""
    meta = await cs.start_session(async_redis, org, **kw)
    sid = meta["id"]
    raw = await async_redis.hgetall(cs._meta_key(sid))
    sync_redis.hset(cs._meta_key(sid), mapping=raw)
    sync_redis.sadd(cs._org_index_key(org), sid)
    cs.reset_session_cache()
    return sid


def _read_sync(sync_redis, sid, limit=500) -> list:
    import json

    return [json.loads(x) for x in sync_redis.lrange(cs._events_key(sid), 0, limit - 1)]


# ── (v) filtro de vendor case-insensitive ────────────────────────────────────


@pytest.mark.asyncio
async def test_vendor_filter_is_case_insensitive(async_redis):
    """Sessão criada como "Sophos" (o operador digita o nome) DEVE casar eventos
    rotulados "sophos" (o coletor emite o slug). Antes: comparação exata → 0 eventos."""
    meta = await cs.start_session(async_redis, 7, vendor="Sophos")
    await cs.record(async_redis, [_env("a", vendor="sophos"), _env("b", vendor="SOPHOS")], 7)
    events = await cs.read_events(async_redis, meta["id"])
    assert len(events) == 2


@pytest.mark.asyncio
async def test_vendor_filter_still_excludes_other_vendors(async_redis):
    meta = await cs.start_session(async_redis, 7, vendor="Sophos")
    await cs.record(async_redis, [_env("a", vendor="wazuh")], 7)
    assert await cs.read_events(async_redis, meta["id"]) == []


# ── (i) o registro carrega o DESFECHO ────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_defaults_to_delivered_and_keeps_legacy_fields(async_redis):
    """Compatibilidade: o formato que a UI já lê (event/vendor/captured_at) continua,
    e ``outcome`` é ADICIONADO (default = delivered, o call-site histórico)."""
    meta = await cs.start_session(async_redis, 7)
    await cs.record(async_redis, [_env("a")], 7)
    (ev,) = await cs.read_events(async_redis, meta["id"])
    assert ev["vendor"] == "sophos"
    assert ev["captured_at"] > 0
    assert ev["event"]["_centralops"]["event_id"] == "a"
    assert ev["outcome"] == cs.OUTCOME_DELIVERED


@pytest.mark.asyncio
async def test_record_carries_destination_and_detail(async_redis):
    meta = await cs.start_session(async_redis, 7)
    await cs.record(
        async_redis, [_env("a")], 7,
        outcome=cs.OUTCOME_DELIVERY_FAILED, destination_id="d1", detail="sink 503",
    )
    (ev,) = await cs.read_events(async_redis, meta["id"])
    assert ev["outcome"] == cs.OUTCOME_DELIVERY_FAILED
    assert ev["destination_id"] == "d1"
    assert ev["detail"] == "sink 503"


@pytest.mark.asyncio
async def test_detail_is_truncated(async_redis):
    meta = await cs.start_session(async_redis, 7)
    await cs.record(async_redis, [_env("a")], 7, detail="x" * 5000)
    (ev,) = await cs.read_events(async_redis, meta["id"])
    assert len(ev["detail"]) == cs.MAX_DETAIL_CHARS


@pytest.mark.asyncio
async def test_fan_out_produces_one_delivered_record_per_destination(async_redis):
    """Um evento entregue a N destinos gera N registros ``delivered`` — desfecho POR
    destino (desejável), mas o ring segue limitado pelo ltrim."""
    meta = await cs.start_session(async_redis, 7)
    for dest in ("d1", "d2", "d3"):
        await cs.record(async_redis, [_env("a")], 7, destination_id=dest)
    events = await cs.read_events(async_redis, meta["id"])
    assert sorted(e["destination_id"] for e in events) == ["d1", "d2", "d3"]


@pytest.mark.asyncio
async def test_ring_size_still_caps_the_capture(async_redis):
    """Volume: mais desfechos por evento NÃO podem furar o teto do ring."""
    meta = await cs.start_session(async_redis, 7, ring_size=5)
    for i in range(40):
        await cs.record(async_redis, [_env(f"e{i}")], 7, outcome=cs.OUTCOME_DROPPED)
    assert len(await cs.read_events(async_redis, meta["id"], limit=500)) == 5


@pytest.mark.asyncio
async def test_outcome_counters_survive_ring_trim(async_redis):
    """Os contadores ``outcome:<nome>`` ficam no META, não no ring — então a UI
    distingue "nada aconteceu" de "houve tráfego, mas o ring já rolou"."""
    meta = await cs.start_session(async_redis, 7, ring_size=2)
    for i in range(6):
        await cs.record(async_redis, [_env(f"e{i}")], 7, outcome=cs.OUTCOME_DROPPED)
    await cs.record(async_redis, [_env("d")], 7, outcome=cs.OUTCOME_DELIVERED)
    raw = await async_redis.hgetall(cs._meta_key(meta["id"]))
    assert raw["outcome:dropped"] == "6"
    assert raw["outcome:delivered"] == "1"
    assert raw["event_count"] == "7"  # total real, mesmo com o ring podado em 2
    assert len(await cs.read_events(async_redis, meta["id"], limit=500)) == 2


def test_outcome_counters_written_by_sync_tap(sync_redis, monkeypatch):
    """O tap SÍNCRONO (roteamento/quarentena) mantém o mesmo contrato de contadores."""
    sid = "s-sync"
    sync_redis.hset(
        cs._meta_key(sid),
        mapping={
            "id": sid, "org_id": "7", "vendor": "", "status": "active",
            "expires_at": str(9e12), "ring_size": "100", "event_count": "0",
        },
    )
    sync_redis.sadd(cs._org_index_key(7), sid)
    cs.reset_session_cache()
    cs.record_sync([_env("a"), _env("b")], 7, outcome=cs.OUTCOME_UNROUTED)
    assert sync_redis.hget(cs._meta_key(sid), "outcome:unrouted") == "2"


# ── engine: acumula os EVENTOS de cada desfecho (puro, sem I/O) ──────────────


def _drop_route() -> CompiledRoute:
    return CompiledRoute(
        id="r-drop", name="drop", priority=100, condition={}, action="drop",
        destination_ids=(), is_final=True,
    )


def _route(dest="d1", **kw) -> CompiledRoute:
    base = dict(
        id="r1", name="r1", priority=100, condition={}, action="route",
        destination_ids=(dest,), is_final=True,
    )
    base.update(kw)
    return CompiledRoute(**base)


def test_engine_accumulates_dropped_events_with_route_id():
    batch = [_env("a"), _env("b")]
    res = route_batch(batch, [_drop_route()])
    assert res.dropped == 2
    assert [rid for _e, rid in res.dropped_events] == ["r-drop", "r-drop"]
    assert [e for e, _r in res.dropped_events] == batch


def test_engine_accumulates_unrouted_events():
    batch = [_env("a")]
    res = route_batch(batch, [])  # nenhuma rota, nenhum fallback
    assert res.unrouted == 1 and res.unrouted_events == batch


def test_engine_accumulates_loop_blocked_events():
    batch = [_env("a", platform="wazuh")]
    res = route_batch(
        batch, [_route(dest="wz")],
        wazuh_loop_destination_ids=frozenset({"wz"}),
    )
    assert res.loop_blocked == 1
    assert res.loop_blocked_events[0][0] is batch[0]
    assert res.loop_blocked_events[0][1]  # motivo não-vazio


def test_engine_accumulates_residency_blocked_events():
    batch = [_env("a", data_geography="EU")]
    res = route_batch(
        batch, [_route(dest="us-sink")],
        destination_residency={"us-sink": "US"},
    )
    assert res.residency_blocked == 1
    assert res.residency_blocked_events == [(batch[0], "us-sink")]


def test_engine_accumulates_sampled_events():
    batch = [_env("a")]
    route = _route(sample_percent=0, protect_detection=False)
    res = route_batch(batch, [route], sampling=SamplingConfig(enabled=True))
    assert res.sampled == 1
    assert res.sampled_events == [(batch[0], "d1", "r1")]


# ── (iii) drop credita bytes_saved ───────────────────────────────────────────


def test_engine_measures_dropped_bytes_when_asked():
    batch = [_env("a"), _env("b")]
    res = route_batch(batch, [_drop_route()], measure_drop_bytes=True)
    expected = float(sum(len(dumps_bytes(e)) for e in batch))
    assert res.dropped_bytes_per_org == {7: expected}


def test_engine_skips_drop_measurement_by_default():
    """Off ⇒ ZERO serialização extra no ramo de drop (default byte-idêntico)."""
    res = route_batch([_env("a")], [_drop_route()])
    assert res.dropped_bytes_per_org == {}


def test_engine_drop_measurement_never_raises_on_unserializable():
    """INVARIANTE: o metering nunca derruba o roteamento."""
    circular: dict = {}
    circular["self"] = circular
    env = _env("a")
    env["raw"] = circular
    res = route_batch([env], [_drop_route()], measure_drop_bytes=True)
    assert res.dropped == 1
    assert res.dropped_bytes_per_org.get(7, 0.0) == 0.0


def _fake_obs(monkeypatch):
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(obs, "_redis", lambda: r)
    return r


def test_record_drop_saving_gated_only_by_cost_metering(monkeypatch):
    """Drop NÃO tem flag REDUCTION_* — é config de rota, sempre ativa. Basta o
    COST_METERING_ENABLED (ao contrário de sample/suppress/trim/aggregate)."""
    _fake_obs(monkeypatch)
    monkeypatch.setattr(settings, "COST_METERING_ENABLED", True)
    monkeypatch.setattr(settings, "REDUCTION_SAMPLE_ENABLED", False)
    monkeypatch.setattr(settings, "REDUCTION_SUPPRESS_ENABLED", False)
    monkeypatch.setattr(settings, "REDUCTION_TRIM_ENABLED", False)
    metering.record_drop_saving(7, 4096.0)
    assert obs.read_window_total("org", "7", "bytes_saved", minutes=_W) == 4096.0


def test_record_drop_saving_noop_when_metering_off(monkeypatch):
    _fake_obs(monkeypatch)
    monkeypatch.setattr(settings, "COST_METERING_ENABLED", False)
    metering.record_drop_saving(7, 4096.0)
    assert obs.read_window_total("org", "7", "bytes_saved", minutes=_W) == 0.0


def test_record_drop_saving_fail_closed_on_missing_org(monkeypatch):
    _fake_obs(monkeypatch)
    monkeypatch.setattr(settings, "COST_METERING_ENABLED", True)
    metering.record_drop_saving(None, 4096.0)
    assert obs.read_window_total("org", "None", "bytes_saved", minutes=_W) == 0.0


def test_other_levers_still_credit_savings(monkeypatch):
    """Regressão: creditar drop não pode ter quebrado trim/sample/suppress/aggregate."""
    _fake_obs(monkeypatch)
    monkeypatch.setattr(settings, "COST_METERING_ENABLED", True)
    monkeypatch.setattr(settings, "REDUCTION_TRIM_ENABLED", True)
    monkeypatch.setattr(settings, "REDUCTION_SAMPLE_ENABLED", True)
    monkeypatch.setattr(settings, "REDUCTION_SUPPRESS_ENABLED", True)
    metering.record_trim_saving(7, {"a": "x" * 100}, {"a": "x"})
    metering.record_sample_saving(7, 1000.0)
    metering.record_suppress_saving(7, {"_centralops": {"organization_id": 7}})
    metering.record_saving(7, "d1", "aggregate", bytes_=500.0)
    assert obs.read_window_total("org", "7", "bytes_saved", minutes=_W) > 1500.0


# ── (iv) coerção de organization_id ──────────────────────────────────────────


def test_batch_org_id_coerces_int_string():
    """BUG: org como STRING devolvia None e a captura/audit/linhagem eram puladas
    em SILÊNCIO (sem log, sem métrica)."""
    assert pipeline._batch_org_id([_env("a", org="1")]) == 1
    assert pipeline._batch_org_id([_env("a", org=" 42 ")]) == 42


def test_batch_org_id_accepts_int_and_integral_float():
    assert pipeline._batch_org_id([_env("a", org=7)]) == 7
    assert pipeline._batch_org_id([_env("a", org=7.0)]) == 7


def test_batch_org_id_rejects_garbage():
    for bad in ("abc", "", "1.5", 1.5, True, [], {}, None):
        assert pipeline._batch_org_id([_env("a", org=bad)]) is None
    assert pipeline._batch_org_id([]) is None


def test_batch_org_id_logs_debug_on_non_coercible(caplog):
    import logging

    with caplog.at_level(logging.DEBUG, logger=pipeline.logger.name):
        assert pipeline._batch_org_id([_env("a", org="abc")]) is None
    assert any("não-coercível" in r.message for r in caplog.records)


# ── (ii) o pipeline escreve os desfechos NÃO entregues ───────────────────────


def _wire_enqueue(monkeypatch):
    monkeypatch.setattr(pipeline, "_load_destination_residency", lambda ids: {})
    monkeypatch.setattr(pipeline, "_load_wazuh_loop_destination_ids", lambda ids: frozenset())
    monkeypatch.setattr(pipeline, "_load_fallback_destination_id", lambda org: None)
    monkeypatch.setattr(tracing, "carrier", lambda: {})
    monkeypatch.setattr(settings, "EVENT_DATAPLANE", "celery")
    monkeypatch.setattr(tasks, "dispatch_to_destination", MagicMock())
    monkeypatch.setattr(dataplane, "produce_delivery", lambda *a, **k: None)


@pytest.mark.asyncio
async def test_dropped_event_reaches_capture(async_redis, sync_redis, monkeypatch):
    sid = await _start_session(async_redis, sync_redis)
    _wire_enqueue(monkeypatch)
    pipeline._enqueue_routed([_env("a")], [_drop_route()])
    (ev,) = _read_sync(sync_redis, sid)
    assert ev["outcome"] == cs.OUTCOME_DROPPED
    assert ev["detail"] == "route=r-drop"
    assert ev["event"]["_centralops"]["event_id"] == "a"


@pytest.mark.asyncio
async def test_unrouted_event_reaches_capture(async_redis, sync_redis, monkeypatch):
    sid = await _start_session(async_redis, sync_redis)
    _wire_enqueue(monkeypatch)
    monkeypatch.setattr(pipeline, "persist_batch_dlq", lambda *a, **k: None, raising=False)
    with patch("backend.app.collectors.delivery.persist_batch_dlq", lambda *a, **k: None):
        pipeline._enqueue_routed([_env("a")], [])
    (ev,) = _read_sync(sync_redis, sid)
    assert ev["outcome"] == cs.OUTCOME_UNROUTED


@pytest.mark.asyncio
async def test_loop_blocked_event_reaches_capture(async_redis, sync_redis, monkeypatch):
    sid = await _start_session(async_redis, sync_redis)
    _wire_enqueue(monkeypatch)
    monkeypatch.setattr(
        pipeline, "_load_wazuh_loop_destination_ids", lambda ids: frozenset({"wz"})
    )
    pipeline._enqueue_routed([_env("a", platform="wazuh")], [_route(dest="wz")])
    (ev,) = _read_sync(sync_redis, sid)
    assert ev["outcome"] == cs.OUTCOME_LOOP_BLOCKED


@pytest.mark.asyncio
async def test_residency_blocked_event_reaches_capture(async_redis, sync_redis, monkeypatch):
    sid = await _start_session(async_redis, sync_redis)
    _wire_enqueue(monkeypatch)
    monkeypatch.setattr(
        pipeline, "_load_destination_residency", lambda ids: {"us-sink": "US"}
    )
    with patch("backend.app.collectors.delivery.persist_batch_dlq", lambda *a, **k: None):
        pipeline._enqueue_routed([_env("a", data_geography="EU")], [_route(dest="us-sink")])
    events = _read_sync(sync_redis, sid)
    # o bloqueio esvaziou o fan-out → o evento seguiu para o caminho unrouted (DLQ).
    # A escuta mostra a CADEIA inteira ("como entrou e como saiu"), não só o fim.
    blocked = [e for e in events if e["outcome"] == cs.OUTCOME_RESIDENCY_BLOCKED]
    assert len(blocked) == 1 and blocked[0]["destination_id"] == "us-sink"
    assert any(e["outcome"] == cs.OUTCOME_UNROUTED for e in events)


@pytest.mark.asyncio
async def test_sampled_out_event_reaches_capture(async_redis, sync_redis, monkeypatch):
    sid = await _start_session(async_redis, sync_redis)
    _wire_enqueue(monkeypatch)
    monkeypatch.setattr(settings, "REDUCTION_SAMPLE_ENABLED", True)
    monkeypatch.setattr(settings, "COST_METERING_ENABLED", True)
    monkeypatch.setattr(settings, "REDUCTION_SAMPLE_PROTECT_DETECTION", True)
    _fake_obs(monkeypatch)
    pipeline._enqueue_routed(
        [_env("a")], [_route(sample_percent=0, protect_detection=False)]
    )
    (ev,) = _read_sync(sync_redis, sid)
    assert ev["outcome"] == cs.OUTCOME_SAMPLED_OUT
    assert ev["destination_id"] == "d1"


@pytest.mark.asyncio
async def test_enqueue_routed_credits_drop_bytes_saved(async_redis, sync_redis, monkeypatch):
    """(iii) ponta a ponta: uma rota action=drop passa a creditar Evitado."""
    _wire_enqueue(monkeypatch)
    _fake_obs(monkeypatch)
    monkeypatch.setattr(settings, "COST_METERING_ENABLED", True)
    batch = [_env("a"), _env("b")]
    pipeline._enqueue_routed(batch, [_drop_route()])
    expected = float(sum(len(dumps_bytes(e)) for e in batch))
    assert obs.read_window_total("org", "7", "bytes_saved", minutes=_W) == expected


@pytest.mark.asyncio
async def test_capture_skipped_for_other_org(async_redis, sync_redis, monkeypatch):
    """Isolamento multi-tenant: o desfecho de outro org não vaza para a sessão."""
    sid = await _start_session(async_redis, sync_redis, org=7)
    _wire_enqueue(monkeypatch)
    pipeline._enqueue_routed([_env("a", org=999)], [_drop_route()])
    assert _read_sync(sync_redis, sid) == []


@pytest.mark.asyncio
async def test_capture_works_with_string_org_in_envelope(async_redis, sync_redis, monkeypatch):
    """(iv) ponta a ponta: org "7" (string) NÃO é mais descartado — antes o lote
    inteiro ficava invisível para a captura."""
    sid = await _start_session(async_redis, sync_redis, org=7)
    _wire_enqueue(monkeypatch)
    pipeline._enqueue_routed([_env("a", org="7")], [_drop_route()])
    assert len(_read_sync(sync_redis, sid)) == 1


# ── (vi) best-effort: captura/metering nunca quebram o hot path ──────────────


@pytest.mark.asyncio
async def test_capture_failure_does_not_break_enqueue(async_redis, sync_redis, monkeypatch):
    """INVARIANTE: uma exceção na captura NÃO pode alterar o enfileiramento."""
    await _start_session(async_redis, sync_redis)
    _wire_enqueue(monkeypatch)
    dispatch = MagicMock()
    monkeypatch.setattr(tasks, "dispatch_to_destination", dispatch)

    def _boom(*a, **k):
        raise RuntimeError("captura explodiu")

    monkeypatch.setattr(cs, "active_sessions_sync", _boom)
    monkeypatch.setattr(cs, "record_sync", _boom)

    pipeline._enqueue_routed([_env("a")], [_route()])  # não deve levantar
    assert dispatch.apply_async.call_count == 1  # entrega intacta


def test_metering_failure_does_not_break_enqueue(monkeypatch):
    """INVARIANTE: o metering de drop é best-effort — uma falha nele não pode
    derrubar o roteamento."""
    _wire_enqueue(monkeypatch)
    _fake_obs(monkeypatch)
    monkeypatch.setattr(settings, "COST_METERING_ENABLED", True)

    def _boom(*a, **k):
        raise RuntimeError("metering explodiu")

    monkeypatch.setattr(metering, "record_saving", _boom)
    pipeline._enqueue_routed([_env("a")], [_drop_route()])  # não deve levantar


def test_capture_outcomes_tolerates_duck_typed_result(monkeypatch, sync_redis):
    """Resultados duck-typed (mocks de route_batch nos testes existentes) não têm os
    campos novos — o tap usa getattr defensivo."""
    monkeypatch.setattr(cs, "active_sessions_sync", lambda org, **k: [{"id": "s1"}])
    pipeline._capture_outcomes(7, types.SimpleNamespace())  # não deve levantar


# ── (i)/(ii) desfechos de ENTREGA no dispatch ────────────────────────────────


@pytest.fixture()
def static_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    import backend.app.db.database as db_module

    original = db_module.SessionLocal
    db_module.SessionLocal = TestingSessionLocal
    yield TestingSessionLocal, engine
    db_module.SessionLocal = original
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def seeded_destination(static_db):
    TestingSessionLocal, _ = static_db
    dest_id = "cap-splunk-001"
    with TestingSessionLocal() as session:
        session.add(
            models.Destination(
                id=dest_id, name="Cap Splunk", kind="splunk_hec", enabled=True,
                config='{"url": "https://splunk:8088", "sourcetype": "cap"}',
                secret_ref=None, delivery="{}", config_version="v1",
                organization_id=None,
            )
        )
        session.commit()
    return dest_id


def _patched_dispatch(target, worker_redis):
    return (
        patch("backend.app.core.secrets.get_default_backend", return_value=None),
        patch(
            "backend.app.collectors.output.destination_cache.get_destination",
            new_callable=AsyncMock, return_value=target,
        ),
        patch(
            "backend.app.collectors.celery_app.get_worker_redis",
            return_value=worker_redis,
        ),
    )


@pytest.mark.asyncio
async def test_dispatch_records_delivered_outcome(
    static_db, seeded_destination, async_redis, sync_redis
):
    sid = await _start_session(async_redis, sync_redis)
    target = AsyncMock()
    target.send_batch.return_value = DeliveryResult(accepted=1)
    a, b, c = _patched_dispatch(target, fakeredis_aio.FakeRedis(decode_responses=True))
    with a, b, c:
        from backend.app.collectors.pipeline import dispatch_batch_to_destination

        await dispatch_batch_to_destination(seeded_destination, [_env("ok-1")])

    (ev,) = _read_sync(sync_redis, sid)
    assert ev["outcome"] == cs.OUTCOME_DELIVERED
    assert ev["destination_id"] == seeded_destination


@pytest.mark.asyncio
async def test_dispatch_records_delivery_failed_when_nothing_accepted(
    static_db, seeded_destination, async_redis, sync_redis
):
    """A REGRESSÃO PRINCIPAL: com ``accepted_total == 0`` o tap histórico não gravava
    NADA — o operador via "capturei nada" e não sabia se houve tráfego."""
    sid = await _start_session(async_redis, sync_redis)
    target = AsyncMock()
    target.send_batch.return_value = DeliveryResult(
        accepted=0,
        rejected=[RejectedEvent(event_id="bad-1", reason="schema")],
        retryable=False,
    )
    a, b, c = _patched_dispatch(target, fakeredis_aio.FakeRedis(decode_responses=True))
    with a, b, c:
        from backend.app.collectors.pipeline import dispatch_batch_to_destination

        await dispatch_batch_to_destination(seeded_destination, [_env("bad-1")])

    (ev,) = _read_sync(sync_redis, sid)
    assert ev["outcome"] == cs.OUTCOME_DELIVERY_FAILED
    assert ev["destination_id"] == seeded_destination


@pytest.mark.asyncio
async def test_dispatch_records_delivery_failed_for_missing_destination(
    static_db, async_redis, sync_redis
):
    """Destino deletado/desabilitado depois do enqueue: o lote vai para a DLQ e o
    desfecho agora aparece na escuta."""
    sid = await _start_session(async_redis, sync_redis)
    with patch(
        "backend.app.collectors.delivery.persist_batch_dlq", lambda *a, **k: None
    ):
        from backend.app.collectors.pipeline import dispatch_batch_to_destination

        await dispatch_batch_to_destination("nao-existe", [_env("orphan-1")])

    (ev,) = _read_sync(sync_redis, sid)
    assert ev["outcome"] == cs.OUTCOME_DELIVERY_FAILED
    assert "ausente" in ev["detail"]


@pytest.mark.asyncio
async def test_dispatch_survives_capture_explosion(
    static_db, seeded_destination, sync_redis, monkeypatch
):
    """INVARIANTE: exceção na captura NÃO altera o resultado do dispatch."""
    def _boom(*a, **k):
        raise RuntimeError("captura explodiu")

    monkeypatch.setattr(cs, "record_sync", _boom)
    monkeypatch.setattr(cs, "active_sessions_sync", _boom)

    target = AsyncMock()
    target.send_batch.return_value = DeliveryResult(accepted=1)
    a, b, c = _patched_dispatch(target, fakeredis_aio.FakeRedis(decode_responses=True))
    with a, b, c:
        from backend.app.collectors.pipeline import dispatch_batch_to_destination

        await dispatch_batch_to_destination(seeded_destination, [_env("ok-1")])

    target.send_batch.assert_awaited_once()  # entrega intacta


# ── quarentena ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_quarantine_records_outcome(async_redis, sync_redis, monkeypatch):
    sid = await _start_session(async_redis, sync_redis)
    monkeypatch.setattr(
        pipeline.quarantine, "send_to_quarantine", lambda **k: None
    )
    await pipeline._quarantine_async(
        capture_org_id=7,
        integration_id=1,
        vendor="sophos",
        event_type="alert",
        raw={"id": "raw-1"},
        error_kind="map",
        error_detail="campo obrigatório ausente",
    )
    (ev,) = _read_sync(sync_redis, sid)
    assert ev["outcome"] == cs.OUTCOME_QUARANTINED
    assert ev["vendor"] == "sophos"
    assert ev["event"]["raw"] == {"id": "raw-1"}
    assert "map" in ev["detail"]


@pytest.mark.asyncio
async def test_quarantine_capture_failure_does_not_break_write(monkeypatch, sync_redis):
    """A quarentena é o write que importa — a captura não pode derrubá-la."""
    written = []
    monkeypatch.setattr(
        pipeline.quarantine, "send_to_quarantine", lambda **k: written.append(k)
    )

    def _boom(*a, **k):
        raise RuntimeError("captura explodiu")

    monkeypatch.setattr(cs, "record_sync", _boom)
    monkeypatch.setattr(cs, "active_sessions_sync", _boom)
    await pipeline._quarantine_async(
        capture_org_id=7, integration_id=1, vendor="sophos",
        event_type="alert", raw={}, error_kind="map", error_detail="x",
    )
    assert len(written) == 1


@pytest.mark.asyncio
async def test_quarantine_does_not_forward_capture_org_id(monkeypatch, sync_redis):
    """``capture_org_id`` é SÓ para o tap — não pode virar coluna da quarentena."""
    written = []
    monkeypatch.setattr(
        pipeline.quarantine, "send_to_quarantine", lambda **k: written.append(k)
    )
    await pipeline._quarantine_async(
        capture_org_id=7, integration_id=1, vendor="sophos",
        event_type="alert", raw={}, error_kind="map", error_detail="x",
    )
    assert "capture_org_id" not in written[0]


# ── supressão (rate-limit por assinatura, pré-roteamento) ────────────────────


@pytest.mark.asyncio
async def test_suppressed_event_reaches_capture(async_redis, sync_redis):
    """O evento suprimido morria ANTES do dispatch — nunca chegava à captura, e o
    operador via um buraco sem explicação."""
    sid = await _start_session(async_redis, sync_redis)
    await pipeline._capture_outcome(
        [_env("a")], 7, cs.OUTCOME_SUPPRESSED, detail="route=r-noise"
    )
    (ev,) = _read_sync(sync_redis, sid)
    assert ev["outcome"] == cs.OUTCOME_SUPPRESSED
    assert ev["detail"] == "route=r-noise"


@pytest.mark.asyncio
async def test_capture_outcome_is_fail_closed_on_missing_org(async_redis, sync_redis):
    """Anti cross-tenant: sem org, nada é gravado (nunca num bucket compartilhado)."""
    sid = await _start_session(async_redis, sync_redis)
    await pipeline._capture_outcome([_env("a")], None, cs.OUTCOME_SUPPRESSED)
    assert _read_sync(sync_redis, sid) == []


@pytest.mark.asyncio
async def test_capture_outcome_never_raises(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("captura explodiu")

    monkeypatch.setattr(cs, "record_sync", _boom)
    monkeypatch.setattr(cs, "active_sessions_sync", _boom)
    await pipeline._capture_outcome([_env("a")], 7, cs.OUTCOME_SUPPRESSED)


# ── short-circuit barato (performance) ───────────────────────────────────────


def test_no_active_session_short_circuits_after_first_probe(sync_redis):
    """Sem sessão, o tap não pode custar um round-trip Redis por chamada: o cache
    NEGATIVO memoiza a ausência por uma janela curta."""
    calls = {"n": 0}
    real_smembers = sync_redis.smembers

    def counting(key):
        calls["n"] += 1
        return real_smembers(key)

    sync_redis.smembers = counting
    for _ in range(50):
        cs.record_sync([_env("a")], 7, outcome=cs.OUTCOME_DROPPED)
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_start_session_invalidates_negative_cache(async_redis, sync_redis):
    """Uma sessão NOVA não pode ser mascarada pelo cache negativo do processo."""
    cs.record_sync([_env("a")], 7)  # marca "sem sessão"
    sid = await _start_session(async_redis, sync_redis, org=7)
    cs.record_sync([_env("b")], 7, outcome=cs.OUTCOME_DROPPED)
    assert len(_read_sync(sync_redis, sid)) == 1
