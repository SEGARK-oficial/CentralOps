"""Cache L1/L2 do enriquecimento remoto (ADR-LOCAL-0002, Fase 3).

Cobre as três decisões que, se caírem, transformam o cache num amplificador de bug
em vez de uma economia: fail-closed sem L2 dedicado, três estados (não dois), e
single-flight por CHAVE.
"""

from __future__ import annotations

import asyncio
import os

import pytest

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

from backend.app.collectors.enrich.cache import (
    CacheState,
    EnrichCache,
    L1Cache,
    build_redis_client,
    cache_key,
)


# ── chave ───────────────────────────────────────────────────────────────────

def test_cache_key_is_scoped_by_organization():
    """Sem org na chave, o CMDB da Org A é servido para a Org B no mesmo fork.

    Não há afinidade por tenant no data-plane e o fork atende 500 tasks — a classe
    exata dos 11 gaps cross-org já fechados no projeto.
    """
    a = cache_key(1, "virustotal", "1", "8.8.8.8")
    b = cache_key(2, "virustotal", "1", "8.8.8.8")
    assert a != b
    assert a.startswith("enr:1:virustotal:1:")
    # A versão da política entra: editar a política invalida o cache sem purge.
    assert cache_key(1, "virustotal", "2", "8.8.8.8") != a
    # O indicador não fica legível no Redis (e não quebra o namespace).
    assert "8.8.8.8" not in a


def test_cache_key_survives_hostile_values():
    """URL com ':' e espaço não pode quebrar o namespace."""
    k = cache_key(1, "vt", "1", "http://x.com/a b:c")
    assert k.count(":") == 4


# ── L1 ──────────────────────────────────────────────────────────────────────

def test_l1_expires_and_evicts_lru():
    l1 = L1Cache(max_entries=2)
    l1.put("a", CacheState.HIT, {"v": 1}, ttl_s=60)
    l1.put("b", CacheState.HIT, {"v": 2}, ttl_s=60)
    assert l1.get("a")[0] is CacheState.HIT
    l1.put("c", CacheState.HIT, {"v": 3}, ttl_s=60)  # evicta "b" (menos recente)
    assert l1.get("b")[0] is CacheState.UNKNOWN
    assert l1.get("a")[0] is CacheState.HIT

    l1.put("d", CacheState.HIT, {"v": 4}, ttl_s=-1)  # TTL não-positivo não entra
    assert l1.get("d")[0] is CacheState.UNKNOWN


def test_l1_never_stores_unknown():
    """UNKNOWN é ausência de resposta, não resposta — cachear seria mentir."""
    l1 = L1Cache()
    l1.put("k", CacheState.UNKNOWN, None, ttl_s=60)
    assert len(l1) == 0


def test_l1_stores_negative_hits():
    """MISS confirmado é cacheável: reperguntar queima cota por nada."""
    l1 = L1Cache()
    l1.put("k", CacheState.MISS, None, ttl_s=60)
    state, value = l1.get("k")
    assert state is CacheState.MISS and value is None


# ── L2 (fake redis) ─────────────────────────────────────────────────────────

class _FakeRedis:
    """Fake mínimo com o subconjunto usado: mget/set/get/delete/pipeline."""

    def __init__(self):
        self.data = {}
        self.calls = {"mget": 0, "set": 0}

    async def mget(self, keys):
        self.calls["mget"] += 1
        return [self.data.get(k) for k in keys]

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None, nx=False, px=None):
        self.calls["set"] += 1
        if nx and key in self.data:
            return None
        self.data[key] = value
        return True

    async def delete(self, key):
        self.data.pop(key, None)

    def pipeline(self):
        outer = self

        class _P:
            def __init__(self):
                self.ops = []

            def set(self, key, value, ex=None):
                self.ops.append((key, value))
                return self

            async def execute(self):
                for k, v in self.ops:
                    outer.data[k] = v
                return [True] * len(self.ops)

        return _P()


def test_three_states_round_trip():
    """HIT / MISS / UNKNOWN sobrevivem ao ciclo de escrita e leitura no L2.

    O bug que isto previne: colapsar MISS e UNKNOWN faria uma cota estourada virar
    'indicador limpo' pelas próximas 6 horas — falso negativo de segurança que se
    propaga por todo o TTL.
    """
    redis = _FakeRedis()
    cache = EnrichCache(redis, l1=L1Cache())

    async def run():
        # "a" tem valor, "b" é miss confirmado, "c" NÃO foi resolvida (ausente).
        await cache.put_many(
            {"a": {"score": 9}, "b": None}, ttl_s=60, negative_ttl_s=30
        )
        cache.l1.clear()  # força a leitura a passar pelo L2
        values, states, missing = await cache.get_many(["a", "b", "c"])
        return values, states, missing

    values, states, missing = asyncio.run(run())
    assert values["a"] == {"score": 9}
    assert states["a"] is CacheState.HIT
    assert states["b"] is CacheState.MISS      # não vai à origem de novo
    assert missing == ["c"]                    # UNKNOWN → precisa resolver
    assert "c" not in states


def test_unresolved_keys_are_never_written():
    """Chave ausente do retorno do provedor não pode virar MISS cacheado."""
    redis = _FakeRedis()
    cache = EnrichCache(redis, l1=L1Cache())
    asyncio.run(cache.put_many({"a": {"x": 1}}, ttl_s=60, negative_ttl_s=30))
    assert "a" in redis.data and "nao_resolvida" not in redis.data


def test_l1_hit_avoids_touching_l2():
    """A repetição dentro do ciclo não pode gerar round-trip."""
    redis = _FakeRedis()
    cache = EnrichCache(redis, l1=L1Cache())

    async def run():
        await cache.put_many({"a": {"x": 1}}, ttl_s=60, negative_ttl_s=30)
        before = redis.calls["mget"]
        await cache.get_many(["a"])   # deve sair do L1
        return before, redis.calls["mget"]

    before, after = asyncio.run(run())
    assert before == after, "L1 hit não pode chamar MGET"


def test_corrupted_l2_entry_is_treated_as_missing():
    redis = _FakeRedis()
    redis.data["k"] = "{isso não é json"
    cache = EnrichCache(redis, l1=L1Cache())
    _, states, missing = asyncio.run(cache.get_many(["k"]))
    assert missing == ["k"] and "k" not in states


def test_l2_failure_degrades_to_origin_instead_of_raising():
    """Redis fora do ar não pode derrubar a coleta — degrada para a origem."""

    class _Broken(_FakeRedis):
        async def mget(self, keys):
            raise RuntimeError("redis down")

    cache = EnrichCache(_Broken(), l1=L1Cache())
    values, states, missing = asyncio.run(cache.get_many(["a", "b"]))
    assert missing == ["a", "b"] and not values and not states


# ── single-flight ───────────────────────────────────────────────────────────

def test_single_flight_is_per_key_not_per_batch():
    """Lock por chave: dois lotes com conjuntos sobrepostos não repagam o comum.

    Travar em sha1(chaves_do_lote) não preveniria stampede algum — basta um evento
    diferir e o hash muda, e os dois lotes batem na origem para as ~199 em comum.
    """
    redis = _FakeRedis()
    cache = EnrichCache(redis, l1=L1Cache())

    async def run():
        first = await cache.acquire_single_flight("k1")
        second = await cache.acquire_single_flight("k1")   # mesmo indicador
        other = await cache.acquire_single_flight("k2")    # indicador diferente
        await cache.release_single_flight("k1")
        after_release = await cache.acquire_single_flight("k1")
        return first, second, other, after_release

    first, second, other, after_release = asyncio.run(run())
    assert first is True
    assert second is False, "o segundo pedido da MESMA chave não pode resolver"
    assert other is True, "chave diferente não pode ser bloqueada"
    assert after_release is True


def test_wait_for_peer_is_bounded():
    """A espera é curta: o orçamento do lote inteiro é de 300 ms."""
    import time

    redis = _FakeRedis()
    cache = EnrichCache(redis, l1=L1Cache(), singleflight_wait_ms=30)
    t0 = time.monotonic()
    state, _ = asyncio.run(cache.wait_for_peer("nunca-preenchida"))
    elapsed_ms = (time.monotonic() - t0) * 1000
    assert state is CacheState.UNKNOWN
    assert elapsed_ms < 300, f"esperou {elapsed_ms:.0f} ms — estouraria o orçamento"


def test_wait_for_peer_returns_the_value_the_peer_wrote():
    redis = _FakeRedis()
    cache = EnrichCache(redis, l1=L1Cache(), singleflight_wait_ms=200)

    async def run():
        async def peer():
            await asyncio.sleep(0.02)
            await cache.put_many({"k": {"score": 7}}, ttl_s=60, negative_ttl_s=30)

        task = asyncio.create_task(peer())
        result = await cache.wait_for_peer("k")
        await task
        return result

    state, value = asyncio.run(run())
    assert state is CacheState.HIT and value == {"score": 7}


# ── fail-closed ─────────────────────────────────────────────────────────────

def test_no_url_means_no_l2_client():
    """Sem ENRICH_REDIS_URL não há cliente — e sem cliente não há remoto.

    O caminho fácil (cair no REDIS_URL principal) é o errado: aquela instância roda
    volatile-lru com 512 MB junto do dedupe, cuja evicção silenciosa reaparece como
    reentrega no SIEM.
    """
    assert build_redis_client("") is None
    assert build_redis_client(None) is None


def test_remote_enrichment_is_disabled_without_dedicated_redis(monkeypatch):
    from backend.app.collectors.enrich import enrichers  # noqa: F401
    from backend.app.collectors.enrich.contract import EnrichContext
    from backend.app.collectors.enrich.dsl import compile_policy
    from backend.app.collectors.enrich.runtime import EnrichRuntime
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "ENRICH_REDIS_URL", "", raising=False)
    policy = compile_policy([
        {
            "id": "vt",
            "enricher": "virustotal",
            "source": "fonte-de-teste",
            "key": {"source": "normalized.src_endpoint.ip", "kind": "ip"},
            "outputs": [
                {"from": "malicious", "target": "_centralops.enrichment.vt.m"}
            ],
        }
    ])
    rt = EnrichRuntime(max_table_bytes=1_000_000, lru_bytes=1_000_000)
    batch = [{"_centralops": {}, "normalized": {"src_endpoint": {"ip": "8.8.8.8"}}}]
    resolution = asyncio.run(
        rt.resolve_remote(
            policy, batch, EnrichContext(organization_id=1), budget_s=1.0
        )
    )
    # Nada resolvido, e — crucialmente — nenhuma chamada ao VirusTotal foi feita.
    assert resolution.get("vt", "8.8.8.8") == (False, None)


def test_table_lru_and_value_cache_are_distinct_attributes():
    """Regressão: os dois já compartilharam o nome ``_cache`` e o LRU foi zerado."""
    from backend.app.collectors.enrich.runtime import EnrichRuntime

    rt = EnrichRuntime(max_table_bytes=1, lru_bytes=1)
    assert isinstance(rt._cache, dict), "o LRU de TABELAS precisa continuar existindo"
    assert rt._kv_cache is None
