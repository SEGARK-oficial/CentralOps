"""task_ignore_result — fire-and-forget.

O pipeline é assíncrono e ninguém lê o VALOR de retorno das tasks (os call-sites
só usam ``result.id`` e ``AsyncResult.revoke()``, ambos independentes do result
backend). Gravar um result-row no Redis por task era escrita pura no broker
single-node a cada despacho (gargalo). Confirmamos que a config
global ignora results — sem desabilitar o backend (tasks podem optar por
``ignore_result=False`` e ferramentas de inspeção continuam funcionando).
"""

from __future__ import annotations

# Importa o módulo de tasks para garantir o registro no app (include é lazy).
import backend.app.collectors.tasks  # noqa: F401
from backend.app.collectors.celery_app import celery_app


def test_task_ignore_result_is_enabled() -> None:
    assert celery_app.conf.task_ignore_result is True


def test_result_backend_still_configured_for_opt_in() -> None:
    # ignore_result NÃO desabilita o backend: result_expires segue setado e o
    # backend continua resolvido (uma task pode optar por ignore_result=False).
    assert celery_app.conf.result_expires == 3600
    assert celery_app.conf.result_backend, "result backend deve seguir configurado"


def test_dispatch_tasks_do_not_opt_into_storing_results() -> None:
    """Tasks de despacho herdam o default do app (não gravam result próprio)."""
    for name in ("collectors.dispatch_to_destination", "collectors.dispatch_to_dlq"):
        task = celery_app.tasks.get(name)
        assert task is not None, f"task {name} deve estar registrada"
        # ignore_result no nível da task: None => herda o conf (True). Garantimos
        # que nenhuma destas opta explicitamente por GRAVAR result (False).
        assert task.ignore_result in (None, True)
