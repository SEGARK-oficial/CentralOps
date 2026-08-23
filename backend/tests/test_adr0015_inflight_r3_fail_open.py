"""R3 (ADR-0015) verificada por COMPORTAMENTO: o detector nunca é porteiro.

O comentário no bloco de classificação em voo de ``pipeline.py`` promete que
"nada aqui tem ``continue``, ``return`` ou mutação do envelope" — e até agora
essa promessa era só o comentário. O irmão da promessa (o enriquecimento) tem
teste; este não tinha. Um ``return``/``continue``/``raise`` acrescentado ali por
engano passaria verde, e o sintoma em produção seria PERDA SILENCIOSA DE EVENTO
do cliente: o dado não chega ao SIEM, nada levanta, nada conta.

O que se afirma aqui é a CONTAGEM de eventos que saem pelo hand-off de dispatch,
com o detector quebrado nos três pontos em que ele pode quebrar:

  * o matcher levantando (``evaluate_ruleset``);
  * o acumulador levantando (``InflightAccumulator.add``);
  * o flush levantando (``flush_inflight``, chamado no ``finally`` do ciclo).

Nada é lido do ``.py`` — sem marker ``source_only`` de propósito, porque na
imagem Cython ``pipeline`` e ``runtime`` são ``.so`` e é exatamente lá que uma
regressão dessas custaria caro.

ANTI-VACUIDADE, o risco real deste arquivo: se o detector não RODAR (ruleset
vazio ⇒ ``_inflight_acc is None`` ⇒ o bloco inteiro é pulado), todo teste de
"quebrei o detector e o evento sobreviveu" fica verde sem provar nada. Por isso
cada teste conta as invocações da sonda que deveria ter explodido, e há um
controle positivo (``test_the_detector_really_runs_in_this_harness``) que exige
o caminho feliz completo antes de qualquer cenário de falha valer.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import asyncio
import contextlib
import logging
from typing import Any

import pytest

from backend.app.collectors import pipeline
from backend.app.collectors.inflight import matcher as matcher_mod
from backend.app.collectors.inflight import runtime as runtime_mod
from backend.app.collectors.inflight.matcher import (
    CompiledInflightRule,
    CompiledRuleSet,
)
from backend.app.collectors.state import dedupe


# ── Dublês do ciclo (mesma forma do harness de conservação de claim) ─────────


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
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def scalar(self, *a, **k):
        return _FakeIntegration()

    def expunge(self, *a, **k):
        return None


class _FakeConfig:
    collector_batch_size = 2          # força >1 hand-off por ciclo
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


class _FakeCursorStore:
    def __init__(self, *a, **k):
        pass

    async def load(self, *a, **k):
        return {}

    async def save(self, *a, **k):
        return None


class _FakeRegistration:
    refresh_fn = None
    collector_cls: Any = None


def _acoro(value):
    async def _f(*a, **k):
        return value
    return _f


async def _noop_coro():
    return None


@contextlib.asynccontextmanager
async def _null_session():
    yield None


def _make_collector_cls(raw_events):
    class _Collector:
        event_type = "alert"

        def __init__(self, ctx):
            self.ctx = ctx

        async def collect(self):
            for ev in raw_events:
                yield ev

        def extract_message_id(self, raw):
            return raw["id"]

        @staticmethod
        def watermark_at(cursor):
            return None

    return _Collector


def _events(n: int) -> list[dict]:
    return [{"id": f"m{i}", "payload": "x"} for i in range(n)]


_REGRA = CompiledInflightRule(
    rule_id=77,
    name="regra-de-teste",
    severity_id=4,
    suppression_window_seconds=3600,
    group_by_path=None,
    clauses=(),
)
#: Ruleset NÃO vazio: é a única forma de ``_inflight_acc`` deixar de ser
#: ``None`` e o bloco de classificação passar a ser executado por evento. Com
#: ruleset vazio este arquivo inteiro passaria por vacuidade.
_RULESET = CompiledRuleSet(rules=(_REGRA,), share_paths=False)


class _Sonda:
    """Contadores das três chamadas que R3 protege. CONTAGEM, e não presença:
    provar que "o matcher explodiu e o evento sobreviveu" exige saber que ele
    explodiu em TODOS os eventos, não em um."""

    def __init__(self) -> None:
        self.dispatched: list[str] = []
        self.matcher_calls = 0
        self.add_calls = 0
        self.flush_calls = 0
        self.claimed: list[str] = []
        self.released: list[str] = []
        self.acumuladores: list[Any] = []


@pytest.fixture()
def harness(monkeypatch):
    """Monta o ciclo REAL contra dublês. Devolve ``(sonda, run)``."""
    sonda = _Sonda()

    monkeypatch.setattr(
        "backend.app.collectors.celery_app.get_worker_redis", lambda: _FakeRedis()
    )
    monkeypatch.setattr(pipeline.database, "SessionLocal", lambda: _FakeDb())
    monkeypatch.setattr(pipeline, "_load_routes_for_org", lambda oid: [])
    monkeypatch.setattr(pipeline, "registry_has", lambda p, s: True)
    monkeypatch.setattr(pipeline, "_headers_for", lambda *a, **k: {})
    monkeypatch.setattr(pipeline, "_load_collection_filters", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "get_collector_config", _acoro(_FakeConfig()))
    monkeypatch.setattr(pipeline, "get_or_refresh_token", _acoro("tok"))
    monkeypatch.setattr(pipeline, "_load_current_mapping", lambda *a, **k: (1, [], "v2"))
    monkeypatch.setattr(pipeline, "CursorStore", _FakeCursorStore)
    monkeypatch.setattr(pipeline, "RedisRateLimiter", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "DomainLimiter", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_aiohttp_session", _null_session)
    monkeypatch.setattr(pipeline, "default_engine", _FakeEngine())
    monkeypatch.setattr(
        pipeline, "build_envelope",
        lambda raw, out, ctx, vendor_msg_id=None: {
            "_centralops": {
                "event_id": vendor_msg_id,
                "organization_id": 7,
                "customer_id": 7,
            }
        },
    )
    monkeypatch.setattr(pipeline, "likely_no_session", lambda oid: True)
    monkeypatch.setattr(pipeline, "_record_source_ingested", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.drift, "should_capture", lambda *a, **k: False)
    monkeypatch.setattr(pipeline.sample_reservoir, "push", lambda *a, **k: _noop_coro())

    async def _claim(redis, integration_id, msg_id, **kw):
        sonda.claimed.append(msg_id)
        return True

    monkeypatch.setattr(pipeline, "claim", _claim)

    async def _release_many(redis, integration_id, ids):
        sonda.released.extend(sorted(ids))
        return len(list(ids))

    monkeypatch.setattr(dedupe, "release_many", _release_many)

    # ── o detector, LIGADO: ruleset não vazio ⇒ o bloco roda por evento ────
    monkeypatch.setattr(
        runtime_mod, "load_inflight_rules_for_org", lambda oid: _RULESET
    )

    def run(
        raw_events,
        *,
        matcher_raises: bool = False,
        add_raises: bool = False,
        flush_raises: bool = False,
        dispatch_raises: bool = False,
    ):
        def _evaluate(envelope, ruleset):
            sonda.matcher_calls += 1
            if matcher_raises:
                raise RuntimeError("regra compilada com cláusula venenosa")
            return (_REGRA,)

        monkeypatch.setattr(matcher_mod, "evaluate_ruleset", _evaluate)

        class _AccSonda(runtime_mod.InflightAccumulator):
            def __init__(self) -> None:
                super().__init__()
                sonda.acumuladores.append(self)

            def add(self, *a, **k):
                sonda.add_calls += 1
                if add_raises:
                    raise RuntimeError("acumulador quebrado")
                return super().add(*a, **k)

        monkeypatch.setattr(runtime_mod, "InflightAccumulator", _AccSonda)

        async def _flush(acc, organization_id):
            sonda.flush_calls += 1
            if flush_raises:
                raise RuntimeError("Postgres fora no fim do ciclo")

        monkeypatch.setattr(runtime_mod, "flush_inflight", _flush)

        reg = _FakeRegistration()
        reg.collector_cls = _make_collector_cls(raw_events)
        monkeypatch.setattr(pipeline, "registry_get", lambda p, s: reg)

        def _dispatch(batch, routes=None, **kw):
            if dispatch_raises:
                raise RuntimeError("broker fora do ar")
            sonda.dispatched.extend(e["_centralops"]["event_id"] for e in batch)

        monkeypatch.setattr(pipeline, "_enqueue_dispatch", _dispatch)

        try:
            asyncio.run(pipeline._run_collection_once(integration_id=42, stream="alerts"))
            return None
        except Exception as exc:  # noqa: BLE001 — o teste inspeciona a sonda
            return exc

    return sonda, run


# ── 0. Controle POSITIVO: sem ele todo o resto é vacuidade ──────────────────


def test_the_detector_really_runs_in_this_harness(harness):
    """Se o bloco de classificação não for alcançado, quebrá-lo não prova nada.
    Este teste é a pré-condição de todos os outros: o matcher é chamado UMA VEZ
    POR EVENTO, o acumulador recebe o match, o flush roda 1x no fim — e os 5
    eventos saem."""
    sonda, run = harness
    assert run(_events(5)) is None

    assert sonda.matcher_calls == 5, (
        "o matcher não foi chamado por evento — o ruleset chegou vazio e os "
        "cenários de falha abaixo estariam medindo um bloco que nem executa"
    )
    assert sonda.add_calls == 5
    assert sonda.flush_calls == 1, "o flush é ÚNICO e roda no finally do ciclo"
    assert sonda.dispatched == ["m0", "m1", "m2", "m3", "m4"]


# ── 1. As três formas de quebrar o detector ─────────────────────────────────


def test_a_raising_matcher_does_not_drop_a_single_event(harness):
    """R3: o matcher explode em TODOS os eventos e a contagem entregue não se
    mexe. Um ``continue`` acrescentado no ``except`` do bloco — a regressão
    plausível, porque parece limpeza — derrubaria os 5 para 0 aqui."""
    sonda, run = harness
    assert run(_events(5), matcher_raises=True) is None, (
        "a exceção do matcher escapou do ciclo de coleta"
    )

    assert sonda.matcher_calls == 5, "sem isto o assert abaixo passa por vacuidade"
    assert sonda.dispatched == ["m0", "m1", "m2", "m3", "m4"], (
        "evento perdido com o matcher quebrado — o detector virou porteiro"
    )


def test_a_raising_accumulator_does_not_drop_a_single_event(harness):
    """O outro lado do mesmo ``try``: o matcher devolve match e é o acumulador
    que explode. Cobre a regressão de quem acha que "só o matcher é arriscado" e
    move o ``acc.add`` para fora da rede."""
    sonda, run = harness
    assert run(_events(5), add_raises=True) is None

    assert sonda.add_calls == 5
    assert sonda.dispatched == ["m0", "m1", "m2", "m3", "m4"]


def test_a_raising_flush_does_not_drop_a_single_event(harness):
    """O flush roda no ``finally`` do ciclo. Sem o ``try`` que o envolve, a
    exceção dele SUBSTITUIRIA a exceção original do ciclo (ou criaria uma onde
    não havia) e a task inteira falharia — com os eventos já entregues, mas com
    o ciclo marcado como erro e o retry reprocessando."""
    sonda, run = harness
    assert run(_events(5), flush_raises=True) is None, (
        "a exceção do flush escapou do finally do ciclo de coleta"
    )

    assert sonda.flush_calls == 1
    assert sonda.dispatched == ["m0", "m1", "m2", "m3", "m4"]


def test_a_raising_flush_never_swallows_the_unsettled_claims(harness):
    """O modo de falha caro, e o menos óbvio: o release das claims não
    liquidadas vem DEPOIS do flush no mesmo ``finally``. Um flush que levantasse
    puralaria o release — e claim de pé até o TTL significa que o retry descarta
    o evento como duplicado. Perda silenciosa de EVENTO causada pelo DETECTOR,
    que é precisamente o que R3 proíbe.

    Cenário: hand-off falha (há o que soltar) E o flush explode.

    A comparação é contra as claims REIVINDICADAS, não contra os 4 eventos: o
    primeiro hand-off levanta e o ciclo aborta ali, então m2/m3 nunca chegam a
    ser reivindicados. Fixar ``["m0".."m3"]`` faria este teste medir o tamanho
    do lote em vez do invariante."""
    sonda, run = harness
    exc = run(_events(4), flush_raises=True, dispatch_raises=True)
    assert isinstance(exc, RuntimeError), "a falha do broker tem de sair do ciclo"

    assert sonda.flush_calls == 1, (
        "o flush nem rodou — sem ele não há exceção a engolir e este guard "
        "estaria verde sem exercitar nada"
    )
    assert sonda.claimed, "nada foi reivindicado — não há compensação a verificar"
    assert sonda.dispatched == [], "o dublê de dispatch levantou; nada foi entregue"
    assert sonda.released == sonda.claimed, (
        "claims NÃO soltas com o flush quebrado: a exceção do detector pulou a "
        "compensação e o retry vai descartar os eventos como duplicados "
        f"(reivindicadas={sonda.claimed} soltas={sonda.released})"
    )


# ── 2. Fail-open não é fail-SILENT ──────────────────────────────────────────


def test_the_matcher_failure_is_counted_once_per_event_and_logged_once(
    harness, caplog: pytest.LogCaptureFixture
):
    """Sobreviver não basta: R3 exige que a falha seja CONTADA, senão o
    operador vê 0 detecções e nenhum sinal de por quê. E o log é rate-limited
    por ciclo — um evento ruim repetido não pode trocar degradação de detecção
    por amplificação de escrita no log."""
    sonda, run = harness
    with caplog.at_level(logging.ERROR):
        assert run(_events(5), matcher_raises=True) is None

    (acc,) = sonda.acumuladores
    assert acc.errors.get("matcher") == 5, (
        f"a falha do matcher não foi contada por evento: {acc.errors}"
    )
    # Filtrado pelo logger E pelo prefixo da mensagem: o ciclo tem outros
    # subsistemas best-effort (o enriquecimento, contra os mesmos dublês) que
    # também logam em ERROR. Contar tudo mediria o ruído dos vizinhos e este
    # guard quebraria por motivo errado — ou, pior, passaria por motivo errado.
    do_inflight = [
        r for r in caplog.records
        if r.name == pipeline.logger.name and r.getMessage().startswith("inflight:")
    ]
    assert len(do_inflight) == 1, (
        f"o log do matcher saiu {len(do_inflight)}x num ciclo de 5 eventos — o "
        "rate-limit por ciclo caiu e uma regra ruim troca degradação de "
        "detecção por amplificação de escrita no log"
    )
