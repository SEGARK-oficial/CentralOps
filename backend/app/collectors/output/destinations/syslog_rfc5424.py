"""Kind ``syslog_rfc5424`` — destino Syslog RFC 5424 com STRUCTURED-DATA.

Substitui o caminho RFC 5424 do kind monolítico ``wazuh_syslog``.
Adequado para SIEMs que suportam STRUCTURED-DATA (Graylog, Splunk Heavy
Forwarder com syslog input, QRadar). **Não recomendado para Wazuh Manager
vanilla**: o JSON_Decoder nativo não casa em linhas RFC 5424 por causa do
framing octet-counting + cabeçalho estruturado (issue #2038). Para Wazuh,
use ``syslog_rfc3164``.

**Lógica composite "both" (syslog + jsonl) foi descartada intencionalmente.**
Fan-out agora é responsabilidade do roteador/dispatcher.

Ativo quando há um destino deste kind configurado (multi-destino é GA).
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any, Optional

from pydantic import BaseModel, Field

from ..base import (
    Destination,
    LegacyTargetDestination,
    TestResult,
)
# FONTE ÚNICA do wire: ``format_rfc5424`` vem do módulo de formatadores —
# o MESMO objeto que ``SyslogTCPClient.send_batch`` usa no caminho de envio.
from ..formatters import format_rfc5424
from ..syslog_sender import SyslogTCPClient
from .registry import DestinationConfig, DestinationRegistration, register

KIND = "syslog_rfc5424"


class SyslogRfc5424Config(BaseModel):
    """Schema de config do destino Syslog RFC 5424.

    Campos expostos no catálogo da UI (``GET /collectors/destination-types``).
    ``ca_bundle`` é um path no filesystem do collector, não um secret cifrado.
    """

    host: Optional[str] = Field(default=None, description="Endereço do servidor syslog")
    port: int = Field(default=514, description="Porta TCP")
    use_tls: bool = Field(default=False, description="Ativar TLS 1.2+")
    ca_bundle: Optional[str] = Field(
        default=None, description="Path do CA bundle PEM (apenas com use_tls=True)"
    )


def _make_probe(cfg: SyslogRfc5424Config):
    """Cria um probe TCP assíncrono para o host:port configurado."""

    async def _probe() -> TestResult:
        if not cfg.host:
            return TestResult.failed(
                "host vazio — syslog_rfc5424 exige host para enviar"
            )
        try:
            fut = asyncio.open_connection(cfg.host, cfg.port)
            reader, writer = await asyncio.wait_for(fut, timeout=5.0)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # pragma: no cover — best-effort close
                pass
            return TestResult.passed(
                f"conexão TCP ok: {cfg.host}:{cfg.port}"
            )
        except (asyncio.TimeoutError, OSError, socket.gaierror) as exc:
            return TestResult.failed(
                f"falha de conexão {cfg.host}:{cfg.port}: {exc}"
            )

    return _probe


def _factory(config: DestinationConfig, secrets: Optional[Any] = None) -> Destination:
    cfg = SyslogRfc5424Config(**dict(config.config or {}))
    target = SyslogTCPClient(
        host=cfg.host or "",
        port=cfg.port,
        ca_bundle=cfg.ca_bundle,
        use_tls=cfg.use_tls,
    )
    return LegacyTargetDestination(
        KIND,
        target,
        formatter=format_rfc5424,
        probe=_make_probe(cfg),
    )


register(
    DestinationRegistration(
        kind=KIND,
        factory=_factory,
        config_schema=SyslogRfc5424Config,
        default_queue="dispatch.syslog",
        capabilities=frozenset({"tls", "batch", "test"}),
        required_secrets=(),  # ca_bundle é path, não secret
        label="Syslog RFC 5424 (Structured Data)",
        # Socket TCP único por target — concorrência baixa evita interleave.
        delivery_defaults={"concurrency": 2},
        # Campos de catálogo self-describing (galeria de destinos).
        category="Rede / Syslog",
        icon_id="syslog",
        tier="stable",
        order=100,
        description="Syslog estruturado IETF (RFC 5424) com structured-data, via UDP/TCP.",
    )
)
