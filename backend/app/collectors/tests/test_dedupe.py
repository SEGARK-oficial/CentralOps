"""RNF07 — idempotência por message_id."""

from __future__ import annotations


from ..state.dedupe import compute_message_id


def test_compute_message_id_prefers_native_id() -> None:
    assert compute_message_id({"id": "abc", "createdAt": "2026"}) == "abc"
    assert compute_message_id({"alertId": 42}) == "42"
    assert compute_message_id({"uuid": "u-1"}) == "u-1"


def test_compute_message_id_falls_back_to_sha256() -> None:
    event = {"no_id": True, "payload": {"x": 1}}
    msg_id = compute_message_id(event, fallback_fields=("no_id", "payload"))
    assert len(msg_id) == 64  # sha256 hex


def test_compute_message_id_is_deterministic() -> None:
    event = {"timestamp": "2026-04-23", "data": "xyz"}
    id1 = compute_message_id(event, fallback_fields=("timestamp", "data"))
    id2 = compute_message_id(event, fallback_fields=("timestamp", "data"))
    assert id1 == id2
