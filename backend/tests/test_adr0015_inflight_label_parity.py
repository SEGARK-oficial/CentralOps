"""Paridade de labels e enums FECHADOS das séries ``collector_inflight_*``.

Este arquivo existe porque o teste que ``iter_reject_reasons`` prometia nunca
foi escrito: a função tinha ZERO chamadores e o docstring dizia "para o teste
que trava o enum fechado". Os asserts que existiam eram de INCLUSÃO
(``⊆ REJECT_REASONS``) — widening-safe: acrescentar um reason novo hoje não
quebrava nada, e a label do painel de ops divergia do enum em silêncio. Aqui a
verificação é por COMPORTAMENTO (o que a emissão realmente produz) e por
IGUALDADE.

Sem marker ``source_only`` de propósito: o ponto é justamente rodar na imagem
Cython, onde ``runtime.py`` é ``.so`` e nenhuma leitura de fonte é possível —
é lá que a divergência entre catálogo e call site custaria caro.

ARMADILHA que este arquivo tem de driblar: ``OTEL_ENABLED`` é ``False`` por
default e ``otel_metrics.count``/``set_gauge`` viram no-op ANTES de tocar o
instrumento. A fachada de ``metrics.py`` chega até elas, mas nada sai do outro
lado — então a espionagem é sobre ``otel_metrics.count``/``set_gauge`` direto,
nunca sobre o instrumento.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import types

import pytest

from backend.app.collectors import observability_store as obs
from backend.app.collectors import otel_metrics
from backend.app.collectors.inflight import runtime as runtime_mod
from backend.app.collectors.inflight.runtime import (
    ERROR_REASONS,
    UNATTRIBUTED_ERROR_REASONS,
    REJECT_REASONS,
    InflightAccumulator,
    flush_inflight,
    load_inflight_rules_for_org,
)
from backend.app.core.config import settings

#: Toda série do catálogo cujo nome começa assim tem de ser exercitada abaixo.
#: É o guard anti-vacuidade estrutural: uma série nova de inflight que ninguém
#: exercite reprova aqui em vez de passar despercebida.
_INFLIGHT_SERIES = tuple(
    sorted(n for n in otel_metrics._SPEC if n.startswith("collector_inflight_"))
)


def _row(rid: int, where_json: str, group_by: str | None = None) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=rid, name=f"regra-{rid}", where_json=where_json, severity_id=4,
        suppression_window_seconds=3600, group_by_field=group_by,
    )


def _rows_covering_every_reject_reason() -> list[types.SimpleNamespace]:
    """Uma linha por razão de rejeição + preenchimento válido até o teto de
    regras por ciclo (o que dispara ``truncated``)."""
    cap_clauses = int(settings.INFLIGHT_MAX_WHERE_CLAUSES)
    ok = '[{"field":"a","op":"eq","value":"x"}]'
    rows = [
        _row(1, "{nao é json}"),                                    # bad_json
        _row(2, "[]"),                                              # empty_where
        _row(3, '[{"field":"a","op":"regex","value":"x"}]'),        # unknown_op
        _row(4, "[" + ",".join([ok[1:-1]] * (cap_clauses + 1)) + "]"),  # over_cap
    ]
    # As válidas: precisam existir para o gauge ``rules_loaded`` sair > 0 e para
    # ``len(rows) >= cap`` disparar ``truncated``.
    cap_rules = int(settings.INFLIGHT_MAX_RULES_PER_CYCLE)
    rows += [_row(100 + i, ok, group_by="u") for i in range(cap_rules - len(rows))]
    return rows


class _FakeSession:
    def __enter__(self) -> "_FakeSession":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


def _install_fake_db(monkeypatch: pytest.MonkeyPatch, rows: list) -> None:
    """Substitui a sessão e o repositório reais. ``load_inflight_rules_for_org``
    importa ambos DENTRO da função, então o patch tem de ser no módulo de
    origem — não há símbolo em ``runtime`` para trocar."""
    from backend.app.db import database, repository

    class _FakeRepo:
        def __init__(self, _db: object) -> None:
            pass

        def list_inflight_for_org(self, _org: int, limit: int) -> list:
            return rows[:limit]

        def count_inflight_for_org(self, _org: int) -> int:
            # > cap ⇒ dispara o aviso de truncamento de REGRAS.
            return len(rows) + 7

        def count_enabled_for_org(self, _org: int) -> int:
            return len(rows)

    monkeypatch.setattr(database, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(repository, "CorrelationRuleRepository", _FakeRepo)


@pytest.fixture
def emitidos(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    """``(nome, attrs)`` de tudo que os call sites empurram para o OTel.

    Espiona ``otel_metrics.count``/``set_gauge`` — e não o instrumento — porque
    com ``OTEL_ENABLED=False`` (o default) elas retornam antes de tocar nele.
    """
    capturado: list[tuple[str, dict]] = []

    def _count(name: str, value: float = 1, attrs: dict | None = None) -> None:
        capturado.append((name, dict(attrs or {})))

    def _set_gauge(name: str, value: float, attrs: dict | None = None) -> None:
        capturado.append((name, dict(attrs or {})))

    monkeypatch.setattr(otel_metrics, "count", _count)
    monkeypatch.setattr(otel_metrics, "set_gauge", _set_gauge)
    return capturado


async def _exercise(monkeypatch: pytest.MonkeyPatch, record_counter=None) -> None:
    """Roda os DOIS pontos de emissão do subsistema: a carga (1x por ciclo) e o
    flush (1x por ciclo). Nenhuma métrica de inflight sai de outro lugar.

    ``record_counter`` é parâmetro e não um patch fixo aqui dentro: quem quiser
    ESPIAR o observability_store precisa que o espião sobreviva a esta função —
    patchar por último venceria o do teste, silenciosamente.
    """
    _install_fake_db(monkeypatch, _rows_covering_every_reject_reason())
    monkeypatch.setattr(
        obs, "record_counter", record_counter or (lambda *a, **k: None)
    )

    ruleset = load_inflight_rules_for_org(organization_id=7)
    assert ruleset.rules, "a carga precisa devolver regras, ou o gauge sai 0"

    from backend.app.collectors.inflight.matcher import CompiledInflightRule

    def _rule(rid: int, group_by: tuple[str, ...] | None = ("u",)) -> CompiledInflightRule:
        return CompiledInflightRule(
            rule_id=rid, name=f"r{rid}", severity_id=4,
            suppression_window_seconds=3600, group_by_path=group_by, clauses=(),
        )

    key_cap = int(settings.INFLIGHT_MAX_DEDUP_KEYS_PER_RULE_PER_CYCLE)
    val_cap = int(settings.INFLIGHT_MAX_GROUP_VALUE_LEN)

    acc = InflightAccumulator()
    acc.add(_rule(10, group_by=("ausente",)), {"u": "x"}, organization_id=7)  # group_by_unresolved
    for i in range(key_cap + 2):                                              # key_cap
        acc.add(_rule(11), {"u": f"user{i}"}, organization_id=7)
    acc.add(_rule(12), {"u": "A" * (val_cap + 1)}, organization_id=7)         # group_value_truncated

    def _boom(*_a: object, **_k: object) -> int:                              # flush_lost
        raise RuntimeError("Postgres indisponível")

    # ``matcher`` NÃO tem call site dentro deste módulo: quem escreve é o
    # ``except`` de ``pipeline.py`` que envolve ``evaluate_ruleset``, e na forma
    # PLANA legada (``errors[reason] = int``), porque ali não existe ``rule_id``
    # — a exceção nasce antes de se saber qual regra estava sendo avaliada.
    #
    # Reproduzir a forma exata do call site externo é o ponto: sem esta linha o
    # teste de igualdade abaixo ficaria verde com ``matcher`` faltando no enum,
    # que foi exatamente o furo encontrado em revisão. Um guard que só enxerga
    # os call sites do próprio módulo promete mais do que entrega.
    acc.errors["matcher"] = acc.errors.get("matcher", 0) + 1                  # noqa: E501

    monkeypatch.setattr(runtime_mod, "_flush_sync", _boom)
    await flush_inflight(acc, organization_id=7)


# ── 1. Paridade: label emitido ≡ label declarado em otel_metrics._SPEC ──────


@pytest.mark.asyncio
async def test_every_inflight_label_matches_the_declared_spec(
    monkeypatch: pytest.MonkeyPatch, emitidos: list[tuple[str, dict]]
) -> None:
    """Um call site que passe um label a mais (ou a menos) produz uma série que
    o dashboard não casa. A fachada só valida a CONTAGEM de labels posicionais;
    os call sites de inflight usam kwargs, que passam direto."""
    await _exercise(monkeypatch)

    inflight = [(n, a) for n, a in emitidos if n.startswith("collector_inflight_")]

    # ANTI-VACUIDADE, antes de qualquer comparação: sem emissão nenhuma o laço
    # abaixo aprovaria por vacuidade.
    assert len(inflight) >= 4, f"nada foi emitido — o exercício não exercitou: {emitidos}"
    assert set(n for n, _ in inflight) == set(_INFLIGHT_SERIES), (
        "toda série collector_inflight_* do catálogo tem de ser exercitada aqui"
    )

    for name, attrs in inflight:
        assert set(attrs) == set(otel_metrics.labels_for(name)), (
            f"{name} emitido com labels {sorted(attrs)}, declarado "
            f"{sorted(otel_metrics.labels_for(name))}"
        )
        assert all(isinstance(v, str) for v in attrs.values())


@pytest.mark.asyncio
async def test_rule_id_is_never_a_label_of_the_error_series(
    monkeypatch: pytest.MonkeyPatch, emitidos: list[tuple[str, dict]]
) -> None:
    """O breakdown por regra é REAL (ele existe, no observability_store) — só
    não é label. Sem o par positivo abaixo, este assert negativo passaria por
    vacuidade num flush que não emitisse nada."""
    gravados: list[tuple[str, str, str]] = []
    await _exercise(
        monkeypatch,
        record_counter=lambda kind, oid, metric, *a, **k: gravados.append(
            (kind, oid, metric)
        ),
    )

    erros = [a for n, a in emitidos if n == "collector_inflight_errors_total"]
    assert len(erros) == len(ERROR_REASONS)
    assert all(set(a) == {"reason"} for a in erros)

    # Par POSITIVO: a atribuição por regra existe, e vive no Redis (TTL 25h).
    # Só para os reasons ATRIBUÍVEIS: ``matcher`` sai no OTel (por isso conta na
    # asserção acima) e NÃO no breakdown, porque não há regra a que atribuí-lo.
    # Essa assimetria é o contrato, não um esquecimento — e é por isso que ela
    # está declarada em ``UNATTRIBUTED_ERROR_REASONS`` e verificada aqui.
    atribuiveis = set(ERROR_REASONS) - set(UNATTRIBUTED_ERROR_REASONS)
    err_por_regra = [g for g in gravados if g[0] == "rule" and g[2].startswith("err_")]
    assert err_por_regra, "o breakdown por regra não foi gravado em lugar nenhum"
    assert {g[2] for g in err_por_regra} == {f"err_{r}" for r in atribuiveis}
    assert not any(
        g[2] == f"err_{r}" for g in err_por_regra for r in UNATTRIBUTED_ERROR_REASONS
    ), "um reason não-atribuível ganhou breakdown por regra com rule_id inventado"


# ── 2. Enums fechados POR COMPORTAMENTO, com IGUALDADE ─────────────────────


@pytest.mark.asyncio
async def test_emitted_error_reasons_equal_the_closed_enum(
    monkeypatch: pytest.MonkeyPatch, emitidos: list[tuple[str, dict]]
) -> None:
    """IGUALDADE, não inclusão: ``⊆ ERROR_REASONS`` é widening-safe e deixaria
    passar tanto um reason novo não declarado quanto um reason declarado que
    nenhum call site emite mais (label morta no painel)."""
    await _exercise(monkeypatch)

    razoes = [
        a["reason"] for n, a in emitidos if n == "collector_inflight_errors_total"
    ]
    assert len(razoes) >= 4, f"exercício não cobriu os 4 caminhos: {razoes}"
    assert set(razoes) == set(ERROR_REASONS)
    assert len(set(razoes)) == len(razoes), "cada razão sai UMA vez por flush"


@pytest.mark.asyncio
async def test_emitted_reject_reasons_equal_the_closed_enum(
    monkeypatch: pytest.MonkeyPatch, emitidos: list[tuple[str, dict]]
) -> None:
    """O enum que ``iter_reject_reasons`` prometia travar e nunca travou."""
    await _exercise(monkeypatch)

    razoes = [
        a["reason"]
        for n, a in emitidos
        if n == "collector_inflight_rules_rejected_total"
    ]
    assert len(razoes) >= 4, f"exercício não cobriu as rejeições: {razoes}"
    assert set(razoes) == set(REJECT_REASONS)


@pytest.mark.asyncio
async def test_the_two_enums_never_share_a_series(
    monkeypatch: pytest.MonkeyPatch, emitidos: list[tuple[str, dict]]
) -> None:
    """``rules_rejected`` (falha de COMPILAÇÃO, na carga) e ``errors`` (falha de
    AVALIAÇÃO/flush) são séries e momentos distintos. Um reason vazando de uma
    para a outra tornaria os dois painéis indistinguíveis."""
    await _exercise(monkeypatch)

    por_serie: dict[str, set[str]] = {}
    for n, a in emitidos:
        if n in ("collector_inflight_errors_total", "collector_inflight_rules_rejected_total"):
            por_serie.setdefault(n, set()).add(a["reason"])

    assert len(por_serie) == 2, f"as duas séries têm de ter sido emitidas: {por_serie}"
    erro = por_serie["collector_inflight_errors_total"]
    rejeicao = por_serie["collector_inflight_rules_rejected_total"]
    assert erro & rejeicao == set()
    assert set(ERROR_REASONS) & set(REJECT_REASONS) == set()


def test_the_dead_helper_is_gone() -> None:
    """``iter_reject_reasons`` existia SÓ para este arquivo, que não existia.
    O enum agora é travado por comportamento; manter o atalho convidaria um
    teste de inclusão sobre a constante — exatamente o que não pega nada."""
    assert not hasattr(runtime_mod, "iter_reject_reasons")
