"""Runtime async long-lived do dispatcher.

Prova que o loop persistente: (a) é reusado entre submissões → 1 socket
por destino por processo; (b) propaga exceções com tipo original (DLQ/retry
idênticos); (c) trata timeout como retryable e dropa o(s) writer(s)
envenenado(s); (d) é fork-safe via guarda os.getpid(); (e) faz teardown
limpo; (f) degrada para asyncio.run com a flag OFF (rollback bit-a-bit).

A lane dedicada Wazuh (``wazuh_target``) foi removida. A
recuperação de poison-writer e o teardown agora descartam os targets
GENÉRICOS cacheados via ``output.destination_cache.reset_destinations``
(importado lazy por ``dispatch_runtime._safe_reset_target``).
"""

from __future__ import annotations

import asyncio
import os

import pytest

from backend.app.collectors import dispatch_runtime as dr
from backend.app.collectors.output import destination_cache
from backend.app.collectors.output.destinations.registry import DestinationConfig
from backend.app.collectors.tasks import _RETRYABLE


@pytest.fixture(autouse=True)
def _reset_runtime():
    """Zera o runtime e o cache de destinos antes/depois de cada teste — o
    estado é global de módulo (igual a test_destination_cache)."""
    dr._loop = None
    dr._thread = None
    dr._owner_pid = None
    destination_cache._cache.clear()
    destination_cache._lock = None
    destination_cache._lock_loop = None
    yield
    try:
        dr.shutdown_runtime(timeout=5.0)
    except Exception:
        pass
    dr._loop = None
    dr._thread = None
    dr._owner_pid = None
    destination_cache._cache.clear()
    destination_cache._lock = None
    destination_cache._lock_loop = None


def _on(monkeypatch):
    monkeypatch.setenv("DISPATCH_PERSISTENT_LOOP", "1")


def _dest_config() -> DestinationConfig:
    return DestinationConfig(
        destination_id="wazuh-default",
        kind="syslog_rfc3164",
        config={"host": "wazuh.test.local", "port": 514},
        config_version="v1",
    )


async def _loop_id() -> int:
    return id(asyncio.get_running_loop())


# ── 1. mesmo loop reusado / flag OFF não cria runtime ──────────────────


def test_same_loop_reused_across_submissions(monkeypatch):
    _on(monkeypatch)
    a = dr.run_coro_blocking(_loop_id(), timeout=5)
    b = dr.run_coro_blocking(_loop_id(), timeout=5)
    assert a == b, "loop persistente deve ser reusado entre submissões"
    assert dr._thread is not None and dr._thread.is_alive()


def test_flag_off_creates_no_persistent_runtime(monkeypatch):
    monkeypatch.delenv("DISPATCH_PERSISTENT_LOOP", raising=False)
    out = dr.run_coro_blocking(_loop_id())  # roda via asyncio.run
    assert isinstance(out, int)
    assert dr._loop is None and dr._thread is None, (
        "flag OFF não pode criar thread/loop persistente (rollback bit-a-bit)"
    )


# ── 2. reuso do target de destino → 1 socket por processo ──────────────


def test_destination_target_reuse_under_persistent_loop(monkeypatch):
    """Mesma config + mesmo loop persistente → mesmo target (1 socket por
    destino). Substitui o antigo teste de reuso da lane Wazuh dedicada;
    agora exercita a lane GENÉRICA (``destination_cache.get_destination``)."""
    _on(monkeypatch)
    builds = {"n": 0}

    class _FakeTarget:
        async def send_batch(self, batch):  # pragma: no cover
            return None

        async def close(self):
            return None

    def _fake_build(config, secrets=None):
        builds["n"] += 1
        return _FakeTarget()

    monkeypatch.setattr(destination_cache.registry, "build", _fake_build)
    cfg = _dest_config()

    async def _grab():
        return await destination_cache.get_destination(cfg)

    t1 = dr.run_coro_blocking(_grab(), timeout=5)
    t2 = dr.run_coro_blocking(_grab(), timeout=5)
    assert t1 is t2, "mesma config + mesmo loop persistente → mesmo target (1 socket)"
    assert builds["n"] == 1, "writer deve ser construído UMA vez (sem reconexão por lote)"


# ── 3. propagação de exceção com tipo original ─────────────────────────


def test_exception_propagates_with_original_type(monkeypatch):
    _on(monkeypatch)

    async def _boom_conn():
        raise ConnectionError("boom")

    with pytest.raises(ConnectionError) as ei:
        dr.run_coro_blocking(_boom_conn(), timeout=5)
    assert isinstance(ei.value, _RETRYABLE), "ConnectionError ∈ _RETRYABLE → autoretry"

    async def _boom_value():
        raise ValueError("nope")

    with pytest.raises(ValueError) as ei2:
        dr.run_coro_blocking(_boom_value(), timeout=5)
    assert not isinstance(ei2.value, _RETRYABLE), "ValueError → except Exception → DLQ"


# ── 4. timeout é retryable sem tradução (fixa o aliasing do 3.11+) ─────


def test_timeout_is_retryable_no_translation(monkeypatch):
    _on(monkeypatch)

    async def _slow():
        await asyncio.sleep(5)

    with pytest.raises(TimeoutError) as ei:
        dr.run_coro_blocking(_slow(), timeout=0.1)
    # Python 3.11+: TimeoutError é subclasse de OSError e asyncio.TimeoutError
    # é o mesmo objeto → já está em _RETRYABLE sem tradução.
    assert issubclass(type(ei.value), OSError)
    assert isinstance(ei.value, _RETRYABLE)


# ── 5. timeout dropa o(s) writer(s) envenenado(s) ──────────────────────


def test_timeout_drops_poisoned_writer(monkeypatch):
    _on(monkeypatch)
    reset_called = asyncio.Event()

    async def _spy_reset():
        reset_called.set()

    # o reset agora descarta os targets GENÉRICOS cacheados.
    # _safe_reset_target importa reset_destinations lazy de destination_cache,
    # então patchamos no módulo de origem.
    monkeypatch.setattr(destination_cache, "reset_destinations", _spy_reset)

    async def _slow():
        await asyncio.sleep(5)

    with pytest.raises(TimeoutError):
        dr.run_coro_blocking(_slow(), timeout=0.1)

    # _schedule_reset_target é fire-and-forget no loop — espera o flag.
    async def _wait():
        await asyncio.wait_for(reset_called.wait(), timeout=3)
        return True

    assert dr.run_coro_blocking(_wait(), timeout=5) is True, (
        "após timeout, reset_destinations deve ser agendado no loop (drop do writer)"
    )


# ── 6. guarda de pid reconstrói (modela fork sem fork real) ────────────


def test_pid_guard_rebuilds_on_pid_change(monkeypatch):
    _on(monkeypatch)
    loop1 = dr._ensure_runtime()
    thread1 = dr._thread
    closed = {"n": 0}
    orig_close = loop1.close

    def _spy_close():
        closed["n"] += 1
        return orig_close()

    monkeypatch.setattr(loop1, "close", _spy_close)

    # Simula um filho pós-fork: mesmo objeto loop herdado, pid diferente.
    # Captura o pid real ANTES de patchar (senão a lambda recursa no patch).
    fake_pid = os.getpid() + 1
    monkeypatch.setattr(dr.os, "getpid", lambda: fake_pid)
    loop2 = dr._ensure_runtime()
    thread2 = dr._thread

    assert loop2 is not loop1, "pid diferente → loop novo (herdado-stale descartado)"
    assert thread2 is not thread1
    assert closed["n"] == 0, "loop herdado NÃO pode ser .close() (fds do pai)"

    # Cleanup explícito: reap AS DUAS runtimes (a herdada-stale órfã + a nova).
    # O teardown do fixture cairia no ramo no-op de pid-herdado e vazaria as
    # duas threads daemon para os testes seguintes.
    for lp, th in ((loop1, thread1), (loop2, thread2)):
        try:
            lp.call_soon_threadsafe(lp.stop)
            th.join(timeout=2.0)
            lp.close()
        except Exception:
            pass
    dr._loop = dr._thread = dr._owner_pid = None


# ── 7. único teste com fork real ───────────────────────────────────────


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork() indisponível")
def test_real_fork_child_builds_own_runtime(monkeypatch):
    _on(monkeypatch)
    # Pai cria o runtime primeiro — o filho herda os globais e deve reconstruir.
    dr._ensure_runtime()

    pid = os.fork()
    if pid == 0:  # filho
        try:
            out = dr.run_coro_blocking(_loop_id(), timeout=5)
            os._exit(0 if isinstance(out, int) else 2)
        except BaseException:
            os._exit(3)
    else:  # pai
        _, status = os.waitpid(pid, 0)
        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, (
            "filho pós-fork deve construir o próprio runtime e rodar a corro"
        )


# ── 8. shutdown fecha o(s) target(s) e faz join da thread ──────────────


def test_shutdown_closes_target_and_joins_thread(monkeypatch):
    _on(monkeypatch)
    closed = {"n": 0}

    async def _spy_reset():
        closed["n"] += 1

    # shutdown drena os targets GENÉRICOS via reset_destinations.
    monkeypatch.setattr(destination_cache, "reset_destinations", _spy_reset)

    dr.run_coro_blocking(_loop_id(), timeout=5)  # garante runtime vivo
    thread = dr._thread
    assert thread is not None and thread.is_alive()

    dr.shutdown_runtime(timeout=5)
    assert closed["n"] == 1, "shutdown deve fechar os targets de destino (reset_destinations)"
    assert not thread.is_alive(), "thread do loop deve ter feito join"
    assert dr._loop is None and dr._thread is None

    dr.shutdown_runtime(timeout=5)  # idempotente
    assert dr._loop is None


# ── 9. flag OFF não toca o runtime (rollback) ──────────────────────────


def test_rollback_flag_uses_asyncio_run(monkeypatch):
    monkeypatch.delenv("DISPATCH_PERSISTENT_LOOP", raising=False)
    calls = {"ensure": 0}
    orig = dr._ensure_runtime

    def _spy_ensure():
        calls["ensure"] += 1
        return orig()

    monkeypatch.setattr(dr, "_ensure_runtime", _spy_ensure)

    out = dr.run_coro_blocking(_loop_id())
    assert isinstance(out, int), "corro ainda roda (via asyncio.run)"
    assert calls["ensure"] == 0, "flag OFF não pode tocar o runtime persistente"
    assert dr._loop is None
