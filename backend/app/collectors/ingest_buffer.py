"""Buffer Redis de ingestão push (FortiGate syslog, Windows Event Log/WEC, …).

O endpoint ``POST /api/ingest/...`` valida + autentica e EMPURRA os eventos crus
para uma lista Redis por ``(integration_id, stream)``. O collector virtual
``PushBufferCollector`` DRENA essa lista dentro do ``run_collection_once`` normal
— reaproveitando 100% do pipeline existente (dedupe → mapping → routing → dispatch
→ quarentena → tracing). Não há um 2º caminho de normalização.

**Backpressure.** A lista é capada (``LTRIM``) em ``max_len`` — sob ingestão mais
rápida que o dreno, os eventos MAIS ANTIGOS são descartados (drop-oldest) e o
contador de descarte é exposto. Isso protege a memória do Redis (incidente de
sizing OOM) em vez de crescer sem limite.

**FIFO.** ``LPUSH`` insere na cabeça; o dreno faz ``RPOP`` da cauda (mais antigo
primeiro). Ordem não é crítica (o pipeline dedup + ordena por timestamp), mas FIFO
minimiza latência de cauda.
"""

from __future__ import annotations

import json
import logging
from typing import Any, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

# Prefixos de chave. ``buf`` = lista de eventos; ``dropped`` = contador de
# descartes por backpressure (informativo, TTL para não vazar).
_KEY_PREFIX = "ingest:buf"
_DROPPED_PREFIX = "ingest:dropped"
# Capacidade default da lista por (integração, stream). Cap de segurança de
# memória; configurável por chamada. ~ alguns MB por buffer no pior caso.
DEFAULT_MAX_LEN = 100_000
# Máximo de eventos drenados por ciclo de coleta (bound do tempo de ciclo).
DEFAULT_DRAIN_BUDGET = 50_000
# Tamanho do chunk de ``RPOP count`` (Redis ≥ 6.2).
_DRAIN_CHUNK = 500
# TTL do contador de descarte (s) — 7 dias, auto-podado.
_DROPPED_TTL = 7 * 24 * 3600


def buffer_key(integration_id: int, stream: str) -> str:
    return f"{_KEY_PREFIX}:{integration_id}:{stream}"


def dropped_key(integration_id: int, stream: str) -> str:
    return f"{_DROPPED_PREFIX}:{integration_id}:{stream}"


async def push_events(
    redis: Any,
    integration_id: int,
    stream: str,
    events: List[Mapping[str, Any]],
    *,
    max_len: int = DEFAULT_MAX_LEN,
) -> Tuple[int, int]:
    """Empurra ``events`` para o buffer; capa em ``max_len`` (drop-oldest).

    Devolve ``(accepted, dropped)``: ``accepted`` = quantos foram inseridos;
    ``dropped`` = quantos foram descartados por exceder o cap (estimado pelo
    overflow após o ``LTRIM``). Serializa cada evento como JSON compacto.
    """
    if not events:
        return 0, 0

    key = buffer_key(integration_id, stream)
    payloads = [
        json.dumps(dict(ev), separators=(",", ":"), default=str, ensure_ascii=False)
        for ev in events
    ]
    # LPUSH (cabeça) + LTRIM para manter os ``max_len`` mais RECENTES. Sob
    # overflow, os mais antigos (cauda) são descartados — drop-oldest.
    pipe = redis.pipeline()
    pipe.lpush(key, *payloads)
    pipe.ltrim(key, 0, max_len - 1)
    pipe.llen(key)
    results = await pipe.execute()
    new_len = int(results[-1] or 0)

    # Overflow: o ``LPUSH`` devolve o comprimento APÓS o push (atômico). O
    # ``LTRIM(0, max_len-1)`` mantém no máximo ``max_len``, então o excedente que
    # ESTE push provocou é ``lpush_len - max_len`` (quando positivo). Derivar do
    # LPUSH (e não do LLEN pós-trim) é robusto a writers concorrentes intercalando
    # entre os comandos do pipeline — o LLEN pós-trim pode refletir outro estado.
    length_after_push = int(results[0] or 0)
    dropped = max(0, length_after_push - max_len)
    if dropped:
        try:
            dpipe = redis.pipeline()
            dpipe.incrby(dropped_key(integration_id, stream), dropped)
            dpipe.expire(dropped_key(integration_id, stream), _DROPPED_TTL)
            await dpipe.execute()
        except Exception:  # noqa: BLE001 — contador é best-effort
            logger.debug("ingest_buffer: falha ao incrementar contador de descarte")
        logger.warning(
            "ingest_buffer: backpressure integration_id=%s stream=%s descartou %d evento(s) "
            "(cap=%d) — dreno mais lento que a ingestão",
            integration_id, stream, dropped, max_len,
        )
    return len(payloads), dropped


async def drain_events(
    redis: Any,
    integration_id: int,
    stream: str,
    *,
    budget: int = DEFAULT_DRAIN_BUDGET,
) -> List[dict]:
    """Drena até ``budget`` eventos do buffer (FIFO, mais antigos primeiro).

    Faz ``RPOP key count`` em chunks até esvaziar ou bater o ``budget``. Eventos
    com JSON corrompido são pulados (log debug). Nunca levanta — devolve o que
    conseguiu drenar."""
    key = buffer_key(integration_id, stream)
    out: List[dict] = []
    while len(out) < budget:
        remaining = budget - len(out)
        count = min(_DRAIN_CHUNK, remaining)
        try:
            chunk = await redis.rpop(key, count)
        except TypeError:
            # Redis < 6.2 (sem count): faz um RPOP simples por vez.
            single = await redis.rpop(key)
            chunk = [single] if single is not None else None
        if not chunk:
            break
        if isinstance(chunk, (str, bytes)):
            chunk = [chunk]
        for raw in chunk:
            if raw is None:
                continue
            try:
                out.append(json.loads(raw))
            except (TypeError, ValueError):
                logger.debug("ingest_buffer: evento com JSON inválido descartado")
    return out


async def buffer_depth(redis: Any, integration_id: int, stream: str) -> int:
    """Profundidade atual do buffer (para saúde/observabilidade)."""
    try:
        return int(await redis.llen(buffer_key(integration_id, stream)) or 0)
    except Exception:  # noqa: BLE001
        return 0
