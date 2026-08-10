"""Cache de duas camadas do enriquecimento REMOTO (ADR-LOCAL-0002, Fase 3).

Só o seam remoto usa cache. O local não precisa: a tabela está inteira em memória,
então "não achei" já é um ``dict.get`` — pôr cache ali seria acrescentar uma camada
na frente de uma consulta que custa nanossegundos.

**L1** — LRU+TTL em processo. Absorve a repetição dentro do ciclo (o mesmo host
aparece em dezenas de eventos) sem tocar a rede.
**L2** — Redis COMPARTILHADO entre workers. É o que faz o segundo worker não repagar
a chamada que o primeiro já fez.

Três decisões que não são detalhe:

1. **Instância dedicada, e fail-closed por default.** Sem ``ENRICH_REDIS_URL``
   explícita, o L2 **não liga** — e sem L2 o enriquecimento remoto também não. O
   caminho fácil (cair no ``REDIS_URL`` principal) é o errado: aquela instância roda
   ``--maxmemory 512mb --maxmemory-policy volatile-lru`` (``docker-compose.yml:210``)
   e a política é GLOBAL por instância, então chave de cache com TTL disputaria
   memória com o dedupe — cuja evicção é silenciosa e reaparece como reentrega no
   SIEM, sem erro, sem log, sem alerta. DB lógico não isola memória. Já custou um
   incidente (310k chaves evicted); um default inseguro documentado continua sendo
   um default inseguro.

2. **Três estados, não dois.** ``HIT`` (valor), ``MISS`` (o provedor respondeu
   "não conheço" — cacheável com TTL curto) e ``UNKNOWN`` (não resolvemos: cota,
   timeout, erro). Colapsar MISS e UNKNOWN faria uma cota estourada virar "indicador
   limpo" no cache pelas próximas 6 horas — falso negativo de segurança que se
   propaga. É o bug que ``services/threat_intel/cache.py:33,39`` tem hoje.

3. **Single-flight por CHAVE, nunca por lote.** Travar em ``sha1(chaves_do_lote)``
   não previne stampede nenhum: basta um evento diferir e o hash muda, e os dois
   lotes batem na origem para as ~199 chaves em comum. E a espera é curta (≤50 ms
   por default): esperar 3 s por um lock dentro de um orçamento de 300 ms/lote é
   trocar economia de cota por estouro de orçamento.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import OrderedDict
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "CacheState",
    "EnrichCache",
    "L1Cache",
    "cache_key",
]


class CacheState(str, Enum):
    """Estado de uma entrada. Ver decisão (2) no docstring do módulo."""

    HIT = "hit"
    #: Provedor respondeu e não conhece o indicador. Cacheável (TTL curto).
    MISS = "miss"
    #: Não conseguimos resolver. **Nunca cacheado.**
    UNKNOWN = "unknown"


def cache_key(org_id: int, enricher: str, version: str, key: str) -> str:
    """Chave canônica. ``organization_id`` é OBRIGATÓRIO e vem primeiro.

    Não há afinidade por tenant no data-plane (``queues.py`` particiona por
    ``sha1(destination_id)``) e ``worker_max_tasks_per_child=500`` faz o mesmo fork
    atender centenas de orgs. Chave sem org serve o resultado do CMDB da Org A para
    a Org B no ciclo seguinte, dentro do mesmo processo — a classe exata dos 11 gaps
    cross-org já fechados no projeto.

    A chave do indicador é hasheada: um valor cru (URL, hostname) pode conter
    espaço, ``:`` e caracteres que quebram o namespace, e o dado do cliente não
    precisa ficar legível no Redis.
    """
    digest = hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:24]
    return f"enr:{org_id}:{enricher}:{version}:{digest}"


class L1Cache:
    """LRU com TTL, em processo. Não é thread-safe por design.

    O worker Celery é prefork e cada task roda num único thread por processo, então
    lock aqui seria custo puro. Se o modelo de execução mudar (gevent/solo), isto
    precisa ser revisto — está escrito para ser encontrado.
    """

    __slots__ = ("_max", "_data", "hits", "misses")

    def __init__(self, max_entries: int = 10_000) -> None:
        self._max = max(int(max_entries), 1)
        #: chave → (expira_em_monotonic, estado, valor)
        self._data: "OrderedDict[str, Tuple[float, CacheState, Any]]" = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Tuple[CacheState, Any]:
        entry = self._data.get(key)
        if entry is None:
            self.misses += 1
            return CacheState.UNKNOWN, None
        expires_at, state, value = entry
        if expires_at <= time.monotonic():
            # Expirada: remove agora em vez de deixar ocupando espaço no LRU.
            self._data.pop(key, None)
            self.misses += 1
            return CacheState.UNKNOWN, None
        self._data.move_to_end(key)
        self.hits += 1
        return state, value

    def put(self, key: str, state: CacheState, value: Any, ttl_s: float) -> None:
        if state is CacheState.UNKNOWN or ttl_s <= 0:
            # UNKNOWN nunca entra: é ausência de resposta, não resposta.
            return
        self._data[key] = (time.monotonic() + ttl_s, state, value)
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


class EnrichCache:
    """L1 + L2 com single-flight por chave.

    O cliente Redis é injetado (não criado aqui): o worker abre um cliente por task
    porque ``redis.asyncio`` amarra futures ao event loop em que o pool nasceu, e
    reaproveitar entre tasks dispara "Event loop is closed" — o mesmo motivo
    documentado em ``celery_app.get_worker_redis``.
    """

    #: Marcador de MISS no L2. Precisa ser distinguível de um valor real e de
    #: ausência de chave — daí a sentinela explícita em vez de string vazia.
    _MISS_SENTINEL = "\x00miss"

    def __init__(
        self,
        redis: Any,
        *,
        l1: Optional[L1Cache] = None,
        singleflight_wait_ms: int = 50,
        lock_ttl_ms: int = 5_000,
    ) -> None:
        self._redis = redis
        self.l1 = l1 or L1Cache()
        self._wait_ms = max(int(singleflight_wait_ms), 0)
        self._lock_ttl_ms = max(int(lock_ttl_ms), 100)

    # ── leitura ─────────────────────────────────────────────────────────────
    async def get_many(
        self, keys: Sequence[str]
    ) -> Tuple[Dict[str, Any], Dict[str, CacheState], list]:
        """``(valores, estados, faltantes)``.

        ``faltantes`` são as chaves que precisam ir à origem — L1 e L2 não sabem
        responder. MISS cacheado NÃO entra em faltantes: já sabemos que o provedor
        não conhece o indicador, e reperguntar queima cota por nada.
        """
        values: Dict[str, Any] = {}
        states: Dict[str, CacheState] = {}
        pending: list = []

        for k in keys:
            state, value = self.l1.get(k)
            if state is CacheState.HIT:
                values[k] = value
                states[k] = state
            elif state is CacheState.MISS:
                states[k] = state
            else:
                pending.append(k)

        if not pending or self._redis is None:
            return values, states, pending

        try:
            raw = await self._redis.mget(pending)
        except Exception:  # noqa: BLE001 — L2 fora do ar degrada para a origem
            logger.debug("enrich cache: MGET falhou; seguindo para a origem", exc_info=True)
            return values, states, pending

        still_missing = []
        import json as _json

        for k, blob in zip(pending, raw or []):
            if blob is None:
                still_missing.append(k)
                continue
            if blob == self._MISS_SENTINEL:
                states[k] = CacheState.MISS
                continue
            try:
                values[k] = _json.loads(blob)
                states[k] = CacheState.HIT
            except Exception:  # noqa: BLE001 — entrada corrompida = tratar como ausente
                still_missing.append(k)
        return values, states, still_missing

    # ── escrita ─────────────────────────────────────────────────────────────
    async def put_many(
        self,
        resolved: Mapping[str, Optional[Mapping[str, Any]]],
        *,
        ttl_s: int,
        negative_ttl_s: int,
    ) -> None:
        """Grava HIT e MISS. Chave AUSENTE do mapa é UNKNOWN e não é gravada.

        Essa assimetria é o ponto: ``resolve`` omite a chave quando não conseguiu
        resolver (cota estourada, timeout). Gravar isso como MISS transformaria uma
        indisponibilidade momentânea em "indicador limpo" pelas próximas horas.
        """
        import json as _json

        for key, value in resolved.items():
            if value is None:
                self.l1.put(key, CacheState.MISS, None, negative_ttl_s)
            else:
                self.l1.put(key, CacheState.HIT, value, ttl_s)

        if self._redis is None:
            return
        try:
            pipe = self._redis.pipeline()
            for key, value in resolved.items():
                if value is None:
                    pipe.set(key, self._MISS_SENTINEL, ex=max(int(negative_ttl_s), 1))
                else:
                    pipe.set(key, _json.dumps(value, separators=(",", ":")), ex=max(int(ttl_s), 1))
            await pipe.execute()
        except Exception:  # noqa: BLE001 — cache é best-effort
            logger.debug("enrich cache: escrita no L2 falhou", exc_info=True)

    # ── single-flight ───────────────────────────────────────────────────────
    async def acquire_single_flight(self, key: str) -> bool:
        """``True`` se ESTE processo deve resolver a chave.

        ``False`` significa que outro já está resolvendo; o chamador deve reler o L2
        (a espera curta acontece em :meth:`wait_for_peer`). Lock POR CHAVE — travar
        por lote não previne stampede algum.
        """
        if self._redis is None:
            return True
        try:
            got = await self._redis.set(
                f"{key}:lock", "1", nx=True, px=self._lock_ttl_ms
            )
            return bool(got)
        except Exception:  # noqa: BLE001 — sem lock, resolve mesmo assim
            return True

    async def release_single_flight(self, key: str) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.delete(f"{key}:lock")
        except Exception:  # noqa: BLE001
            pass

    async def wait_for_peer(self, key: str) -> Tuple[CacheState, Any]:
        """Espera CURTA pelo resultado de quem tem o lock.

        Teto de ``singleflight_wait_ms`` (50 ms default) porque o orçamento do lote
        inteiro é de 300 ms: esperar mais trocaria economia de cota por estouro de
        orçamento, que é pior — o orçamento estourado desliga o enriquecimento do
        ciclo INTEIRO (gate binário).
        """
        if self._redis is None or self._wait_ms <= 0:
            return CacheState.UNKNOWN, None
        import asyncio
        import json as _json

        deadline = time.monotonic() + (self._wait_ms / 1000.0)
        while time.monotonic() < deadline:
            await asyncio.sleep(0.005)
            try:
                blob = await self._redis.get(key)
            except Exception:  # noqa: BLE001
                return CacheState.UNKNOWN, None
            if blob is None:
                continue
            if blob == self._MISS_SENTINEL:
                return CacheState.MISS, None
            try:
                return CacheState.HIT, _json.loads(blob)
            except Exception:  # noqa: BLE001
                return CacheState.UNKNOWN, None
        return CacheState.UNKNOWN, None


def build_redis_client(url: Optional[str]) -> Optional[Any]:
    """Cliente do L2 a partir de uma URL EXPLÍCITA, ou ``None``.

    **Nunca** usa ``core.redis_client.get_redis()``: aquele helper cai num
    ``_InMemoryRedisFallback`` em silêncio (``redis_client.py:162-168``), e um cache
    "compartilhado" que na verdade é por-processo daria hit rate plausível e
    economia de cota nenhuma — falha invisível, que é o pior tipo aqui.
    """
    if not url:
        return None
    try:
        import redis.asyncio as redis_async

        return redis_async.from_url(url, decode_responses=True)
    except Exception:  # noqa: BLE001
        logger.warning(
            "enrich: não foi possível criar o cliente do Redis de enriquecimento — "
            "o enriquecimento REMOTO fica desligado neste ciclo",
            extra={"event": "enrich.l2_unavailable"},
            exc_info=True,
        )
        return None
