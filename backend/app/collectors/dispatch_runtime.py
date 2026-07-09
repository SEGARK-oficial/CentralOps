"""Runtime async **long-lived** do dispatcher.

## Problema

Hoje cada task de dispatch faz ``asyncio.run(dispatch_batch(batch))``: um
**event loop novo por task**, fechado ao terminar. O ``wazuh_target``
cacheia o ``StreamWriter`` (socket TCP/TLS ao Wazuh) atado ao loop em que
foi criado; como o loop morre a cada task, ``get_target`` detecta a troca
de loop e **descarta o writer** — ou seja, **reconecta a cada lote**. Em
volume isso esgota handshakes TLS e conexões no Wazuh.

## Solução

Um **único event loop persistente por processo**, rodando em uma thread
daemon dedicada (``loop.run_forever()``). As tasks (síncronas) submetem
corrotinas via ``run_coroutine_threadsafe`` e bloqueiam pelo resultado.
Com o loop estável, ``get_target`` cai no ramo de **reuso** → **um socket
por processo** (sem reconexão por lote). Beneficia o caminho Wazuh atual
— não é específico de multi-destino.

## Fork-safety (Celery prefork — landmine)

O dispatcher roda ``celery worker ... --concurrency=8`` com pool
**prefork**: o pai faz ``os.fork()`` de 8 filhos. ``fork()`` copia a
memória mas **só a thread que chamou** sobrevive. Um loop+thread criado
no PAI (ex.: via ``worker_init``) ficaria, em cada filho, com: (a) um
``Thread`` cujo OS-thread não existe → ``run_coroutine_threadsafe`` nunca
roda → ``.result()`` trava; (b) o fd do socket Wazuh **compartilhado**
entre os 8 filhos → writes intercalados corrompem o wire byte-idêntico.
Por isso o runtime é criado **pós-fork, por filho**, via guarda
``os.getpid()`` (lazy) — auto-cura no recycle ``worker_max_tasks_per_child``
e funciona sob pytest/eager onde nenhum signal Celery dispara.

## Kill-switch

Gated por ``DISPATCH_PERSISTENT_LOOP`` (default **OFF** — paridade com o
caminho legado dormente). OFF: ``run_coro_blocking`` degrada para ``asyncio.run``
— **byte-a-byte o caminho legado**. Liga-se com ``=1`` após soak, e
desliga com restart do worker (rollback sem redeploy).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Awaitable, Optional, TypeVar

from celery.exceptions import SoftTimeLimitExceeded

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# ── Estado por processo (espelha o padrão de wazuh_target: globais que os
# testes resetam direto via shutdown_runtime/reset). ─────────────────────
_loop: Optional[asyncio.AbstractEventLoop] = None
_thread: Optional[threading.Thread] = None
_owner_pid: Optional[int] = None
_lock = threading.Lock()  # guarda apenas a (re)criação do runtime

# Teto do bloqueio cross-thread por lote. 600s < soft (720s) < hard (900s)
# do Celery (celery_app.conf), de modo que NOSSO timeout dispara primeiro e
# o cancelamento acontece DENTRO do loop (efetivo em pontos de await).
DISPATCH_RESULT_TIMEOUT: float = float(os.getenv("DISPATCH_RESULT_TIMEOUT", "600"))
# Folga do backstop do .result() para o wait_for do loop vencer a corrida.
_RESULT_GRACE_SECONDS: float = 30.0


def _persistent_enabled() -> bool:
    """True sse ``DISPATCH_PERSISTENT_LOOP`` for truthy. Lido por chamada
    (barato) — default OFF. Operador liga com ``=1`` + restart do worker."""
    return os.getenv("DISPATCH_PERSISTENT_LOOP", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _run_loop(loop: asyncio.AbstractEventLoop, ready: threading.Event) -> None:
    """Corpo da thread do runtime: fixa o loop na thread, sinaliza pronto e
    roda para sempre. ``loop.close()`` é responsabilidade de
    ``shutdown_runtime`` (após o join), não daqui."""
    asyncio.set_event_loop(loop)
    loop.call_soon(ready.set)
    loop.run_forever()


def _ensure_runtime() -> asyncio.AbstractEventLoop:
    """Retorna o loop persistente DESTE processo, criando-o se ausente ou
    se herdado-stale após ``fork()`` (pid gravado != ``os.getpid()``).

    No mismatch de pid o loop antigo é **descartado SEM** ``.close()`` —
    sua thread nunca rodou aqui e seus fds são compartilhados com o pai;
    tocá-los corromperia os sockets Wazuh/redis do pai. Double-check sob
    ``_lock``; bloqueia até o loop estar rodando para a 1ª submissão não
    correr com o startup da thread."""
    global _loop, _thread, _owner_pid

    pid = os.getpid()
    if (
        _loop is not None
        and _owner_pid == pid
        and _thread is not None
        and _thread.is_alive()
    ):
        return _loop

    with _lock:
        if (
            _loop is not None
            and _owner_pid == pid
            and _thread is not None
            and _thread.is_alive()
        ):
            return _loop

        if _loop is not None and _owner_pid != pid:
            # Herdado via fork — descarta o objeto sem tocar nos fds do pai.
            logger.info(
                "dispatch_runtime: loop herdado de pid=%s descartado em pid=%s",
                _owner_pid, pid,
            )

        ready = threading.Event()
        loop = asyncio.new_event_loop()
        thread = threading.Thread(
            target=_run_loop,
            args=(loop, ready),
            name=f"dispatch-loop-{pid}",
            daemon=True,
        )
        thread.start()
        if not ready.wait(timeout=5.0):  # pragma: no cover — startup patológico
            # Falha rápido em vez de cachear um loop morto (que faria o 1º
            # dispatch bloquear ~timeout+grace e só então levantar). NÃO
            # cacheia → a próxima chamada tenta um runtime fresco.
            logger.error("dispatch_runtime: loop não iniciou em 5s — abortando")
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass
            raise RuntimeError("dispatch_runtime: event loop não iniciou em 5s")
        _loop, _thread, _owner_pid = loop, thread, pid
        return loop


async def _safe_reset_target() -> None:
    """Fecha os targets de destino cacheados, no loop. Best-effort.

    A lane dedicada Wazuh (``wazuh_target``) foi removida. O
    ``run_coro_blocking`` agora serve só a lane GENÉRICA (``dispatch_to_destination``
    → ``destination_cache``), então a recuperação de poison-writer descarta os
    targets cacheados genéricos (``reset_destinations``) para o próximo lote
    reconstruir sockets limpos."""
    try:
        from .output.destination_cache import reset_destinations

        await reset_destinations()
    except Exception:  # pragma: no cover — defensivo
        logger.exception("dispatch_runtime: reset_destinations falhou")


def _schedule_reset_target(loop: asyncio.AbstractEventLoop) -> None:
    """Agenda (fire-and-forget) o descarte dos writers de destino cacheados no
    loop, para o PRÓXIMO lote reconstruir sockets limpos em vez de herdar um
    writer meio-drenado (recuperação de poison-writer)."""
    try:
        asyncio.run_coroutine_threadsafe(_safe_reset_target(), loop)
    except Exception:  # pragma: no cover — defensivo
        logger.exception("dispatch_runtime: falha ao agendar reset")


def run_coro_blocking(
    coro: Awaitable[_T], *, timeout: Optional[float] = None
) -> _T:
    """Submete ``coro`` ao loop persistente e bloqueia a thread da task até
    completar; **re-levanta a exceção da corrotina com o tipo original** —
    ``_RETRYABLE``/autoretry/DLQ disparam idênticos ao caminho legado.

    Com ``DISPATCH_PERSISTENT_LOOP`` OFF, degrada para ``asyncio.run(coro)``
    — bit-a-bit o caminho atual.

    O timeout é imposto **dentro do loop** via ``asyncio.wait_for``: a corro é
    cancelada E aguardada no loop, então ``fut.result()`` só retorna após o
    orfão desenrolar. (``fut.cancel()`` cross-thread também propaga
    ``CancelledError`` no próximo ponto de await — efetivo no caminho
    I/O-bound; ambos só agem ao chegar num await.) Em timeout/soft-limit dropa
    o writer Wazuh possivelmente envenenado (``_schedule_reset_target``) e
    re-levanta ``TimeoutError`` (∈ ``_RETRYABLE`` no Python 3.11+, subclasse
    de ``OSError``)."""
    if not _persistent_enabled():
        return asyncio.run(coro)  # type: ignore[arg-type]

    loop = _ensure_runtime()
    # Guarda de reentrância: chamar daqui de DENTRO da thread do loop pararia a
    # própria thread em fut.result() → self-deadlock. Nenhum caller atual
    # reentra (só os corpos síncronos das tasks Celery), mas falha rápido e
    # claro caso algum dia reentre.
    if _thread is not None and threading.current_thread() is _thread:
        raise RuntimeError("run_coro_blocking reentrado da thread do loop de dispatch")
    wrapped: Awaitable[_T] = (
        asyncio.wait_for(coro, timeout) if timeout is not None else coro
    )
    fut = asyncio.run_coroutine_threadsafe(wrapped, loop)

    result_timeout = (timeout + _RESULT_GRACE_SECONDS) if timeout is not None else None
    try:
        return fut.result(result_timeout)
    except SoftTimeLimitExceeded:
        # Levantada na thread da task enquanto parada em .result(). fut.cancel()
        # propaga CancelledError à corro orfã no próximo ponto de await. Espera
        # o orfão DESENROLAR antes de agendar o reset, para o reset não correr
        # com um send_batch em voo (fecha a janela de corrupção do writer p/
        # destinos futuros cujo close/flush awaita). Não muda o desfecho (cai no
        # except Exception → DLQ da task), é higiene.
        fut.cancel()
        try:
            fut.exception(timeout=5.0)
        except Exception:
            pass  # CancelledError/timeout ao esperar o orfão — segue p/ reset
        _schedule_reset_target(loop)
        raise
    except (asyncio.TimeoutError, TimeoutError) as exc:
        # wait_for (loop-side) já cancelou a corro; dropa o writer.
        fut.cancel()
        _schedule_reset_target(loop)
        raise TimeoutError(f"dispatch excedeu {timeout}s") from exc
    # Demais exceções (ConnectionError/OSError/ValueError/...) propagam
    # do fut.result() com tipo+traceback originais — sem interceptação.


def shutdown_runtime(timeout: float = 10.0) -> None:
    """Teardown gracioso por filho (e seam de teste). Ordem obrigatória:
    ``reset_target`` NO loop (close_notify TLS + FIN limpos) →
    ``call_soon_threadsafe(loop.stop)`` → ``thread.join(timeout)`` →
    ``loop.close()`` → zera globais. Idempotente; no-op se o loop estiver
    ausente ou herdado via fork. Se um drain travar contra um Wazuh morto,
    a thread daemon é abandonada e o OS recupera o fd na saída do processo."""
    global _loop, _thread, _owner_pid

    with _lock:
        loop, thread, pid = _loop, _thread, _owner_pid
        if loop is None or thread is None:
            _loop = _thread = _owner_pid = None
            return
        if pid != os.getpid():
            # Loop herdado: não opera nos fds do pai — só esquece.
            _loop = _thread = _owner_pid = None
            return

        try:
            fut = asyncio.run_coroutine_threadsafe(_safe_reset_target(), loop)
            fut.result(timeout)
        except Exception:  # pragma: no cover — best-effort no shutdown
            logger.exception("dispatch_runtime: erro ao fechar target no shutdown")

        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout)
        # Drena tarefas residuais (ex.: orfão cancelado) antes de fechar — evita
        # "Task was destroyed but it is pending!" no stop/recycle do filho.
        if not thread.is_alive():
            try:
                pending = asyncio.all_tasks(loop)
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            except Exception:  # pragma: no cover — best-effort drain
                pass
        try:
            loop.close()
        except Exception:  # pragma: no cover
            logger.exception("dispatch_runtime: erro ao fechar loop")
        _loop = _thread = _owner_pid = None


def warmup_runtime() -> None:
    """Init eager best-effort para ``worker_process_init`` — evita pagar o
    spin-up (loop+thread+socket) inline no 1º dispatch. Puro
    ``_ensure_runtime``; seguro pular (o caminho lazy garante a correção)."""
    if not _persistent_enabled():
        return
    try:
        _ensure_runtime()
    except Exception:  # pragma: no cover — warmup nunca pode derrubar o worker
        logger.exception("dispatch_runtime: warmup falhou (lazy cobre)")


# Quando ``dispatch_to_destination`` ativar, o drain de
# ``shutdown_runtime`` deve também chamar
# ``destination_cache.reset_destinations()`` para fechar o socket do
# destino. Dormente enquanto o caminho multi-destino é gated.
