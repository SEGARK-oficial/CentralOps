"""Harness opt-in (ADR-0016): o que o volume real diz sobre o PRAZO da ausência.

A regra de ausência do exemplo ("cliente sem ponto de restauração há 26 h")
tem dois números escolhidos à mão: o prazo (``window_seconds``) e o
esquecimento. Só o tráfego real diz se 26 h é folga ou é ruído:

1. **Intervalo entre dois pontos de restauração criados** do mesmo tenant —
   a distribuição (p50/p95/máx) é o que o prazo tem de cobrir. Um p95 acima
   do prazo é um alerta falso por semana, garantido.
2. **Quantos tenants distintos** produzem o evento — é a cardinalidade da
   chave vigiada, e o que ``ABSENCE_MAX_KEYS_PER_RULE`` tem de comportar.

O dataset local (ver ``test_dataset_local_harness``) é PRÉ-normalização e
particionado por ``org-*/{stream}/*.ndjson``: o diretório da org é a chave de
tenant, e o código do evento está no campo cru ``detectionRule`` de
``sophos.detection``. Nenhum assert falha por causa do número: o objetivo é
torná-lo VISÍVEL. Os valores impressos são CONTAGENS e intervalos; nenhum
valor de evento sai no log.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional

import pytest

ENV_VAR = "CENTRALOPS_DATASET_DIR"
SKIP_REASON = f"{ENV_VAR} não definida — aponte para o diretório 'ds-bruto' do dataset local."
STREAM = "sophos.detection"
EXPECTED = "XDR-veeam-restorepointcreated"

pytestmark = pytest.mark.dataset


def _root() -> Path:
    raw = os.environ.get(ENV_VAR, "").strip()
    root = Path(raw).expanduser() if raw else None
    if root is None or not root.is_dir():
        pytest.skip(SKIP_REASON)
    return root


def _events_by_org(limit_per_org: int = 200_000) -> Iterator[tuple[str, dict[str, Any]]]:
    for org_dir in sorted(_root().glob("org-*")):
        seen = 0
        for shard in sorted((org_dir / STREAM).glob("*.ndjson")):
            with shard.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield org_dir.name, json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    seen += 1
                    if seen >= limit_per_org:
                        break


def _ts(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def test_gap_between_restore_points_per_tenant():
    """p50/p95/máx do intervalo entre dois ``restorepointcreated`` do mesmo
    tenant, e a cardinalidade da chave. É o número que escolhe o prazo."""
    times: dict[str, list[float]] = {}
    for org, ev in _events_by_org():
        if str(ev.get("detectionRule") or "") != EXPECTED:
            continue
        when = _ts(ev.get("sensorGeneratedAt")) or _ts(ev.get("time"))
        if when is not None:
            times.setdefault(org, []).append(when)
    if not times:
        pytest.skip(f"nenhum {EXPECTED} no dataset")

    gaps: list[float] = []
    per_org: dict[str, tuple[int, Optional[float]]] = {}
    for org, ts in times.items():
        ts.sort()
        g = [b - a for a, b in zip(ts, ts[1:]) if b > a]
        gaps.extend(g)
        per_org[org] = (len(ts), max(g) if g else None)
    gaps.sort()
    hours = 3600.0

    def _p(q: float) -> float:
        return gaps[min(len(gaps) - 1, int(len(gaps) * q))] / hours

    print(f"\n[dataset] {EXPECTED}: {len(times)} tenants com o evento, {sum(len(t) for t in times.values())} ocorrências")
    if gaps:
        print(f"  intervalo entre criações (h): p50={_p(0.5):.1f} p95={_p(0.95):.1f} max={gaps[-1] / hours:.1f}")
        for window_h in (26, 30, 48):
            over = sum(1 for g in gaps if g > window_h * hours)
            print(f"  prazo {window_h} h: {over}/{len(gaps)} intervalos passariam do prazo ({over / len(gaps):.1%})")
    only_once = sum(1 for n, _ in per_org.values() if n == 1)
    print(f"  tenants com UMA só ocorrência (viram alerta ao fim do prazo, somem no esquecimento): {only_once}")
