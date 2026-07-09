"""Benchmark da etapa de normalização do pipeline (RNF1.1).

Mede o custo de aplicar o mapping default + montar o envelope sobre
um payload sintético, replicado N vezes. Não toca DB nem Redis — mede
só a porção CPU-bound do pipeline.

Uso:

    APP_MASTER_KEY=test-master-key-for-centralops-suite-12345 \
    APP_ENV=test \
    /private/tmp/centralops-venv/bin/python backend/scripts/benchmark_normalize.py

Saída: tempo médio por evento (μs) e throughput estimado (ev/s).

Alvo de referência (RNF1.1): 1k–10k ev/s no nó. Como rodamos numa
instância Python single-thread com cache de regras quente, o número
aqui é "limite teórico CPU"; throughput end-to-end com I/O fica
abaixo. Use como baseline para detectar regressão de versão.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# Garante import do package mesmo rodando direto.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

from backend.app.collectors.normalize.defaults import load_default_rules
from backend.app.collectors.normalize.engine import (
    apply_compiled,
    compile_rules,
)
from backend.app.collectors.normalize.envelope import (
    EnvelopeContext,
    build_envelope,
)


SAMPLE_RAW = {
    "id": "alert-uuid-001",
    "createdAt": "2026-04-23T14:22:10Z",
    "raisedAt": "2026-04-23T14:22:08Z",
    "severity": "critical",
    "type": "malware",
    "category": "Threats",
    "description": "Trojan.GenericKD detected",
    "managedAgent": {
        "id": "agent-1",
        "name": "WIN-DESKTOP-01",
        "type": "computer",
    },
    "person": {"id": "user-1", "name": "alice"},
    "product": "Endpoint",
    "tenant": {"id": "tenant-x"},
}


def run(iterations: int = 10_000) -> None:
    rules = load_default_rules("sophos", "sophos.alert")
    compiled = compile_rules(rules)
    ctx = EnvelopeContext(
        vendor="sophos",
        integration_id=1,
        customer_id=42,
        stream="alerts",
        event_type="sophos.alert",
        mapping_version_id="bench",
    )

    # Warm-up — JIT-like effects do CPython + cache do hashlib + jmespath.
    for _ in range(500):
        applied = apply_compiled(compiled, SAMPLE_RAW)
        build_envelope(SAMPLE_RAW, applied.output, ctx, vendor_msg_id=SAMPLE_RAW["id"])

    started = time.perf_counter()
    for _ in range(iterations):
        applied = apply_compiled(compiled, SAMPLE_RAW)
        build_envelope(SAMPLE_RAW, applied.output, ctx, vendor_msg_id=SAMPLE_RAW["id"])
    elapsed = time.perf_counter() - started

    per_event_us = (elapsed / iterations) * 1_000_000
    throughput = iterations / elapsed if elapsed > 0 else float("inf")

    print(f"iterations: {iterations:>8}")
    print(f"elapsed:    {elapsed * 1000:>7.1f} ms")
    print(f"per event:  {per_event_us:>7.1f} μs")
    print(f"throughput: {throughput:>7.0f} ev/s (single-thread, in-memory)")


if __name__ == "__main__":
    iters = int(sys.argv[1]) if len(sys.argv) > 1 else 10_000
    run(iters)
