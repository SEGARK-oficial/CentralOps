"""Preservação de blocos top-level da DSL v2 no normalizador de rules.

REGRESSÃO COBERTA: ``_normalize_rules_to_v2`` reconstruía o dict com apenas
``preprocess`` e ``rules``, descartando silenciosamente ``raw_reduction`` — o
único mecanismo de poda do payload bruto. Como a função roda ao SERVIR a
definição E ao COMMITAR, o efeito era destrutivo: a UI recebia o mapping já sem
o bloco, o operador salvava, e a configuração de redução sumia para sempre.
Foi assim que o ``sophos.detection`` perdeu seus 3 specs em produção.
"""
from __future__ import annotations

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

from backend.app.routers.mappings import _normalize_rules_to_v2

_REDUCTION = [
    {"path": "rawData.raw", "max_bytes": 16384},
    {"path": "rawData.items", "max_bytes": 16384},
]


def test_preserves_raw_reduction_block():
    out = _normalize_rules_to_v2(
        {"preprocess": [], "rules": [{"target": "normalized.x"}], "raw_reduction": _REDUCTION}
    )
    assert out["raw_reduction"] == _REDUCTION


def test_preserves_unknown_future_blocks():
    """Forward-compat: um bloco novo da DSL não pode ser apagado por um
    normalizador que não o conhece."""
    out = _normalize_rules_to_v2(
        {"rules": [], "bloco_futuro": {"a": 1}, "raw_reduction": _REDUCTION}
    )
    assert out["bloco_futuro"] == {"a": 1}
    assert out["raw_reduction"] == _REDUCTION


def test_still_normalizes_preprocess_and_rules_to_lists():
    out = _normalize_rules_to_v2({"rules": None, "preprocess": None})
    assert out["preprocess"] == [] and out["rules"] == []


def test_v1_list_shape_still_converted():
    out = _normalize_rules_to_v2([{"target": "normalized.a"}])
    assert out == {"preprocess": [], "rules": [{"target": "normalized.a"}]}


def test_garbage_input_yields_empty_v2_shape():
    assert _normalize_rules_to_v2("nao é um mapping") == {"preprocess": [], "rules": []}


def test_round_trip_does_not_lose_reduction():
    """O ciclo servir → editar → commitar (dois passes pelo normalizador) tem
    que preservar o bloco. Era exatamente aqui que a config morria."""
    stored = {"preprocess": [], "rules": [{"target": "normalized.a"}], "raw_reduction": _REDUCTION}

    served = _normalize_rules_to_v2(stored)            # GET /mappings/{id}
    edited = dict(served)
    edited["rules"] = [{"target": "normalized.b"}]     # operador edita na UI
    committed = _normalize_rules_to_v2(edited)         # POST nova versão

    assert committed["raw_reduction"] == _REDUCTION
    assert committed["rules"] == [{"target": "normalized.b"}]
