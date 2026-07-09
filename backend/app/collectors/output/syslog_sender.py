"""Envio RFC 5424 / RFC 6587 (octet-counting) sobre TCP + TLS 1.2+ (RF06, RNF06).

Decisões:

- **TCP**, não UDP — SIEMs não toleram perda.
- **Octet-counting framing** (RFC 6587) em vez de LF, porque payloads
  JSON podem conter newlines embutidos em campos como ``rawMessage``.
- **TLS estrito** — sem bypass de verificação, sem downgrade para TLS
  < 1.2. Host verification ativo (``server_hostname``).
"""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..normalize.severity_map import pri_for_event
from ._fastjson import dumps_str as _json_dumps

logger = logging.getLogger(__name__)

# facility=local0 (16), severity=info (6) → PRI = 16*8 + 6 = 134
PRI_INFO = 134


class SyslogTCPClient:
    def __init__(
        self,
        host: str,
        port: int,
        ca_bundle: Optional[str] = None,
        use_tls: bool = True,
    ) -> None:
        """Cliente Syslog TCP com TLS opcional.

        ``use_tls=False`` é o caminho para Wazuh Manager vanilla (não
        aceita TLS no input Syslog — issue oficial desde 2021). Usar TLS
        apenas quando houver stunnel/rsyslog à frente ou SIEM alternativo.

        Quando ``use_tls=True``: TLS 1.2+ estrito, hostname verification
        ativo, ``CERT_REQUIRED``. Não mexemos nesses defaults por design.
        """
        self.host = host
        self.port = port
        self.use_tls = use_tls

        if use_tls:
            ctx = (
                ssl.create_default_context(cafile=ca_bundle)
                if ca_bundle
                else ssl.create_default_context()
            )
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            self.ctx: Optional[ssl.SSLContext] = ctx
        else:
            self.ctx = None

        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._lock = asyncio.Lock()

    async def _connect(self) -> None:
        if self.use_tls:
            self._reader, self._writer = await asyncio.open_connection(
                self.host,
                self.port,
                ssl=self.ctx,
                server_hostname=self.host,
            )
        else:
            self._reader, self._writer = await asyncio.open_connection(
                self.host,
                self.port,
            )

    async def _ensure(self) -> None:
        if self._writer is None or self._writer.is_closing():
            await self._connect()

    async def send_batch(self, batch: List[Dict[str, Any]]) -> None:
        if not batch:
            return
        async with self._lock:
            await self._ensure()
            assert self._writer is not None
            for event in batch:
                line = format_rfc5424(event).encode("utf-8")
                frame = f"{len(line)} ".encode("ascii") + line
                self._writer.write(frame)
            await self._writer.drain()

    async def close(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:  # pragma: no cover
                logger.exception("syslog: erro ao fechar writer")
            finally:
                self._writer = None
                self._reader = None


def format_rfc5424(event: Dict[str, Any]) -> str:
    """Formata um evento enriquecido em linha RFC 5424.

    Estrutura:

        <PRI>1 TIMESTAMP HOSTNAME APP-NAME PROCID MSGID STRUCTURED-DATA MSG

    Raises:
        ValueError: se o evento não tiver o namespace ``_centralops``.
    """
    meta = event.get("_centralops")
    if not meta:
        raise ValueError(
            "evento sem namespace '_centralops' — enrichment não aplicado (RF04)"
        )

    ts = meta.get("collected_at") or _now_iso()
    hostname = socket.gethostname()
    app_name = "centralops-collector"
    procid = "-"
    msgid = str(meta.get("integration_id") or "-")

    # ``platform`` foi renomeado para ``vendor`` no envelope canônico
    # da Fase 1; aceitamos ambos para compatibilidade com fixtures
    # legadas em testes. Decoders Wazuh devem ler ``vendor``.
    vendor = meta.get("vendor") or meta.get("platform")
    sd = (
        f'[centralops@32473 '
        f'integration_id="{_sdv(meta.get("integration_id"))}" '
        f'customer_id="{_sdv(meta.get("customer_id"))}" '
        f'vendor="{_sdv(vendor)}" '
        f'stream="{_sdv(meta.get("stream"))}" '
        f'event_type="{_sdv(meta.get("event_type"))}"]'
    )

    # JSON compacto via _fastjson (orjson quando disponível, fallback stdlib).
    # Wire bytes idênticos: compact separators, ensure_ascii=False, default=str.
    msg = _json_dumps(event)
    pri = pri_for_event(event)
    return f"<{pri}>1 {ts} {hostname} {app_name} {procid} {msgid} {sd} {msg}"


def _sdv(value: Any) -> str:
    """Escapa um valor para STRUCTURED-DATA (RFC 5424 §6.3.3)."""
    if value is None:
        return ""
    s = str(value)
    return s.replace("\\", r"\\").replace('"', r"\"").replace("]", r"\]")


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
