"""Compensação de claim de dedupe — sem perda silenciosa (ADR-0015, Fase 2).

O ``claim`` é o único guard de idempotência do hot path. Um evento reivindicado
mas NÃO entregue é perda silenciosa: o run falha, o cursor não avança, o retry
re-vê o evento, ``claim`` devolve False e ele é descartado como "duplicado". O
log de segurança some sem erro, sem métrica e sem rastro.

Existia compensação, mas gated por ``EVENT_DATAPLANE == "kafka"`` — ou seja, o
data-plane DEFAULT não tinha nenhuma. A claim é risco do PIPELINE, não do
transporte.

Este arquivo trava a IDENTIDADE DE CONSERVAÇÃO: todo id reivindicado ou foi
entregue (e liquidado) ou foi solto. Nunca as duas, nunca nenhuma.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import inspect

import pytest

from backend.app.collectors import pipeline
from backend.app.collectors.state import dedupe


class _FakeRedis:
    """Redis mínimo: registra os DEL recebidos."""

    def __init__(self, fail: bool = False) -> None:
        self.deleted: list[str] = []
        self.fail = fail

    async def delete(self, *keys):
        if self.fail:
            raise ConnectionError("redis fora do ar")
        self.deleted.extend(keys)
        return len(keys)


@pytest.mark.asyncio
async def test_release_many_deletes_every_key_in_one_call():
    r = _FakeRedis()
    n = await dedupe.release_many(r, 7, ["a", "b", "c"])
    assert n == 3
    assert len(r.deleted) == 3
    assert all("7" in k for k in r.deleted), "a chave precisa ser org/integração-escopada"


@pytest.mark.asyncio
async def test_release_many_is_a_noop_on_empty_input():
    """Guard de R2: nada a soltar não pode custar um round-trip."""
    r = _FakeRedis()
    assert await dedupe.release_many(r, 7, []) == 0
    assert await dedupe.release_many(r, 7, [None, ""]) == 0
    assert r.deleted == []


@pytest.mark.asyncio
async def test_release_many_propagates_failure_to_the_best_effort_caller():
    """Falha de Redis PRECISA subir para o chamador logar. Engolir aqui deixaria
    a perda invisível — o resíduo é claim órfã até o TTL."""
    with pytest.raises(ConnectionError):
        await dedupe.release_many(_FakeRedis(fail=True), 7, ["a"])


# ── Guards estruturais sobre o ciclo de coleta ───────────────────────────────

def test_compensation_is_unconditional():
    """O gate por data-plane sumiu: era ele que deixava o caminho default nu."""
    src = inspect.getsource(pipeline._run_collection_once)
    assert "_track_claims" not in src, (
        "a compensação voltou a ser gated por data-plane — o caminho default "
        "fica sem nenhuma proteção contra claim órfã"
    )
    assert "unsettled_claims" in src


def test_release_runs_in_finally_not_only_in_except():
    """``except Exception`` não pega ``WorkerShutdown`` (BaseException). No
    ``finally``, o kill do worker também solta as claims."""
    src = inspect.getsource(pipeline._run_collection_once)
    idx_finally = src.rindex("finally:")
    idx_release = src.index("release_many")
    assert idx_release > idx_finally, "release_many precisa estar no finally"


def test_release_happens_before_the_redis_client_is_closed():
    """Soltar depois do ``aclose`` mandaria o DEL contra um cliente fechado —
    a compensação viraria no-op silencioso, que é pior que não existir."""
    src = inspect.getsource(pipeline._run_collection_once)
    assert src.index("release_many") < src.index("await redis.aclose()")


def test_settlement_happens_after_the_durable_handoff():
    """Liquidar ANTES do ``_enqueue_dispatch`` converteria falha de enqueue em
    perda: a claim ficaria de pé e o retry descartaria o evento."""
    src = inspect.getsource(pipeline._run_collection_once)
    first_dispatch = src.index("_enqueue_dispatch(batch, dispatch_routes)")
    first_settle = src.index("unsettled_claims.difference_update")
    assert first_settle > first_dispatch, (
        "a liquidação precede o hand-off durável — inverte a direção do risco"
    )


def test_every_dispatch_site_settles():
    """São dois: o flush por tamanho/tempo dentro do laço e o dreno TERMINAL.

    Sem o terminal, uma integração que devolve poucos eventos e encerra a página
    nunca atinge o batch nem o gatilho de tempo (que é avaliado dentro do corpo
    do laço, não por timer) — e as claims desses eventos seriam soltas à toa.
    """
    src = inspect.getsource(pipeline._run_collection_once)
    assert src.count("_enqueue_dispatch(batch, dispatch_routes)") == 2
    assert src.count("unsettled_claims.difference_update(batch_msg_ids)") == 2


def test_batch_and_msg_ids_stay_aligned():
    """``batch_msg_ids`` é paralelo 1:1 com ``batch``: os dois crescem e zeram
    juntos. Desalinhamento liquidaria a claim errada."""
    src = inspect.getsource(pipeline._run_collection_once)
    assert src.count("batch_msg_ids.append(msg_id)") == 1
    assert src.count("batch.append(envelope)") == 1
    assert src.count("batch_msg_ids = []") == 2, (
        "batch_msg_ids precisa zerar nos dois pontos em que batch zera"
    )


def test_envelope_is_not_mutated_to_carry_the_msg_id():
    """O envelope é serializado para o data-plane; carregar um campo transiente
    nele vazaria para o destino."""
    src = inspect.getsource(pipeline._run_collection_once)
    assert "_dedupe_msg_id" not in src
