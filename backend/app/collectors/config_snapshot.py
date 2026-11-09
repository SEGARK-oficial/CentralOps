"""Snapshot de configuração com last-known-good (ADR-0018 Onda 3b).

O ``processor`` lia do Postgres, a cada lote do tópico raw, a integração, as
rotas, o mapping, as regras em voo, a política de enriquecimento, o modo OCSF e
três mapas de destino. Com o banco fora o lote falhava (rotas, mapping) ou, pior,
seguia com a proteção desligada (residência de dados, laço Wazuh e fallback
viravam vazio; regras em voo e enriquecimento sumiam em silêncio).

Aqui cada leitura vira uma entrada com idade:

* mais nova que ``ttl`` → servida da memória, sem ir ao banco (é também o que
  tira o Postgres do caminho de cada lote);
* vencida → recarrega; se a recarga falhar por **indisponibilidade do banco** e a
  entrada tiver menos de ``max_stale``, serve a última versão boa
  (last-known-good) e conta ``collector_config_snapshot_stale_total{kind}``;
* sem entrada boa, ou mais velha que ``max_stale``, ou erro que NÃO é
  indisponibilidade (rota que não compila, mapping corrompido) → o desfecho de
  antes desta onda: as cargas fail-closed (rotas, mapping, integração) levantam
  e o lote fica no Kafka; as fail-safe voltam ao valor degradado de sempre.

Só o ``processor`` ativa o snapshot (``ACTIVE``). O ciclo inline segue lendo a
cada ciclo, sem cache — o comportamento que os testes do ciclo descrevem.
"""

from __future__ import annotations

import logging
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Dict, Hashable, Optional, Tuple, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: Snapshot em uso no contexto atual. ``None`` = sem cache (ciclo inline).
#: ``asyncio.to_thread`` copia o contexto, então as cargas feitas em thread
#: enxergam o mesmo snapshot.
ACTIVE: "ContextVar[Optional[SnapshotCache]]" = ContextVar("config_snapshot", default=None)

_MAX_ENTRIES = 4096


def is_store_outage(exc: BaseException) -> bool:
    """O erro (ou algum da cadeia ``__cause__``/``__context__``) é o banco fora
    do ar — e não configuração inválida, que precisa continuar falhando."""
    from sqlalchemy import exc as sa_exc

    outage = (
        sa_exc.OperationalError,
        sa_exc.InterfaceError,
        sa_exc.DisconnectionError,
        sa_exc.TimeoutError,
        ConnectionError,
        TimeoutError,
    )
    seen = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, outage):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


@dataclass
class _Entry:
    value: Any
    loaded_at: float


class SnapshotCache:
    """Cache por (tipo, chave) com TTL e last-known-good. Thread-safe: as cargas
    rodam em ``asyncio.to_thread``."""

    def __init__(
        self,
        ttl_seconds: float,
        max_stale_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl = float(ttl_seconds)
        self.max_stale = max(float(max_stale_seconds), self.ttl)
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: Dict[Tuple[str, Hashable], _Entry] = {}
        self._stale_logged: set = set()

    def get(self, kind: str, key: Hashable, loader: Callable[[], T]) -> T:
        now = self._clock()
        with self._lock:
            entry = self._entries.get((kind, key))
        if entry is not None and now - entry.loaded_at < self.ttl:
            return entry.value
        try:
            value = loader()
        except Exception as exc:
            if (
                entry is not None
                and now - entry.loaded_at < self.max_stale
                and is_store_outage(exc)
            ):
                self._note_stale(kind, now - entry.loaded_at)
                return entry.value
            raise
        with self._lock:
            if len(self._entries) >= _MAX_ENTRIES and (kind, key) not in self._entries:
                oldest = min(self._entries, key=lambda k: self._entries[k].loaded_at)
                del self._entries[oldest]
            self._entries[(kind, key)] = _Entry(value, now)
            self._stale_logged.discard(kind)
        return value

    def _note_stale(self, kind: str, age: float) -> None:
        from .metrics import CONFIG_SNAPSHOT_STALE

        try:
            CONFIG_SNAPSHOT_STALE.labels(kind=kind).inc()
        except Exception:  # pragma: no cover — métrica nunca derruba o lote
            pass
        with self._lock:
            first = kind not in self._stale_logged
            self._stale_logged.add(kind)
        if first:
            logger.warning(
                "config: banco indisponível — usando a última versão boa de %r "
                "(idade %.0fs, teto %.0fs)",
                kind, age, self.max_stale,
                extra={"event": "config.snapshot_stale", "kind": kind},
            )


def read(
    kind: str,
    key: Hashable,
    strict_loader: Callable[[], T],
    lenient_loader: Optional[Callable[[], T]] = None,
) -> T:
    """Lê pelo snapshot ativo; sem snapshot, chama o loader de sempre.

    ``strict_loader`` levanta quando o banco falha (é o que permite servir a
    última versão boa). ``lenient_loader`` é a versão fail-safe histórica, para
    as cargas que degradam em vez de falhar: sem snapshot ela é a única
    chamada, e com snapshot ela só roda quando o banco está fora e ainda não há
    versão boa — o mesmo desfecho de antes desta onda."""
    cache = ACTIVE.get()
    if cache is None:
        return (lenient_loader or strict_loader)()
    try:
        return cache.get(kind, key, strict_loader)
    except Exception as exc:
        if lenient_loader is not None and is_store_outage(exc):
            return lenient_loader()
        raise
