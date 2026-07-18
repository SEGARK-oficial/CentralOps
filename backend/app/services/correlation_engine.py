"""Motor de correlação por threshold — funções PURAS, testáveis.

Avalia uma regra threshold sobre uma lista de eventos (os resultados cross-source
de uma query federada): filtra por ``where``, agrupa por ``group_by_field`` e, com
janela deslizante de ``window_seconds`` sobre ``timestamp_field``, devolve os grupos
que atingiram ``min_count``. Sem efeito colateral nem dependência de pySigma/OCSF —
opera sobre dicts (vendor-native ou normalizado); o ``CorrelationService`` é quem
persiste as ``Detection``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

_OPS = ("eq", "ne", "contains", "gt", "lt", "gte", "lte")


def extract_path(item: Any, dotted: Optional[str]) -> Any:
    """Navega um caminho ``a.b.c`` num dict aninhado. ``None`` se ausente/inválido."""
    if not dotted:
        return None
    cur = item
    for part in dotted.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
        if cur is None:
            return None
    return cur


def _coerce_number(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def matches_where(item: dict, filters: Iterable[dict]) -> bool:
    """True se ``item`` casa TODOS os filtros (AND). Filtro inválido ⇒ não casa."""
    for flt in filters or ():
        field = flt.get("field")
        op = flt.get("op", "eq")
        target = flt.get("value")
        if op not in _OPS:
            return False
        actual = extract_path(item, field)
        if op == "eq":
            # campo ausente (None) nunca casa um valor concreto — evita o footgun
            # de stringificar None para "None".
            if actual is None or str(actual) != str(target):
                return False
        elif op == "ne":
            # ausente é "diferente" de qualquer valor concreto → mantém o evento.
            if actual is not None and str(actual) == str(target):
                return False
        elif op == "contains":
            if actual is None or str(target) not in str(actual):
                return False
        else:  # gt/lt/gte/lte — numérico
            a, b = _coerce_number(actual), _coerce_number(target)
            if a is None or b is None:
                return False
            if op == "gt" and not a > b:
                return False
            if op == "lt" and not a < b:
                return False
            if op == "gte" and not a >= b:
                return False
            if op == "lte" and not a <= b:
                return False
    return True


# Limiar segundos-vs-ms para epochs numéricos. Espelha (deliberadamente, para
# manter este módulo sem dependência de collectors/OCSF — ver docstring do
# módulo) ``collectors.normalize.operators._EPOCH_MS_THRESHOLD``: como segundos
# 1e11 ≈ ano 5138, como ms ≈ 1973-03-03. Necessário porque ``timestamp_field``
# pode apontar para um campo ``timestamp_t`` do OCSF, que é em MILISSEGUNDOS —
# sem a conversão, ``window_seconds`` compararia deltas 1000× maiores e as
# regras threshold simplesmente parariam de disparar.
_EPOCH_MS_THRESHOLD = 100_000_000_000  # 1e11


def _parse_ts(value: Any) -> Optional[float]:
    """ISO-8601 (ou epoch em segundos/ms) → SEGUNDOS. ``None`` se não parsear."""
    if value is None:
        return None
    num = _coerce_number(value)
    if num is not None:
        # Epoch numérico de unidade desconhecida: ms acima do limiar.
        return num / 1000.0 if abs(num) >= _EPOCH_MS_THRESHOLD else num
    try:
        return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _window_max_count(timestamps: list[float], window_seconds: int) -> int:
    """Máximo de timestamps dentro de QUALQUER janela deslizante de ``window_seconds``
    (two-pointer sobre a lista ordenada)."""
    if not timestamps:
        return 0
    ts = sorted(timestamps)
    best = 1
    left = 0
    for right in range(len(ts)):
        while ts[right] - ts[left] > window_seconds:
            left += 1
        best = max(best, right - left + 1)
    return best


def evaluate_threshold(rule: Any, items: list[dict]) -> list[dict]:
    """Avalia uma regra threshold. Devolve ``[{group, count, sample}]`` p/ grupos que
    dispararam. ``rule`` precisa de: ``group_by_field``, ``min_count``,
    ``window_seconds``, ``timestamp_field``, ``where_json`` (parseado)."""
    import json

    min_count = int(getattr(rule, "min_count", 0) or 0)
    group_field = getattr(rule, "group_by_field", None)
    window = int(getattr(rule, "window_seconds", 0) or 0)
    ts_field = getattr(rule, "timestamp_field", None)
    try:
        filters = json.loads(getattr(rule, "where_json", None) or "[]")
    except (ValueError, TypeError):
        filters = []
    if not isinstance(filters, list):
        filters = []

    if not group_field or min_count <= 0:
        return []

    groups: dict[str, list[dict]] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        if filters and not matches_where(it, filters):
            continue
        key = extract_path(it, group_field)
        if key is None:
            continue
        groups.setdefault(str(key), []).append(it)

    hits: list[dict] = []
    for key, evs in groups.items():
        if window > 0 and ts_field:
            tss = [t for t in (_parse_ts(extract_path(e, ts_field)) for e in evs) if t is not None]
            # FAIL-CLOSED: timestamps inválidos/ausentes ⇒ tss vazio ⇒ count 0 (NÃO
            # len(evs)). Senão a regra "N em W segundos" viraria "N em qualquer tempo"
            # silenciosamente → falso-positivo.
            count = _window_max_count(tss, window)
        else:
            count = len(evs)  # sem janela/timestamp ⇒ conta todos do grupo (por design)
        if count >= min_count:
            hits.append({"group": key, "count": count, "sample": evs[0]})
    return hits
