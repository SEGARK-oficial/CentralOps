"""Commit-time OCSF gate helpers in routers/mappings.

Pure-function coverage of the shift-left analysis (emitted class_uid extraction +
dry-run output validation + declared-vs-emitted cross-check + blocking flag).
Imports use ``backend.app.*`` (compiled .so dual-root gotcha).
"""

from __future__ import annotations

from backend.app.routers import mappings as M


def _v2(class_uid: int = 3002) -> dict:
    return {
        "preprocess": [],
        "rules": [
            {"target": "normalized.class_uid", "const": class_uid},
            {"target": "normalized.time", "source": "t"},
        ],
    }


def _out(**over) -> dict:
    base = {
        "class_uid": 3002, "category_uid": 3, "activity_id": 1,
        "type_uid": 300201, "severity_id": 1, "user": {"name": "x"},
    }
    base.update(over)
    return {"normalized": base}


def _v2_por_evento() -> dict:
    """Mapping que decide a classe POR EVENTO — a forma do sophos.siem_event."""
    return {
        "rules": [
            {
                "target": "normalized.class_uid",
                "source": "group",
                "value_map": {"PUA": 2004, "MALWARE": 2004},
                "default": 0,
            }
        ]
    }


def test_emitted_class_uid_v1_and_v2() -> None:
    assert M._emitted_class_uids(_v2(4001)) == {4001}
    # v1 (bare list)
    assert M._emitted_class_uids([{"target": "normalized.class_uid", "const": 2004}]) == {2004}
    # sem regra de class_uid → conjunto vazio (indeterminável)
    assert M._emitted_class_uids({"rules": [{"target": "normalized.time", "source": "t"}]}) == set()


def test_mapping_por_evento_emite_um_CONJUNTO_de_classes() -> None:
    """Antes isto devolvia ``None`` e desligava a checagem de mismatch inteira.

    O efeito era perverso: mapping de classe única era conferido contra a classe
    declarada, e feed heterogêneo — justamente onde errar a classe custa caro —
    passava sem conferência nenhuma.
    """
    assert M._emitted_class_uids(_v2_por_evento()) == {0, 2004}


def test_declarada_ENTRE_as_emitidas_nao_e_mismatch() -> None:
    """Um mapping por evento não tem "a" classe. A pergunta certa é se a classe
    declarada na definição está entre as que ele emite."""
    stats = M._ocsf_validate_commit(_v2_por_evento(), [], declared_class_uid=0)
    assert stats["class_uid_mismatch"] is False
    assert stats["class_uid_emitted"] == [0, 2004]


def test_declarada_pode_ser_QUALQUER_uma_das_emitidas() -> None:
    """Pertinência ao conjunto, não igualdade a um elemento escolhido.

    `sophos.siem_event` declara 0 e emite {0, 2004}; um mapping de finding com
    ramo de telemetria declara 2004 e emite o MESMO conjunto. Uma implementação
    que compare com "a primeira" classe aceita o primeiro e bloqueia o segundo —
    e passa nos dois testes acima sem nenhum sinal.
    """
    stats = M._ocsf_validate_commit(_v2_por_evento(), [], declared_class_uid=2004)
    assert stats["class_uid_mismatch"] is False
    assert stats["blocking"] is False


def test_declarada_FORA_das_emitidas_e_mismatch() -> None:
    """O caso que a versão anterior não conseguia pegar de jeito nenhum:
    declarar 2005 num mapping que só emite 0 e 2004."""
    stats = M._ocsf_validate_commit(_v2_por_evento(), [], declared_class_uid=2005)
    assert stats["class_uid_mismatch"] is True
    assert stats["blocking"] is True


def test_gate_valid_matching_declared_is_not_blocking() -> None:
    stats = M._ocsf_validate_commit(_v2(3002), [_out()], declared_class_uid=3002)
    assert stats["checked"] == 1 and stats["valid"] == 1
    assert stats["invalid_by_reason"] == {}
    assert stats["class_uid_mismatch"] is False
    assert stats["blocking"] is False


def test_gate_declared_vs_emitted_mismatch_blocks() -> None:
    stats = M._ocsf_validate_commit(_v2(3002), [_out()], declared_class_uid=4001)
    assert stats["class_uid_emitted"] == 3002 and stats["class_uid_declared"] == 4001
    assert stats["class_uid_mismatch"] is True and stats["blocking"] is True


def test_gate_invalid_output_blocks() -> None:
    stats = M._ocsf_validate_commit(_v2(3002), [_out(severity_id=7)], declared_class_uid=3002)
    assert stats["invalid_by_reason"] == {"bad_severity_id": 1}
    assert stats["blocking"] is True


def test_gate_out_of_scope_is_not_blocking() -> None:
    # class 1001 is a valid OCSF class we don't vendor → graceful, not a hard defect
    oos = {"normalized": {"class_uid": 1001, "category_uid": 1, "activity_id": 1,
                          "type_uid": 100101, "severity_id": 1}}
    stats = M._ocsf_validate_commit({"rules": []}, [oos], declared_class_uid=None)
    assert stats["invalid_by_reason"] == {"out_of_scope": 1}
    assert stats["blocking"] is False  # out_of_scope alone must not block a commit
