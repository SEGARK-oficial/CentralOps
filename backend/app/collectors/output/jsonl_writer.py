"""JSONL rotativo por dia / vendor (fallback do Syslog, RF06).

Arquivo: ``{base}/{platform}/{YYYY-MM-DD}.log``

- Append-only por linha (thread-safe via asyncio.Lock por arquivo).
- Sem rotação automática por tamanho — delega ao logrotate do host
  ou ao Filebeat com ``close_renamed``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List

from .formatters import format_jsonl

logger = logging.getLogger(__name__)


class JSONLWriter:
    def __init__(self, base_dir: str) -> None:
        self.base_dir = base_dir
        self._locks: Dict[str, asyncio.Lock] = {}

    def _path_for(self, platform: str) -> str:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        directory = os.path.join(self.base_dir, platform)
        os.makedirs(directory, exist_ok=True)
        return os.path.join(directory, f"{day}.log")

    def _lock_for(self, path: str) -> asyncio.Lock:
        lock = self._locks.get(path)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[path] = lock
        return lock

    async def send_batch(self, batch: List[Dict[str, Any]]) -> None:
        if not batch:
            return
        # Agrupa por vendor para abrir 1 arquivo por vendor no lote.
        # ``vendor`` é o nome canônico (Fase 1); ``platform`` é o legado.
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for event in batch:
            meta = event.get("_centralops") or {}
            vendor = meta.get("vendor") or meta.get("platform") or "unknown"
            grouped.setdefault(vendor, []).append(event)

        for vendor, events in grouped.items():
            path = self._path_for(vendor)
            lock = self._lock_for(path)
            async with lock:
                # I/O síncrono — arquivos locais são rápidos. Para alto
                # volume (>10k/s) considere ``aiofiles``.
                with open(path, "ab") as fh:
                    for ev in events:
                        # FONTE ÚNICA do wire (C6): a mesma ``format_jsonl`` que
                        # o ``Destination.format()`` expõe. O framing LF (b"\n")
                        # é responsabilidade DESTE writer, não da formatação.
                        fh.write(format_jsonl(ev) + b"\n")

    async def close(self) -> None:  # interface parity com SyslogTCPClient
        return None
