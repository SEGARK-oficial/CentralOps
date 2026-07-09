"""Baseline benchmark — full pipeline cold-cache cost.

Measures the combined cost of:
  1. ``compile_rules``   — JMESPath compilation from raw DSL rules.
  2. ``apply_compiled``  — Applying compiled rules to the raw event.
  3. ``build_envelope``  — Wrapping normalized output in the canonical envelope.

This intentionally measures the *cold-cache* cost: every benchmark round
calls ``compile_rules`` fresh, simulating a first-time normalization before
the ``MappingEngine`` LRU cache has warmed up.  This is the worst-case
scenario for a newly deployed worker or after a mapping cache eviction.

Compare these timings against ``test_apply_compiled_bench`` to quantify
the amortization benefit of the ``MappingEngine`` cache.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import pytest

from backend.app.collectors.normalize.defaults import load_default_rules
from backend.app.collectors.normalize.engine import apply_compiled, compile_rules
from backend.app.collectors.normalize.envelope import EnvelopeContext, build_envelope


# Reuse the same parametrize matrix as test_apply_compiled_bench for
# direct comparison.  The ID prefix ``bench_full_`` distinguishes these
# from the hot-path benchmarks.
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
    vendor_slug = vendor.replace(".", "_")
    event_slug = event_type.replace(".", "_")
    return f"bench_full_{vendor_slug}_{event_slug}_{size}"


def _make_ctx(vendor: str, event_type: str) -> EnvelopeContext:
    """Build a minimal EnvelopeContext for the pipeline benchmark."""
    stream = event_type.split(".", 1)[1] if "." in event_type else event_type
    return EnvelopeContext(
        vendor=vendor,
        integration_id=1,
        customer_id=1,
        stream=stream,
        event_type=event_type,
        mapping_version_id="bench-cold-cache",
    )


@pytest.mark.parametrize(
    "vendor,event_type,size",
    _BENCH_PARAMS,
    ids=[_bench_id(v, e, s) for v, e, s in _BENCH_PARAMS],
)
def test_full_pipeline_bench(
    benchmark: Any,
    vendor: str,
    event_type: str,
    size: str,
    fixture_registry: Dict[Tuple[str, str, str], Dict[str, Any]],
) -> None:
    """Benchmark the full cold-cache pipeline: compile + apply + build_envelope.

    ``load_default_rules`` is called *outside* the timed region because it
    performs disk I/O (importlib.resources read); we benchmark CPU cost, not
    filesystem access.  The rules list (Python dicts) is passed into the
    timed lambda fresh each round, which forces ``compile_rules`` to run
    JMESPath compilation on every iteration.
    """
    raw = fixture_registry[(vendor, event_type, size)]
    rules = load_default_rules(vendor, event_type)
    ctx = _make_ctx(vendor, event_type)

    def _full_pipeline() -> None:
        compiled = compile_rules(rules)
        result = apply_compiled(compiled, raw)
        build_envelope(raw, result.output, ctx)

    benchmark(_full_pipeline)
