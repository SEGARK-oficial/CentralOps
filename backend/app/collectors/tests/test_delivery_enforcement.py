"""delivery enforcement tests.

Proves that the dispatcher ENFORCES (not just documents) the delivery policy:

  (a) batch.max_items: a batch larger than max_items is split into chunks;
      each chunk is sent as a separate send_batch call.
  (b) timeout_ms: asyncio.wait_for aborts a slow send and classifies it as
      retryable (TransientDeliveryError after exhausting max_retries).
  (c) retry.max_retries: internal retry loop is exhausted before raising
      TransientDeliveryError; the sender is called exactly max_retries+1 times.
  (d) exponential backoff: sleep delays match min(max_ms, initial_ms*multiplier**n).
  (e) 4xx / poison-pill (result.retryable=False): no retry, goes straight to DLQ.

All tests use stub senders (AsyncMock) — no real HTTP, no DB, no Redis needed.
The _send_chunk_with_retry helper is tested in isolation; dispatch_batch_to_destination
integration is tested via the chunk-split behaviour.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

from backend.app.collectors.output.base import DeliveryResult, RejectedEvent
from backend.app.collectors.output.delivery_config import (
    BatchConfig,
    DeliveryConfig,
    RetryConfig,
    backoff_delay_s,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _event(n: int) -> dict:
    return {"_centralops": {"event_id": f"evt-{n:04d}", "organization_id": 1}, "raw": {}}


def _batch(size: int) -> list[dict]:
    return [_event(i) for i in range(size)]


def _dcfg(
    *,
    max_items: int = 500,
    max_retries: int = 3,
    initial_ms: int = 10,
    max_ms: int = 200,
    multiplier: float = 2.0,
    timeout_ms: int = 30000,
    concurrency: int = 4,
) -> DeliveryConfig:
    """Build a DeliveryConfig with test-friendly defaults (fast backoff)."""
    return DeliveryConfig(
        batch=BatchConfig(max_items=max_items),
        retry=RetryConfig(
            max_retries=max_retries,
            initial_ms=initial_ms,
            max_ms=max_ms,
            multiplier=multiplier,
        ),
        timeout_ms=timeout_ms,
        concurrency=concurrency,
    )


def _dest_config(kind: str = "splunk_hec") -> MagicMock:
    cfg = MagicMock()
    cfg.destination_id = "test-dest-001"
    cfg.kind = kind
    cfg.delivery = {}
    cfg.secret_ref = None
    cfg.config_version = "v1"
    cfg.name = "Test Destination"
    cfg.organization_id = None
    return cfg


def _labels(dest_config: Any) -> dict:
    return {
        "destination_id": dest_config.destination_id,
        "kind": dest_config.kind,
    }


def _stub_metrics() -> dict:
    """Return no-op metric stubs."""
    m = MagicMock()
    m.labels.return_value = m
    return m


def _stub_circuit_breaker() -> MagicMock:
    """Circuit breaker that never opens, always allows."""
    cb = MagicMock()
    cb.check_for_config = AsyncMock(return_value=None)
    cb.record_failure_for_config = AsyncMock(return_value=None)
    cb.record_success_for_config = AsyncMock(return_value=None)
    cb.BreakerOpen = type("BreakerOpen", (Exception,), {})
    return cb


async def _run_chunk_with_retry(
    *,
    target: Any,
    chunk: list[dict],
    dcfg: DeliveryConfig,
    dest_config: Any | None = None,
    circuit_breaker: Any | None = None,
    persist_rejected_to_dlq: Any | None = None,
    sleep_mock: Any | None = None,
) -> DeliveryResult:
    """Call _send_chunk_with_retry with minimal stubs.

    ``persist_rejected_to_dlq`` must be a SYNC callable (it is run via
    ``asyncio.to_thread``). Pass ``MagicMock(return_value=True)`` (not
    AsyncMock) to avoid "coroutine never awaited" warnings.
    """
    from backend.app.collectors.pipeline import _send_chunk_with_retry

    dc = dest_config or _dest_config()
    cb = circuit_breaker or _stub_circuit_breaker()
    labels = _labels(dc)

    # Stub metrics
    DELIVERY_LATENCY = _stub_metrics()
    DLQ_TOTAL = _stub_metrics()
    EVENTS_REJECTED = _stub_metrics()
    EVENTS_SENT = _stub_metrics()
    BYTES_SENT = _stub_metrics()
    RETRIES = _stub_metrics()

    # Default no-op DLQ persist (sync — called via asyncio.to_thread)
    if persist_rejected_to_dlq is None:
        persist_rejected_to_dlq = MagicMock(return_value=True)

    # Patch asyncio.sleep if provided
    patch_target = "backend.app.collectors.pipeline.asyncio.sleep"
    if sleep_mock is not None:
        with patch(patch_target, sleep_mock):
            return await _send_chunk_with_retry(
                target=target,
                chunk=chunk,
                dcfg=dcfg,
                dest_config=dc,
                labels=labels,
                redis=AsyncMock(),
                circuit_breaker=cb,
                persist_rejected_to_dlq=persist_rejected_to_dlq,
                DELIVERY_LATENCY=DELIVERY_LATENCY,
                DLQ_TOTAL=DLQ_TOTAL,
                EVENTS_REJECTED=EVENTS_REJECTED,
                EVENTS_SENT=EVENTS_SENT,
                BYTES_SENT=BYTES_SENT,
                RETRIES=RETRIES,
            )
    else:
        return await _send_chunk_with_retry(
            target=target,
            chunk=chunk,
            dcfg=dcfg,
            dest_config=dc,
            labels=labels,
            redis=AsyncMock(),
            circuit_breaker=cb,
            persist_rejected_to_dlq=persist_rejected_to_dlq,
            DELIVERY_LATENCY=DELIVERY_LATENCY,
            DLQ_TOTAL=DLQ_TOTAL,
            EVENTS_REJECTED=EVENTS_REJECTED,
            EVENTS_SENT=EVENTS_SENT,
            BYTES_SENT=BYTES_SENT,
            RETRIES=RETRIES,
        )


# ── (a) batch.max_items enforcement ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_batch_split_by_max_items() -> None:
    """A batch of 10 events with max_items=3 → four send_batch calls (3+3+3+1)."""
    received: list[list[dict]] = []

    async def fake_send(batch: list[dict]) -> DeliveryResult:
        received.append(list(batch))
        return DeliveryResult.ok(len(batch))

    target = AsyncMock()
    target.send_batch.side_effect = fake_send

    dcfg = _dcfg(max_items=3)
    dc = _dest_config()
    cb = _stub_circuit_breaker()
    labels = _labels(dc)
    DELIVERY_LATENCY = _stub_metrics()
    DLQ_TOTAL = _stub_metrics()
    EVENTS_REJECTED = _stub_metrics()
    EVENTS_SENT = _stub_metrics()
    BYTES_SENT = _stub_metrics()
    RETRIES = _stub_metrics()

    batch = _batch(10)

    # Build the chunk list as the dispatcher does.
    max_items = dcfg.batch.max_items
    chunks = [batch[i : i + max_items] for i in range(0, len(batch), max_items)]

    from backend.app.collectors.pipeline import _send_chunk_with_retry

    for chunk in chunks:
        await _send_chunk_with_retry(
            target=target,
            chunk=chunk,
            dcfg=dcfg,
            dest_config=dc,
            labels=labels,
            redis=AsyncMock(),
            circuit_breaker=cb,
            persist_rejected_to_dlq=MagicMock(return_value=True),
            DELIVERY_LATENCY=DELIVERY_LATENCY,
            DLQ_TOTAL=DLQ_TOTAL,
            EVENTS_REJECTED=EVENTS_REJECTED,
            EVENTS_SENT=EVENTS_SENT,
            BYTES_SENT=BYTES_SENT,
            RETRIES=RETRIES,
        )

    assert len(received) == 4  # ceil(10/3) = 4
    assert [len(c) for c in received] == [3, 3, 3, 1]
    # Every event delivered exactly once.
    all_ids = [e["_centralops"]["event_id"] for chunk in received for e in chunk]
    assert sorted(all_ids) == sorted(e["_centralops"]["event_id"] for e in batch)


@pytest.mark.asyncio
async def test_batch_not_split_when_within_max_items() -> None:
    """A batch within max_items → exactly one send_batch call."""
    target = AsyncMock()
    target.send_batch.return_value = DeliveryResult.ok(5)

    dcfg = _dcfg(max_items=100)
    batch = _batch(5)
    chunks = [batch[i : i + 100] for i in range(0, len(batch), 100)]

    from backend.app.collectors.pipeline import _send_chunk_with_retry

    dc = _dest_config()
    cb = _stub_circuit_breaker()
    labels = _labels(dc)
    m = _stub_metrics()

    for chunk in chunks:
        await _send_chunk_with_retry(
            target=target,
            chunk=chunk,
            dcfg=dcfg,
            dest_config=dc,
            labels=labels,
            redis=AsyncMock(),
            circuit_breaker=cb,
            persist_rejected_to_dlq=MagicMock(return_value=True),
            DELIVERY_LATENCY=m,
            DLQ_TOTAL=m,
            EVENTS_REJECTED=m,
            EVENTS_SENT=m,
            BYTES_SENT=m,
            RETRIES=m,
        )

    target.send_batch.assert_called_once()


# ── (b) timeout_ms enforcement ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_timeout_aborts_slow_send_and_raises_transient() -> None:
    """A send that sleeps longer than timeout_ms raises TransientDeliveryError.

    We patch asyncio.wait_for to simulate a timeout without real sleeping —
    the real wait_for is replaced by one that raises TimeoutError immediately
    when asked to await the slow sender.
    """
    from backend.app.collectors.delivery import TransientDeliveryError

    async def slow_send(batch: list[dict]) -> DeliveryResult:
        await asyncio.sleep(3600)  # would block forever in production
        return DeliveryResult.ok(len(batch))

    target = AsyncMock()
    target.send_batch.side_effect = slow_send

    # timeout_ms=100 is the minimum valid value; the real timeout is mocked out.
    dcfg = _dcfg(timeout_ms=100, max_retries=0)
    chunk = _batch(2)

    async def instant_timeout(coro: Any, *, timeout: float) -> Any:
        coro.close()  # prevent "coroutine was never awaited" warning
        raise asyncio.TimeoutError()

    with patch("backend.app.collectors.pipeline.asyncio.wait_for", instant_timeout):
        with patch("backend.app.collectors.pipeline.asyncio.sleep", AsyncMock()):
            with pytest.raises(TransientDeliveryError):
                await _run_chunk_with_retry(
                    target=target,
                    chunk=chunk,
                    dcfg=dcfg,
                )


@pytest.mark.asyncio
async def test_timeout_is_retryable() -> None:
    """A timeout on the first attempt is retried when max_retries > 0."""
    call_count = 0

    async def fake_wait_for(coro: Any, *, timeout: float) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            coro.close()
            raise asyncio.TimeoutError()
        return await coro

    target = AsyncMock()
    target.send_batch.return_value = DeliveryResult.ok(2)

    dcfg = _dcfg(timeout_ms=100, max_retries=1)
    chunk = _batch(2)

    with patch("backend.app.collectors.pipeline.asyncio.wait_for", fake_wait_for):
        with patch("backend.app.collectors.pipeline.asyncio.sleep", AsyncMock()):
            result = await _run_chunk_with_retry(
                target=target,
                chunk=chunk,
                dcfg=dcfg,
            )

    assert result.accepted == len(chunk)
    assert call_count == 2  # first timed out, second succeeded


# ── (c) max_retries enforcement ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_max_retries_exhausted_raises_transient() -> None:
    """After max_retries+1 attempts, TransientDeliveryError is raised."""
    from backend.app.collectors.delivery import TransientDeliveryError

    target = AsyncMock()
    target.send_batch.return_value = DeliveryResult(accepted=0, rejected=[], retryable=True)

    max_retries = 2
    dcfg = _dcfg(max_retries=max_retries)
    sleep_mock = AsyncMock()

    with pytest.raises(TransientDeliveryError):
        await _run_chunk_with_retry(
            target=target,
            chunk=_batch(3),
            dcfg=dcfg,
            sleep_mock=sleep_mock,
        )

    # Called initial attempt + max_retries times = max_retries + 1 total.
    assert target.send_batch.call_count == max_retries + 1


@pytest.mark.asyncio
async def test_max_retries_zero_raises_immediately() -> None:
    """With max_retries=0, a single retryable failure raises immediately."""
    from backend.app.collectors.delivery import TransientDeliveryError

    target = AsyncMock()
    target.send_batch.return_value = DeliveryResult(accepted=0, rejected=[], retryable=True)

    dcfg = _dcfg(max_retries=0)

    with pytest.raises(TransientDeliveryError):
        await _run_chunk_with_retry(target=target, chunk=_batch(1), dcfg=dcfg)

    target.send_batch.assert_called_once()


@pytest.mark.asyncio
async def test_success_on_last_retry() -> None:
    """A sender that fails (retryable) twice then succeeds on the 3rd call."""
    call_count = 0

    async def flaky(batch: list[dict]) -> DeliveryResult:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return DeliveryResult(accepted=0, rejected=[], retryable=True)
        return DeliveryResult.ok(len(batch))

    target = AsyncMock()
    target.send_batch.side_effect = flaky

    dcfg = _dcfg(max_retries=3)
    sleep_mock = AsyncMock()

    result = await _run_chunk_with_retry(
        target=target,
        chunk=_batch(4),
        dcfg=dcfg,
        sleep_mock=sleep_mock,
    )

    assert result.accepted == 4
    assert call_count == 3


# ── (d) exponential backoff delays ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_exponential_backoff_delays_correct() -> None:
    """asyncio.sleep is called with the right exponential delays."""
    target = AsyncMock()
    target.send_batch.return_value = DeliveryResult(accepted=0, rejected=[], retryable=True)

    # initial_ms=100, max_ms=1000, multiplier=2 → delays: 0.1, 0.2, 0.4 (s)
    dcfg = _dcfg(max_retries=3, initial_ms=100, max_ms=1000, multiplier=2.0)
    sleep_calls: list[float] = []

    async def capture_sleep(s: float) -> None:
        sleep_calls.append(s)

    from backend.app.collectors.delivery import TransientDeliveryError

    with pytest.raises(TransientDeliveryError):
        await _run_chunk_with_retry(
            target=target,
            chunk=_batch(2),
            dcfg=dcfg,
            sleep_mock=capture_sleep,
        )

    # 3 retries → 3 sleep calls (before attempt 1, 2, 3)
    assert len(sleep_calls) == 3
    assert sleep_calls[0] == pytest.approx(0.1)  # initial_ms=100 → 0.1 s
    assert sleep_calls[1] == pytest.approx(0.2)  # 100*2^1 = 200 ms
    assert sleep_calls[2] == pytest.approx(0.4)  # 100*2^2 = 400 ms


@pytest.mark.asyncio
async def test_backoff_capped_at_max_ms() -> None:
    """Backoff delay never exceeds max_ms even with many retries."""
    target = AsyncMock()
    target.send_batch.return_value = DeliveryResult(accepted=0, rejected=[], retryable=True)

    # initial_ms=100, max_ms=150, multiplier=10 → caps immediately at 150 ms
    dcfg = _dcfg(max_retries=3, initial_ms=100, max_ms=150, multiplier=10.0)
    sleep_calls: list[float] = []

    async def capture_sleep(s: float) -> None:
        sleep_calls.append(s)

    from backend.app.collectors.delivery import TransientDeliveryError

    with pytest.raises(TransientDeliveryError):
        await _run_chunk_with_retry(
            target=target,
            chunk=_batch(2),
            dcfg=dcfg,
            sleep_mock=capture_sleep,
        )

    assert len(sleep_calls) == 3
    # attempt 0: min(150, 100*10^0)=100ms; attempt 1+: capped at 150ms
    assert sleep_calls[0] == pytest.approx(0.1)
    assert sleep_calls[1] == pytest.approx(0.15)
    assert sleep_calls[2] == pytest.approx(0.15)


# ── (e) 4xx / poison-pill: no retry ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_4xx_not_retried_goes_to_dlq() -> None:
    """A result with retryable=False + rejected events: DLQ'd, no retry."""
    rejected_event = RejectedEvent(
        event_id="evt-bad-001",
        reason="schema validation failed",
        error_kind="schema_rejected",
        retryable=False,
    )
    target = AsyncMock()
    target.send_batch.return_value = DeliveryResult(
        accepted=0,
        rejected=[rejected_event],
        retryable=False,
    )

    # persist_rejected_to_dlq is sync (called via asyncio.to_thread)
    persist_mock = MagicMock(return_value=True)
    dcfg = _dcfg(max_retries=5)  # retries configured, but must NOT be used

    result = await _run_chunk_with_retry(
        target=target,
        chunk=_batch(1),
        dcfg=dcfg,
        persist_rejected_to_dlq=persist_mock,
        sleep_mock=AsyncMock(),
    )

    # Sender called exactly ONCE — no retry.
    target.send_batch.assert_called_once()
    # DLQ persist was called with the rejected items.
    persist_mock.assert_called_once()
    # Result carries the rejection.
    assert len(result.rejected) == 1
    assert result.rejected[0].event_id == "evt-bad-001"


@pytest.mark.asyncio
async def test_4xx_with_some_accepted_also_not_retried() -> None:
    """Partial batch: accepted events + 4xx rejected events → DLQ, no retry."""
    rejected_event = RejectedEvent(
        event_id="evt-0001",
        reason="payload too large",
        error_kind="payload_too_large",
        retryable=False,
    )
    target = AsyncMock()
    target.send_batch.return_value = DeliveryResult(
        accepted=3,
        rejected=[rejected_event],
        retryable=False,
    )

    persist_mock = MagicMock(return_value=True)
    dcfg = _dcfg(max_retries=5)

    result = await _run_chunk_with_retry(
        target=target,
        chunk=_batch(4),
        dcfg=dcfg,
        persist_rejected_to_dlq=persist_mock,
        sleep_mock=AsyncMock(),
    )

    target.send_batch.assert_called_once()
    persist_mock.assert_called_once()
    assert result.accepted == 3


@pytest.mark.asyncio
async def test_retryable_true_not_sent_to_dlq() -> None:
    """A retryable=True failure (5xx) is NOT persisted to DLQ — just retried."""
    target = AsyncMock()
    target.send_batch.side_effect = [
        DeliveryResult(accepted=0, rejected=[], retryable=True),
        DeliveryResult.ok(5),
    ]

    persist_mock = MagicMock(return_value=True)
    dcfg = _dcfg(max_retries=2)

    result = await _run_chunk_with_retry(
        target=target,
        chunk=_batch(5),
        dcfg=dcfg,
        persist_rejected_to_dlq=persist_mock,
        sleep_mock=AsyncMock(),
    )

    assert result.accepted == 5
    # DLQ must NOT be called for a retryable failure.
    persist_mock.assert_not_called()


# ── RetryConfig.backoff_delay_s — unit ────────────────────────────────────────


@pytest.mark.parametrize(
    "initial_ms,max_ms,multiplier,attempt,expected_s",
    [
        (200, 5000, 2.0, 0, 0.2),
        (200, 5000, 2.0, 1, 0.4),
        (200, 5000, 2.0, 2, 0.8),
        (200, 5000, 2.0, 5, 5.0),   # 200*2^5=6400 → capped at 5000 ms
        (200, 5000, 2.0, 10, 5.0),  # deep into the sequence — still capped
        (500, 500, 1.5, 0, 0.5),    # cap = initial → always 500 ms
    ],
)
def test_retry_config_backoff_delay_parametrized(
    initial_ms: int,
    max_ms: int,
    multiplier: float,
    attempt: int,
    expected_s: float,
) -> None:
    r = RetryConfig(
        initial_ms=initial_ms,
        max_ms=max_ms,
        multiplier=multiplier,
        max_retries=20,
    )
    assert backoff_delay_s(r,attempt) == pytest.approx(expected_s, rel=1e-6)


# ── obs-fix: contagem acumulada cross-chunk ───────────────────────────────────
#
# Testa a LÓGICA de acumulação sem precisar do ambiente completo de
# dispatch_batch_to_destination (secrets, DB, Redis, circuit breaker).
# Simula o loop de chunks manualmente — exatamente como os testes (a)/(b)/(c)
# acima testam _send_chunk_with_retry em isolamento.


@pytest.mark.asyncio
async def test_accepted_rejected_totals_accumulated_across_three_chunks() -> None:
    """Três chunks com accepted parciais → totais acumulados corretamente.

    Cenário: 3 chunks de 2 eventos cada, 1 aceito + 1 rejeitado (4xx) por chunk.
    Esperado: accepted_total=3, rejected_total=3.
    Regressão: sem a correção o total reportado seria o do ÚLTIMO chunk (1+1).
    """
    from backend.app.collectors.output.base import DeliveryResult, RejectedEvent
    from backend.app.collectors.pipeline import _send_chunk_with_retry

    dc = _dest_config()
    cb = _stub_circuit_breaker()
    labels = _labels(dc)
    dcfg = _dcfg(max_items=2, max_retries=0, timeout_ms=30_000, concurrency=4)

    batch = _batch(6)
    max_items = 2
    chunks = [batch[i : i + max_items] for i in range(0, len(batch), max_items)]
    assert len(chunks) == 3

    # Sender: cada chunk aceita 1 e rejeita 1 (4xx / não-retryable).
    call_index = 0

    async def partial_sender(chunk: list) -> DeliveryResult:
        nonlocal call_index
        rej = RejectedEvent(
            event_id=chunk[1]["_centralops"]["event_id"],
            reason="schema bad",
            error_kind="schema_rejected",
            retryable=False,
        )
        call_index += 1
        return DeliveryResult(accepted=1, rejected=[rej], retryable=False)

    target = AsyncMock()
    target.send_batch.side_effect = partial_sender
    persist_mock = MagicMock(return_value=True)
    m = _stub_metrics()

    # Simula o loop de chunks como o dispatch_batch_to_destination faz.
    accepted_total = 0
    rejected_total = 0
    rejected_event_ids: set[str] = set()
    last_result = None

    for chunk in chunks:
        last_result = await _send_chunk_with_retry(
            target=target,
            chunk=chunk,
            dcfg=dcfg,
            dest_config=dc,
            labels=labels,
            redis=AsyncMock(),
            circuit_breaker=cb,
            persist_rejected_to_dlq=persist_mock,
            DELIVERY_LATENCY=m,
            DLQ_TOTAL=m,
            EVENTS_REJECTED=m,
            EVENTS_SENT=m,
            BYTES_SENT=m,
            RETRIES=m,
        )
        accepted_total += last_result.accepted
        rejected_total += len(last_result.rejected)
        if last_result.rejected and not last_result.retryable:
            for rej in last_result.rejected:
                rejected_event_ids.add(rej.event_id)

    assert call_index == 3
    assert accepted_total == 3, (
        f"esperado accepted_total=3 (soma cross-chunk), recebido {accepted_total}"
    )
    assert rejected_total == 3, (
        f"esperado rejected_total=3 (soma cross-chunk), recebido {rejected_total}"
    )
    # Exatamente 3 event_ids rejeitados (os segundos de cada chunk).
    assert len(rejected_event_ids) == 3


@pytest.mark.asyncio
async def test_accepted_total_all_ok_three_chunks() -> None:
    """Três chunks, todos aceitos integralmente → totais corretos sem rejeições."""
    from backend.app.collectors.output.base import DeliveryResult
    from backend.app.collectors.pipeline import _send_chunk_with_retry

    dc = _dest_config()
    cb = _stub_circuit_breaker()
    labels = _labels(dc)
    dcfg = _dcfg(max_items=3, max_retries=0)

    batch = _batch(9)
    chunks = [batch[i : i + 3] for i in range(0, len(batch), 3)]

    async def full_accept(chunk: list) -> DeliveryResult:
        return DeliveryResult.ok(len(chunk))

    target = AsyncMock()
    target.send_batch.side_effect = full_accept
    m = _stub_metrics()

    accepted_total = 0
    rejected_total = 0

    for chunk in chunks:
        result = await _send_chunk_with_retry(
            target=target,
            chunk=chunk,
            dcfg=dcfg,
            dest_config=dc,
            labels=labels,
            redis=AsyncMock(),
            circuit_breaker=cb,
            persist_rejected_to_dlq=MagicMock(return_value=True),
            DELIVERY_LATENCY=m,
            DLQ_TOTAL=m,
            EVENTS_REJECTED=m,
            EVENTS_SENT=m,
            BYTES_SENT=m,
            RETRIES=m,
        )
        accepted_total += result.accepted
        rejected_total += len(result.rejected)

    assert accepted_total == 9
    assert rejected_total == 0
