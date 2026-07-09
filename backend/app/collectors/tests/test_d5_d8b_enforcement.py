"""Enforcement de ``retry.enabled`` / ``retry.max_elapsed_ms``
e emissão das séries ``BYTES_SENT`` / ``RETRIES`` no dispatcher
(``pipeline._send_chunk_with_retry``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.collectors.output.base import DeliveryResult
from backend.app.collectors.output.delivery_config import (
    DeliveryConfig,
    RetryConfig,
)


def _metric() -> MagicMock:
    m = MagicMock()
    m.labels.return_value = m
    return m


def _cb() -> MagicMock:
    cb = MagicMock()
    cb.check_for_config = AsyncMock(return_value=None)
    cb.record_failure_for_config = AsyncMock(return_value=None)
    cb.record_success_for_config = AsyncMock(return_value=None)
    return cb


def _dest() -> MagicMock:
    d = MagicMock()
    d.destination_id = "d1"
    d.kind = "splunk_hec"
    return d


class _SeqTarget:
    """``send_batch`` percorre uma sequência de resultados (retryable→ok)."""

    def __init__(self, results: list[DeliveryResult]) -> None:
        self._results = list(results)
        self.calls = 0

    async def send_batch(self, batch: list) -> DeliveryResult:
        self.calls += 1
        return self._results[min(self.calls - 1, len(self._results) - 1)]


async def _run(target, dcfg, *, bytes_sent, retries, sleep_mock=None):
    from backend.app.collectors.pipeline import _send_chunk_with_retry

    kwargs = dict(
        target=target,
        chunk=[{"_centralops": {"event_id": "e1"}, "raw": "payload"}],
        dcfg=dcfg,
        dest_config=_dest(),
        labels={"destination_id": "d1", "kind": "splunk_hec"},
        redis=AsyncMock(),
        circuit_breaker=_cb(),
        persist_rejected_to_dlq=MagicMock(return_value=True),
        DELIVERY_LATENCY=_metric(),
        DLQ_TOTAL=_metric(),
        EVENTS_REJECTED=_metric(),
        EVENTS_SENT=_metric(),
        BYTES_SENT=bytes_sent,
        RETRIES=retries,
    )
    if sleep_mock is not None:
        with patch("backend.app.collectors.pipeline.asyncio.sleep", sleep_mock):
            return await _send_chunk_with_retry(**kwargs)
    return await _send_chunk_with_retry(**kwargs)


@pytest.mark.asyncio
async def test_bytes_sent_incremented_on_accept() -> None:
    bytes_sent, retries = _metric(), _metric()
    target = _SeqTarget([DeliveryResult.ok(1)])
    await _run(target, DeliveryConfig(), bytes_sent=bytes_sent, retries=retries)
    inc = bytes_sent.labels.return_value.inc
    assert inc.called
    assert inc.call_args.args[0] > 0  # bytes do chunk (wire-proxy) > 0


@pytest.mark.asyncio
async def test_retries_counted_one_per_attempt() -> None:
    bytes_sent, retries = _metric(), _metric()
    target = _SeqTarget(
        [
            DeliveryResult(accepted=0, rejected=[], retryable=True),
            DeliveryResult(accepted=0, rejected=[], retryable=True),
            DeliveryResult.ok(1),
        ]
    )
    dcfg = DeliveryConfig(
        retry=RetryConfig(max_retries=5, initial_ms=10, max_ms=20)
    )
    await _run(
        target, dcfg, bytes_sent=bytes_sent, retries=retries, sleep_mock=AsyncMock()
    )
    # 2 retries (attempts 1 e 2) antes do sucesso no 3º send.
    assert retries.labels.return_value.inc.call_count == 2
    assert target.calls == 3


@pytest.mark.asyncio
async def test_retry_disabled_sends_once() -> None:
    from backend.app.collectors.delivery import TransientDeliveryError

    bytes_sent, retries = _metric(), _metric()
    target = _SeqTarget([DeliveryResult(accepted=0, rejected=[], retryable=True)])
    dcfg = DeliveryConfig(retry=RetryConfig(enabled=False, max_retries=5))
    with pytest.raises(TransientDeliveryError):
        await _run(
            target, dcfg, bytes_sent=bytes_sent, retries=retries, sleep_mock=AsyncMock()
        )
    assert target.calls == 1  # entrega única, sem backoff
    assert retries.labels.return_value.inc.call_count == 0


@pytest.mark.asyncio
async def test_max_elapsed_ms_stops_retrying() -> None:
    from backend.app.collectors.delivery import TransientDeliveryError

    bytes_sent, retries = _metric(), _metric()
    target = _SeqTarget([DeliveryResult(accepted=0, rejected=[], retryable=True)])
    dcfg = DeliveryConfig(
        retry=RetryConfig(max_retries=5, initial_ms=10, max_ms=20, max_elapsed_ms=1000)
    )
    # time.monotonic: loop_start=0, started/elapsed do attempt 0 = 0, e na checagem
    # do attempt 1 retorna 100s → excede max_elapsed_ms (1s) → para de re-tentar.
    monotonic = MagicMock(side_effect=[0.0, 0.0, 0.0] + [100.0] * 30)
    with patch("backend.app.collectors.pipeline.time.monotonic", monotonic):
        with pytest.raises(TransientDeliveryError):
            await _run(
                target,
                dcfg,
                bytes_sent=bytes_sent,
                retries=retries,
                sleep_mock=AsyncMock(),
            )
    assert target.calls == 1  # só o attempt 0; o teto interrompe antes do 2º send
    assert retries.labels.return_value.inc.call_count == 0  # parou ANTES do inc
