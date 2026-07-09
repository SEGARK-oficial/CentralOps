"""RFC 3164 (BSD syslog) sender com JSON puro no MSG.

Compatível com Wazuh JSON_Decoder nativo (``^{`` prematch). Diferente do
RFC 5424 sender, NÃO usa STRUCTURED-DATA — toda metadata vai dentro do
JSON em ``_centralops``.

Header: <PRI>Mmm dd HH:MM:SS hostname centralops[pid]: {json}
Framing: LF-delimited (Wazuh syslog input aceita; octet-counting opcional).

Por que RFC 3164 em vez de RFC 5424 para Wazuh:
- Wazuh issue #2038: o JSON_Decoder nativo não casa em linhas RFC 5424
  porque o framing octet-counting + cabeçalho estruturado impede o
  prematch ``^{``. RFC 3164 coloca o JSON diretamente no MSG, após
  ``app[pid]: ``, onde o decoder encontra ``{`` e decodifica.
- RFC 5424 continua disponível para SIEMs alternativos (Graylog, Splunk)
  que suportam STRUCTURED-DATA. O chaveamento é feito em wazuh_target.py.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import ssl
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..normalize.severity_map import pri_for_event
from ._fastjson import dumps_str as _json_dumps

logger = logging.getLogger(__name__)

# facility=local0 (16), severity=info (6) → PRI = 16*8 + 6 = 134
PRI_INFO = 134
APP_NAME = "centralops"
# Nomes curtos dos meses conforme RFC 3164 §4.1.2.
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


class Rfc3164JsonClient:
    """Cliente Syslog RFC 3164 TCP com JSON puro no MSG.

    Uso típico (Wazuh Manager vanilla, sem TLS):

        client = Rfc3164JsonClient(host="192.168.3.211", port=514)
        await client.send_batch([event1, event2])
        await client.close()

    Para SIEMs com TLS:

        client = Rfc3164JsonClient(
            host="siem.exemplo.com",
            port=6514,
            use_tls=True,
            ca_bundle="/certs/ca.pem",
        )
    """

    def __init__(
        self,
        host: str,
        port: int,
        ca_bundle: Optional[str] = None,
        use_tls: bool = False,
    ) -> None:
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
                self.host, self.port, ssl=self.ctx, server_hostname=self.host,
            )
        else:
            self._reader, self._writer = await asyncio.open_connection(
                self.host, self.port,
            )

    async def _ensure(self) -> None:
        """Reconecta se o writer não existir ou estiver fechando."""
        if self._writer is None or self._writer.is_closing():
            await self._connect()

    async def send_batch(self, batch: List[Dict[str, Any]]) -> None:
        """Envia lote de eventos em formato RFC 3164 + JSON, LF-delimited."""
        if not batch:
            return
        async with self._lock:
            await self._ensure()
            assert self._writer is not None
            for event in batch:
                line = format_rfc3164(event).encode("utf-8")
                # Framing LF-delimited: Wazuh syslog input aceita \n como
                # separador de mensagens (padrão mais antigo, mais simples).
                self._writer.write(line + b"\n")
            await self._writer.drain()

    async def close(self) -> None:
        """Fecha a conexão TCP."""
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:  # pragma: no cover
                logger.exception("rfc3164: erro ao fechar writer")
            finally:
                self._writer = None
                self._reader = None


def format_rfc3164(event: Dict[str, Any]) -> str:
    """Formata um evento em linha RFC 3164 com JSON puro no MSG.

    Estrutura:
        <PRI>Mmm dd HH:MM:SS HOSTNAME APP[PID]: {json}

    Wazuh JSON_Decoder casa o ``^{`` no MSG (após ``APP[PID]: ``) e
    decodifica todo o JSON — inclusive o namespace ``_centralops``.

    Args:
        event: dicionário do evento (pode ser envelope canônico ou raw).

    Returns:
        String RFC 3164 sem ``\\n`` final.

    Raises:
        ValueError: se ``event`` for vazio ou não for dict.
    """
    if not isinstance(event, dict) or not event:
        raise ValueError("evento vazio ou não-dict")

    now = datetime.utcnow()
    # RFC 3164 timestamp: "Mmm dd HH:MM:SS"
    # Dia alinhado à direita com espaço para 1 dígito: " 6" vs "26".
    day = f"{now.day:>2d}"
    ts = f"{_MONTHS[now.month - 1]} {day} {now.strftime('%H:%M:%S')}"

    hostname = socket.gethostname() or "centralops"
    pid = os.getpid()

    # JSON compacto via _fastjson (orjson quando disponível, fallback stdlib).
    # Wire bytes idênticos: compact separators, ensure_ascii=False, default=str.
    msg = _json_dumps(event)

    pri = pri_for_event(event)
    return f"<{pri}>{ts} {hostname} {APP_NAME}[{pid}]: {msg}"
