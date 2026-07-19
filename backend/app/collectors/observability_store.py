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

_TTL_SECONDS = 3 * 60 * 60  # 3h sliding window — DEFAULT ttl for callers that don't override
_BUCKET_SECONDS = 60  # per-minute buckets — DEFAULT granularity for callers that don't override
_TAP_MAX = 50  # live data-tap ring size per destination
_client = None
_client_pid: Optional[int] = None
_lock = threading.Lock()


def _now() -> float:
    return time.time()


def _bucket_epoch(now: Optional[float], bucket_seconds: int) -> int:
    """Epoch of the bucket ``now`` falls into, aligned to ``bucket_seconds``.

    Generalizes the old per-minute-only ``_minute()``: a 24h read window over
    per-minute buckets means 1440 hash fields per key (and a TTL that must
    outlive all of them); an hourly bucket for the same window is 24 fields.
    Callers pick the granularity per call via ``record_counter``/
    ``read_window_total``'s ``bucket_seconds`` kwarg — the default (60s)
    reproduces the original per-minute behaviour byte-for-byte.
    """
    return int((now if now is not None else _now()) // bucket_seconds) * bucket_seconds


def _minute(now: Optional[float]) -> int:
    return _bucket_epoch(now, _BUCKET_SECONDS)


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


def record_counter(
    kind: str,
    oid: str,
    metric: str,
    value: float = 1.0,
    *,
    now: Optional[float] = None,
    ttl_seconds: int = _TTL_SECONDS,
    bucket_seconds: int = _BUCKET_SECONDS,
) -> None:
    """Accumulate ``value`` into the current bucket of ``metric``. Best-effort
    (never raises). ``ttl_seconds``/``bucket_seconds`` default to the original
    3h/per-minute pair — pass them explicitly for a different retention/
    granularity profile (e.g. hourly buckets with a 25h TTL for a 24h read
    window; per-minute buckets would need 1440 fields per hash for the same
    window). A caller that reads this series back MUST pass the matching
    ``bucket_seconds`` to ``read_window_total``/``read_series``, or the window
    cutoff will be computed against the wrong epoch alignment."""
    if value == 0:
        return
    try:
        b = str(_bucket_epoch(now, bucket_seconds))
        key = _key(kind, oid, metric)
        r = _redis()
        pipe = r.pipeline()
        pipe.hincrbyfloat(key, b, float(value))
        pipe.expire(key, ttl_seconds)
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
    kind: str,
    oid: str,
    metrics: List[str],
    *,
    minutes: int = 60,
    now: Optional[float] = None,
    bucket_seconds: int = _BUCKET_SECONDS,
) -> Dict[str, List[list]]:
    """{metric: [[bucket_epoch, value], ...]} for the last ``minutes``. The
    synthetic metric ``latency_avg`` is derived from latency_sum/latency_count.
    ``bucket_seconds`` must match what ``record_counter`` used to write the
    series (default: per-minute, same as before this kwarg existed)."""
    cutoff = _bucket_epoch(now, bucket_seconds) - minutes * 60
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


def _warn_if_window_exceeds_ttl(
    kind: str, oid: str, metric: str, *, minutes: int, ttl_seconds: int
) -> None:
    """A window longer than the TTL used to write the series will silently
    under-count (the oldest buckets have already expired in Redis) — this is
    exactly the "3h TTL, 24h window" bug this module used to have. Diagnostic
    only (debug level): the caller is responsible for keeping the two in sync,
    this just makes the mismatch discoverable instead of a mystery zero."""
    if minutes * 60 > ttl_seconds:
        logger.debug(
            "observability_store: janela de %d min pedida > TTL de %d s "
            "configurado (%s/%s/%s) — buckets fora da janela retida já "
            "expiraram no Redis; a soma pode estar sub-contada",
            minutes, ttl_seconds, kind, oid, metric,
        )


def read_window_total(
    kind: str,
    oid: str,
    metric: str,
    *,
    minutes: int,
    now: Optional[float] = None,
    bucket_seconds: int = _BUCKET_SECONDS,
    ttl_seconds: int = _TTL_SECONDS,
) -> float:
    """Soma do counter ``metric`` na janela de ``minutes`` (0.0 em erro/sem dado).

    Best-effort DE PROPÓSITO — qualquer falha de leitura (Redis fora do ar,
    timeout, ...) vira ``0.0``, INDISTINGUÍVEL de "a série existe e soma zero".
    Quando essa ambiguidade importa (ex.: contador de disparos de regra, onde
    um operador vendo "0" precisa saber se a regra está muda ou se a leitura
    falhou), use ``read_window_total_strict``.

    ``bucket_seconds``/``ttl_seconds`` devem espelhar o que ``record_counter``
    usou para escrever a série (defaults = comportamento original: per-minute/
    3h)."""
    _warn_if_window_exceeds_ttl(kind, oid, metric, minutes=minutes, ttl_seconds=ttl_seconds)
    series = read_series(kind, oid, [metric], minutes=minutes, now=now, bucket_seconds=bucket_seconds)
    return float(sum(v for _, v in series.get(metric, [])))


def read_window_total_strict(
    kind: str,
    oid: str,
    metric: str,
    *,
    minutes: int,
    now: Optional[float] = None,
    bucket_seconds: int = _BUCKET_SECONDS,
    ttl_seconds: int = _TTL_SECONDS,
) -> float:
    """Como ``read_window_total``, mas PROPAGA qualquer falha de leitura (Redis
    indisponível, timeout, resposta corrompida) em vez de mascará-la como
    ``0.0``.

    ``read_window_total``/``read_series`` são best-effort de propósito (nunca
    podem derrubar um dashboard) — mas isso faz ``0.0`` ambíguo: tanto "a regra
    não disparou na janela" quanto "não consegui falar com o Redis" produzem o
    mesmo zero. Para contadores onde essa distinção importa — ex.: "disparos
    nas últimas 24h" por regra de correlação, onde "0 disparos" e "não sei"
    NUNCA podem parecer a mesma coisa para o operador — o chamador usa esta
    função, captura a exceção NO SEU nível e devolve ``None`` (nunca ``0``)
    para a UI renderizar "—" com tooltip de erro.

    Soma direta do hash de um único counter — não passa por ``read_series``
    (que engole exceções por design) e não suporta métricas sintéticas como
    ``latency_avg``; para essas, use ``read_series``."""
    _warn_if_window_exceeds_ttl(kind, oid, metric, minutes=minutes, ttl_seconds=ttl_seconds)
    cutoff = _bucket_epoch(now, bucket_seconds) - minutes * 60
    r = _redis()
    raw = r.hgetall(_key(kind, oid, metric)) or {}
    return float(sum(float(v) for b, v in raw.items() if int(b) >= cutoff))


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
