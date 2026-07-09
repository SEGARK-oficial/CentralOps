"""Entrypoint do role ``dispatcher`` (consumer do data-plane).

Processo standalone (NÃO é worker Celery): consome o tópico Kafka ``deliver`` e
chama o despacho existente. Lançado pelo ``start-collector.sh`` (que garante a
APP_MASTER_KEY + migra o schema) com::

    command: ["python", "-c", "from app.collectors.dataplane.dispatcher import main; main()"]

NÃO use ``python -m``: na imagem Cython-compilada este módulo é um ``.so`` e o
``-m`` falha com "No code object available". Importar o ``.so`` e chamar ``main()``
funciona compilado E em ``.py`` (dev).

Drain gracioso COOPERATIVO: SIGTERM/SIGINT setam um ``stop_event``; o
``run_dispatch_consumer`` termina+COMMITA o registro em voo e SÓ ENTÃO sai do
loop (não cancela no meio de um dispatch, o que abandonaria o lote sem commit).
Hard-cancel é só último recurso, se o drain exceder ``_DRAIN_DEADLINE_S`` (que
fica abaixo do ``terminationGracePeriodSeconds``). Nota: ``consumer.stop()`` faz
leave-group limpo mas NÃO commita (enable_auto_commit=False) — a durabilidade vem
do commit-após-dispatch + replay dos offsets não-commitados (dedupe no destino
absorve a reentrega).
"""

from __future__ import annotations

import asyncio
import logging
import signal
import threading

from .kafka_transport import run_dispatch_consumer

logger = logging.getLogger(__name__)

# Deadline do drain cooperativo: tempo p/ o registro em voo terminar+commitar
# antes do hard-cancel de último recurso. DEVE ser < terminationGracePeriodSeconds
# (930s no chart/compose) p/ o orquestrador não matar à força antes da hora.
_DRAIN_DEADLINE_S = 900.0


async def _amain() -> None:
    # Cooperativo: passa o stop_event; o consumer sai APÓS terminar+
    # commitar o registro corrente. O poll-com-timeout dentro do loop garante que
    # o stop seja observado em ~1s mesmo com a partição ociosa.
    stop_flag = threading.Event()
    consumer_task = asyncio.create_task(run_dispatch_consumer(stop_event=stop_flag))
    sig_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, sig_event.set)
        except NotImplementedError:  # pragma: no cover — plataformas sem add_signal_handler
            signal.signal(sig, lambda *_: sig_event.set())

    # Encerra quando (a) chega um sinal, ou (b) o consumer morre sozinho.
    done, _pending = await asyncio.wait(
        {consumer_task, asyncio.create_task(sig_event.wait())},
        return_when=asyncio.FIRST_COMPLETED,
    )

    if not consumer_task.done():
        logger.info("dispatcher: sinal recebido — drain cooperativo (termina registro em voo)")
        stop_flag.set()  # cooperativo: deixa o registro corrente terminar+commitar
        try:
            await asyncio.wait_for(consumer_task, timeout=_DRAIN_DEADLINE_S)
        except asyncio.TimeoutError:  # pragma: no cover — sink travado além do deadline
            logger.warning(
                "dispatcher: drain excedeu %.0fs — cancelando (fallback)", _DRAIN_DEADLINE_S
            )
            consumer_task.cancel()
            try:
                await consumer_task
            except asyncio.CancelledError:
                pass
    else:
        # Consumer terminou sozinho — propaga exceção se houve (restart pelo orquestrador).
        exc = consumer_task.exception()
        if exc is not None:
            raise exc


def _init_observability() -> None:
    """Inicializa OTel NESTE processo.

    O dispatcher é um processo standalone (``python -c ... main()``), NÃO um worker Celery —
    então o ``worker_process_init`` (que monta o SDK OTel nos workers) NUNCA dispara
    aqui. Sem esta init, as métricas/traces de entrega emitidos pelo
    ``dispatch_batch_to_destination`` reusado (latência/DLQ/retries) + as métricas
    do data-plane (produce/consume/lag) NÃO seriam exportados via OTLP. Espelha os
    3 sinais dos workers (tracing/metrics/logs); cada um é no-op se OTEL_ENABLED off.
    """
    for label, mod, fn in (
        ("tracing", "tracing", "init_tracing"),
        ("metrics", "otel_metrics", "init_metrics"),
        ("logs", "otel_logs", "init_logs"),
    ):
        try:
            import importlib

            getattr(importlib.import_module(f"..{mod}", __package__), fn)()
        except Exception:  # pragma: no cover — telemetria jamais derruba o dispatcher
            logger.warning("dispatcher: init OTel %s falhou (segue sem)", label, exc_info=True)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    _init_observability()
    logger.info("dispatcher: iniciando consumer do data-plane Kafka")
    asyncio.run(_amain())


if __name__ == "__main__":  # pragma: no cover — entrypoint de processo
    main()
