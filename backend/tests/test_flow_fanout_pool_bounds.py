"""O custo de /flow não pode escalar com o tamanho do deployment.

Bug original: a tela "Fluxo de dados" derrubava a API inteira. O endpoint
abria uma corrotina por integração com ``asyncio.gather`` SEM teto, e cada uma
abria DUAS sessões efêmeras — uma delas presa durante a I/O do Redis. Com 26
integrações isso pedia ~52 conexões de um pool de 40 (``pool_size=20`` +
``max_overflow=20``), e o recurso estourado é do PROCESSO: toda request passava
a esperar ``pool_timeout=30s``, o ``/readyz`` falhava e o healthcheck do
container marcava o serviço unhealthy.

Duas propriedades que faltavam, travadas aqui:

1. o fan-out respeita um teto de concorrência (o pico não cresce com N);
2. o EPM custa UMA query independente de quantas integrações existem, e a
   sessão fica livre antes de qualquer await de rede.

E a equivalência: o lote produz exatamente a mesma taxa que a versão single.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest
from sqlalchemy import create_engine, event as sa_event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import models
from backend.app.db.models import Base
from backend.app.routers.pipeline_health import (
    _epm_from_delta,
    _snapshot_key,
    get_events_per_minute_batch,
)
from backend.app.routers.routes import _gather_bounded


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    with Session() as session:
        yield session
    engine.dispose()


class _FakeRedis:
    """Redis assíncrono mínimo: só o que o batch usa (mget + pipeline)."""

    def __init__(self, store: dict[str, str] | None = None) -> None:
        self.store: dict[str, str] = dict(store or {})
        self.mget_calls = 0
        self.pipeline_calls = 0

    async def mget(self, keys):
        self.mget_calls += 1
        return [self.store.get(k) for k in keys]

    def pipeline(self):
        self.pipeline_calls += 1
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, redis: _FakeRedis) -> None:
        self._redis = redis
        self._ops: list = []

    def set(self, key, value, ex=None):
        self._ops.append(("set", key, value))

    def delete(self, *keys):
        for k in keys:
            self._ops.append(("delete", k, None))

    async def execute(self):
        for op, key, value in self._ops:
            if op == "set":
                self._redis.store[key] = value
            else:
                self._redis.store.pop(key, None)
        self._ops = []


def _seed_states(db, *, count: int, events_each: int = 1000) -> list[int]:
    """Cria ``count`` integrações, cada uma com um CollectionState."""
    org = models.Organization(name="Org", slug="org", is_active=True)
    db.add(org)
    db.commit()
    ids: list[int] = []
    for i in range(count):
        integ = models.Integration(
            organization_id=org.id,
            name=f"Integ {i}",
            platform="sophos",
            kind="tenant",
            auth_status="healthy",
            is_active=True,
        )
        db.add(integ)
        db.commit()
        db.refresh(integ)
        db.add(
            models.CollectionState(
                integration_id=integ.id,
                stream="alerts",
                events_collected_total=events_each,
            )
        )
        db.commit()
        ids.append(integ.id)
    return ids


# ── 1. Teto de concorrência do fan-out ────────────────────────────────


@pytest.mark.parametrize("total,limit", [(26, 8), (200, 8), (50, 1), (5, 32)])
def test_gather_bounded_nunca_excede_o_teto(total, limit):
    """O pico de corrotinas simultâneas é o teto, não N.

    É esta a propriedade que faltava: com ``asyncio.gather`` puro o pico era
    ``total``, então o consumo de pool crescia junto com o nº de integrações.
    """
    live = 0
    peak = 0

    async def _node(idx: int) -> int:
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0)  # cede o loop — maximiza a sobreposição
        live -= 1
        return idx

    async def _run():
        return await _gather_bounded(
            [(lambda i=i: _node(i)) for i in range(total)], limit=limit
        )

    results = asyncio.run(_run())

    assert peak <= limit, f"pico {peak} excedeu o teto {limit}"
    assert results == list(range(total))  # ordem preservada, como no gather


def test_gather_bounded_preserva_excecao():
    """Erro num nó continua propagando — o teto não engole falha."""

    async def _boom():
        raise ValueError("boom")

    async def _ok():
        return 1

    async def _run():
        return await _gather_bounded([lambda: _ok(), lambda: _boom()])

    with pytest.raises(ValueError, match="boom"):
        asyncio.run(_run())


# ── 2. EPM em lote: custo fixo ────────────────────────────────────────


def _count_queries(db, fn):
    """Conta SELECTs emitidos durante ``fn()``."""
    seen: list[str] = []

    def _before(conn, cursor, statement, params, context, executemany):
        if statement.strip().upper().startswith("SELECT"):
            seen.append(statement)

    sa_event.listen(db.get_bind(), "before_cursor_execute", _before)
    try:
        result = fn()
    finally:
        sa_event.remove(db.get_bind(), "before_cursor_execute", _before)
    return result, seen


@pytest.mark.parametrize("n_integrations", [1, 5, 26, 60])
def test_epm_batch_faz_uma_query_independente_de_n(db, n_integrations):
    """UMA query para qualquer número de integrações.

    Antes era 1 query POR integração, cada uma numa sessão efêmera própria
    mantida aberta durante os round-trips do Redis.
    """
    ids = _seed_states(db, count=n_integrations)
    redis = _FakeRedis()

    _, queries = _count_queries(
        db, lambda: asyncio.run(get_events_per_minute_batch(redis, db, ids))
    )

    assert len(queries) == 1, f"esperava 1 query, saíram {len(queries)}"
    assert redis.mget_calls == 1  # e um único round-trip de leitura


def test_epm_batch_primeira_chamada_grava_baseline_e_retorna_none(db):
    """Sem snapshot anterior não há taxa — grava baseline e devolve None."""
    ids = _seed_states(db, count=3)
    redis = _FakeRedis()

    out = asyncio.run(get_events_per_minute_batch(redis, db, ids))

    assert out == {i: None for i in ids}
    for i in ids:
        assert _snapshot_key(i) in redis.store  # baseline persistido


def test_epm_batch_calcula_taxa_a_partir_do_snapshot(db):
    """Delta de 600 eventos em 60s = 600 eventos/min."""
    ids = _seed_states(db, count=2, events_each=1000)
    now = datetime.utcnow().timestamp()
    redis = _FakeRedis(
        {
            _snapshot_key(i): json.dumps({"total": 400, "ts": now - 60})
            for i in ids
        }
    )

    out = asyncio.run(get_events_per_minute_batch(redis, db, ids))

    for i in ids:
        assert out[i] == pytest.approx(600.0, rel=0.05)


def test_epm_batch_equivale_a_matematica_single(db):
    """O lote não reimplementa a regra: usa o mesmo ``_epm_from_delta``."""
    ids = _seed_states(db, count=1, events_each=1000)
    now = datetime.utcnow().timestamp()
    redis = _FakeRedis(
        {_snapshot_key(ids[0]): json.dumps({"total": 250, "ts": now - 30})}
    )

    out = asyncio.run(get_events_per_minute_batch(redis, db, ids))

    # Tolerância mínima só porque o lote lê o relógio alguns microssegundos
    # depois do teste; uma fórmula diferente erraria por ordens de grandeza.
    assert out[ids[0]] == pytest.approx(
        _epm_from_delta(1000, 250, now - 30, now), rel=1e-4
    )


def test_epm_batch_descarta_snapshot_corrompido(db):
    """Snapshot ilegível → None e a chave é apagada (recomeça limpo)."""
    ids = _seed_states(db, count=1)
    key = _snapshot_key(ids[0])
    redis = _FakeRedis({key: "{lixo!!"})

    out = asyncio.run(get_events_per_minute_batch(redis, db, ids))

    assert out[ids[0]] is None
    assert key not in redis.store


def test_epm_batch_contador_reiniciado_nao_vira_taxa_negativa(db):
    """Worker reiniciou o contador (total atual < snapshot) → None, não lixo."""
    ids = _seed_states(db, count=1, events_each=10)
    now = datetime.utcnow().timestamp()
    redis = _FakeRedis(
        {_snapshot_key(ids[0]): json.dumps({"total": 99999, "ts": now - 60})}
    )

    assert asyncio.run(get_events_per_minute_batch(redis, db, ids))[ids[0]] is None


def test_epm_batch_redis_indisponivel_degrada_sem_levantar(db):
    """Redis fora → None para todas, nunca exceção (a tela degrada, não cai)."""
    ids = _seed_states(db, count=3)

    class _BrokenRedis(_FakeRedis):
        async def mget(self, keys):
            raise ConnectionError("redis down")

    out = asyncio.run(get_events_per_minute_batch(_BrokenRedis(), db, ids))

    assert out == {i: None for i in ids}


def test_epm_batch_lista_vazia_nao_toca_o_banco(db):
    """Sem integrações não há query nenhuma."""
    redis = _FakeRedis()
    out, queries = _count_queries(
        db, lambda: asyncio.run(get_events_per_minute_batch(redis, db, []))
    )
    assert out == {}
    assert queries == []
    assert redis.mget_calls == 0


def test_epm_batch_integracao_sem_collection_state_conta_zero(db):
    """Integração sem nenhum CollectionState não quebra o agregado."""
    ids = _seed_states(db, count=1)
    ghost_id = ids[0] + 999
    redis = _FakeRedis()

    out = asyncio.run(get_events_per_minute_batch(redis, db, ids + [ghost_id]))

    assert out[ghost_id] is None
    baseline = json.loads(redis.store[_snapshot_key(ghost_id)])
    assert baseline["total"] == 0
