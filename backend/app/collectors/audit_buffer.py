"""Ring buffer dos últimos eventos despachados — para auditoria/tuning.

**Por que existe.** Operadores precisam saber *o que exatamente* o Collector
está mandando pro Wazuh para:

1. Validar se o decoder Wazuh está casando (rodar trecho no ``wazuh-logtest``).
2. Caçar campos vazios / bugs de enrichment (ex: ``severity`` ausente).
3. Confirmar shape antes de escrever rules novas.

**Por que Redis e não banco.** Auditoria é *ao vivo*, janela curta. Gravar
em Postgres/SQLite a cada evento duplica a carga de dispatch e cria
tabela gigante para inspeção rara. Redis com ``LPUSH`` + ``LTRIM`` mantém
N eventos num ring de custo constante. Arquivo JSONL (modo ``both``) já
cobre o forense de longo prazo.

**Contrato.**

- ``record_batch(redis, batch, org_id)`` é chamado dentro do ``dispatch_batch``
  logo após ``send_batch`` ter sucesso. Best-effort — não deve quebrar o
  fluxo de dispatch em caso de falha no Redis.
- Usa ``LPUSH`` + ``LTRIM 0 N-1`` para manter só os últimos N eventos.
- Cada entrada é o JSON do evento enriquecido (mesma carga que foi ao
  Wazuh), serializado como string compacta — com **PII redactada**.
- Chave: ``collector:audit:{org_id}:recent``. Segmentada
  por tenant — o ring "log tapping" era GLOBAL e misturava todos os
  tenants (vazamento cross-tenant verificado). A leitura exige ``org_id``.
"""

from __future__ import annotations

import json
import logging
import socket
from typing import Any, Dict, List, Optional

import redis.asyncio as redis_async

from ..core.logging_config import SENSITIVE_FIELD_NAMES

logger = logging.getLogger(__name__)


def _audit_key(org_id: int) -> str:
    """Chave do ring de auditoria escopada por tenant."""
    return f"collector:audit:{org_id}:recent"


def _redact(obj: Any) -> Any:
    """Redação recursiva de PII/segredos.

    Reusa ``SENSITIVE_FIELD_NAMES`` do logging — qualquer chave cujo nome
    (lowercased) esteja na lista tem o valor trocado por ``"[REDACTED]"``.
    Não muta o original (constrói cópia)."""
    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in SENSITIVE_FIELD_NAMES:
                out[k] = "[REDACTED]"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj

# Tamanho do ring — balanço entre "ter contexto útil" e "custo de RAM".
# 500 eventos × ~2KB = ~1MB. Barato para qualquer deploy Redis.
DEFAULT_RING_SIZE = 500

# TTL global da chave — se ninguém usar o audit por 24h, o ring expira
# sozinho (útil em deploys de teste que rotacionam containers).
DEFAULT_TTL_SECONDS = 86400

# PRI usado pelo SyslogTCPClient (facility=local0, severity=info).
# Gravado no ring para que a UI reconstrua a linha RFC 5424 fiel.
_PRI_INFO = 134


async def record_batch(
    redis: redis_async.Redis,
    batch: List[Dict[str, Any]],
    org_id: int,
    *,
    ring_size: int = DEFAULT_RING_SIZE,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    syslog_format: Optional[str] = None,
) -> None:
    """Grava o lote no ring buffer. Best-effort.

    Cada entrada é envelopada com o **contexto da linha wire real**
    (hostname do processo + PRI + formato syslog) para que a UI
    reconstrua a linha byte-a-byte idêntica à que o dispatcher enviou ao
    Wazuh — sem placeholder. Shape no ring:

        {"envelope": {"hostname": "...", "pri": 134},
         "event":    {...},
         "syslog_format": "rfc3164"}   # None em entradas legadas

    ``syslog_format`` deve ser ``"rfc3164"`` ou ``"rfc5424"`` (ou None
    para entradas legadas — UI assume rfc5424 nesses
    casos para compatibilidade com o comportamento anterior).
    """
    if not batch:
        return
    key = _audit_key(org_id)
    try:
        # gethostname() reflete o host do worker que fez o dispatch —
        # mesma string usada pelo ``syslog_sender.format_rfc5424``.
        hostname = socket.gethostname()
        envelope = {"hostname": hostname, "pri": _PRI_INFO}

        serialized = [
            json.dumps(
                {
                    "envelope": envelope,
                    # PII/segredos redactados antes de gravar no ring.
                    "event": _redact(event),
                    "syslog_format": syslog_format,
                    "org_id": org_id,  # redundância defensiva além da chave
                },
                separators=(",", ":"),
                default=str,
            )
            for event in batch
        ]
        pipe = redis.pipeline()
        pipe.lpush(key, *serialized)
        pipe.ltrim(key, 0, ring_size - 1)
        pipe.expire(key, ttl_seconds)
        await pipe.execute()
    except Exception as exc:  # pragma: no cover — nunca quebrar dispatch
        logger.warning("audit_buffer: falha ao gravar ring (%s)", exc)


async def read_recent(
    redis: redis_async.Redis,
    org_id: int,
    *,
    limit: int = 100,
    platform: Optional[str] = None,
    vendor: Optional[str] = None,
    stream: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Lê últimos eventos do ring. Filtra client-side por vendor/stream.

    Retorna lista de dicts no formato ``{event, envelope}``. Tolerante a
    entradas legadas que não tinham envelope (fallback vazio — usuário
    verá um aviso visual na UI).

    ``platform`` é alias retrocompatível para ``vendor``: o envelope
    canônico usa ``vendor``, mas chamadas antigas continuam
    funcionando.
    """
    limit = max(1, min(limit, DEFAULT_RING_SIZE))
    vendor_filter = vendor or platform
    try:
        raw = await redis.lrange(_audit_key(org_id), 0, limit - 1)
    except Exception as exc:  # pragma: no cover
        logger.warning("audit_buffer: falha ao ler ring (%s)", exc)
        return []

    events: List[Dict[str, Any]] = []
    for item in raw:
        try:
            parsed = json.loads(item)
        except (json.JSONDecodeError, TypeError):
            continue

        # Formato atual: {"envelope": {...}, "event": {...}}
        # Formato legado (antes do fix de fidelidade): o próprio evento
        # sem envelope. Detectamos por presença da chave.
        if "envelope" in parsed and "event" in parsed:
            envelope = parsed["envelope"]
            event = parsed["event"]
            # syslog_format presente em entradas recentes; None em entradas legadas.
            syslog_fmt: Optional[str] = parsed.get("syslog_format")
        else:
            envelope = {}
            event = parsed
            syslog_fmt = None

        meta = event.get("_centralops") or {}
        # ``vendor`` é o canônico; ``platform`` é o legado.
        event_vendor = meta.get("vendor") or meta.get("platform")
        if vendor_filter and event_vendor != vendor_filter:
            continue
        if stream and meta.get("stream") != stream:
            continue
        events.append({"event": event, "envelope": envelope, "syslog_format": syslog_fmt})
    return events


async def clear(redis: redis_async.Redis, org_id: int) -> int:
    """Zera o ring do tenant (útil em testes/demo). Retorna nº removidos."""
    key = _audit_key(org_id)
    try:
        length = await redis.llen(key)
        await redis.delete(key)
        return int(length)
    except Exception:  # pragma: no cover
        return 0
