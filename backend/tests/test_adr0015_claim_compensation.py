"""Compensação de claim de dedupe — sem perda silenciosa (ADR-0015, Fase 2).

O ``claim`` é o único guard de idempotência do hot path. Um evento reivindicado
mas NÃO entregue é perda silenciosa: o run falha, o cursor não avança, o retry
re-vê o evento, ``claim`` devolve False e ele é descartado como "duplicado". O
log de segurança some sem erro, sem métrica e sem rastro.

Existia compensação, mas gated pelo data-plane — o caminho default não tinha
nenhuma. A claim é risco do PIPELINE, não do transporte. Desde o ADR-0018 Onda 5
a claim mora na janela em memória da partição (não há mais chave Redis), e o
lote que falha volta do Kafka: soltá-la é o que impede a janela de descartá-lo.

Este arquivo trava a IDENTIDADE DE CONSERVAÇÃO: todo id reivindicado ou foi
entregue (e liquidado) ou foi solto. Nunca as duas, nunca nenhuma.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import ast
import inspect
import re

import pytest

from backend.app.collectors import pipeline
from backend.app.collectors.state.dedupe_window import DedupeWindow


def test_release_returns_the_claims_to_the_window():
    """Soltar devolve o id à janela: a reentrega do lote volta a ser processada."""
    w = DedupeWindow(max_ids=100, ttl_seconds=3600)
    assert w.claim(7, "a") and w.claim(7, "b")
    assert w.release(7, ["a", "b"]) == 2
    assert w.claim(7, "a") is True and w.claim(7, "b") is True


def test_release_is_a_noop_on_empty_input():
    w = DedupeWindow(max_ids=100, ttl_seconds=3600)
    assert w.release(7, []) == 0


# ── Guards estruturais sobre o estágio de processamento ──────────────────────
#
# Desde a ADR-0018 Onda 3 a compensação mora em ``pipeline.StreamProcessor``
# (``close`` solta, ``flush`` liquida). Desde a Onda 5 há UM chamador: o processor
# do tópico raw (``process_raw_events``) — o ciclo de coleta e o backfill só
# gravam no raw. A invariante tem duas metades: o estágio solta no ``close``, e o
# chamador chama o ``close`` num ``finally`` e antes de fechar o Redis.

_CALLERS = ("process_raw_events",)


def _caller_sources() -> list[tuple[str, str]]:
    return [(name, inspect.getsource(getattr(pipeline, name))) for name in _CALLERS]


@pytest.mark.source_only  # lê o .py; na imagem Cython o fonte não existe
def test_every_stream_processor_caller_is_guarded():
    """Um chamador novo do estágio fora da lista acima escaparia das duas
    metades da invariante sem nenhum teste reclamar."""
    import pathlib

    root = pathlib.Path(pipeline.__file__).resolve().parents[1]
    callers = sorted(
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if "/tests/" not in str(path) and "StreamProcessor(" in path.read_text()
    )
    assert callers == ["collectors/pipeline.py"], callers
    src = inspect.getsource(pipeline)
    assert src.count("StreamProcessor(") == 1, "o estágio ganhou um chamador fora da lista"

@pytest.mark.source_only  # lê o .py; na imagem Cython o fonte não existe
def test_compensation_is_unconditional():
    """O gate por data-plane sumiu: era ele que deixava o caminho default nu."""
    src = inspect.getsource(pipeline.StreamProcessor)
    assert "_track_claims" not in src, (
        "a compensação voltou a ser gated por data-plane — o caminho default "
        "fica sem nenhuma proteção contra claim órfã"
    )
    assert "unsettled_claims" in src


@pytest.mark.source_only  # lê o .py; na imagem Cython o fonte não existe
def test_release_runs_in_finally_not_only_in_except():
    """``except Exception`` não pega ``WorkerShutdown`` (BaseException). No
    ``finally``, o kill do worker também solta as claims."""
    close_src = inspect.getsource(pipeline.StreamProcessor.close)
    assert "self._dedupe.release(" in close_src, "o close do estágio precisa soltar as claims"
    for name, src in _caller_sources():
        idx_finally = src.rindex("finally:")
        idx_close = src.index("await processor.close()")
        assert idx_close > idx_finally, f"{name}: o close do estágio precisa estar no finally"


@pytest.mark.source_only  # lê o .py; na imagem Cython o fonte não existe
def test_release_happens_before_the_redis_client_is_closed():
    """Soltar depois do ``aclose`` mandaria o DEL contra um cliente fechado —
    a compensação viraria no-op silencioso, que é pior que não existir."""
    for name, src in _caller_sources():
        assert src.index("await processor.close()") < src.index("await redis.aclose()"), name


def _sites_de_handoff_e_liquidacao() -> tuple[list[int], list[int]]:
    """Acha por AST os hand-offs duráveis e as liquidações de claim.

    Por AST, e não por casamento de string, por um motivo aprendido na prática:
    a versão anterior deste guard procurava o texto literal
    ``_enqueue_dispatch(batch, dispatch_routes)``. O commit b65e9b0 quebrou a
    chamada em várias linhas e acrescentou um kwarg. A string sumiu, os dois
    call sites continuaram lá, e o guard passou a falhar sem que nada de
    semântico tivesse mudado.

    Um guard que quebra com formatação treina o time a ignorá-lo, e um guard
    ignorado é pior que nenhum: ele ocupa o lugar de um que funcionaria.
    """
    arvore = ast.parse(inspect.getsource(pipeline).lstrip())
    alvo = next(
        n for n in ast.walk(arvore)
        if isinstance(n, ast.ClassDef) and n.name == "StreamProcessor"
    )

    handoffs: list[int] = []
    liquidacoes: list[int] = []
    for n in ast.walk(alvo):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Name) and f.id == "_enqueue_dispatch":
            handoffs.append(n.lineno)
        alvo_set = f.value if isinstance(f, ast.Attribute) else None
        nome_set = (
            alvo_set.id if isinstance(alvo_set, ast.Name)
            else alvo_set.attr if isinstance(alvo_set, ast.Attribute)
            else None
        )
        if (
            isinstance(f, ast.Attribute)
            and f.attr == "difference_update"
            and nome_set == "unsettled_claims"
        ):
            liquidacoes.append(n.lineno)
    return sorted(handoffs), sorted(liquidacoes)


@pytest.mark.source_only  # lê o .py; na imagem Cython o fonte não existe
def test_settlement_happens_after_the_durable_handoff():
    """Liquidar ANTES do ``_enqueue_dispatch`` converteria falha de enqueue em
    perda: a claim ficaria de pé e o retry descartaria o evento como duplicado,
    sem exceção, sem DLQ e sem métrica que distinga isso de dedupe legítimo.
    """
    handoffs, liquidacoes = _sites_de_handoff_e_liquidacao()

    assert handoffs, "nenhum hand-off encontrado: o guard perdeu o alvo"
    for handoff, liquidacao in zip(handoffs, liquidacoes):
        assert liquidacao > handoff, (
            f"liquidação na linha {liquidacao} precede o hand-off da linha "
            f"{handoff}: inverte a direção do risco e vira perda silenciosa"
        )


@pytest.mark.source_only  # lê o .py; na imagem Cython o fonte não existe
def test_every_dispatch_site_settles():
    """Todo hand-off tem a sua liquidação, um para um.

    Desde a Onda 3 há um site só, o ``flush`` do estágio, chamado pelo gatilho
    de tamanho/tempo E como dreno TERMINAL por todo chamador. Sem o terminal,
    uma integração que devolve poucos eventos e encerra a página nunca atinge o
    batch nem o gatilho de tempo (avaliado dentro do corpo do laço, não por
    timer) e as claims desses eventos seriam soltas à toa.

    A asserção é sobre a IGUALDADE entre as duas contagens: um segundo site de
    despacho é legítimo, desde que ele também liquide.
    """
    handoffs, liquidacoes = _sites_de_handoff_e_liquidacao()
    for name, src in _caller_sources():
        assert re.search(r"await processor\.flush\(\)", src), f"{name}: sem flush terminal"

    assert len(handoffs) == len(liquidacoes), (
        f"{len(handoffs)} hand-off(s) para {len(liquidacoes)} liquidação(ões). "
        f"Hand-offs nas linhas {handoffs}, liquidações em {liquidacoes}. "
        "Um despacho sem liquidação solta a claim à toa e o evento é reentregue."
    )


@pytest.mark.source_only  # lê o .py; na imagem Cython o fonte não existe
def test_batch_and_msg_ids_stay_aligned():
    """``batch_msg_ids`` é paralelo 1:1 com ``batch``: os dois crescem e zeram
    juntos. Desalinhamento liquidaria a claim errada."""
    src = inspect.getsource(pipeline.StreamProcessor)
    assert src.count("self.batch_msg_ids.append(msg_id)") == 1
    assert src.count("self.batch.append(envelope)") == 1
    assert src.count("self.batch_msg_ids = []") == src.count("self.batch = []") >= 1, (
        "batch_msg_ids precisa zerar nos mesmos pontos em que batch zera"
    )


@pytest.mark.source_only  # lê o .py; na imagem Cython o fonte não existe
def test_envelope_is_not_mutated_to_carry_the_msg_id():
    """O envelope é serializado para o data-plane; carregar um campo transiente
    nele vazaria para o destino."""
    src = inspect.getsource(pipeline.StreamProcessor)
    assert "_dedupe_msg_id" not in src
