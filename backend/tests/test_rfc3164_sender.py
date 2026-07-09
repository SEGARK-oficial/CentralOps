"""Testes para Rfc3164JsonClient + format_rfc3164.

Cobre:
- Formato correto da linha RFC 3164.
- Prematch Wazuh (``^{`` após ``APP[PID]: ``).
- Unicode / caracteres especiais.
- ValueError em evento vazio/não-dict.
- send_batch via mock de open_connection (framing LF).
- Reconexão quando writer está fechado.
- TLS: ctx.minimum_version = TLSv1_2.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import ssl
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.collectors.output.rfc3164_sender import (
    PRI_INFO,
    Rfc3164JsonClient,
    format_rfc3164,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

SIMPLE_EVENT: Dict[str, Any] = {
    "_centralops": {"vendor": "sophos", "integration_id": 42},
    # severity_id=1 (informational) → PRI=134 (PRI_INFO).
    # Mapeamento OCSF→syslog em normalize/severity_map.py.
    "normalized": {"severity_id": 1},
    "raw": {"id": "abc123"},
}


# ── format_rfc3164 ────────────────────────────────────────────────────────────


def test_format_rfc3164_basic_event() -> None:
    """Linha deve ter: <134>, timestamp RFC 3164, hostname, APP[pid], JSON."""
    line = format_rfc3164(SIMPLE_EVENT)

    # Prefixo PRI
    assert line.startswith(f"<{PRI_INFO}>"), f"PRI incorreto: {line[:10]}"

    # Timestamp: "Mmm dd HH:MM:SS" ou "Mmm  d HH:MM:SS" (dia 1 dígito)
    # Exemplo: "Jan  6 14:05:01" ou "Apr 26 09:30:00"
    ts_pattern = r"^<\d+>[A-Z][a-z]{2}\s+\d{1,2} \d{2}:\d{2}:\d{2}"
    assert re.match(ts_pattern, line), f"Timestamp não casou: {line[:40]}"

    # Deve terminar com JSON válido
    json_start = line.index("{", line.index("]: ") + 3)
    payload = json.loads(line[json_start:])
    assert payload["_centralops"]["vendor"] == "sophos"


def test_format_rfc3164_starts_with_open_brace_after_colon() -> None:
    """Wazuh JSON_Decoder usa prematch ``^{``.

    O MSG (parte após ``APP[pid]: ``) deve começar com ``{`` para que o
    decoder nativo do Wazuh identifique a mensagem como JSON.
    """
    line = format_rfc3164(SIMPLE_EVENT)
    # Localiza o marcador "centralops[pid]: "
    marker = "]: "
    pos = line.index(marker)
    msg_start = line[pos + len(marker):]
    assert msg_start.startswith("{"), (
        f"MSG não começa com '{{': ...{msg_start[:40]!r}"
    )


def test_format_rfc3164_handles_unicode() -> None:
    """Acentos e caracteres especiais não devem quebrar a serialização."""
    event = {"message": "Alerta crítico — São Paulo / München 中文", "severity": 3}
    line = format_rfc3164(event)
    # Deve conter o texto Unicode intacto (ensure_ascii=False)
    assert "crítico" in line
    assert "中文" in line


@pytest.mark.parametrize("bad_input", [
    {},
    None,
    "string",
    42,
    [],
])
def test_format_rfc3164_raises_on_empty_or_non_dict(bad_input: Any) -> None:
    """ValueError esperado para evento vazio ou não-dict."""
    with pytest.raises(ValueError):
        format_rfc3164(bad_input)  # type: ignore[arg-type]


# ── Rfc3164JsonClient.send_batch ──────────────────────────────────────────────


def _make_mock_writer() -> MagicMock:
    """Cria mock de asyncio.StreamWriter com buffer capturável."""
    writer = MagicMock(spec=asyncio.StreamWriter)
    writer.is_closing.return_value = False
    writer.write = MagicMock()
    writer.drain = AsyncMock(return_value=None)
    writer.close = MagicMock()
    writer.wait_closed = AsyncMock(return_value=None)
    return writer


@pytest.mark.asyncio
async def test_send_batch_writes_lf_delimited() -> None:
    """send_batch deve serializar cada evento e separar com \\n."""
    mock_writer = _make_mock_writer()
    mock_reader = MagicMock(spec=asyncio.StreamReader)

    events = [
        {"id": 1, "msg": "first"},
        {"id": 2, "msg": "second"},
    ]

    client = Rfc3164JsonClient(host="127.0.0.1", port=514)

    with patch("asyncio.open_connection", return_value=(mock_reader, mock_writer)):
        await client.send_batch(events)

    # Coleta todos os bytes escritos
    written_calls = mock_writer.write.call_args_list
    assert len(written_calls) == 2, f"Esperado 2 writes, obtido {len(written_calls)}"

    for call in written_calls:
        raw: bytes = call[0][0]
        # Cada linha deve terminar com \n (LF-delimited)
        assert raw.endswith(b"\n"), f"Linha não termina com \\n: {raw[-5:]!r}"
        # Decode deve produzir linha RFC 3164 válida
        decoded = raw[:-1].decode("utf-8")
        assert decoded.startswith(f"<{PRI_INFO}>")
        # JSON válido no final
        json_start = decoded.index("{", decoded.index("]: ") + 3)
        payload = json.loads(decoded[json_start:])
        assert payload["id"] in (1, 2)


@pytest.mark.asyncio
async def test_send_batch_reconnects_on_closed_writer() -> None:
    """Quando writer.is_closing() == True, deve reconectar antes de escrever."""
    closed_writer = _make_mock_writer()
    closed_writer.is_closing.return_value = True  # writer "fechado"

    new_writer = _make_mock_writer()
    mock_reader = MagicMock(spec=asyncio.StreamReader)

    client = Rfc3164JsonClient(host="127.0.0.1", port=514)
    client._writer = closed_writer  # injeta writer fechado

    with patch("asyncio.open_connection", return_value=(mock_reader, new_writer)) as mock_conn:
        await client.send_batch([{"event": "reconnect_test"}])

    # open_connection deve ter sido chamado (reconexão)
    mock_conn.assert_called_once()
    # Deve ter escrito no novo writer
    new_writer.write.assert_called_once()


@pytest.mark.asyncio
async def test_send_batch_empty_is_noop() -> None:
    """send_batch com lista vazia não deve conectar nem escrever."""
    client = Rfc3164JsonClient(host="127.0.0.1", port=514)
    with patch("asyncio.open_connection") as mock_conn:
        await client.send_batch([])
    mock_conn.assert_not_called()


# ── TLS ───────────────────────────────────────────────────────────────────────


def test_use_tls_creates_ssl_context_with_min_tls_1_2() -> None:
    """Quando use_tls=True, o SSLContext deve exigir TLSv1.2 no mínimo."""
    client = Rfc3164JsonClient(host="wazuh.exemplo.com", port=6514, use_tls=True)
    assert client.ctx is not None, "ctx deve ser criado quando use_tls=True"
    assert client.ctx.minimum_version == ssl.TLSVersion.TLSv1_2


def test_no_tls_does_not_create_ssl_context() -> None:
    """Quando use_tls=False (default), ctx deve ser None."""
    client = Rfc3164JsonClient(host="192.168.3.211", port=514, use_tls=False)
    assert client.ctx is None
