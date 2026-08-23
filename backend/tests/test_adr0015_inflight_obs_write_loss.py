"""A face de ESCRITA do zero-vs-null nos contadores de disparo por regra.

O lote anterior fechou a face de LEITURA: ``GET /{rule_id}/metrics`` usa
``read_window_total_strict`` e devolve ``null`` ("não sei") em vez de ``0.0``
quando o Redis cai NA LEITURA. Este arquivo cobre o passo ANTERIOR, onde a
mesma distinção era destruída e nada registrava: com o Redis fora na ESCRITA a
chave ``obs:rule:{id}:{metric}`` nunca nasce; depois a leitura ENCONTRA a
ausência — hash vazio, soma ``0.0``, zero exceções — e o operador lê
"0 disparos" para uma regra que disparou.

Nenhuma leitura conserta isso, por construção: ausência de chave e "a regra não
disparou" são o MESMO estado no Redis, então nem o ``strict`` os separa. A perda
só é conhecível no lado que a produz. É o que estes testes travam: ela vira
contador (``collector_inflight_rule_metric_write_failures_total``) e WARNING
rate-limitado, sem nunca derrubar o flush.

Comportamento puro (sem ``source_only``) de propósito: o ponto é rodar também na
imagem Cython, onde ``runtime.py`` é ``.so`` e ler fonte é impossível.

ARMADILHA herdada dos vizinhos: ``OTEL_ENABLED`` é ``False`` por default e
``otel_metrics.count`` retorna ANTES de tocar o instrumento. A espionagem é
sobre ``otel_metrics.count``, nunca sobre o instrumento.
"""

from __future__ import annotations

import logging
import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import fakeredis
import pytest

from backend.app.collectors import observability_store as obs
from backend.app.collectors import otel_metrics
from backend.app.collectors.inflight.runtime import (
    ERROR_REASONS,
    RULE_METRIC_BUCKET_SECONDS,
    RULE_METRIC_TTL_SECONDS,
    RULE_METRIC_WINDOW_MINUTES,
    UNATTRIBUTED_ERROR_REASONS,
    InflightAccumulator,
    flush_inflight,
)

#: A série que torna a perda visível. Nome literal: é o que o alerta do Grafana
#: casa, e um rename silencioso quebraria o alerta sem quebrar o código.
SERIE = "collector_inflight_rule_metric_write_failures_total"

#: Trecho estável do WARNING. Casar pelo ``msg`` (format string) e não pela
#: mensagem renderizada mantém o assert imune a mudança de argumento.
TRECHO_DO_AVISO = "recusou a escrita dos contadores"


@pytest.fixture
def emitidos(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict, float]]:
    """``(nome, attrs, valor)`` de tudo que os call sites empurram para o OTel."""
    capturado: list[tuple[str, dict, float]] = []
    monkeypatch.setattr(
        otel_metrics,
        "count",
        lambda name, value=1, attrs=None: capturado.append(
            (name, dict(attrs or {}), value)
        ),
    )
    return capturado


def _falhas(emitidos: list[tuple[str, dict, float]]) -> list[tuple[str, dict, float]]:
    return [e for e in emitidos if e[0] == SERIE]


def _avisos(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        rec
        for rec in caplog.records
        if rec.levelno == logging.WARNING and TRECHO_DO_AVISO in rec.msg
    ]


def _store_fora(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redis indisponível NA ESCRITA. Patch em ``_redis`` e não em
    ``record_counter``: o ponto do trabalho é que ``record_counter`` engole a
    exceção internamente — trocar a função inteira por um duplo que levanta
    testaria um caminho que a produção não percorre."""

    def _boom() -> object:
        raise RuntimeError("Redis indisponível")

    monkeypatch.setattr(obs, "_redis", _boom)


def _store_ok(monkeypatch: pytest.MonkeyPatch) -> "fakeredis.FakeStrictRedis":
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(obs, "_redis", lambda: r)
    return r


def _acc(matches: dict[int, int] | None = None) -> InflightAccumulator:
    acc = InflightAccumulator()
    acc.matches.update(matches or {7: 3})
    return acc


def _ler_disparos(rule_id: int) -> float:
    return obs.read_window_total(
        "rule", str(rule_id), "matches",
        minutes=RULE_METRIC_WINDOW_MINUTES,
        bucket_seconds=RULE_METRIC_BUCKET_SECONDS,
        ttl_seconds=RULE_METRIC_TTL_SECONDS,
    )


# ── 1. Falha de escrita: contada E avisada ─────────────────────────────────


@pytest.mark.asyncio
async def test_a_lost_write_is_counted_and_warned(
    monkeypatch: pytest.MonkeyPatch,
    emitidos: list[tuple[str, dict, float]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Redis fora na ESCRITA ⇒ a perda existe como número e como aviso.

    Sem isto, a única evidência era um ``logger.debug`` — e DEBUG em produção
    não existe."""
    _store_fora(monkeypatch)

    with caplog.at_level(logging.WARNING):
        await flush_inflight(_acc({7: 3}), organization_id=1)

    assert _falhas(emitidos) == [(SERIE, {"metric": "matches"}, 1)]
    assert len(_avisos(caplog)) == 1


@pytest.mark.asyncio
async def test_a_successful_write_neither_counts_nor_warns(
    monkeypatch: pytest.MonkeyPatch,
    emitidos: list[tuple[str, dict, float]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """PAR POSITIVO do teste acima. Sem ele, um código que contasse falha
    SEMPRE passaria pelo negativo — e o alerta do operador viraria ruído
    permanente, que é a mesma cegueira com outro nome.

    O ``assert`` do meio é o anti-vacuidade: "não houve falha" só significa
    alguma coisa se a gravação de fato aconteceu. Um ``return`` antecipado em
    ``_mirror_rule_metric`` passaria pelos outros dois asserts sem tocar o
    store."""
    _store_ok(monkeypatch)

    with caplog.at_level(logging.WARNING):
        await flush_inflight(_acc({7: 3}), organization_id=1)

    assert _ler_disparos(7) == 3.0, "a escrita tem de ter acontecido de verdade"
    assert _falhas(emitidos) == []
    assert _avisos(caplog) == []


@pytest.mark.asyncio
async def test_a_double_without_a_verdict_is_never_counted_as_loss(
    monkeypatch: pytest.MonkeyPatch,
    emitidos: list[tuple[str, dict, float]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``record_counter`` só passou a devolver veredito agora, e é ponto de
    monkeypatch em dezenas de testes deste repo e do EE, cujos duplos devolvem
    ``None``. Ler ``None`` como falha inverteria o sinal: a série de perda
    passaria a contar prejuízo que não houve — o mesmo erro de que
    ``flush_lost`` acabou de ser curado, com o sinal trocado.

    O par positivo aqui é o contador de CHAMADAS: sem ele, um
    ``_record_rule_metric`` que nunca gravasse nada também passaria."""
    chamadas: list[tuple] = []
    monkeypatch.setattr(
        obs,
        "record_counter",
        lambda *a, **k: chamadas.append((a, k)),  # devolve None: sem veredito
    )

    with caplog.at_level(logging.WARNING):
        await flush_inflight(_acc({7: 3}), organization_id=1)

    assert len(chamadas) == 1, "o duplo tem de ter sido chamado"
    assert _falhas(emitidos) == []
    assert _avisos(caplog) == []


# ── 2. Rate-limit: 1 aviso por CICLO, contador sem rate-limit ──────────────


@pytest.mark.asyncio
async def test_the_warning_is_emitted_once_per_cycle_even_with_many_rules(
    monkeypatch: pytest.MonkeyPatch,
    emitidos: list[tuple[str, dict, float]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """CONTAGEM, não ``not any(...)``: um assert negativo aqui aprovaria por
    vacuidade se o aviso parasse de sair por completo.

    N regras falhando no mesmo ciclo são N sintomas de UMA causa (o store é um
    só). Avisar por regra trocaria degradação de observabilidade por
    amplificação de escrita no log, que é o dano maior.

    O segundo flush, com acumulador NOVO, é o que separa "1x por ciclo" de
    "1x por processo": o rate-limit mora no acumulador, que nasce a cada ciclo.
    Um ``_logged_once`` promovido a global de módulo passaria pela primeira
    metade deste teste e reprovaria na segunda."""
    _store_fora(monkeypatch)
    regras = {10: 1, 11: 2, 12: 3, 13: 4, 14: 5}

    with caplog.at_level(logging.WARNING):
        await flush_inflight(_acc(regras), organization_id=1)

        assert len(_avisos(caplog)) == 1
        # O log é rate-limitado; o CONTADOR não — é ele que mede o tamanho do
        # buraco, e uma regra por evento perdido é exatamente o que se quer ali.
        assert len(_falhas(emitidos)) == len(regras)

        await flush_inflight(_acc(regras), organization_id=1)  # ciclo NOVO

    assert len(_avisos(caplog)) == 2
    assert len(_falhas(emitidos)) == 2 * len(regras)


# ── 3. Best-effort preservado ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_flush_inflight_still_never_raises_when_the_store_is_down(
    monkeypatch: pytest.MonkeyPatch, emitidos: list[tuple[str, dict, float]]
) -> None:
    """Regressão do contrato: ``flush_inflight`` roda no ``finally`` do ciclo de
    coleta, e uma exceção nova ali SUBSTITUI a exceção original que estivesse se
    propagando (semântica de ``finally`` do Python). Contar a perda não pode
    custar isso.

    O par positivo é a última linha: "não levantou" seria trivialmente
    verdadeiro num flush que não fizesse nada."""
    _store_fora(monkeypatch)
    acc = _acc({7: 3})
    acc.overflow[7] = 2
    acc.count_error("key_cap", 7, 4)

    await flush_inflight(acc, organization_id=1)  # não pode levantar

    assert len(_falhas(emitidos)) == 3  # matches + overflow + err_key_cap


@pytest.mark.asyncio
async def test_counting_the_loss_never_breaks_the_otel_series_it_shadows(
    monkeypatch: pytest.MonkeyPatch, emitidos: list[tuple[str, dict, float]]
) -> None:
    """A contabilidade da perda é um EXTRA: o que o OTel receberia sem ela tem
    de continuar chegando intacto, inclusive com o store fora."""
    _store_fora(monkeypatch)
    acc = _acc({9: 3})
    acc.count_error("group_by_unresolved", 9, 3)

    await flush_inflight(acc, organization_id=1)

    assert [e for e in emitidos if e[0] == "collector_inflight_matches_total"] == [
        ("collector_inflight_matches_total", {"rule_id": "9"}, 3)
    ]
    assert [e for e in emitidos if e[0] == "collector_inflight_errors_total"] == [
        ("collector_inflight_errors_total", {"reason": "group_by_unresolved"}, 3)
    ]


# ── 4. Invariante do label ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_metric_label_is_a_closed_enum_and_carries_no_rule_id(
    monkeypatch: pytest.MonkeyPatch, emitidos: list[tuple[str, dict, float]]
) -> None:
    """IGUALDADE contra o conjunto derivado das constantes do módulo, não
    inclusão: ``⊆`` é widening-safe e deixaria entrar tanto um valor novo não
    declarado quanto um valor declarado que nenhum call site emite mais.

    ``rule_id`` fora do label é a mesma recusa já escrita para
    ``collector_inflight_errors_total``: id global, sem TTL no OTLP para
    envelhecer a série de uma regra apagada. O breakdown por regra vive no
    observability_store — que é justamente o que está fora do ar quando esta
    série sobe."""
    _store_fora(monkeypatch)
    atribuiveis = set(ERROR_REASONS) - set(UNATTRIBUTED_ERROR_REASONS)

    acc = _acc({7: 3})
    acc.overflow[7] = 2
    for reason in atribuiveis:
        acc.count_error(reason, 7, 1)

    await flush_inflight(acc, organization_id=1)

    familias = {attrs["metric"] for _n, attrs, _v in _falhas(emitidos)}
    assert familias == {"matches", "overflow"} | {f"err_{r}" for r in atribuiveis}

    for _n, attrs, _v in _falhas(emitidos):
        assert set(attrs) == set(otel_metrics.labels_for(SERIE)) == {"metric"}
        assert "7" not in attrs.values(), "rule_id vazou para o label da série"


@pytest.mark.asyncio
async def test_the_warning_never_carries_a_value_coming_from_an_event(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """O único argumento do aviso é a família do contador — enum fechado,
    derivado de constantes deste módulo. Nome de regra e valor de group_by
    (cmdline, usuário, host) vêm do EVENTO e nunca podem chegar ao log deste
    caminho, que dispara em rajada quando o Redis cai."""
    _store_fora(monkeypatch)
    permitidos = {"matches", "overflow"} | {f"err_{r}" for r in ERROR_REASONS}

    with caplog.at_level(logging.WARNING):
        await flush_inflight(_acc({7: 3}), organization_id=1)

    (aviso,) = _avisos(caplog)
    assert aviso.args, "sem argumento, o assert abaixo aprovaria por vacuidade"
    assert set(aviso.args) <= permitidos
