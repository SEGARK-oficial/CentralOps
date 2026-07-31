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
def _clean_module_state(monkeypatch):
    # ``_append_sha`` é estado de MÓDULO, como o breaker: o SHA é por servidor
    # Redis, e um fake diferente por teste invalida o anterior. Em produção o
    # caso equivalente (reconectar em outro servidor) é coberto pelo ramo
    # NOSCRIPT de ``_append_entries``.
    monkeypatch.setattr(cs, "_append_sha", None)
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


# ── orçamento de bytes RESIDENTES ─────────────────────────────────────


class _LuaRedis:
    """Fake com LIST + HASH suficiente para exercitar o script de append.

    Implementa o script em Python com a MESMA semântica, porque o objetivo do
    teste é a CONTABILIDADE (residência vs. cumulativo), não o dialeto Lua.
    """

    def __init__(self):
        self.lists: dict = {}
        self.hashes: dict = {}
        self.sha = None

    def script_load(self, src):
        self.sha = "sha-1"
        return self.sha

    def evalsha(self, sha, nkeys, events_key, meta_key, *args):
        assert sha == self.sha
        ring_size, ring_bytes, ttl, outcome = int(args[0]), int(args[1]), int(args[2]), args[3]
        entries = list(args[4:])
        lst = self.lists.setdefault(events_key, [])
        h = self.hashes.setdefault(meta_key, {})
        added = 0
        for e in entries:
            lst.insert(0, e)
            added += len(e)
        h["event_count"] = h.get("event_count", 0) + len(entries)
        h[f"outcome:{outcome}"] = h.get(f"outcome:{outcome}", 0) + len(entries)
        used = h.get("ring_bytes_used", 0) + added
        while len(lst) > ring_size:
            used -= len(lst.pop())
        while used > ring_bytes and lst:
            used -= len(lst.pop())
        h["ring_bytes_used"] = used
        return [len(entries), used]


def _session(ring_size=10_000, ring_bytes=None):
    m = {"id": "s1", "vendor": "", "ring_size": str(ring_size), "expires_at": "9e9"}
    if ring_bytes is not None:
        m["ring_bytes"] = str(ring_bytes)
    return m


def test_ring_budget_measures_residency_not_cumulative_writes() -> None:
    """O teste central do orçamento.

    ``LTRIM`` descarta SEM contar, então um contador de bytes ESCRITOS mede
    acumulado: escrever 40 MiB e medir daria 40 MiB mesmo com o ring podado.
    A contabilidade tem de ser de RESIDÊNCIA.
    """
    r = _LuaRedis()
    budget = 200_000
    entry = "x" * 10_000
    for _ in range(100):  # 1 MB escrito, muito acima do teto
        cs._append_entries(
            r, "s1", [entry],
            ring_size=10_000, evt_ttl=100, ring_bytes=budget, outcome="delivered",
        )

    used = r.hashes["capture:session:s1:meta"]["ring_bytes_used"]
    assert used <= budget, f"residência {used} estourou o teto {budget}"
    residente = sum(len(e) for e in r.lists["capture:session:s1:events"])
    assert used == residente, "contador divergiu do que está de fato no ring"


def test_budget_shortens_the_ring_never_kills_the_session() -> None:
    r = _LuaRedis()
    for i in range(50):
        cs._append_entries(
            r, "s1", ["y" * 10_000],
            ring_size=10_000, evt_ttl=100, ring_bytes=100_000, outcome="delivered",
        )
    # Continua gravando (a última entrada está lá) — o teto ENCURTA, não encerra.
    assert len(r.lists["capture:session:s1:events"]) > 0
    assert r.hashes["capture:session:s1:meta"]["event_count"] == 50


def test_count_cap_still_applies() -> None:
    r = _LuaRedis()
    for i in range(20):
        cs._append_entries(
            r, "s1", [f"e{i}"],
            ring_size=5, evt_ttl=100, ring_bytes=10**9, outcome="delivered",
        )
    assert len(r.lists["capture:session:s1:events"]) == 5


class _NoEvalRedis:
    """Redis sem suporte a EVAL (ou cluster com CROSSSLOT)."""

    def __init__(self):
        self.calls = []
        self.hset_args = []

    def script_load(self, src):
        raise RuntimeError("ERR unknown command 'SCRIPT'")

    def pipeline(self):
        return self

    def lpush(self, *a, **k):
        self.calls.append("lpush")

    def ltrim(self, *a, **k):
        self.calls.append("ltrim")

    def expire(self, *a, **k):
        pass

    def hincrby(self, *a, **k):
        pass

    def hset(self, key, field, value):
        self.hset_args.append((field, value))

    def execute(self):
        self.calls.append("execute")


def test_falls_back_and_announces_when_eval_unavailable(monkeypatch) -> None:
    """Degradação ANUNCIADA. O que não pode acontecer é o operador achar que o
    teto de bytes está valendo quando não está."""
    monkeypatch.setattr(cs, "_append_sha", None)
    r = _NoEvalRedis()
    cs._append_entries(
        r, "s1", ["a"],
        ring_size=10, evt_ttl=100, ring_bytes=1000, outcome="delivered",
    )
    assert "lpush" in r.calls and "execute" in r.calls
    assert ("budget_enforcement", "unavailable") in r.hset_args


def test_ring_params_clamps_budget_to_the_hard_ceiling() -> None:
    _, _, rb = cs._ring_params({"ring_bytes": str(10**12)}, 0.0)
    assert rb == cs.CAPTURE_SESSION_MAX_BYTES
    _, _, rb2 = cs._ring_params({}, 0.0)
    assert rb2 == cs.CAPTURE_SESSION_MAX_BYTES


def test_global_ceiling_is_smaller_than_maxmemory() -> None:
    """8 × 24 MiB = 192 MiB contra os 512 MB do Redis compartilhado com o
    dedupe. Se alguém subir os tetos, este teste é o freio."""
    total = cs.MAX_ACTIVE_SESSIONS_GLOBAL * cs.CAPTURE_SESSION_MAX_BYTES
    assert total <= 200 * 1024 * 1024, (
        f"teto global de captura ({total} B) grande demais para um Redis de "
        "512 MB compartilhado com o dedupe — evictar dedupe é reentrega silenciosa"
    )
