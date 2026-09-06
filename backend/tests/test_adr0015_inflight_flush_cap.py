"""Teto GLOBAL de Detections por flush: invariantes (R8) e degradação declarada.

O buraco que este teto fecha: ``INFLIGHT_MAX_RULES_PER_CYCLE`` (50) e
``INFLIGHT_MAX_DEDUP_KEYS_PER_RULE_PER_CYCLE`` (256, W1.5) limitam POR REGRA e
não somam a nada. 50 × 256 = 12.800 chaves pendentes num único flush, e
``DetectionRepository.record`` COMMITA POR CHAVE — em Postgres são 5 round-trips
por chave (``pg_advisory_xact_lock``, SELECT da janela, INSERT/UPDATE, COMMIT,
refresh), ou seja até 64.000 EM SÉRIE dentro do ``finally`` do ciclo de coleta,
por org. Não é hot path por evento, e foi por isso que passou despercebido; é
trabalho serial que não coleta nada e cresce sozinho com a cardinalidade do
group_by.

A DEGRADAÇÃO é declarada e é perda de DETECÇÃO, não de performance: no teto, as
chaves excedentes não viram Detection. O que impede isso de ser a mesma classe
de erro que ``flush_lost`` acabou de curar é que a perda não é silenciosa —
contada em ``flush_cap`` ATRIBUÍDA à regra, avisada 1x por ciclo, com
``acc.matches`` intacto (a regra não parece morta) e com o corte em ROUND-ROBIN
entre as regras (uma regra ruidosa não cala as outras).

Cada afirmação acima é um teste abaixo. Nada é lido do fonte.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import logging

import pytest

from backend.app.collectors import observability_store as obs
from backend.app.collectors import otel_metrics
from backend.app.collectors.inflight import runtime as runtime_mod
from backend.app.collectors.inflight.matcher import CompiledInflightRule
from backend.app.collectors.inflight.runtime import (
    ERROR_REASONS,
    InflightAccumulator,
    flush_inflight,
)
from backend.app.core.config import settings

DURACAO = "collector_inflight_flush_seconds"


def _rule(rid: int) -> CompiledInflightRule:
    return CompiledInflightRule(
        rule_id=rid, name=f"r{rid}", severity_id=4,
        suppression_window_seconds=3600, group_by_path=("u",), clauses=(),
    )


def _acc(por_regra: dict[int, int]) -> InflightAccumulator:
    """Acumulador com ``n`` chaves DISTINTAS por regra, pela API pública."""
    acc = InflightAccumulator()
    for rid, n in por_regra.items():
        for i in range(n):
            acc.add(_rule(rid), {"u": f"entidade-{rid}-{i}"}, organization_id=1)
    return acc


@pytest.fixture
def espia(monkeypatch: pytest.MonkeyPatch):
    """Captura o ``pending`` que chegou a ``_flush_sync`` e as durações OTel."""
    visto: dict[str, object] = {"pending": None, "duracoes": []}

    def _flush(pending: dict, _org: int) -> tuple:
        # Cópia: ``flush_inflight`` não muta ``pending`` depois daqui, mas
        # guardar a referência viva faria o assert medir o estado FINAL do
        # acumulador em vez do que a escrita recebeu.
        visto["pending"] = dict(pending)
        return ()

    def _record(name: str, value: float, attrs: dict | None = None) -> None:
        if name == DURACAO:
            visto["duracoes"].append((dict(attrs or {}), float(value)))

    monkeypatch.setattr(runtime_mod, "_flush_sync", _flush)
    monkeypatch.setattr(otel_metrics, "record", _record)
    monkeypatch.setattr(obs, "record_counter", lambda *a, **k: True)
    return visto


# ── 1. Invariantes da constante (R8) ───────────────────────────────────────


def test_the_global_cap_lets_a_single_rule_reach_its_own_ceiling() -> None:
    """Abaixo do teto POR REGRA, o teto global tornaria o outro letra morta: uma
    única regra dentro do próprio limite já seria cortada, e o operador veria
    ``key_cap`` e ``flush_cap`` disputando a explicação do mesmo sintoma."""
    assert (
        int(settings.INFLIGHT_MAX_DETECTIONS_PER_FLUSH)
        >= int(settings.INFLIGHT_MAX_DEDUP_KEYS_PER_RULE_PER_CYCLE)
    )


def test_the_global_cap_actually_binds_below_the_structural_ceiling() -> None:
    """No teto estrutural (regras × chaves) ou acima dele, a constante seria
    DECORATIVA — existiria, teria comentário, e nunca morderia. É a forma de
    dívida que este subsistema já pagou três vezes."""
    estrutural = (
        int(settings.INFLIGHT_MAX_RULES_PER_CYCLE)
        * int(settings.INFLIGHT_MAX_DEDUP_KEYS_PER_RULE_PER_CYCLE)
    )
    assert estrutural == 50 * 256 == 12_800, (
        "o pior caso mudou; recalcule o teto global antes de mexer nos outros"
    )
    assert int(settings.INFLIGHT_MAX_DETECTIONS_PER_FLUSH) < estrutural


def test_the_global_cap_is_not_zero() -> None:
    """Zero não é kill-switch aqui: seria a feature inteira gravando nada, com
    a UI mostrando lista vazia. O kill-switch do subsistema é
    ``INFLIGHT_MAX_RULES_PER_CYCLE=0``, que sai ANTES de qualquer avaliação."""
    assert int(settings.INFLIGHT_MAX_DETECTIONS_PER_FLUSH) > 0


def test_flush_cap_is_a_declared_error_reason() -> None:
    """A razão vira label de métrica. Fora do enum FECHADO ela seria uma série
    que nenhum painel casa — e a perda voltaria a ser invisível."""
    assert "flush_cap" in ERROR_REASONS


# ── 2. O par POSITIVO/NEGATIVO do corte ────────────────────────────────────


@pytest.mark.asyncio
async def test_below_the_cap_nothing_is_cut_and_nothing_is_counted(
    monkeypatch: pytest.MonkeyPatch, espia
) -> None:
    """O caminho de todo mundo. Sem este par negativo, o teste de corte abaixo
    ficaria verde num call site que cortasse SEMPRE."""
    monkeypatch.setattr(settings, "INFLIGHT_MAX_DETECTIONS_PER_FLUSH", 10)
    acc = _acc({1: 3, 2: 4})

    await flush_inflight(acc, organization_id=1)

    assert len(espia["pending"]) == 7, "o flush recebeu menos do que havia pendente"
    assert "flush_cap" not in acc.errors


@pytest.mark.asyncio
async def test_above_the_cap_exactly_cap_keys_are_written_and_the_rest_is_counted(
    monkeypatch: pytest.MonkeyPatch, espia
) -> None:
    """O teto morde e a conta FECHA: escritas + cortadas = pendentes. Uma
    aritmética que não fecha é como ``flush_lost`` reportava perda maior que a
    real — o número que manda investigar prejuízo que não houve."""
    monkeypatch.setattr(settings, "INFLIGHT_MAX_DETECTIONS_PER_FLUSH", 6)
    acc = _acc({1: 10, 2: 4})

    await flush_inflight(acc, organization_id=1)

    assert len(espia["pending"]) == 6
    cortadas = sum(acc.errors["flush_cap"].values())
    assert cortadas == 14 - 6
    assert len(espia["pending"]) + cortadas == 14


@pytest.mark.asyncio
async def test_the_loss_is_attributed_to_the_rule_that_produced_it(
    monkeypatch: pytest.MonkeyPatch, espia
) -> None:
    """"Perdi 8 detecções" não diz qual regra comeu o orçamento. O item pendente
    já carrega a regra compilada, então atribuir não custa volta ao banco — e é
    o breakdown por regra que a UI lê."""
    monkeypatch.setattr(settings, "INFLIGHT_MAX_DETECTIONS_PER_FLUSH", 6)
    acc = _acc({1: 10, 2: 4})

    await flush_inflight(acc, organization_id=1)

    # Round-robin com 2 regras e orçamento 6 ⇒ 3 para cada; a regra 1 perde 7
    # das suas 10 e a regra 2 perde 1 das suas 4.
    assert acc.errors["flush_cap"] == {1: 7, 2: 1}


@pytest.mark.asyncio
async def test_a_noisy_rule_cannot_starve_the_quiet_ones(
    monkeypatch: pytest.MonkeyPatch, espia
) -> None:
    """A armadilha de segunda ordem, e a razão de o corte NÃO ser "as ``cap``
    primeiras de ``pending``": esse dict é ordenado por INSERÇÃO, então cortar a
    cauda entregaria o orçamento inteiro à regra que casou primeiro. Uma regra
    ruidosa recém-publicada calaria as de volume baixo e severidade alta — que
    são exatamente as que não se pode perder. É a mesma armadilha já escrita
    para o truncamento de REGRAS na carga ("as descartadas são sempre as mais
    recentes"), com o eixo trocado.

    A regra 1 entra primeiro e com 40 chaves; as outras três, com 2 cada."""
    monkeypatch.setattr(settings, "INFLIGHT_MAX_DETECTIONS_PER_FLUSH", 8)
    acc = _acc({1: 40, 2: 2, 3: 2, 4: 2})

    await flush_inflight(acc, organization_id=1)

    escritas_por_regra: dict[int, int] = {}
    for item in espia["pending"].values():
        rid = int(item["rule"].rule_id)
        escritas_por_regra[rid] = escritas_por_regra.get(rid, 0) + 1

    assert sum(escritas_por_regra.values()) == 8
    assert set(escritas_por_regra) == {1, 2, 3, 4}, (
        f"regra(s) caladas pelo corte: {escritas_por_regra} — o orçamento foi "
        "para quem chegou primeiro"
    )
    # 2 rodadas completas (4 regras × 2) esgotam o orçamento: as três quietas
    # saem INTEIRAS e a ruidosa fica com a mesma fatia de qualquer uma delas.
    assert escritas_por_regra == {1: 2, 2: 2, 3: 2, 4: 2}


@pytest.mark.asyncio
async def test_the_matches_counter_is_untouched_by_the_cut(
    monkeypatch: pytest.MonkeyPatch, espia
) -> None:
    """O que se perdeu foi a ESCRITA, não o match. Zerar ``matches`` junto faria
    a regra parecer morta no painel — e o operador desligaria justamente a regra
    que está funcionando demais."""
    monkeypatch.setattr(settings, "INFLIGHT_MAX_DETECTIONS_PER_FLUSH", 3)
    acc = _acc({1: 10})
    assert acc.matches == {1: 10}

    await flush_inflight(acc, organization_id=1)

    assert acc.matches == {1: 10}


@pytest.mark.asyncio
async def test_a_cut_key_is_never_also_counted_as_flush_lost(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """As duas razões medem perdas DIFERENTES e não podem se sobrepor: uma
    chave cortada pelo teto nunca chegou a ser tentada, e contá-la também em
    ``flush_lost`` inflaria a única série que responde "quanto de detecção o
    cliente deixou de receber" — exatamente o erro de que ``flush_lost`` acabou
    de ser curado, com o sinal trocado."""
    monkeypatch.setattr(settings, "INFLIGHT_MAX_DETECTIONS_PER_FLUSH", 6)
    monkeypatch.setattr(obs, "record_counter", lambda *a, **k: True)

    def _boom(*_a: object, **_k: object):
        # Falha ANTES de commitar qualquer coisa: tudo que sobreviveu ao teto
        # vira ``flush_lost``, e só isso.
        raise runtime_mod.InflightFlushInterrupted((), ())

    monkeypatch.setattr(runtime_mod, "_flush_sync", _boom)
    acc = _acc({1: 10, 2: 4})

    await flush_inflight(acc, organization_id=1)

    cortadas = sum(acc.errors["flush_cap"].values())
    perdidas = sum(acc.errors["flush_lost"].values())
    assert cortadas == 8 and perdidas == 6
    assert cortadas + perdidas == 14, "a soma das duas perdas tem de fechar com o total"


# ── 3. Fail-open não é fail-SILENT ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_cut_warns_once_per_cycle_with_no_value_from_the_event(
    monkeypatch: pytest.MonkeyPatch, espia, caplog: pytest.LogCaptureFixture
) -> None:
    """UM aviso por ciclo — N regras cortadas são N sintomas de UMA causa (o
    orçamento), e um aviso por regra seria a amplificação de log que o
    rate-limit existe para impedir. E só INTEIROS nos args: nome de regra e
    valor de group_by (cmdline, usuário, host) vêm do EVENTO e nunca entram
    neste log."""
    monkeypatch.setattr(settings, "INFLIGHT_MAX_DETECTIONS_PER_FLUSH", 4)
    acc = _acc({1: 10, 2: 10, 3: 10})

    with caplog.at_level(logging.WARNING, logger=runtime_mod.logger.name):
        await flush_inflight(acc, organization_id=1)

    avisos = [
        r for r in caplog.records
        if r.name == runtime_mod.logger.name and "teto global" in r.getMessage()
    ]
    assert len(avisos) == 1, f"o aviso do teto saiu {len(avisos)}x num só flush"
    assert avisos[0].args, "sem args, o assert abaixo aprovaria por vacuidade"
    assert all(isinstance(a, int) for a in avisos[0].args), (
        f"argumento não-inteiro no aviso do teto: {avisos[0].args}"
    )


@pytest.mark.asyncio
async def test_the_flush_duration_is_measured_once_per_flush_without_labels(
    monkeypatch: pytest.MonkeyPatch, espia
) -> None:
    """O teto conta o que já se perdeu; esta série mostra a APROXIMAÇÃO. Sem
    ela, o operador só descobre o problema depois de ele virar perda — que é a
    forma do incidente do watermark. Sem labels: ``org_id`` daria uma série por
    tenant numa métrica lida como saúde do worker."""
    monkeypatch.setattr(settings, "INFLIGHT_MAX_DETECTIONS_PER_FLUSH", 500)
    acc = _acc({1: 3})

    await flush_inflight(acc, organization_id=1)

    assert len(espia["duracoes"]) == 1, "a duração do flush não foi medida"
    attrs, valor = espia["duracoes"][0]
    assert attrs == {}
    assert set(otel_metrics.labels_for(DURACAO)) == set()
    assert valor >= 0.0


@pytest.mark.asyncio
async def test_the_duration_is_measured_even_when_the_write_blows_up(
    monkeypatch: pytest.MonkeyPatch, espia
) -> None:
    """É no caminho de FALHA que a medida importa: um flush que estoura o
    soft-timeout só existe no ramo de exceção, e medir só o caminho feliz
    esconderia justamente o percentil que manda mexer no teto."""
    def _boom(*_a: object, **_k: object):
        raise runtime_mod.InflightFlushInterrupted((), ())

    monkeypatch.setattr(runtime_mod, "_flush_sync", _boom)
    await flush_inflight(_acc({1: 3}), organization_id=1)

    assert len(espia["duracoes"]) == 1


@pytest.mark.asyncio
async def test_an_empty_flush_measures_nothing(
    monkeypatch: pytest.MonkeyPatch, espia
) -> None:
    """Par negativo da série de duração: um ciclo sem chave pendente não escreve
    nada e não pode empurrar zeros para o histograma — o p95 seria arrastado
    para baixo pelos ciclos ociosos, que são a maioria."""
    await flush_inflight(InflightAccumulator(), organization_id=1)

    assert espia["duracoes"] == []
    assert espia["pending"] is None


@pytest.mark.asyncio
async def test_a_broken_cap_degrades_to_writing_everything_not_to_writing_nothing(
    monkeypatch: pytest.MonkeyPatch, espia
) -> None:
    """A direção da degradação do PRÓPRIO teto.

    ``_apply_flush_cap`` roda FORA do ``try`` que contabiliza ``flush_lost``:
    uma exceção ali pularia a escrita inteira E a contagem da perda — nenhuma
    Detection gravada e nenhuma série dizendo que faltou algo, que é a mentira
    completa. Falhando, o teto tem de deixar de CORTAR, não deixar de GRAVAR:
    escrever tudo é lento, perder tudo calado é o que não se pode fazer.
    """
    chamadas = {"n": 0}

    def _boom(_acc: object) -> int:
        chamadas["n"] += 1
        raise RuntimeError("bug no seletor round-robin")

    monkeypatch.setattr(settings, "INFLIGHT_MAX_DETECTIONS_PER_FLUSH", 2)
    monkeypatch.setattr(runtime_mod, "_apply_flush_cap", _boom)
    acc = _acc({1: 5})

    await flush_inflight(acc, organization_id=1)

    assert chamadas["n"] == 1, "o dublê nem foi chamado — nada foi exercitado"
    assert espia["pending"] is not None and len(espia["pending"]) == 5, (
        "o teto quebrado levou a escrita junto: 0 Detection gravada e nenhuma "
        "razão contada — a perda ficaria invisível dos dois lados"
    )
