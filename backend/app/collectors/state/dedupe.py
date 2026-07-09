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
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable

import redis.asyncio as redis_async

DEFAULT_TTL_DAYS = 7
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


# ── suppression durável por assinatura ─────────────────────

SUPPRESS_KEY_TMPL = "cops:suppress:{route_id}:{signature}"


def suppress_signature(labels: Dict[str, Any], suppress_key: str) -> str:
    """Assinatura estável (16 hex) de um evento p/ rate-limit de supressão. ``suppress_key``
    é uma lista CSV de nomes de label (ex.: ``"src_ip,event_type"``); a assinatura é o
    SHA-256 dos VALORES desses labels. Labels ausentes entram como "" (agrupa os sem-campo).
    Sem PII em métrica: a assinatura é hasheada (nunca vira label de OTel)."""
    parts = [str(labels.get(k.strip(), "")) for k in (suppress_key or "").split(",") if k.strip()]
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
