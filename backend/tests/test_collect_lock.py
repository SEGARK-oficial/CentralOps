"""Exclusão mútua por (integração, stream) no ciclo de coleta.

O agendador dispara na cadência declarada pelo plugin sem saber se o ciclo
anterior terminou. Quando o ciclo passa a demorar mais que a cadência — que é
justamente o que acontece sob backlog — os runs se empilham. Medido em produção
(jul/2026): ``schedule=2min`` contra ciclos de ~3,7min produzia 2 a 3 workers
lendo o MESMO cursor e buscando as MESMAS 10.000 linhas, terminando com 34ms de
diferença. O cursor avançava uma vez só; o resto era trabalho jogado fora
competindo pelos mesmos shards do Indexer — o que deixava o ciclo mais lento e
realimentava o empilhamento.

Os testes usam ``fakeredis`` com Lua REAL (via lupa), então o
compare-and-delete de ``_RELEASE_IF_MINE_LUA`` é executado de verdade, e não
reimplementado por um dublê. Cada chamada recebe um cliente novo apontando para
o mesmo ``FakeServer``, espelhando ``get_worker_redis()`` (que abre uma conexão
por task Celery).
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import asyncio
from typing import List, Tuple

import fakeredis.aioredis
import pytest

from backend.app.collectors import celery_app as celery_module
from backend.app.collectors import pipeline


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture()
def fake_redis(monkeypatch):
    """Substitui ``get_worker_redis`` por clientes sobre um FakeServer compartilhado.

    Um cliente NOVO por chamada é fiel ao produção: ``get_worker_redis`` cria
    conexão por task (o pool não sobrevive ao ``asyncio.run()`` do prefork), e o
    ``finally`` do ciclo faz ``aclose()`` nela. Se o teste compartilhasse uma
    única instância, o primeiro ``aclose()`` derrubaria a trava do segundo.
    """
    server = fakeredis.FakeServer()
    created: List[fakeredis.aioredis.FakeRedis] = []

    def _factory():
        client = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
        created.append(client)
        return client

    monkeypatch.setattr(celery_module, "get_worker_redis", _factory)
    # Cliente de inspeção, fora do ciclo — não é fechado pelo código sob teste.
    inspector = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    yield inspector, created


@pytest.fixture()
def spy_cycle(monkeypatch):
    """Troca o corpo do ciclo por um espião — a trava é o que está sob teste."""
    calls: List[Tuple[int, str]] = []

    async def _fake(integration_id: int, stream: str) -> None:
        calls.append((integration_id, stream))

    monkeypatch.setattr(pipeline, "_run_collection_once", _fake)
    return calls


def _key(integration_id: int, stream: str) -> str:
    return pipeline._COLLECT_LOCK_KEY.format(integration_id=integration_id, stream=stream)


# ── O caso que originou a trava ──────────────────────────────────────────


async def test_concurrent_cycle_of_the_same_stream_is_skipped(
    fake_redis, spy_cycle, monkeypatch
) -> None:
    """O segundo tick do MESMO (integração, stream) não entra — ele sai.

    Sair é o comportamento correto, não um erro: o ciclo em curso avança o
    cursor e o próximo tick retoma de onde ele parar. Rodar em paralelo não
    acelera nada — os dois leem o mesmo cursor e buscam as mesmas linhas.
    """
    _inspector, _ = fake_redis
    entered = asyncio.Event()
    release = asyncio.Event()
    calls: List[Tuple[int, str]] = []

    async def _cycle(integration_id: int, stream: str) -> None:
        calls.append((integration_id, stream))
        # SÓ o primeiro segura o slot. Assim, se a trava regredir, o segundo
        # ciclo entra e RETORNA — o teste falha na asserção em vez de travar a
        # suíte esperando um evento que ninguém vai soltar.
        if len(calls) == 1:
            entered.set()
            await release.wait()

    monkeypatch.setattr(pipeline, "_run_collection_once", _cycle)

    primeiro = asyncio.create_task(pipeline.run_collection_once(1, "detections"))
    await asyncio.wait_for(entered.wait(), timeout=5)

    # Segundo tick enquanto o primeiro ainda roda.
    await asyncio.wait_for(pipeline.run_collection_once(1, "detections"), timeout=5)
    assert calls == [(1, "detections")], "o segundo ciclo entrou apesar da trava"

    release.set()
    await asyncio.wait_for(primeiro, timeout=5)
    assert calls == [(1, "detections")]


async def test_different_streams_of_the_same_integration_do_not_block_each_other(
    fake_redis, monkeypatch
) -> None:
    """A trava é por (integração, stream), não por integração.

    Serializar streams distintos da mesma integração cortaria a vazão pela
    metade sem resolver nada: eles têm cursores independentes e não disputam o
    mesmo trabalho.
    """
    entered = asyncio.Event()
    release = asyncio.Event()
    calls: List[Tuple[int, str]] = []

    async def _slow_cycle(integration_id: int, stream: str) -> None:
        calls.append((integration_id, stream))
        if (integration_id, stream) == (1, "detections"):
            entered.set()
            await release.wait()

    monkeypatch.setattr(pipeline, "_run_collection_once", _slow_cycle)

    primeiro = asyncio.create_task(pipeline.run_collection_once(1, "detections"))
    await asyncio.wait_for(entered.wait(), timeout=5)

    await asyncio.wait_for(pipeline.run_collection_once(1, "alerts"), timeout=5)
    await asyncio.wait_for(pipeline.run_collection_once(2, "detections"), timeout=5)

    release.set()
    await asyncio.wait_for(primeiro, timeout=5)
    assert calls == [(1, "detections"), (1, "alerts"), (2, "detections")]


async def test_sequential_cycles_are_not_blocked_by_the_previous_one(
    fake_redis, spy_cycle
) -> None:
    """Ciclo que terminou não pode deixar a trava presa até o TTL expirar.

    Se ficasse, a coleta pararia por 16 minutos depois do primeiro run — um
    modo de falha bem pior que o trabalho duplicado que a trava evita.
    """
    await pipeline.run_collection_once(1, "detections")
    await pipeline.run_collection_once(1, "detections")
    assert spy_cycle == [(1, "detections"), (1, "detections")]


# ── Ciclo de vida da trava ───────────────────────────────────────────────


async def test_lock_is_released_at_the_end_of_the_cycle(fake_redis, spy_cycle) -> None:
    inspector, _ = fake_redis
    await pipeline.run_collection_once(1, "detections")
    assert await inspector.get(_key(1, "detections")) is None


async def test_lock_is_released_even_when_the_cycle_raises(fake_redis, monkeypatch) -> None:
    """Ciclo que estoura não pode envenenar o stream até o TTL.

    ``_run_collection_once`` re-lança de propósito (o Celery precisa ver a
    falha); a trava tem de sair junto.
    """

    async def _boom(integration_id: int, stream: str) -> None:
        raise RuntimeError("indexer fora do ar")

    monkeypatch.setattr(pipeline, "_run_collection_once", _boom)
    inspector, _ = fake_redis

    with pytest.raises(RuntimeError, match="indexer fora do ar"):
        await pipeline.run_collection_once(1, "detections")
    assert await inspector.get(_key(1, "detections")) is None


async def test_release_does_not_delete_a_lock_owned_by_someone_else(
    fake_redis, monkeypatch
) -> None:
    """O compare-and-delete existe para o ciclo que estourou o TTL.

    Cenário real: o ciclo A demora mais que ``_COLLECT_LOCK_TTL_SECONDS``, a
    trava expira, o ciclo B a adquire e começa a trabalhar — e então A termina.
    Um ``DEL`` cego de A apagaria a trava de B e liberaria um terceiro ciclo
    concorrente, ressuscitando exatamente o empilhamento que a trava evita.

    Aqui o Lua roda de verdade (fakeredis + lupa): sobrescrevemos o valor da
    chave com outro dono antes de A soltar.
    """
    inspector, _ = fake_redis
    chave = _key(1, "detections")

    async def _steal(integration_id: int, stream: str) -> None:
        # Simula: TTL expirou e OUTRO ciclo adquiriu a trava.
        await inspector.set(chave, "token-do-ciclo-B", ex=900)

    monkeypatch.setattr(pipeline, "_run_collection_once", _steal)

    await pipeline.run_collection_once(1, "detections")
    assert await inspector.get(chave) == "token-do-ciclo-B", (
        "o ciclo A apagou a trava do ciclo B — dois ciclos passariam a rodar juntos"
    )


async def test_lock_carries_a_ttl_so_a_dead_worker_does_not_park_the_stream(
    fake_redis, spy_cycle
) -> None:
    """Worker morto (SIGKILL/OOM) não roda o ``finally``; só o TTL destrava.

    O TTL precisa cobrir o run mais longo (hard time limit 900s) e ficar abaixo
    do ``visibility_timeout`` (3600s), senão uma redelivery legítima após crash
    duro ficaria bloqueada.
    """
    inspector, _ = fake_redis
    entered = asyncio.Event()
    release = asyncio.Event()
    ttl_visto: List[int] = []

    async def _slow_cycle(integration_id: int, stream: str) -> None:
        ttl_visto.append(await inspector.ttl(_key(integration_id, stream)))
        entered.set()
        await release.wait()

    import unittest.mock as _mock

    with _mock.patch.object(pipeline, "_run_collection_once", _slow_cycle):
        tarefa = asyncio.create_task(pipeline.run_collection_once(1, "detections"))
        await asyncio.wait_for(entered.wait(), timeout=5)
        release.set()
        await asyncio.wait_for(tarefa, timeout=5)

    assert ttl_visto and 0 < ttl_visto[0] <= pipeline._COLLECT_LOCK_TTL_MAX_SECONDS
    assert pipeline._COLLECT_LOCK_TTL_MAX_SECONDS >= 900, (
        "TTL menor que o hard time limit da task deixa a trava expirar com o "
        "ciclo ainda rodando — a duplicação volta"
    )
    assert pipeline._COLLECT_LOCK_TTL_MAX_SECONDS < 3600, (
        "TTL >= visibility_timeout bloquearia a redelivery após crash duro"
    )


def test_ttl_acompanha_a_cadencia_do_stream(monkeypatch) -> None:
    """TTL proporcional à cadência: stream rápido não fica parado por 16 minutos.

    Um TTL único penalizava os streams de PUSH (drenam a cada 20s): um worker
    morto por SIGKILL deixava a trava de pé e o dreno parado por até 16 minutos,
    enquanto o ciclo real dura segundos.
    """
    from datetime import timedelta
    import types

    from backend.app.collectors import pipeline

    def _fake_ttl(cadencia_s: int) -> int:
        monkeypatch.setattr(
            pipeline, "_collect_lock_ttl_seconds",
            pipeline._collect_lock_ttl_seconds,
        )
        reg = types.SimpleNamespace(schedule=timedelta(seconds=cadencia_s))
        monkeypatch.setattr(
            "backend.app.collectors.registry.get", lambda p, s: reg, raising=True
        )
        # platform resolvido; o helper só precisa de um valor não vazio
        import backend.app.db.database as _db

        class _S:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def query(self, *a, **k): return self
            def filter(self, *a, **k): return self
            def scalar(self): return "wazuh"

        monkeypatch.setattr(_db, "SessionLocal", lambda: _S())
        return pipeline._collect_lock_ttl_seconds(1, "detections")

    # 20s de cadência (push) → 3 ciclos = 60s, o piso.
    assert _fake_ttl(20) == pipeline._COLLECT_LOCK_TTL_MIN_SECONDS
    # 2min (wazuh detections) → 360s: cobre um ciclo que atrase o dobro.
    assert _fake_ttl(120) == 360
    # cadência longa é clampada no teto, que continua abaixo do visibility_timeout.
    assert _fake_ttl(9999) == pipeline._COLLECT_LOCK_TTL_MAX_SECONDS


async def test_lock_key_is_scoped_by_integration_and_stream(fake_redis, monkeypatch) -> None:
    """A chave carrega os dois eixos — errar isso serializa a frota inteira."""
    inspector, _ = fake_redis
    vistas: List[str] = []

    async def _peek(integration_id: int, stream: str) -> None:
        async for k in inspector.scan_iter(match="collect:lock:*"):
            vistas.append(k)

    monkeypatch.setattr(pipeline, "_run_collection_once", _peek)
    await pipeline.run_collection_once(77, "detections")
    assert vistas == ["collect:lock:77:detections"]


# ── Degradação: sem Redis a coleta continua ──────────────────────────────


async def test_cycle_runs_when_redis_is_unavailable(monkeypatch, spy_cycle, caplog) -> None:
    """Sem Redis o ciclo RODA — a trava é otimização, não pré-requisito.

    Abortar aqui pararia a ingestão por causa de uma dependência que só existe
    para evitar trabalho duplicado. Sem ela o pior caso é o comportamento que já
    existia antes da trava; com o abort, o pior caso é perda de coleta.
    """

    def _explode():
        raise ConnectionError("redis fora do ar")

    monkeypatch.setattr(celery_module, "get_worker_redis", _explode)

    with caplog.at_level("WARNING"):
        await pipeline.run_collection_once(1, "detections")

    assert spy_cycle == [(1, "detections")]
    assert "segue sem ela" in caplog.text


async def test_cycle_runs_when_acquiring_the_lock_fails(monkeypatch, spy_cycle) -> None:
    """Redis que aceita conexão mas erra no SET também não pode parar a coleta."""

    class _BrokenRedis:
        async def set(self, *a, **kw):
            raise ConnectionError("READONLY You can't write against a read only replica")

        async def eval(self, *a, **kw):  # pragma: no cover — não deve ser chamado
            raise AssertionError("não deveria tentar soltar uma trava que não pegou")

        async def aclose(self):  # pragma: no cover
            return None

    monkeypatch.setattr(celery_module, "get_worker_redis", lambda: _BrokenRedis())
    await pipeline.run_collection_once(1, "detections")
    assert spy_cycle == [(1, "detections")]


async def test_release_failure_does_not_mask_the_cycle_result(
    fake_redis, spy_cycle, monkeypatch
) -> None:
    """Falha ao SOLTAR a trava é engolida — o TTL cobre, e o ciclo já teve êxito.

    Deixar a exceção do ``finally`` subir converteria um ciclo bem-sucedido em
    falha de task: o Celery reenfileiraria e o cursor seria revertido, perdendo
    trabalho já feito por causa de um problema de limpeza.
    """
    _inspector, criados = fake_redis
    await pipeline.run_collection_once(1, "detections")
    cliente = criados[-1]

    async def _boom(*a, **kw):
        raise ConnectionError("conexão caiu antes do release")

    monkeypatch.setattr(cliente, "eval", _boom)
    monkeypatch.setattr(celery_module, "get_worker_redis", lambda: cliente)

    await pipeline.run_collection_once(1, "detections")
    assert spy_cycle == [(1, "detections"), (1, "detections")]


# ── Contrato do script de release ────────────────────────────────────────


def test_release_script_is_a_compare_and_delete() -> None:
    """Trava do subsistema: o release NUNCA pode virar um ``DEL`` cego."""
    src = pipeline._RELEASE_IF_MINE_LUA
    assert "redis.call('get', KEYS[1]) == ARGV[1]" in src
    assert "redis.call('del', KEYS[1])" in src
