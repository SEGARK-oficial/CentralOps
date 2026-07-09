"""Testes de isolamento Redis para workers Celery prefork (Bug 1 — revert W3).

Garante que:
1. ``get_worker_redis`` retorna objetos distintos por chamada (sem pool global).
2. O atributo ``_worker_redis_pool`` não existe mais no módulo celery_app
   (evidência direta da reversão do W3).

Razão do isolamento: workers Celery prefork chamam ``asyncio.run()`` por task,
criando um loop novo a cada execução. Um ``ConnectionPool`` async criado no
loop do processo-pai tem futures vinculadas àquele loop; quando a task tenta
usá-lo em seu próprio loop, o redis-py lança "Event loop is closed" /
"Future attached to a different loop". Solução: cliente efêmero por task.
"""

from __future__ import annotations

import backend.app.collectors.celery_app as celery_mod


def test_get_worker_redis_returns_fresh_client_per_call() -> None:
    """Duas chamadas consecutivas retornam objetos distintos.

    Com pool compartilhado, redis-py devolve o mesmo client wrapper
    (pool interno é singleton). Sem pool, from_url cria objeto novo
    a cada chamada — garantia de isolamento de event loop entre tasks.
    """
    client_a = celery_mod.get_worker_redis()
    client_b = celery_mod.get_worker_redis()

    # Objetos distintos → sem estado compartilhado entre loops de tasks.
    assert client_a is not client_b, (
        "get_worker_redis deveria retornar instâncias distintas por chamada; "
        "pool compartilhado causa 'Event loop is closed' em Celery prefork"
    )


def test_get_worker_redis_no_pool_shared() -> None:
    """O atributo ``_worker_redis_pool`` não deve mais existir no módulo.

    Sua presença seria indício de que o W3 (pool compartilhado) ainda
    está ativo e pode causar "Event loop is closed" em produção.
    """
    assert not hasattr(celery_mod, "_worker_redis_pool"), (
        "_worker_redis_pool ainda existe em celery_app — W3 não foi completamente "
        "revertido. Pool compartilhado quebra workers Celery prefork."
    )
