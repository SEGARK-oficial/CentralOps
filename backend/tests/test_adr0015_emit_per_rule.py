"""Emissão do evento de detecção decidida POR REGRA (ADR-0015, W1.4).

Até aqui a única forma de a Detection em voo sair como evento OCSF 2004 era o
interruptor global ``INFLIGHT_EMIT_OCSF_EVENT`` — tudo ou nada, por ambiente.
A decisão de produto é por regra: é o operador, na tela, quem escolhe qual
alerta chega ao SIEM e paga volume por ele.

Contrato fixado aqui, por COMPORTAMENTO (roda também na imagem Cython):

* flag global OFF + regra com ``emit_event`` ⇒ sai SÓ a Detection dessa regra;
  a da regra que não pediu é contada como ``rule_opt_out``, nunca como falha;
* flag global OFF + nenhuma regra pedindo ⇒ o emissor nem roda: nenhuma série
  nova nasce numa instalação que não pediu a feature (byte-idêntico);
* flag global ON ⇒ emite tudo, como antes — a flag é o "emite tudo", não um
  teto que sobrepõe a regra;
* ``compile_rule`` lê a coluna e ``DetectionEmit`` a carrega: o emissor só vê
  tickets, então sem o campo no ticket a decisão não teria como chegar lá.

Imports usam ``backend.app.*`` (gate compilado dual-root).
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

from dataclasses import replace
from typing import Any
from uuid import uuid4

import fakeredis
import pytest
from sqlalchemy import create_engine
from sqlalchemy import event as sa_event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.collectors import otel_metrics
from backend.app.collectors.inflight import runtime as runtime_mod
from backend.app.collectors.inflight.matcher import CompiledInflightRule
from backend.app.collectors.inflight.runtime import (
    DetectionEmit,
    InflightAccumulator,
    compile_rule,
    flush_inflight,
)
from backend.app.collectors import observability_store as obs
from backend.app.core.config import settings
from backend.app.db import database, models
from backend.app.db.database import Base


@pytest.fixture()
def db_session(monkeypatch: pytest.MonkeyPatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @sa_event.listens_for(engine, "connect")
    def _enforce_fk(dbapi_conn: object, _rec: object) -> None:
        cur = dbapi_conn.cursor()  # type: ignore[attr-defined]
        try:
            cur.execute("PRAGMA foreign_keys=ON")
        finally:
            cur.close()

    maker = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", maker)
    yield maker
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture(autouse=True)
def _fake_obs(monkeypatch: pytest.MonkeyPatch) -> None:
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(obs, "_redis", lambda: r)


@pytest.fixture()
def despachados(monkeypatch: pytest.MonkeyPatch) -> list[list[dict]]:
    lotes: list[list[dict]] = []
    monkeypatch.setattr(
        runtime_mod, "_dispatch_sync", lambda envelopes: lotes.append(list(envelopes))
    )
    return lotes


@pytest.fixture()
def metricas(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, float, dict]]:
    capturado: list[tuple[str, float, dict]] = []

    def _count(name: str, value: float = 1, attrs: dict | None = None) -> None:
        capturado.append((name, float(value), dict(attrs or {})))

    monkeypatch.setattr(otel_metrics, "count", _count)
    return capturado


def _total(metricas: list[tuple[str, float, dict]], nome: str, **attrs: str) -> float:
    return sum(
        v for n, v, a in metricas
        if n == nome and all(a.get(k) == val for k, val in attrs.items())
    )


def _seed_org(db_session) -> int:
    slug = f"org-{uuid4().hex[:8]}"
    with db_session() as db:
        org = models.Organization(name=slug, slug=slug, is_active=True)
        db.add(org)
        db.commit()
        db.refresh(org)
        return int(org.id)


def _rule(rule_id: int, *, emit: bool) -> CompiledInflightRule:
    return CompiledInflightRule(
        rule_id=rule_id, name=f"regra-{rule_id}", severity_id=5,
        suppression_window_seconds=3600, group_by_path=("raw", "user"),
        clauses=(), emit_event=emit,
    )


def _envelope(org_id: int, user: str) -> dict[str, Any]:
    return {
        "_centralops": {
            "vendor": "sophos", "platform": "sophos", "stream": "alerts",
            "event_type": "sophos.alert", "event_id": f"evt-{user}",
            "customer_id": org_id, "organization_id": org_id,
            "organization_slug": "acme",
        },
        "normalized": {"class_uid": 3002, "time": 1_750_000_000_000, "severity_id": 4},
        "raw": {"user": user},
    }


def _emitted_rule_ids(despachados: list[list[dict]]) -> list[str]:
    out: list[str] = []
    for lote in despachados:
        for env in lote:
            fi = (env.get("normalized") or {}).get("finding_info") or {}
            out.append(str(fi.get("uid") or fi.get("title") or env))
    return out


# ── flag OFF: só quem pediu ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_global_off_emits_only_the_rules_that_asked(
    db_session, despachados, metricas
) -> None:
    assert settings.INFLIGHT_EMIT_OCSF_EVENT is False, "o default global segue OFF"
    org_id = _seed_org(db_session)
    acc = InflightAccumulator()
    acc.add(_rule(1, emit=True), _envelope(org_id, "a"), organization_id=org_id)
    acc.add(_rule(2, emit=False), _envelope(org_id, "b"), organization_id=org_id)
    await flush_inflight(acc, organization_id=org_id)

    assert len(despachados) == 1 and len(despachados[0]) == 1, "só a regra 1 sai"
    assert _total(metricas, "collector_inflight_events_emitted_total") == 1
    assert _total(
        metricas, "collector_inflight_events_not_emitted_total", reason="rule_opt_out"
    ) == 1
    # As DUAS Detections estão gravadas: a emissão é decisão de saída, não de registro.
    with db_session() as db:
        assert db.query(models.Detection).count() == 2


@pytest.mark.asyncio
async def test_global_off_and_nobody_asking_creates_no_series(
    db_session, despachados, metricas
) -> None:
    """Byte-idêntico: sem regra pedindo, o emissor nem roda — nem a série de
    opt-out nasce. Uma instalação que não usa a feature não ganha painel."""
    org_id = _seed_org(db_session)
    acc = InflightAccumulator()
    acc.add(_rule(1, emit=False), _envelope(org_id, "a"), organization_id=org_id)
    await flush_inflight(acc, organization_id=org_id)
    assert despachados == []
    assert _total(metricas, "collector_inflight_events_not_emitted_total") == 0
    assert _total(metricas, "collector_inflight_events_emitted_total") == 0


@pytest.mark.asyncio
async def test_global_on_still_emits_everything(
    db_session, despachados, metricas, monkeypatch
) -> None:
    """A flag global é o "emite tudo" de antes, não um teto sobre a regra."""
    monkeypatch.setattr(settings, "INFLIGHT_EMIT_OCSF_EVENT", True)
    org_id = _seed_org(db_session)
    acc = InflightAccumulator()
    acc.add(_rule(1, emit=True), _envelope(org_id, "a"), organization_id=org_id)
    acc.add(_rule(2, emit=False), _envelope(org_id, "b"), organization_id=org_id)
    await flush_inflight(acc, organization_id=org_id)
    assert len(despachados) == 1 and len(despachados[0]) == 2
    assert _total(
        metricas, "collector_inflight_events_not_emitted_total", reason="rule_opt_out"
    ) == 0


# ── a decisão atravessa compile → ticket ─────────────────────────────────────


class _Row:
    def __init__(self, **kw: Any) -> None:
        self.id = kw.get("id", 7)
        self.name = kw.get("name", "r")
        self.severity_id = 4
        self.suppression_window_seconds = 60
        self.group_by_field = kw.get("group_by_field", "raw.user")
        self.where_json = kw.get("where_json", '[{"field":"raw.user","op":"exists","value":true}]')
        self.emit_event = kw.get("emit_event")
        self.max_dedup_keys = kw.get("max_dedup_keys")


@pytest.mark.parametrize("stored,expected", [(True, True), (False, False), (None, False)])
def test_compile_rule_reads_emit_event_and_defaults_to_off(stored, expected):
    rule, reason = compile_rule(_Row(emit_event=stored))
    assert reason is None
    assert rule.emit_event is expected


def test_compile_rule_reads_the_per_rule_dedup_cap_and_treats_zero_as_env():
    """0 não é "sem teto": é "usa o env". Um teto de zero chaves seria uma
    regra que nunca gera Detection — e ninguém digita isso de propósito."""
    assert compile_rule(_Row(max_dedup_keys=300))[0].max_dedup_keys == 300
    assert compile_rule(_Row(max_dedup_keys=0))[0].max_dedup_keys is None
    assert compile_rule(_Row(max_dedup_keys=None))[0].max_dedup_keys is None


def test_ticket_defaults_keep_existing_constructions_valid():
    t = DetectionEmit(dedup_key="k", detection_id=1, rule_id=1, rule_name="r",
                      severity_id=4, integration_id=None, source={})
    assert t.emit_event is False
    assert replace(t, emit_event=True).emit_event is True


# ── teto de chaves por regra (W1.5) ──────────────────────────────────────────


def test_per_rule_dedup_cap_overrides_the_env(monkeypatch) -> None:
    monkeypatch.setattr(settings, "INFLIGHT_MAX_DEDUP_KEYS_PER_RULE_PER_CYCLE", 2)
    org_id = 1
    larga = replace(_rule(1, emit=False), max_dedup_keys=5)
    estreita = _rule(2, emit=False)
    acc = InflightAccumulator()
    for u in "abcde":
        acc.add(larga, _envelope(org_id, u), organization_id=org_id)
        acc.add(estreita, _envelope(org_id, u), organization_id=org_id)
    chaves_por_regra = {}
    for key in acc.pending:
        rid = int(key.split(":")[2])
        chaves_por_regra[rid] = chaves_por_regra.get(rid, 0) + 1
    assert chaves_por_regra == {1: 5, 2: 2}, "a regra com teto próprio ignora o env"
    assert acc.overflow.get(2) == 3 and acc.overflow.get(1) is None


def test_env_default_covers_the_measured_worst_case() -> None:
    """Medido em 121k eventos reais: máx 192 chaves/ciclo em
    ``sophos.siem_event`` por ``endpoint_id``. O default tem de cobrir isso —
    com 50, 74% dos ciclos perdiam chaves em silêncio."""
    assert int(settings.INFLIGHT_MAX_DEDUP_KEYS_PER_RULE_PER_CYCLE) >= 192
