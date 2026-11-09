"""Nomes canônicos de queues e tasks — string constants (evita typos).

Mudança aqui é contrato API — reflete no ``celery_app.task_routes``,
nos serviços Docker (``-Q ...``) e em testes.
"""

from __future__ import annotations

# Queues Celery (RF07).
Q_PRIORITY = "collect.priority"
Q_BULK = "collect.bulk"
# Reenvio da DLQ de um destino (``drain_destination_dlq``). A entrega em si é do
# data-plane Kafka: as filas ``dispatch.destination.N`` saíram (ADR-0018 Onda 5).
Q_DLQ = "dispatch.dlq"
# Fila DEDICADA de query ao vivo (QueryService). NUNCA compartilha
# com collect.bulk (noisy-neighbor) — um worker dedicado a consome
# (collector-worker-query, ``-Q collect.query``). Query lenta de um tenant não
# trava a ingestão realtime.
Q_QUERY = "collect.query"

# Tasks — os nomes canônicos usados em @celery_app.task(name=...).
T_COLLECT_PRIORITY = "collectors.collect_vendor_logs_priority"
T_COLLECT_BULK = "collectors.collect_vendor_logs_bulk"

# Legacy scheduler (migração de services/scheduler.py → Celery Beat).
T_SCHED_DISPATCH_DUE = "collectors.scheduler.dispatch_due_scheduled_queries"
T_SCHED_RUN = "collectors.scheduler.run_scheduled_query"
T_SCHED_PRUNE_RESULTS = "collectors.scheduler.prune_search_result_retention"

# Execução de um job de query federada ao vivo (QueryService).
T_QUERY_RUN_JOB = "collectors.query.run_job"
# Poll curto/idempotente de runs async (Sophos Data Lake) — libera
# o worker em vez de bloquear no wait_and_fetch.
T_QUERY_POLL_JOB = "collectors.query.poll_job"
