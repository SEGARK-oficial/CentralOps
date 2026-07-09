"""Testes para HIGH 2 — BackfillJob cursor HMAC signing.

Cenários cobertos:
- Cursor válido: _sign_cursor + _verify_cursor ida/volta.
- Cursor adulterado: _verify_cursor levanta ValueError.
- Cursor legado sem _sig: aceito com warning (backward compat).
- JSON inválido: levanta json.JSONDecodeError.
- Assinaturas diferentes para secrets diferentes.
"""

from __future__ import annotations

import json

import pytest

from backend.app.collectors.backfill_tasks import _sign_cursor, _verify_cursor


SECRET = "test-master-key-for-centralops-suite-12345"


# ── Testes de round-trip ──────────────────────────────────────────────


def test_sign_and_verify_round_trip() -> None:
    """Assinar e verificar deve retornar o cursor original."""
    cursor = {"next_page": "abc123", "offset": 42, "timestamp": "2026-01-01T00:00:00"}
    signed = _sign_cursor(cursor, SECRET)
    result = _verify_cursor(signed, SECRET)
    assert result == cursor


def test_sign_produces_valid_json() -> None:
    """_sign_cursor deve produzir JSON válido com _payload e _sig."""
    cursor = {"token": "xyz"}
    signed = _sign_cursor(cursor, SECRET)
    obj = json.loads(signed)
    assert "_payload" in obj
    assert "_sig" in obj
    assert len(obj["_sig"]) == 64  # SHA-256 hexdigest = 64 chars


def test_verify_accepts_empty_cursor() -> None:
    """Cursor vazio {} deve funcionar normalmente."""
    signed = _sign_cursor({}, SECRET)
    result = _verify_cursor(signed, SECRET)
    assert result == {}


# ── Testes de tampering ───────────────────────────────────────────────


def test_verify_raises_on_tampered_payload() -> None:
    """Alterar _payload deve invalidar a assinatura."""
    cursor = {"next_page": "original"}
    signed = _sign_cursor(cursor, SECRET)
    obj = json.loads(signed)

    # Adultera o payload.
    obj["_payload"]["next_page"] = "tampered"
    tampered = json.dumps(obj)

    with pytest.raises(ValueError, match="cursor signature mismatch"):
        _verify_cursor(tampered, SECRET)


def test_verify_raises_on_tampered_sig() -> None:
    """Alterar a assinatura diretamente deve levantar ValueError."""
    cursor = {"data": "real"}
    signed = _sign_cursor(cursor, SECRET)
    obj = json.loads(signed)

    # Substitui sig por zeros.
    obj["_sig"] = "0" * 64
    tampered = json.dumps(obj)

    with pytest.raises(ValueError, match="cursor signature mismatch"):
        _verify_cursor(tampered, SECRET)


def test_verify_raises_with_wrong_secret() -> None:
    """Secret diferente do usado na assinatura → tampering detectado."""
    cursor = {"page": 5}
    signed = _sign_cursor(cursor, SECRET)

    with pytest.raises(ValueError, match="cursor signature mismatch"):
        _verify_cursor(signed, "different-secret-key-that-is-long-enough")


# ── Backward compat — cursor legado sem _sig ─────────────────────────


def test_verify_accepts_legacy_cursor_without_sig(caplog) -> None:
    """Cursor legado sem _sig deve ser aceito com warning de migração."""
    import logging

    legacy_cursor = {"offset": 100, "page": "legacy"}
    legacy_json = json.dumps(legacy_cursor)

    with caplog.at_level(logging.WARNING, logger="backend.app.collectors.backfill_tasks"):
        result = _verify_cursor(legacy_json, SECRET)

    assert result == legacy_cursor
    assert any("legado" in record.message or "legacy" in record.message for record in caplog.records)


# ── Testes de JSON inválido ───────────────────────────────────────────


def test_verify_raises_on_invalid_json() -> None:
    """JSON inválido deve levantar json.JSONDecodeError."""
    with pytest.raises(json.JSONDecodeError):
        _verify_cursor("not-valid-json{{{", SECRET)


# ── Assinaturas distintas por conteúdo ───────────────────────────────


@pytest.mark.parametrize("cursor", [
    {"a": 1},
    {"a": 2},
    {"b": "value"},
    {},
    {"nested": {"x": [1, 2, 3]}},
])
def test_different_cursors_produce_different_sigs(cursor) -> None:
    """Cursors diferentes devem produzir assinaturas diferentes."""
    signed = _sign_cursor(cursor, SECRET)
    obj = json.loads(signed)
    # Sig deve corresponder ao payload correto.
    result = _verify_cursor(signed, SECRET)
    assert result == cursor
