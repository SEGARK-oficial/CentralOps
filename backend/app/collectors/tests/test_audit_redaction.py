"""Redação do ring de captura/auditoria (`audit_buffer._redact`).

O ring grava payload de cliente e é lido pelo inspetor de captura e pelo export.
Estes testes travam as duas camadas: redação por NOME de campo de segredo e
scrubbing por VALOR (segredo embutido em campo de nome inocente).
"""
from __future__ import annotations

from backend.app.collectors.audit_buffer import _redact


def test_redacts_secret_field_by_name():
    out = _redact({"password": "hunter2", "api_key": "abc", "user": "alice"})
    assert out["password"] == "[REDACTED]"
    assert out["api_key"] == "[REDACTED]"
    # PII legítima do próprio tenant NÃO é apagada no ring (o admin da org tem
    # direito de vê-la no troubleshooting; a máscara de export é camada separada).
    assert out["user"] == "alice"


def test_scrubs_pat_embedded_in_an_innocent_value():
    # antes: um PAT dentro de uma URL num campo "url" ia em claro para o ring.
    out = _redact({"url": "https://api.example/cb?token=copsk_" + "a" * 32})
    assert "copsk_" + "a" * 32 not in out["url"]
    assert "[REDACTED]" in out["url"]


def test_scrubs_vault_token_in_nested_value():
    out = _redact({"log": {"msg": "auth with hvs.CAESIabcdefghij failed"}})
    assert "hvs.CAESIabcdefghij" not in out["log"]["msg"]


def test_scrubs_secrets_inside_lists():
    out = _redact({"args": ["--token", "copsk_" + "b" * 40]})
    assert all("copsk_" + "b" * 40 != v for v in out["args"])


def test_preserves_non_secret_values_and_structure():
    payload = {"rule": {"level": 5}, "agent": {"name": "srv-01"}, "count": 3}
    assert _redact(payload) == payload
