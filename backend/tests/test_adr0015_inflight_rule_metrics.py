"""Contador de "disparos nas últimas 24h" por regra de correlação (ADR-0015).

``flush_inflight`` espelha ``acc.matches``/``acc.overflow`` no
``observability_store`` (Redis nativo, kind="rule") — a UI precisa disso
porque o instrumento OTel equivalente (``INFLIGHT_MATCHES``) é NO-OP quando
``OTEL_ENABLED=False`` (default da instalação padrão): sem este espelhamento
o contador ficaria permanentemente zerado fora de um deployment com OTel
Collector configurado.

DUAS métricas (``matches`` e ``overflow``), não uma: a razão entre elas é o
diagnóstico de cardinalidade de ``group_by`` estourando o teto por
regra/ciclo — ver o docstring de ``flush_inflight``.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import fakeredis
import pytest

from backend.app.collectors import observability_store as obs
from backend.app.collectors.inflight.runtime import (
    RULE_METRIC_BUCKET_SECONDS,
    RULE_METRIC_TTL_SECONDS,
    RULE_METRIC_WINDOW_MINUTES,
    InflightAccumulator,
    flush_inflight,
)


def _fake_redis() -> "fakeredis.FakeStrictRedis":
    return fakeredis.FakeStrictRedis(decode_responses=True)


def _read_rule_window(kind_oid: str, metric: str) -> float:
    return obs.read_window_total(
        "rule", kind_oid, metric,
        minutes=RULE_METRIC_WINDOW_MINUTES,
        bucket_seconds=RULE_METRIC_BUCKET_SECONDS,
        ttl_seconds=RULE_METRIC_TTL_SECONDS,
    )


# ── R8: invariantes das constantes novas ───────────────────────────────────


def test_rule_metric_bucket_is_hourly_not_per_minute() -> None:
    """A feature inteira depende de NÃO regredir para per-minute: 1440 campos
    por hash por regra (per-minute) vs 24 (horário) numa janela de 24h."""
    assert RULE_METRIC_BUCKET_SECONDS == 60 * 60


def test_rule_metric_window_is_24h() -> None:
    assert RULE_METRIC_WINDOW_MINUTES == 24 * 60


def test_rule_metric_ttl_covers_the_full_24h_read_window() -> None:
    """A MESMA invariante que este trabalho existe pra corrigir: o TTL default
    do observability_store (3h) é insuficiente por construção pra uma janela
    de 24h — os buckets do início da janela expirariam antes da leitura.
    ``RULE_METRIC_TTL_SECONDS`` tem que superar a janela, com folga."""
    assert RULE_METRIC_TTL_SECONDS >= RULE_METRIC_WINDOW_MINUTES * 60
    assert RULE_METRIC_TTL_SECONDS - RULE_METRIC_WINDOW_MINUTES * 60 == 60 * 60  # folga de 1h, explícita
    # trava o bug original verificado: TTL default (3h) NÃO cobre 24h.
    assert obs._TTL_SECONDS < RULE_METRIC_WINDOW_MINUTES * 60


def test_rule_metric_hash_field_count_stays_small() -> None:
    """Regressão mecânica do "1440 campos" citado no contrato: com bucket
    horário, o hash de uma regra nunca passa de TTL/bucket campos — ordens de
    magnitude menor que o equivalente per-minute pela MESMA retenção."""
    max_fields_hourly = RULE_METRIC_TTL_SECONDS // RULE_METRIC_BUCKET_SECONDS
    max_fields_per_minute_equivalent = RULE_METRIC_TTL_SECONDS // 60
    assert max_fields_hourly == 25
    assert max_fields_hourly < max_fields_per_minute_equivalent
    assert max_fields_hourly <= 30  # teto generoso — pega qualquer revert acidental


# ── flush_inflight: grava matches E overflow ────────────────────────────────


@pytest.mark.asyncio
async def test_flush_inflight_writes_matches_and_overflow(monkeypatch: pytest.MonkeyPatch) -> None:
    r = _fake_redis()
    monkeypatch.setattr(obs, "_redis", lambda: r)

    acc = InflightAccumulator()
    acc.matches[101] = 1240
    acc.matches[102] = 3
    acc.overflow[101] = 900  # cardinalidade do group_by estourando o teto

    await flush_inflight(acc, organization_id=7)

    assert _read_rule_window("101", "matches") == 1240.0
    assert _read_rule_window("101", "overflow") == 900.0
    assert _read_rule_window("102", "matches") == 3.0
    # regra 102 nunca estourou o teto — nada gravado, leitura é 0.0 (sem dado).
    assert _read_rule_window("102", "overflow") == 0.0


@pytest.mark.asyncio
async def test_flush_inflight_accumulates_across_cycles(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dois ciclos seguidos somam no MESMO bucket horário (dentro da mesma
    hora) — o contador de 24h é sobre o acumulado, não um snapshot do
    último ciclo."""
    r = _fake_redis()
    monkeypatch.setattr(obs, "_redis", lambda: r)

    acc1 = InflightAccumulator()
    acc1.matches[5] = 10
    await flush_inflight(acc1, organization_id=1)

    acc2 = InflightAccumulator()
    acc2.matches[5] = 7
    await flush_inflight(acc2, organization_id=1)

    assert _read_rule_window("5", "matches") == 17.0


@pytest.mark.asyncio
async def test_flush_inflight_writes_nothing_when_no_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    r = _fake_redis()
    monkeypatch.setattr(obs, "_redis", lambda: r)

    acc = InflightAccumulator()  # nenhum match, nenhum overflow
    await flush_inflight(acc, organization_id=1)

    assert r.keys("obs:rule:*") == []


# ── best-effort: observability_store falhando não pode derrubar o flush ───


@pytest.mark.asyncio
async def test_flush_inflight_never_raises_when_observability_store_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simula o observability_store inteiro quebrado (ex.: Redis fora do ar).
    ``flush_inflight`` roda no ``finally`` do ciclo de coleta — se levantasse
    aqui, mascararia qualquer exceção original que estivesse se propagando
    por esse ``finally`` (semântica de ``finally`` do Python: uma exceção nova
    substitui a antiga)."""
    def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("observability_store totalmente indisponível")

    monkeypatch.setattr(obs, "record_counter", _boom)

    acc = InflightAccumulator()
    acc.matches[1] = 5
    acc.matches[2] = 1
    acc.overflow[1] = 2

    # não deve levantar
    await flush_inflight(acc, organization_id=1)


@pytest.mark.asyncio
async def test_flush_inflight_still_persists_detections_when_observability_store_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gravação de métrica é um EXTRA best-effort — não pode competir com a
    persistência das Detections (``acc.pending``), que segue seu próprio
    caminho (``_flush_sync``) independentemente do observability_store."""
    from backend.app.collectors.inflight import runtime as runtime_mod
    from backend.app.collectors.inflight.matcher import CompiledInflightRule

    written: list[str] = []

    def _fake_flush_sync(pending: dict, organization_id: int) -> int:
        written.extend(pending.keys())
        return len(pending)

    def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("observability_store indisponível")

    monkeypatch.setattr(runtime_mod, "_flush_sync", _fake_flush_sync)
    monkeypatch.setattr(obs, "record_counter", _boom)

    rule = CompiledInflightRule(
        rule_id=1, name="r1", severity_id=4,
        suppression_window_seconds=3600, group_by_path=None, clauses=(),
    )
    acc = InflightAccumulator()
    acc.pending["inflight:1:1:*"] = {"rule": rule, "integration_id": None}
    acc.matches[1] = 1

    await flush_inflight(acc, organization_id=1)

    assert written == ["inflight:1:1:*"]


# ── Erro ATRIBUÍVEL por regra (B + C2) ──────────────────────────────────────
#
# ``rule_id`` NÃO é label de ``collector_inflight_errors_total`` — é id global,
# multiplicaria a cardinalidade por ``reason`` e o lado OTLP não tem TTL para
# envelhecer a série de uma regra apagada. O breakdown por regra desce para o
# observability_store (Redis, TTL 25h), que é de onde a UI lê. O OTel continua
# recebendo só ``reason``, com a MESMA soma de antes.


def _rule(rule_id: int, group_by: tuple[str, ...] | None = ("u",)) -> "CompiledInflightRule":
    from backend.app.collectors.inflight.matcher import CompiledInflightRule

    return CompiledInflightRule(
        rule_id=rule_id, name=f"r{rule_id}", severity_id=4,
        suppression_window_seconds=3600, group_by_path=group_by, clauses=(),
    )


def _long_value(sufixo: str) -> str:
    from backend.app.core.config import settings

    return "A" * (int(settings.INFLIGHT_MAX_GROUP_VALUE_LEN) + 1) + sufixo


@pytest.mark.asyncio
async def test_all_four_error_reasons_are_attributed_to_a_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Os quatro caminhos ATRIBUÍVEIS de ``ERROR_REASONS``, exercitados pela
    API pública, e
    a forma aninhada (razão → rule_id → contagem) em cada um. É o teste que
    torna o enum verificável: um reason novo que não passe por ``count_error``
    fica de fora do breakdown e a UI não o mostra.

    POR QUE O NÚMERO DE ``flush_lost`` MUDOU: este teste afirmava
    ``sum(flush_lost) == len(acc.pending)`` — e com isso consagrava uma
    SOBRECONTAGEM. ``DetectionRepository.record`` commita POR CHAVE, então uma
    falha no meio da escrita deixa as Detections anteriores DURÁVEIS no banco;
    contá-las como perda inflava a única série que mede o dano ao cliente e
    mandava investigar prejuízo que não houve. ``_flush_sync`` agora devolve as
    chaves já commitadas em ``InflightFlushInterrupted.written_keys``, e a
    contagem passa a ser ``pendentes − gravadas``. O total por razão que o OTel
    recebe continua sendo a soma do dict interno (cardinalidade intacta) — o que
    mudou foi o VALOR, que deixou de mentir para mais.
    """
    from backend.app.collectors.inflight import runtime as runtime_mod
    from backend.app.collectors.inflight.runtime import (
        ERROR_REASONS,
        UNATTRIBUTED_ERROR_REASONS,
    )
    from backend.app.core.config import settings

    key_cap = int(settings.INFLIGHT_MAX_DEDUP_KEYS_PER_RULE_PER_CYCLE)
    acc = InflightAccumulator()

    # 1) group_by_unresolved — regra 10 aponta para campo que o evento não tem.
    acc.add(_rule(10, group_by=("ausente",)), {"u": "x"}, organization_id=1)
    acc.add(_rule(10, group_by=("ausente",)), {"u": "y"}, organization_id=1)

    # 2) key_cap — regra 11 estoura o teto de chaves distintas do ciclo.
    for i in range(key_cap + 3):
        acc.add(_rule(11), {"u": f"user{i}"}, organization_id=1)

    # 3) group_value_truncated — regra 12 com valor acima do teto de chars.
    acc.add(_rule(12), {"u": _long_value("alpha")}, organization_id=1)
    acc.add(_rule(12), {"u": _long_value("beta")}, organization_id=1)

    assert acc.errors["group_by_unresolved"] == {10: 2}
    assert acc.errors["key_cap"] == {11: 3}
    assert acc.errors["group_value_truncated"] == {12: 2}

    # 4) flush_lost — a escrita das Detections falha DEPOIS de commitar parte
    #    delas. A perda é atribuída por regra a partir de ``item["rule"]``, sem
    #    volta ao banco, e SÓ sobre o que não foi gravado.
    #
    #    As duas primeiras chaves pendentes são da regra 11 (foi ela que
    #    populou ``pending`` primeiro), então a regra 11 ainda perde e a 12
    #    perde tudo — as duas continuam no breakdown. Uma falha que "gravasse"
    #    uma regra inteira transformaria este teste num teste de 3 razões.
    ja_gravadas = tuple(list(acc.pending)[:2])
    assert len(ja_gravadas) == 2

    # 5) emit_failed — as Detections COMMITADAS antes da falha são duráveis, e
    #    portanto têm de sair também como evento OCSF 2004. A exceção carrega os
    #    tickets delas exatamente por isso; aqui o DESPACHO é que falha, e a
    #    perda da entrega é atribuída à regra dona do ticket. Sem este ramo, o
    #    reason ficaria declarado no enum e sem nenhum call site exercitado — a
    #    forma de vacuidade que este arquivo inteiro existe para não ter.
    tickets = tuple(
        runtime_mod.DetectionEmit(
            dedup_key=chave,
            detection_id=100 + i,
            rule_id=acc.pending[chave]["rule"].rule_id,
            rule_name=acc.pending[chave]["rule"].name,
            severity_id=4,
            integration_id=None,
            source=acc.pending[chave].get("source") or {},
        )
        for i, chave in enumerate(ja_gravadas)
    )

    def _boom_flush(*_a: object, **_k: object) -> int:
        raise runtime_mod.InflightFlushInterrupted(ja_gravadas, tickets)

    def _boom_dispatch(_envelopes: object) -> None:
        raise RuntimeError("broker de dispatch fora do ar")

    monkeypatch.setattr(runtime_mod, "_flush_sync", _boom_flush)
    monkeypatch.setattr(runtime_mod, "_dispatch_sync", _boom_dispatch)
    monkeypatch.setattr(settings, "INFLIGHT_EMIT_OCSF_EVENT", True)
    monkeypatch.setattr(obs, "record_counter", lambda *a, **k: None)

    perdidas_por_regra: dict[int, int] = {}
    for dedup_key, item in acc.pending.items():
        if dedup_key in ja_gravadas:
            continue
        rid = item["rule"].rule_id
        perdidas_por_regra[rid] = perdidas_por_regra.get(rid, 0) + 1
    assert set(perdidas_por_regra) == {11, 12}, "as duas regras têm de perder algo"

    await flush_inflight(acc, organization_id=1)

    assert acc.errors["flush_lost"] == perdidas_por_regra
    assert sum(acc.errors["flush_lost"].values()) == len(acc.pending) - len(ja_gravadas)
    # O número VELHO, escrito por extenso para que um revert do contrato apareça
    # como falha e não como silêncio: contar ``pending`` inteiro reportaria
    # perda de Detections que estão no banco.
    assert sum(acc.errors["flush_lost"].values()) != len(acc.pending)
    # A entrega ao SIEM falhou para os DOIS tickets das Detections que já
    # estavam commitadas, e a falha é ATRIBUÍDA — "perdi 2 eventos" não diz qual
    # regra parou de chegar no destino.
    esperado_emit: dict[int, int] = {}
    for chave in ja_gravadas:
        rid = int(acc.pending[chave]["rule"].rule_id)
        esperado_emit[rid] = esperado_emit.get(rid, 0) + 1
    assert acc.errors["emit_failed"] == esperado_emit
    # CONTAGEM, não presença: as duas chaves são da MESMA regra, então um
    # ``count_error`` chamado uma vez só por regra passaria num assert de
    # chaves e esconderia metade da perda de entrega.
    assert sum(esperado_emit.values()) == len(ja_gravadas) == 2

    # 6) flush_cap — o teto GLOBAL de Detections por flush. Exercitado num
    #    acumulador À PARTE, e isso não é conveniência: o teto corta
    #    ``acc.pending`` ANTES da escrita, então fazê-lo morder no acumulador
    #    acima mudaria toda a aritmética de ``flush_lost`` verificada logo
    #    antes — o teste passaria a medir o corte no lugar da perda, que são
    #    duas perdas DIFERENTES e não podem se sobrepor.
    acc_teto = InflightAccumulator()
    for i in range(6):
        acc_teto.add(_rule(13), {"u": f"entidade{i}"}, organization_id=1)
    monkeypatch.setattr(settings, "INFLIGHT_MAX_DETECTIONS_PER_FLUSH", 2)
    monkeypatch.setattr(runtime_mod, "_flush_sync", lambda _p, _o: ())

    await flush_inflight(acc_teto, organization_id=1)

    # CONTAGEM por regra, não presença: 6 chaves pendentes, 2 cabem, 4 se
    # perdem — e a perda é atribuída à regra que a produziu, que é a única
    # forma de o operador achar a de alta cardinalidade.
    assert acc_teto.errors["flush_cap"] == {13: 4}

    # ANTI-VACUIDADE: os SEIS ATRIBUÍVEIS, e nenhum reason fora do enum.
    # ``matcher`` fica de fora de propósito: ele é escrito pelo ``except``
    # de ``pipeline.py``, que não sabe qual regra estava sendo avaliada quando a
    # exceção subiu, e por isso é o único reason sem breakdown por regra. A
    # distinção é declarada em ``UNATTRIBUTED_ERROR_REASONS`` — se alguém
    # acrescentar um reason externo sem declará-lo lá, este assert reprova.
    atribuiveis = set(ERROR_REASONS) - set(UNATTRIBUTED_ERROR_REASONS)
    assert set(acc.errors) | set(acc_teto.errors) == atribuiveis
    assert len(atribuiveis) == 6


@pytest.mark.asyncio
async def test_flush_inflight_writes_err_breakdown_to_the_observability_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """É deste Redis que a UI lê. Sem estas chaves, "1200 group_by_unresolved"
    é um número que não aponta para regra nenhuma e o operador não tem o que
    corrigir."""
    r = _fake_redis()
    monkeypatch.setattr(obs, "_redis", lambda: r)

    acc = InflightAccumulator()
    acc.count_error("group_by_unresolved", 101, 12)
    acc.count_error("key_cap", 101, 3)
    acc.count_error("group_value_truncated", 202, 7)
    acc.count_error("flush_lost", 202, 5)

    await flush_inflight(acc, organization_id=7)

    assert _read_rule_window("101", "err_group_by_unresolved") == 12.0
    assert _read_rule_window("101", "err_key_cap") == 3.0
    assert _read_rule_window("202", "err_group_value_truncated") == 7.0
    assert _read_rule_window("202", "err_flush_lost") == 5.0
    # Espelho negativo COM par positivo acima: o breakdown é POR REGRA, então a
    # regra que não errou daquele jeito não pode ganhar chave.
    assert _read_rule_window("101", "err_flush_lost") == 0.0
    assert _read_rule_window("202", "err_key_cap") == 0.0
    assert sorted(r.keys("obs:rule:*")) == [
        "obs:rule:101:err_group_by_unresolved",
        "obs:rule:101:err_key_cap",
        "obs:rule:202:err_flush_lost",
        "obs:rule:202:err_group_value_truncated",
    ]


@pytest.mark.asyncio
async def test_otel_error_series_keeps_reason_only_and_the_same_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A metade que NÃO pode mudar: ``sum by (reason)`` idêntico ao de antes e
    zero label novo. Espia ``otel_metrics.count`` direto — com
    ``OTEL_ENABLED=False`` (default) a fachada de ``metrics.py`` chega até lá,
    mas o emit vira no-op dentro dela."""
    from backend.app.collectors import otel_metrics

    emitidos: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        otel_metrics, "count",
        lambda name, value=1, attrs=None: emitidos.append((name, dict(attrs or {}), value)),
    )
    monkeypatch.setattr(obs, "record_counter", lambda *a, **k: None)

    acc = InflightAccumulator()
    acc.count_error("key_cap", 1, 4)
    acc.count_error("key_cap", 2, 6)  # MESMA razão, outra regra

    await flush_inflight(acc, organization_id=1)

    erros = [e for e in emitidos if e[0] == "collector_inflight_errors_total"]
    assert len(erros) == 1, "duas regras, UMA série — a cardinalidade não mudou"
    assert erros[0][1] == {"reason": "key_cap"}, "rule_id NUNCA vira label aqui"
    assert erros[0][2] == 10, "o total por razão soma as regras"


def test_error_reasons_is_disjoint_from_reject_reasons() -> None:
    """São duas séries e dois momentos: ``rules_rejected`` é falha de
    COMPILAÇÃO (1x por ciclo, na carga); ``errors`` é falha de AVALIAÇÃO/flush.
    Um nome em comum tornaria os dois painéis indistinguíveis."""
    from backend.app.collectors.inflight.runtime import ERROR_REASONS, REJECT_REASONS

    assert set(ERROR_REASONS) & set(REJECT_REASONS) == set()
    assert len(set(ERROR_REASONS)) == len(ERROR_REASONS)  # sem duplicata


def test_warning_is_emitted_once_per_reason_and_rule_per_cycle(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Rate-limit de log por (razão, regra). CONTAGEM, não ``not any(...)``: um
    assert negativo aqui aprovaria por vacuidade se o log parasse de sair."""
    import logging as _logging

    from backend.app.core.config import settings

    key_cap = int(settings.INFLIGHT_MAX_DEDUP_KEYS_PER_RULE_PER_CYCLE)
    acc = InflightAccumulator()

    with caplog.at_level(_logging.WARNING):
        for i in range(key_cap + 40):
            # As duas regras truncam o valor E estouram o teto de chaves no
            # MESMO ciclo — é por isso que ``_logged_once`` é chaveado pela
            # DUPLA: chavear só por rule_id calaria o segundo aviso.
            acc.add(_rule(21), {"u": _long_value(f"a{i}")}, organization_id=1)
            acc.add(_rule(22), {"u": _long_value(f"b{i}")}, organization_id=1)

    truncamento = [rec for rec in caplog.records if "digest" in rec.msg]
    teto = [rec for rec in caplog.records if "teto de %d chaves" in rec.msg]

    assert len(truncamento) == 2, "1 aviso de truncamento por regra, não por evento"
    assert len(teto) == 2, "1 aviso de teto por regra, não por evento"
    assert {rec.args[0] for rec in truncamento} == {21, 22}
    assert {rec.args[0] for rec in teto} == {21, 22}

    # O log é rate-limited; o CONTADOR não — é ele que a UI soma.
    assert acc.errors["group_value_truncated"] == {21: key_cap + 40, 22: key_cap + 40}
    assert acc.errors["key_cap"] == {21: 40, 22: 40}

    # O valor vindo do evento JAMAIS entra no log: só o comprimento.
    for rec in truncamento:
        assert "AAAA" not in rec.getMessage()
        assert int(settings.INFLIGHT_MAX_GROUP_VALUE_LEN) + 3 in rec.args


@pytest.mark.asyncio
async def test_err_breakdown_failure_does_not_change_what_flush_inflight_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_record_rule_metric`` explodindo (Redis fora) é um EXTRA best-effort:
    não pode derrubar o flush, nem tirar do OTel o que ele receberia."""
    from backend.app.collectors import otel_metrics

    emitidos: list[tuple[str, dict, float]] = []
    monkeypatch.setattr(
        otel_metrics, "count",
        lambda name, value=1, attrs=None: emitidos.append((name, dict(attrs or {}), value)),
    )

    def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("observability_store indisponível")

    monkeypatch.setattr(obs, "record_counter", _boom)

    acc = InflightAccumulator()
    acc.count_error("group_by_unresolved", 9, 3)
    acc.matches[9] = 3

    await flush_inflight(acc, organization_id=1)  # não pode levantar

    erros = [e for e in emitidos if e[0] == "collector_inflight_errors_total"]
    matches = [e for e in emitidos if e[0] == "collector_inflight_matches_total"]
    assert erros == [("collector_inflight_errors_total", {"reason": "group_by_unresolved"}, 3)]
    assert matches == [("collector_inflight_matches_total", {"rule_id": "9"}, 3)]


def test_record_rule_metric_swallows_a_broken_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard direto da função: ela roda dentro do ``finally`` do ciclo de
    coleta, e uma exceção nova ali SUBSTITUI a exceção original que estivesse
    se propagando (semântica de ``finally`` do Python).

    Engolir a exceção continua sendo o contrato; o que mudou é que a função
    passou a DEVOLVER o veredito, em vez de descartá-lo junto. Era o descarte
    do veredito que deixava a perda invisível: quem chama precisa saber que a
    chave não foi criada, porque do lado da leitura ausência de chave é
    indistinguível de "a regra não disparou"."""
    from backend.app.collectors.inflight import runtime as runtime_mod

    chamadas: list[tuple] = []

    def _boom(*a: object, **k: object) -> None:
        chamadas.append((a, k))
        raise RuntimeError("Redis fora")

    monkeypatch.setattr(obs, "record_counter", _boom)

    assert runtime_mod._record_rule_metric("err_key_cap", 1, 2) is False
    # Par positivo: a função REALMENTE tentou gravar — sem isto, um ``return``
    # antecipado passaria por este teste sem tocar no store.
    assert len(chamadas) == 1


def test_record_rule_metric_reports_a_confirmed_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PAR POSITIVO do guard acima: sem ele, um ``return False`` incondicional
    passaria — e o contador de perda acusaria prejuízo em todo ciclo saudável,
    trocando cegueira por ruído permanente.

    Store REAL (fakeredis) e não um duplo que devolve ``True``: o veredito tem
    de vir da ida ao Redis, que é o que a produção faz."""
    from backend.app.collectors.inflight import runtime as runtime_mod

    r = _fake_redis()
    monkeypatch.setattr(obs, "_redis", lambda: r)

    assert runtime_mod._record_rule_metric("err_key_cap", 1, 2) is True
    assert _read_rule_window("1", "err_key_cap") == 2.0


def test_record_rule_metric_treats_a_verdictless_double_as_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``record_counter`` só passou a devolver veredito agora, e é ponto de
    monkeypatch em dezenas de testes deste repo e do EE, cujos duplos devolvem
    ``None``. Ler ``None`` como falha inverteria o sinal da série de perda —
    contaria prejuízo que não houve, que é o mesmo erro de que ``flush_lost``
    acabou de ser curado, com o sinal trocado. Sem veredito ⇒ nada a reportar."""
    from backend.app.collectors.inflight import runtime as runtime_mod

    monkeypatch.setattr(obs, "record_counter", lambda *a, **k: None)

    assert runtime_mod._record_rule_metric("err_key_cap", 1, 2) is True


@pytest.mark.asyncio
async def test_legacy_flat_error_shape_still_emits_and_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``pipeline.py`` ainda escreve ``acc.errors["matcher"] = int`` (call site
    fora deste módulo). Um ``sum`` sobre int levantaria DENTRO do ``finally``
    do ciclo e mascararia a exceção original — por isso o flush aceita as duas
    formas. Este teste é o que trava esse contrato até o call site migrar para
    ``count_error``."""
    from backend.app.collectors import otel_metrics

    emitidos: list[tuple[str, dict, float]] = []
    monkeypatch.setattr(
        otel_metrics, "count",
        lambda name, value=1, attrs=None: emitidos.append((name, dict(attrs or {}), value)),
    )
    monkeypatch.setattr(obs, "record_counter", lambda *a, **k: None)

    acc = InflightAccumulator()
    acc.errors["matcher"] = 7  # forma legada, plana
    acc.count_error("key_cap", 3, 2)  # forma canônica, aninhada

    await flush_inflight(acc, organization_id=1)

    erros = {e[1]["reason"]: e[2] for e in emitidos if e[0] == "collector_inflight_errors_total"}
    assert erros == {"matcher": 7, "key_cap": 2}
