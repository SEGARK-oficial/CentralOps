"""Native observability store — self-sufficient time-series.

The product's own dashboards read from HERE (Redis), so the UI works WITHOUT a
Prometheus/Grafana deployment (Superfície A). The same events also feed the
OTel-native instruments (``metrics.py`` → OTLP-push) for the SRE/ops surface
(Superfície B) — "one set of events, two surfaces": the native UI (this store)
and the vendor-neutral OTel export.

Model: per-minute buckets in Redis HASHes, shared across ALL worker containers
(they connect to the same state Redis → naturally aggregated). Keys carry a
sliding TTL so inactive entities self-expire; the bucket count is bounded by the
TTL window.

  obs:{kind}:{id}:{metric}   HASH {minute_epoch -> accumulated float}  (counters)
  obs:{kind}:{id}:gauges     HASH {name -> "value"}                    (latest state)

``kind`` ∈ {dest, route}. ``metric`` ∈ {sent, rejected, dlq, shed,
latency_sum, latency_count, routed, dropped, matched, ...}. Cardinality is bounded
(1 key per active destination/route × metric); event_id is NEVER part of a key.

ALL writes are best-effort (never raise into the hot path). The client is a
fork-safe cached SYNC redis (mirrors load_shedder) so producer (sync) and worker
(async, via asyncio.to_thread) and the API (async, via to_thread) share one path.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_TTL_SECONDS = 3 * 60 * 60  # 3h sliding window of minute buckets
_TAP_MAX = 50  # live data-tap ring size per destination
_client = None
_client_pid: Optional[int] = None
_lock = threading.Lock()


def _now() -> float:
    return time.time()


def _minute(now: Optional[float]) -> int:
    return int((now if now is not None else _now()) // 60) * 60


def _redis():
    """Fork-safe cached sync client to the STATE redis (shared across containers)."""
    global _client, _client_pid
    pid = os.getpid()
    if _client is not None and _client_pid == pid:
        return _client
    with _lock:
        if _client is None or _client_pid != pid:
            import redis as redis_sync

            from ..core.config import settings

            _client = redis_sync.from_url(
                settings.REDIS_URL or "redis://localhost:6379/0", decode_responses=True
            )
            _client_pid = pid
    return _client


def _key(kind: str, oid: str, metric: str) -> str:
    return f"obs:{kind}:{oid}:{metric}"


def _gauge_key(kind: str, oid: str) -> str:
    return f"obs:{kind}:{oid}:gauges"


# ── Writes (best-effort) ────────────────────────────────────────────────


def record_counter(kind: str, oid: str, metric: str, value: float = 1.0, *, now: Optional[float] = None) -> None:
    if value == 0:
        return
    try:
        m = str(_minute(now))
        key = _key(kind, oid, metric)
        r = _redis()
        pipe = r.pipeline()
        pipe.hincrbyfloat(key, m, float(value))
        pipe.expire(key, _TTL_SECONDS)
        pipe.execute()
    except Exception:
        logger.debug("observability_store.record_counter falhou (%s/%s/%s)", kind, oid, metric, exc_info=True)


def record_latency(kind: str, oid: str, seconds: float, *, now: Optional[float] = None) -> None:
    """Accumulate latency sum + count per minute → the reader computes the avg."""
    record_counter(kind, oid, "latency_sum", seconds, now=now)
    record_counter(kind, oid, "latency_count", 1.0, now=now)


def set_gauge(kind: str, oid: str, name: str, value: object) -> None:
    try:
        key = _gauge_key(kind, oid)
        r = _redis()
        pipe = r.pipeline()
        pipe.hset(key, name, str(value))
        pipe.expire(key, _TTL_SECONDS)
        pipe.execute()
    except Exception:
        logger.debug("observability_store.set_gauge falhou (%s/%s/%s)", kind, oid, name, exc_info=True)


# ── Reads (best-effort → empty on error) ────────────────────────────────


def read_series(
    kind: str, oid: str, metrics: List[str], *, minutes: int = 60, now: Optional[float] = None
) -> Dict[str, List[list]]:
    """{metric: [[minute_epoch, value], ...]} for the last ``minutes``. The
    synthetic metric ``latency_avg`` is derived from latency_sum/latency_count."""
    cutoff = _minute(now) - minutes * 60
    raw_metrics = set(metrics)
    if "latency_avg" in raw_metrics:
        raw_metrics.update({"latency_sum", "latency_count"})

    fetched: Dict[str, Dict[int, float]] = {}
    try:
        r = _redis()
        for metric in raw_metrics:
            try:
                raw = r.hgetall(_key(kind, oid, metric)) or {}
            except Exception:
                raw = {}
            fetched[metric] = {
                int(mm): float(vv) for mm, vv in raw.items() if int(mm) >= cutoff
            }
    except Exception:
        logger.debug("observability_store.read_series falhou (%s/%s)", kind, oid, exc_info=True)

    out: Dict[str, List[list]] = {}
    for metric in metrics:
        # ``latency_sum``/``latency_count`` são accounting INTERNO (derivam o
        # latency_avg); nunca os emitimos como série, mesmo se o caller pedir
        # explicitamente (evita vazar contadores crus).
        if metric in ("latency_sum", "latency_count"):
            continue
        if metric == "latency_avg":
            sums = fetched.get("latency_sum", {})
            cnts = fetched.get("latency_count", {})
            pts = [
                [m, sums[m] / cnts[m]] for m in sorted(sums) if cnts.get(m)
            ]
        else:
            d = fetched.get(metric, {})
            pts = [[m, d[m]] for m in sorted(d)]
        out[metric] = pts
    return out


def read_window_total(
    kind: str, oid: str, metric: str, *, minutes: int, now: Optional[float] = None
) -> float:
    """Soma do counter ``metric`` na janela de ``minutes`` (0.0 em erro/sem dado)."""
    series = read_series(kind, oid, [metric], minutes=minutes, now=now)
    return float(sum(v for _, v in series.get(metric, [])))


def read_window_rate(
    kind: str, oid: str, metric: str, *, minutes: int, now: Optional[float] = None
) -> float:
    """Taxa média (eventos/segundo) do counter ``metric`` na janela de ``minutes``
    — soma por-minuto / segundos da janela. Padrão AxoSyslog ``eps_last_*``:
    pré-computado no app, sem depender do Prometheus. 0.0 sem dado/erro."""
    if minutes <= 0:
        return 0.0
    return read_window_total(kind, oid, metric, minutes=minutes, now=now) / (minutes * 60)


def read_gauges(kind: str, oid: str, names: List[str]) -> Dict[str, Optional[str]]:
    try:
        raw = _redis().hgetall(_gauge_key(kind, oid)) or {}
    except Exception:
        raw = {}
    return {n: raw.get(n) for n in names}


# ── Live data-tap per destination (Axoflow-style) ──────


def _tap_key(destination_id: str) -> str:
    return f"obs:tap:dest:{destination_id}"


def record_tap(destination_id: str, envelopes: List[Dict[str, Any]]) -> None:
    """Push recently-dispatched envelopes onto the destination's tap ring
    (newest first, capped). Best-effort.

    Redação: aplica ``_redact`` (mascara chaves cujo NOME é de segredo conhecido
    — token/password/secret/…). NÃO é um classificador de PII: campos PII sob
    chaves arbitrárias do evento ``raw`` do vendor passam. É redação-de-segredos,
    não PII-safe.

    LPUSH+LTRIM rodam em MULTI/EXEC (``transaction=True``) → o ring é cap-ado de
    forma ATÔMICA; um leitor concorrente nunca enxerga mais que ``_TAP_MAX``.
    """
    if not envelopes:
        return
    try:
        from .audit_buffer import _redact

        items = [
            json.dumps(_redact(e), default=str) for e in envelopes[:_TAP_MAX]
        ]
        key = _tap_key(destination_id)
        r = _redis()
        pipe = r.pipeline(transaction=True)  # MULTI/EXEC — LPUSH+LTRIM atômicos
        pipe.lpush(key, *items)
        pipe.ltrim(key, 0, _TAP_MAX - 1)
        pipe.expire(key, _TTL_SECONDS)
        pipe.execute()
    except Exception:
        logger.debug("observability_store.record_tap falhou (%s)", destination_id, exc_info=True)


def read_tap(destination_id: str, *, limit: int = _TAP_MAX) -> List[Dict[str, Any]]:
    """Most-recent dispatched envelopes for a destination (redacted)."""
    try:
        raw = _redis().lrange(_tap_key(destination_id), 0, limit - 1) or []
    except Exception:
        raw = []
    out: List[Dict[str, Any]] = []
    for item in raw:
        try:
            out.append(json.loads(item))
        except (TypeError, ValueError):
            continue
    return out


def reset() -> None:
    """Test seam — drop the cached client."""
    global _client, _client_pid
    _client = None
    _client_pid = None
