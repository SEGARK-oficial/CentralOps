"""Dedupe em memória por partição do tópico raw (ADR-0018 Onda 3c).

O dedupe do ciclo de coleta é uma chave Redis por evento com TTL de 24 h: um
round-trip por evento e ~115 B de Redis por evento visto. Com 512 MB e TTL de
24 h a evicção começa em ~54 eps somando todos os tenants — a garantia de 24 h
já não existia em escala, o Redis só apagava as chaves mais velhas.

No ``processor`` cada partição do raw tem um único consumidor por vez, e o
coletor pull escolhe a partição pelo id do evento (``raw.produce_raw``): o mesmo
evento re-buscado em outro ciclo cai na mesma partição. Então a janela pode
morar na memória do processo dono da partição, sem I/O:

* ``claim`` devolve False para id já liquidado na janela OU em voo no lote;
* ``settle`` registra os ids depois do hand-off durável (como o SREM do Redis);
* ``release`` devolve os ids não liquidados quando o lote falha — o retry os
  reprocessa em vez de descartá-los como duplicados (a mesma compensação da
  ADR-0015 Fase 2).

A janela é limitada por tempo (``RAW_DEDUPE_WINDOW_SECONDS``) e por quantidade
(``RAW_DEDUPE_WINDOW_MAX_IDS``). Cobre o que de fato se repete: a borda do
cursor re-buscada no ciclo seguinte e a reentrega do Kafka depois de uma queda.
Perde-se a janela quando o processo reinicia ou a partição muda de dono — aí a
entrega é at-least-once, como o ADR aceita (Questão 2).
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from typing import Callable, Iterable, Set


def _fingerprint(integration_id: int, msg_id: str) -> int:
    """64 bits do id escopado pela integração: ~4× menos memória que guardar a
    string, colisão desprezível no tamanho da janela."""
    digest = hashlib.blake2b(f"{integration_id}:{msg_id}".encode("utf-8"), digest_size=8)
    return int.from_bytes(digest.digest(), "big")


class DedupeWindow:
    """Ids vistos numa partição, com expiração por tempo e por quantidade.

    Não é thread-safe: cada partição é processada por uma única corrotina."""

    def __init__(
        self,
        max_ids: int,
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_ids = max(1, int(max_ids))
        self.ttl = float(ttl_seconds)
        self._clock = clock
        self._seen: "OrderedDict[int, float]" = OrderedDict()
        self._pending: Set[int] = set()

    def __len__(self) -> int:
        return len(self._seen)

    def _expire(self, now: float) -> None:
        seen = self._seen
        while seen:
            fp, ts = next(iter(seen.items()))
            if now - ts < self.ttl and len(seen) <= self.max_ids:
                break
            seen.popitem(last=False)

    def claim(self, integration_id: int, msg_id: str) -> bool:
        """True = evento novo (fica em voo até ``settle``/``release``)."""
        fp = _fingerprint(integration_id, msg_id)
        if fp in self._pending:
            return False
        ts = self._seen.get(fp)
        if ts is not None and self._clock() - ts < self.ttl:
            return False
        self._pending.add(fp)
        return True

    def settle(self, integration_id: int, msg_ids: Iterable[str]) -> None:
        now = self._clock()
        for msg_id in msg_ids:
            fp = _fingerprint(integration_id, msg_id)
            self._pending.discard(fp)
            self._seen.pop(fp, None)
            self._seen[fp] = now
        self._expire(now)

    def release(self, integration_id: int, msg_ids: Iterable[str]) -> int:
        n = 0
        for msg_id in msg_ids:
            fp = _fingerprint(integration_id, msg_id)
            if fp in self._pending:
                self._pending.discard(fp)
                n += 1
        return n
