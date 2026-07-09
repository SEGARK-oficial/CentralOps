#!/usr/bin/env python3
"""Benchmark regression checker — compares p95 timings between two JSON reports.

Usage:
    python check_regression.py <baseline.json> <new.json> [--baseline-required=true|false]

Exit codes:
    0 — all benchmarks within threshold (or baseline missing with --baseline-required=false)
    1 — one or more benchmarks exceeded threshold, or fatal error

Thresholds:
    - Benchmarks with IDs containing ``_ab`` (array_builder): 1.25x
    - All other benchmarks: 1.10x

Comparison metric: p95 (injected by the conftest pytest_benchmark_generate_json hook).

Pure stdlib — no third-party dependencies.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_THRESHOLD_AB = 1.25       # array_builder benchmarks (id contains "_ab")
_THRESHOLD_DEFAULT = 1.10  # all other benchmarks


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> dict:
    """Load a JSON file; raise SystemExit on parse error."""
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        print(f"ERROR: Cannot parse JSON file {path}: {exc}", file=sys.stderr)
        sys.exit(1)


def _extract_benchmarks(data: dict) -> Dict[str, Optional[float]]:
    """Extract benchmark name → p95 mapping from a pytest-benchmark JSON report.

    Supports both the top-level ``benchmarks`` list format (pytest-benchmark
    default) and the nested ``machine_info`` / ``benchmarks`` format.

    Returns a dict keyed by benchmark ``fullname`` (or ``name`` as fallback),
    with p95 values.  Benchmarks missing p95 get None.
    """
    result: Dict[str, Optional[float]] = {}

    benchmarks = data.get("benchmarks", [])
    if not isinstance(benchmarks, list):
        print(
            "ERROR: 'benchmarks' key is not a list in JSON report.",
            file=sys.stderr,
        )
        sys.exit(1)

    for bench in benchmarks:
        # Prefer fullname for uniqueness; fall back to name.
        bench_id = bench.get("fullname") or bench.get("name") or "unknown"
        stats = bench.get("stats", {})
        p95 = stats.get("p95")
        result[bench_id] = p95

    return result


def _threshold_for(bench_id: str) -> float:
    """Return the regression threshold multiplier for a given benchmark ID."""
    return _THRESHOLD_AB if "_ab" in bench_id else _THRESHOLD_DEFAULT


def _format_time(seconds: Optional[float]) -> str:
    """Format a time in seconds to a human-readable µs/ms string."""
    if seconds is None:
        return "N/A"
    us = seconds * 1_000_000
    if us >= 1000:
        return f"{us / 1000:.3f} ms"
    return f"{us:.2f} µs"


# ---------------------------------------------------------------------------
# Comparison logic
# ---------------------------------------------------------------------------

def compare(
    baseline: Dict[str, Optional[float]],
    new: Dict[str, Optional[float]],
) -> Tuple[bool, list[dict]]:
    """Compare new p95 timings against baseline.

    Returns:
        (all_pass, rows)  where rows is a list of dicts for table rendering.
    """
    all_pass = True
    rows: list[dict] = []

    all_ids = sorted(set(baseline) | set(new))

    for bench_id in all_ids:
        baseline_p95 = baseline.get(bench_id)
        new_p95 = new.get(bench_id)
        threshold = _threshold_for(bench_id)

        # Determine status
        if bench_id not in baseline:
            status = "NEW"
            ratio = None
        elif bench_id not in new:
            status = "MISSING"
            ratio = None
            # A benchmark disappearing from new is a warning, not a failure
            # (the suite may have been narrowed intentionally).
        elif baseline_p95 is None:
            status = "NO_BASELINE_P95"
            ratio = None
        elif new_p95 is None:
            status = "NO_NEW_P95"
            ratio = None
        else:
            ratio = new_p95 / baseline_p95
            if ratio <= threshold:
                status = "PASS"
            else:
                status = "FAIL"
                all_pass = False

        rows.append(
            {
                "id": bench_id,
                "baseline_p95": baseline_p95,
                "new_p95": new_p95,
                "ratio": ratio,
                "threshold": threshold,
                "status": status,
            }
        )

    return all_pass, rows


# ---------------------------------------------------------------------------
# Table rendering
# ---------------------------------------------------------------------------

def _render_table(rows: list[dict]) -> None:
    """Print a formatted ASCII table of benchmark comparison results."""
    # Determine column widths
    id_width = max((len(r["id"]) for r in rows), default=20)
    id_width = max(id_width, 20)

    header = (
        f"{'Benchmark':<{id_width}}  "
        f"{'Baseline p95':>15}  "
        f"{'New p95':>15}  "
        f"{'Ratio':>7}  "
        f"{'Threshold':>9}  "
        f"{'Status':<10}"
    )
    separator = "-" * len(header)

    print(separator)
    print(header)
    print(separator)

    for r in rows:
        ratio_str = f"{r['ratio']:.3f}x" if r["ratio"] is not None else "  N/A  "
        line = (
            f"{r['id']:<{id_width}}  "
            f"{_format_time(r['baseline_p95']):>15}  "
            f"{_format_time(r['new_p95']):>15}  "
            f"{ratio_str:>7}  "
            f"{r['threshold']:.2f}x    "
            f"{r['status']:<10}"
        )
        print(line)

    print(separator)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare benchmark p95 timings for regression detection.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "baseline",
        type=Path,
        help="Path to the baseline benchmark JSON (generated by pytest-benchmark).",
    )
    parser.add_argument(
        "new",
        type=Path,
        help="Path to the new benchmark JSON to compare against baseline.",
    )
    parser.add_argument(
        "--baseline-required",
        type=lambda v: v.lower() not in ("false", "0", "no"),
        default=True,
        metavar="true|false",
        help=(
            "If true (default), exit with error when baseline file is missing. "
            "If false, exit 0 when baseline is absent (useful in CI before first run)."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    baseline_path: Path = args.baseline
    new_path: Path = args.new

    # --- Validate file existence ---
    if not baseline_path.exists():
        if args.baseline_required:
            print(
                f"ERROR: Baseline file not found: {baseline_path}\n"
                "Use --baseline-required=false to skip this check.",
                file=sys.stderr,
            )
            return 1
        else:
            print(
                f"WARNING: Baseline file not found: {baseline_path}. "
                "Skipping regression check (--baseline-required=false).",
                file=sys.stderr,
            )
            return 0

    if not new_path.exists():
        print(f"ERROR: New benchmark file not found: {new_path}", file=sys.stderr)
        return 1

    # --- Load and parse ---
    baseline_data = _load_json(baseline_path)
    new_data = _load_json(new_path)

    baseline_benchmarks = _extract_benchmarks(baseline_data)
    new_benchmarks = _extract_benchmarks(new_data)

    if not baseline_benchmarks:
        print(
            f"WARNING: No benchmarks found in baseline file: {baseline_path}",
            file=sys.stderr,
        )

    if not new_benchmarks:
        print(
            f"ERROR: No benchmarks found in new file: {new_path}",
            file=sys.stderr,
        )
        return 1

    # --- Compare ---
    all_pass, rows = compare(baseline_benchmarks, new_benchmarks)

    # --- Render ---
    print(f"\nRegression check: {baseline_path} → {new_path}\n")
    _render_table(rows)

    fail_count = sum(1 for r in rows if r["status"] == "FAIL")
    pass_count = sum(1 for r in rows if r["status"] == "PASS")
    new_count = sum(1 for r in rows if r["status"] == "NEW")
    missing_count = sum(1 for r in rows if r["status"] == "MISSING")

    print(
        f"\nSummary: {pass_count} PASS, {fail_count} FAIL, "
        f"{new_count} NEW, {missing_count} MISSING\n"
    )

    if all_pass:
        print("Result: OK — no regressions detected.")
        return 0
    else:
        print(
            f"Result: FAIL — {fail_count} benchmark(s) exceeded regression threshold.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
