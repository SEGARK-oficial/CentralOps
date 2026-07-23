"""Idempotência por ``message_id``.

Estratégia:

    SET dedupe:{integration_id}:{message_id} 1 NX EX <ttl>

O ``NX`` garante atomicidade: só o primeiro worker a ver o evento
consegue reclamar a chave; os demais recebem ``None`` e devem
descartar o evento silenciosamente.

``compute_message_id`` tenta usar o id nativo do vendor (mais robusto a
replays). Quando o evento não tem id natural, cai em SHA-256 sobre um
conjunto determinístico de campos — cuidado para não incluir timestamps
de coleta ou headers que mudem entre reenvios.

**Este é o ÚNICO guard de idempotência no hot path** (1 round-trip Redis por
evento — o gargalo dominante do pipeline, ~2-4k EPS/task). O Redis do compose
roda ``volatile-lru`` com 512mb (compose/docker-compose.yml) — memória finita.
Um TTL longo demais contra memória finita = evicção silenciosa: a chave
``dedupe:*`` some ANTES do TTL lógico expirar, o próximo `claim()` do MESMO
evento retorna ``True`` (parece novo) e o evento é reentregue como duplicata
— sem exceção, sem log, sem sinal algum (já houve incidente real com 310k
chaves evicted). Ver ``sample_redis_health`` abaixo para tornar isso visível.
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

# TTL default do dedupe (dias) — usado só como fallback do parâmetro de
# ``claim()``; em produção o valor real vem de settings/config (por-org, ver
# ``config_loader.CollectorConfigSnapshot.dedupe_ttl_days`` e
# ``core.config.Settings.DEDUPE_TTL_DAYS``, ambos default 1 dia após esta
# revisão).
#
# Por que 1 dia (era 7): o TTL só precisa cobrir a janela real de REENTREGA
# possível — não é dedupe de longo prazo (isso é papel do destino, ver
# docstring de ``release()`` abaixo). Medido neste repo:
#   - overlap de polling entre ciclos: schedule por vendor é de 1-5 min
#     (registry.py: sophos=1min; defender/crowdstrike/okta/wazuh=2min;
#     veeam/ninjaone/aws=5min) — a janela onde o MESMO evento pode aparecer
#     em dois polls consecutivos é da ordem de minutos.
#   - retry automático do Celery (tasks.py collect_vendor_logs_*): backoff
#     exponencial com jitter, teto 120s/600s, max_retries 5/8 — soma do pior
#     caso (sem contar tempo de execução) < 5 min.
#   - crash TOTAL do worker ANTES do except de pipeline.py:784-798 rodar
#     (SIGKILL/OOM — o release() nunca executa, a claim fica ÓRFÃ): o pior
#     caso de redelivery é limitado pelo broker Redis via
#     acks_late+task_reject_on_worker_lost+visibility_timeout=3600s (1h),
#     invariante já documentada em celery_app.py:248-256
#     (DISPATCH_RESULT_TIMEOUT(600) < soft(720) < hard(900) < visibility(3600)).
# 24h dá ~24x de folga sobre o pior caso AUTOMÁTICO (1h) e ainda cobre um
# replay MANUAL do mesmo turno (operador investigando um incidente). E, ao
# contrário do TTL de 7 dias, uma claim ÓRFÃ (crash que nunca chama release())
# agora tem raio de silêncio máximo de 24h em vez de 7 dias — ela se
# auto-cicatriza (expira e volta a ser reclamável) muito mais rápido, E o
# footprint de memória do keyspace ``dedupe:*`` cai ~7x (chaves vivem 1 dia em
# vez de 7), aliviando diretamente a pressão que causa evicção sob os 512mb do
# compose. Isto é seguro reduzir porque o dedupe Redis é OTIMIZAÇÃO, não a
# única linha de defesa: reentregas além do TTL são absorvidas pelo dedupe no
# destino por ``event_id`` (at-least-once — ver docstring de ``release()``).
#
# NÃO baixe abaixo de ~1h (o teto de visibility_timeout) sem reavaliar a
# invariante — ver test_dedupe_ttl_invariant.py.
DEFAULT_TTL_DAYS = 1
KEY_TMPL = "dedupe:{integration_id}:{message_id}"

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


async def claim(
    redis: redis_async.Redis,
    integration_id: int,
    message_id: str,
    ttl_days: int = DEFAULT_TTL_DAYS,
) -> bool:
    """True se o evento é inédito (pode ser despachado). False se duplicado."""
    key = KEY_TMPL.format(integration_id=integration_id, message_id=message_id)
    result = await redis.set(key, "1", nx=True, ex=ttl_days * 86400)
    return bool(result)


async def release(
    redis: redis_async.Redis,
    integration_id: int,
    message_id: str,
) -> None:
    """Solta uma claim de dedupe (compensação).

    Usado quando o evento foi reclamado mas o hand-off durável (produce no
    data-plane Kafka) FALHOU: sem soltar a chave, o reprocesso pós-falha
    (cursor não avança) re-veria o evento, ``claim()`` retornaria False, e ele
    seria descartado como "duplicado" — PERDA SILENCIOSA. Soltar a claim deixa
    o retry re-reclamar e re-produzir (at-least-once + dedupe no destino por
    event_id absorve qualquer reentrega). Best-effort: erro de Redis aqui não
    deve mascarar a exceção original do produce.
    """
    key = KEY_TMPL.format(integration_id=integration_id, message_id=message_id)
    await redis.delete(key)


async def release_many(
    redis: redis_async.Redis,
    integration_id: int,
    message_ids: Iterable[str],
) -> int:
    """``DEL`` em lote das claims de ``message_ids``. Devolve quantas soltou.

    Mesma compensação de :func:`release`, em uma única chamada. Pipelinar AQUI é
    seguro — e é a diferença que importa em relação a pipelinar o ``claim``:
    nenhuma decisão de "processar ou pular" depende deste resultado. O ``claim``
    é caminho de DECISÃO (agrupar exigiria bufferizar eventos e criaria uma
    janela onde chaves são reivindicadas para eventos ainda não processados);
    o release é caminho de COMPENSAÇÃO, executado quando o run já falhou.

    Best-effort: falha de Redis é engolida pelo chamador. O residual é claim
    órfã até o TTL — que é exatamente o estado de hoje, então não piora nada.
    """
    ids = [m for m in message_ids if m]
    if not ids:
        return 0
    keys = [
        KEY_TMPL.format(integration_id=integration_id, message_id=m) for m in ids
    ]
    await redis.delete(*keys)
    return len(keys)


# ── suppression durável por assinatura ─────────────────────

SUPPRESS_KEY_TMPL = "cops:suppress:{route_id}:{signature}"


def suppress_signature(labels: Dict[str, Any], suppress_key: str) -> Optional[str]:
    """Assinatura estável (16 hex) de um evento p/ rate-limit de supressão, ou ``None``
    quando a assinatura seria DEGENERADA (ver abaixo).

    ``suppress_key`` é uma lista CSV de nomes de label (ex.: ``"vendor,event_type"``);
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
    """Snapshot de ``INFO`` do Redis usado pelo dedupe — o suficiente para
    detectar evicção antes que vire reentrega silenciosa."""

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

    **NÃO chame isto no hot path de ``claim()``** — seria um 2º round-trip
    Redis POR EVENTO, dobrando o gargalo dominante do pipeline. Este é um
    sample de PROCESSO/INSTÂNCIA (não por-evento): chame periodicamente (ex.:
    1x/min) de um healthcheck ou task de manutenção — o custo é 1 ``INFO`` por
    ciclo, amortizado sobre milhares de eventos.

    ``evicted_keys`` é o contador cru do Redis inteiro (não filtrado pelo
    prefixo ``dedupe:``) — o Redis não expõe evicção por-prefixo/por-keyspace,
    então este é o sinal mais barato e direto disponível: se ele estiver
    subindo, ALGO está sendo evictado sob pressão de memória, e como o dedupe
    é o maior consumidor de chaves com TTL deste Redis, é o suspeito primário.
    ``memory_used_ratio`` alerta ANTES da evicção começar (útil para calibrar
    ``REDIS_MAXMEMORY``/``DEDUPE_TTL_DAYS`` antes do incidente, não depois).

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
            "dedupe: Redis reportou %d chaves evictadas (policy=%s, "
            "used=%d/%d bytes) — dedupe pode estar deixando reentregas "
            "passarem como \"novas\" silenciosamente",
            health.evicted_keys,
            health.maxmemory_policy,
            health.used_memory_bytes,
            health.maxmemory_bytes,
        )
    return health
