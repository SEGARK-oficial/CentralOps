"""Identidade de conservação da claim de dedupe, verificada por COMPORTAMENTO.

Substitui UM guard de texto-fonte — ``test_settlement_happens_after_the_durable_
handoff``, que casava a chamada literal de ``_enqueue_dispatch`` e morreu na
primeira reformatação, sem que nada de semântico mudasse. O outro guard daquele
arquivo, ``test_every_dispatch_site_settles``, NÃO foi substituído e continua
lá: ele é um CENSO dos sites de dispatch, e censo é justamente o que este
harness não sabe fazer — ele só enxerga os sites que o ciclo ALCANÇA.

Aqui nada é lido do ``.py``. O ciclo REAL (``pipeline._run_collection_once``) é
executado contra dublês, e o que se afirma é o EFEITO observável:

  * todo ``msg_id`` reivindicado termina o ciclo entregue-e-liquidado OU solto;
  * nunca as duas coisas, nunca nenhuma;
  * a liquidação nunca precede o hand-off durável (provado invertendo o mundo:
    se o hand-off levanta, a claim PRECISA aparecer no release);
  * o release acontece ANTES do ``aclose`` do cliente Redis — depois dele o DEL
    iria contra um cliente fechado e a compensação viraria no-op silencioso.

Refactor-safety, na medida certa: como os fatos são medidos por ORDEM DE EVENTOS
numa fita, mover a liquidação para um helper, um context manager ou um decorator
continua passando; removê-la, ou movê-la para antes do dispatch, falha. Mas as
sondas são monkeypatch POR NOME (``pipeline.claim``, ``pipeline._enqueue_
dispatch``, ``dedupe.release_many``): renomear ou realocar um desses seams faz a
fita parar de registrar. O que impede isso de ficar VERDE por vacuidade é o
assert positivo que abre ``_assert_conservation`` (houve claim) e os asserts de
presença ao lado de cada assert de ordem — sem eles, fita vazia aprova tudo.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import asyncio
import contextlib
from typing import Any

import pytest

from backend.app.collectors import pipeline
from backend.app.collectors.state import dedupe


# ── Dublês ────────────────────────────────────────────────────────────────────

# Evento de fita sem payload. Mantém a forma (nome, args) dos demais para as
# leituras derivadas do _Tape continuarem podendo desempacotar qualquer evento.
_ACLOSE = ("aclose", ())


class _FakeRedis:
    """Cliente Redis do ciclo. Só ``aclose`` é chamado nele por dentro do
    pipeline (claim e release_many são sondados à parte) — e ele REGISTRA esse
    fechamento na fita: um DEL contra cliente fechado não deixa rastro que o
    ``except`` do finally torne visível, então a compensação simplesmente não
    acontece e a claim órfã só cai no TTL. Sem o registro não há como provar a
    ordem release→aclose sem voltar a ler o fonte."""

    def __init__(self, tape: "_Tape") -> None:
        self._tape = tape

    async def aclose(self) -> None:
        self._tape.closed()


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
    collector_batch_size = 2          # força >1 flush por tamanho
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


def _make_collector_cls(raw_events, boom_at=None):
    """Coletor que devolve ``raw_events``; opcionalmente levanta no i-ésimo."""

    class _Collector:
        event_type = "alert"

        def __init__(self, ctx):
            self.ctx = ctx

        async def collect(self):
            for i, ev in enumerate(raw_events):
                if boom_at is not None and i == boom_at:
                    raise RuntimeError("vendor caiu no meio da página")
                yield ev

        def extract_message_id(self, raw):
            return raw["id"]

        @staticmethod
        def watermark_at(cursor):
            return None

    return _Collector


class _Tape:
    """Fita de eventos observáveis do ciclo, na ordem em que aconteceram."""

    def __init__(self) -> None:
        self.events: list[tuple[str, tuple]] = []

    def claimed(self, msg_id):
        self.events.append(("claim", (msg_id,)))

    def dispatched(self, ids):
        self.events.append(("dispatch", tuple(ids)))

    def released(self, ids):
        self.events.append(("release", tuple(sorted(ids))))

    def closed(self):
        # Entra na MESMA fita do claim/release porque o que se quer provar é a
        # ORDEM entre eles, não o fato isolado de fechar o cliente.
        self.events.append(_ACLOSE)

    # ── leituras derivadas ────────────────────────────────────────────
    @property
    def claims(self) -> list[str]:
        return [e[1][0] for e in self.events if e[0] == "claim"]

    @property
    def delivered(self) -> list[str]:
        return [i for e in self.events if e[0] == "dispatch" for i in e[1]]

    @property
    def released_ids(self) -> list[str]:
        return [i for e in self.events if e[0] == "release" for i in e[1]]


@pytest.fixture()
def harness(monkeypatch):
    """Monta o ciclo real com dublês e devolve (tape, run)."""
    tape = _Tape()

    monkeypatch.setattr(
        "backend.app.collectors.celery_app.get_worker_redis", lambda: _FakeRedis(tape)
    )
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
    monkeypatch.setattr(
        pipeline.sample_reservoir, "push", lambda *a, **k: _noop_coro()
    )

    # ── as três sondas do invariante ──────────────────────────────────
    async def _claim(redis, integration_id, msg_id, **kw):
        tape.claimed(msg_id)
        return True

    monkeypatch.setattr(pipeline, "claim", _claim)

    async def _release_many(redis, integration_id, ids):
        tape.released(ids)
        return len(list(ids))

    monkeypatch.setattr(dedupe, "release_many", _release_many)

    def run(raw_events, *, boom_at=None, dispatch_raises=False):
        reg = _FakeRegistration()
        reg.collector_cls = _make_collector_cls(raw_events, boom_at=boom_at)
        monkeypatch.setattr(pipeline, "registry_get", lambda p, s: reg)

        def _dispatch(batch, routes=None, **kw):
            ids = [e["_centralops"]["event_id"] for e in batch]
            if dispatch_raises:
                # NÃO registra entrega: o hand-off falhou.
                raise RuntimeError("broker fora do ar")
            tape.dispatched(ids)

        monkeypatch.setattr(pipeline, "_enqueue_dispatch", _dispatch)

        coro = pipeline._run_collection_once(integration_id=42, stream="alerts")
        try:
            asyncio.run(coro)
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


def _events(n: int):
    return [{"id": f"m{i}", "payload": "x"} for i in range(n)]


# ── O invariante ──────────────────────────────────────────────────────────────


def _assert_conservation(tape: _Tape) -> None:
    """Toda claim ou foi entregue-e-liquidada, ou foi solta. Nunca as duas."""
    claimed = set(tape.claims)
    delivered = set(tape.delivered)
    released = set(tape.released_ids)

    assert claimed, "o harness não reivindicou nada — teste vazio, não verde"
    orfas = claimed - delivered - released
    assert not orfas, (
        f"claims reivindicadas, NÃO entregues e NÃO soltas: {sorted(orfas)} — "
        "perda silenciosa: o retry vai descartá-las como duplicadas"
    )
    duplas = delivered & released
    assert not duplas, (
        f"claims entregues E soltas: {sorted(duplas)} — a liquidação sumiu do "
        "site de dispatch e o evento será reentregue"
    )


def test_happy_path_every_claim_is_delivered_and_settled(harness):
    """Ciclo sem falha: tudo entregue, nada solto (release é no-op)."""
    tape, run = harness
    assert run(_events(5)) is None

    assert tape.claims == ["m0", "m1", "m2", "m3", "m4"]
    assert sorted(tape.delivered) == ["m0", "m1", "m2", "m3", "m4"]
    _assert_conservation(tape)
    # nada não-liquidado sobrou → o release nem chega a rodar com ids.
    assert tape.released_ids == []


def test_terminal_drain_settles_the_partial_batch(harness):
    """1 evento só: nunca atinge batch_size nem o gatilho de tempo. O dreno
    TERMINAL precisa entregar E liquidar — senão o finally solta à toa e o
    próximo ciclo reprocessa (duplicata)."""
    tape, run = harness
    assert run(_events(1)) is None

    assert tape.delivered == ["m0"]
    assert tape.released_ids == [], (
        "o dreno terminal não liquidou: a claim de um evento JÁ ENTREGUE foi "
        "solta e o evento será reentregue no próximo ciclo"
    )
    _assert_conservation(tape)


def test_odd_tail_after_size_flush_is_still_settled(harness):
    """3 eventos com batch_size=2: um flush por tamanho + um dreno terminal.
    Cobre os DOIS sites de dispatch num único ciclo."""
    tape, run = harness
    assert run(_events(3)) is None

    dispatches = [e[1] for e in tape.events if e[0] == "dispatch"]
    assert dispatches == [("m0", "m1"), ("m2",)], (
        "esperados 2 hand-offs (flush por tamanho + dreno terminal)"
    )
    assert tape.released_ids == []
    _assert_conservation(tape)


def test_mid_cycle_failure_releases_only_the_unsettled(harness):
    """O vendor cai depois de 3 eventos (1 lote entregue + 1 parcial em voo).
    O entregue fica liquidado; só o parcial é solto."""
    tape, run = harness
    exc = run(_events(5), boom_at=3)
    assert isinstance(exc, RuntimeError)

    assert sorted(tape.delivered) == ["m0", "m1"]
    assert tape.released_ids == ["m2"], (
        "o finally precisa soltar EXATAMENTE o não-liquidado"
    )
    _assert_conservation(tape)


def test_settlement_never_precedes_the_durable_handoff(harness):
    """A prova de ORDEM, sem ler o fonte: se o hand-off LEVANTA, nenhum id pode
    ter sido liquidado. Liquidar antes do dispatch converteria falha de enqueue
    em perda silenciosa — e é exatamente isto que falharia aqui."""
    tape, run = harness
    exc = run(_events(4), dispatch_raises=True)
    assert isinstance(exc, RuntimeError)

    assert tape.delivered == [], "o dublê de dispatch levantou; nada foi entregue"
    assert tape.released_ids == tape.claims, (
        "hand-off falhou e alguma claim foi liquidada assim mesmo — a "
        "liquidação precede o dispatch e o retry descartará o evento como "
        f"duplicado (reivindicadas={tape.claims} soltas={tape.released_ids})"
    )
    _assert_conservation(tape)


def test_release_happens_before_the_redis_client_is_closed(harness):
    """A outra prova de ORDEM, também sem ler o fonte: soltar DEPOIS do
    ``aclose`` mandaria o DEL contra um cliente fechado. Esse modo de falha é
    mudo — o release está dentro de um ``try/except`` que só loga, então a
    compensação viraria no-op e a claim órfã ficaria de pé até o TTL, sem
    exceção, sem métrica. Pior que não existir, porque parece que existe.
    """
    tape, run = harness
    # Ciclo COM falha no meio: é o único em que o finally tem algo a soltar.
    exc = run(_events(4), dispatch_raises=True)
    assert isinstance(exc, RuntimeError)

    # ANTI-VACUIDADE, obrigatório antes do assert de ordem: os dois `index`
    # abaixo comparam posições de eventos que podem simplesmente não estar na
    # fita. Se o dublê parar de registrar (ou o ciclo parar de fechar o
    # cliente), o teste tem de FALHAR aqui, não passar por lista vazia.
    assert _ACLOSE in tape.events, (
        "o ciclo não fechou o cliente Redis — a sonda de ordem ficou cega e "
        "este guard não estaria provando nada"
    )
    assert tape.released_ids, (
        "nenhuma claim foi solta neste ciclo falho — sem release não há ordem a "
        "verificar; o cenário deixou de exercitar a compensação"
    )

    i_release = next(i for i, ev in enumerate(tape.events) if ev[0] == "release")
    i_aclose = tape.events.index(_ACLOSE)
    assert i_release < i_aclose, (
        "o release das claims veio DEPOIS do aclose: o DEL vai contra um "
        "cliente fechado e a compensação vira no-op silencioso — mova o bloco "
        "de release para antes do fechamento do cliente no finally"
    )


def test_release_uses_the_batch_api_not_one_roundtrip_per_id(harness):
    """R2: a compensação é UMA chamada em lote, não N. Um DEL por claim num
    ciclo de 10k eventos travaria o worker justo no caminho de falha."""
    tape, run = harness
    run(_events(5), boom_at=3)

    releases = [e for e in tape.events if e[0] == "release"]
    assert len(releases) == 1 and releases[0][1] == ("m2",)
