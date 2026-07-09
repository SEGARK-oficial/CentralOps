#!/usr/bin/env python
"""Bench — prova de reuso de socket do dispatcher.

Evidência **determinística** (não depende de Celery/Redis/Wazuh reais) do
ganho do loop persistente: dirige o **mesmo** ``Rfc3164JsonClient`` de
produção (via ``wazuh_target.get_target``) contra um servidor TCP local
que CONTA conexões aceitas e bytes recebidos, comparando:

- **OLD** — ``asyncio.run(dispatch())`` por lote (loop novo/lote → o
  ``get_target`` detecta troca de loop e reconstrói o writer →
  reconecta). Esperado: ``connections == N``.
- **NEW** — ``run_coro_blocking(dispatch())`` no loop persistente
  (``DISPATCH_PERSISTENT_LOOP=1``) → ``get_target`` reusa → **1 socket**.
  Esperado: ``connections == 1``.

Asserts (gate de regressão, machine-checkable):
  old.connections == N · new.connections == 1 · bytes idênticos ·
  fd-leak guard (a NEW loop não vaza fds além do 1 socket).
Informativo (impresso, NUNCA falha CI — "benchmark informativo"): CPU
(getrusage) e wall-clock OLD×NEW.

Uso:
    cd backend && PYTHONPATH=. .venv/bin/python scripts/bench_dispatch_socket_reuse.py [-n 200] [--mode both|old|new]

py-spy (artefato best-effort; record-on-launch evita sudo na maioria dos
casos; nunca um gate):
    py-spy record -o dispatch_new.svg -- .venv/bin/python \
        scripts/bench_dispatch_socket_reuse.py --mode new -n 500 || true
"""

from __future__ import annotations

import argparse
import asyncio
import os
import resource
import sys
import threading
import time

os.environ.setdefault("APP_MASTER_KEY", "bench-master-key-centralops-0001")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

# Garante import root ``app.*`` quando rodado de backend/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.collectors import dispatch_runtime as dr  # noqa: E402
from app.collectors.config_loader import CollectorConfigSnapshot  # noqa: E402
from app.collectors.normalize.envelope import (  # noqa: E402
    EnvelopeContext,
    build_envelope,
)
from app.collectors.output import wazuh_target  # noqa: E402


def _fd_count() -> int:
    """Contagem portátil de file descriptors abertos do processo."""
    for path in ("/proc/self/fd", "/dev/fd"):
        if os.path.isdir(path):
            try:
                return len(os.listdir(path))
            except OSError:
                pass
    return -1  # indisponível


class CountingServer:
    """Servidor TCP que conta accepts e bytes — substitui o Wazuh.

    Roda no **próprio thread/loop** (``run_forever``): aceita conexões
    independentemente do que a thread principal faz (asyncio.run-por-lote
    no OLD, loop persistente no NEW), evitando starvation de accept."""

    def __init__(self) -> None:
        self.connections = 0
        self.bytes_received = 0
        self.port = 0
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="bench-server", daemon=True)
        self._ready = threading.Event()
        self._server: asyncio.AbstractServer | None = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                self.bytes_received += len(chunk)
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _serve(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        self._ready.set()

    @staticmethod
    def _exc_handler(loop, context) -> None:
        msg = str(context.get("exception") or context.get("message") or "")
        if "Event loop is closed" in msg:
            return  # ruído benigno de teardown
        loop.default_exception_handler(context)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.set_exception_handler(self._exc_handler)
        self._loop.run_until_complete(self._serve())
        self._loop.run_forever()

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(timeout=5.0)

    async def _shutdown(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        # Cancela E AGUARDA handlers pendentes (conexão persistente do NEW) para
        # não vazar "Task pending"/"loop closed" no teardown.
        pending = [t for t in asyncio.all_tasks(self._loop) if t is not asyncio.current_task()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def stop(self) -> None:
        try:
            asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop).result(timeout=5.0)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5.0)


def _snapshot(port: int) -> CollectorConfigSnapshot:
    return CollectorConfigSnapshot(
        wazuh_syslog_host="127.0.0.1",
        wazuh_syslog_port=port,
        wazuh_ca_bundle=None,
        wazuh_syslog_use_tls=False,
        wazuh_dispatch_mode="syslog",
        wazuh_syslog_format="rfc3164",
        collector_jsonl_dir="/tmp/cops-bench-jsonl",
    )


def _batch() -> list[dict]:
    ctx = EnvelopeContext(
        vendor="sophos",
        integration_id=1,
        customer_id=7,
        stream="alerts",
        event_type="sophos.alert",
        mapping_version_id="bench",
        collector_host="bench-host",
    )
    env = build_envelope(
        raw={"id": "bench-1", "severity": "High"},
        normalized={"class_uid": 2004, "severity_id": 4},
        ctx=ctx,
        vendor_msg_id="bench-1",
    )
    return [env]


async def _dispatch_once(
    snapshot: CollectorConfigSnapshot, batch: list[dict], *, close_after: bool = False
) -> None:
    target = await wazuh_target.get_target(snapshot)
    await target.send_batch(batch)
    if close_after:
        # OLD: fecha o writer no MESMO loop (vivo) antes do asyncio.run encerrar,
        # evitando que o transport seja coletado num loop já fechado (ruído
        # "Event loop is closed"). Mantém N conexões (cada lote reconecta).
        await wazuh_target.reset_target()


def _rusage_seconds() -> float:
    ru = resource.getrusage(resource.RUSAGE_SELF)
    return ru.ru_utime + ru.ru_stime


def run_old(n: int, port: int) -> tuple[float, float]:
    """N lotes via asyncio.run-por-lote (reconecta a cada lote)."""
    snapshot = _snapshot(port)
    batch = _batch()
    cpu0, wall0 = _rusage_seconds(), time.perf_counter()
    for _ in range(n):
        asyncio.run(_dispatch_once(snapshot, batch, close_after=True))
    return _rusage_seconds() - cpu0, time.perf_counter() - wall0


def run_new(n: int, port: int) -> tuple[float, float, int]:
    """N lotes via loop persistente (1 socket). Retorna (cpu, wall, fd_delta)."""
    os.environ["DISPATCH_PERSISTENT_LOOP"] = "1"
    snapshot = _snapshot(port)
    batch = _batch()
    dr.warmup_runtime()
    fd_before = _fd_count()
    cpu0, wall0 = _rusage_seconds(), time.perf_counter()
    for _ in range(n):
        dr.run_coro_blocking(_dispatch_once(snapshot, batch), timeout=30)
    cpu, wall = _rusage_seconds() - cpu0, time.perf_counter() - wall0
    fd_after = _fd_count()
    dr.shutdown_runtime(timeout=10)
    fd_delta = (fd_after - fd_before) if (fd_before >= 0 and fd_after >= 0) else 0
    return cpu, wall, fd_delta


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-n", type=int, default=200, help="número de lotes (default 200)")
    ap.add_argument("--mode", choices=["both", "old", "new"], default="both")
    args = ap.parse_args()
    mode, n = args.mode, args.n

    old_conn = old_bytes = 0
    cpu_old = wall_old = 0.0

    if mode in {"both", "old"}:
        srv = CountingServer()
        srv.start()
        asyncio.run(wazuh_target.reset_target())
        cpu_old, wall_old = run_old(n, srv.port)
        time.sleep(0.2)  # deixa o server drenar os closes
        srv.stop()
        old_conn, old_bytes = srv.connections, srv.bytes_received
        print(f"[OLD] connections={old_conn}  bytes={old_bytes}  "
              f"cpu={cpu_old:.3f}s  wall={wall_old:.3f}s")
        if mode == "both":
            assert old_conn == n, (
                f"OLD deveria reconectar por lote: connections={old_conn} != N={n}"
            )

    if mode in {"both", "new"}:
        srv = CountingServer()
        srv.start()
        asyncio.run(wazuh_target.reset_target())
        cpu_new, wall_new, fd_delta = run_new(n, srv.port)
        time.sleep(0.2)
        srv.stop()
        print(f"[NEW] connections={srv.connections}  bytes={srv.bytes_received}  "
              f"cpu={cpu_new:.3f}s  wall={wall_new:.3f}s  fd_delta={fd_delta}")
        if mode == "both":
            assert srv.connections == 1, (
                f"NEW deveria reusar 1 socket: connections={srv.connections} != 1"
            )
            assert old_bytes == srv.bytes_received, (
                f"wire não byte-idêntico: OLD={old_bytes} NEW={srv.bytes_received}"
            )
            # fd-leak guard: a NEW loop deve adicionar um nº CONSTANTE de fds
            # (1 socket Wazuh + selector/self-pipe do loop) — NÃO O(N). Um
            # vazamento por-lote daria delta≈N. Teto constante pequeno (4)
            # prova a ausência de leak por lote independentemente de N.
            assert fd_delta <= 4, f"NEW vazou fds: delta={fd_delta} cresce com N={n}?"
            speedup = (wall_old / wall_new) if wall_new else float("inf")
            cpu_drop = (1 - cpu_new / cpu_old) * 100 if cpu_old else 0.0
            print(f"[CMP] socket_reuse: {n}→1  wall_speedup={speedup:.2f}x  "
                  f"cpu_reduction={cpu_drop:.1f}%  fd_delta={fd_delta}")
            print("OK — loop persistente: 1 socket, wire byte-idêntico, sem vazamento de fd")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
