"""ADR-0018, Onda 0 — nenhum evento confirmado some sem registro.

A auditoria de arquitetura de nov/2026 achou caminhos de PERDA SILENCIOSA no
desenho atual. Estes testes fixam o contrato que os fecha:

* entrega: erro não classificado do sender (conexão recusada pelo destino
  syslog que reinicia, banco fora ao ler o destino) NUNCA commita o offset —
  é transitório; e o commit só acontece com a DLQ confirmada;
* dedupe: a claim de um processamento morto (SIGKILL/OOM) não descarta como
  "duplicado" o evento relido;
* push: o evento só é confirmado depois de gravado, e o que não gravou fica
  retido para a próxima tentativa em vez de sumir;
* syslog TCP: frames octet-counted colados (RFC 6587) chegam todos;
* roteamento: supressão vale só para os destinos da rota que suprimiu; rota que
  não compila retém o lote em vez de mandar tudo ao destino default.

Por convenção do projeto, este arquivo é o artefato durável da decisão. A Onda 5
tirou a lane Celery de entrega, a claim Redis por evento e a lista Redis do push;
os testes daqui que os exercitavam provam agora o mesmo contrato pelo caminho
que ficou (janela de dedupe do processor, raw com ack).
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest

from backend.app.collectors import pipeline
from backend.app.collectors.dataplane import kafka_transport as kt
from backend.app.collectors.delivery import TransientDeliveryError
from backend.app.collectors.routing import CompiledRoute
from backend.app.collectors.routing.engine import route_batch
from backend.app.collectors.state.dedupe_window import DedupeWindow
from backend.app.collectors.tests.test_dataplane_consumer import (
    _FakeConsumer,
    _install,
    _msg,
)
from backend.app.collectors.tests.test_delivery_enforcement import (
    _dcfg,
    _run_chunk_with_retry,
    _stub_circuit_breaker,
)


def _run(coro):
    return asyncio.run(coro)


# ── Entrega ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "error",
    [
        ConnectionRefusedError("destino syslog reiniciando"),
        RuntimeError("OperationalError: server closed the connection"),
    ],
)
async def test_unknown_delivery_error_never_commits(monkeypatch, error):
    """Erro desconhecido é transitório: re-tentado, e o offset só anda com a entrega
    ou o estacionamento confirmados (Ondas 0 e 4a)."""
    import threading

    stop = threading.Event()

    async def _dispatch(dest, batch):
        raise error

    fake = _FakeConsumer([_msg(5, "wazuh-default", [{"e": 1}])])
    dlq: list = []
    _install(monkeypatch, fake, _dispatch, dlq_capture=dlq)
    monkeypatch.setattr(kt.settings, "KAFKA_MAX_DELIVERY_ATTEMPTS", 2)

    from backend.app.collectors.dataplane import parked as _parked

    async def _park_refused(value, destination_id, reason):
        stop.set()
        return False

    monkeypatch.setattr(_parked, "park", _park_refused)
    await kt.run_dispatch_consumer(stop_event=stop)

    assert fake.commits == []          # nada commitado: o lote segue no tópico
    assert dlq == []                   # transitório nunca vira DLQ


async def test_unknown_delivery_error_exhausted_is_parked(monkeypatch):
    calls: list = []

    async def _dispatch(dest, batch):
        calls.append(dest)
        raise ConnectionRefusedError("destino fora")

    fake = _FakeConsumer([_msg(1, "d", [{"e": 1}])])
    dlq: list = []
    parked: list = []
    _install(monkeypatch, fake, _dispatch, dlq_capture=dlq, park_capture=parked)
    monkeypatch.setattr(kt.settings, "KAFKA_MAX_DELIVERY_ATTEMPTS", 2)

    await kt.run_dispatch_consumer()

    # Onda 4a: esgotado ESTACIONA (espera no Kafka), não vai à DLQ do Postgres.
    assert len(calls) == 3
    assert dlq == []
    assert [p["reason"] for p in parked] == ["exhausted"]
    assert len(fake.commits) == 1


@pytest.mark.parametrize("kind", ["breaker_open", "exhausted"])
async def test_park_failure_blocks_commit(monkeypatch, kind):
    import threading

    from backend.app.collectors import circuit_breaker
    from backend.app.collectors.dataplane import parked as _parked

    stop = threading.Event()

    async def _dispatch(dest, batch):
        if kind == "breaker_open":
            raise circuit_breaker.BreakerOpen(dest)
        raise TransientDeliveryError(dest)

    fake = _FakeConsumer([_msg(7, "d", [{"e": 1}])])
    _install(monkeypatch, fake, _dispatch, dlq_capture=[])
    monkeypatch.setattr(kt.settings, "KAFKA_MAX_DELIVERY_ATTEMPTS", 1)
    refused: list = []

    async def _park_refused(value, destination_id, reason):
        refused.append(reason)
        if len(refused) == 2:          # re-tentou e o broker segue recusando
            stop.set()
        return False

    monkeypatch.setattr(_parked, "park", _park_refused)
    await kt.run_dispatch_consumer(stop_event=stop)

    assert refused == [kind, kind]     # re-tenta estacionar, não desiste
    assert fake.commits == []          # estacionar não confirmou → offset NÃO avança


async def test_sender_connection_error_is_retried_then_transient():
    """O syslog deixa ``OSError`` subir; o envio trata como transitório, conta no
    breaker e, esgotado, levanta ``TransientDeliveryError`` (antes subia cru e o
    consumer commitava sem DLQ)."""
    calls = {"n": 0}

    class _Target:
        async def send_batch(self, chunk):
            calls["n"] += 1
            raise ConnectionRefusedError("manager reiniciando")

    cb = _stub_circuit_breaker()
    with pytest.raises(TransientDeliveryError):
        await _run_chunk_with_retry(
            target=_Target(),
            chunk=[{"_centralops": {"event_id": "e1"}}],
            dcfg=_dcfg(max_retries=2),
            circuit_breaker=cb,
            sleep_mock=AsyncMock(),
        )
    assert calls["n"] == 3                              # 1 + 2 retries
    assert cb.record_failure_for_config.await_count == 3


def test_unrouted_dlq_failure_holds_the_batch(monkeypatch):
    """Sem rota e sem default, a DLQ é o único destino: se ela falha, o lote falha
    ANTES de qualquer produce (o ciclo volta o cursor)."""
    from backend.app.collectors import delivery

    monkeypatch.setattr(pipeline, "_load_fallback_destination_id", lambda org: None)
    monkeypatch.setattr(pipeline, "_load_destination_residency", lambda ids: {})
    monkeypatch.setattr(pipeline, "_load_wazuh_loop_destination_ids", lambda ids: frozenset())
    monkeypatch.setattr(delivery, "persist_batch_dlq", lambda *a, **k: False)
    monkeypatch.setattr(pipeline, "_capture_outcomes", lambda *a, **k: None)

    env = {"_centralops": {"event_id": "e1", "organization_id": 7, "vendor": "x"}}
    with pytest.raises(RuntimeError, match="lote retido"):
        pipeline._enqueue_routed([env], [])


# ── Dedupe: claim órfã ──────────────────────────────────────────────────────


def test_orphan_claim_is_released_by_the_next_cycle():
    """A claim mora na janela em memória do processor (Onda 5). Um processo
    morto leva a janela junto, e quem relê o lote do Kafka (o próximo dono da
    partição) começa com a janela vazia: o evento em voo é processado, não
    descartado como "duplicado" — não há claim persistente para ficar órfã."""
    from backend.app.collectors.dataplane import processor as proc

    dying = DedupeWindow(max_ids=100, ttl_seconds=86400)
    assert dying.claim(42, "e1") and dying.claim(42, "e2")
    dying.settle(42, ["e1"])
    # SIGKILL aqui: nenhum ``finally`` roda. O novo dono da partição cria a sua.
    proc._DEDUPE_WINDOWS.pop(7, None)
    fresh = proc._window_for(7)
    assert fresh is not dying
    assert fresh.claim(42, "e2") is True   # em voo: reprocessado
    proc._DEDUPE_WINDOWS.pop(7, None)


# ── Push: confirmado só depois de gravado ───────────────────────────────────


def test_push_drain_survives_abort(monkeypatch):
    """Não há mais dreno de lista Redis: o receptor syslog grava no raw e, se o
    broker recusa, retém o lote em memória e o grava inteiro na próxima vez —
    nada se perde e nada é gravado duas vezes."""
    from backend.app.collectors.dataplane import raw as raw_mod
    from backend.app.syslog import server as srv

    written: list = []
    calls = {"n": 0}

    async def _produce(iid, stream, events, ids=None, historical=False):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("broker fora")
        written.extend(e["i"] for e in events)
        return len(events)

    monkeypatch.setattr(raw_mod, "produce_raw_async", _produce)
    rec = srv.Receiver(sources=srv.SourceTable([]), registry_has=lambda p, s: True,
                       batch_max=100, flush_ms=10, max_line=1024)
    rec._queues[(3, "traffic", "fortinet_fortigate")] = [{"i": i} for i in range(4)]

    assert _run(rec.flush()) == 0          # falhou: retido
    assert rec.pending() == 4 and written == []
    assert _run(rec.flush()) == 4          # próxima tentativa grava tudo
    assert written == [0, 1, 2, 3] and rec.pending() == 0


# ── Syslog TCP: framing RFC 6587 ─────────────────────────────────────────────


class _Receiver:
    max_line = 4096

    def __init__(self):
        self.lines: list = []
        self.stats: dict = {}

    def handle_line(self, body, **_k):
        self.lines.append(body)

    def maybe_flush_soon(self):
        return False

    def _bump(self, k):
        self.stats[k] = self.stats.get(k, 0) + 1


class _Writer:
    def get_extra_info(self, _k):
        return ("10.0.0.1", 50514)

    def close(self):
        pass


def _serve(data: bytes) -> list:
    from backend.app.syslog import server as srv

    async def go():
        reader = asyncio.StreamReader()
        reader.feed_data(data)
        reader.feed_eof()
        rc = _Receiver()
        await srv._serve_stream(reader, _Writer(), receiver=rc, port=6514, transport="tcp")
        return rc.lines

    return _run(go())


def test_octet_counting_chained_frames():
    m1, m2, m3 = b"<13>1 - a", b"<13>1 - b\ncom quebra", b"<14>1 - c"
    data = b"".join(b"%d %s" % (len(m), m) for m in (m1, m2, m3))   # sem LF entre frames
    assert _serve(data) == [m1, m2, m3]


def test_lf_framing_and_trailers_still_work():
    data = b"<13>linha um\n<13>linha dois\n9 <13>1 - x\r\n200 OK linha crua\n"
    assert _serve(data) == [b"<13>linha um", b"<13>linha dois", b"<13>1 - x", b"200 OK linha crua"]


# ── Roteamento ───────────────────────────────────────────────────────────────


def _route(rid, dest, *, priority, final):
    return CompiledRoute(
        id=rid, name=rid, priority=priority, condition={}, action="route",
        destination_ids=(dest,), is_final=final, protect_detection=False,
    )


def _ev(eid):
    return {"_centralops": {"event_id": eid, "organization_id": 7, "vendor": "x"}}


def test_suppression_scoped_to_destination():
    lake = _route("lake", "d-lake", priority=10, final=False)
    siem = _route("siem", "d-siem", priority=20, final=True)
    res = route_batch(
        [_ev("e1"), _ev("e2")], [lake, siem],
        fallback_destination_id="d-default",
        suppressed={"e1": frozenset({"siem"})},
    )
    lake_ids = [e["_centralops"]["event_id"] for e in res.sub_batches.get("d-lake", [])]
    siem_ids = [e["_centralops"]["event_id"] for e in res.sub_batches.get("d-siem", [])]
    assert lake_ids == ["e1", "e2"]          # o lago recebe o suprimido
    assert siem_ids == ["e2"]                # só a rota que suprimiu deixa de entregar
    assert res.suppressed_per_route == {"siem": 1}
    assert "d-default" not in res.sub_batches


def test_suppressed_only_route_is_reduced_not_fallback():
    siem = _route("siem", "d-siem", priority=20, final=True)
    res = route_batch(
        [_ev("e1")], [siem], fallback_destination_id="d-default",
        suppressed={"e1": frozenset({"siem"})},
    )
    assert res.sub_batches == {} and res.fallback == 0 and res.suppressed == 1


def test_bad_route_fails_closed(monkeypatch):
    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    good = SimpleNamespace(id="ok")
    bad = SimpleNamespace(id="quebrada")

    class _Repo:
        def __init__(self, *_a, **_k):
            pass

        def list_enabled_for_org(self, *_a, **_k):
            return [good, bad]

    def _compile(row):
        if row.id == "quebrada":
            raise ValueError("spec de PII corrompida")
        return row

    monkeypatch.setattr(pipeline.database, "SessionLocal", lambda: _Session())
    monkeypatch.setattr("backend.app.db.repository.RouteRepository", _Repo)
    monkeypatch.setattr(pipeline, "_compile_route_row", _compile)

    with pytest.raises(pipeline.RoutingUnavailable, match="quebrada"):
        pipeline._load_routes_for_org(7)
