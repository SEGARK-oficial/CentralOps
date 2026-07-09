"""Baseline benchmark — ``apply_compiled`` hot path.

Measures only the cost of applying pre-compiled rules to a raw event payload.
JMESPath compilation cost is intentionally excluded (handled by
``compiled_rules_registry`` session fixture) to isolate the true hot-path
performance: the inner loop of the normalization engine.

Benchmark IDs follow the stable pattern: ``bench_apply_<vendor>_<event_type>_<size>``
(dots in event_type are replaced with underscores in the parametrize ID).
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

import pytest

from backend.app.collectors.normalize.engine import apply_compiled


# All (vendor, event_type, size) combinations across all registered mappings.
_BENCH_PARAMS: list[Tuple[str, str, str]] = [
    ("sophos", "sophos.alert", "small"),
    ("sophos", "sophos.alert", "medium"),
    ("sophos", "sophos.alert", "large"),
    ("sophos", "sophos.case", "small"),
    ("sophos", "sophos.case", "medium"),
    ("sophos", "sophos.case", "large"),
    ("sophos", "sophos.detection", "small"),
    ("sophos", "sophos.detection", "medium"),
    ("sophos", "sophos.detection", "large"),
    ("microsoft_defender", "defender.alert", "small"),
    ("microsoft_defender", "defender.alert", "medium"),
    ("microsoft_defender", "defender.alert", "large"),
    ("microsoft_defender", "defender.incident", "small"),
    ("microsoft_defender", "defender.incident", "medium"),
    ("microsoft_defender", "defender.incident", "large"),
    ("ninjaone", "ninjaone.activity", "small"),
    ("ninjaone", "ninjaone.activity", "medium"),
    ("ninjaone", "ninjaone.activity", "large"),
    # fontes push (FortiGate syslog, Windows Event Log/WEC).
    ("fortinet_fortigate", "fortinet_fortigate.traffic", "small"),
    ("fortinet_fortigate", "fortinet_fortigate.traffic", "medium"),
    ("fortinet_fortigate", "fortinet_fortigate.traffic", "large"),
    ("windows_event_log", "windows_event_log.security", "small"),
    ("windows_event_log", "windows_event_log.security", "medium"),
    ("windows_event_log", "windows_event_log.security", "large"),
]


def _bench_id(vendor: str, event_type: str, size: str) -> str:
    """Stable, readable benchmark ID.

    Uses underscores throughout so the ID is safe as a filename component
    and unambiguous in ``check_regression.py`` comparisons.
    """
    vendor_slug = vendor.replace(".", "_")
    event_slug = event_type.replace(".", "_")
    return f"bench_apply_{vendor_slug}_{event_slug}_{size}"


@pytest.mark.parametrize(
    "vendor,event_type,size",
    _BENCH_PARAMS,
    ids=[_bench_id(v, e, s) for v, e, s in _BENCH_PARAMS],
)
def test_apply_compiled_bench(
    benchmark: Any,
    vendor: str,
    event_type: str,
    size: str,
    fixture_registry: Dict[Tuple[str, str, str], Dict[str, Any]],
    compiled_rules_registry: Dict[Tuple[str, str], Any],
) -> None:
    """Benchmark ``apply_compiled`` with pre-compiled rules.

    The timed loop body is exactly one call to ``apply_compiled``.
    No setup, no teardown, no assertions inside the timed region.
    """
    compiled = compiled_rules_registry[(vendor, event_type)]
    raw = fixture_registry[(vendor, event_type, size)]

    # Delegate entirely to pytest-benchmark; it handles warmup, rounds,
    # GC disabling, and timing statistics per its CLI configuration.
    benchmark(apply_compiled, compiled, raw)
