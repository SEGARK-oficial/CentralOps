"""Identidade de evento, supressão por assinatura e saúde do Redis.

``compute_message_id`` dá o id de dedupe de um evento: o id nativo do vendor
quando existe (mais robusto a replays), senão SHA-256 sobre um conjunto
determinístico de campos — cuidado para não incluir timestamps de coleta ou
headers que mudem entre reenvios.

O dedupe em si NÃO mora mais aqui: é a janela em memória por partição do raw
(``state.dedupe_window``, ADR-0018 Onda 3c). A chave Redis por evento com TTL
(``dedupe:{integração}:{id}``), o conjunto de claims pendentes e o TTL
configurável saíram na Onda 5 — eram o gargalo do caminho quente (1 round-trip
por evento) e a fonte de evicção silenciosa do Redis.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

import redis.asyncio as redis_async

from .. import metrics

logger = logging.getLogger(__name__)

# Campos comumente presentes como id primário em payloads de vendors.
_ID_CANDIDATES = ("id", "alertId", "eventId", "uuid", "incidentId")


def compute_message_id(
    event: Dict[str, Any],
    fallback_fields: Iterable[str] = (),
) -> str:
    for candidate in _ID_CANDIDATES:
        value = event.get(candidate)
        if value not in (None, ""):
            return str(value)

    fields = list(fallback_fields) or sorted(event.keys())
    blob = json.dumps(
        {k: event.get(k) for k in fields},
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ── suppression durável por assinatura ─────────────────────

SUPPRESS_KEY_TMPL = "cops:suppress:{route_id}:{signature}"


def suppress_signature(labels: Dict[str, Any], suppress_key: str) -> Optional[str]:
    """Assinatura estável (16 hex) de um evento p/ rate-limit de supressão, ou ``None``
    quando a assinatura seria DEGENERADA (ver abaixo).

    ``suppress_key`` é uma lista CSV de labels DE ROTEAMENTO (mesma allowlist da ``condition``;
    ex.: ``"vendor,severity_id"``);
    a assinatura é o SHA-256 dos VALORES desses labels. Sem PII em métrica: a
    assinatura é hasheada (nunca vira label de OTel).

    FAIL-SAFE DE ASSINATURA DEGENERADA — devolve ``None`` quando NENHUM componente
    resolveu (todos os labels ausentes/vazios). Sem isso, uma chave que não existe no
    escopo de labels fazia TODOS os eventos colapsarem na MESMA assinatura
    (``labels.get(k, "")`` → ``""`` para todo mundo) e, passados ``suppress_allow``
    eventos na janela, o pipeline descartava 100% do tráfego — em silêncio, sem erro,
    sem DLQ. Foi exatamente o que aconteceu em produção com o ``suppress_key`` que a
    própria UI sugeria (``src_ip``, que nunca existe em ``_centralops``).

    Uma assinatura sem nenhum componente resolvido não IDENTIFICA nada: agrupar por
    ela é indistinguível de "descarte tudo". Preferimos não suprimir (fail-open,
    coerente com o resto da supressão, que é otimização de custo e jamais correção).
    Resolução PARCIAL (alguns componentes vazios) segue válida — é o caso legítimo de
    "agrupa os que não têm o campo"."""
    parts = [str(labels.get(k.strip(), "")) for k in (suppress_key or "").split(",") if k.strip()]
    if not parts or not any(parts):
        return None
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


async def claim_suppress(
    redis: redis_async.Redis,
    route_id: str,
    signature: str,
    allow: int,
    window_s: int,
) -> "tuple[bool, int]":
    """Rate-limit "Number-to-Allow": deixa passar os primeiros ``allow`` eventos de
    ``(route_id, signature)`` por janela de ``window_s`` s; suprime o resto.

    Retorna ``(keep, count)``: ``keep`` = este evento deve ser entregue; ``count`` = total
    visto na janela (p/ decorar o liberado com ``suppress_count``, preservando a contagem
    p/ detecção). Usa ``INCR`` (atômico) + ``EXPIRE`` no 1º da janela (TTL auto-poda a
    chave — sem estado durável a limpar). ``allow<=0`` = supressão desligada p/ a rota
    (no-op, sem I/O). Best-effort no call site: um erro de Redis NÃO deve derrubar a coleta
    (fail-OPEN → entrega; supressão é otimização, não correção)."""
    if allow <= 0:
        return True, 0
    key = SUPPRESS_KEY_TMPL.format(route_id=route_id, signature=signature)
    count = int(await redis.incr(key))
    if count == 1:
        await redis.expire(key, max(int(window_s), 1))
    return (count <= allow), count


# ── saúde do Redis: visibilidade de evicção silenciosa ──────────────


@dataclass(frozen=True)
class RedisHealth:
    """Snapshot de ``INFO`` do Redis — o suficiente para detectar evicção antes
    que ela apague estado de controle (cursor, supressão, breaker, OAuth)."""

    evicted_keys: int
    used_memory_bytes: int
    maxmemory_bytes: int
    maxmemory_policy: str

    @property
    def memory_used_ratio(self) -> float:
        """``used_memory / maxmemory``. ``0.0`` quando ``maxmemory`` não está
        configurado (sem teto ⇒ sem pressão por definição, embora nesse caso o
        risco vire "Redis derruba o processo/host", não evicção)."""
        if self.maxmemory_bytes <= 0:
            return 0.0
        return self.used_memory_bytes / self.maxmemory_bytes


async def sample_redis_health(redis: redis_async.Redis) -> Optional[RedisHealth]:
    """Amostra ``INFO`` do Redis e expõe evicção/pressão de memória como
    gauges OTel (``collector_dedupe_redis_evicted_keys`` e
    ``collector_dedupe_redis_memory_used_ratio``).

    Sample de PROCESSO/INSTÂNCIA, nunca por evento: chame periodicamente (ex.:
    1x/min) de uma task de manutenção. Os nomes das métricas guardam o prefixo
    ``dedupe`` porque as regras de alerta já os usam; desde a Onda 5 do
    ADR-0018 o Redis não guarda mais chave de dedupe por evento.

    ``evicted_keys`` é o contador cru do Redis inteiro — o Redis não expõe
    evicção por prefixo, então este é o sinal mais barato disponível: se ele
    sobe, ALGO está sendo evictado sob pressão de memória. ``memory_used_ratio``
    alerta ANTES da evicção começar.

    Best-effort: erro de Redis aqui é logado e retorna ``None`` — observabilidade
    nunca deve derrubar quem a chama (mesmo espírito de ``claim_suppress``, mas
    aqui não há decisão de negócio a fazer fail-open/closed, só não propagar).
    """
    try:
        info = await redis.info()
    except Exception:  # pragma: no cover — best-effort, nunca propaga
        logger.warning("sample_redis_health: INFO falhou", exc_info=True)
        return None

    health = RedisHealth(
        evicted_keys=int(info.get("evicted_keys", 0) or 0),
        used_memory_bytes=int(info.get("used_memory", 0) or 0),
        maxmemory_bytes=int(info.get("maxmemory", 0) or 0),
        maxmemory_policy=str(info.get("maxmemory_policy", "") or ""),
    )
    metrics.DEDUPE_REDIS_EVICTED_KEYS.set(float(health.evicted_keys))
    metrics.DEDUPE_REDIS_MEMORY_USED_RATIO.set(health.memory_used_ratio)
    if health.evicted_keys > 0:
        logger.warning(
            "redis: %d chaves evictadas (policy=%s, used=%d/%d bytes) — estado "
            "de controle (cursor, supressão, breaker) pode estar sumindo",
            health.evicted_keys,
            health.maxmemory_policy,
            health.used_memory_bytes,
            health.maxmemory_bytes,
        )
    return health
