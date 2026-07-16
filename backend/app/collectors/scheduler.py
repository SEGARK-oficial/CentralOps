"""Utilitários de registro/remoção de integrações no RedBeat scheduler.

API pública
-----------
register_integration_in_beat(integration_id)
    Cria ou atualiza RedBeatSchedulerEntry para cada stream registrado
    para a platform da integração. Idempotente — pode ser chamada
    múltiplas vezes com o mesmo integration_id sem efeitos colaterais.

deregister_integration_from_beat(integration_id)
    Remove todas as RedBeatSchedulerEntry do integration_id.
    Idempotente — não falha se a entry já não existe.

sync_all_active_integrations()
    Boot-time: lê todas as integrações ativas do banco e registra no
    RedBeat. Substitui o _dynamic_entries() estático do beat_schedule.py.

Ambas as funções são **fire-and-forget com fallback silencioso**: se o
Redis estiver indisponível, logam error mas não levantam exceção. A
integração continua existindo no banco; ela entrará no schedule quando
o Beat sincronizar (sync_every) ou quando for chamada novamente.
"""

from __future__ import annotations

import logging

from ..db import database, models
from .registry import all_registrations, iter_for_platform

logger = logging.getLogger(__name__)


def _make_entry_key(integration_id: int, beat_key: str) -> str:
    """Chave estável para identificar a entry no RedBeat.

    Formato: ``<beat_key>-<integration_id>``
    Exemplo: ``sophos-alerts-42``

    Deve ser estável — mudá-la equivale a criar entry órfã + nova entry.
    """
    return f"{beat_key}-{integration_id}"


def _existing_entry_matches(redis_key, reg, integration_id, expires, celery_app) -> bool:
    """True se a entry JÁ existe no RedBeat com exatamente a definição desejada.

    Usada para tornar o registro de fato idempotente: ``entry.save()`` numa entry
    existente recalcula o score do zset — e, para entries que nunca rodaram (sem
    meta), reagenda para ``now + intervalo``. Como o boot-sync roda a cada
    (re)start do Beat, re-salvar sempre vira INANIÇÃO sob restart: qualquer
    stream de intervalo maior que o uptime do Beat é empurrada eternamente
    (incidente jul/2026: sophos cases/detections nunca disparavam enquanto
    alerts, de 1 min, sobrevivia). Só re-salvamos quando não existe ou mudou.

    Fail-safe: QUALQUER dúvida (ausente, corrompida, schedule de outro tipo,
    erro de leitura) retorna False → o caller re-salva (comportamento antigo).
    """
    from redbeat import RedBeatSchedulerEntry

    try:
        existing = RedBeatSchedulerEntry.from_key(redis_key, app=celery_app)
    except KeyError:
        return False
    except Exception:
        logger.debug(
            "scheduler: falha ao ler entry key=%s — re-salvando", redis_key, exc_info=True
        )
        return False
    try:
        options = existing.options or {}
        return (
            existing.task == reg.task_name
            # timedelta é serializado como celery schedule(run_every=...); um
            # schedule de outro tipo (ex.: crontab) não tem run_every → False.
            and getattr(existing.schedule, "run_every", None) == reg.schedule
            and list(existing.args or ()) == [integration_id, reg.stream]
            and options.get("queue") == reg.queue
            and options.get("expires") == expires
        )
    except Exception:
        return False


def register_integration_in_beat(integration_id: int) -> None:
    """Registra ou atualiza entries RedBeat para a integração.

    Idempotente: ``RedBeatSchedulerEntry(...).save()`` faz upsert no Redis.
    Silencioso: se Redis ou DB indisponíveis, loga error e retorna sem raise.

    Chamada por:
    - POST /integrations (hook on-create, com countdown=5s).
    - Boot do Beat (via sync_all_active_integrations).
    - Eventual reativação de integração (PATCH futuro).
    """
    try:
        _register_integration_in_beat_unsafe(integration_id)
    except Exception:
        logger.error(
            "scheduler: falha ao registrar integration_id=%s no RedBeat "
            "(integração criada, coleta pendente de reconciliação)",
            integration_id,
            exc_info=True,
        )


def _register_integration_in_beat_unsafe(integration_id: int) -> None:
    """Implementação interna — pode levantar. Não chame direto em produção."""
    from redbeat import RedBeatSchedulerEntry

    from .celery_app import celery_app  # lazy: evita circular import beat_schedule→scheduler→celery_app

    with database.SessionLocal() as db:
        integration = (
            db.query(models.Integration)
            .filter(
                models.Integration.id == integration_id,
                models.Integration.is_active.is_(True),
            )
            .first()
        )

    if integration is None:
        logger.warning(
            "scheduler: integration_id=%s não encontrada ou inativa — "
            "nenhuma entry registrada",
            integration_id,
        )
        return

    # Partners e Organizations são "guarda-chuvas": não têm alerts/cases/
    # detections próprios — só seus children (kind='tenant') têm. Tentar
    # coletar deles dispara MissingApiHostError em loop.
    if integration.kind in ("partner", "organization"):
        logger.info(
            "scheduler: skipping kind=%s integration_id=%s "
            "(collection runs on children, not parent)",
            integration.kind,
            integration.id,
        )
        return

    platform = integration.platform
    registered_count = 0

    key_prefix = celery_app.conf.redbeat_key_prefix

    for reg in iter_for_platform(platform):
        key = _make_entry_key(integration_id, reg.beat_key)
        expires = max(30, int(reg.schedule.total_seconds()) - 5)

        # Idempotência real: não re-salvar entry idêntica (preserva agenda/meta
        # — ver docstring de _existing_entry_matches).
        if _existing_entry_matches(
            f"{key_prefix}{key}", reg, integration_id, expires, celery_app
        ):
            registered_count += 1
            logger.debug(
                "scheduler: entry key=%s já registrada e idêntica — agenda preservada",
                key,
            )
            continue

        entry = RedBeatSchedulerEntry(
            name=key,
            task=reg.task_name,
            schedule=reg.schedule,
            args=(integration_id, reg.stream),
            kwargs={},
            options={"queue": reg.queue, "expires": expires},
            app=celery_app,
        )
        entry.save()
        registered_count += 1
        logger.debug(
            "scheduler: entry registrada key=%s task=%s schedule=%s",
            key,
            reg.task_name,
            reg.schedule,
        )

    if registered_count == 0:
        logger.warning(
            "scheduler: nenhum stream registrado para platform=%r "
            "(integration_id=%s) — verifique o registry",
            platform,
            integration_id,
        )
    else:
        logger.info(
            "scheduler: %d entries registradas para integration_id=%s platform=%r",
            registered_count,
            integration_id,
            platform,
        )


def deregister_integration_from_beat(integration_id: int) -> None:
    """Remove todas as entries RedBeat da integração.

    Idempotente: se a entry não existe, o delete é no-op.
    Silencioso: se Redis indisponível, loga error e retorna sem raise.

    Chamada por:
    - DELETE /integrations (soft-delete ou hard-delete).
    """
    try:
        _deregister_integration_from_beat_unsafe(integration_id)
    except Exception:
        logger.error(
            "scheduler: falha ao remover integration_id=%s do RedBeat "
            "(integração removida do banco, entry pode persistir até expirar)",
            integration_id,
            exc_info=True,
        )


def _deregister_integration_from_beat_unsafe(integration_id: int) -> None:
    """Implementação interna — pode levantar. Não chame direto em produção."""
    from redbeat import RedBeatSchedulerEntry

    from .celery_app import celery_app  # lazy: evita circular import

    # Precisamos da platform para saber quais beat_keys existem.
    # Lemos o banco sem filtro is_active para cobrir integrações já desativadas.
    with database.SessionLocal() as db:
        integration = (
            db.query(models.Integration)
            .filter(models.Integration.id == integration_id)
            .first()
        )

    if integration is None:
        logger.warning(
            "scheduler: integration_id=%s não encontrada no banco — "
            "impossível determinar platform para deregistrar",
            integration_id,
        )
        return

    platform = integration.platform
    removed_count = 0
    # Chave Redis completa: prefixo configurado + nome da entry.
    # Construída diretamente para não depender de API interna do RedBeatScheduler
    # (que mudou entre versões do pacote celery-redbeat).
    key_prefix = celery_app.conf.redbeat_key_prefix

    for reg in iter_for_platform(platform):
        key = _make_entry_key(integration_id, reg.beat_key)
        redis_key = f"{key_prefix}{key}"
        try:
            entry = RedBeatSchedulerEntry.from_key(
                redis_key,
                app=celery_app,
            )
            entry.delete()
            removed_count += 1
            logger.debug("scheduler: entry removida key=%s", key)
        except KeyError:
            # Entry não existe — idempotente.
            logger.debug("scheduler: entry key=%s já não existia", key)
        except Exception:
            # Erros por entry não devem parar o loop — tentamos remover todas.
            logger.warning(
                "scheduler: erro ao remover entry key=%s — continuando",
                key,
                exc_info=True,
            )

    logger.info(
        "scheduler: %d entries removidas para integration_id=%s platform=%r",
        removed_count,
        integration_id,
        platform,
    )


def _orphan_entry_keys(
    existing_keys: list[str],
    beat_keys: set[str],
    key_prefix: str,
    active_ids: set[int],
) -> list[str]:
    """Lógica PURA (testável sem Redis): dado o conjunto de chaves RedBeat
    existentes, retorna as ÓRFÃS — entries no formato ``{prefix}{beat_key}-{id}``
    cujo ``id`` NÃO está em ``active_ids``.

    Segurança: só considera órfã uma chave cujo sufixo após ``{beat_key}-`` é um
    inteiro puro (``isdigit``). Entries estáticas (ex.: ``sophos-partner-sync``,
    tarefas de retenção) e qualquer chave que não case com um ``beat_key`` do
    registry são IGNORADAS — nunca deletadas. Beat_keys com prefixo comum (ex.:
    ``wazuh`` vs ``wazuh-detections``) são resolvidos pelo guarda ``isdigit``.
    """
    orphans: list[str] = []
    for name in existing_keys:
        for beat_key in beat_keys:
            prefix = f"{key_prefix}{beat_key}-"
            if not name.startswith(prefix):
                continue
            suffix = name[len(prefix):]
            if suffix.isdigit():
                if int(suffix) not in active_ids:
                    orphans.append(name)
                break  # casou com um beat_key — não testa os demais
    return orphans


def _sweep_orphan_redbeat_entries(active_ids: set[int]) -> int:
    """Remove entries RedBeat de integrações que NÃO estão mais ativas.

    Fecha o ÚNICO leak estrutural do RedBeat: a lib não expira entries (HSET/ZADD
    sem TTL) e ``deregister`` é best-effort fire-and-forget — sob churn de
    integrações (inativação/exclusão/troca de kind), entries órfãs acumulam sem
    nenhum mecanismo de poda. Este sweep roda no boot do Beat (reconciliação) e
    varre o Redis pelos ``beat_key`` conhecidos, deletando só as órfãs.
    """
    from redbeat import RedBeatSchedulerEntry
    from redbeat.schedulers import get_redis

    from .celery_app import celery_app  # lazy: evita circular import

    key_prefix = celery_app.conf.redbeat_key_prefix
    beat_keys = {reg.beat_key for reg in all_registrations()}
    if not beat_keys:
        return 0

    try:
        client = get_redis(celery_app)
        existing: list[str] = []
        for beat_key in beat_keys:
            for raw in client.scan_iter(match=f"{key_prefix}{beat_key}-*", count=200):
                existing.append(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
    except Exception:
        logger.warning(
            "scheduler: sweep de órfãs RedBeat — falha ao listar entries no Redis",
            exc_info=True,
        )
        return 0

    orphans = _orphan_entry_keys(existing, beat_keys, key_prefix, active_ids)
    removed = 0
    for name in orphans:
        try:
            RedBeatSchedulerEntry.from_key(name, app=celery_app).delete()
            removed += 1
            logger.debug("scheduler: entry órfã removida key=%s", name)
        except KeyError:
            pass  # já não existe — idempotente
        except Exception:
            logger.warning(
                "scheduler: erro ao remover entry órfã key=%s — continuando",
                name,
                exc_info=True,
            )

    if removed:
        logger.info("scheduler: sweep removeu %d entries RedBeat órfãs", removed)
    return removed


def sync_all_active_integrations() -> None:
    """Sincroniza todas as integrações ativas do banco → RedBeat.

    Chamada no boot do Beat (via beat_schedule.py). Substitui o
    _dynamic_entries() que retornava um dict estático: agora as entries
    vivem no Redis e sobrevivem a restarts sem re-leitura do banco.

    Também serve como reconciliação manual após incidentes.
    Idempotente — pode ser chamada múltiplas vezes.
    """
    try:
        _sync_all_active_integrations_unsafe()
    except Exception:
        logger.error(
            "scheduler: sync_all_active_integrations falhou — Beat pode "
            "iniciar sem entries dinâmicas; reconcilie manualmente",
            exc_info=True,
        )


def _sync_all_active_integrations_unsafe() -> None:
    with database.SessionLocal() as db:
        # Apenas kind='tenant' coleta. Partners e Organizations são
        # guarda-chuvas — seus children (também kind='tenant') é que
        # geram entries no scheduler.
        integrations = (
            db.query(models.Integration)
            .filter(
                models.Integration.kind == "tenant",
                models.Integration.is_active.is_(True),
            )
            .all()
        )
        # Carrega ids antes de fechar a sessão.
        integration_ids = [i.id for i in integrations]

    logger.info(
        "scheduler: sincronizando %d integrações ativas (kind=tenant) com RedBeat",
        len(integration_ids),
    )

    for integration_id in integration_ids:
        # Chama a função pública para herdar o tratamento de erro por integração.
        register_integration_in_beat(integration_id)

    # Reconciliação: remove entries de integrações que não estão mais ativas
    # (inativadas/deletadas/kind alterado). Fecha o leak de entries órfãs do
    # RedBeat. Falha aqui NÃO invalida o registro já feito acima.
    try:
        _sweep_orphan_redbeat_entries(set(integration_ids))
    except Exception:
        logger.error(
            "scheduler: sweep de órfãs RedBeat falhou — entries órfãs podem persistir",
            exc_info=True,
        )
