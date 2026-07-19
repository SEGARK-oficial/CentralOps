"""Corretude e observabilidade do motor batch (ADR-0015, Fase 2).

Quatro defeitos, todos da mesma família: o motor produzia o MESMO sintoma
observável — silêncio, com a regra verde na UI — para causas completamente
diferentes. Um detector que falha em silêncio é pior que um detector ausente,
porque o cliente acredita estar coberto e para de procurar cobertura em outro
lugar.

1. ISO sem timezone lido no fuso LOCAL do processo (3h de deslocamento em prod).
2. Epoch em µs/ns tratado como segundos inflados (evento no ano 58.000).
3. ``where_json`` malformado degradando para "sem filtro" — fail-OPEN.
4. ``logger`` declarado e nunca chamado em nenhum dos seis caminhos de descarte.
"""

from __future__ import annotations

import logging
import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import types
from datetime import datetime, timedelta, timezone

import pytest

from backend.app.services.correlation_engine import _parse_ts, evaluate_threshold

_LOGGER = "backend.app.services.correlation_engine"


def _rule(**over):
    base = dict(
        id=42, min_count=2, group_by_field="ip", window_seconds=300,
        timestamp_field="ts", where_json=None,
    )
    base.update(over)
    return types.SimpleNamespace(**base)


# ── 1. Corretude temporal ────────────────────────────────────────────────────

_BASE = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "label,value",
    [
        ("epoch segundos", int(_BASE.timestamp())),
        ("epoch ms", int(_BASE.timestamp() * 1e3)),
        ("epoch µs", int(_BASE.timestamp() * 1e6)),
        ("epoch ns", int(_BASE.timestamp() * 1e9)),
        ("ISO com Z", "2026-07-18T12:00:00Z"),
        ("ISO com offset", "2026-07-18T09:00:00-03:00"),
        ("ISO NAIVE", "2026-07-18T12:00:00"),
    ],
)
def test_every_timestamp_form_resolves_to_the_same_instant(label, value):
    """Sete formas do MESMO instante têm de convergir.

    O caso que motivou: numa correlação cross-source basta uma fonte emitir
    "...Z" e outra naive — Wazuh + CloudTrail é cenário normal — para os eventos
    se espalharem por horas e a regra nunca mais disparar. E o bug era
    dependente do ambiente: funcionava em dev (UTC) e falhava em produção
    (America/Sao_Paulo).
    """
    got = _parse_ts(value)
    assert got is not None, f"{label} não parseou"
    assert abs(got - _BASE.timestamp()) < 1.0, (
        f"{label} → {got}, esperado ~{_BASE.timestamp()} "
        f"(delta {abs(got - _BASE.timestamp()):.0f}s)"
    )


def test_naive_iso_does_not_depend_on_process_timezone(monkeypatch):
    """Blinda contra o modo de falha real: mesmo dado, resultado diferente
    conforme o TZ do container."""
    import time as _time

    got_utc = _parse_ts("2026-07-18T12:00:00")
    monkeypatch.setenv("TZ", "America/Sao_Paulo")
    if hasattr(_time, "tzset"):
        _time.tzset()
    try:
        got_sp = _parse_ts("2026-07-18T12:00:00")
    finally:
        monkeypatch.delenv("TZ", raising=False)
        if hasattr(_time, "tzset"):
            _time.tzset()
    assert got_utc == got_sp, (
        "o mesmo ISO naive produziu instantes diferentes conforme o TZ do "
        "processo — é o bug de 3h que quebra correlação cross-source em prod"
    )


def test_nanosecond_epoch_no_longer_lands_in_the_far_future():
    """Antes, com um único degrau, ns virava segundo inflado por 1e9."""
    ns = int(_BASE.timestamp() * 1e9)
    got = _parse_ts(ns)
    assert 1.7e9 < got < 1.9e9, f"epoch ns virou {got} — fora da era atual"


def test_window_actually_fires_with_microsecond_timestamps():
    """Prova de ponta a ponta: com µs, a janela precisa fechar e a regra disparar."""
    us = lambda d: int((_BASE + timedelta(seconds=d)).timestamp() * 1e6)  # noqa: E731
    items = [{"ip": "10.0.0.1", "ts": us(i)} for i in range(5)]
    hits = evaluate_threshold(_rule(min_count=3, window_seconds=60), items)
    assert len(hits) == 1 and hits[0]["count"] == 5


# ── 2. where_json malformado agora é FAIL-CLOSED ─────────────────────────────

@pytest.mark.parametrize("bad", ["nao-e-json", '{"nao":"lista"}', "[1,2,", '"string"'])
def test_malformed_where_json_never_fires(bad, caplog):
    """O defeito: ``filters = []`` fazia o laço não filtrar NADA, então
    "5 eventos ONDE event=auth_failed" virava "5 eventos QUAISQUER"."""
    items = [{"ip": "10.0.0.1", "ts": int(_BASE.timestamp())} for _ in range(50)]
    with caplog.at_level(logging.ERROR, logger=_LOGGER):
        hits = evaluate_threshold(_rule(where_json=bad), items)
    assert hits == [], "where_json inválido disparou sobre todos os eventos"
    assert any("where_json" in r.message for r in caplog.records), (
        "fail-closed precisa ser RUIDOSO — silêncio aqui esconde regra quebrada"
    )


def test_valid_where_json_still_filters_normally():
    """A correção não pode quebrar o caminho bom."""
    items = (
        [{"ip": "1.1.1.1", "ev": "auth_failed", "ts": int(_BASE.timestamp())}] * 3
        + [{"ip": "1.1.1.1", "ev": "auth_ok", "ts": int(_BASE.timestamp())}] * 9
    )
    rule = _rule(where_json='[{"field":"ev","op":"eq","value":"auth_failed"}]', min_count=3)
    hits = evaluate_threshold(rule, items)
    assert len(hits) == 1 and hits[0]["count"] == 3, "o filtro deixou passar auth_ok"


# ── 3. Os seis caminhos de descarte deixaram de ser mudos ────────────────────

def test_no_group_formed_is_explained(caplog):
    """'Criei a regra e ela não dispara' é o ticket nº1 desta feature."""
    items = [{"outro_campo": "x", "ts": int(_BASE.timestamp())} for _ in range(10)]
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        evaluate_threshold(_rule(), items)
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "0 grupos" in msg or "0 grupo" in msg
    assert "10" in msg, "o log precisa dizer quantos eventos foram vistos"
    assert "ip" in msg, "o log precisa nomear o campo de agrupamento que falhou"


def test_window_without_timestamp_field_warns_that_the_window_is_off(caplog):
    """Comportamento preservado por compatibilidade, mas não mais silencioso."""
    items = [{"ip": "1.1.1.1"} for _ in range(5)]
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        hits = evaluate_threshold(_rule(timestamp_field=None, min_count=3), items)
    assert len(hits) == 1, "sem timestamp_field a contagem ignora a janela (by design)"
    assert any("JANELA ESTÁ DESLIGADA" in r.getMessage() for r in caplog.records)


def test_group_without_any_valid_timestamp_is_reported(caplog):
    """Fail-closed correto, mas era invisível — o sintoma de timestamp_field
    apontando para o campo errado."""
    items = [{"ip": "1.1.1.1", "ts": "nao-e-data"} for _ in range(5)]
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        hits = evaluate_threshold(_rule(min_count=2), items)
    assert hits == []
    assert any("timestamp" in r.getMessage().lower() for r in caplog.records)


def test_missing_group_by_field_is_explained(caplog):
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert evaluate_threshold(_rule(group_by_field=None), [{"a": 1}]) == []
    assert any("group_by_field" in r.getMessage() for r in caplog.records)


def test_non_positive_min_count_is_explained(caplog):
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert evaluate_threshold(_rule(min_count=0), [{"ip": "1.1.1.1"}]) == []
    assert any("min_count" in r.getMessage() for r in caplog.records)


def test_groups_formed_but_none_reached_min_count_is_explained(caplog):
    items = [{"ip": f"10.0.0.{i}", "ts": int(_BASE.timestamp())} for i in range(5)]
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        assert evaluate_threshold(_rule(min_count=99), items) == []
    assert any("min_count" in r.getMessage() for r in caplog.records)


def test_the_logger_is_actually_used():
    """Guard estrutural: o ``logger`` do módulo era declarado e nunca chamado.

    Um grep por ``logger.`` retornava EXCLUSIVAMENTE a linha da declaração —
    seis caminhos de descarte, todos mudos.
    """
    import inspect

    from backend.app.services import correlation_engine

    src = inspect.getsource(correlation_engine)
    calls = sum(src.count(f"logger.{lvl}(") for lvl in
                ("debug", "info", "warning", "error", "exception"))
    assert calls >= 6, (
        f"apenas {calls} chamadas de logger — cada causa de descarte precisa "
        "ser distinguível das outras no log"
    )
