"""Detector MORTO tem de ser distinguível de org SEM REGRA.

``load_inflight_rules_for_org`` tem um ``except`` amplo — obrigatório, porque um
problema de banco não pode derrubar a COLETA, que é o produto que se vende. Ele
devolve ruleset VAZIO, e com isso a avaliação do ciclo inteiro é desligada.

O buraco: nesse caminho o gauge ``collector_inflight_rules_loaded`` não era
emitido. O OTLP não expira série e ninguém reescreve o ponto, então o painel
seguia mostrando o ÚLTIMO valor conhecido — "12 regras carregadas" — com o
detector parado. É a forma EXATA do incidente de jul/2026, em que
``lag_seconds`` reportava ``healthy`` sobre um coletor 15h atrasado: a
degradação não mentia, ela simplesmente não falava.

E o zero sozinho não resolve, porque ele é indistinguível do caso saudável mais
comum (a org não tem regra em modo inflight). O que separa os dois é o PAR:

    gauge rules_loaded = 0   +   rules_rejected{reason="load_failed"} subindo

Este arquivo verifica os DOIS lados do par, e o par NEGATIVO junto (org sem
regra e kill-switch: gauge 0, contador parado). Sem o lado negativo, um call
site que contasse ``load_failed`` sempre passaria aqui e destruiria a única
distinção que a série existe para fazer.

Sem marker ``source_only``: nada é lido do fonte, tudo é o que a carga EMITE.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import types

import pytest

from backend.app.collectors import otel_metrics
from backend.app.collectors.inflight.runtime import (
    REJECT_REASONS,
    load_inflight_rules_for_org,
)
from backend.app.core.config import settings

GAUGE = "collector_inflight_rules_loaded"
CONTADOR = "collector_inflight_rules_rejected_total"


def _row(rid: int) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=rid, name=f"regra-{rid}", severity_id=4,
        where_json='[{"field":"a","op":"eq","value":"x"}]',
        suppression_window_seconds=3600, group_by_field="u",
    )


class _FakeSession:
    def __enter__(self) -> "_FakeSession":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


def _install_db(monkeypatch: pytest.MonkeyPatch, rows: list, *, boom: bool = False):
    """Sessão e repositório dublados. ``load_inflight_rules_for_org`` importa os
    dois DENTRO da função, então o patch tem de ser no módulo de origem."""
    from backend.app.db import database, repository

    class _FakeRepo:
        def __init__(self, _db: object) -> None:
            pass

        def list_inflight_for_org(self, _org: int, limit: int) -> list:
            return rows[:limit]

        def count_inflight_for_org(self, _org: int) -> int:
            return len(rows)

        def list_inflight_cut_for_org(
            self, _org: int, limit: int, max_rows: int
        ) -> list:
            return rows[limit:][:max_rows]

        def count_enabled_for_org(self, _org: int) -> int:
            return len(rows)

    def _session():
        if boom:
            # O modo de falha REAL: o pool não entrega conexão. Levantar na
            # abertura da sessão (e não dentro do repo) é o caminho que o
            # ``except`` amplo cobre e o que acontece quando o Postgres cai.
            raise RuntimeError("connection pool exhausted / Postgres fora")
        return _FakeSession()

    monkeypatch.setattr(database, "SessionLocal", _session)
    monkeypatch.setattr(repository, "CorrelationRuleRepository", _FakeRepo)


@pytest.fixture
def emitidos(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict, float]]:
    """``(nome, attrs, valor)`` de tudo que a carga empurra para o OTel.

    ARMADILHA: ``OTEL_ENABLED`` é ``False`` por default e ``count``/``set_gauge``
    viram no-op ANTES de tocar o instrumento. A fachada de ``metrics.py`` chega
    até elas, mas nada sai do outro lado — então a espionagem é sobre estas
    funções, nunca sobre o instrumento."""
    capturado: list[tuple[str, dict, float]] = []

    def _count(name: str, value: float = 1, attrs: dict | None = None) -> None:
        capturado.append((name, dict(attrs or {}), float(value)))

    def _set_gauge(name: str, value: float, attrs: dict | None = None) -> None:
        capturado.append((name, dict(attrs or {}), float(value)))

    monkeypatch.setattr(otel_metrics, "count", _count)
    monkeypatch.setattr(otel_metrics, "set_gauge", _set_gauge)
    return capturado


def _gauges(emitidos) -> list[float]:
    return [v for n, _a, v in emitidos if n == GAUGE]


def _load_failed(emitidos) -> list[float]:
    return [
        v for n, a, v in emitidos
        if n == CONTADOR and a.get("reason") == "load_failed"
    ]


# ── 1. O par POSITIVO: banco fora ──────────────────────────────────────────


def test_a_dead_database_zeroes_the_gauge_and_counts_the_failure(
    monkeypatch: pytest.MonkeyPatch, emitidos: list[tuple[str, dict, float]]
) -> None:
    """Sem estas duas emissões o detector fica morto reportando o último valor
    conhecido — e nenhuma outra série do subsistema sobe, porque ninguém
    avalia, ninguém casa e ninguém faz flush."""
    _install_db(monkeypatch, [_row(1), _row(2)], boom=True)

    ruleset = load_inflight_rules_for_org(organization_id=7)

    assert ruleset.rules == (), "fail-safe: a coleta não pode cair com o banco fora"
    assert _gauges(emitidos) == [0.0], (
        "o gauge não foi reescrito: o painel segue mostrando o último valor "
        "conhecido com o detector parado"
    )
    assert _load_failed(emitidos) == [1.0], (
        "a falha de carga não foi contada — gauge 0 sozinho é indistinguível "
        "de 'esta org não tem regra em voo'"
    )


def test_the_failure_reason_belongs_to_the_closed_enum_and_carries_no_id(
    monkeypatch: pytest.MonkeyPatch, emitidos: list[tuple[str, dict, float]]
) -> None:
    """``reason`` é label. O id da org é global e não pode virar label de uma
    série de contagem; ele vai no LOG e no label do gauge, que é declarado
    ``org_id`` no catálogo e existe justamente para ser por tenant."""
    _install_db(monkeypatch, [_row(1)], boom=True)
    load_inflight_rules_for_org(organization_id=7)

    (attrs_contador,) = [a for n, a, _v in emitidos if n == CONTADOR]
    assert set(attrs_contador) == {"reason"}
    assert attrs_contador["reason"] in REJECT_REASONS
    assert "7" not in attrs_contador.values(), "id de org vazou para o label"

    (attrs_gauge,) = [a for n, a, _v in emitidos if n == GAUGE]
    assert set(attrs_gauge) == set(otel_metrics.labels_for(GAUGE)) == {"org_id"}


# ── 2. O par NEGATIVO: sem ele a distinção não existe ──────────────────────


def test_an_org_without_inflight_rules_zeroes_the_gauge_without_counting_a_failure(
    monkeypatch: pytest.MonkeyPatch, emitidos: list[tuple[str, dict, float]]
) -> None:
    """O caso SAUDÁVEL e mais comum. Ele emite o MESMO zero — e é por isso que
    o contador tem de ficar parado aqui: se ``load_failed`` subisse nos dois
    caminhos, o par deixaria de distinguir qualquer coisa e este arquivo estaria
    verde provando nada."""
    _install_db(monkeypatch, [])

    ruleset = load_inflight_rules_for_org(organization_id=7)

    assert ruleset.rules == ()
    assert _gauges(emitidos) == [0.0]
    assert _load_failed(emitidos) == [], (
        "org sem regra foi contada como falha de carga — 'detector morto' e "
        "'nada configurado' viraram o mesmo alerta"
    )


def test_a_healthy_load_reports_the_real_count_and_no_failure(
    monkeypatch: pytest.MonkeyPatch, emitidos: list[tuple[str, dict, float]]
) -> None:
    """O outro lado positivo: com regra, o gauge é o número REAL. Sem este
    teste, um call site que emitisse 0 sempre passaria nos dois de cima."""
    _install_db(monkeypatch, [_row(1), _row(2), _row(3)])

    ruleset = load_inflight_rules_for_org(organization_id=7)

    assert len(ruleset.rules) == 3
    assert _gauges(emitidos) == [3.0]
    assert _load_failed(emitidos) == []


def test_the_kill_switch_also_zeroes_the_gauge_and_is_not_a_failure(
    monkeypatch: pytest.MonkeyPatch, emitidos: list[tuple[str, dict, float]]
) -> None:
    """``INFLIGHT_MAX_RULES_PER_CYCLE=0`` desliga a avaliação. Detector parado
    também, e o gauge congelado mentiria igual — mas é ato DELIBERADO do
    operador, então não é degradação e não conta falha."""
    _install_db(monkeypatch, [_row(1), _row(2)])
    monkeypatch.setattr(settings, "INFLIGHT_MAX_RULES_PER_CYCLE", 0)

    ruleset = load_inflight_rules_for_org(organization_id=7)

    assert ruleset.rules == ()
    assert _gauges(emitidos) == [0.0], (
        "kill-switch ligado com o gauge congelado no último valor: o painel "
        "diz que há regras carregadas e nada é avaliado"
    )
    assert _load_failed(emitidos) == []


# ── 3. A métrica do fracasso não pode virar o fracasso ─────────────────────


def test_a_broken_metric_backend_does_not_break_the_collection_fail_safe(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """O ``except`` da carga é a rede de segurança da COLETA. Emitir métrica
    dentro dela abriu um caminho novo de exceção — e se essa emissão levantasse,
    a rede deixaria de existir justamente no ciclo em que o banco já caiu."""
    _install_db(monkeypatch, [_row(1)], boom=True)

    chamadas = {"n": 0}

    def _boom_gauge(*_a: object, **_k: object) -> None:
        chamadas["n"] += 1
        raise RuntimeError("exporter OTLP quebrado")

    monkeypatch.setattr(otel_metrics, "set_gauge", _boom_gauge)

    ruleset = load_inflight_rules_for_org(organization_id=7)

    # CONTAGEM, e não "não levantou": sem provar que o dublê foi chamado, este
    # teste ficaria verde num futuro em que a emissão saísse do caminho.
    assert chamadas["n"] == 1, "o dublê nem foi chamado — nada foi exercitado"
    assert ruleset.rules == (), "a carga deixou de ser fail-safe para a coleta"
