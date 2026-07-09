"""Detector de drift — flatten_paths e compute_unknown_paths.

Os tests de persistência (record_unknown_fields tocando DB) são
cobertos no test integration via seed/migrations.
"""

from __future__ import annotations

import backend.app.collectors.normalize.drift as drift_mod
from backend.app.collectors.normalize.drift import (
    compute_unknown_paths,
    flatten_paths,
    should_capture,
)


def test_flatten_paths_simple_dict() -> None:
    paths = dict(flatten_paths({"a": 1, "b": "x"}))
    assert paths == {"a": 1, "b": "x"}


def test_flatten_paths_nested_dict() -> None:
    paths = dict(flatten_paths({"a": {"b": {"c": 42}}}))
    assert paths == {"a.b.c": 42}


def test_flatten_paths_with_list_samples() -> None:
    paths = dict(flatten_paths({"items": [{"id": "x"}, {"id": "y"}, {"id": "z"}, {"id": "w"}]}))
    # _MAX_LIST_SAMPLES = 3 → só primeiros 3 índices.
    assert "items[0].id" in paths
    assert "items[1].id" in paths
    assert "items[2].id" in paths
    assert "items[3].id" not in paths


def test_flatten_paths_empty_dict() -> None:
    paths = flatten_paths({})
    # Dict vazio aparece como folha (com placeholder ``{}``) — sample_value útil.
    assert len(paths) == 1
    assert paths[0][0] == "{}"


def test_flatten_paths_empty_list() -> None:
    paths = dict(flatten_paths({"tags": []}))
    assert "tags[]" in paths


def test_compute_unknown_paths_excludes_consumed_top_keys() -> None:
    raw = {
        "id": "a",
        "severity": "high",
        "createdAt": "2026-04-23T10:00:00Z",
        "extra_field_1": "yes",
        "nested": {"deep": "value"},
    }
    consumed = {
        "normalized.finding_info.uid",  # vem de "id"
        "normalized.severity_id",       # vem de "severity"
        "normalized.time",               # vem de "createdAt"
    }
    # NOTA: a heurística atual filtra pelo TOP-key do path. Como
    # consumed_paths são targets (com prefixo "normalized."), a função
    # remove o prefixo e usa o segmento seguinte como sinal de "consumido".
    # Aqui consumed_top_keys = {"finding_info", "severity_id", "time"} —
    # NENHUM bate com top-keys do raw, então TODOS os campos viram
    # "unknown". O cenário realista: a engine vai evoluir para anotar
    # source paths (e.g. ``severity``) em vez de targets.

    unknowns = compute_unknown_paths(raw, consumed)
    # Pelo menos os campos extras DEVEM estar presentes.
    paths = {p for p, _ in unknowns}
    assert "extra_field_1" in paths
    assert "nested.deep" in paths


def test_compute_unknown_paths_treats_lookalike_target_as_consumed() -> None:
    # Caso onde a target é ``normalized.severity_id`` mas o raw tem
    # diretamente uma chave ``severity_id`` no topo: a heurística
    # atualmente trata o segmento como consumido. Versões futuras podem
    # refinar — esse teste documenta o comportamento corrente.
    raw = {"severity_id": 4, "extra": "x"}
    consumed = {"normalized.severity_id"}
    unknowns = compute_unknown_paths(raw, consumed)
    paths = {p for p, _ in unknowns}
    # ``severity_id`` filtrado; ``extra`` segue como unknown.
    assert "extra" in paths
    assert "severity_id" not in paths


def test_compute_unknown_skips_underscore_namespace() -> None:
    """Campos do namespace ``_`` (preprocess virtual) nunca viram unknown.

    ``source:_processed.x`` e ``_processed.x`` são paths virtuais
    produzidos por ops preprocess. Não existem no raw — o
    detector de drift deve ignorá-los silenciosamente. Apenas
    ``other_field``, que está no raw mas não consumido, deve aparecer.
    """
    raw = {
        "processedData": "some-value",
        "other_field": "unrelated",
    }
    consumed = {
        "source:processedData",    # raw path legítimo — top-key consumida
        "source:_processed.x",     # virtual preprocess via source: prefix
        "_processed.x",            # virtual preprocess, literal
    }
    unknowns = compute_unknown_paths(raw, consumed)
    paths = {p for p, _ in unknowns}

    # ``other_field`` não foi consumido → deve aparecer como unknown.
    assert "other_field" in paths
    # ``processedData`` foi consumido via "source:processedData" → não é unknown.
    assert "processedData" not in paths
    # Nenhum path com prefixo ``_`` deve vazar como unknown.
    assert not any(p.startswith("_") for p in paths)


def test_compute_unknown_with_dsl_version_2() -> None:
    """Passar dsl_version=2 não altera o comportamento atual (compat forward).

    O parâmetro é reservado para lógica v2-específica. Hoje
    o resultado deve ser idêntico ao obtido com o default dsl_version=1.
    """
    raw = {
        "alert": {"severity": "high"},
        "unmapped_field": "value",
    }
    consumed = {"source:alert"}

    result_v1 = compute_unknown_paths(raw, consumed, dsl_version=1)
    result_v2 = compute_unknown_paths(raw, consumed, dsl_version=2)

    paths_v1 = {p for p, _ in result_v1}
    paths_v2 = {p for p, _ in result_v2}

    # Comportamento idêntico independente da versão da DSL por enquanto.
    assert paths_v1 == paths_v2
    # ``unmapped_field`` não consumido → unknown em ambas as versões.
    assert "unmapped_field" in paths_v1
    # ``alert.severity`` consumido via top-key "alert" → não é unknown.
    assert "alert.severity" not in paths_v1


# ── Janela de aprendizado (should_capture) ───────────────────────────────────


def test_learning_window_forces_capture_for_new_source() -> None:
    """Fonte NOVA: os 1ºs ``learning_events`` são capturados a 100% mesmo com
    sample_rate=0 (auto-discovery de syslog recém-apontado)."""
    drift_mod._seen_counts.clear()
    # sample_rate=0 → sem a janela, NUNCA capturaria. Com a janela, os 3 primeiros sim.
    caps = [
        should_capture("fortinet_fortigate", "traffic", 1, 0.0, learning_events=3)
        for _ in range(5)
    ]
    assert caps[:3] == [True, True, True]  # janela de aprendizado
    assert caps[3:] == [False, False]      # depois cai na amostragem (rate=0 → nunca)


def test_learning_window_is_per_org_vendor_event() -> None:
    """A janela é por combinação (org, vendor, event_type): orgs/vendors distintos
    têm janelas independentes — um não consome a cota do outro."""
    drift_mod._seen_counts.clear()
    assert should_capture("fortigate", "traffic", 1, 0.0, learning_events=1) is True
    assert should_capture("fortigate", "traffic", 1, 0.0, learning_events=1) is False
    # outra org → janela nova
    assert should_capture("fortigate", "traffic", 2, 0.0, learning_events=1) is True
    # outro vendor, mesma org → janela nova
    assert should_capture("paloalto", "traffic", 1, 0.0, learning_events=1) is True


def test_no_learning_window_falls_back_to_sampling() -> None:
    """learning_events=0 desliga o boost → puramente sample_rate."""
    drift_mod._seen_counts.clear()
    assert should_capture("v", "e", 1, 1.0, learning_events=0) is True   # rate=1 sempre
    assert should_capture("v", "e", 1, 0.0, learning_events=0) is False  # rate=0 nunca


def test_learning_window_respects_key_cap() -> None:
    """Mapa de rastreio cheio → chaves novas não ganham boost (degradação graciosa),
    caindo direto na amostragem sem estourar memória."""
    drift_mod._seen_counts.clear()
    original = drift_mod._MAX_TRACKED_KEYS
    try:
        drift_mod._MAX_TRACKED_KEYS = 1
        assert should_capture("v1", "e", 1, 0.0, learning_events=5) is True   # 1ª chave ok
        # 2ª chave: mapa cheio → sem boost, sample_rate=0 → False
        assert should_capture("v2", "e", 1, 0.0, learning_events=5) is False
    finally:
        drift_mod._MAX_TRACKED_KEYS = original
        drift_mod._seen_counts.clear()
