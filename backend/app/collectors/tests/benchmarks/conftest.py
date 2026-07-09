"""Benchmark conftest — session-scoped fixtures and pytest hooks.

Provides:
- ``fixture_registry``: all JSON fixtures pre-loaded, keyed by (vendor, event_type, size).
- ``compiled_rules_registry``: all mapping rules pre-compiled, keyed by (vendor, event_type).
  This isolates JMESPath compile cost so it never pollutes ``apply_compiled`` timings.
- ``pytest_benchmark_update_json`` hook: injects p95/p99 into each benchmark's stats dict.
- Autouse session fixture guard: raises pytest.UsageError if the engine is not importable.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

try:
    from backend.app.collectors.normalize.engine import compile_rules
    from backend.app.collectors.normalize.defaults import (
        DEFAULT_MAPPING_FILES,
        load_default_rules,
    )
except ImportError as _import_exc:
    raise pytest.UsageError(
        f"Cannot import compile_rules from backend.app.collectors.normalize.engine: "
        f"{_import_exc}. "
        "Make sure the backend package is installed and PYTHONPATH is set correctly."
    ) from _import_exc


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

_FixtureKey = Tuple[str, str, str]   # (vendor, event_type, size)
_MappingKey = Tuple[str, str]         # (vendor, event_type)

SIZES = ("small", "medium", "large")

# Canonical vendor → fixture directory name mapping
_VENDOR_DIR: Dict[str, str] = {
    "sophos": "sophos",
    "microsoft_defender": "microsoft_defender",
    "ninjaone": "ninjaone",
    # toda nova (vendor, event_type) em DEFAULT_MAPPING_FILES
    # PRECISA de fixtures em disco aqui, senão fixture_registry quebra na sessão.
    "crowdstrike": "crowdstrike",
    "entra_id": "entra_id",
    "okta": "okta",
    "aws_cloudtrail": "aws_cloudtrail",
    "wazuh": "wazuh",
    # fontes push (FortiGate syslog, Windows Event Log/WEC).
    "fortinet_fortigate": "fortinet_fortigate",
    "windows_event_log": "windows_event_log",
}

# Canonical (vendor, event_type) → fixture file base name
_EVENT_TYPE_BASE: Dict[_MappingKey, str] = {
    ("sophos", "sophos.alert"): "alert",
    ("sophos", "sophos.case"): "case",
    ("sophos", "sophos.detection"): "detection",
    ("microsoft_defender", "defender.alert"): "alert",
    ("microsoft_defender", "defender.incident"): "incident",
    ("ninjaone", "ninjaone.activity"): "activity",
    ("crowdstrike", "crowdstrike.detection"): "detection",
    ("entra_id", "entra_id.signin"): "signin",
    ("entra_id", "entra_id.audit"): "audit",
    ("okta", "okta.system_log"): "system_log",
    ("aws_cloudtrail", "aws_cloudtrail.event"): "event",
    ("wazuh", "wazuh.detection"): "detection",
    # fontes push.
    ("fortinet_fortigate", "fortinet_fortigate.traffic"): "traffic",
    ("windows_event_log", "windows_event_log.security"): "security",
}

_FIXTURES_ROOT = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Session-scoped fixture registry
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def fixture_registry() -> Dict[_FixtureKey, Dict[str, Any]]:
    """Load all benchmark fixtures from disk once per test session.

    Walks ``fixtures/<vendor>/<event_type_base>_<size>.json`` for every
    (vendor, event_type) pair in DEFAULT_MAPPING_FILES and every size in
    SIZES.  Raises FileNotFoundError at session startup if any expected
    fixture is missing — prevents silent benchmark gaps.
    """
    registry: Dict[_FixtureKey, Dict[str, Any]] = {}

    for (vendor, event_type), _mapping_file in DEFAULT_MAPPING_FILES.items():
        # Convenção: o diretório de fixtures é o próprio ``vendor`` e o ``base`` é
        # o sufixo do event_type (``vendor.<suffix>``). Os dicts acima só são
        # necessários para exceções à convenção. O fallback evita um ``KeyError``
        # críptico quando um mapping novo é adicionado sem entrada explícita — o
        # erro útil (``FileNotFoundError`` abaixo) aponta o arquivo a criar.
        vendor_dir = _VENDOR_DIR.get(vendor, vendor)
        base = _EVENT_TYPE_BASE.get(
            (vendor, event_type),
            event_type.split(".", 1)[1] if "." in event_type else event_type,
        )

        for size in SIZES:
            fixture_path = _FIXTURES_ROOT / vendor_dir / f"{base}_{size}.json"
            if not fixture_path.exists():
                raise FileNotFoundError(
                    f"Missing benchmark fixture: {fixture_path}. "
                    f"Create it for (vendor={vendor!r}, event_type={event_type!r}, size={size!r})."
                )
            with fixture_path.open(encoding="utf-8") as fh:
                data: Dict[str, Any] = json.load(fh)
            registry[(vendor, event_type, size)] = data

    return registry


# ---------------------------------------------------------------------------
# Session-scoped compiled rules registry
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def compiled_rules_registry() -> Dict[_MappingKey, tuple]:  # type: ignore[type-arg]
    """Pre-compile all default mapping rules once per session.

    Separates JMESPath compilation cost from ``apply_compiled`` so the
    ``test_apply_compiled_bench`` benchmarks measure only the hot path.
    """
    registry: Dict[_MappingKey, tuple] = {}  # type: ignore[type-arg]

    for vendor, event_type in DEFAULT_MAPPING_FILES:
        rules = load_default_rules(vendor, event_type)
        registry[(vendor, event_type)] = compile_rules(rules)

    return registry


# ---------------------------------------------------------------------------
# GC guard (autouse session fixture)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def _gc_guard() -> None:
    """Session-scoped no-op guard.

    Presence documents the intent: the benchmark suite MUST be run with
    ``--benchmark-disable-gc`` (enforced by the canonical run command in the
    spec).  Per-round GC disabling is handled by pytest-benchmark itself when
    that flag is active; enforcing it here unconditionally would break regular
    non-benchmark test runs where GC is rightly enabled.
    """
    pass


# ---------------------------------------------------------------------------
# pytest_benchmark_update_json hook — inject p95/p99
# ---------------------------------------------------------------------------

def pytest_benchmark_update_json(
    config: Any,
    benchmarks: Any,
    output_json: Dict[str, Any],
) -> None:
    """Inject p95 and p99 percentiles into each benchmark's stats block.

    ``output_json`` is the fully-serialised dict that pytest-benchmark is
    about to write to disk.  Each entry in ``output_json['benchmarks']`` is
    a plain dict produced by ``Metadata.as_dict(include_data=True)`` so
    ``stats.data`` contains the raw per-round timing list.

    Statistics:
        ``statistics.quantiles(data, n=100)`` returns 99 cut-points; index 94
        is p95 (the 95th percentile) and index 98 is p99.
    """
    benchmark_dicts: List[Dict[str, Any]] = output_json.get("benchmarks", [])

    for bench_dict in benchmark_dicts:
        stats: Dict[str, Any] = bench_dict.get("stats", {})
        data: List[float] = stats.get("data") or []

        if len(data) >= 2:
            quantile_cuts = statistics.quantiles(data, n=100)
            stats["p95"] = quantile_cuts[94]   # 95th percentile
            stats["p99"] = quantile_cuts[98]   # 99th percentile
        elif len(data) == 1:
            stats["p95"] = data[0]
            stats["p99"] = data[0]
        else:
            stats["p95"] = None
            stats["p99"] = None

        stats["data"] = []
