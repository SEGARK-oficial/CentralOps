"""ADR-0018, Onda 0 — nenhum evento confirmado some sem registro.

A auditoria de arquitetura de nov/2026 achou caminhos de PERDA SILENCIOSA no
desenho atual. Estes testes fixam o contrato que os fecha:

* entrega: erro não classificado do sender (conexão recusada pelo destino
  syslog que reinicia, banco fora ao ler o destino) NUNCA commita o offset —
  é transitório; e o commit só acontece com a DLQ confirmada;
* lane Celery: a última tentativa vai à DLQ, e a DLQ que falha re-tenta;
* dedupe: a claim que um ciclo morto (SIGKILL/OOM) deixou pendente é solta pelo
  próximo ciclo, em vez de descartar como "duplicado" o evento re-buscado;
* push: o dreno não apaga o buffer antes do ciclo confirmar, e buffer cheio
  recusa o lote novo em vez de apagar eventos já aceitos;
* syslog TCP: frames octet-counted colados (RFC 6587) chegam todos;
* roteamento: supressão vale só para os destinos da rota que suprimiu; rota que
  não compila retém o lote em vez de mandar tudo ao destino default.

Por convenção do projeto, este arquivo é o artefato durável da decisão.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import fakeredis.aioredis
import pytest

from backend.app.collectors import pipeline
from backend.app.collectors.dataplane import kafka_transport as kt
from backend.app.collectors.delivery import TransientDeliveryError
from backend.app.collectors.routing import CompiledRoute
from backend.app.collectors.routing.engine import route_batch
from backend.app.collectors.state import dedupe
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
    async def _dispatch(dest, batch):
        raise error

    fake = _FakeConsumer([_msg(5, "wazuh-default", [{"e": 1}])])
    dlq: list = []
    _install(monkeypatch, fake, _dispatch, dlq_capture=dlq)

    await kt.run_dispatch_consumer()

    assert fake.commits == []          # nada commitado: o lote segue no tópico
    assert len(fake.seeks) == 1        # relido com backoff
    assert dlq == []                   # ainda não esgotou


async def test_unknown_delivery_error_exhausted_is_parked(monkeypatch):
    async def _dispatch(dest, batch):
        raise ConnectionRefusedError("destino fora")

    # A mesma mensagem 3× (o fake não re-enfileira no seek): a 3ª passa do teto.
    fake = _FakeConsumer([_msg(1, "d", [{"e": 1}]) for _ in range(3)])
    dlq: list = []
    parked: list = []
    _install(monkeypatch, fake, _dispatch, dlq_capture=dlq, park_capture=parked)
    monkeypatch.setattr(kt.settings, "KAFKA_MAX_DELIVERY_ATTEMPTS", 2)

    await kt.run_dispatch_consumer()

    # Onda 4a: esgotado ESTACIONA (espera no Kafka), não vai à DLQ do Postgres.
    assert dlq == []
    assert [p["reason"] for p in parked] == ["exhausted"]
    assert len(fake.commits) == 1


@pytest.mark.parametrize("kind", ["breaker_open", "exhausted"])
async def test_park_failure_blocks_commit(monkeypatch, kind):
    from backend.app.collectors import circuit_breaker

    async def _dispatch(dest, batch):
        if kind == "breaker_open":
            raise circuit_breaker.BreakerOpen(dest)
        raise TransientDeliveryError(dest)

    n = 1 if kind == "breaker_open" else 2
    fake = _FakeConsumer([_msg(7, "d", [{"e": 1}]) for _ in range(n)])
    _install(monkeypatch, fake, _dispatch, dlq_capture=[], park_capture=[], park_ok=False)
    monkeypatch.setattr(kt.settings, "KAFKA_MAX_DELIVERY_ATTEMPTS", 1)

    await kt.run_dispatch_consumer()

    assert fake.commits == []          # estacionar não confirmou → offset NÃO avança
    assert fake.seeks                  # o lote é relido depois


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


def test_celery_last_retry_goes_to_dlq(monkeypatch):
    from backend.app.collectors import tasks

    def _boom(*a, **k):
        raise TransientDeliveryError("d")

    monkeypatch.setattr(tasks, "run_coro_blocking", _boom)
    sent: list = []
    monkeypatch.setattr(
        tasks.dispatch_to_dlq, "apply_async", lambda **kw: sent.append(kw["kwargs"])
    )
    task = SimpleNamespace(request=SimpleNamespace(retries=10), max_retries=10)

    tasks._run_dispatch_to_destination(task, "d", [{"e": 1}])   # não levanta

    assert [k["error_kind"] for k in sent] == ["exhausted"]


def test_celery_dlq_write_failure_retries(monkeypatch):
    from backend.app.collectors import delivery, tasks

    monkeypatch.setattr(delivery, "persist_batch_dlq", lambda *a, **k: False)

    class _Retry(Exception):
        pass

    with patch.object(tasks.dispatch_to_dlq, "retry", side_effect=_Retry) as retry:
        with pytest.raises(_Retry):
            tasks.dispatch_to_dlq.run([{"e": 1}], destination_id="d", error_kind="exhausted")
    assert retry.called


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
    async def go():
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        pend = dedupe.pending_set_key(42, "alerts")
        # Ciclo A reivindica 2 eventos, liquida 1 e leva SIGKILL (sem finally).
        assert await dedupe.claim(r, 42, "e1", ttl_seconds=86400, pending_set=pend)
        assert await dedupe.claim(r, 42, "e2", ttl_seconds=86400, pending_set=pend)
        await dedupe.settle_pending(r, pend, ["e1"])
        # Ciclo B (com a trava) recupera ANTES de reivindicar.
        recovered = await dedupe.recover_orphan_claims(r, 42, pend)
        e1_again = await dedupe.claim(r, 42, "e1", ttl_seconds=86400, pending_set=pend)
        e2_again = await dedupe.claim(r, 42, "e2", ttl_seconds=86400, pending_set=pend)
        await r.aclose()
        return recovered, e1_again, e2_again

    recovered, e1_again, e2_again = _run(go())
    assert recovered == 1
    assert e1_again is False   # liquidado (já saiu): continua duplicado
    assert e2_again is True    # órfão: reprocessado, não descartado


def test_duplicate_claim_does_not_enter_pending_set():
    """O SADD só acontece quando a claim é DESTE ciclo — senão a recuperação
    soltaria a claim já liquidada de outro ciclo."""

    async def go():
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await dedupe.claim(r, 1, "x", ttl_seconds=86400)                       # outro ciclo
        pend = dedupe.pending_set_key(1, "s")
        dup = await dedupe.claim(r, 1, "x", ttl_seconds=86400, pending_set=pend)
        members = await r.smembers(pend)
        await r.aclose()
        return dup, members

    dup, members = _run(go())
    assert dup is False and not members


@pytest.mark.source_only  # lê o .py; na imagem Cython o fonte não existe
def test_cycle_recovers_orphans_before_claiming():
    import inspect

    # ADR-0018 Onda 3: a recuperação é do estágio de processamento, e todo
    # chamador a faz antes do primeiro evento.
    for name in ("_run_collection_once", "process_raw_events"):
        src = inspect.getsource(getattr(pipeline, name))
        assert "processor.recover_orphan_claims()" in src, name
        assert src.index("processor.recover_orphan_claims()") < src.index("processor.process("), name
    stage = inspect.getsource(pipeline.StreamProcessor.recover_orphan_claims)
    assert "recover_orphan_claims(" in stage
    assert "pending_set=_pending_set" in inspect.getsource(pipeline.StreamProcessor.process)


# ── Push: dreno confiável e backpressure sem apagar dado aceito ─────────────


def test_push_drain_survives_abort():
    from backend.app.collectors import ingest_buffer as ib

    async def go():
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await ib.push_events(r, 3, "traffic", [{"i": i} for i in range(4)])
        first = await ib.drain_events(r, 3, "traffic")
        # ciclo falhou ou morreu: nada confirmado → o próximo dreno relê tudo
        again = await ib.drain_events(r, 3, "traffic")
        await ib.ack_drained(r, 3, "traffic")
        after_ack = await ib.drain_events(r, 3, "traffic")
        await r.aclose()
        return first, again, after_ack

    first, again, after_ack = _run(go())
    assert [e["i"] for e in first] == [0, 1, 2, 3]
    assert sorted(e["i"] for e in again) == [0, 1, 2, 3]
    assert after_ack == []


def test_push_collector_acks_only_after_full_drain_and_commit():
    from backend.app.collectors import ingest_buffer as ib
    from backend.app.collectors.vendors.push_ingest import FortiGateTrafficCollector

    async def go():
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await ib.push_events(r, 5, "traffic", [{"i": i} for i in range(3)])
        col = FortiGateTrafficCollector.__new__(FortiGateTrafficCollector)
        col.ctx = SimpleNamespace(redis=r, integration_id=5)

        gen = col.collect()
        await gen.__anext__()                 # pipeline parou no 1º evento
        await gen.aclose()
        await col.on_cycle_committed()        # não pode confirmar o dreno inteiro
        partial = await r.llen(ib.inflight_key(5, "traffic"))

        seen = [e async for e in col.collect()]
        await col.on_cycle_aborted()          # erro: mantém
        kept = await r.llen(ib.inflight_key(5, "traffic"))

        _ = [e async for e in col.collect()]
        await col.on_cycle_committed()        # sucesso: apaga
        done = await r.llen(ib.inflight_key(5, "traffic"))
        await r.aclose()
        return partial, len(seen), kept, done

    partial, seen, kept, done = _run(go())
    assert partial == 3 and seen == 3 and kept == 3 and done == 0


def test_push_full_buffer_refuses_new_batch_and_keeps_accepted():
    from backend.app.collectors import ingest_buffer as ib

    async def go():
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await ib.push_events(r, 9, "t", [{"i": i} for i in range(3)], max_len=3)
        res = await ib.push_events(r, 9, "t", [{"i": 99}], max_len=3)
        drained = await ib.drain_events(r, 9, "t")
        await r.aclose()
        return res, drained

    res, drained = _run(go())
    assert res == (0, 1)
    assert [e["i"] for e in drained] == [0, 1, 2]


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
