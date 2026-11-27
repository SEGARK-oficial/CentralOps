"""Métricas do ciclo de coleta, por fluxo.

Um fluxo ficou 47 h atrasado sem erro (nov/2026) e nenhuma série mostrava:

- nada contava o que a COLETA trouxe (``collector_events_total`` sai do
  ``processor``);
- "o ciclo parou no teto" só existia numa coluna do banco;
- o atraso era um gauge gravado no fim do ciclo — congela quando o ciclo para de
  terminar, que é exatamente o caso que importa.

Aqui o ciclo REAL (``pipeline._run_collection_once``) roda contra dublês e o que
se afirma é o que foi emitido. As fachadas são trocadas por gravadores: contar é
à prova de ``except`` amplo, levantar não é.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pytest

from backend.app.collectors import pipeline
from backend.app.collectors.dataplane import raw as raw_mod

Emit = Tuple[str, Dict[str, str], float]


class _Recorder:
    """Fachada de métrica que só anota: (operação, labels, valor)."""

    def __init__(self, boom: bool = False) -> None:
        self.emits: List[Emit] = []
        self._boom = boom

    def labels(self, **labels: Any) -> "_Bound":
        if self._boom:
            raise RuntimeError("backend de métricas fora")
        return _Bound(self, {k: str(v) for k, v in labels.items()})

    def total(self, **match: str) -> float:
        return sum(v for _, lb, v in self.emits if all(lb.get(k) == x for k, x in match.items()))


class _Bound:
    def __init__(self, rec: _Recorder, labels: Dict[str, str]) -> None:
        self._rec, self._labels = rec, labels

    def inc(self, amount: float = 1) -> None:
        self._rec.emits.append(("inc", self._labels, amount))

    def set(self, amount: float) -> None:
        self._rec.emits.append(("set", self._labels, amount))


class _Org:
    name = "Org Exemplo"
    slug = "org-exemplo"


class _Integration:
    id = 42
    name = "Fonte Exemplo"
    is_active = True
    kind = "tenant"
    platform = "fakevendor"
    organization_id = 7
    organization = _Org()
    data_geography = None


class _Db:
    integration: Any = _Integration()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def scalar(self, *a, **k):
        return self.integration

    def expunge(self, *a, **k):
        return None


class _Config:
    collector_batch_size = 1  # um flush por evento: o parcial de um ciclo com erro conta
    collector_batch_flush_seconds = 1e9
    rate_limits_by_vendor: dict = {}
    domain_concurrency_limits: dict = {}


class _Redis:
    async def aclose(self) -> None:
        return None


_WATERMARK = datetime(2026, 11, 25, 19, 31, 14)  # naive UTC, como ``watermark_at`` devolve


def _collector_cls(*, capped: bool = False, boom: bool = False, watermark: Optional[datetime] = None):
    class _Collector:
        event_type = "fake.event"

        def __init__(self, ctx):
            self.ctx = ctx

        async def collect(self):
            yield {"id": "e0"}
            if boom:
                raise RuntimeError("fornecedor caiu no meio do ciclo")
            yield {"id": "e1"}
            if capped:
                self.ctx.hit_cycle_cap = True

        def extract_message_id(self, raw):
            return raw["id"]

        @staticmethod
        def watermark_at(cursor):
            return watermark

    return _Collector


class _Registration:
    refresh_fn = None
    schedule = timedelta(minutes=5)
    collector_cls: Any = None


@pytest.fixture()
def cycle(monkeypatch):
    recs = {
        name: _Recorder()
        for name in (
            "CYCLES_TOTAL", "CYCLE_SECONDS", "EVENTS_COLLECTED",
            "STREAM_CADENCE", "WATERMARK_TIMESTAMP", "INTEGRATION_INFO",
        )
    }
    for name, rec in recs.items():
        monkeypatch.setattr(pipeline, name, rec)

    class _CursorStore:
        def __init__(self, *a, **k):
            pass

        async def load(self, *a, **k):
            return {}

        async def save(self, *a, **k):
            return None

    state = {"produce_boom": False, "push": False}

    async def _produce(integration_id, stream, events, ids=None, historical=False):
        if state["produce_boom"]:
            raise RuntimeError("broker fora")
        return len(events)

    @contextlib.asynccontextmanager
    async def _session():
        yield None

    async def _config(*a, **k):
        return _Config()

    async def _token(*a, **k):
        return "tok"

    monkeypatch.setattr("backend.app.collectors.celery_app.get_worker_redis", lambda: _Redis())
    monkeypatch.setattr(pipeline.database, "SessionLocal", lambda: _Db())
    monkeypatch.setattr(pipeline, "registry_has", lambda p, s: True)
    monkeypatch.setattr(pipeline, "registry_is_push", lambda p: state["push"])
    monkeypatch.setattr(pipeline, "_headers_for", lambda *a, **k: {})
    monkeypatch.setattr(pipeline, "_load_collection_filters", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "get_collector_config", _config)
    monkeypatch.setattr(pipeline, "get_or_refresh_token", _token)
    monkeypatch.setattr(pipeline, "CursorStore", _CursorStore)
    monkeypatch.setattr(pipeline, "RedisRateLimiter", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "DomainLimiter", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_aiohttp_session", _session)
    monkeypatch.setattr(raw_mod, "produce_raw_async", _produce)

    def run(**collector_kw) -> Optional[BaseException]:
        reg = _Registration()
        reg.collector_cls = _collector_cls(**collector_kw)
        monkeypatch.setattr(pipeline, "registry_get", lambda p, s: reg)
        try:
            asyncio.run(pipeline._run_collection_once(integration_id=42, stream="events"))
            return None
        except Exception as exc:  # noqa: BLE001 — o teste inspeciona o que saiu
            return exc

    return recs, run, state, monkeypatch


_FLOW = {"vendor": "fakevendor", "integration_id": "42", "stream": "events"}


def test_ciclo_ok_conta_o_ciclo_os_eventos_e_o_tempo(cycle) -> None:
    recs, run, _, _ = cycle
    assert run() is None

    assert recs["CYCLES_TOTAL"].emits == [("inc", {**_FLOW, "outcome": "ok"}, 1)]
    assert recs["EVENTS_COLLECTED"].total(**_FLOW) == 2
    (op, labels, seconds), = recs["CYCLE_SECONDS"].emits
    assert (op, labels) == ("inc", _FLOW) and 0 <= seconds < 5


def test_ciclo_que_para_no_teto_sai_como_capped(cycle) -> None:
    """É o sinal que faltava: "sobrou trabalho" só existia numa coluna do banco."""
    recs, run, _, _ = cycle
    assert run(capped=True) is None
    assert [lb["outcome"] for _, lb, _ in recs["CYCLES_TOTAL"].emits] == ["capped"]


def test_ciclo_com_erro_conta_como_error_e_o_erro_sobe(cycle) -> None:
    """O parcial gravado no raw antes da falha também conta: foi trabalho feito
    (e será relido), e é ele que denuncia um fluxo que coleta e nunca conclui."""
    recs, run, _, _ = cycle
    exc = run(boom=True)
    assert isinstance(exc, RuntimeError) and "fornecedor caiu" in str(exc)

    assert [lb["outcome"] for _, lb, _ in recs["CYCLES_TOTAL"].emits] == ["error"]
    assert recs["EVENTS_COLLECTED"].total(**_FLOW) == 1
    assert len(recs["CYCLE_SECONDS"].emits) == 1


def test_evento_so_conta_depois_do_raw_confirmar(cycle) -> None:
    recs, run, state, _ = cycle
    state["produce_boom"] = True
    assert isinstance(run(), RuntimeError)
    assert recs["EVENTS_COLLECTED"].emits == []
    assert [lb["outcome"] for _, lb, _ in recs["CYCLES_TOTAL"].emits] == ["error"]


def test_watermark_sai_como_instante_e_nao_como_atraso(cycle) -> None:
    """Atraso gravado no fim do ciclo congela quando o ciclo não termina. O
    instante não: ``time() - valor`` segue crescendo no Prometheus."""
    recs, run, _, _ = cycle
    assert run(watermark=_WATERMARK) is None
    esperado = _WATERMARK.replace(tzinfo=timezone.utc).timestamp()
    assert recs["WATERMARK_TIMESTAMP"].emits == [
        ("set", {"integration_id": "42", "stream": "events"}, esperado)
    ]


def test_fluxo_sem_cursor_temporal_nao_inventa_watermark(cycle) -> None:
    recs, run, _, _ = cycle
    assert run(watermark=None) is None
    assert recs["WATERMARK_TIMESTAMP"].emits == []


def test_cadencia_e_nome_da_integracao_acompanham_o_ciclo(cycle) -> None:
    """Sem a cadência não dá para dizer se o ciclo cabe no agendamento; sem o
    nome, o painel mostra "42" e ninguém sabe de quem é."""
    recs, run, _, _ = cycle
    assert run() is None
    assert recs["STREAM_CADENCE"].emits == [
        ("set", {"vendor": "fakevendor", "stream": "events"}, 300.0)
    ]
    assert recs["INTEGRATION_INFO"].emits == [
        ("set", {
            "integration_id": "42", "vendor": "fakevendor",
            "integration_name": "Fonte Exemplo", "organization_name": "Org Exemplo",
        }, 1)
    ]


def test_fonte_push_e_integracao_inativa_nao_contam_como_ciclo(cycle) -> None:
    recs, run, state, monkeypatch = cycle
    state["push"] = True
    assert run() is None
    state["push"] = False

    class _Inativa(_Integration):
        is_active = False

    monkeypatch.setattr(_Db, "integration", _Inativa())
    assert run() is None

    assert recs["CYCLES_TOTAL"].emits == [] and recs["CYCLE_SECONDS"].emits == []


def test_metrica_quebrada_nao_derruba_a_coleta(cycle) -> None:
    recs, run, _, monkeypatch = cycle
    monkeypatch.setattr(pipeline, "CYCLES_TOTAL", _Recorder(boom=True))
    assert run() is None, "telemetria é acessória: o ciclo tem de terminar e gravar o cursor"
    assert recs["EVENTS_COLLECTED"].total(**_FLOW) == 2


def test_backfill_nao_entra_na_vazao_do_fluxo(cycle) -> None:
    """``RawProducer`` sem vendor (o backfill) grava no raw sem contar: carga
    histórica somada à série faria um fluxo parado parecer em dia."""
    recs, _, _, _ = cycle

    async def _go() -> None:
        producer = pipeline.RawProducer(42, "events", _Config(), historical=True)
        await producer.process({"id": "h0"}, "h0")
        await producer.flush()
        assert producer.events_count == 1

    asyncio.run(_go())
    assert recs["EVENTS_COLLECTED"].emits == []
