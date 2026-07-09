"""Nomes canônicos de queues e tasks — string constants (evita typos).

Mudança aqui é contrato API — reflete no ``celery_app.task_routes``,
nos serviços Docker (``-Q ...``) e em testes.
"""

from __future__ import annotations

import hashlib

# Queues Celery (RF07).
Q_PRIORITY = "collect.priority"
Q_BULK = "collect.bulk"
# A fila dedicada Wazuh (Q_DISPATCH="dispatch.wazuh") foi removida.
Q_DLQ = "dispatch.dlq"
# Fila genérica do dispatcher multi-destino (catch-all).
# Ativa — multi-destino é GA.
Q_DISPATCH_DESTINATION = "dispatch.destination"
# Fila DEDICADA de query ao vivo (QueryService). NUNCA compartilha
# com collect.bulk (noisy-neighbor) — um worker dedicado a consome
# (collector-worker-query, ``-Q collect.query``). Query lenta de um tenant não
# trava a ingestão realtime.
Q_QUERY = "collect.query"

# ── Bulkhead — hash-routing para N shard queues ─────────────────────
# Destinos são dinâmicos (linhas no DB), então não dá pra pré-declarar uma fila
# Celery por destino. Aproximação: hash estável do ``destination_id`` em N
# shards fixos (``dispatch.destination.0..N-1``). Cada destino sempre cai no
# MESMO shard (ordering + cache de socket estáveis). Isolamento real = rodar um
# worker dedicado por shard (``-Q dispatch.destination.3``): um destino lento
# satura só o seu shard, não os EPS dos demais. O cap de concorrência GLOBAL por
# destino usa um lease Redis cross-process.
DISPATCH_DEST_SHARDS = 8


def dispatch_dest_shard_queue(destination_id: str) -> str:
    """Shard queue estável para um ``destination_id`` (sha1 % N)."""
    digest = hashlib.sha1(destination_id.encode("utf-8")).hexdigest()
    shard = int(digest, 16) % DISPATCH_DEST_SHARDS
    return f"{Q_DISPATCH_DESTINATION}.{shard}"


def all_dispatch_dest_queues() -> list[str]:
    """Lista das N shard queues — usada no registro de queues + ``-Q`` do worker."""
    return [f"{Q_DISPATCH_DESTINATION}.{i}" for i in range(DISPATCH_DEST_SHARDS)]

# Tasks — os nomes canônicos usados em @celery_app.task(name=...).
T_COLLECT_PRIORITY = "collectors.collect_vendor_logs_priority"
T_COLLECT_BULK = "collectors.collect_vendor_logs_bulk"
T_DISPATCH_DESTINATION = "collectors.dispatch_to_destination"
T_DISPATCH_DLQ = "collectors.dispatch_to_dlq"

# Legacy scheduler (migração de services/scheduler.py → Celery Beat).
T_SCHED_DISPATCH_DUE = "collectors.scheduler.dispatch_due_scheduled_queries"
T_SCHED_RUN = "collectors.scheduler.run_scheduled_query"
T_SCHED_PRUNE_RESULTS = "collectors.scheduler.prune_search_result_retention"

# Execução de um job de query federada ao vivo (QueryService).
T_QUERY_RUN_JOB = "collectors.query.run_job"
# Poll curto/idempotente de runs async (Sophos Data Lake) — libera
# o worker em vez de bloquear no wait_and_fetch.
T_QUERY_POLL_JOB = "collectors.query.poll_job"
