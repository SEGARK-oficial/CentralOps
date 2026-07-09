"""registry de destinos: kinds vendor-neutros + paridade byte-a-byte.

Garante:
- Os kinds built-in (syslog_rfc3164, syslog_rfc5424, jsonl, splunk_hec)
  se auto-registram no import.
- O catálogo (``describe_all``) expõe config_schema p/ a UI.
- A factory do ``syslog_rfc3164`` constrói um ``LegacyTargetDestination``
  com ``Rfc3164JsonClient`` como target.
- A factory do ``syslog_rfc5424`` constrói um ``LegacyTargetDestination``
  com ``SyslogTCPClient`` como target.
- O wire de um evento real via ``syslog_rfc3164`` é byte-a-byte igual ao
  produzido pelo ``Rfc3164JsonClient`` direto (paridade de formatação).
- O wire de um evento real via ``syslog_rfc5424`` é byte-a-byte igual ao
  produzido pelo ``SyslogTCPClient`` direto.
"""

from __future__ import annotations

from typing import List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.collectors.normalize.envelope import EnvelopeContext, build_envelope
from backend.app.collectors.output.base import (
    Destination,
    DeliveryResult,
    LegacyTargetDestination,
)
from backend.app.collectors.output.rfc3164_sender import Rfc3164JsonClient, format_rfc3164
from backend.app.collectors.output.syslog_sender import SyslogTCPClient, format_rfc5424
from backend.app.collectors.output.destinations import registry
from backend.app.collectors.output.destinations.registry import (
    DestinationConfig,
    compute_config_version,
)


# ── Helpers ─────────────────────────────────────────────────────────────


def _syslog_rfc3164_config(
    host: str = "siem.test.local",
    port: int = 514,
    use_tls: bool = False,
) -> DestinationConfig:
    config = {"host": host, "port": port, "use_tls": use_tls, "ca_bundle": None}
    return DestinationConfig(
        destination_id="test-rfc3164",
        kind="syslog_rfc3164",
        config=config,
        config_version=compute_config_version(config, {}),
    )


def _syslog_rfc5424_config(
    host: str = "siem.test.local",
    port: int = 514,
    use_tls: bool = False,
) -> DestinationConfig:
    config = {"host": host, "port": port, "use_tls": use_tls, "ca_bundle": None}
    return DestinationConfig(
        destination_id="test-rfc5424",
        kind="syslog_rfc5424",
        config=config,
        config_version=compute_config_version(config, {}),
    )


def _splunk_hec_config() -> DestinationConfig:
    config = {"url": "https://splunk.test.local:8088", "sourcetype": "centralops"}
    return DestinationConfig(
        destination_id="test-splunk",
        kind="splunk_hec",
        config=config,
        config_version=compute_config_version(config, {}),
    )


def _sample_envelope() -> dict:
    ctx = EnvelopeContext(
        vendor="sophos",
        integration_id=1,
        customer_id=7,
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


# ── Registro ────────────────────────────────────────────────────────────


def test_all_builtins_registered_on_import() -> None:
    """Os quatro kinds built-in devem estar no registry após o import."""
    kinds = set(registry.all_kinds())
    assert {"syslog_rfc3164", "syslog_rfc5424", "jsonl", "splunk_hec"}.issubset(kinds)


def test_wazuh_syslog_no_longer_registered() -> None:
    """O kind monolítico 'wazuh_syslog' foi removido — não deve existir."""
    assert "wazuh_syslog" not in registry.all_kinds()


def test_get_raises_keyerror_for_unknown() -> None:
    with pytest.raises(KeyError, match="nenhum destino registrado"):
        registry.get("inexistente")


def test_describe_all_exposes_config_schemas() -> None:
    cat = {c["kind"]: c for c in registry.describe_all()}

    # syslog_rfc3164
    assert "syslog_rfc3164" in cat
    schema_3164 = cat["syslog_rfc3164"]["config_schema"]
    assert "host" in schema_3164["properties"]
    assert "port" in schema_3164["properties"]
    assert cat["syslog_rfc3164"]["label"]

    # syslog_rfc5424
    assert "syslog_rfc5424" in cat
    schema_5424 = cat["syslog_rfc5424"]["config_schema"]
    assert "host" in schema_5424["properties"]
    assert "port" in schema_5424["properties"]
    assert cat["syslog_rfc5424"]["label"]

    # splunk_hec
    assert "splunk_hec" in cat
    schema_hec = cat["splunk_hec"]["config_schema"]
    assert "url" in schema_hec["properties"]
    assert "index" in schema_hec["properties"]
    assert cat["splunk_hec"]["required_secrets"] == ["hec_token"]


# ── Factory → Destination protocol ──────────────────────────────────────


def test_build_rfc3164_returns_destination_protocol() -> None:
    dest = registry.build(_syslog_rfc3164_config())
    assert isinstance(dest, Destination)
    assert dest.kind == "syslog_rfc3164"


def test_build_rfc5424_returns_destination_protocol() -> None:
    dest = registry.build(_syslog_rfc5424_config())
    assert isinstance(dest, Destination)
    assert dest.kind == "syslog_rfc5424"


def test_build_splunk_hec_returns_destination_protocol() -> None:
    from backend.app.collectors.output.splunk_hec_sender import SplunkHecClient

    dest = registry.build(_splunk_hec_config())
    assert isinstance(dest, SplunkHecClient)
    assert dest.kind == "splunk_hec"


# ── Factory escolhe o sender correto ────────────────────────────────────


def test_rfc3164_factory_builds_rfc3164_client() -> None:
    dest = registry.build(_syslog_rfc3164_config())
    assert isinstance(dest, LegacyTargetDestination)
    assert isinstance(dest.legacy_target, Rfc3164JsonClient)


def test_rfc5424_factory_builds_syslog_tcp_client() -> None:
    dest = registry.build(_syslog_rfc5424_config())
    assert isinstance(dest, LegacyTargetDestination)
    assert isinstance(dest.legacy_target, SyslogTCPClient)


# ── Probe fail-closed sem host ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_rfc3164_probe_fails_closed_without_host() -> None:
    """Sem host configurado, test() deve falhar com mensagem clara."""
    config = {"host": None, "port": 514}
    dest = registry.build(
        DestinationConfig(
            destination_id="no-host",
            kind="syslog_rfc3164",
            config=config,
            config_version=compute_config_version(config, {}),
        )
    )
    result = await dest.test()
    assert result.ok is False
    assert "host" in result.detail.lower()


@pytest.mark.asyncio
async def test_rfc5424_probe_fails_closed_without_host() -> None:
    config = {"host": None, "port": 514}
    dest = registry.build(
        DestinationConfig(
            destination_id="no-host",
            kind="syslog_rfc5424",
            config=config,
            config_version=compute_config_version(config, {}),
        )
    )
    result = await dest.test()
    assert result.ok is False
    assert "host" in result.detail.lower()


# ── Paridade byte-a-byte do formatter ────────────────────────────────────


def test_rfc3164_format_bytes_match_format_rfc3164() -> None:
    """``dest.format(event)`` deve retornar exatamente o que ``format_rfc3164``
    retorna — paridade de formatação (canônico → wire)."""
    event = _sample_envelope()
    dest = registry.build(_syslog_rfc3164_config())

    with patch("socket.gethostname", return_value="centralops-test"), \
         patch("os.getpid", return_value=4242), \
         patch("backend.app.collectors.output.rfc3164_sender.datetime") as dt:
        dt.utcnow.return_value.day = 1
        dt.utcnow.return_value.month = 6
        dt.utcnow.return_value.strftime.return_value = "00:00:00"

        direct = format_rfc3164(event)
        via_dest = dest.format(event)

    assert direct == via_dest


def test_rfc5424_format_bytes_match_format_rfc5424() -> None:
    """``dest.format(event)`` deve retornar exatamente o que ``format_rfc5424``
    retorna — paridade de formatação (canônico → wire)."""
    event = _sample_envelope()
    dest = registry.build(_syslog_rfc5424_config())

    with patch("socket.gethostname", return_value="centralops-test"):
        direct = format_rfc5424(event)
        via_dest = dest.format(event)

    assert direct == via_dest


# ── Paridade byte-a-byte do wire (send_batch) ────────────────────────────


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


@pytest.mark.asyncio
async def test_rfc3164_wire_bytes_identical_direct_vs_factory() -> None:
    """O mesmo evento, enviado por um Rfc3164JsonClient direto e pelo
    destino construído pela factory, produz bytes idênticos no wire."""
    event = _sample_envelope()

    direct_client = Rfc3164JsonClient(host="siem.test.local", port=514)
    dest = registry.build(_syslog_rfc3164_config())

    w1, w2 = _capture_writer(), _capture_writer()

    with patch("backend.app.collectors.output.rfc3164_sender.datetime") as dt, \
         patch("socket.gethostname", return_value="centralops-test"), \
         patch("os.getpid", return_value=4242):
        dt.utcnow.return_value.day = 1
        dt.utcnow.return_value.month = 6
        dt.utcnow.return_value.strftime.return_value = "00:00:00"

        with patch("asyncio.open_connection", return_value=(MagicMock(), w1)):
            await direct_client.send_batch([event])
        with patch("asyncio.open_connection", return_value=(MagicMock(), w2)):
            result = await dest.send_batch([event])

    assert _written_bytes(w1) == _written_bytes(w2)
    assert _written_bytes(w1), "nenhum byte capturado — teste inválido"
    assert isinstance(result, DeliveryResult)
    assert result.accepted == 1 and result.all_accepted


@pytest.mark.asyncio
async def test_rfc5424_wire_bytes_identical_direct_vs_factory() -> None:
    """O mesmo evento, enviado por um SyslogTCPClient direto e pelo
    destino construído pela factory, produz bytes idênticos no wire."""
    event = _sample_envelope()

    direct_client = SyslogTCPClient(host="siem.test.local", port=514, use_tls=False)
    dest = registry.build(_syslog_rfc5424_config())

    w1, w2 = _capture_writer(), _capture_writer()

    with patch("socket.gethostname", return_value="centralops-test"):
        with patch("asyncio.open_connection", return_value=(MagicMock(), w1)):
            await direct_client.send_batch([event])
        with patch("asyncio.open_connection", return_value=(MagicMock(), w2)):
            result = await dest.send_batch([event])

    assert _written_bytes(w1) == _written_bytes(w2)
    assert _written_bytes(w1), "nenhum byte capturado — teste inválido"
    assert isinstance(result, DeliveryResult)
    assert result.accepted == 1 and result.all_accepted


# ── compute_config_version ──────────────────────────────────────────────


def test_config_version_is_stable_and_sensitive() -> None:
    a = compute_config_version({"x": 1}, {"y": 2})
    b = compute_config_version({"x": 1}, {"y": 2})
    c = compute_config_version({"x": 2}, {"y": 2})
    assert a == b
    assert a != c
    assert len(a) == 12
