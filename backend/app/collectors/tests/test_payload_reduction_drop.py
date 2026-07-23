"""Primitiva de DROP do raw_reduction (drop / keep_only / drop_nulls / listas).

Contexto: até aqui o raw_reduction só CLIPAVA tamanho (max_bytes/max_items) e
não entrava em listas — não havia como remover o lixo que sobra depois da
extração (rawData.lineage já parseado, alerts[].evidences, ...). Era o único
eixo de redução que todo concorrente trata como primitiva de 1ª classe
(Vector del(), Cribl "Remove fields", Tenzir drop, Fluent Bit Remove_key).
"""
from __future__ import annotations

import pytest

from backend.app.collectors.normalize.exceptions import MappingDefinitionError
from backend.app.collectors.normalize.payload_reduction import (
    apply_raw_reduction,
    compile_raw_reduction,
)


def _c(specs):
    return compile_raw_reduction(specs)


# ── drop ─────────────────────────────────────────────────────────────────────

def test_drop_removes_the_key_entirely():
    raw = {"rawData": {"lineage": "x" * 5000, "useful": "keep"}}
    out = apply_raw_reduction(raw, _c([{"path": "rawData.lineage", "drop": True}]))
    assert "lineage" not in out["rawData"]
    assert out["rawData"]["useful"] == "keep"
    assert "rawData.lineage(dropped)" in out["_centralops_reduced"]


def test_drop_never_mutates_the_original_raw():
    raw = {"rawData": {"lineage": "x", "useful": "y"}}
    apply_raw_reduction(raw, _c([{"path": "rawData.lineage", "drop": True}]))
    assert raw["rawData"]["lineage"] == "x"  # original intacto


def test_drop_of_absent_path_is_a_noop():
    raw = {"a": 1}
    assert apply_raw_reduction(raw, _c([{"path": "nao.existe", "drop": True}])) is None


def test_drop_of_top_level_raw_blob():
    """O caso do sophos.detection: os blobs JSON-string já parseados pelo
    preprocess viram lixo puro no envelope."""
    raw = {"rawData": {"raw": "{...}", "items": "[...]", "meta_hostname": "srv-01"}}
    out = apply_raw_reduction(
        raw,
        _c([{"path": "rawData.raw", "drop": True}, {"path": "rawData.items", "drop": True}]),
    )
    assert set(out["rawData"]) == {"meta_hostname"}


# ── keep_only ────────────────────────────────────────────────────────────────

def test_keep_only_prunes_siblings():
    raw = {"rawData": {"a": 1, "b": 2, "lixo1": 3, "lixo2": 4}}
    out = apply_raw_reduction(raw, _c([{"path": "rawData", "keep_only": ["a", "b"]}]))
    assert out["rawData"] == {"a": 1, "b": 2}


def test_keep_only_is_noop_when_nothing_to_remove():
    raw = {"rawData": {"a": 1}}
    assert apply_raw_reduction(raw, _c([{"path": "rawData", "keep_only": ["a", "b"]}])) is None


def test_keep_only_survives_vendor_adding_fields():
    """A vantagem sobre enumerar drops: campo novo do vendor já nasce podado."""
    raw = {"d": {"keep": 1, "campo_novo_do_vendor": "surpresa"}}
    out = apply_raw_reduction(raw, _c([{"path": "d", "keep_only": ["keep"]}]))
    assert out["d"] == {"keep": 1}


# ── drop_nulls ───────────────────────────────────────────────────────────────

def test_drop_nulls_is_recursive():
    raw = {"a": None, "b": {"c": None, "d": 1}, "e": [{"f": None, "g": 2}]}
    out = apply_raw_reduction(raw, _c([{"drop_nulls": True}]))
    assert "a" not in out
    assert out["b"] == {"d": 1}
    assert out["e"] == [{"g": 2}]


def test_drop_nulls_noop_without_nulls():
    assert apply_raw_reduction({"a": 1}, _c([{"drop_nulls": True}])) is None


# ── travessia de listas ──────────────────────────────────────────────────────

def test_list_wildcard_reaches_into_array_items():
    """Antes, blobs dentro de arrays eram INALCANÇÁVEIS — o navegador recusava
    listas. É o caso do defender.incident (alerts[].evidences)."""
    raw = {"alerts": [{"id": 1, "evidences": ["e"] * 10}, {"id": 2, "evidences": ["e"] * 10}]}
    out = apply_raw_reduction(raw, _c([{"path": "alerts[].evidences", "drop": True}]))
    assert all("evidences" not in a for a in out["alerts"])
    assert [a["id"] for a in out["alerts"]] == [1, 2]


def test_list_wildcard_with_max_items():
    raw = {"alerts": [{"tags": [1, 2, 3, 4, 5]}]}
    out = apply_raw_reduction(raw, _c([{"path": "alerts[].tags", "max_items": 2}]))
    assert out["alerts"][0]["tags"] == [1, 2]


def test_list_wildcard_does_not_mutate_original():
    raw = {"alerts": [{"evidences": ["e"]}]}
    apply_raw_reduction(raw, _c([{"path": "alerts[].evidences", "drop": True}]))
    assert raw["alerts"][0]["evidences"] == ["e"]


# ── compilação / validação ───────────────────────────────────────────────────

def test_spec_without_any_op_is_rejected():
    with pytest.raises(MappingDefinitionError, match="ao menos uma op"):
        _c([{"path": "a"}])


def test_drop_combined_with_other_ops_is_rejected():
    """drop remove a chave — combinar com clip seria inalcançável. Erro
    explícito em vez de precedência silenciosa."""
    with pytest.raises(MappingDefinitionError, match="exclusivo"):
        _c([{"path": "a", "drop": True, "max_bytes": 10}])


def test_keep_only_must_be_list_of_strings():
    with pytest.raises(MappingDefinitionError, match="keep_only"):
        _c([{"path": "a", "keep_only": [1, 2]}])


def test_drop_nulls_global_spec_needs_no_path():
    specs = _c([{"drop_nulls": True}])
    assert len(specs) == 1 and specs[0].is_global


def test_legacy_clip_specs_still_compile_and_apply():
    """Retrocompatibilidade: os specs antigos (só max_bytes/max_items)
    continuam válidos e com o mesmo comportamento."""
    raw = {"blob": "x" * 100, "lst": list(range(10))}
    out = apply_raw_reduction(
        raw, _c([{"path": "blob", "max_bytes": 10}, {"path": "lst", "max_items": 3}])
    )
    assert len(out["blob"].encode()) == 10
    assert out["lst"] == [0, 1, 2]


# ── combinação + proveniência ────────────────────────────────────────────────

def test_ops_compose_and_are_all_recorded():
    raw = {
        "rawData": {"raw": "{...}", "keep": "v", "nulo": None},
        "alerts": [{"evidences": ["e"] * 3}],
    }
    out = apply_raw_reduction(
        raw,
        _c([
            {"path": "rawData.raw", "drop": True},
            {"path": "alerts[].evidences", "drop": True},
            {"drop_nulls": True},
        ]),
    )
    assert "raw" not in out["rawData"]
    assert "nulo" not in out["rawData"]
    assert "evidences" not in out["alerts"][0]
    assert len(out["_centralops_reduced"]) == 3
