"""Fase 0 da captura ao vivo: o tap não pode derrubar nem atrasar a coleta.

O tap grava com um cliente Redis SÍNCRONO, e o tap de roteamento roda DENTRO do
event loop da coleta. O ``try/except`` best-effort que já existia protege contra
EXCEÇÃO, não contra LATÊNCIA — e o cliente não tinha ``socket_timeout`` nenhum.
Um Redis lento (não caído) pendurava a coleta inteira; o ciclo estourava o
soft-timeout do Celery, cuja consequência documentada neste repo é reversão de
cursor, isto é, perda de janela de coleta.

Cobre as quatro correções da fase:
  * timeout no cliente do tap (o defeito era AUSÊNCIA de configuração);
  * breaker de 3 falhas → 30 s cego, para o timeout não virar uma parada
    determinística e repetida;
  * batelamento dos desfechos de roteamento, que eram 1 round-trip POR EVENTO;
  * teto de bytes por entrada, que não existia — só ``detail`` era truncado.
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import pytest

from backend.app.collectors import capture_session as cs


@pytest.fixture(autouse=True)
def _clean_breaker():
    cs.reset_tap_breaker()
    cs.reset_session_cache()
    yield
    cs.reset_tap_breaker()
    cs.reset_session_cache()


# ── timeout do cliente ────────────────────────────────────────────────


def test_sync_client_has_socket_timeout(monkeypatch) -> None:
    """Parece bobo e é o teste mais valioso do arquivo: o defeito era a
    AUSÊNCIA de configuração, e ausência não falha em teste nenhum."""
    captured: dict = {}

    class _FakeRedisModule:
        @staticmethod
        def from_url(url, **kwargs):
            captured.update(kwargs)
            return object()

    monkeypatch.setattr(cs, "_sync_client", None)
    monkeypatch.setattr(cs, "_sync_client_pid", None)
    monkeypatch.setitem(__import__("sys").modules, "redis", _FakeRedisModule)

    cs._sync_redis()

    assert captured["socket_timeout"] == 0.25
    assert captured["socket_connect_timeout"] == 0.25
    # Retry dobraria a janela de bloqueio dentro do event loop.
    assert captured["retry_on_timeout"] is False


# ── breaker ───────────────────────────────────────────────────────────


def test_breaker_opens_after_three_consecutive_failures() -> None:
    assert cs.likely_no_session(7) is False
    for _ in range(2):
        cs._tap_failed()
    assert cs._tap_blind() is False, "não pode abrir antes do limiar"

    cs._tap_failed()
    assert cs._tap_blind() is True
    # Enquanto cego, a sonda diz "não há sessão" para TODO org — é o que faz o
    # caller pular o hop de thread-pool e o round-trip.
    assert cs.likely_no_session(7) is True
    assert cs.likely_no_session(999) is True


def test_success_resets_the_failure_counter() -> None:
    cs._tap_failed()
    cs._tap_failed()
    cs._tap_ok()
    cs._tap_failed()
    assert cs._tap_blind() is False, "falhas não-consecutivas não abrem o breaker"


def test_breaker_counts_one_opening_not_one_per_failure(monkeypatch) -> None:
    """Sob Redis morto, um WARNING por falha seria milhares de linhas/s no
    caminho de coleta. O contador conta ABERTURAS."""
    seen: list = []

    class _Counter:
        def inc(self, *a, **k):
            seen.append(1)

    from backend.app.collectors import metrics

    monkeypatch.setattr(metrics, "CAPTURE_TAP_DISABLED", _Counter())
    for _ in range(10):
        cs._tap_failed()
    assert len(seen) == 1


def test_record_sync_is_noop_while_blind(monkeypatch) -> None:
    """``sessions=`` pré-resolvido NÃO protege — o LPUSH aconteceria mesmo."""
    called = []

    def _boom():
        called.append(1)
        raise AssertionError("não pode tocar o Redis com o breaker aberto")

    monkeypatch.setattr(cs, "_sync_redis", _boom)
    for _ in range(3):
        cs._tap_failed()

    cs.record_sync(
        [{"_centralops": {"vendor": "sophos", "organization_id": 7}}],
        7,
        sessions=[{"id": "s1", "vendor": "", "ring_size": "10", "expires_at": "9e9"}],
    )
    assert called == []


def test_active_sessions_sync_never_raises_on_redis_failure(monkeypatch) -> None:
    class _Broken:
        def smembers(self, *a, **k):
            raise TimeoutError("redis lento")

    monkeypatch.setattr(cs, "_sync_redis", lambda: _Broken())
    assert cs.active_sessions_sync(7) == []
    assert cs._tap_fails == 1


# ── teto de bytes por entrada ─────────────────────────────────────────


def _entry(raw: dict, normalized: dict | None = None) -> dict:
    return {
        "event": {
            "_centralops": {"vendor": "x", "event_id": "e1", "organization_id": 7},
            "normalized": normalized if normalized is not None else {"a": 1},
            "raw": raw,
        },
        "vendor": "x",
        "captured_at": 1.0,
        "outcome": "delivered",
    }


def test_small_payload_passes_through_without_clip_metadata() -> None:
    text, meta = cs._clip_for_ring(_entry({"msg": "ok"}))
    assert meta is None
    assert json.loads(text)["event"]["raw"] == {"msg": "ok"}


def test_serialization_keeps_utf8_raw_not_escaped() -> None:
    """O stdlib escapa não-ASCII e infla ~1,29× — enquanto a contabilidade de
    bytes do resto do sistema usa orjson com UTF-8 bruto."""
    text, _ = cs._clip_for_ring(_entry({"msg": "ação coração"}))
    assert "ação" in text
    assert "\\u00e7" not in text


def test_huge_payload_is_clipped_and_stays_valid_json() -> None:
    """A asserção anti-JSON-inválido. O repo tem o incidente documentado:
    cortar a string JÁ SERIALIZADA quebrou o reprocesso de quarentena."""
    text, meta = cs._clip_for_ring(_entry({"blob": "ção" * 200_000}))

    assert len(text.encode("utf-8")) <= cs.MAX_ENTRY_BYTES
    parsed = json.loads(text)  # não pode levantar
    assert meta is not None
    assert meta["original_bytes"] > cs.MAX_ENTRY_BYTES
    # A identidade SOBREVIVE ao corte — sem ela o registro deixa de ser juntável.
    assert parsed["event"]["_centralops"]["event_id"] == "e1"


def test_clip_preserves_structure_instead_of_slicing_json() -> None:
    text, _ = cs._clip_for_ring(_entry({"a": "x" * 100_000, "b": "curto"}))
    parsed = json.loads(text)
    # A chave irmã continua existindo: reduziu a ESTRUTURA, não cortou o texto.
    assert parsed["event"]["raw"]["b"] == "curto"
    assert len(parsed["event"]["raw"]["a"]) < 100_000


def test_accented_clip_decodes_cleanly() -> None:
    """``_reduce_structure`` corta por CARACTERE. Nunca pode cortar codepoint
    no meio nem produzir bytes inválidos."""
    text, _ = cs._clip_for_ring(_entry({"m": "ção" * 50_000}))
    text.encode("utf-8").decode("utf-8")  # não pode levantar
    json.loads(text)


def test_entries_for_attaches_clip_metadata_outside_event() -> None:
    """``_capture`` vai FORA de ``event``: o export mascara ``entry["event"]``,
    e metadado do tap não pode ser confundido com dado do vendor."""
    meta = {"id": "s1", "vendor": "", "ring_size": "10", "expires_at": "9e9"}
    ev = {
        "_centralops": {"vendor": "x", "organization_id": 7},
        "raw": {"blob": "z" * 300_000},
    }
    entries = cs._entries_for(meta, [ev], 1.0, "delivered", None, None)

    assert len(entries) == 1
    parsed = json.loads(entries[0])
    assert "_capture" in parsed
    assert "_capture" not in parsed["event"]
    assert parsed["_capture"]["original_bytes"] > cs.MAX_ENTRY_BYTES


# ── batelamento dos desfechos de roteamento ───────────────────────────


class _CountingPipe:
    def __init__(self, sink):
        self._sink = sink

    def lpush(self, key, *entries):
        self._sink["entries"].extend(entries)

    def ltrim(self, *a, **k):
        pass

    def expire(self, *a, **k):
        pass

    def hincrby(self, *a, **k):
        pass

    def execute(self):
        pass


class _CountingRedis:
    """Conta chamadas a ``pipeline()`` — cada uma é um round-trip SÍNCRONO
    dentro do event loop da coleta."""

    def __init__(self):
        self.pipelines = 0
        self.sink = {"entries": []}

    def pipeline(self):
        self.pipelines += 1
        return _CountingPipe(self.sink)


def test_routing_outcomes_are_batched_not_one_call_per_event(monkeypatch) -> None:
    """Um lote de 200 eventos dropados em 3 rotas emitia até 200 round-trips
    bloqueantes. Agrupado, são no máximo 1 por (outcome, chave de atribuição)."""
    from backend.app.collectors import pipeline as pl

    fake = _CountingRedis()
    monkeypatch.setattr(cs, "_sync_redis", lambda: fake)
    sessions = [{"id": "s1", "vendor": "", "ring_size": "10000", "expires_at": "9e9"}]
    monkeypatch.setattr(cs, "active_sessions_sync", lambda *a, **k: sessions)

    def _env(i):
        return {"_centralops": {"vendor": "x", "organization_id": 7, "event_id": f"e{i}"}}

    class _Result:
        dropped_events = [(_env(i), f"rota-{i % 3}") for i in range(200)]
        unrouted_events = []
        loop_blocked_events = []
        residency_blocked_events = []
        sampled_events = []

    pl._capture_outcomes(7, _Result())

    assert fake.pipelines <= 3, f"esperava <= 3 round-trips, veio {fake.pipelines}"
    assert len(fake.sink["entries"]) == 200, "nenhum evento pode se perder no agrupamento"


def test_batching_preserves_per_event_route_attribution(monkeypatch) -> None:
    """O teste que impede a "otimização" de agrupar por sessão.

    ``_entries_for`` aplica ``route_id`` como ESCALAR a todas as entradas do
    lote — agrupar por qualquer chave mais grossa gravaria todos os eventos com
    o MESMO route_id, corrompendo o campo que responde "em qual rota bateu".
    """
    from backend.app.collectors import pipeline as pl

    fake = _CountingRedis()
    monkeypatch.setattr(cs, "_sync_redis", lambda: fake)
    sessions = [{"id": "s1", "vendor": "", "ring_size": "10000", "expires_at": "9e9"}]
    monkeypatch.setattr(cs, "active_sessions_sync", lambda *a, **k: sessions)

    def _env(i):
        return {"_centralops": {"vendor": "x", "organization_id": 7, "event_id": f"e{i}"}}

    class _Result:
        dropped_events = [(_env(i), f"rota-{i % 3}") for i in range(30)]
        unrouted_events = []
        loop_blocked_events = []
        residency_blocked_events = []
        sampled_events = []

    pl._capture_outcomes(7, _Result())

    by_event = {}
    for raw in fake.sink["entries"]:
        d = json.loads(raw)
        by_event[d["event"]["_centralops"]["event_id"]] = d["route_id"]

    assert len(by_event) == 30
    for i in range(30):
        assert by_event[f"e{i}"] == f"rota-{i % 3}", "atribuição por evento corrompida"


def test_sampled_events_group_by_destination_and_route(monkeypatch) -> None:
    from backend.app.collectors import pipeline as pl

    fake = _CountingRedis()
    monkeypatch.setattr(cs, "_sync_redis", lambda: fake)
    sessions = [{"id": "s1", "vendor": "", "ring_size": "10000", "expires_at": "9e9"}]
    monkeypatch.setattr(cs, "active_sessions_sync", lambda *a, **k: sessions)

    def _env(i):
        return {"_centralops": {"vendor": "x", "organization_id": 7, "event_id": f"e{i}"}}

    class _Result:
        dropped_events = []
        unrouted_events = []
        loop_blocked_events = []
        residency_blocked_events = []
        # 40 eventos em 2 destinos × 2 rotas = 4 grupos
        sampled_events = [(_env(i), f"d{i % 2}", f"r{i % 2}") for i in range(40)]

    pl._capture_outcomes(7, _Result())

    assert fake.pipelines <= 4
    pairs = set()
    for raw in fake.sink["entries"]:
        d = json.loads(raw)
        pairs.add((d["destination_id"], d["route_id"]))
    assert pairs == {("d0", "r0"), ("d1", "r1")}
