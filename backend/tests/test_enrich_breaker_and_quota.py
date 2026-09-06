"""Circuit breaker por fonte e cota local dos enrichers remotos (ADR-LOCAL-0002).

Dois incidentes previsíveis que o runtime não cobria:

1. PROVEDOR MORTO custava ciclo. ``_disabled_this_cycle`` zera a cada ciclo,
   então uma fonte fora do ar era re-tentada a cada ~2 min e consumia o
   ``remaining`` do orçamento do lote antes de falhar — em TODO worker, porque
   nada era compartilhado. O ``circuit_breaker`` que protege os destinos agora
   protege a fonte: abre após N falhas na janela, no Redis L2, e o cooldown
   dobra a cada reabertura.

2. COTA descoberta pelo 429. O VirusTotal público aceita 4/min; o enricher
   deduplicava e limitava o lote, mas nada contava. Um token-bucket local
   (por chave de API) deixa para o próximo ciclo o que passaria da cota, e
   ``Retry-After`` vira espera respeitada — e abre o breaker na primeira.

Imports usam ``backend.app.*`` (gate compilado dual-root). Os testes de runtime
não sobem Redis: um fake com o subconjunto que ``circuit_breaker`` e
``EnrichCache`` usam.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest

from backend.app.collectors.enrich import ratelimit
from backend.app.collectors.enrich.ratelimit import (
    QuotaBuckets,
    TokenBucket,
    buckets_for,
    parse_retry_after,
)


# ── token bucket ──────────────────────────────────────────────────────────────


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_bucket_grants_capacity_then_refills_at_rate():
    clock = _Clock()
    b = TokenBucket(capacity=4, rate=4 / 60.0, clock=clock)
    assert [b.try_acquire() for _ in range(5)] == [True, True, True, True, False]
    clock.now += 15  # 1 token a cada 15 s
    assert b.try_acquire() is True
    assert b.try_acquire() is False


def test_bucket_never_waits():
    """``try_acquire`` decide na hora: esperar aqui consumiria o orçamento de
    300 ms do lote em troca de UMA chave."""
    clock = _Clock()
    b = TokenBucket(capacity=1, rate=0.0, clock=clock)
    assert b.try_acquire() is True
    before = clock.now
    assert b.try_acquire() is False
    assert clock.now == before


def test_retry_after_blocks_regardless_of_balance():
    clock = _Clock()
    b = TokenBucket(capacity=10, rate=1.0, clock=clock)
    b.block_for(30)
    assert b.try_acquire() is False, "saldo cheio, mas o provedor mandou esperar"
    assert 29 < b.blocked_remaining() <= 30
    clock.now += 31
    assert b.try_acquire() is True


def test_daily_bucket_is_refunded_when_minute_bucket_refuses():
    """Recusa no minuto não pode consumir o dia: a requisição não aconteceu."""
    clock = _Clock()
    q = QuotaBuckets(
        minute=TokenBucket(capacity=1, rate=0.0, clock=clock),
        day=TokenBucket(capacity=5, rate=0.0, clock=clock),
    )
    assert q.try_acquire() is True
    assert q.try_acquire() is False
    assert q.day.tokens == 4


def test_buckets_are_shared_per_identity_and_limits():
    ratelimit.reset_registry()
    a = buckets_for("k1", requests_per_minute=4, requests_per_day=500)
    b = buckets_for("k1", requests_per_minute=4, requests_per_day=500)
    c = buckets_for("k1", requests_per_minute=8, requests_per_day=500)
    assert a is b, "mesma chave e mesmos limites ⇒ mesmo bucket (a cota é do provedor)"
    assert a is not c, "limites diferentes ⇒ bucket novo, sem herdar saldo"


@pytest.mark.parametrize(
    "raw,expected",
    [(None, 60.0), ("", 60.0), ("15", 15.0), ("0", 0.0), ("99999", 3600.0), ("Wed, 21 Oct", 60.0)],
)
def test_parse_retry_after(raw, expected):
    assert parse_retry_after(raw) == expected


# ── VirusTotal honra a cota antes de chamar ───────────────────────────────────


class _Resp:
    def __init__(self, status: int, body: Optional[dict] = None, headers: Optional[dict] = None):
        self.status = status
        self._body = body or {}
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._body

    def raise_for_status(self):
        pass


class _Session:
    """Sessão aiohttp falsa: conta chamadas e devolve o que o teste mandar."""

    def __init__(self, responder):
        self.calls: List[str] = []
        self._responder = responder

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def get(self, url: str):
        self.calls.append(url)
        return self._responder(url)


def _vt_env(monkeypatch, responder):
    from backend.app.collectors.enrich.enrichers import virustotal as vt

    ratelimit.reset_registry()
    session = _Session(responder)
    monkeypatch.setattr(vt.aiohttp, "ClientSession", lambda **kw: session)

    async def _key(ctx):
        return "chave-de-teste"

    monkeypatch.setattr(vt, "_resolve_api_key", _key)
    return vt, session


def _ok(url):
    return _Resp(200, {"data": {"attributes": {"last_analysis_stats": {"malicious": 1, "harmless": 9}}}})


def test_virustotal_defers_keys_beyond_the_minute_quota(monkeypatch):
    from backend.app.collectors.enrich.contract import EnrichContext

    vt, session = _vt_env(monkeypatch, _ok)
    enricher = vt.VirusTotalEnricher({"requests_per_minute": 2, "requests_per_day": 100})
    keys = ["1.1.1.1", "2.2.2.2", "3.3.3.3", "4.4.4.4"]
    out = asyncio.run(enricher.resolve(keys, EnrichContext(organization_id=1, secret_ref="x")))
    assert len(session.calls) == 2, "só a cota do minuto vai para a rede"
    assert set(out) == {"1.1.1.1", "2.2.2.2"}
    # As duas restantes NÃO estão no mapa (UNKNOWN), nunca ``None`` (MISS).
    assert "3.3.3.3" not in out and "4.4.4.4" not in out


def test_virustotal_429_honours_retry_after_and_surfaces_when_nothing_resolved(monkeypatch):
    from backend.app.collectors.enrich.contract import EnrichContext

    vt, session = _vt_env(monkeypatch, lambda url: _Resp(429, headers={"Retry-After": "45"}))
    enricher = vt.VirusTotalEnricher({"requests_per_minute": 10, "concurrency": 1})
    ctx = EnrichContext(organization_id=1, secret_ref="x")
    with pytest.raises(vt.VirusTotalQuotaExceeded) as exc:
        asyncio.run(enricher.resolve(["1.1.1.1", "2.2.2.2"], ctx))
    assert exc.value.retry_after_s == 45.0
    # Com concurrency=1 o 429 da 1ª trava o bucket e a 2ª nem tenta.
    assert len(session.calls) == 1
    # O próximo lote, dentro do Retry-After, é recusado SEM chamada de rede.
    with pytest.raises(vt.VirusTotalQuotaExceeded):
        asyncio.run(enricher.resolve(["3.3.3.3"], ctx))
    assert len(session.calls) == 1


def test_virustotal_partial_result_is_returned_not_raised(monkeypatch):
    """Metade resolveu, metade levou 429: o lote segue com o que veio."""
    from backend.app.collectors.enrich.contract import EnrichContext

    state = {"n": 0}

    def responder(url):
        state["n"] += 1
        return _ok(url) if state["n"] == 1 else _Resp(429, headers={"Retry-After": "5"})

    vt, _ = _vt_env(monkeypatch, responder)
    enricher = vt.VirusTotalEnricher({"requests_per_minute": 10, "concurrency": 1})
    out = asyncio.run(
        enricher.resolve(["1.1.1.1", "2.2.2.2"], EnrichContext(organization_id=1, secret_ref="x"))
    )
    assert set(out) == {"1.1.1.1"}


# ── breaker por fonte no runtime ──────────────────────────────────────────────


class _FakeRedis:
    """Subconjunto usado por ``circuit_breaker`` + ``EnrichCache``, com TTL fake."""

    def __init__(self) -> None:
        self.data: Dict[str, Any] = {}
        self.ttl_s: Dict[str, int] = {}

    async def exists(self, key):
        return 1 if key in self.data else 0

    async def get(self, key):
        return self.data.get(key)

    async def mget(self, keys):
        return [self.data.get(k) for k in keys]

    async def set(self, key, value, ex=None, nx=False, px=None):
        if nx and key in self.data:
            return None
        self.data[key] = value
        if ex:
            self.ttl_s[key] = int(ex)
        return True

    async def delete(self, *keys):
        for k in keys:
            self.data.pop(k, None)
            self.ttl_s.pop(k, None)

    async def incr(self, key):
        self.data[key] = int(self.data.get(key) or 0) + 1
        return self.data[key]

    async def expire(self, key, seconds):
        self.ttl_s[key] = int(seconds)
        return True

    async def ttl(self, key):
        return self.ttl_s.get(key, -1) if key in self.data else -2

    def pipeline(self):
        outer = self

        class _P:
            def __init__(self):
                self.ops = []

            def set(self, key, value, ex=None):
                self.ops.append(("set", key, value, ex))
                return self

            def incr(self, key):
                self.ops.append(("incr", key, None, None))
                return self

            def expire(self, key, seconds):
                self.ops.append(("expire", key, seconds, None))
                return self

            async def execute(self):
                results = []
                for op, key, value, ex in self.ops:
                    if op == "set":
                        results.append(await outer.set(key, value, ex=ex))
                    elif op == "incr":
                        results.append(await outer.incr(key))
                    else:
                        results.append(await outer.expire(key, value))
                return results

        return _P()


class _Boom(Exception):
    pass


#: O registry de enrichers é GLOBAL por processo e o nome só pode ser registrado
#: uma vez. A fábrica registrada lê daqui, então cada teste troca o comportamento
#: sem registrar de novo — senão o 2º teste usaria o provedor do 1º.
_CURRENT: Dict[str, Any] = {"behaviour": None, "calls": []}


def _runtime_with_fake_provider(monkeypatch, behaviour):
    """Runtime com L2 fake e um enricher remoto controlado pelo teste.

    ``behaviour(keys)`` decide: devolve mapa (sucesso) ou levanta.
    """
    from backend.app.collectors.enrich import registry as registry_mod
    from backend.app.collectors.enrich import runtime as rt_mod
    from backend.app.collectors.enrich.contract import EnricherCapabilities, EnricherRegistration
    from backend.app.collectors.enrich.dsl import compile_policy
    from backend.app.collectors.enrich.runtime import EnrichRuntime
    from backend.app.core.config import settings

    calls: List[List[str]] = []
    _CURRENT["behaviour"] = behaviour
    _CURRENT["calls"] = calls

    class _Provider:
        caps = EnricherCapabilities(
            key_kinds=frozenset({"ip"}), mode="remote", supports_bulk=True,
            p99_budget_ms=50.0, suggested_ttl_s=60, suggested_negative_ttl_s=10,
        )

        def __init__(self, cfg):
            pass

        async def resolve(self, keys, ctx):
            _CURRENT["calls"].append(list(keys))
            return _CURRENT["behaviour"](list(keys))

    name = "fake_remote_breaker"
    if name not in registry_mod.registered_names():
        registry_mod.register(
            EnricherRegistration(
                name=name, factory=lambda cfg: _Provider(cfg), caps=_Provider.caps,
                output_fields={"verdict": "veredito"},
            )
        )
    fake = _FakeRedis()
    monkeypatch.setattr(settings, "ENRICH_REDIS_URL", "redis://fake", raising=False)
    monkeypatch.setattr(settings, "ENRICH_BREAKER_FAILURE_THRESHOLD", 2, raising=False)
    monkeypatch.setattr(settings, "ENRICH_BREAKER_COOLDOWN_S", 100, raising=False)
    monkeypatch.setattr(settings, "ENRICH_BREAKER_MAX_COOLDOWN_S", 400, raising=False)
    monkeypatch.setattr(rt_mod, "build_redis_client", lambda url: fake)

    policy = compile_policy([
        {
            "id": "r1",
            "enricher": name,
            "source": "fonte-x",
            "key": {"source": "normalized.src_endpoint.ip", "kind": "ip"},
            "outputs": [{"from": "verdict", "target": "_centralops.enrichment.fake.v"}],
        }
    ])
    rt = EnrichRuntime(max_table_bytes=1_000_000, lru_bytes=1_000_000)
    # A fonte vem do banco; aqui não há banco. A credencial é irrelevante.
    monkeypatch.setattr(rt, "_resolve_source", lambda org, src: ({}, None))
    return rt, policy, fake, calls


def _batch(*ips: str):
    return [{"_centralops": {}, "normalized": {"src_endpoint": {"ip": ip}}} for ip in ips]


def _resolve(rt, policy, batch, org=1):
    from backend.app.collectors.enrich.contract import EnrichContext

    rt.begin_cycle(org)
    try:
        return asyncio.run(
            rt.resolve_remote(policy, batch, EnrichContext(organization_id=org), budget_s=5.0)
        )
    finally:
        rt.end_cycle()


def test_open_breaker_allows_one_probe_per_cooldown_and_marks_the_reason(monkeypatch):
    """Duas falhas abrem. Aberto, o breaker deixa passar UMA sonda por cooldown
    (meio-aberto, como nos destinos) e bloqueia o resto — com a razão
    ``breaker_open``, não ``http``, para o operador ver que é quarentena."""
    from backend.app.collectors.enrich import runtime as rt_mod

    def boom(keys):
        raise _Boom("provedor fora do ar")

    rt, policy, fake, calls = _runtime_with_fake_provider(monkeypatch, boom)
    reasons: List[str] = []
    monkeypatch.setattr(rt_mod, "_count_error", lambda enricher, reason: reasons.append(reason))

    _resolve(rt, policy, _batch("1.1.1.1"))
    _resolve(rt, policy, _batch("2.2.2.2"))
    assert len(calls) == 2
    assert "breaker:enrich:1:fonte-x:open" in fake.data, "abriu"

    # 3º ciclo: a sonda. 4º e 5º, dentro do mesmo cooldown: bloqueados.
    _resolve(rt, policy, _batch("3.3.3.3"))
    assert len(calls) == 3, "meio-aberto ⇒ exatamente UMA sonda"
    _resolve(rt, policy, _batch("4.4.4.4"))
    _resolve(rt, policy, _batch("5.5.5.5"))
    assert len(calls) == 3, "aberto com sonda em curso ⇒ zero chamadas"
    assert reasons.count("breaker_open") == 2
    # Nada ficou trancado: os single-flights das chaves bloqueadas foram soltos.
    assert not [k for k in fake.data if "sf:" in k or ":lock" in k], fake.data


def test_success_closes_the_breaker(monkeypatch):
    state = {"fail": True}

    def flaky(keys):
        if state["fail"]:
            raise _Boom("x")
        return {k: {"verdict": "ok"} for k in keys}

    rt, policy, fake, calls = _runtime_with_fake_provider(monkeypatch, flaky)
    _resolve(rt, policy, _batch("1.1.1.1"))
    state["fail"] = False
    res = _resolve(rt, policy, _batch("2.2.2.2"))
    assert res.get("r1", "2.2.2.2") == (True, {"verdict": "ok"})
    assert not [k for k in fake.data if k.endswith(":fail")], "sucesso zera o contador"


def test_cooldown_doubles_on_each_reopen_up_to_the_cap(monkeypatch):
    def boom(keys):
        raise _Boom("x")

    rt, policy, fake, _ = _runtime_with_fake_provider(monkeypatch, boom)

    def open_key():
        return next(k for k in fake.data if k.startswith("breaker:") and k.endswith(":open"))

    # 1ª abertura: cooldown base (100)
    _resolve(rt, policy, _batch("a"))
    _resolve(rt, policy, _batch("b"))
    assert fake.ttl_s[open_key()] == 100
    # Simula o cooldown vencer e a sonda meio-aberta falhar de novo.
    del fake.data[open_key()]
    for k in [k for k in fake.data if k.endswith(":fail") or k.endswith(":probe")]:
        del fake.data[k]
    _resolve(rt, policy, _batch("c"))
    _resolve(rt, policy, _batch("d"))
    assert fake.ttl_s[open_key()] == 200, "2ª abertura dobra"
    del fake.data[open_key()]
    for k in [k for k in fake.data if k.endswith(":fail") or k.endswith(":probe")]:
        del fake.data[k]
    _resolve(rt, policy, _batch("e"))
    _resolve(rt, policy, _batch("f"))
    assert fake.ttl_s[open_key()] == 400, "3ª abertura chega ao teto (400)"


def test_quota_exception_opens_on_first_failure_with_retry_after_as_cooldown(monkeypatch):
    """429 com Retry-After: o provedor mandou esperar. Não conta para o limiar —
    abre já, e o cooldown é no mínimo o Retry-After."""
    from backend.app.collectors.enrich.enrichers.virustotal import VirusTotalQuotaExceeded

    def quota(keys):
        raise VirusTotalQuotaExceeded("429", retry_after_s=900)

    rt, policy, fake, calls = _runtime_with_fake_provider(monkeypatch, quota)
    _resolve(rt, policy, _batch("1.1.1.1"))
    open_keys = [k for k in fake.data if k.startswith("breaker:") and k.endswith(":open")]
    assert open_keys, "abriu na primeira"
    assert fake.ttl_s[open_keys[0]] == 900


def test_breaker_is_scoped_by_organization(monkeypatch):
    """A fonte da org A em quarentena não cala a fonte homônima da org B."""
    def boom(keys):
        raise _Boom("x")

    rt, policy, fake, calls = _runtime_with_fake_provider(monkeypatch, boom)
    _resolve(rt, policy, _batch("a"), org=1)
    _resolve(rt, policy, _batch("b"), org=1)
    assert len(calls) == 2
    _resolve(rt, policy, _batch("c"), org=2)
    assert len(calls) == 3, "org 2 ainda chama: breaker é por (org, fonte)"
    assert "breaker:enrich:1:fonte-x:open" in fake.data
    assert "breaker:enrich:2:fonte-x:open" not in fake.data


def test_without_l2_there_is_no_breaker_and_no_crash(monkeypatch):
    """Sem ENRICH_REDIS_URL o remoto nem roda (fail-closed já existente); o
    breaker não pode introduzir um caminho novo de erro nesse caso."""
    from backend.app.collectors.enrich.runtime import EnrichRuntime
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "ENRICH_REDIS_URL", "", raising=False)
    rt = EnrichRuntime(max_table_bytes=1_000_000, lru_bytes=1_000_000)
    assert asyncio.run(rt._breaker_is_open(None, "enrich:1:x")) is None
    asyncio.run(rt._breaker_failure(None, "enrich:1:x", "e"))
    asyncio.run(rt._breaker_success(None, "enrich:1:x", "e"))
