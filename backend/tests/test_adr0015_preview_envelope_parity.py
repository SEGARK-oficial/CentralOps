"""Paridade entre o envelope do preview e o de produção (ADR-0015, Fase 3).

ESTE É O PORTÃO DA PR DE PREVIEW. Se falhar, o preview não deve ser mergeado.

O reservoir guarda o raw NU, pré-normalização. O matcher avalia paths enraizados
no ENVELOPE (``_centralops.*``, ``normalized.*``, ``raw.*``). Se o preview
avaliar a saída crua do ``peek``, TODA cláusula resolve ``None`` e o autor vê
"0 de 100" para uma regra perfeita — e conclui que a regra está errada quando o
errado é o preview.

A armadilha mais sutil é a segunda: em produção o envelope carrega
``applied.reduced_raw or raw_event``, o raw TRIMADO quando o mapping tem
``raw_reduction``. Um preview que usasse o raw nu enxergaria campos que em
produção foram cortados, e diria "funciona" para uma regra que nunca dispara —
o pior resultado possível, porque dá confiança falsa.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import inspect

import pytest

from backend.app.collectors.inflight.preview import (
    build_preview_envelope,
    evaluate_preview,
)


_RULES = [
    {"target": "class_uid", "source": "eventType", "default": 1001},
    {"target": "severity_id", "source": "sev", "default": 1},
    {"target": "message", "source": "msg", "default": ""},
]

_RAW = {
    "id": "evt-1",
    "eventType": 2001,
    "sev": 4,
    "msg": "Malware detected",
    "user": "alice",
    "src_ip": "10.0.0.5",
}


def _envelope(raw=None, rules=None):
    return build_preview_envelope(
        raw or dict(_RAW),
        vendor="sophos",
        integration_id=7,
        organization_id=1,
        organization_name="ACME",
        customer_id="c-1",
        stream="alerts",
        event_type="alert",
        mapping_version_id=3,
        rules=rules or _RULES,
        dsl_version=1,
    )


# ── A estrutura precisa ser a que o matcher espera ───────────────────────────

def test_envelope_has_the_three_blocks_the_matcher_navigates():
    env = _envelope()
    assert env is not None, "a normalização falhou — o fixture está inválido"
    for block in ("_centralops", "normalized", "raw"):
        assert block in env, (
            f"bloco '{block}' ausente: toda cláusula que aponta para ele "
            "resolveria None e o preview diria '0 de N' para regra correta"
        )


def test_centralops_carries_the_routing_labels_rules_use():
    """``_centralops.vendor`` e ``event_type`` são as cláusulas mais comuns —
    tipicamente a PRIMEIRA de qualquer regra, que discrimina a fonte."""
    cc = _envelope()["_centralops"]
    assert cc.get("vendor") == "sophos"
    assert cc.get("event_type") == "alert"
    assert cc.get("organization_id") == 1
    assert cc.get("stream") == "alerts"


def test_raw_block_preserves_the_original_fields():
    raw = _envelope()["raw"]
    assert raw.get("user") == "alice"
    assert raw.get("src_ip") == "10.0.0.5"


def test_clauses_against_a_reconstructed_envelope_actually_match():
    """Prova de ponta a ponta do portão: uma regra correta CASA."""
    env = _envelope()
    where = (
        '[{"field":"_centralops.vendor","op":"eq","value":"sophos"},'
        '{"field":"raw.user","op":"eq","value":"alice"}]'
    )
    result = evaluate_preview([env], where)
    assert result.state == "ok"
    assert result.matched == 1, (
        "regra correta não casou contra o envelope reconstruído — é exatamente "
        "o falso-negativo que este arquivo existe para impedir"
    )


def test_a_naked_raw_would_fail_proving_the_test_is_not_vacuous():
    """Mutação: avaliar o raw NU (o que o ``peek`` devolve) precisa FALHAR.

    Sem esta prova, o teste acima poderia passar por acidente e o portão seria
    decorativo.
    """
    where = '[{"field":"_centralops.vendor","op":"eq","value":"sophos"}]'
    result = evaluate_preview([dict(_RAW)], where)
    assert result.matched == 0


# ── A armadilha do raw_reduction ─────────────────────────────────────────────

def test_preview_uses_the_same_reduced_raw_expression_as_the_pipeline():
    """Guard estrutural sobre a expressão exata.

    Em produção o envelope recebe ``applied.reduced_raw or raw_event``. Se o
    preview usar só ``raw_event``, ele enxerga campos que o mapping trimou e diz
    "funciona" para uma regra muda — confiança falsa, o pior resultado.
    """
    src = inspect.getsource(build_preview_envelope)
    assert "applied.reduced_raw or raw_event" in src, (
        "a expressão precisa ser IDÊNTICA à de pipeline.py; qualquer variação "
        "faz o preview avaliar uma estrutura que não roda em produção"
    )


def test_pipeline_still_uses_that_expression():
    """A paridade é bilateral: se o pipeline mudar, este teste avisa."""
    from backend.app.collectors import pipeline

    src = inspect.getsource(pipeline._run_collection_once)
    assert "applied.reduced_raw or raw_event" in src, (
        "pipeline.py mudou a montagem do envelope — preview.py precisa "
        "acompanhar, senão o preview passa a mentir"
    )


def test_degraded_fields_are_propagated():
    """``degraded_fields`` marca campos preenchidos por fallback de ingestão. Uma
    regra que dependa deles precisa ver o mesmo que produção vê."""
    src = inspect.getsource(build_preview_envelope)
    assert "degraded_fields" in src


# ── O preview nunca escreve ──────────────────────────────────────────────────

def test_preview_never_calls_flush_or_creates_detections():
    """Se reusasse ``flush_inflight``, um autor testando 30 vezes injetaria
    disparos fantasma no contador de 24h — contaminando justamente o sinal que
    o kill switch usa para decidir."""
    import ast
    from pathlib import Path

    from backend.app.collectors.inflight import preview

    # AST e não substring: o docstring do módulo CITA ``flush_inflight``
    # justamente para documentar que não o usa, e um guard ingênuo acusaria a
    # própria explicação. O que importa é se há CHAMADA.
    tree = ast.parse(Path(preview.__file__).read_text())
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name):
                called.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                called.add(fn.attr)

    forbidden = {"flush_inflight", "record", "add", "commit"} & called
    assert not forbidden, (
        f"preview.py CHAMA {forbidden} — o preview não pode escrever. Se "
        "reusasse o flush, um autor testando 30 vezes injetaria disparos "
        "fantasma no contador de 24h, contaminando o sinal do kill switch."
    )
    # E o import também não pode existir.
    imported = {
        n.name for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) for n in node.names
    }
    assert "flush_inflight" not in imported
    assert "DetectionRepository" not in imported


def test_preview_uses_the_pure_matcher():
    from backend.app.collectors.inflight import preview

    src = inspect.getsource(preview)
    assert "evaluate_ruleset" in src


# ── Diagnóstico: path resolvido vs valor casado ──────────────────────────────

def test_verdict_separates_missing_field_from_wrong_value():
    """A distinção que torna o preview útil.

    ``path_resolved=0`` = o campo não existe onde você apontou (path errado,
    atravessando array, ou trimado). ``path_resolved=N, matched=0`` = o campo
    existe e o valor não bate. Problemas diferentes, correções diferentes.
    """
    env = _envelope()
    campo_errado = evaluate_preview(
        [env], '[{"field":"raw.nao_existe","op":"eq","value":"x"}]'
    )
    assert campo_errado.clauses[0].path_resolved == 0

    valor_errado = evaluate_preview(
        [env], '[{"field":"raw.user","op":"eq","value":"bob"}]'
    )
    assert valor_errado.clauses[0].path_resolved == 1
    assert valor_errado.clauses[0].matched == 0


def test_observed_values_help_the_author_see_what_the_field_holds():
    env = _envelope()
    r = evaluate_preview([env], '[{"field":"raw.user","op":"eq","value":"bob"}]')
    assert "alice" in r.clauses[0].observed, (
        "mostrar o valor observado é o que mata a coerção numérica e o typo "
        "num olhar"
    )


def test_observed_values_are_truncated():
    env = _envelope({**_RAW, "msg": "x" * 5000})
    r = evaluate_preview([env], '[{"field":"raw.msg","op":"eq","value":"nada"}]')
    assert all(len(v) <= 121 for v in r.clauses[0].observed)


# ── Frescor e estados ────────────────────────────────────────────────────────

def test_result_reports_the_sample_time_window():
    """O reservoir NÃO tem TTL: uma integração parada há 3 meses devolve dados
    de 3 meses atrás, indistinguíveis de tráfego vivo."""
    env = _envelope()
    r = evaluate_preview([env], '[{"field":"raw.user","op":"eq","value":"alice"}]')
    assert r.state == "ok"
    # normalized["time"] vem do mapping; quando ausente os campos são None, mas
    # a CHAVE precisa existir para a UI poder avisar.
    assert hasattr(r, "oldest_event_time") and hasattr(r, "newest_event_time")


def test_empty_reservoir_is_a_distinct_state():
    r = evaluate_preview([], '[{"field":"a","op":"eq","value":"x"}]')
    assert r.state == "empty"


@pytest.mark.parametrize(
    "where,reason",
    [("nao-e-json", "bad_json"), ("[]", "empty_where"),
     ('[{"field":"a","op":"regex","value":"x"}]', "unknown_op")],
)
def test_uncompilable_rule_reports_the_reason_not_a_count(where, reason):
    """Dizer "0 de 100" para uma regra que nem compila seria a mentira mais
    cara do preview."""
    r = evaluate_preview([_envelope()], where)
    assert r.state == "invalid"
    assert r.reason == reason
