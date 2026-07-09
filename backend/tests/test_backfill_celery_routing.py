"""Smoke tests de roteamento Celery para backfill (Bug 2).

Verifica que:
1. A task ``collectors.collect_backfill_job`` está registrada no
   celery_app (pode ser descoberta por workers que consomem collect.backfill).
2. O ``task_routes`` da conf Celery aponta ``collect.backfill_job`` para
   a fila ``collect.backfill`` — garantia de que o Beat despacha para a
   fila certa e o worker-bulk (que agora consome collect.backfill) vai
   processar.

Contexto: antes do fix, nenhum worker consumia ``collect.backfill``.
``collector-worker-priority`` consome ``collect.priority``,
``collector-worker-bulk`` consumia apenas ``collect.bulk``,
``collector-dispatcher`` consome as filas de dispatch por-destino
(``dispatch.dest.*,dispatch.dlq``).
Resultado: tasks acumulavam na fila Redis indefinidamente.

Fix: worker-bulk agora lista ``collect.bulk,collect.backfill`` em -Q.
Ver: compose/docker-compose.yml (serviço collector-worker-bulk).
"""

from __future__ import annotations

import backend.app.collectors.celery_app as celery_mod


def test_collect_backfill_job_task_registered() -> None:
    """A task collectors.collect_backfill_job deve estar registrada no app.

    Workers que consomem collect.backfill só podem processar a task se
    ela estiver no registry do Celery. Importar backfill_tasks é suficiente
    (o decorator @celery_app.task registra automaticamente).
    """
    # Importação tardio para garantir que backfill_tasks foi carregado.
    import backend.app.collectors.backfill_tasks  # noqa: F401 — side-effect: registra task

    registered = set(celery_mod.celery_app.tasks.keys())
    assert "collectors.collect_backfill_job" in registered, (
        "Task 'collectors.collect_backfill_job' não está registrada no Celery app. "
        "Workers não conseguem executá-la mesmo que consumam a fila collect.backfill."
    )


def test_celery_routing_assigns_collect_backfill_queue() -> None:
    """O task_routes deve mapear collect_backfill_job para collect.backfill.

    Isso garante que qualquer chamada ``apply_async`` sem ``queue=`` explícito
    vai para a fila correta, que o worker-bulk agora consome.
    """
    routes = celery_mod.celery_app.conf.task_routes or {}

    # Aceita dict direto ou lista de (pattern, route).
    if isinstance(routes, dict):
        route = routes.get("collectors.collect_backfill_job", {})
    else:
        # Lista de (padrão, dict) — busca por nome exato.
        route = {}
        for pattern, mapping in routes:
            if pattern == "collectors.collect_backfill_job":
                route = mapping
                break

    assert route.get("queue") == "collect.backfill", (
        f"Rota para collectors.collect_backfill_job aponta para "
        f"'{route.get('queue')}' em vez de 'collect.backfill'. "
        "Worker-bulk consumindo collect.backfill não vai processar a task."
    )
