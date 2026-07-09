"""Kind ``syslog_rfc3164`` — destino Syslog RFC 3164 com JSON no MSG.

Substitui o kind monolítico ``wazuh_syslog`` para o caminho RFC 3164.
O nome intencionalmente não carrega "wazuh": um destino é "syslog", não
"wazuh". O Wazuh é um consumidor; este kind serve qualquer SIEM que aceite
RFC 3164 (BSD syslog) com JSON puro no campo MSG, usando compatibilidade com
o JSON_Decoder nativo do Wazuh (prematch ``^{``).

**Lógica composite "both" (syslog + jsonl em paralelo) foi descartada
intencionalmente.** O fan-out para múltiplos destinos agora é responsabilidade
do roteador/dispatcher. Para gravar JSONL em paralelo,
crie duas entradas na tabela ``routes``: uma kind=syslog_rfc3164 e outra
kind=jsonl.

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
# FONTE ÚNICA do wire: ``format_rfc3164`` vem do módulo de formatadores —
# o MESMO objeto que ``Rfc3164JsonClient.send_batch`` usa no caminho de envio.
from ..formatters import format_rfc3164
from ..rfc3164_sender import Rfc3164JsonClient
from .registry import DestinationConfig, DestinationRegistration, register

KIND = "syslog_rfc3164"


class SyslogRfc3164Config(BaseModel):
    """Schema de config do destino Syslog RFC 3164.

    Campos expostos no catálogo da UI (``GET /collectors/destination-types``).
    ``ca_bundle`` é um path no filesystem do collector, não um secret cifrado
    (igual ao modelo atual do legacy Wazuh).
    """

    host: Optional[str] = Field(default=None, description="Endereço do servidor syslog")
    port: int = Field(default=514, description="Porta TCP")
    use_tls: bool = Field(default=False, description="Ativar TLS 1.2+")
    ca_bundle: Optional[str] = Field(
        default=None, description="Path do CA bundle PEM (apenas com use_tls=True)"
    )


def _make_probe(cfg: SyslogRfc3164Config):
    """Cria um probe TCP assíncrono para o host:port configurado."""

    async def _probe() -> TestResult:
        if not cfg.host:
            return TestResult.failed(
                "host vazio — syslog_rfc3164 exige host para enviar"
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
    cfg = SyslogRfc3164Config(**dict(config.config or {}))
    target = Rfc3164JsonClient(
        host=cfg.host or "",
        port=cfg.port,
        ca_bundle=cfg.ca_bundle,
        use_tls=cfg.use_tls,
    )
    return LegacyTargetDestination(
        KIND,
        target,
        formatter=format_rfc3164,
        probe=_make_probe(cfg),
    )


register(
    DestinationRegistration(
        kind=KIND,
        factory=_factory,
        config_schema=SyslogRfc3164Config,
        default_queue="dispatch.syslog",
        capabilities=frozenset({"tls", "batch", "test"}),
        required_secrets=(),  # ca_bundle é path, não secret
        label="Syslog RFC 3164 (JSON no MSG)",
        # Socket TCP único por target — concorrência baixa evita interleave.
        delivery_defaults={"concurrency": 2},
        # Campos de catálogo self-describing (galeria de destinos).
        category="Rede / Syslog",
        icon_id="syslog",
        tier="stable",
        order=110,
        description="Syslog legado BSD (RFC 3164) com JSON no MSG, via UDP/TCP.",
    )
)
