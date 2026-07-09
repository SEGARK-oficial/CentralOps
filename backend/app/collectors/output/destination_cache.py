"""Cache multi-singleton de destinos.

Generaliza ``wazuh_target.get_target`` (singleton único por
``config_version``+loop) para **N destinos vivos**, chaveando por
``(destination_id, config_version, event_loop)``. Reusa a mesma lógica
de loop-tracking que resolve o "Event loop is closed" do Celery prefork
(``asyncio.run()`` por task cria loop novo).

O caminho Wazuh de produção continua no ``wazuh_target.get_target``
(intocado, byte-idêntico, lane dedicada). Este cache é exercido pelo
dispatcher multi-destino (GA).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .base import Destination
from .destinations import registry
from .destinations.registry import DestinationConfig

logger = logging.getLogger(__name__)


@dataclass
class _Entry:
    target: Destination
    version: str
    loop: asyncio.AbstractEventLoop


# Cache por destination_id. O event loop e a versão fazem parte do
# critério de reuso (não só a chave) — espelha wazuh_target.
_cache: Dict[str, _Entry] = {}

# Lock por event loop (igual a wazuh_target._get_lock_for_current_loop):
# um asyncio.Lock está atrelado ao loop em que foi criado.
_lock: Optional[asyncio.Lock] = None
_lock_loop: Optional[asyncio.AbstractEventLoop] = None


def _get_lock_for_current_loop() -> asyncio.Lock:
    global _lock, _lock_loop
    current = asyncio.get_running_loop()
    if _lock is None or _lock_loop is not current:
        _lock = asyncio.Lock()
        _lock_loop = current
    return _lock


async def get_destination(
    config: DestinationConfig,
    secrets: Optional[Any] = None,
) -> Destination:
    """Resolve o destino para ``config.destination_id``. Recicla quando:

    - ``config_version`` mudou (operador editou o destino na UI); ou
    - o event loop atual difere do em que o destino foi construído
      (Celery prefork criou loop novo via ``asyncio.run()``).
    """
    current_loop = asyncio.get_running_loop()
    lock = _get_lock_for_current_loop()

    async with lock:
        dest_id = config.destination_id
        version = config.config_version
        entry = _cache.get(dest_id)

        if entry is not None and entry.version == version and entry.loop is current_loop:
            return entry.target

        if entry is not None and entry.loop is not current_loop:
            # Loop morto: não dá pra ``await close()`` — descarta e deixa GC.
            logger.debug(
                "destination_cache: loop mudou para dest=%s, descartando target",
                dest_id,
            )
            _cache.pop(dest_id, None)
        elif entry is not None:
            # Versão mudou no mesmo loop — fechamento limpo.
            logger.info(
                "destination_cache: config mudou para dest=%s (%s → %s), recriando",
                dest_id, entry.version, version,
            )
            try:
                await entry.target.close()
            except Exception:  # pragma: no cover
                logger.exception("destination_cache: erro ao fechar target dest=%s", dest_id)
            _cache.pop(dest_id, None)

        target = registry.build(config, secrets)
        _cache[dest_id] = _Entry(target=target, version=version, loop=current_loop)
        return target


async def reset_destinations() -> None:
    """Fecha + descarta todos os targets de destino cacheados (força recriação).

    Usado por testes E pela recuperação de poison-writer do ``dispatch_runtime``
    (substituiu o ``wazuh_target.reset_target`` da lane removida)."""
    global _lock, _lock_loop
    try:
        lock = _get_lock_for_current_loop()
        async with lock:
            for entry in list(_cache.values()):
                try:
                    await entry.target.close()
                except Exception:  # pragma: no cover
                    pass
            _cache.clear()
    except RuntimeError:
        # Sem loop corrente (chamado fora de async) — limpa direto.
        _cache.clear()
    _lock = None
    _lock_loop = None
