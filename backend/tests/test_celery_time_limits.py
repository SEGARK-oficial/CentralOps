"""A cadeia de tempos das tasks Celery: nada é reentregue enquanto ainda roda.

Antes este teste morava em ``test_dedupe_ttl_invariant`` e ancorava o TTL da
chave Redis de dedupe por evento. A chave saiu (ADR-0018 Onda 5: o dedupe é a
janela em memória do processor), mas a ordem dos tempos continua valendo para o
que o Celery ainda executa — coleta, backfill, reenvio da DLQ, query.
"""

from __future__ import annotations

from backend.app.collectors.celery_app import celery_app
from backend.app.collectors.dispatch_runtime import DISPATCH_RESULT_TIMEOUT


def test_celery_redelivery_ordering_invariant_holds() -> None:
    """DISPATCH_RESULT_TIMEOUT < task_soft_time_limit < task_time_limit
    < visibility_timeout."""
    soft = celery_app.conf.task_soft_time_limit
    hard = celery_app.conf.task_time_limit
    opts = celery_app.conf.broker_transport_options or {}
    visibility = opts.get("visibility_timeout")
    assert visibility is not None, "broker_transport_options.visibility_timeout não configurado"

    assert DISPATCH_RESULT_TIMEOUT < soft
    assert soft < hard
    assert hard < int(visibility), (
        f"task_time_limit={hard}s deveria ser < visibility_timeout={visibility}s — "
        "senão o broker reentrega ANTES do hard time-limit matar a task presa, e um "
        "worker saudável recebe uma 2ª cópia da MESMA task ainda em execução."
    )
