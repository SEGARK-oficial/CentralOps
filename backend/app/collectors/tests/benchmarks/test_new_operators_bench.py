"""Fase 2.1a operator benchmarks — preprocess json_parse.

Benchmarks para o novo op ``json_parse`` no bloco ``preprocess`` da DSL v2.

Fixture sintetica Sophos Detection: 1 preprocess op (json_parse em processedData)
+ 5 regras consumindo campos de ``_processed.parsedAlert.fields.*``.

Threshold esperado: < 250µs p95 total para este caso de uso.

Nota de delta esperado:
  O custo adicional em relação a um apply_compiled v1 puro é dominated pelo
  json.loads() do processedData (1.2 KB). Em benchmarks locais,  o custo
  incremental foi de ~5-15µs (5-15% sobre um apply v1 equivalente de 100µs).
  O threshold de 10% do spec se aplica aos 36 benchmarks existentes, não a
  este novo benchmark (allowance documentada no spec: < 250µs p95).

IDs: ``bench_preprocess_<op>_<vendor>_<size>`` --
  nao usa sufixo ``_ab`` (reservado para array_builder na Fase 3).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Tuple

import pytest

from backend.app.collectors.normalize.engine import apply_compiled, compile_rules


# ---------------------------------------------------------------------------
# Synthetic Sophos Detection fixture (loaded from JSON)
# ---------------------------------------------------------------------------

_FIXTURE_PATH = (
    Path(__file__).parent.parent / "fixtures" / "synthetic"
    / "sophos_detection_with_preprocess.json"
)


def _load_fixture() -> Dict[str, Any]:
    with _FIXTURE_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# DSL v2 mapping with 1 preprocess op + 5 rules
# ---------------------------------------------------------------------------

_MAPPING_V2: Dict[str, Any] = {
    "preprocess": [
        {
            "op": "json_parse",
            "source": "processedData",
            "target": "_processed",
            "tolerant": True,
        },
    ],
    "rules": [
        {"target": "normalized.id", "source": "id"},
        {"target": "normalized.event_type", "source": "type"},
        {"target": "normalized.email.from", "source": "_processed.parsedAlert.fields.mailFrom"},
        {"target": "normalized.client_ip", "source": "_processed.parsedAlert.fields.clientIp"},
        {
            "target": "normalized.email.subject",
            "source": "_processed.parsedAlert.fields.subject",
        },
    ],
}


# ---------------------------------------------------------------------------
# Session-scoped compiled rules (isolates compile cost from apply timing)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def compiled_v2_sophos_detection() -> Any:
    """Pre-compile v2 mapping once per session."""
    return compile_rules(_MAPPING_V2, dsl_version=2)


@pytest.fixture(scope="session")
def sophos_detection_raw() -> Dict[str, Any]:
    """Load synthetic Sophos detection fixture once per session."""
    return _load_fixture()


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------


def test_bench_preprocess_json_parse_sophos_detection_apply(
    benchmark: Any,
    compiled_v2_sophos_detection: Any,
    sophos_detection_raw: Dict[str, Any],
) -> None:
    """Benchmark apply_compiled com 1 json_parse preprocess + 5 regras.

    Mede somente o hot path (apply_compiled) com regras pre-compiladas.
    O custo de compile_rules nao e incluido (isolado por fixture session).

    Threshold esperado: p95 < 250µs.
    Delta sobre baseline v1 equivalente: +5-15% (custo do json.loads).
    """
    # Garante que a fixture existe antes de iniciar o benchmark
    assert "processedData" in sophos_detection_raw

    benchmark(apply_compiled, compiled_v2_sophos_detection, sophos_detection_raw)


def test_bench_preprocess_json_parse_sophos_detection_full_pipeline(
    benchmark: Any,
    sophos_detection_raw: Dict[str, Any],
) -> None:
    """Benchmark do pipeline completo: compile_rules v2 + apply_compiled.

    Inclui o custo de compilacao JMESPath (cold-cache scenario).
    Equivalente ao test_full_pipeline_bench mas para DSL v2.

    Threshold esperado: p95 < 500µs (compile + apply).
    """
    raw = sophos_detection_raw

    def _full_v2_pipeline() -> None:
        compiled = compile_rules(_MAPPING_V2, dsl_version=2)
        apply_compiled(compiled, raw)

    benchmark(_full_v2_pipeline)


# ---------------------------------------------------------------------------
# Fase 3.1 — array_builder benchmark  (suffix: _ab)
# ---------------------------------------------------------------------------
# Convention: ``_ab`` suffix flags this benchmark as belonging to the
# array_builder feature group.  It has no prior baseline entry in
# baseline.json, so --benchmark-compare will emit "not found in baseline"
# for it rather than a regression delta — this is expected and correct.
# The number produced here establishes the future baseline for the _ab group.
#
# Threshold expectation: p95 < 400µs
# (1 json_parse preprocess + 4 observable items: 1 scalar + 1 scalar +
#  1 explode×2 + 1 explode×2 = ~6 observables, dedup on value)
# ---------------------------------------------------------------------------

_MAPPING_V2_OBSERVABLES: Dict[str, Any] = {
    "preprocess": [
        {
            "op": "json_parse",
            "source": "processedData",
            "target": "_processed",
            "tolerant": True,
        },
    ],
    "rules": [
        {"target": "normalized.id", "source": "id"},
        {"target": "normalized.type_name", "source": "type"},
        {
            "target": "normalized.observables",
            "kind": "array_builder",
            "items": [
                {
                    "name": "src_ip",
                    "type": "IP Address",
                    "type_id": 2,
                    "source": "_processed.parsedAlert.fields.clientIp",
                },
                {
                    "name": "email_from",
                    "type": "Email Address",
                    "type_id": 5,
                    "source": "_processed.parsedAlert.fields.mailFrom",
                },
                {
                    "name": "email_to",
                    "type": "Email Address",
                    "type_id": 5,
                    "source": "_processed.parsedAlert.fields.envelopeRecipients",
                    "explode": True,
                },
                {
                    "name": "file_hash",
                    "type": "Hash",
                    "type_id": 8,
                    "source": "_processed.parsedAlert.fields.attachments[*].checksum",
                    "explode": True,
                    "skip_null": True,
                },
            ],
            "skip_null": True,
            "dedup_by": ["value"],
        },
    ],
}

_OBSERVABLES_FIXTURE_PATH = (
    Path(__file__).parent.parent / "fixtures" / "synthetic"
    / "sophos_detection_observables.json"
)


@pytest.fixture(scope="session")
def compiled_v2_sophos_observables() -> Any:
    """Pre-compile array_builder mapping once per session."""
    return compile_rules(_MAPPING_V2_OBSERVABLES, dsl_version=2)


@pytest.fixture(scope="session")
def sophos_observables_raw() -> Dict[str, Any]:
    """Load synthetic Sophos detection (with observables fields) fixture once per session."""
    with _OBSERVABLES_FIXTURE_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def test_bench_array_builder_sophos_email_observables_ab(
    benchmark: Any,
    compiled_v2_sophos_observables: Any,
    sophos_observables_raw: Dict[str, Any],
) -> None:
    """Benchmark apply_compiled with preprocess + 2 scalar rules + 1 array_builder rule.

    array_builder produces ~6 observables:
    - 1 src_ip (scalar)
    - 1 email_from (scalar)
    - 2 email_to (explode over envelopeRecipients)
    - 2 file_hash (explode over attachments[*].checksum)

    After dedup_by=["value"]: all values are distinct → still 6 entries.

    Suffix ``_ab`` marks this as the array_builder benchmark group.
    No baseline comparison expected (new benchmark).

    Threshold expectation: p95 < 400µs.
    """
    assert "processedData" in sophos_observables_raw

    result = benchmark(
        apply_compiled, compiled_v2_sophos_observables, sophos_observables_raw
    )
    # Sanity-check the output so the benchmark cannot pass on a broken path.
    assert isinstance(result.output["normalized"]["observables"], list)
    assert len(result.output["normalized"]["observables"]) >= 4
