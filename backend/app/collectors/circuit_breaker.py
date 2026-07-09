"""Redis-backed per-destination circuit breaker.

State is stored in Redis so ALL prefork worker processes share the same view:
opening the breaker in one process protects all others immediately.

Keys used (all volatile — TTLed by the breaker logic):
  breaker:{id}:fail   INCR counter, EX window_s        — consecutive failure count
  breaker:{id}:open   SET 1, EX cooldown_s              — breaker is OPEN
  breaker:{id}:probe  SET NX 1, EX cooldown_s           — half-open probe token

State machine:
  CLOSED  → OPEN  : INCR fail; if fail >= threshold  SET open EX cooldown_s
  OPEN    → half  : check() sees open key; SET NX probe → if acquired allow probe
  half    → CLOSED: record_success() DEL fail+open+probe
  half    → OPEN  : record_failure() re-opens (SET open EX cooldown_s)

The half-open probe token is held for ``cooldown_s`` (NOT a fixed 5s): the
probe lifetime is coterminous with the OPEN window so that exactly one probe
runs per cooldown even when the send is slow (a 5s probe TTL
shorter than ``DISPATCH_RESULT_TIMEOUT`` let a second worker win a concurrent
probe mid-send).

BreakerOpen is TERMINAL — it MUST NOT be in tasks._RETRYABLE; instead it
propagates to the except-all handler in dispatch_to_destination, which calls
dispatch_to_dlq (DLQ fallback, no autoretry).  This is the whole point of
the circuit breaker: stop hammering a failing destination.

Redis-outage policy (fail-closed-to-DLQ must not happen):
the breaker store is an OPTIMIZATION, never a delivery gate.  A Redis blip
must never dead-letter healthy traffic nor discard an already-delivered batch.
So every Redis op here is wrapped: ``check()`` FAILS OPEN (allows the send) on
a ``RedisError`` and ``record_success``/``record_failure`` are BEST-EFFORT
(log + return).  ``redis.exceptions.ConnectionError`` is NOT a builtin
``ConnectionError`` and would otherwise escape ``_RETRYABLE`` and land the
batch in the DLQ as "exhausted" with zero retries.

Defaults (per-destination override via dest_config.delivery["breaker"]):
  failure_threshold  = 5    consecutive failures before opening
  cooldown_s         = 30   seconds the breaker stays open (+ half-open probe)
  window_s           = 60   rolling window for the fail counter TTL

Metric:
  BREAKER_STATE Gauge per (destination_id, kind):
    0 = closed, 1 = open, 2 = half-open probe in flight
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

# Redis infra-error base class. ``redis.exceptions.ConnectionError`` /
# ``TimeoutError`` subclass this but are NOT the Python builtins, so they would
# slip past ``tasks._RETRYABLE`` — we catch them here and fail-safe instead.
try:  # pragma: no cover - redis is a hard dependency in all real envs
    from redis.exceptions import RedisError as _RedisError
except Exception:  # pragma: no cover
    _RedisError = ()  # type: ignore[assignment]  # matches nothing

# ── Defaults ─────────────────────────────────────────────────────────────────

_DEFAULT_FAILURE_THRESHOLD = 5
_DEFAULT_COOLDOWN_S = 30
_DEFAULT_WINDOW_S = 60


class BreakerOpen(Exception):
    """Raised by check() when the breaker is OPEN and no probe slot is
    available.  Terminal — must NOT be in ``_RETRYABLE``."""

    def __init__(self, destination_id: str) -> None:
        self.destination_id = destination_id
        super().__init__(
            f"circuit breaker open for destination_id={destination_id!r}"
        )


# ── Key helpers ───────────────────────────────────────────────────────────────


def _key_fail(destination_id: str) -> str:
    return f"breaker:{destination_id}:fail"


def _key_open(destination_id: str) -> str:
    return f"breaker:{destination_id}:open"


def _key_probe(destination_id: str) -> str:
    return f"breaker:{destination_id}:probe"


# ── Config helper ─────────────────────────────────────────────────────────────


def _breaker_cfg(
    delivery: Optional[Mapping[str, Any]],
) -> tuple[int, int, int]:
    """Extract (failure_threshold, cooldown_s, window_s) from the delivery dict.

    Reads via the validated ``BreakerConfig`` (delivery schema) — the
    single source of truth for breaker defaults/bounds — instead of ad-hoc
    ``dict.get``. Lenient: a malformed breaker blob falls back to defaults
    (create-time validation already rejects bad values with 422).

    NOTE: the fallback resets ALL breaker fields to stock (5/30/60), not just the
    offending one. The API validates at create/update, but a direct DB insert or
    migration could land an out-of-bounds row here — so we log a WARNING (review
    LOW) rather than silently reverting."""
    from .output.delivery_config import BreakerConfig

    raw: Mapping[str, Any] = (delivery or {}).get("breaker", {}) or {}
    try:
        bc = BreakerConfig(**dict(raw))
    except Exception:
        logger.warning(
            "circuit_breaker: config de breaker inválida %r — revertendo TODOS os "
            "campos aos defaults (5/30/60). Corrija a linha de destino.",
            dict(raw),
        )
        bc = BreakerConfig()
    return (bc.failure_threshold, bc.cooldown_s, bc.window_s)


# ── Public API ────────────────────────────────────────────────────────────────


async def check(
    redis: Any,
    destination_id: str,
    *,
    kind: str = "unknown",
    probe_ttl_s: int = _DEFAULT_COOLDOWN_S,
) -> None:
    """Check the breaker before sending a batch.

    Raises BreakerOpen if the breaker is OPEN and no half-open probe slot is
    available.  Does nothing when CLOSED.

    Half-open probe: when the breaker is open, the FIRST caller that wins the
    SET NX on ``breaker:{id}:probe`` (``probe_ttl_s`` TTL, default = cooldown)
    is allowed through as a probe.  If the probe succeeds, record_success()
    closes the breaker.  All other callers during the probe window raise
    BreakerOpen.

    FAILS OPEN on Redis errors: if the breaker store is
    unreachable we MUST NOT block delivery nor dead-letter — we allow the send
    and let the destination's own success/failure drive retry.  A retry against
    a still-dead Redis would be pointless, so we do not raise either.

    Args:
        redis:          async redis client (redis.asyncio or fakeredis).
        destination_id: destination identifier.
        kind:           destination kind (for metrics label).
        probe_ttl_s:    half-open probe token TTL (s) — tie to cooldown so one
                        probe runs per OPEN window even for slow sends.
    """
    try:
        is_open = await redis.exists(_key_open(destination_id))
        if not is_open:
            # CLOSED — allow.
            _set_breaker_metric(destination_id, kind, state=0)
            return

        # OPEN — attempt to acquire a half-open probe slot.
        acquired = await redis.set(
            _key_probe(destination_id), "1", nx=True, ex=probe_ttl_s
        )
    except _RedisError:
        # Breaker store down — FAIL OPEN. Never convert a Redis blip into DLQ.
        logger.warning(
            "circuit_breaker.check: Redis indisponível destination_id=%s — "
            "fail-open (permitindo envio; breaker é otimização, não gate)",
            destination_id,
        )
        return

    if acquired:
        # We won the probe slot — allow this one request.
        _set_breaker_metric(destination_id, kind, state=2)
        logger.info(
            "circuit_breaker: half-open probe granted destination_id=%s",
            destination_id,
        )
        return

    # OPEN, no probe slot — reject.
    _set_breaker_metric(destination_id, kind, state=1)
    raise BreakerOpen(destination_id)


async def record_success(
    redis: Any,
    destination_id: str,
    *,
    kind: str = "unknown",
) -> None:
    """Close the breaker after a successful delivery.

    Deletes fail counter, open flag, and probe token.

    BEST-EFFORT: runs AFTER a successful send, so a Redis error
    here must never propagate — propagating would either discard an already
    delivered batch (→ DLQ) or re-send it (→ duplicate). We log and return.
    """
    try:
        await redis.delete(
            _key_fail(destination_id),
            _key_open(destination_id),
            _key_probe(destination_id),
        )
    except _RedisError:
        logger.warning(
            "circuit_breaker.record_success: Redis indisponível destination_id=%s "
            "— bookkeeping best-effort, ignorando (lote já entregue)",
            destination_id,
        )
        return
    _set_breaker_metric(destination_id, kind, state=0)
    logger.debug(
        "circuit_breaker: closed destination_id=%s",
        destination_id,
    )


async def record_failure(
    redis: Any,
    destination_id: str,
    *,
    kind: str = "unknown",
    threshold: int = _DEFAULT_FAILURE_THRESHOLD,
    cooldown_s: int = _DEFAULT_COOLDOWN_S,
    window_s: int = _DEFAULT_WINDOW_S,
) -> None:
    """Record a failure and open the breaker if the threshold is reached.

    Uses INCR on the fail counter (with EX=window_s on first creation) then
    conditionally opens the breaker.  The fail counter TTL slides on each
    INCR because SET is not used; the window is approximate but sufficient
    for the use case.

    Note: Redis INCR does not reset TTL on existing keys, which is the desired
    behaviour: the window is fixed from the first failure.

    BEST-EFFORT: a Redis error must never propagate to the
    dispatcher — it would land an otherwise-handled batch in the DLQ as
    "exhausted" with no retry. We log and return.
    """
    try:
        pipe = redis.pipeline()
        pipe.incr(_key_fail(destination_id))
        # Expire only if the key is new (EXPIRE returns 1 = key exists, 0 = not);
        # we expire unconditionally to reset the window on each failure run.
        pipe.expire(_key_fail(destination_id), window_s)
        results = await pipe.execute()
        fail_count = results[0]
    except _RedisError:
        logger.warning(
            "circuit_breaker.record_failure: Redis indisponível destination_id=%s "
            "— bookkeeping best-effort, ignorando",
            destination_id,
        )
        return

    if fail_count >= threshold:
        try:
            await redis.set(_key_open(destination_id), "1", ex=cooldown_s)
        except _RedisError:
            logger.warning(
                "circuit_breaker.record_failure: Redis indisponível ao abrir "
                "destination_id=%s — best-effort, ignorando",
                destination_id,
            )
            return
        _set_breaker_metric(destination_id, kind, state=1)
        logger.warning(
            "circuit_breaker: OPENED destination_id=%s "
            "fail_count=%d threshold=%d cooldown_s=%d",
            destination_id,
            fail_count,
            threshold,
            cooldown_s,
        )
    else:
        _set_breaker_metric(destination_id, kind, state=0)
        logger.debug(
            "circuit_breaker: failure recorded destination_id=%s "
            "fail_count=%d threshold=%d",
            destination_id,
            fail_count,
            threshold,
        )


# ── Convenience wrapper using DestinationConfig ───────────────────────────────


async def check_for_config(redis: Any, dest_config: Any) -> None:
    """check() using parameters from dest_config.delivery["breaker"].

    The half-open probe TTL is bound to the destination's ``cooldown_s`` so the
    probe slot lasts exactly as long as the OPEN window (one probe per cooldown,
    even for slow sends).
    """
    _, cooldown_s, _ = _breaker_cfg(dest_config.delivery)
    await check(
        redis,
        dest_config.destination_id,
        kind=dest_config.kind,
        probe_ttl_s=cooldown_s,
    )


async def record_success_for_config(redis: Any, dest_config: Any) -> None:
    """record_success() using dest_config."""
    await record_success(
        redis,
        dest_config.destination_id,
        kind=dest_config.kind,
    )


async def record_failure_for_config(redis: Any, dest_config: Any) -> None:
    """record_failure() using parameters from dest_config.delivery["breaker"]."""
    threshold, cooldown_s, window_s = _breaker_cfg(dest_config.delivery)
    await record_failure(
        redis,
        dest_config.destination_id,
        kind=dest_config.kind,
        threshold=threshold,
        cooldown_s=cooldown_s,
        window_s=window_s,
    )


# ── Metrics ─────────────────────────────────────────────────────────


def _set_breaker_metric(destination_id: str, kind: str, *, state: int) -> None:
    """Set the BREAKER_STATE gauge (OTel-native, no-op when OTel export is off).

    Best-effort: silently skips on any error (never breaks the hot path).
    state: 0=closed, 1=open, 2=half-open.
    """
    try:
        from .metrics import BREAKER_STATE

        BREAKER_STATE.labels(
            destination_id=destination_id, kind=kind
        ).set(state)
    except Exception:
        pass  # Metrics are best-effort; never break hot path
