"""ADR-0016 — o TIQUE que decide silêncio, com relógio e Redis injetados.

O que uma amostra nunca pode provar sobre ausência este arquivo prova com o
tempo na mão: a chave calada além do prazo vira Detection e sai como evento; o
tique seguinte dentro da supressão bumpa ``count`` e não emite; a chave que
volta FECHA a Detection; a esquecida some sem alertar; e — o que torna a
ausência honesta — o observador parado produz ZERO alertas com o contador
aceso, e o mesmo estado com observador vivo alerta (o positivo ao lado do
negativo, senão o negativo passa por vacuidade).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from types import SimpleNamespace

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import fakeredis
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.collectors import observability_store as obs
from backend.app.collectors import otel_metrics
from backend.app.collectors.inflight import absence as absence_mod
from backend.app.collectors.inflight import runtime as runtime_mod
from backend.app.collectors.inflight.absence import (
    AbsenceTickResult,
    TICK_STATE_LAGGING,
    TICK_STATE_OK,
    TICK_STATE_UNOBSERVABLE,
    evaluate_absence_rule,
    evaluate_absence_rules_once,
)
from backend.app.collectors.inflight.runtime import compile_rule
from backend.app.core.config import settings
from backend.app.db import models
from backend.app.db.database import Base

ORG = 7
NOW = 1_800_000_000.0
WINDOW = 100
FORGET = 400

_WHERE = json.dumps([
    {"field": "_centralops.stream", "op": "eq", "value": "sophos.detection"},
    {"field": "normalized.metadata.event_code", "op": "eq", "value": "XDR-veeam-restorepointcreated"},
])


def _row(rid: int = 1, **extra) -> SimpleNamespace:
    base = dict(
        id=rid, name=f"abs-{rid}", severity_id=4, suppression_window_seconds=3600,
        rule_type="absence", eval_mode="inflight", where_json=_WHERE,
        group_by_field="_centralops.customer_name", window_seconds=WINDOW,
        absence_forget_seconds=FORGET, min_count=1, legs_json=None, emit_event=True,
        max_dedup_keys=None, organization_id=ORG,
    )
    base.update(extra)
    return SimpleNamespace(**base)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with Session() as s:
        s.add(models.Organization(id=ORG, name="Org", slug="org"))
        s.commit()
        yield s


@pytest.fixture()
def redis():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture()
def despachados(monkeypatch):
    lote: list[dict] = []
    monkeypatch.setattr(runtime_mod, "_dispatch_sync", lambda envs: lote.extend(envs))
    monkeypatch.setattr(otel_metrics, "record", lambda *a, **k: None)
    monkeypatch.setattr(otel_metrics, "count", lambda *a, **k: None)
    monkeypatch.setattr(obs, "record_counter", lambda *a, **k: True)
    monkeypatch.setattr(settings, "ABSENCE_GRACE_SECONDS", 0)
    monkeypatch.setattr(settings, "DETECTION_LIFECYCLE_EVENTS", False)
    return lote


def _seed(redis, *, last_cycle=NOW - 30, seen=None, alerted=None, rid=1):
    redis.hset(absence_mod.meta_key(ORG, rid), mapping={"last_cycle": int(last_cycle)})
    if seen:
        redis.hset(absence_mod.seen_key(ORG, rid), mapping={k: int(v) for k, v in seen.items()})
    if alerted:
        redis.hset(absence_mod.alerted_key(ORG, rid), mapping=alerted)


def _tick(rule, *, redis, db, now=NOW, budget=200) -> AbsenceTickResult:
    result = AbsenceTickResult()
    evaluate_absence_rule(rule, organization_id=ORG, redis=redis, db=db, now=now,
                          result=result, alert_budget=[budget], lock_ttl_s=55)
    return result


def _detections(db):
    return db.query(models.Detection).order_by(models.Detection.id).all()


# ── o caminho feliz, em três tiques ───────────────────────────────────


def test_silent_key_becomes_a_detection_and_an_event(db, redis, despachados):
    rule, _ = compile_rule(_row())
    _seed(redis, seen={"acme": NOW - 150, "beta": NOW - 10, "old": NOW - 500})

    r = _tick(rule, redis=redis, db=db)

    assert r.states[1] == TICK_STATE_OK
    assert (r.alerted, r.bumped, r.forgotten, r.recovered) == (1, 0, 1, 0)
    assert (r.tracked, r.silent) == (2, 1)
    dets = _detections(db)
    assert [d.dedup_key for d in dets] == ["absence:7:1:acme"]
    assert dets[0].count == 1 and dets[0].status == "open" and dets[0].source == "inflight"
    assert redis.hgetall(absence_mod.alerted_key(ORG, 1)) == {"acme": str(dets[0].id)}
    assert "old" not in redis.hgetall(absence_mod.seen_key(ORG, 1))
    # O evento saiu, e diz o que é.
    assert len(despachados) == 1
    n = despachados[0]["normalized"]
    assert n["finding_info"]["types"] == ["inflight", "absence"]
    assert n["unmapped"]["absence"]["silent_for_seconds"] == 150
    assert n["unmapped"]["group_value"] == "acme"
    meta = redis.hgetall(absence_mod.meta_key(ORG, 1))
    assert meta["state"] == TICK_STATE_OK and meta["tracked"] == "2" and meta["silent"] == "1"


def test_second_tick_inside_suppression_bumps_and_does_not_emit(db, redis, despachados):
    rule, _ = compile_rule(_row())
    _seed(redis, seen={"acme": NOW - 150})
    _tick(rule, redis=redis, db=db)
    redis.delete(absence_mod.lock_key(1))
    redis.hset(absence_mod.meta_key(ORG, 1), "last_cycle", int(NOW + 40))

    r = _tick(rule, redis=redis, db=db, now=NOW + 60)

    assert (r.alerted, r.bumped) == (0, 1)
    dets = _detections(db)
    assert len(dets) == 1 and dets[0].count == 2
    assert len(despachados) == 1  # nada novo saiu


def test_key_that_returns_closes_the_detection(db, redis, despachados, monkeypatch):
    rule, _ = compile_rule(_row())
    _seed(redis, seen={"acme": NOW - 150})
    _tick(rule, redis=redis, db=db)
    det_id = _detections(db)[0].id
    # Voltou: o flush gravou presença nova.
    redis.delete(absence_mod.lock_key(1))
    redis.hset(absence_mod.seen_key(ORG, 1), "acme", int(NOW + 50))
    redis.hset(absence_mod.meta_key(ORG, 1), "last_cycle", int(NOW + 50))
    # Com a flag de ciclo de vida ligada o fechamento sai como 2004 de Close.
    monkeypatch.setattr(settings, "DETECTION_LIFECYCLE_EVENTS", True)
    fechados: list = []
    # ``emit_status_event`` importa ``_enqueue_dispatch`` DENTRO da função, do
    # módulo do pipeline: o patch tem de ser lá, senão o 2004 vai ao broker.
    from backend.app.collectors import pipeline as pipeline_mod
    monkeypatch.setattr(pipeline_mod, "_enqueue_dispatch", lambda envs: fechados.extend(envs))

    r = _tick(rule, redis=redis, db=db, now=NOW + 60)

    assert r.recovered == 1 and r.alerted == 0
    assert _detections(db)[0].status == "closed"
    assert redis.hgetall(absence_mod.alerted_key(ORG, 1)) == {}
    assert len(fechados) == 1 and fechados[0]["normalized"]["activity_id"] == 3
    assert fechados[0]["normalized"]["finding_info"]["uid"] == "absence:7:1:acme"
    assert det_id == _detections(db)[0].id


def test_auto_close_can_be_turned_off(db, redis, despachados, monkeypatch):
    monkeypatch.setattr(settings, "ABSENCE_AUTO_CLOSE", False)
    rule, _ = compile_rule(_row())
    _seed(redis, seen={"acme": NOW - 150})
    _tick(rule, redis=redis, db=db)
    redis.delete(absence_mod.lock_key(1))
    redis.hset(absence_mod.seen_key(ORG, 1), "acme", int(NOW + 50))
    redis.hset(absence_mod.meta_key(ORG, 1), "last_cycle", int(NOW + 50))

    r = _tick(rule, redis=redis, db=db, now=NOW + 60)

    assert r.recovered == 0
    assert _detections(db)[0].status == "open"


# ── as guardas: falhar para o lado visível ────────────────────────────


def test_stale_observer_alerts_nothing_and_counts(db, redis, despachados):
    rule, _ = compile_rule(_row())
    _seed(redis, last_cycle=NOW - 2000, seen={"acme": NOW - 150})

    r = _tick(rule, redis=redis, db=db)

    assert r.unobservable == 1 and r.alerted == 0 and r.evaluated == 0
    assert _detections(db) == [] and despachados == []
    assert redis.hgetall(absence_mod.meta_key(ORG, 1))["state"] == TICK_STATE_UNOBSERVABLE

    # O POSITIVO: o mesmo estado com batimento fresco alerta.
    redis.delete(absence_mod.lock_key(1))
    redis.hset(absence_mod.meta_key(ORG, 1), "last_cycle", int(NOW - 30))
    r2 = _tick(rule, redis=redis, db=db)
    assert r2.alerted == 1 and r2.unobservable == 0


def test_missing_heartbeat_is_unobservable_too(db, redis, despachados):
    rule, _ = compile_rule(_row())
    redis.hset(absence_mod.seen_key(ORG, 1), "acme", int(NOW - 150))  # sem meta

    r = _tick(rule, redis=redis, db=db)

    assert r.unobservable == 1 and _detections(db) == []


def test_lagging_source_holds_the_rule(db, redis, despachados, monkeypatch):
    rule, _ = compile_rule(_row())
    _seed(redis, seen={"acme": NOW - 150})
    perguntas: list = []

    def _lag(_db, org, stream, window, now):
        perguntas.append((org, stream, window))
        return True

    monkeypatch.setattr(absence_mod, "_source_lagging", _lag)
    r = _tick(rule, redis=redis, db=db)

    assert r.lagging == 1 and r.alerted == 0 and _detections(db) == []
    assert perguntas == [(ORG, "sophos.detection", WINDOW)]
    assert redis.hgetall(absence_mod.meta_key(ORG, 1))["state"] == TICK_STATE_LAGGING

    # Positivo: fonte em dia ⇒ alerta.
    monkeypatch.setattr(absence_mod, "_source_lagging", lambda *a: False)
    redis.delete(absence_mod.lock_key(1))
    assert _tick(rule, redis=redis, db=db).alerted == 1


def test_rule_without_pinned_stream_never_asks_the_watermark(db, redis, despachados, monkeypatch):
    where = json.dumps([{"field": "normalized.metadata.event_code", "op": "eq", "value": "x"}])
    rule, _ = compile_rule(_row(where_json=where))
    _seed(redis, seen={"acme": NOW - 150})
    monkeypatch.setattr(absence_mod, "_source_lagging", lambda *a: (_ for _ in ()).throw(AssertionError("não devia perguntar")))

    assert _tick(rule, redis=redis, db=db).alerted == 1


def test_source_lagging_reads_watermark_and_cap_together(db):
    """Atraso sozinho não prova backlog (stream sem eventos mantém o watermark
    parado): só watermark velho E teto atingido segura a regra."""
    integ = models.Integration(organization_id=ORG, name="sophos", platform="sophos")
    db.add(integ)
    db.commit()
    old = datetime.utcfromtimestamp(NOW - 5000)
    st = models.CollectionState(integration_id=integ.id, stream="sophos.detection",
                                watermark_at=old, last_run_capped=False)
    db.add(st)
    db.commit()
    assert absence_mod._source_lagging(db, ORG, "sophos.detection", WINDOW, NOW) is False
    st.last_run_capped = True
    db.commit()
    assert absence_mod._source_lagging(db, ORG, "sophos.detection", WINDOW, NOW) is True
    # Outro stream da mesma integração não conta.
    assert absence_mod._source_lagging(db, ORG, "sophos.alert", WINDOW, NOW) is False


def test_redis_down_in_the_tick_alerts_nothing(db, despachados):
    class _Broken:
        def set(self, *a, **k):
            raise ConnectionError("fora")

        def pipeline(self):
            raise ConnectionError("fora")

    rule, _ = compile_rule(_row())
    r = _tick(rule, redis=_Broken(), db=db)
    assert r.unavailable == 1 and r.alerted == 0 and _detections(db) == []


def test_lock_held_elsewhere_skips_the_rule(db, redis, despachados):
    rule, _ = compile_rule(_row())
    _seed(redis, seen={"acme": NOW - 150})
    redis.set(absence_mod.lock_key(1), "1", ex=55)

    r = _tick(rule, redis=redis, db=db)
    assert r.skipped_lock == 1 and _detections(db) == []


def test_alert_budget_is_global_and_leaves_the_rest_for_the_next_tick(db, redis, despachados):
    rule, _ = compile_rule(_row())
    _seed(redis, seen={"a": NOW - 150, "b": NOW - 150, "c": NOW - 150})

    r = _tick(rule, redis=redis, db=db, budget=2)

    assert r.alerted == 2 and len(_detections(db)) == 2
    # As três seguem caladas e vigiadas; a terceira alerta no próximo tique.
    assert r.silent == 3 and r.tracked == 3


def test_grace_is_added_to_the_window(db, redis, despachados, monkeypatch):
    monkeypatch.setattr(settings, "ABSENCE_GRACE_SECONDS", 100)
    rule, _ = compile_rule(_row())
    _seed(redis, seen={"acme": NOW - 150})  # 150 > 100 mas < 100 + 100
    assert _tick(rule, redis=redis, db=db).alerted == 0
    redis.delete(absence_mod.lock_key(1))
    assert _tick(rule, redis=redis, db=db, now=NOW + 60).alerted == 1


# ── o tique inteiro ───────────────────────────────────────────────────


def test_tick_once_walks_the_enabled_rules_and_never_raises(db, redis, despachados, monkeypatch):
    rows = [_row(1), _row(2, group_by_field=None)]  # a 2ª não compila
    from backend.app.db import repository

    monkeypatch.setattr(repository.CorrelationRuleRepository, "list_absence_enabled", lambda self, limit=500: rows)
    _seed(redis, seen={"acme": NOW - 150}, rid=1)

    r = evaluate_absence_rules_once(now=NOW, redis=redis, session_factory=lambda: db)

    assert r.rules == 1 and r.rejected == 1 and r.alerted == 1 and r.errors == 0
    assert r.as_dict()["states"] == {1: TICK_STATE_OK}


def test_tick_is_a_no_op_when_disabled(db, redis, despachados, monkeypatch):
    monkeypatch.setattr(settings, "ABSENCE_RULES_ENABLED", False)
    from backend.app.db import repository

    monkeypatch.setattr(repository.CorrelationRuleRepository, "list_absence_enabled",
                        lambda self, limit=500: (_ for _ in ()).throw(AssertionError("não devia consultar")))
    r = evaluate_absence_rules_once(now=NOW, redis=redis, session_factory=lambda: db)
    assert r.rules == 0 and r.alerted == 0
