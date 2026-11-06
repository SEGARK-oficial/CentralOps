"""Buffer Redis de ingestão push (FortiGate syslog, Windows Event Log/WEC, …).

O endpoint ``POST /api/ingest/...`` valida + autentica e EMPURRA os eventos crus
para uma lista Redis por ``(integration_id, stream)``. O collector virtual
``PushBufferCollector`` DRENA essa lista dentro do ``run_collection_once`` normal
— reaproveitando 100% do pipeline existente (dedupe → mapping → routing → dispatch
→ quarentena → tracing). Não há um 2º caminho de normalização.

**Backpressure.** A lista tem teto ``max_len``. Um push que não cabe é RECUSADO
inteiro (o endpoint responde 503 + ``Retry-After`` e o edge re-tenta com o buffer
de disco dele). Antes era drop-oldest: o ``LTRIM`` apagava eventos de requisições
ANTERIORES, já respondidas com 200 — perda que a borda não tinha como reenviar
(ADR-0018 Onda 0). O teto continua protegendo a memória do Redis.

**Dreno confiável.** O dreno MOVE os eventos para uma lista "em processamento"
(``ingest:inflight:<int>:<stream>``) em vez de apagá-los. Só o fim de ciclo bem
sucedido (``on_cycle_committed``) apaga essa lista; um ciclo que falha ou morre
a deixa de pé, e o próximo dreno a reprocessa primeiro. As claims de dedupe dos
eventos que já tinham saído descartam a reentrega; os demais são processados.

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
_INFLIGHT_PREFIX = "ingest:inflight"
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


def inflight_key(integration_id: int, stream: str) -> str:
    """Lista dos eventos drenados por um ciclo e ainda não confirmados."""
    return f"{_INFLIGHT_PREFIX}:{integration_id}:{stream}"


def dropped_key(integration_id: int, stream: str) -> str:
    return f"{_DROPPED_PREFIX}:{integration_id}:{stream}"


#: Push tudo-ou-nada: cabe inteiro → LPUSH (em fatias, ``unpack`` tem teto de
#: argumentos) e devolve {aceitos, comprimento}; não cabe → {0, comprimento}.
_PUSH_LUA = """
local len = redis.call('LLEN', KEYS[1])
local n = #ARGV - 1
if len + n > tonumber(ARGV[1]) then
  return {0, len}
end
for i = 2, #ARGV, 500 do
  len = redis.call('LPUSH', KEYS[1], unpack(ARGV, i, math.min(i + 499, #ARGV)))
end
return {n, len}
"""

#: Move até ARGV[1] eventos da cauda do buffer (mais antigos) para a lista em
#: processamento, atômico: um evento nunca está fora das duas listas.
_MOVE_LUA = """
local items = redis.call('RPOP', KEYS[1], ARGV[1])
if not items then
  return {}
end
redis.call('LPUSH', KEYS[2], unpack(items))
return items
"""


async def push_events(
    redis: Any,
    integration_id: int,
    stream: str,
    events: List[Mapping[str, Any]],
    *,
    max_len: int = DEFAULT_MAX_LEN,
) -> Tuple[int, int]:
    """Empurra ``events`` para o buffer, TUDO OU NADA, com teto ``max_len``.

    Devolve ``(accepted, refused)``. ``refused > 0`` quer dizer que nada foi
    gravado: o buffer não comporta o lote inteiro e o chamador deve responder
    503 para o edge re-tentar. Nunca apaga evento já aceito (sem drop-oldest).
    """
    if not events:
        return 0, 0

    key = buffer_key(integration_id, stream)
    payloads = [
        json.dumps(dict(ev), separators=(",", ":"), default=str, ensure_ascii=False)
        for ev in events
    ]
    accepted, _length = await redis.eval(_PUSH_LUA, 1, key, max_len, *payloads)
    accepted = int(accepted or 0)
    refused = len(payloads) - accepted
    if refused:
        try:
            dpipe = redis.pipeline()
            dpipe.incrby(dropped_key(integration_id, stream), refused)
            dpipe.expire(dropped_key(integration_id, stream), _DROPPED_TTL)
            await dpipe.execute()
        except Exception:  # noqa: BLE001 — contador é best-effort
            logger.debug("ingest_buffer: falha ao incrementar contador de recusa")
        logger.warning(
            "ingest_buffer: backpressure integration_id=%s stream=%s recusou %d evento(s) "
            "(cap=%d) — dreno mais lento que a ingestão; o edge deve re-tentar",
            integration_id, stream, refused, max_len,
        )
    return accepted, refused


def _decode_all(raws: Any) -> List[dict]:
    out: List[dict] = []
    for raw in raws or ():
        if raw is None:
            continue
        try:
            out.append(json.loads(raw))
        except (TypeError, ValueError):
            logger.debug("ingest_buffer: evento com JSON inválido descartado")
    return out


async def drain_events(
    redis: Any,
    integration_id: int,
    stream: str,
    *,
    budget: int = DEFAULT_DRAIN_BUDGET,
) -> List[dict]:
    """Drena até ``budget`` eventos para processamento, SEM apagá-los.

    Primeiro relê o que um ciclo anterior deixou em processamento (falhou ou
    morreu antes de confirmar); depois move do buffer para a lista em
    processamento, dos mais antigos para os mais novos, até completar o
    ``budget``. Os eventos só somem em :func:`ack_drained`, chamado quando o
    ciclo termina com sucesso. Exceção do Redis propaga (o ciclo falha e nada
    se perde)."""
    ikey = inflight_key(integration_id, stream)
    leftover = await redis.lrange(ikey, 0, -1)
    out = _decode_all(leftover)
    if leftover:
        logger.warning(
            "ingest_buffer: reprocessando %d evento(s) de um ciclo não confirmado "
            "integration_id=%s stream=%s",
            len(leftover), integration_id, stream,
        )
    key = buffer_key(integration_id, stream)
    remaining = budget - len(leftover or ())
    while remaining > 0:
        chunk = await redis.eval(_MOVE_LUA, 2, key, ikey, min(_DRAIN_CHUNK, remaining))
        if not chunk:
            break
        out.extend(_decode_all(chunk))
        remaining -= len(chunk)
    return out


async def ack_drained(redis: Any, integration_id: int, stream: str) -> None:
    """Confirma o dreno do ciclo: apaga a lista em processamento."""
    await redis.delete(inflight_key(integration_id, stream))


async def buffer_depth(redis: Any, integration_id: int, stream: str) -> int:
    """Profundidade atual do buffer (para saúde/observabilidade)."""
    try:
        return int(await redis.llen(buffer_key(integration_id, stream)) or 0)
    except Exception:  # noqa: BLE001
        return 0
