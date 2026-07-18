"""Orçamento de escrita de quarentena por ciclo (ADR-0015, Fase 0).

Antes desta ADR só o caminho validate-OCSF tinha teto; os outros quatro
(``missing_mapping``, ``map`` ×2, ``missing_customer_id``) escreviam sem limite.
Sob uma regressão sistêmica — mapping deletado, ``customer_id`` que parou de
resolver, vendor mudando o schema — TODO evento do ciclo vira uma escrita no DB.
É a mesma forma do poison-loop de coletor já vivido em produção: drenar o backlog
inteiro num run → soft-timeout → rollback → não coleta.

Este é o guard executável exigido pela regra R8 da ADR ("toda constante nova de
teto/TTL nasce com teste de invariante"). O produto já foi mordido três vezes por
invariante que era só comentário.
"""

from __future__ import annotations

import logging
import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import pytest

from backend.app.collectors import quarantine
from backend.app.collectors.pipeline import _make_quarantine_budget
from backend.app.core.config import settings


def test_budget_allows_exactly_cap_writes():
    ok = _make_quarantine_budget(integration_id=1, platform="sophos")
    assert [ok("map", 3) for _ in range(3)] == [True, True, True]
    assert ok("map", 3) is False


def test_budget_is_per_kind_not_shared():
    """O ponto do desenho: uma razão barulhenta não pode esconder as outras.

    Com orçamento compartilhado, 100 ``missing_mapping`` consumiriam tudo e o
    único erro de ``map`` do ciclo — o mais informativo — nunca seria escrito.
    """
    ok = _make_quarantine_budget(integration_id=1, platform="sophos")
    for _ in range(3):
        assert ok(quarantine.ERROR_KIND_MISSING_MAPPING, 3) is True
    assert ok(quarantine.ERROR_KIND_MISSING_MAPPING, 3) is False

    # A razão vizinha continua com orçamento intacto.
    assert ok(quarantine.ERROR_KIND_MAP, 3) is True


def test_budget_is_per_cycle_not_global():
    """Cada ciclo recebe orçamento novo — senão um pico transitório silenciaria
    a quarentena para sempre, o que seria pior que a amplificação de escrita."""
    a = _make_quarantine_budget(integration_id=1, platform="sophos")
    assert a("map", 1) is True
    assert a("map", 1) is False

    b = _make_quarantine_budget(integration_id=1, platform="sophos")
    assert b("map", 1) is True


def test_cap_exhaustion_is_loud_exactly_once(caplog):
    """Fail-LOUD, mas sem inundar o log.

    Silêncio ao estourar seria o pior caso: o operador veria a fila de quarentena
    parar de crescer e concluiria que o problema cessou, quando na verdade
    escalou. Logar por evento seria a outra falha — trocaria amplificação de
    escrita no DB por amplificação de escrita no log.
    """
    ok = _make_quarantine_budget(integration_id=42, platform="sophos")
    with caplog.at_level(logging.WARNING, logger="backend.app.collectors.pipeline"):
        assert ok("map", 1) is True
        for _ in range(50):
            assert ok("map", 1) is False

    warnings = [r for r in caplog.records if "teto de escrita atingido" in r.message]
    assert len(warnings) == 1, (
        f"esperado exatamente 1 WARNING por razão por ciclo, veio {len(warnings)}"
    )
    # O log precisa carregar o suficiente para diagnosticar sem abrir o código.
    formatted = warnings[0].getMessage()
    assert "map" in formatted and "42" in formatted and "sophos" in formatted


def test_zero_cap_blocks_every_write():
    """Teto 0 = desligar a escrita de quarentena sem desligar a métrica."""
    ok = _make_quarantine_budget(integration_id=1, platform="sophos")
    assert ok("map", 0) is False


@pytest.mark.parametrize(
    "setting_name",
    ["QUARANTINE_MAX_PER_KIND_PER_RUN", "OCSF_QUARANTINE_MAX_PER_RUN"],
)
def test_caps_are_configured_and_positive(setting_name: str):
    """Um teto <= 0 por engano desligaria a quarentena inteira em silêncio —
    o evento seguiria não-despachado, mas sem NENHUM registro para diagnosticar."""
    value = getattr(settings, setting_name)
    assert isinstance(value, int)
    assert value > 0, f"{setting_name}={value} desligaria a escrita de quarentena"


def test_every_quarantine_write_in_the_pipeline_is_budgeted():
    """Guard estrutural: nenhum ``_quarantine_async`` pode escapar do orçamento.

    Um caminho novo de quarentena adicionado sem teto reintroduz exatamente a
    vulnerabilidade que esta ADR fechou, e passaria despercebido por qualquer
    teste comportamental que não conhecesse esse caminho específico.
    """
    import inspect

    from backend.app.collectors import pipeline

    src = inspect.getsource(pipeline._run_collection_once)
    calls = src.count("await _quarantine_async(")
    guards = src.count("_quarantine_budget_ok(")
    assert calls == guards, (
        f"{calls} chamadas a _quarantine_async mas {guards} checagens de orçamento "
        "— algum caminho de quarentena escreve sem teto por ciclo."
    )


# ── Beat: nenhuma entry pode apontar para task inexistente (ADR-0015) ────────
#
# A Fase 0 adicionou ``collectors.dedupe_sample_redis_health`` ao beat. Uma entry
# cujo nome de task não está registrada no Celery falha em SILÊNCIO: o beat
# publica na fila, nenhum worker sabe executar, e a mensagem expira. O sintoma é
# ausência de dados — que é indistinguível de "está tudo bem" numa métrica de
# saúde. É a mesma classe de falha que esta ADR persegue no resto do produto.

def _registered_task_names() -> set[str]:
    """Nomes de task após importar os módulos do ``include``.

    ``celery_app.tasks`` é populado PREGUIÇOSAMENTE: no import do módulo ele está
    quase vazio, e só o worker (ou ``import_default_modules``) carrega o
    ``include``. Sem forçar isso, um guard ingênuo acusaria TODA entry de beat —
    inclusive as que funcionam em produção — e seria descartado como ruído.
    """
    import importlib

    from backend.app.collectors.celery_app import _build_include, celery_app

    for module in _build_include():
        importlib.import_module(module)
    return set(celery_app.tasks.keys())


def test_every_beat_entry_points_at_a_registered_task():
    from backend.app.collectors.beat_schedule import _static_entries

    registered = _registered_task_names()
    entries = _static_entries()
    missing = {
        name: cfg["task"]
        for name, cfg in entries.items()
        if cfg.get("task") and cfg["task"] not in registered
    }
    assert not missing, (
        "entries de beat apontando para tasks NÃO registradas (publicariam numa "
        f"fila que ninguém consome): {missing}\n"
        "Registre o módulo em celery_app._MODULES_WITH_TASKS."
    )


def test_dedupe_health_task_is_scheduled_and_registered():
    """Guard específico do item da Fase 0 — o call-site que faltava.

    ``sample_redis_health`` existia, testada, e não era chamada por ninguém: uma
    métrica de saúde que nunca é amostrada é pior que nenhuma, porque o painel
    fica verde por ausência de dado.
    """
    from backend.app.collectors.beat_schedule import _static_entries

    entry = _static_entries().get("dedupe-redis-health")
    assert entry is not None, "entry de beat da saúde do dedupe ausente"
    assert entry["task"] == "collectors.dedupe_sample_redis_health"
    assert entry["task"] in _registered_task_names(), "task não registrada no Celery"
