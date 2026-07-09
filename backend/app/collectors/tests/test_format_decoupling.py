"""A FORMATAÇÃO desacoplada do ENVIO (kinds legados).

Prova, para os TRÊS kinds legados
(``syslog_rfc3164``, ``syslog_rfc5424``, ``jsonl``), que o ``Destination.format``
é a FONTE ÚNICA do wire: os bytes que o caminho de ENVIO (``send_batch``)
enfileira/escreve são EXATAMENTE os de ``destination.format(env)``, a menos do
**framing** (que é, por design, responsabilidade do sender, não da formatação).

Distinção em relação a ``test_destination_registry.py``
-------------------------------------------------------
Aquele arquivo prova duas coisas SEPARADAMENTE: (a) ``dest.format`` == a função
pura, e (b) o wire do client direto == o wire da factory. Este arquivo prova o
elo que faltava: que o que o ``send_batch`` REALMENTE coloca no
writer/socket é derivado da MESMA ``format()`` — i.e. não há um ``format()``
paralelo decorativo enquanto o envio usa outra lógica.

Framing por kind (o que ``send_batch`` adiciona ao redor de ``format()``):
  - syslog_rfc3164: ``format(env).encode("utf-8") + b"\\n"`` (LF-delimited)
  - syslog_rfc5424: ``f"{len(line)} ".encode("ascii") + line`` (octet-counting)
  - jsonl:          ``format(env) + b"\\n"`` (NDJSON, LF por linha)

INVARIANTE: byte-idêntico. Qualquer drift entre envio e ``format()`` falha aqui.
"""

from __future__ import annotations

import os
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Defaults defensivos (paridade com os wire-contract tests irmãos).
os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

from backend.app.collectors.normalize.envelope import EnvelopeContext, build_envelope
from backend.app.collectors.output.destinations import registry
from backend.app.collectors.output.destinations.registry import (
    DestinationConfig,
    compute_config_version,
)


# ── Helpers ──────────────────────────────────────────────────────────────


def _sample_envelope() -> dict:
    ctx = EnvelopeContext(
        organization_id=7,
        integration_id=42,
        customer_id=7,
        vendor="sophos",
        stream="alerts",
        event_type="sophos.alert",
        mapping_version_id="v1",
        collector_host="centralops-test",
    )
    return build_envelope(
        raw={"id": "evt-1", "severity": "Critical"},
        normalized={"class_uid": 2004, "severity_id": 5},
        ctx=ctx,
        vendor_msg_id="evt-1",
    )


def _config(kind: str, **cfg) -> DestinationConfig:
    return DestinationConfig(
        destination_id=f"test-{kind}",
        kind=kind,
        config=cfg,
        config_version=compute_config_version(cfg, {}),
    )


def _capture_writer() -> MagicMock:
    writer = MagicMock()
    writer.is_closing.return_value = False
    writer.write = MagicMock()
    writer.drain = AsyncMock(return_value=None)
    writer.close = MagicMock()
    writer.wait_closed = AsyncMock(return_value=None)
    return writer


def _written_bytes(writer: MagicMock) -> List[bytes]:
    return [call.args[0] for call in writer.write.call_args_list]


# ── 1. syslog_rfc3164 — send path de-framed == format() ──────────────────


@pytest.mark.asyncio
async def test_rfc3164_send_payload_equals_format() -> None:
    """O que o ``Rfc3164JsonClient.send_batch`` escreve, menos o framing LF,
    é EXATAMENTE ``destination.format(env).encode('utf-8')``."""
    event = _sample_envelope()
    dest = registry.build(_config("syslog_rfc3164", host="siem.test.local", port=514))

    writer = _capture_writer()
    with patch("backend.app.collectors.output.rfc3164_sender.datetime") as dt, \
         patch("socket.gethostname", return_value="centralops-test"), \
         patch("os.getpid", return_value=4242):
        dt.utcnow.return_value.day = 1
        dt.utcnow.return_value.month = 6
        dt.utcnow.return_value.strftime.return_value = "00:00:00"

        with patch("asyncio.open_connection", return_value=(MagicMock(), writer)):
            await dest.send_batch([event])

        # ``format()`` deve ser avaliado sob o MESMO clock congelado do envio,
        # senão o timestamp (não-determinístico) divergiria por acidente.
        formatted = dest.format(event)

    written = _written_bytes(writer)
    assert written, "nenhum byte capturado — teste inválido"
    assert len(written) == 1

    # framing do rfc3164: a linha é seguida de um único LF.
    sent_line = written[0]
    assert sent_line.endswith(b"\n"), "sender deve adicionar framing LF"
    deframed = sent_line[:-1]  # remove o framing — sobra a FORMATAÇÃO pura

    # ``format()`` retorna str (a linha); o wire é a sua codificação UTF-8.
    assert isinstance(formatted, str)
    assert deframed == formatted.encode("utf-8")


# ── 2. syslog_rfc5424 — send path de-framed == format() ──────────────────


@pytest.mark.asyncio
async def test_rfc5424_send_payload_equals_format() -> None:
    """O que o ``SyslogTCPClient.send_batch`` escreve, menos o framing
    octet-counting (``"<len> "`` prefixado), é EXATAMENTE
    ``destination.format(env).encode('utf-8')``."""
    event = _sample_envelope()
    dest = registry.build(_config("syslog_rfc5424", host="siem.test.local", port=514))

    writer = _capture_writer()
    with patch("socket.gethostname", return_value="centralops-test"):
        with patch("asyncio.open_connection", return_value=(MagicMock(), writer)):
            await dest.send_batch([event])
        formatted = dest.format(event)

    written = _written_bytes(writer)
    assert written, "nenhum byte capturado — teste inválido"
    assert len(written) == 1

    frame = written[0]
    expected_line = formatted.encode("utf-8")
    # framing octet-counting do rfc5424: "<len> " + linha (RFC 6587).
    expected_prefix = f"{len(expected_line)} ".encode("ascii")
    assert frame.startswith(expected_prefix), "sender deve prefixar octet-count"

    deframed = frame[len(expected_prefix):]
    assert isinstance(formatted, str)
    assert deframed == expected_line


# ── 3. jsonl — send path de-framed == format() ───────────────────────────


@pytest.mark.asyncio
async def test_jsonl_send_payload_equals_format(tmp_path) -> None:
    """O que o ``JSONLWriter.send_batch`` grava no arquivo, menos o framing LF
    por linha, é EXATAMENTE ``destination.format(env)`` (que já é bytes)."""
    event = _sample_envelope()
    dest = registry.build(_config("jsonl", jsonl_dir=str(tmp_path)))

    await dest.send_batch([event])
    formatted = dest.format(event)

    # O writer agrupa por vendor → {tmp}/{vendor}/{YYYY-MM-DD}.log.
    written_files = list(tmp_path.rglob("*.log"))
    assert len(written_files) == 1, f"esperava 1 arquivo NDJSON, achei {written_files}"
    raw = written_files[0].read_bytes()

    # framing do jsonl: exatamente um LF por linha.
    assert raw.endswith(b"\n"), "writer deve adicionar framing LF por linha"
    assert raw.count(b"\n") == 1, "um evento → uma linha NDJSON"
    deframed = raw[:-1]

    # ``format()`` do jsonl retorna bytes (não str).
    assert isinstance(formatted, (bytes, bytearray))
    assert deframed == formatted


# ── 4. Identidade da função: send path e format() compartilham UMA def ───


def test_legacy_kinds_share_single_format_function() -> None:
    """Garante que NÃO há duas implementações: o objeto-função usado pelo
    caminho de envio é o MESMO que ``formatters``/``format()`` expõe."""
    from backend.app.collectors.output import formatters
    from backend.app.collectors.output import rfc3164_sender, syslog_sender
    from backend.app.collectors.output.destinations import jsonl as jsonl_dest
    from backend.app.collectors.output import jsonl_writer

    # rfc3164: a função chamada inline no send_batch é a re-exportada.
    assert rfc3164_sender.format_rfc3164 is formatters.format_rfc3164
    # rfc5424: idem.
    assert syslog_sender.format_rfc5424 is formatters.format_rfc5424
    # jsonl: a antiga duplicação (writer inline vs _jsonl_format) agora é UMA.
    assert jsonl_writer.format_jsonl is formatters.format_jsonl
    assert jsonl_dest._jsonl_format is formatters.format_jsonl
