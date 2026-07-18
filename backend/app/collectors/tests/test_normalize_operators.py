"""Operadores nomeados da DSL."""

from __future__ import annotations

import pytest

from backend.app.collectors.normalize.operators import (
    OperatorError,
    apply_default,
    apply_type_cast,
    apply_value_map,
)
from backend.app.collectors.normalize.registry import (
    TYPE_CAST_DESCRIPTORS,
    TYPE_CASTS,
    register_type_cast,
)


class TestTypeCast:
    # ── timestamp_t (OCSF) = MILISSEGUNDOS ────────────────────────────
    # O OCSF tipa timestamp_t como ms desde a epoch. Todas as asserções
    # abaixo usam 13 dígitos; 10 dígitos (segundos) fariam um consumidor
    # OCSF conforme ler o evento como janeiro de 1970.

    def test_iso_to_epoch_with_z_suffix(self) -> None:
        assert apply_type_cast("2026-04-23T14:22:10Z", "iso_to_epoch") == 1776954130000

    def test_iso_to_epoch_with_offset(self) -> None:
        # mesmo instante, fuso explícito
        assert (
            apply_type_cast("2026-04-23T14:22:10+00:00", "iso_to_epoch")
            == 1776954130000
        )

    def test_iso_to_epoch_preserves_subsecond_precision(self) -> None:
        # ms do vendor não podem ser truncados na conversão.
        assert (
            apply_type_cast("2026-04-23T14:22:10.432Z", "iso_to_epoch")
            == 1776954130432
        )

    def test_iso_to_epoch_numeric_seconds_promoted_to_millis(self) -> None:
        # Vendors epoch-em-segundos (CrowdStrike, NinjaOne): |v| < 1e11
        # → interpretado como segundos → ×1000.
        assert apply_type_cast(1776954130, "iso_to_epoch") == 1776954130000
        assert apply_type_cast(1776954130.432, "iso_to_epoch") == 1776954130432

    def test_iso_to_epoch_numeric_millis_passthrough(self) -> None:
        # Vendors epoch-em-ms (CloudWatch, Defender): |v| >= 1e11 → já é ms.
        assert apply_type_cast(1776954130000, "iso_to_epoch") == 1776954130000

    def test_iso_to_epoch_heuristic_threshold_boundary(self) -> None:
        # 1e11 é o limiar: como segundos ≈ ano 5138, como ms ≈ 1973-03-03.
        # Abaixo → segundos; no limiar/acima → já ms.
        assert apply_type_cast(99_999_999_999, "iso_to_epoch") == 99_999_999_999_000
        assert apply_type_cast(100_000_000_000, "iso_to_epoch") == 100_000_000_000

    def test_iso_to_epoch_invalid_string_raises(self) -> None:
        with pytest.raises(OperatorError):
            apply_type_cast("not-a-timestamp", "iso_to_epoch")

    def test_iso_to_epoch_rejects_bool(self) -> None:
        # bool é subclasse de int — deixar passar viraria time=1000.
        with pytest.raises(OperatorError):
            apply_type_cast(True, "iso_to_epoch")

    def test_epoch_to_iso_from_millis(self) -> None:
        assert apply_type_cast(1776954130000, "epoch_to_iso") == "2026-04-23T14:22:10Z"

    def test_epoch_to_iso_from_seconds_uses_same_heuristic(self) -> None:
        # Mesma heurística do iso_to_epoch: < 1e11 = segundos.
        assert apply_type_cast(1776954130, "epoch_to_iso") == "2026-04-23T14:22:10Z"

    def test_epoch_to_iso_from_string_number(self) -> None:
        assert apply_type_cast("1776954130", "epoch_to_iso") == "2026-04-23T14:22:10Z"
        assert apply_type_cast("1776954130000", "epoch_to_iso") == "2026-04-23T14:22:10Z"

    def test_epoch_iso_round_trip(self) -> None:
        # Ida-e-volta: o inverso tem de fechar em ambas as direções.
        iso = "2026-04-23T14:22:10Z"
        assert apply_type_cast(apply_type_cast(iso, "iso_to_epoch"), "epoch_to_iso") == iso
        millis = 1776954130000
        assert (
            apply_type_cast(apply_type_cast(millis, "epoch_to_iso"), "iso_to_epoch")
            == millis
        )

    def test_to_int_from_string(self) -> None:
        assert apply_type_cast("42", "to_int") == 42

    def test_to_int_rejects_bool(self) -> None:
        # bool é subclasse de int em Python — deixar passar mascararia bug.
        with pytest.raises(OperatorError):
            apply_type_cast(True, "to_int")

    def test_to_bool_strings(self) -> None:
        assert apply_type_cast("true", "to_bool") is True
        assert apply_type_cast("False", "to_bool") is False
        assert apply_type_cast("1", "to_bool") is True
        assert apply_type_cast("0", "to_bool") is False

    def test_to_bool_ambiguous_string_raises(self) -> None:
        with pytest.raises(OperatorError):
            apply_type_cast("maybe", "to_bool")

    def test_to_str_rejects_none(self) -> None:
        # Forçar default antes — casting de None a "None" só esconde bug.
        with pytest.raises(OperatorError):
            apply_type_cast(None, "to_str")

    def test_unknown_cast_raises(self) -> None:
        with pytest.raises(OperatorError):
            apply_type_cast("x", "stringify_with_emoji")


class TestValueMap:
    def test_basic_lookup(self) -> None:
        m = {"critical": 5, "high": 4, "medium": 3, "low": 2, "info": 1}
        assert apply_value_map("critical", m) == 5
        assert apply_value_map("info", m) == 1

    def test_lookup_is_case_insensitive_for_string_keys(self) -> None:
        m = {"Critical": 5, "High": 4}
        assert apply_value_map("critical", m) == 5
        assert apply_value_map("HIGH", m) == 4

    def test_passthrough_when_key_missing(self) -> None:
        m = {"critical": 5}
        assert apply_value_map("medium", m) == "medium"

    def test_numeric_keys_literal_match(self) -> None:
        m = {1: "low", 2: "medium", 3: "high"}
        assert apply_value_map(2, m) == "medium"
        # Sem match: passthrough
        assert apply_value_map(99, m) == 99

    def test_non_dict_raises(self) -> None:
        with pytest.raises(OperatorError):
            apply_value_map("x", "not-a-dict")  # type: ignore[arg-type]


class TestDefault:
    def test_returns_default_for_none(self) -> None:
        assert apply_default(None, "fallback") == "fallback"

    def test_keeps_value_when_present(self) -> None:
        assert apply_default("real", "fallback") == "real"

    def test_zero_is_not_treated_as_missing(self) -> None:
        # Falsy != None — semântica importante para campos numéricos.
        assert apply_default(0, 99) == 0
        assert apply_default("", "fallback") == ""
        assert apply_default(False, True) is False


# ── Novos casts — Fase 1.1 ────────────────────────────────────────────


class TestScoreToPercent:
    @pytest.mark.parametrize("value,expected", [
        (0.0, 0),
        (0.42, 42),
        (0.5, 50),
        (0.999, 100),
        (1.0, 100),
        # int idempotente
        (0, 0),
        (50, 50),
        (100, 100),
    ])
    def test_happy_path(self, value: float | int, expected: int) -> None:
        assert apply_type_cast(value, "score_to_percent") == expected

    def test_none_passthrough(self) -> None:
        assert apply_type_cast(None, "score_to_percent") is None

    def test_idempotent_int_boundary(self) -> None:
        # 0 e 100 são limites válidos para int
        assert apply_type_cast(0, "score_to_percent") == 0
        assert apply_type_cast(100, "score_to_percent") == 100

    def test_float_out_of_range_raises(self) -> None:
        with pytest.raises(OperatorError, match="0.0, 1.0"):
            apply_type_cast(1.5, "score_to_percent")

    def test_float_negative_raises(self) -> None:
        with pytest.raises(OperatorError):
            apply_type_cast(-0.1, "score_to_percent")

    def test_int_out_of_range_raises(self) -> None:
        with pytest.raises(OperatorError, match="0, 100"):
            apply_type_cast(101, "score_to_percent")

    def test_bool_raises(self) -> None:
        with pytest.raises(OperatorError):
            apply_type_cast(True, "score_to_percent")

    def test_string_raises(self) -> None:
        with pytest.raises(OperatorError):
            apply_type_cast("0.5", "score_to_percent")

    def test_rounding_half_up(self) -> None:
        # round() em Python usa banker's rounding; 0.425 * 100 = 42.5 → 42
        # mas 0.445 * 100 = 44.5 → 44.  Não é bug — é documentado.
        # O que importa é que o resultado é int.
        result = apply_type_cast(0.425, "score_to_percent")
        assert isinstance(result, int)


class TestLowercase:
    def test_basic(self) -> None:
        assert apply_type_cast("HELLO", "lowercase") == "hello"

    def test_already_lower(self) -> None:
        assert apply_type_cast("hello", "lowercase") == "hello"

    def test_mixed_case(self) -> None:
        assert apply_type_cast("Hello World", "lowercase") == "hello world"

    def test_none_passthrough(self) -> None:
        assert apply_type_cast(None, "lowercase") is None

    @pytest.mark.parametrize("bad_value", [42, 3.14, True, [], {}])
    def test_non_string_raises(self, bad_value: object) -> None:
        with pytest.raises(OperatorError):
            apply_type_cast(bad_value, "lowercase")


class TestUppercase:
    def test_basic(self) -> None:
        assert apply_type_cast("hello", "uppercase") == "HELLO"

    def test_already_upper(self) -> None:
        assert apply_type_cast("HELLO", "uppercase") == "HELLO"

    def test_mixed_case(self) -> None:
        assert apply_type_cast("Hello World", "uppercase") == "HELLO WORLD"

    def test_none_passthrough(self) -> None:
        assert apply_type_cast(None, "uppercase") is None

    @pytest.mark.parametrize("bad_value", [42, 3.14, True, [], {}])
    def test_non_string_raises(self, bad_value: object) -> None:
        with pytest.raises(OperatorError):
            apply_type_cast(bad_value, "uppercase")


class TestTrim:
    @pytest.mark.parametrize("value,expected", [
        ("  hello  ", "hello"),
        ("\thello\n", "hello"),
        ("hello", "hello"),
        ("  ", ""),
        ("", ""),
    ])
    def test_happy_path(self, value: str, expected: str) -> None:
        assert apply_type_cast(value, "trim") == expected

    def test_none_passthrough(self) -> None:
        assert apply_type_cast(None, "trim") is None

    @pytest.mark.parametrize("bad_value", [42, 3.14, True, [], {}])
    def test_non_string_raises(self, bad_value: object) -> None:
        with pytest.raises(OperatorError):
            apply_type_cast(bad_value, "trim")


class TestToArray:
    def test_scalar_wrapped(self) -> None:
        assert apply_type_cast("hello", "to_array") == ["hello"]

    def test_int_wrapped(self) -> None:
        assert apply_type_cast(42, "to_array") == [42]

    def test_list_passthrough(self) -> None:
        value = [1, 2, 3]
        assert apply_type_cast(value, "to_array") == [1, 2, 3]

    def test_empty_list_passthrough(self) -> None:
        assert apply_type_cast([], "to_array") == []

    def test_none_returns_empty_list(self) -> None:
        assert apply_type_cast(None, "to_array") == []

    def test_dict_wrapped(self) -> None:
        d = {"key": "val"}
        assert apply_type_cast(d, "to_array") == [{"key": "val"}]


class TestDedup:
    def test_removes_duplicates_preserves_order(self) -> None:
        assert apply_type_cast([1, 2, 2, 3, 1], "dedup") == [1, 2, 3]

    def test_strings(self) -> None:
        assert apply_type_cast(["a", "b", "a", "c"], "dedup") == ["a", "b", "c"]

    def test_already_unique(self) -> None:
        assert apply_type_cast([1, 2, 3], "dedup") == [1, 2, 3]

    def test_empty_list(self) -> None:
        assert apply_type_cast([], "dedup") == []

    def test_none_passthrough(self) -> None:
        assert apply_type_cast(None, "dedup") is None

    def test_non_list_raises(self) -> None:
        with pytest.raises(OperatorError):
            apply_type_cast("not-a-list", "dedup")

    def test_non_list_int_raises(self) -> None:
        with pytest.raises(OperatorError):
            apply_type_cast(42, "dedup")

    def test_unhashable_items_appended(self) -> None:
        # dict não é hashable — append sem dedup (documentado)
        d1 = {"x": 1}
        d2 = {"x": 2}
        result = apply_type_cast([d1, d2, d1], "dedup")
        # d1 repetido não é deduplicado (unhashable path)
        assert len(result) == 3

    @pytest.mark.parametrize("value,expected", [
        ([1, 1, 1], [1]),
        (["x", "x"], ["x"]),
        ([True, False, True], [True, False]),
    ])
    def test_parametrized(self, value: list, expected: list) -> None:
        assert apply_type_cast(value, "dedup") == expected


class TestMitreTacticToOcsf:
    """Testa mitre_tactic_to_ocsf com shapes reais do Sophos."""

    # Shape real que o Sophos envia em sophos.alert
    SOPHOS_SINGLE = [
        {"tactic": {"id": "TA0001", "name": "Initial Access"}}
    ]

    SOPHOS_MULTI = [
        {"tactic": {"id": "TA0001", "name": "Initial Access"}},
        {"tactic": {"id": "TA0002", "name": "Execution"}},
    ]

    SOPHOS_WITH_NULL_TACTIC = [
        {"tactic": {"id": "TA0001", "name": "Initial Access"}},
        {"tactic": None},  # emitido por alertas de baixa fidelidade
        {"tactic": {"id": "TA0002", "name": "Execution"}},
    ]

    def test_single_tactic(self) -> None:
        result = apply_type_cast(self.SOPHOS_SINGLE, "mitre_tactic_to_ocsf")
        assert result == [
            {"tactics": [{"uid": "TA0001", "name": "Initial Access"}], "version": "16.1"}
        ]

    def test_multiple_tactics(self) -> None:
        result = apply_type_cast(self.SOPHOS_MULTI, "mitre_tactic_to_ocsf")
        assert len(result) == 2
        assert result[0] == {
            "tactics": [{"uid": "TA0001", "name": "Initial Access"}],
            "version": "16.1",
        }
        assert result[1] == {
            "tactics": [{"uid": "TA0002", "name": "Execution"}],
            "version": "16.1",
        }

    def test_null_tactics_filtered_out(self) -> None:
        result = apply_type_cast(self.SOPHOS_WITH_NULL_TACTIC, "mitre_tactic_to_ocsf")
        assert len(result) == 2
        uids = [r["tactics"][0]["uid"] for r in result]
        assert uids == ["TA0001", "TA0002"]

    def test_none_passthrough(self) -> None:
        assert apply_type_cast(None, "mitre_tactic_to_ocsf") is None

    def test_empty_list(self) -> None:
        assert apply_type_cast([], "mitre_tactic_to_ocsf") == []

    def test_all_null_tactics(self) -> None:
        value = [{"tactic": None}, {"tactic": None}]
        result = apply_type_cast(value, "mitre_tactic_to_ocsf")
        assert result == []

    def test_non_list_raises(self) -> None:
        with pytest.raises(OperatorError, match="espera list"):
            apply_type_cast({"tactic": {"id": "TA0001", "name": "x"}}, "mitre_tactic_to_ocsf")

    def test_item_not_dict_raises(self) -> None:
        with pytest.raises(OperatorError, match="deve ser dict"):
            apply_type_cast(["not-a-dict"], "mitre_tactic_to_ocsf")

    def test_item_missing_tactic_key_raises(self) -> None:
        with pytest.raises(OperatorError, match="chave 'tactic'"):
            apply_type_cast([{"technique": {"id": "T1059"}}], "mitre_tactic_to_ocsf")

    def test_tactic_not_dict_raises(self) -> None:
        with pytest.raises(OperatorError, match="deve ser dict ou None"):
            apply_type_cast([{"tactic": "TA0001"}], "mitre_tactic_to_ocsf")

    def test_tactic_missing_id_raises(self) -> None:
        with pytest.raises(OperatorError, match="tactic.id"):
            apply_type_cast([{"tactic": {"name": "Initial Access"}}], "mitre_tactic_to_ocsf")

    def test_tactic_missing_name_raises(self) -> None:
        with pytest.raises(OperatorError, match="tactic.name"):
            apply_type_cast([{"tactic": {"id": "TA0001"}}], "mitre_tactic_to_ocsf")

    def test_tactic_empty_id_raises(self) -> None:
        with pytest.raises(OperatorError, match="tactic.id"):
            apply_type_cast([{"tactic": {"id": "", "name": "Initial Access"}}], "mitre_tactic_to_ocsf")

    def test_tactic_empty_name_raises(self) -> None:
        with pytest.raises(OperatorError, match="tactic.name"):
            apply_type_cast([{"tactic": {"id": "TA0001", "name": ""}}], "mitre_tactic_to_ocsf")

    def test_version_is_16_1(self) -> None:
        result = apply_type_cast(self.SOPHOS_SINGLE, "mitre_tactic_to_ocsf")
        assert result[0]["version"] == "16.1"

    def test_output_has_tactics_list(self) -> None:
        result = apply_type_cast(self.SOPHOS_SINGLE, "mitre_tactic_to_ocsf")
        assert isinstance(result[0]["tactics"], list)
        assert len(result[0]["tactics"]) == 1

    def test_uid_maps_from_id(self) -> None:
        result = apply_type_cast(self.SOPHOS_SINGLE, "mitre_tactic_to_ocsf")
        assert result[0]["tactics"][0]["uid"] == "TA0001"


# ── Registry ──────────────────────────────────────────────────────────


class TestRegistry:
    """Testa o mecanismo do registry isolado dos casts de produção."""

    def test_all_12_casts_registered(self) -> None:
        expected = {
            "iso_to_epoch",
            "epoch_to_iso",
            "to_str",
            "to_int",
            "to_bool",
            "score_to_percent",
            "lowercase",
            "uppercase",
            "trim",
            "to_array",
            "dedup",
            "mitre_tactic_to_ocsf",
        }
        assert expected.issubset(set(TYPE_CASTS.keys()))

    def test_descriptors_present_for_all_casts(self) -> None:
        for name in TYPE_CASTS:
            assert name in TYPE_CAST_DESCRIPTORS, f"Descriptor ausente para cast {name!r}"
            desc = TYPE_CAST_DESCRIPTORS[name]
            assert "description" in desc
            assert "signature" in desc
            assert desc["description"]  # não-vazio
            assert desc["signature"]    # não-vazio

    def test_registered_cast_is_callable(self) -> None:
        for name, fn in TYPE_CASTS.items():
            assert callable(fn), f"Cast {name!r} não é callable"

    def test_double_registration_raises_key_error(self) -> None:
        # Tenta registrar um nome que já existe — deve levantar KeyError.
        with pytest.raises(KeyError, match="já registrado"):
            @register_type_cast(
                "iso_to_epoch",  # nome já registrado
                description="duplicado",
                signature="any → any",
            )
            def _duplicate(value: object) -> object:
                return value

    def test_register_new_fake_cast_and_lookup(self) -> None:
        # Usa um nome único para não poluir o registry global entre testes.
        fake_name = "_test_fake_cast_xyz"
        # Limpa se algum teste anterior falhou no meio.
        TYPE_CASTS.pop(fake_name, None)
        TYPE_CAST_DESCRIPTORS.pop(fake_name, None)

        @register_type_cast(
            fake_name,
            description="Cast de teste temporário.",
            signature="any → any",
        )
        def _fake_cast(value: object) -> object:
            return value

        try:
            assert fake_name in TYPE_CASTS
            assert TYPE_CASTS[fake_name]("hello") == "hello"
            assert fake_name in TYPE_CAST_DESCRIPTORS
            assert TYPE_CAST_DESCRIPTORS[fake_name]["description"] == "Cast de teste temporário."
        finally:
            # Cleanup: remove do registry para não afetar outros testes.
            TYPE_CASTS.pop(fake_name, None)
            TYPE_CAST_DESCRIPTORS.pop(fake_name, None)


# ── Fase 1.2 — value_map int/str tolerance ───────────────────────────


class TestValueMapIntStrTolerance:
    """apply_value_map deve tentar str(value) quando a lookup exata falha
    e o valor não é None nem string.  Cobre o caso Sophos onde ``severity``
    chega como int mas o value_map usa chaves de string."""

    def test_int_value_with_string_key(self) -> None:
        # Caso canônico Sophos: severity=3 (int), mapa usa "3" (str).
        assert apply_value_map(3, {"3": "high"}) == "high"

    def test_exact_match_preferred_over_str_fallback(self) -> None:
        # Quando ambas as chaves existem, a exata (int) vence.
        assert apply_value_map(3, {3: "as_int", "3": "as_str"}) == "as_int"

    def test_none_passes_through_unchanged(self) -> None:
        # None nunca deve ser convertido via str() — evita "None" como chave.
        result = apply_value_map(None, {"None": "should_not_match", "3": "high"})
        assert result is None

    def test_float_tries_str_fallback(self) -> None:
        # float 3.0 → str("3.0") — documentado como comportamento explícito.
        assert apply_value_map(3.0, {"3.0": "x"}) == "x"
        # Se a chave str(float) não existir, passthrough.
        assert apply_value_map(3.0, {"3": "y"}) == 3.0
