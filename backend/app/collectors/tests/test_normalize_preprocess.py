"""Testes unitários do módulo preprocess (Fase 2.1a).

Cobre:
- json_parse: happy path, None passthrough, invalid JSON (tolerant + strict),
  tipo não-string, limite de bytes (protecao DoS sempre ativa).
- register_preprocess_op: registro e colisão.
- apply_preprocess_op: despacho e operador desconhecido.
- CompiledPreprocessOp: dataclass frozen e campos corretos.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from backend.app.collectors.normalize.preprocess import (
    PREPROCESS_OPS,
    PREPROCESS_OP_DESCRIPTORS,
    CompiledPreprocessOp,
    apply_preprocess_op,
    json_parse,
)
from backend.app.collectors.normalize.registry import OperatorError


# ── json_parse ─────────────────────────────────────────────────────────


def test_json_parse_happy_path() -> None:
    """String JSON valida retorna objeto Python."""
    payload = '{"action": "queuedForDelivery", "score": 0.1}'
    result = json_parse(payload)
    assert result == {"action": "queuedForDelivery", "score": 0.1}


def test_json_parse_happy_path_array() -> None:
    """Array JSON valido."""
    result = json_parse('[1, 2, 3]')
    assert result == [1, 2, 3]


def test_json_parse_none_passthrough() -> None:
    """None retorna None independente de tolerant."""
    assert json_parse(None) is None
    assert json_parse(None, tolerant=True) is None
    assert json_parse(None, tolerant=False) is None


def test_json_parse_tolerant_invalid_returns_none() -> None:
    """JSON invalido com tolerant=True retorna None."""
    result = json_parse("NOT_JSON", tolerant=True)
    assert result is None


def test_json_parse_strict_invalid_raises() -> None:
    """JSON invalido com tolerant=False (default) levanta OperatorError."""
    with pytest.raises(OperatorError, match="JSON"):
        json_parse("NOT_JSON")


def test_json_parse_strict_invalid_raises_explicit_false() -> None:
    with pytest.raises(OperatorError, match="JSON"):
        json_parse("{broken", tolerant=False)


def test_json_parse_oversized_raises_even_when_tolerant() -> None:
    """Payload acima de max_bytes levanta OperatorError SEMPRE.

    Protecao DoS nao pode ser suprimida por tolerant=True.
    Este e o teste de segurança exigido pelo contrato da Fase 2.1a.
    """
    big = "x" * 10
    # max_bytes=5 => 10 bytes > 5 bytes, deve levantar mesmo com tolerant=True
    with pytest.raises(OperatorError, match="excede"):
        json_parse(f'"{big}"', tolerant=True, max_bytes=5)

    # Confirma que o mesmo acontece com tolerant=False
    with pytest.raises(OperatorError, match="excede"):
        json_parse(f'"{big}"', tolerant=False, max_bytes=5)


def test_json_parse_oversized_threshold() -> None:
    """Payload exatamente no limite NAO levanta."""
    payload = '"hello"'  # 7 bytes UTF-8
    # max_bytes=7 => deve passar
    result = json_parse(payload, max_bytes=7)
    assert result == "hello"
    # max_bytes=6 => deve falhar
    with pytest.raises(OperatorError, match="excede"):
        json_parse(payload, max_bytes=6)


def test_json_parse_non_string_strict_raises() -> None:
    """Valor nao-string com tolerant=False levanta OperatorError."""
    with pytest.raises(OperatorError, match="espera str"):
        json_parse(42)

    with pytest.raises(OperatorError, match="espera str"):
        json_parse({"already": "dict"})


def test_json_parse_non_string_tolerant_returns_none() -> None:
    """Valor nao-string com tolerant=True retorna None."""
    assert json_parse(42, tolerant=True) is None
    assert json_parse([], tolerant=True) is None
    assert json_parse({"already": "dict"}, tolerant=True) is None


def test_json_parse_empty_string_invalid_json() -> None:
    """String vazia e JSON invalido."""
    assert json_parse("", tolerant=True) is None
    with pytest.raises(OperatorError):
        json_parse("", tolerant=False)


def test_json_parse_unicode_payload() -> None:
    """Payload UTF-8 multi-byte contado corretamente."""
    # "café" = 5 chars mas 6 bytes UTF-8 (e com aspas = 8 bytes)
    payload = '"café"'
    byte_len = len(payload.encode("utf-8"))
    # dentro do limite
    result = json_parse(payload, max_bytes=byte_len)
    assert result == "café"
    # um byte abaixo do limite
    with pytest.raises(OperatorError, match="excede"):
        json_parse(payload, max_bytes=byte_len - 1)


def test_json_parse_uses_default_max_bytes_from_module() -> None:
    """Sem max_bytes explicito, usa o valor do ambiente (1 MiB por padrao)."""
    # Um payload pequeno deve passar com o default
    result = json_parse('{"ok": true}')
    assert result == {"ok": True}


# ── Registro ──────────────────────────────────────────────────────────


def test_preprocess_ops_has_json_parse() -> None:
    """json_parse deve estar registrado em PREPROCESS_OPS."""
    assert "json_parse" in PREPROCESS_OPS
    assert callable(PREPROCESS_OPS["json_parse"])


def test_preprocess_op_descriptors_has_json_parse() -> None:
    """json_parse deve ter descriptor com description e signature."""
    assert "json_parse" in PREPROCESS_OP_DESCRIPTORS
    desc = PREPROCESS_OP_DESCRIPTORS["json_parse"]
    assert "description" in desc
    assert "signature" in desc


def test_register_duplicate_raises() -> None:
    """Registrar mesmo nome duas vezes levanta KeyError."""
    from backend.app.collectors.normalize.preprocess import register_preprocess_op

    with pytest.raises(KeyError, match="já registrado"):
        @register_preprocess_op("json_parse", description="dup", signature="X")
        def _dup(value: Any) -> Any:  # noqa: ANN401
            return value


# ── apply_preprocess_op ───────────────────────────────────────────────


def test_apply_preprocess_op_json_parse() -> None:
    """apply_preprocess_op despacha para json_parse corretamente."""
    result = apply_preprocess_op('{"x": 1}', "json_parse", tolerant=False)
    assert result == {"x": 1}


def test_apply_preprocess_op_unknown_raises() -> None:
    """Operador desconhecido levanta OperatorError."""
    with pytest.raises(OperatorError, match="desconhecido"):
        apply_preprocess_op("val", "not_a_real_op")


# ── CompiledPreprocessOp ──────────────────────────────────────────────


def test_compiled_preprocess_op_is_frozen() -> None:
    """CompiledPreprocessOp e frozen dataclass."""
    import jmespath

    op = CompiledPreprocessOp(
        op="json_parse",
        compiled_source=jmespath.compile("processedData"),
        source_str="processedData",
        target="_processed",
        tolerant=True,
    )
    with pytest.raises((AttributeError, TypeError)):
        op.op = "other"  # type: ignore[misc]


def test_compiled_preprocess_op_fields() -> None:
    """Todos os campos de CompiledPreprocessOp funcionam corretamente."""
    import jmespath

    expr = jmespath.compile("rawData.raw")
    op = CompiledPreprocessOp(
        op="json_parse",
        compiled_source=expr,
        source_str="rawData.raw",
        target="_raw_parsed",
        tolerant=False,
    )
    assert op.op == "json_parse"
    assert op.source_str == "rawData.raw"
    assert op.target == "_raw_parsed"
    assert op.tolerant is False
    # compiled_source deve pesquisar corretamente
    sample = {"rawData": {"raw": '{"x": 99}'}}
    assert op.compiled_source.search(sample) == '{"x": 99}'
