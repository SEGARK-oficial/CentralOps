"""Teste de carga do ring de captura contra um Redis REAL. FORA DO CI.

Roda à mão porque precisa de um Redis de verdade e escreve ~200 MB — não cabe
num pipeline de CI, e um teste de carga que roda em CI vira um teste de carga
que ninguém confia quando fica vermelho por ruído de máquina compartilhada.

Prova as afirmações que os testes unitários NÃO conseguem provar, porque lá o
fake reimplementa a semântica do Lua em Python — o que valida o entendimento do
autor, não o script que roda em produção:

  1. o script Lua executa de fato, e a contabilidade é de RESIDÊNCIA (não de
     bytes cumulativos escritos, que é o erro que mataria a sessão com o ring
     pela metade);
  2. o teto de bytes ENCURTA o ring e nunca encerra a sessão;
  3. o pior caso PERMITIDO (8 sessões × 24 MiB) não causa evicção das chaves de
     dedupe — o risco real, porque evictar dedupe é reentrega silenciosa de log
     de segurança, e há incidente documentado de 310k chaves evictadas;
  4. sem sessão ativa o tap é indistinguível de zero (a invariante de custo);
  5. sem EVAL, o fallback grava e ANUNCIA que o teto não está valendo.

COMO RODAR

    docker run -d --name co-loadtest-redis -p 6399:6379 redis:7-alpine \
      redis-server --maxmemory 512mb --maxmemory-policy volatile-lru \
      --save "" --appendonly no

    .venv-dev/bin/python scripts/loadtest/capture_ring_budget.py

    docker rm -f co-loadtest-redis

A config do Redis espelha compose/docker-compose.yml de propósito: o ponto do
teste é o comportamento sob `volatile-lru` com o dedupe dividindo a instância.
"""
from __future__ import annotations

import os
import pathlib
import sys
import time

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")
REDIS_URL = os.environ.get("LOADTEST_REDIS_URL", "redis://localhost:6399/0")
os.environ["REDIS_URL"] = REDIS_URL

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from backend.app.collectors import capture_session as cs  # noqa: E402

import redis as redis_sync  # noqa: E402

R = redis_sync.from_url(REDIS_URL, decode_responses=True)


def _reset():
    R.flushall()
    cs.reset_tap_breaker()
    cs.reset_session_cache()
    cs._append_sha = None


def _envelope(i: int, payload_size: int = 2000) -> dict:
    return {
        "_centralops": {
            "vendor": "sophos",
            "organization_id": 7,
            "event_id": f"evt-{i}",
            "integration_id": 1,
        },
        "normalized": {"class_uid": 1001, "id": i},
        "raw": {"blob": "x" * payload_size, "idx": i},
    }


def _session(sid: str, ring_size: int, ring_bytes: int) -> dict:
    return {
        "id": sid,
        "vendor": "",
        "ring_size": str(ring_size),
        "ring_bytes": str(ring_bytes),
        "expires_at": str(time.time() + 3600),
        "status": "active",
    }


def _used(sid: str) -> int:
    return int(R.hget(f"capture:session:{sid}:meta", "ring_bytes_used") or 0)


def _resident(sid: str) -> int:
    return sum(len(e) for e in R.lrange(f"capture:session:{sid}:events", 0, -1))


def _evicted() -> int:
    return int(R.info("stats").get("evicted_keys", 0))


def _used_memory_mb() -> float:
    return R.info("memory")["used_memory"] / 1024 / 1024


print("=" * 72)
print("TESTE 1 — o script Lua roda de verdade e a contabilidade é de RESIDÊNCIA")
print("=" * 72)
_reset()
sid = "s-lua"
sess = [_session(sid, ring_size=100_000, ring_bytes=8 * 1024 * 1024)]  # 8 MiB
escrito = 0
t0 = time.monotonic()
for lote in range(200):  # 200 lotes × 100 eventos × ~2 KB ≈ 41 MB escritos
    batch = [_envelope(lote * 100 + i) for i in range(100)]
    cs.record_sync(batch, 7, sessions=sess, outcome="delivered")
    escrito += sum(len(cs._dumps(e)) for e in batch)
dt = time.monotonic() - t0

usado = _used(sid)
residente = _resident(sid)
print(f"  bytes ESCRITOS (cumulativo) : {escrito/1024/1024:8.2f} MB")
print(f"  ring_bytes_used (contador)  : {usado/1024/1024:8.2f} MB")
print(f"  residente de fato no ring   : {residente/1024/1024:8.2f} MB")
print(f"  teto configurado            : {8:8.2f} MB")
print(f"  entradas no ring            : {R.llen(f'capture:session:{sid}:events')}")
assert usado == residente, f"contador ({usado}) divergiu do residente ({residente})"
assert usado <= 8 * 1024 * 1024, f"residência {usado} estourou o teto"
assert escrito > 2 * usado, "o teste precisa escrever bem mais que o teto para provar algo"
print(f"  ✅ contador == residente, e ambos <= teto (escrevi {escrito/usado:.1f}x o teto)")
print(f"  script Lua carregado: sha={cs._append_sha[:12] if cs._append_sha else 'NENHUM (fallback!)'}")
assert cs._append_sha, "caiu no fallback — o Lua não rodou"
print(f"  throughput: {20000/dt:,.0f} eventos/s no tap ({dt*1000:.0f} ms para 20.000)")

print()
print("=" * 72)
print("TESTE 2 — o teto ENCURTA o ring, nunca encerra a sessão")
print("=" * 72)
_reset()
sid = "s-cap"
sess = [_session(sid, ring_size=100_000, ring_bytes=2 * 1024 * 1024)]  # 2 MiB
for lote in range(40):
    cs.record_sync([_envelope(i) for i in range(50)], 7, sessions=sess, outcome="delivered")
meta = R.hgetall(f"capture:session:{sid}:meta")
llen = R.llen(f"capture:session:{sid}:events")
print(f"  event_count (cumulativo, sobrevive à poda): {meta.get('event_count')}")
print(f"  entradas AINDA no ring                    : {llen}")
print(f"  ring_bytes_used                           : {_used(sid)/1024/1024:.2f} MB (teto 2.00 MB)")
print(f"  budget_enforcement                        : {meta.get('budget_enforcement', 'active')}")
assert int(meta["event_count"]) == 2000, "o contador cumulativo tem de ver TODOS os eventos"
assert llen < 2000, "o ring tem de ter sido encurtado"
assert llen > 0, "a sessão NÃO pode ter sido esvaziada/encerrada"
assert _used(sid) <= 2 * 1024 * 1024
print("  ✅ 2.000 eventos contados, ring encurtado, sessão VIVA")

print()
print("=" * 72)
print("TESTE 3 — a captura NÃO evicta as chaves de dedupe (o risco real)")
print("=" * 72)
_reset()
# Semeia chaves de dedupe COM TTL — sob volatile-lru elas são candidatas
# legítimas à evicção, e evictar dedupe = reentrega silenciosa.
print("  semeando 200.000 chaves de dedupe (com TTL, como em produção)...")
pipe = R.pipeline()
for i in range(200_000):
    pipe.set(f"dedupe:1:msg-{i}", "1", ex=86400)
    if i % 10_000 == 0:
        pipe.execute()
        pipe = R.pipeline()
pipe.execute()
dedupe_antes = R.dbsize()
evicted_antes = _evicted()
mem_antes = _used_memory_mb()
print(f"  chaves no banco: {dedupe_antes:,} | memória: {mem_antes:.1f} MB | evicted: {evicted_antes}")

# Agora o pior caso PERMITIDO: 8 sessões no teto de 24 MiB.
print(f"  escrevendo o pior caso permitido: {cs.MAX_ACTIVE_SESSIONS_GLOBAL} sessões × "
      f"{cs.CAPTURE_SESSION_MAX_BYTES/1024/1024:.0f} MiB...")
t0 = time.monotonic()
for s in range(cs.MAX_ACTIVE_SESSIONS_GLOBAL):
    sid = f"s-full-{s}"
    sess = [_session(sid, ring_size=cs.MAX_RING_SIZE, ring_bytes=cs.CAPTURE_SESSION_MAX_BYTES)]
    # ~24 MiB por sessão em eventos de ~2 KB ≈ 12.500 eventos
    for lote in range(130):
        cs.record_sync([_envelope(i) for i in range(100)], 7, sessions=sess, outcome="delivered")
dt = time.monotonic() - t0

evicted_depois = _evicted()
mem_depois = _used_memory_mb()
dedupe_depois = sum(1 for _ in R.scan_iter(match="dedupe:*", count=5000))
total_ring = sum(_used(f"s-full-{s}") for s in range(cs.MAX_ACTIVE_SESSIONS_GLOBAL))
print(f"  memória depois : {mem_depois:.1f} MB (de 512 MB)")
print(f"  ring total     : {total_ring/1024/1024:.1f} MB (teto global "
      f"{cs.MAX_ACTIVE_SESSIONS_GLOBAL*cs.CAPTURE_SESSION_MAX_BYTES/1024/1024:.0f} MB)")
print(f"  chaves dedupe  : {dedupe_antes:,} antes → {dedupe_depois:,} depois")
print(f"  evicted_keys   : {evicted_antes} → {evicted_depois}")
print(f"  tempo          : {dt:.1f}s")
assert evicted_depois == evicted_antes, (
    f"EVICÇÃO DETECTADA ({evicted_depois - evicted_antes} chaves) — o ring de "
    "captura está comendo o Redis do dedupe"
)
assert dedupe_depois == 200_000, f"chaves de dedupe sumiram: {dedupe_depois}"
print("  ✅ ZERO evicções — as 200.000 chaves de dedupe sobreviveram intactas")

print()
print("=" * 72)
print("TESTE 4 — custo do tap por evento")
print("=" * 72)
_reset()
sid = "s-perf"
sess = [_session(sid, ring_size=cs.MAX_RING_SIZE, ring_bytes=cs.CAPTURE_SESSION_MAX_BYTES)]
batch = [_envelope(i) for i in range(200)]

t0 = time.monotonic()
for _ in range(50):
    cs.record_sync(batch, 7, sessions=sess, outcome="delivered")
com_sessao = (time.monotonic() - t0) / (50 * 200) * 1e6

# Sem sessão: o short-circuit em memória (cache negativo).
cs._mark_absent(999, time.time())
t0 = time.monotonic()
for _ in range(50):
    cs.record_sync(batch, 999, outcome="delivered")
sem_sessao = (time.monotonic() - t0) / (50 * 200) * 1e6

print(f"  COM sessão ativa : {com_sessao:7.2f} µs/evento")
print(f"  SEM sessão       : {sem_sessao:7.2f} µs/evento  ← invariante de custo zero")
print(f"  razão            : {com_sessao/max(sem_sessao,0.001):7.0f}x")
assert sem_sessao < 1.0, f"custo sem sessão ({sem_sessao:.2f} µs) não é desprezível"
print("  ✅ sem sessão o tap é indistinguível de zero")

print()
print("=" * 72)
print("TESTE 5 — fallback quando o EVAL não está disponível")
print("=" * 72)
_reset()
sid = "s-fb"
sess = [_session(sid, ring_size=100, ring_bytes=1024 * 1024)]


class _NoEval:
    """Wrapper que recusa SCRIPT LOAD, como um Redis com scripting desabilitado."""

    def __init__(self, real):
        self._r = real

    def script_load(self, *a, **k):
        raise redis_sync.exceptions.ResponseError("ERR unknown command 'SCRIPT'")

    def evalsha(self, *a, **k):
        raise redis_sync.exceptions.ResponseError("ERR unknown command 'EVALSHA'")

    def __getattr__(self, name):
        return getattr(self._r, name)


cs.record_sync([_envelope(i) for i in range(10)], 7, sessions=sess,
               outcome="delivered", redis=_NoEval(R))
meta = R.hgetall(f"capture:session:{sid}:meta")
print(f"  entradas gravadas    : {R.llen(f'capture:session:{sid}:events')}")
print(f"  budget_enforcement   : {meta.get('budget_enforcement')}")
assert R.llen(f"capture:session:{sid}:events") == 10, "o fallback tem de gravar"
assert meta.get("budget_enforcement") == "unavailable", (
    "a degradação tem de ser ANUNCIADA — o operador não pode achar que o teto "
    "está valendo quando não está"
)
print("  ✅ grava pelo caminho antigo e ANUNCIA que o teto não está sendo aplicado")

print()
print("=" * 72)
print("TODOS OS TESTES PASSARAM")
print("=" * 72)
