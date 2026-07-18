"""Singleton Celery app do subsistema de collectors.

Três queues especializadas:

- ``collect.priority``  → EDR tempo real (Sophos alerts, Defender incidents).
- ``collect.bulk``      → auditoria e inventário (NinjaOne activities).
- ``dispatch.wazuh``    → envio desacoplado ao Wazuh.
- ``dispatch.dlq``      → dead-letter para investigação manual.

Workers são escalados horizontalmente via ``docker compose up --scale``.
Cada task abre sua própria ``SessionLocal`` efêmera — **nenhum
estado mantido em RAM entre execuções**.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from celery import Celery
from kombu import Queue

from ..core import ee_hooks
from ..core.config import settings
from .queues import all_dispatch_dest_queues

if TYPE_CHECKING:
    import redis.asyncio as redis_async_module

logger = logging.getLogger(__name__)


def _broker_url() -> str:
    if settings.CELERY_BROKER_URL:
        return settings.CELERY_BROKER_URL
    # Fallback: se só REDIS_URL estiver setado, usa db 1 do mesmo host.
    base = settings.REDIS_URL or "redis://localhost:6379/0"
    # Troca o número do DB por 1 (pragmático — assumimos sufixo /N).
    if base.rstrip("/").rsplit("/", 1)[-1].isdigit():
        root, _ = base.rsplit("/", 1)
        return f"{root}/1"
    return f"{base.rstrip('/')}/1"


def _result_backend() -> str:
    if settings.CELERY_RESULT_BACKEND:
        return settings.CELERY_RESULT_BACKEND
    base = settings.REDIS_URL or "redis://localhost:6379/0"
    if base.rstrip("/").rsplit("/", 1)[-1].isdigit():
        root, _ = base.rsplit("/", 1)
        return f"{root}/2"
    return f"{base.rstrip('/')}/2"


# ``__package__`` resolve em runtime: ``backend.app.collectors`` quando
# importado a partir da raiz do repo, ou ``app.collectors`` dentro do
# container Docker (CWD=/app). Isso evita hardcode que quebraria em um
# dos ambientes.
_MODULES_WITH_TASKS = [
    f"{__package__}.tasks",                  # coleta + dispatch
    f"{__package__}.scheduler_tasks",        # migração do services/scheduler.py legado
    f"{__package__}.backfill_tasks",         # backfill de janela histórica
    f"{__package__}.retention_tasks",        # purge de dados expirados
    # partner_sync_tasks is an Enterprise feature; the EE re-adds it via
    # ee_hooks.get_extra_task_modules() in _build_include() (registered by the EE
    # worker entrypoint before this module is imported).
    f"{__package__}.api_tokens_housekeeping",  # marca PATs expirados
    f"{__package__}.entra_sync_tasks",       # sync de usuarios do Entra via Graph
    f"{__package__}.dedupe_health_tasks",    # saúde do Redis do dedupe (ADR-0015)
    # query_tasks (federated-query execution) is an Enterprise feature; the EE worker
    # re-adds it via ee_hooks.get_extra_task_modules() in _build_include(). The
    # Queue("collect.query") + task_routes stay here (the Community scheduler's
    # run_scheduled_query also uses that queue).
]

def _build_include() -> list[str]:
    """Core task modules + any EE-registered extras.

    Empty extras in Community → identical to ``_MODULES_WITH_TASKS``
    (behavior-preserving). The EE registers its task modules via
    ``ee_hooks.register_extra_task_modules()`` in its worker/beat bootstrap BEFORE
    importing this module, so its ``@shared_task`` names get included on the worker
    (the worker never calls ``activate()`` — see ee_hooks docstring).
    """
    return [*_MODULES_WITH_TASKS, *ee_hooks.get_extra_task_modules()]


celery_app = Celery(
    "centralops.collectors",
    broker=_broker_url(),
    backend=_result_backend(),
    include=_build_include(),
)

# CRITICAL — sem set_default(), ``current_app`` resolve via LocalStack
# (thread-local) que ``Celery.__init__`` populou via ``set_current()``.
# Em FastAPI, handlers SÍNCRONOS (``def`` puro) são dispatchados pelo
# event loop pro threadpool. Esses threads NÃO têm o app na própria
# stack — ``current_app`` cai pro ``default_app`` que, sem set_default,
# é None → Celery cria um lazy default com broker ``amqp://localhost``.
# ``.delay()`` em ``@shared_task`` resolvido nesse thread vai pro broker
# errado. Erro silencioso, fora do thread principal.
#
# ``set_default()`` registra ``default_app = celery_app`` GLOBALMENTE
# (não thread-local). Qualquer thread acessando ``current_app`` agora
# retorna o nosso app, independente de quem instanciou.
#
# Isso faz par com ``from .collectors.celery_app import celery_app`` em
# main.py — o import garante que o constructor RODOU; o set_default
# garante que o app vence em outros threads também.
celery_app.set_default()


def _redbeat_redis_url() -> str:
    """URL do Redis para o RedBeat scheduler.

    Prioridade: REDBEAT_REDIS_URL > CELERY_BROKER_URL > REDIS_URL.
    RedBeat requer acesso ao mesmo Redis que o broker para que os workers
    possam ler as entries do schedule sem conexão extra.
    """
    if settings.REDBEAT_REDIS_URL:
        return settings.REDBEAT_REDIS_URL
    return _broker_url()


def _redbeat_key_prefix() -> str:
    """Prefixo de namespace para entries RedBeat no Redis.

    Isola schedules por ambiente quando múltiplos ambientes compartilham
    o mesmo Redis (situação rara mas possível em staging).

    Prioridade: REDBEAT_KEY_PREFIX (override explícito) > redbeat::{APP_ENV}::
    Exemplos:
      - production  → "redbeat::production::"
      - staging     → "redbeat::staging::"
      - development → "redbeat::development::"
      - test        → "redbeat::test::"

    ATENÇÃO — migração: ao fazer deploy desta mudança em ambiente que
    já rodava com o prefixo antigo "redbeat::", as entries antigas
    ficam órfãs no Redis — faça o flush das entries legadas no deploy.
    """
    import os
    override = os.environ.get("REDBEAT_KEY_PREFIX")
    if override:
        return override
    return f"redbeat::{settings.APP_ENV}::"


def _redbeat_lock_key() -> str:
    """Chave do lock distribuído do RedBeat (HA do beat).

    Setada explicitamente (em vez de confiar no default da lib) para garantir
    que TODAS as réplicas de beat disputem EXATAMENTE a mesma chave — é o que
    faz a leader-election funcionar. Namespaceada por ambiente, igual às
    entries, para dois ambientes no mesmo Redis não compartilharem o lock.
    """
    return f"{_redbeat_key_prefix()}lock"


celery_app.conf.update(
    # ── RedBeat: scheduler Redis-backed ────────────────────
    # Substitui o PersistentScheduler de arquivo. Permite que entries sejam
    # criadas/removidas em runtime sem reiniciar o Beat.
    # Scheduler RedBeat endurecido: re-adquire o lock in-process em perdas
    # transitórias (evita crash-loop/janela morta) e emite heartbeat por tick p/
    # o healthcheck detectar beat travado. Ver beat_scheduler_resilient.py.
    # ``__package__`` resolve p/ ``app.collectors`` (container) ou
    # ``backend.app.collectors`` (repo root) — o mesmo idioma dos includes.
    beat_scheduler=f"{__package__}.beat_scheduler_resilient:ResilientRedBeatScheduler",
    redbeat_redis_url=_redbeat_redis_url(),
    redbeat_key_prefix=_redbeat_key_prefix(),
    # Lock explícito p/ HA (2+ beats hot-standby). Sem isto,
    # confiava-se nos defaults da lib — frágil sob múltiplas réplicas.
    redbeat_lock_key=_redbeat_lock_key(),
    redbeat_lock_timeout=settings.REDBEAT_LOCK_TIMEOUT,
    # INVARIANTE (RedBeat docs): max_loop_interval DEVE ser menor que o
    # lock_timeout — o Beat renova o lock a cada tick; se dormir mais que o
    # TTL, o próprio lock expira (e outra réplica o rouba) → LockNotOwnedError
    # + crash-loop, e entries de intervalo maior nunca ficam due. O default do
    # celery é 300s; fixamos 30s (lock 150s = 5×, convenção da lib).
    beat_max_loop_interval=30,
    # Beat lê novas entries a cada sync_every segundos (default 5s do
    # PersistentScheduler). Mantemos 5s para consistência com comportamento
    # anterior; não tem impacto em produção — apenas leituras leves do Redis.
    beat_sync_every=5,
    # Roteamento explícito — nunca usar default para tasks da coleta.
    task_default_queue="collect.bulk",
    task_default_exchange="collectors",
    task_default_routing_key="collect.bulk",
    task_queues=(
        Queue("collect.priority", routing_key="collect.priority"),
        Queue("collect.bulk", routing_key="collect.bulk"),
        Queue("collect.backfill", routing_key="collect.backfill"),
        # A fila dedicada dispatch.wazuh foi removida — o
        # wazuh-default agora entrega pela lane genérica dispatch.destination.
        Queue("dispatch.destination", routing_key="dispatch.destination"),
        # Bulkhead: shard queues dispatch.destination.0..N-1 (hash-routing).
        # Um worker pode dedicar-se a um shard (-Q dispatch.destination.3) para
        # isolamento OS-level; o pool default consome todos.
        *(Queue(_q, routing_key=_q) for _q in all_dispatch_dest_queues()),
        Queue("dispatch.dlq", routing_key="dispatch.dlq"),
        # Fila DEDICADA de query ao vivo (QueryService). Consumida
        # pelo serviço dedicado collector-worker-query (-Q collect.query) — isolada
        # da ingestão para uma query lenta não estrangular a coleta realtime.
        Queue("collect.query", routing_key="collect.query"),
        # Manutenção e compliance.
        Queue("maintenance", routing_key="maintenance"),
        Queue("maintenance.high", routing_key="maintenance.high"),
    ),
    task_routes={
        "collectors.collect_vendor_logs_priority": {"queue": "collect.priority"},
        "collectors.collect_vendor_logs_bulk": {"queue": "collect.bulk"},
        "collectors.collect_backfill_job": {"queue": "collect.backfill"},
        # Despacho genérico por destino (lane única).
        "collectors.dispatch_to_destination": {"queue": "dispatch.destination"},
        # Migração do scheduler legado (services/scheduler.py).
        "collectors.scheduler.dispatch_due_scheduled_queries": {"queue": "collect.bulk"},
        # A EXECUÇÃO da scheduled query vai p/ a fila dedicada de
        # query (consumida por collector-worker-query) — fim do noisy-neighbor com
        # a ingestão. O TICK (dispatch_due) e o prune seguem leves em collect.bulk.
        "collectors.scheduler.run_scheduled_query": {"queue": "collect.query"},
        "collectors.scheduler.prune_search_result_retention": {"queue": "collect.bulk"},
        # Job de query federada vai p/ a fila dedicada (nunca bulk).
        "collectors.query.run_job": {"queue": "collect.query"},
        # Poll async na mesma fila dedicada.
        "collectors.query.poll_job": {"queue": "collect.query"},
        # Bulk reprocess de quarantine — uma task por event_id.
        "collectors.reprocess_quarantine_event": {"queue": "maintenance"},
    },
    # Garantias de entrega.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Prefetch=1 evita que um worker açambarque N tasks sem tocá-las.
    worker_prefetch_multiplier=1,
    # Recicla processos filhos periodicamente — mitiga leaks em aiohttp.
    worker_max_tasks_per_child=500,
    broker_connection_retry_on_startup=True,
    # Resiliência a restart/failover do Redis EM RUNTIME (não só no boot): o
    # consumidor/publicador reconecta indefinidamente em vez de o processo morrer
    # quando o broker pisca. Sem isto, um restart do Redis derruba workers/beat.
    broker_connection_retry=True,
    broker_connection_max_retries=None,  # None = reconecta p/ sempre (nunca desiste)
    # Detecção de socket morto + re-tentativa no nível do cliente Redis do broker.
    redis_socket_keepalive=True,
    redis_retry_on_timeout=True,
    redis_socket_connect_timeout=10,
    # Redis broker visibility_timeout: make the
    # at-least-once invariant explicit. With acks_late, a task must finish (or be
    # killed by task_time_limit) BEFORE the broker re-delivers it, otherwise the
    # fan-out batch double-sends. Ordering invariant:
    #   DISPATCH_RESULT_TIMEOUT(600) < task_soft_time_limit(720)
    #     < task_time_limit(900) < visibility_timeout(3600)
    # The hard time-limit kills a stuck task ~45min before redelivery could fire.
    broker_transport_options={
        "visibility_timeout": 3600,
        # Keepalive + health-check da conexão do broker: valida o socket ocioso
        # entre despachos e derruba conexões mortas rápido após um restart do
        # Redis (kombu ignora chaves não-reconhecidas → seguro).
        "socket_keepalive": True,
        "health_check_interval": 30,
    },
    # Fire-and-forget: NÃO gravar um result-row no Redis por
    # task. O pipeline é assíncrono e ninguém lê o VALOR de retorno das tasks — os
    # call-sites só usam ``result.id`` (task id, sempre disponível mesmo sem
    # backend) para logar e para ``AsyncResult.revoke()`` (que opera via broker,
    # não via result backend). Gravar um result por task era escrita pura no
    # broker single-node a cada despacho (gargalo). O result backend segue
    # configurado (ver ``backend=_result_backend()``) para tasks que optem por
    # ``ignore_result=False`` e para inspeção operacional (flower/celery inspect).
    task_ignore_result=True,
    result_expires=3600,
    # Limites conservadores; tasks de coleta raramente excedem 5min.
    task_time_limit=15 * 60,
    task_soft_time_limit=12 * 60,
    timezone="UTC",
    enable_utc=True,
)

# ── Guard executável da invariante RedBeat ────────────────────────────
# O lock DEVE sobreviver ao maior sleep do Beat: se REDBEAT_LOCK_TIMEOUT <=
# beat_max_loop_interval, o lock expira DURANTE o sleep → LockNotOwnedError +
# crash-loop (a causa-raiz histórica do incidente). Hoje a invariante era só um
# comentário — nada impedia baixar o timeout via env (ex.: Helm chegou a usar
# 600 vs. 150 do core) e reintroduzir o bug em silêncio. Transformamos em
# contrato executável: o BEAT recusa bootar (fail-fast, alto), enquanto os demais
# processos (API/worker) só logam — um env ruim não deve derrubar a API junto.
_beat_loop_interval = int(celery_app.conf.beat_max_loop_interval or 0)
_lock_ttl = int(settings.REDBEAT_LOCK_TIMEOUT or 0)
if _lock_ttl <= _beat_loop_interval:
    _invariant_msg = (
        f"Invariante RedBeat violada: REDBEAT_LOCK_TIMEOUT ({_lock_ttl}s) deve ser "
        f"MAIOR que beat_max_loop_interval ({_beat_loop_interval}s) — recomendado "
        ">=5x. O lock expiraria durante o sleep do Beat → LockNotOwnedError/crash-loop."
    )
    if os.environ.get("SERVICE_ROLE") == "beat":
        raise RuntimeError(_invariant_msg)
    logger.error(_invariant_msg)
elif _lock_ttl < 3 * _beat_loop_interval:
    logger.warning(
        "REDBEAT_LOCK_TIMEOUT (%ds) < 3x beat_max_loop_interval (%ds): margem "
        "apertada p/ blips/GC/throttle; a convenção da lib é 5x.",
        _lock_ttl,
        _beat_loop_interval,
    )

# Beat schedule é montado dinamicamente a partir do banco — apenas o
# processo Beat precisa disso. Workers pulam para economizar startup.
if os.environ.get("SERVICE_ROLE") == "beat":
    try:
        from . import beat_schedule  # noqa: F401  (side-effect: popula conf.beat_schedule)
    except Exception as exc:  # pragma: no cover — Beat tolera ausência de DB
        logger.warning("beat_schedule import skipped: %s", exc)
    # Aquece o cache de edição/licença no boot do Beat. O Beat roda
    # em processo próprio (sem fork, sem worker_init) e nunca importa main.py —
    # sem isto, logs/decisões de edição no Beat veriam "community" mesmo sob
    # licença válida. Fail-closed: refresh() nunca levanta.
    try:
        from ..core import edition as _edition_boot

        _edition_boot.refresh()
    except Exception:  # pragma: no cover — licença jamais derruba o Beat
        logger.warning("falha ao aquecer cache de edição no boot do Beat", exc_info=True)


def get_worker_redis() -> "redis_async_module.Redis":
    """Cria cliente Redis para a task atual.

    NOTA: NÃO compartilhamos pool entre tasks Celery (mesmo no mesmo
    processo prefork). Razão: ``redis.asyncio.ConnectionPool`` cria
    coroutines/futures vinculadas ao event loop em que foi instanciado.
    Como Celery prefork chama ``asyncio.run()`` por task, cada task abre
    um loop novo — usar pool de outro loop dispara "Event loop is
    closed" / "Future attached to a different loop".

    O custo de TCP+AUTH por task é ~0.5ms — aceitável. Para reduzir,
    a solução real é mudar para Celery worker_pool=gevent ou solo,
    onde o loop persiste; ou implementar pool por-loop via
    contextvars + weakref. Fora do escopo deste hotfix.

    Revertido de uma tentativa anterior de pool compartilhado — causa raiz
    do "Event loop is closed" em produção.
    """
    import redis.asyncio as redis_async

    return redis_async.from_url(
        settings.REDIS_URL or "redis://localhost:6379/0",
        decode_responses=True,
    )


# Filtro de logs que nunca deixa access_token / client_secret / refresh_token
# aparecer nos logs do worker (defesa em profundidade).
class _SecretsFilter(logging.Filter):
    import re as _re

    _NEEDLES = ("access_token", "refresh_token", "client_secret", "Authorization")

    # Captura redis://[user]:[password]@host:port/db
    # Ex: "redis://:supersecret@host:6379/0" → "redis://[REDACTED]@host:6379/0"
    _REDIS_URL_RE = _re.compile(
        r"redis://[^:@\s]*:[^@\s]+@",
        _re.IGNORECASE,
    )

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        try:
            msg = record.getMessage()
        except Exception:
            return True

        # Primeiro: redatar URLs Redis com senha
        if "redis://" in msg and "@" in msg:
            redacted = self._REDIS_URL_RE.sub("redis://[REDACTED]@", msg)
            if redacted != msg:
                record.msg = redacted
                record.args = ()
                msg = redacted

        # Segundo: redatar tokens/secrets por needle substring
        for needle in self._NEEDLES:
            if needle in msg:
                record.msg = f"[secret redacted: contained '{needle}']"
                record.args = ()
                break
        return True


# Instala filtro na raiz apenas em processos Celery (não no FastAPI app).
if os.environ.get("SERVICE_ROLE") in {"worker", "beat", "dispatcher"}:
    logging.getLogger().addFilter(_SecretsFilter())


# Configura logging JSON nos workers Celery via signal worker_init.
# O signal é disparado uma vez por processo worker ao iniciar,
# garantindo que todos os logs do worker saiam em JSON estruturado.
try:
    from celery.signals import worker_init

    @worker_init.connect
    def _setup_json_logging(**_kwargs) -> None:  # type: ignore[no-redef]
        """Configura logging JSON no worker Celery."""
        from ..core.logging_config import configure_logging

        configure_logging()
        logging.getLogger(__name__).info(
            "Celery worker iniciado com logging JSON estruturado"
        )

except Exception:  # pragma: no cover — guard de import em runtime
    logger.warning("Não foi possível registrar signal worker_init para logging JSON")


# ── Aquece o cache de edição/licença no boot do worker ────────────
# O worker NUNCA importa main.py (onde a API faz edition.refresh()), então sem
# isto o cache fica frio até a 1ª task chamar feature_enabled(): os logs de boot
# diriam "community" mesmo sob licença Enterprise válida, e a 1ª task pagaria um
# spike de I/O do keyring. ``worker_init`` dispara 1× no processo-PAI (antes do
# fork prefork) → os filhos herdam o cache já populado via copy-on-write.
# Registrado APÓS o handler de logging para que o log de edição já saia em JSON.
try:
    from celery.signals import worker_init as _worker_init_for_edition

    @_worker_init_for_edition.connect
    def _warm_edition_cache(**_kwargs) -> None:  # type: ignore[no-redef]
        """Resolve a edição uma vez no boot do worker (fail-closed)."""
        try:
            from ..core import edition

            fs = edition.refresh()
            logging.getLogger(__name__).info(
                "Edition cache aquecido no boot do worker: edition=%s", fs.edition
            )
        except Exception:  # pragma: no cover — licença jamais derruba o worker
            logging.getLogger(__name__).warning(
                "Falha ao aquecer o cache de edição no boot do worker", exc_info=True
            )

except Exception:  # pragma: no cover — guard de import em runtime
    logger.warning("Não foi possível registrar worker_init para aquecer a edição")


# ── Runtime async persistente do dispatcher ─────────
# ``worker_process_init`` dispara em CADA filho prefork APÓS o fork() (ao
# contrário de ``worker_init``, que roda 1× no pai) — é o ponto correto
# para criar o loop+thread persistente por processo (ver dispatch_runtime).
# ``worker_process_shutdown`` dispara por filho no stop gracioso E no
# recycle ``worker_max_tasks_per_child`` — fecha o socket Wazuh limpo.
# Tudo gated por DISPATCH_PERSISTENT_LOOP (default OFF) + SERVICE_ROLE.
try:
    from celery.signals import worker_process_init, worker_process_shutdown

    @worker_process_init.connect
    def _warm_dispatch_runtime(**_kwargs) -> None:  # type: ignore[no-redef]
        # ── Fork-safety do pool de DB (CRÍTICO) ─────────────────────────────
        # O ``worker_init`` roda 1× no PAI e chama ``edition.refresh()``, que ABRE
        # uma conexão psycopg2 no pool ANTES do fork. Os filhos prefork HERDAM esse
        # MESMO socket TCP; dois filhos usando a mesma conexão corrompem o estado do
        # libpq → ``error with status PGRES_TUPLES_OK and no message from the libpq``
        # / ``ResourceClosedError`` no meio de um selectinload (a coleta falha em
        # loop). ``dispose(close=False)`` descarta o pool HERDADO SEM fechar os fds
        # do pai — cada filho passa a abrir conexões próprias. É o padrão oficial do
        # SQLAlchemy para ``os.fork()`` (pool_pre_ping NÃO cobre isto: ambos os
        # filhos passam no ping e ainda assim disputam o mesmo socket). Roda ANTES
        # de qualquer uso de DB do filho (a 1ª query só ocorre dentro de uma task).
        try:
            from ..db import database

            database.engine.dispose(close=False)
        except Exception:  # pragma: no cover — nunca derruba o boot do filho
            logger.warning("Falha ao descartar o pool de DB no fork do worker", exc_info=True)

        from .dispatch_runtime import _persistent_enabled, warmup_runtime

        # Monta o SDK OTel POR filho prefork (nunca no pai —
        # provider/exporter não sobrevivem ao fork). No-op gated por OTEL_ENABLED.
        try:
            from .tracing import init_tracing

            init_tracing()
        except Exception:  # pragma: no cover — tracing jamais derruba o worker
            logger.warning("Falha ao inicializar OTel tracing no worker")

        # Superfície B (ops): métricas via OTLP-PUSH. Cada filho prefork empurra
        # suas séries de entrega — não há scrape. No-op gated por OTEL_ENABLED.
        try:
            from .otel_metrics import init_metrics

            init_metrics()
        except Exception:  # pragma: no cover — métricas jamais derrubam o worker
            logger.warning("Falha ao inicializar OTel metrics no worker")

        # Superfície B (ops): logs OTel correlacionados por trace_id. Toggle
        # separado OTEL_LOGS_ENABLED (volume alto). No-op quando off.
        try:
            from .otel_logs import init_logs

            init_logs()
        except Exception:  # pragma: no cover — logs jamais derrubam o worker
            logger.warning("Falha ao inicializar OTel logs no worker")

        if _persistent_enabled() and os.environ.get("SERVICE_ROLE") in {
            "worker", "dispatcher",
        }:
            warmup_runtime()

    @worker_process_shutdown.connect
    def _close_dispatch_runtime(**_kwargs) -> None:  # type: ignore[no-redef]
        from .dispatch_runtime import shutdown_runtime

        shutdown_runtime()

except Exception:  # pragma: no cover — guard de import em runtime
    logger.warning("Não foi possível registrar signals do dispatch_runtime")
