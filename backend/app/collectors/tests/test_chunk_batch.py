"""Chunking por max_items E max_bytes.

Cobre o ``_chunk_batch`` (função pura do dispatcher): fecha um chunk ao atingir
``max_items`` OU ``max_bytes`` (o que vier primeiro), com ``max_bytes<=0``
desativando o teto de bytes e um evento isolado maior que ``max_bytes`` virando
o próprio chunk.
"""

from __future__ import annotations

from backend.app.collectors.output._fastjson import dumps_bytes
from backend.app.collectors.pipeline import _chunk_batch


def _ev(i: int, pad: int = 0) -> dict:
    return {"_centralops": {"event_id": f"e{i}"}, "raw": "x" * pad}


def test_empty_batch_returns_single_empty_chunk() -> None:
    # Sempre ≥1 chunk para o dispatcher exercer breaker/observability.
    assert _chunk_batch([], max_items=500, max_bytes=1_048_576) == [[]]


def test_splits_by_max_items_when_bytes_disabled() -> None:
    batch = [_ev(i) for i in range(10)]
    chunks = _chunk_batch(batch, max_items=3, max_bytes=0)
    assert [len(c) for c in chunks] == [3, 3, 3, 1]
    # Nada perdido, ordem preservada.
    assert [e["_centralops"]["event_id"] for c in chunks for e in c] == [
        f"e{i}" for i in range(10)
    ]


def test_max_bytes_zero_disables_byte_ceiling() -> None:
    # Eventos grandes, mas max_bytes=0 → só max_items manda.
    batch = [_ev(i, pad=5000) for i in range(4)]
    chunks = _chunk_batch(batch, max_items=10, max_bytes=0)
    assert len(chunks) == 1
    assert len(chunks[0]) == 4


def test_splits_by_max_bytes_before_max_items() -> None:
    # Cada evento ~1 KB; teto de bytes fecha o chunk antes de max_items.
    batch = [_ev(i, pad=1000) for i in range(6)]
    one = len(dumps_bytes(batch[0]))
    # Teto que cabe exatamente 2 eventos por chunk.
    max_bytes = one * 2 + 1
    chunks = _chunk_batch(batch, max_items=1000, max_bytes=max_bytes)
    assert all(len(c) <= 2 for c in chunks)
    assert sum(len(c) for c in chunks) == 6  # zero perda
    # Cada chunk respeita o teto de bytes.
    for c in chunks:
        assert sum(len(dumps_bytes(e)) for e in c) <= max_bytes


def test_single_oversized_event_becomes_its_own_chunk() -> None:
    # Um evento maior que max_bytes não pode ser fatiado abaixo de 1 → chunk só dele.
    big = _ev(0, pad=10_000)
    small = _ev(1, pad=10)
    chunks = _chunk_batch([small, big, small], max_items=1000, max_bytes=500)
    # O grande fica isolado; nada se perde.
    assert sum(len(c) for c in chunks) == 3
    assert any(len(c) == 1 and c[0]["_centralops"]["event_id"] == "e0" for c in chunks)


def test_closes_on_whichever_limit_first() -> None:
    # max_items pequeno domina mesmo com bytes folgados.
    batch = [_ev(i) for i in range(5)]
    chunks = _chunk_batch(batch, max_items=2, max_bytes=10_000_000)
    assert [len(c) for c in chunks] == [2, 2, 1]
