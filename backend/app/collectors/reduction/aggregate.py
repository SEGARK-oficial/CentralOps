"""Agregação / rollup por destino (log→métrica).

Colapsa eventos REPETITIVOS de um lote de dispatch em 1 "metric-event" por grupo
(``group_by``), reduzindo o volume entregue ao tier CARO (ex.: um SIEM de analytics).
É a alavanca mais coarse — e a única com potencial de OOM — por isso vem por último e
com as travas mais fortes.

Segurança de detecção ("nunca agrega fluxo de detecção"): a agregação é
**opt-in POR-DESTINO** (``delivery.aggregate.group_by``). O operador liga só nos destinos
onde a granularidade por-evento não importa; a cópia FULL-FIDELITY chega ao lago/S3 por
uma ROTA separada (sem aggregate). Logo o que alimenta detecção nunca é agregado — a
config é a garantia, não uma flag global.

FAIL-OPEN anti-OOM: se a cardinalidade de grupos
distintos estourar ``max_groups``, o lote passa **INTACTO** (passthrough). Nunca
materializamos um group-by explosivo na memória do worker — degrada para "não agrega",
jamais para "estoura o worker".
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence, Tuple


def coalesce(
    batch: Sequence[Mapping[str, Any]],
    group_by: Sequence[str],
    max_groups: int,
) -> Tuple[List[dict], int, int]:
    """Agrega ``batch`` por ``group_by`` (labels do ``_centralops``).

    Retorna ``(lote_agregado, bytes_evitados, eventos_evitados)``:
      * grupos com >1 evento viram 1 metric-event ``{... , "_aggregate": {count, group}}``
        (o 1º evento do grupo, decorado — preserva o shape p/ o formatter do destino);
      * singletons passam ÍNTEGROS (nada a agregar);
      * ``group_by`` vazio ou lote vazio → no-op (lote intacto, 0, 0);
      * cardinalidade > ``max_groups`` → **passthrough** (lote intacto, 0, 0) — fail-open.
    """
    if not group_by or not batch:
        return list(batch), 0, 0

    groups: Dict[tuple, List[Mapping[str, Any]]] = {}
    for env in batch:
        labels = env.get("_centralops") or {}
        key = tuple(str(labels.get(k, "")) for k in group_by)
        bucket = groups.get(key)
        if bucket is None:
            if len(groups) >= max_groups:
                # Cardinalidade explosiva → NÃO materializa mais grupos: passthrough.
                return list(batch), 0, 0
            groups[key] = [env]
        else:
            bucket.append(env)

    from ..output._fastjson import dumps_bytes

    out: List[dict] = []
    saved_bytes = 0
    saved_events = 0
    for key, evs in groups.items():
        if len(evs) == 1:
            out.append(dict(evs[0]))
            continue
        first = evs[0]
        agg = dict(first)
        agg["_centralops"] = dict(first.get("_centralops") or {})
        agg["_aggregate"] = {"count": len(evs), "group": dict(zip(group_by, key))}
        out.append(agg)
        saved_events += len(evs) - 1
        # bytes evitados = soma dos N-1 eventos que não são mais entregues (o lote já está
        # em memória; medir aqui é 1 dumps por evento colapsado, custo de LOTE, não hot-loop).
        for e in evs[1:]:
            saved_bytes += len(dumps_bytes(e))

    return out, saved_bytes, saved_events
