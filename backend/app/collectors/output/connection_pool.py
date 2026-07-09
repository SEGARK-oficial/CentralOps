"""Pool de conexão por destino — HTTP clients reutilizáveis.

Problema
--------
Sem pool, cada ciclo de dispatch cria um ``aiohttp.ClientSession`` novo por
destino HTTP (Elastic, Splunk HEC). Sessions têm overhead de handshake TCP/TLS
+ DNS na primeira request — em alto volume isso domina a latência de entrega.

Solução
-------
Um dict ``{destination_id: (session, loop)}`` process-global permite reusar a
mesma session entre lotes do mesmo destino enquanto o event loop persistir. O
pool só é ativado quando ``DISPATCH_PERSISTENT_LOOP=1`` — com loop efêmero por
task (padrão), o session é criado/destruído por task de qualquer forma (o loop
morre ao término da task e fecha o connector), então o pool não ajuda.

GATING (obrigatório — preserva comportamento legado)
-------
  - ``DISPATCH_PERSISTENT_LOOP=0`` (default): pool desativado. ``get_session``
    retorna ``None`` — o caller cria sua própria session (caminho atual).
  - ``DISPATCH_PERSISTENT_LOOP=1``: pool ativado. ``get_session`` devolve
    session existente para o dest_id (ou cria uma nova), atada ao loop
    corrente. Se o loop mudou (fork/restart do worker), a session antiga é
    descartada (o loop morreu junto com o connector).

Isolamento
----------
Sessions são por destination_id: o rate-limit/backoff de um destino não
afeta os demais (mesmo TCPConnector por dest, não compartilhado). O pool
não limita quantidade de conexões por session — o ``TCPConnector`` do caller
(criado quando o pool cria a session inicial) dita os limites.

Fork-safety
-----------
Mesmo padrão de ``dispatch_runtime.py`` / ``wazuh_target.py``: o pool
guarda o ``AbstractEventLoop`` junto com cada entry. Na detecção de loop
diferente, a entry stale é descartada sem ``close()`` (o fd do socket
pertence ao pai no caso de fork). Loop novo → session nova.

Uso típico (no sender):
    from .connection_pool import get_pooled_session
    import aiohttp

    session = get_pooled_session(dest_id, connector_factory)
    if session is None:
        session = aiohttp.ClientSession(...)  # caminho legado
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Callable, Dict, Optional, Tuple

import aiohttp

logger = logging.getLogger(__name__)

# (session, event_loop) por destination_id.
_pool: Dict[str, Tuple[aiohttp.ClientSession, asyncio.AbstractEventLoop]] = {}


def _pool_enabled() -> bool:
    """Pool ativado apenas com ``DISPATCH_PERSISTENT_LOOP=1``.

    Lido por chamada (barato, sem cache) — permite rollback sem restart."""
    return os.getenv("DISPATCH_PERSISTENT_LOOP", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


def get_pooled_session(
    destination_id: str,
    connector_factory: Callable[[], aiohttp.TCPConnector],
    headers: Optional[dict] = None,
) -> Optional[aiohttp.ClientSession]:
    """Retorna a session reutilizável para ``destination_id``, ou ``None`` se pool OFF.

    Quando o pool está OFF (``DISPATCH_PERSISTENT_LOOP=0``, default), retorna
    ``None`` para que o caller crie sua própria session — comportamento
    byte-idêntico ao legado.

    Quando o pool está ON:
      - session viva para o loop corrente → devolve a session cacheada.
      - session de loop diferente (fork/restart) → descarta SEM close() (fds
        do pai) e cria nova.
      - session fechada (aiohttp.ClientSession.closed == True) → recria.

    Args:
        destination_id: chave de isolamento (ex: ``"dest-elastic-01"``).
        connector_factory: callable que cria um novo ``aiohttp.TCPConnector``
            para a session nova (TLS, limites de conexão, etc.).
        headers: headers padrão da session (auth, content-type).

    Returns:
        ``aiohttp.ClientSession`` reutilizável, ou ``None`` se pool OFF.
    """
    if not _pool_enabled():
        return None

    loop = asyncio.get_running_loop()
    entry = _pool.get(destination_id)

    if entry is not None:
        session, entry_loop = entry
        if entry_loop is not loop:
            # Loop diferente (fork / restart): descarta sem close() — o
            # connector pertence ao loop antigo (ou ao pai, no fork).
            logger.debug(
                "connection_pool: loop mudou para dest=%s — session descartada",
                destination_id,
            )
            del _pool[destination_id]
        elif session.closed:
            # Session fechada (ex.: close() chamado no shutdown anterior).
            logger.debug(
                "connection_pool: session fechada para dest=%s — recriando",
                destination_id,
            )
            del _pool[destination_id]
        else:
            # Reuso: mesmo loop, session viva.
            return session

    # Cria nova session e registra no pool.
    connector = connector_factory()
    session = aiohttp.ClientSession(
        headers=headers or {},
        connector=connector,
    )
    _pool[destination_id] = (session, loop)
    logger.debug("connection_pool: nova session criada para dest=%s", destination_id)
    return session


async def close_session(destination_id: str) -> None:
    """Fecha e remove a session de ``destination_id`` do pool.

    Idempotente: no-op se o dest_id não está no pool ou a session já fechou.
    Chamado pelo dispatcher no shutdown gracioso (``shutdown_runtime`` →
    ``reset_destinations`` → ``close_session``).
    """
    entry = _pool.pop(destination_id, None)
    if entry is None:
        return
    session, _ = entry
    if not session.closed:
        try:
            await session.close()
        except Exception:  # pragma: no cover — best-effort
            logger.exception("connection_pool: erro ao fechar session dest=%s", destination_id)


async def close_all() -> None:
    """Fecha todas as sessions do pool. Seam de teardown/teste."""
    dest_ids = list(_pool.keys())
    for dest_id in dest_ids:
        await close_session(dest_id)


def reset() -> None:
    """Descarta todas as entries sem fechar (seam de teste / fork recovery).

    Usado quando não há loop corrente (ex.: teardown síncrono pós-fork).
    Não faz ``await session.close()`` — em contexto de fork os fds pertencem
    ao pai; em teste, deixa o GC lidar.
    """
    _pool.clear()
