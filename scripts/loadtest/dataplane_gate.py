"""Gate de carga de ponta a ponta do data-plane (ADR-0018 Onda 3f). FORA DO CI.

Mede o caminho inteiro de um evento push — HTTP → tópico raw → ``processor`` →
tópico de entrega → ``dispatcher`` → destino — e confere, evento a evento, que
nada se perdeu nem duplicou. Sai com código 0 só quando todos os limiares batem.

Duas formas de rodar:

``local`` — sobe tudo nesta máquina, a partir do checkout: Postgres, Redis e
Redpanda em containers; migração, seed (org, integração FortiGate push, token e
um destino ``syslog_rfc5424`` default); API, ``dispatcher`` e N ``processor``
como processos. O destino é um receptor TCP do próprio gate, que confere cada
evento no fio — sem disco no meio, que mediria o sistema de arquivos do host.
Mede em duas fases, para separar os gargalos:

1. **Ingestão**, com os processors parados: o backlog inteiro vai para o raw.
2. **Processamento**, ligando os N processors e o dispatcher: tempo até o lag
   do grupo ``centralops-processor`` zerar (eps de processamento, total e por
   processo) e até o último evento chegar ao destino (eps de ponta a ponta).

``external`` — contra uma instalação já de pé (o cenário de referência de 3
nós): envia no ritmo pedido com os processors rodando, acompanha o lag pelo
broker e, se ``--output-dir`` apontar para o diretório do destino JSONL, confere
perda e duplicata. Sem ele, o gate fica só nos números de vazão.

COMO RODAR

    # 1 nó, 4 processors, 60 s de ingestão o mais rápido possível
    python scripts/loadtest/dataplane_gate.py local --processors 4 --duration 60

    # referência do ADR: 10 mil eps sustentados numa instalação de 3 nós
    python scripts/loadtest/dataplane_gate.py external \\
        --url https://centralops.exemplo/api/ingest/traffic --token coi_<id>_xxxx \\
        --bootstrap kafka-0:9092 --topic-prefix centralops \\
        --eps 10000 --duration 600 --min-e2e-eps 10000

Requer o venv do backend (aiohttp, aiokafka) e, no modo ``local``, Docker.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import pathlib
import shutil
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional

REPO = pathlib.Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
HERE = pathlib.Path(__file__).resolve().parent

PROCESSOR_GROUP = "centralops-processor"
DISPATCHER_GROUP = "centralops-dispatcher"

_SEED = r"""
import json, os, sys
from app.db import database, models
from app.services import ingest_tokens

ports = [int(p) for p in sys.argv[1:]]
out = []
with database.SessionLocal() as db:
    for i, port in enumerate(ports):
        org = models.Organization(name=f"Loadtest {i}", slug=f"loadtest-{i}")
        db.add(org); db.flush()
        integ = models.Integration(
            organization_id=org.id, name=f"FG loadtest {i}", platform="fortinet_fortigate",
            is_active=True,
        )
        db.add(integ); db.flush()
        token = ingest_tokens.issue(integ)
        # Destino da PRÓPRIA org (não global): cada org entrega ao seu receptor.
        db.add(models.Destination(
            name=f"loadtest syslog {i}", kind="syslog_rfc5424", organization_id=org.id,
            enabled=True, is_default=True,
            config=json.dumps({"host": "127.0.0.1", "port": port}),
            delivery=json.dumps({"ordered": True} if os.environ.get("GATE_ORDERED") else {}),
        ))
        out.append({"integration_id": integ.id, "token": token})
    db.commit()
print(json.dumps(out))
"""


def _load_push_module():
    spec = importlib.util.spec_from_file_location("push_ingest_load", HERE / "push_ingest_load.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _log(msg: str) -> None:
    print(f"[gate] {msg}", flush=True)


# ── Broker: lag por grupo ────────────────────────────────────────────────────


async def group_lag(bootstrap: str, group: str, topic: str) -> int:
    """Soma, por partição, do offset final menos o commitado pelo grupo."""
    from aiokafka import AIOKafkaConsumer, TopicPartition
    from aiokafka.admin import AIOKafkaAdminClient

    admin = AIOKafkaAdminClient(bootstrap_servers=bootstrap)
    consumer = AIOKafkaConsumer(bootstrap_servers=bootstrap, enable_auto_commit=False)
    await admin.start()
    await consumer.start()
    try:
        await consumer.topics()  # carrega os metadados
        parts = consumer.partitions_for_topic(topic) or set()
        tps = [TopicPartition(topic, p) for p in sorted(parts)]
        if not tps:
            return 0
        end = await consumer.end_offsets(tps)
        committed = await admin.list_consumer_group_offsets(group)
        lag = 0
        for tp in tps:
            meta = committed.get(tp)
            done = meta.offset if meta is not None and meta.offset >= 0 else 0
            lag += max(0, end[tp] - done)
        return lag
    finally:
        await consumer.stop()
        await admin.close()


async def wait_lag_zero(bootstrap: str, group: str, topic: str, timeout: float) -> Optional[float]:
    """Instante (monotônico) em que o lag zerou, ou None se estourou o prazo."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await group_lag(bootstrap, group, topic) == 0:
            return time.monotonic()
        await asyncio.sleep(1.0)
    return None


# ── Destino: receptor syslog do próprio gate ─────────────────────────────────

_SEQ_RUN = re.compile(rb'"seq":(\d+),"run":"([^"]+)"')


class SyslogSink:
    """Receptor TCP RFC 5424 com octet-counting (RFC 6587), o enquadramento do
    destino ``syslog_rfc5424``. Roda numa thread com loop próprio: o gate mede o
    caminho real até o fio, sem disco no meio. Conta cada ``raw.seq`` recebido."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self.host, self.port = host, port or _free_port()
        self.seen: Dict[bytes, Dict[int, int]] = {}
        #: ``seq`` na ordem de chegada (a 1ª vez de cada um), para medir inversões.
        self.order: Dict[bytes, List[int]] = {}
        self.frames = 0
        self.last_frame_at = 0.0
        #: Até este instante (monotônico), toda conexão é derrubada na chegada:
        #: destino fora do ar (cenário de store-and-forward, ADR-0018 Onda 4).
        self.refuse_until = 0.0
        #: Atraso por quadro lido: destino lento (cenário de isolamento).
        self.delay_s = 0.0
        self.refused = 0
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="syslog-sink", daemon=True)

    def start(self) -> "SyslogSink":
        self._thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("receptor syslog não subiu")
        return self

    def _run(self) -> None:
        asyncio.run(self._serve())

    async def _serve(self) -> None:
        server = await asyncio.start_server(self._client, self.host, self.port)
        self._ready.set()
        async with server:
            await server.serve_forever()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if time.monotonic() < self.refuse_until:
            self.refused += 1
            writer.transport.abort()  # RST: o envio do dispatcher falha na hora
            return
        try:
            while True:
                if time.monotonic() < self.refuse_until:
                    writer.transport.abort()
                    return
                size = await reader.readuntil(b" ")
                frame = await reader.readexactly(int(size[:-1]))
                if self.delay_s:
                    await asyncio.sleep(self.delay_s)
                self.frames += 1
                self.last_frame_at = time.monotonic()
                m = _SEQ_RUN.search(frame)
                if m:
                    runs = self.seen.setdefault(m.group(2), {})
                    seq = int(m.group(1))
                    if seq not in runs:
                        self.order.setdefault(m.group(2), []).append(seq)
                    runs[seq] = runs.get(seq, 0) + 1
        except (asyncio.IncompleteReadError, ConnectionError, ValueError):
            pass
        finally:
            writer.close()

    def count(self, run_id: str) -> int:
        """``seq`` DISTINTOS recebidos — duplicata não conta como entrega."""
        return len(self.seen.get(run_id.encode(), {}))


async def wait_received(sink: SyslogSink, run_id: str, expected: int,
                        timeout: float) -> Optional[float]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sink.count(run_id) >= expected:
            return sink.last_frame_at
        if sink.last_frame_at and time.monotonic() - sink.last_frame_at > 60:
            return None  # nada chega há um minuto: não vai chegar
        await asyncio.sleep(0.5)
    return None


def count_inversions(order: List[int]) -> int:
    """Eventos que chegaram DEPOIS de um ``seq`` maior: com a ingestão em ordem
    (uma requisição por vez, um shard no raw), cada um é um evento entregue fora
    da ordem em que entrou no pipeline."""
    top, n = -1, 0
    for seq in order:
        if seq < top:
            n += 1
        else:
            top = seq
    return n


def verify_counts(seen: Dict[int, int], run_id: str, ranges: List[tuple],
                  foreign: int = 0) -> Dict[str, int]:
    """Cada ``seq`` aceito (lote 200 inteiro) tem de aparecer exatamente uma vez."""
    expected = set()
    for first, size in ranges:
        expected.update(range(first, first + size))
    dups = sorted(seq for seq, n in seen.items() if n > 1)
    runs: List[List[int]] = []  # [início, tamanho] das sequências duplicadas
    for seq in dups:
        if runs and seq == runs[-1][0] + runs[-1][1]:
            runs[-1][1] += 1
        else:
            runs.append([seq, 1])
    return {
        "expected": len(expected),
        "delivered_unique": len(seen),
        "lost": len(expected - seen.keys()),
        "duplicated": sum(n - 1 for n in seen.values() if n > 1),
        "unexpected": len(seen.keys() - expected) + foreign,
        # O tamanho das sequências diz de onde veio a duplicata (registro do raw,
        # lote do processor, chunk do destino).
        "duplicate_runs": runs[:50],
    }


# ── Destino JSONL de uma instalação (modo external) ──────────────────────────


def count_delivered(out_dir: pathlib.Path) -> int:
    total = 0
    for path in out_dir.rglob("*"):
        if path.is_file():
            with path.open("rb") as fh:
                total += sum(1 for _ in fh)
    return total


async def wait_delivered(out_dir: pathlib.Path, expected: int, timeout: float) -> Optional[float]:
    deadline = time.monotonic() + timeout
    last, stable_since = -1, time.monotonic()
    while time.monotonic() < deadline:
        n = count_delivered(out_dir)
        if n >= expected:
            return time.monotonic()
        if n != last:
            last, stable_since = n, time.monotonic()
        elif time.monotonic() - stable_since > 60:
            return None  # parou de crescer há um minuto: não vai chegar
        await asyncio.sleep(1.0)
    return None


def verify(out_dir: pathlib.Path, run_id: str, ranges: List[tuple]) -> Dict[str, int]:
    """Conferência a partir dos arquivos de um destino JSONL."""
    seen: Dict[int, int] = {}
    foreign = 0
    for path in out_dir.rglob("*"):
        if not path.is_file():
            continue
        with path.open() as fh:
            for line in fh:
                try:
                    raw = json.loads(line).get("raw") or {}
                except ValueError:
                    foreign += 1
                    continue
                if raw.get("run") != run_id:
                    foreign += 1
                    continue
                seq = raw.get("seq")
                seen[seq] = seen.get(seq, 0) + 1
    return verify_counts(seen, run_id, ranges, foreign)


# ── Modo local ───────────────────────────────────────────────────────────────


class LocalStack:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.tag = f"cops-gate-{os.getpid()}"
        self.work = pathlib.Path(tempfile.mkdtemp(prefix="cops-gate-"))
        self.sinks = [SyslogSink().start() for _ in range(max(1, args.destinations))]
        self.sink = self.sinks[0]
        self.procs: List[subprocess.Popen] = []
        self.containers: List[str] = []
        self.pg, self.redis, self.kafka, self.api = (_free_port() for _ in range(4))
        self.env = {
            **os.environ,
            "APP_MASTER_KEY": "loadtest-master-key-not-for-production-0001",
            "APP_ENV": "development",
            "SESSION_SECURE_COOKIE": "0",
            "DATABASE_URL": f"postgresql+psycopg2://cops:pw@127.0.0.1:{self.pg}/centralops",
            "REDIS_URL": f"redis://127.0.0.1:{self.redis}/0",
            "CELERY_BROKER_URL": f"redis://127.0.0.1:{self.redis}/1",
            "CELERY_RESULT_BACKEND": f"redis://127.0.0.1:{self.redis}/2",
            "KAFKA_BOOTSTRAP_SERVERS": f"127.0.0.1:{self.kafka}",
            "KAFKA_TOPIC_PREFIX": "lt",
            "KAFKA_RAW_PARTITIONS": str(args.partitions),
            # Cenário de queda: o disjuntor abre rápido e a sonda volta logo,
            # para a drenagem do estacionamento caber no tempo do gate.
            **({"KAFKA_PARKED_HOLD_SECONDS": str(args.parked_hold)} if args.parked_hold else {}),
            # Entrega em ordem (Onda 4e): destinos com ``delivery.ordered`` e o raw
            # num shard só, para a ordem de entrada no pipeline ser a do ``seq``.
            **({"GATE_ORDERED": "1", "KAFKA_RAW_SHARDS_PER_STREAM": "1"} if args.ordered else {}),
        }
        self.bootstrap = self.env["KAFKA_BOOTSTRAP_SERVERS"]

    def _docker(self, name: str, *argv: str) -> None:
        subprocess.run(["docker", "run", "-d", "--name", name, *argv], check=True,
                       stdout=subprocess.DEVNULL)
        self.containers.append(name)

    def up(self) -> None:
        _log(f"containers ({self.tag}) e diretório de trabalho {self.work}")
        self._docker(f"{self.tag}-pg", "-e", "POSTGRES_USER=cops", "-e", "POSTGRES_PASSWORD=pw",
                     "-e", "POSTGRES_DB=centralops", "-p", f"127.0.0.1:{self.pg}:5432",
                     "postgres:16-alpine")
        self._docker(f"{self.tag}-redis", "-p", f"127.0.0.1:{self.redis}:6379", "redis:7-alpine",
                     "redis-server", "--maxmemory", "512mb", "--maxmemory-policy", "volatile-lru")
        self._docker(f"{self.tag}-rp", "-p", f"127.0.0.1:{self.kafka}:{self.kafka}",
                     "redpandadata/redpanda:v24.2.7", "redpanda", "start",
                     f"--smp={self.args.broker_cpus}", "--memory=2G", "--reserve-memory=0M",
                     "--overprovisioned", "--node-id=0", "--check=false",
                     "--kafka-addr", f"PLAINTEXT://0.0.0.0:{self.kafka}",
                     "--advertise-kafka-addr", f"PLAINTEXT://127.0.0.1:{self.kafka}")
        for _ in range(90):
            pg = subprocess.run(["docker", "exec", f"{self.tag}-pg", "pg_isready", "-U", "cops"],
                                capture_output=True)
            rp = subprocess.run(["docker", "exec", f"{self.tag}-rp", "rpk", "cluster", "health"],
                                capture_output=True, text=True)
            if pg.returncode == 0 and "Healthy:" in rp.stdout and "true" in rp.stdout:
                break
            time.sleep(1)
        else:
            raise RuntimeError("containers não ficaram prontos em 90 s")

        _log("migração e seed")
        subprocess.run([sys.executable, "-m", "app.db.migrate"], cwd=BACKEND, env=self.env,
                       check=True, capture_output=True)
        seed = subprocess.run([sys.executable, "-c", _SEED, *(str(k.port) for k in self.sinks)],
                              cwd=BACKEND,
                              env=self.env, check=True, capture_output=True, text=True)
        self.tokens = [d["token"] for d in json.loads(seed.stdout.strip().splitlines()[-1])]
        self.token = self.tokens[0]

        _log("API")
        self.spawn("api", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                   "--port", str(self.api), "--log-level", "warning")
        import urllib.request

        for _ in range(90):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.api}/livez", timeout=2) as r:
                    if r.status == 200:
                        return
            except OSError:
                pass
            time.sleep(1)
        raise RuntimeError("API não respondeu /livez em 90 s")

    def spawn(self, name: str, *argv: str) -> None:
        log = (self.work / f"{name}.log").open("w")
        # Visto num host Ubuntu 26.04 + Docker 29 (gate rodando dentro de um
        # container): ``execve`` devolvendo EPERM de forma intermitente, sem
        # registro no kernel; a mesma chamada passa na tentativa seguinte.
        for attempt in range(5):
            try:
                proc = subprocess.Popen([sys.executable, *argv], cwd=BACKEND, env=self.env,
                                        stdout=log, stderr=subprocess.STDOUT)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                _log(f"exec de {name} recusado (EPERM) — tentativa {attempt + 2}/5")
                time.sleep(0.5)
        self.procs.append(proc)

    def _entry(self, role: str) -> str:
        main = f"from app.collectors.dataplane.{role} import main"
        if role not in (self.args.profile or ()):
            return f"{main}; main()"
        # SIGTERM é cooperativo nos dois papéis: ``main`` retorna e o perfil é gravado.
        out = self.work / f"{role}-{len(self.procs)}.prof"
        return (f"import cProfile; {main}; p = cProfile.Profile(); p.enable()\n"
                f"try:\n    main()\nfinally:\n    p.disable(); p.dump_stats({str(out)!r})")

    def start_data_plane(self) -> None:
        for n in range(max(1, self.args.dispatchers)):
            self.spawn(f"dispatcher{n}" if n else "dispatcher", "-c", self._entry("dispatcher"))
        for n in range(self.args.processors):
            self.spawn(f"processor{n}", "-c", self._entry("processor"))

    def scale_out(self, n: int) -> None:
        """Processors que entram NO MEIO da drenagem: força um rebalance com lote
        em voo, o caso em que a troca de dono duplicava eventos."""
        base = self.args.processors
        for i in range(n):
            self.spawn(f"processor{base + i}", "-c", self._entry("processor"))

    def crashed(self) -> List[str]:
        return [p.args[-1][:60] for p in self.procs if p.poll() is not None]

    def down(self) -> None:
        for p in self.procs:
            p.terminate()
        for p in self.procs:
            try:
                p.wait(timeout=20)
            except subprocess.TimeoutExpired:
                p.kill()
        if self.containers:
            # ``-v``: os volumes anônimos das imagens (Postgres, Redis, Redpanda) vão
            # junto. Sem ele, cada rodada deixava ~1 GB de dados do Redpanda órfão.
            subprocess.run(["docker", "rm", "-f", "-v", *self.containers], capture_output=True)
        if self.args.keep:
            _log(f"logs mantidos em {self.work}")
        else:
            shutil.rmtree(self.work, ignore_errors=True)


def dlq_rows(stack: "LocalStack") -> Optional[int]:
    """Linhas na DLQ do Postgres — destino fora não pode gravar nenhuma."""
    r = subprocess.run(
        ["docker", "exec", f"{stack.tag}-pg", "psql", "-U", "cops", "-d", "centralops", "-tAc",
         "select count(*) from destination_dlq"],
        capture_output=True, text=True,
    )
    return int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else None


async def run_local(args: argparse.Namespace) -> Dict[str, Any]:
    push = _load_push_module()
    stack = LocalStack(args)
    try:
        await asyncio.to_thread(stack.up)
        n = len(stack.sinks)
        _log(f"fase 1 — ingestão por {args.duration:.0f} s em {n} destino(s), processors parados")
        loads = [
            argparse.Namespace(
                url=f"http://127.0.0.1:{stack.api}/api/ingest/traffic", token=tok,
                eps=(args.eps / n if args.eps else 0), duration=args.duration, batch=args.batch,
                concurrency=1 if args.ordered else max(1, args.concurrency // n), gzip=False,
            )
            for tok in stack.tokens
        ]
        sents = await asyncio.gather(
            *(push.send(ld, run_id=f"lt-{int(time.time())}-{i}") for i, ld in enumerate(loads))
        )
        sent = {
            "run_id": sents[0]["run_id"],
            "elapsed": max(x["elapsed"] for x in sents),
            "sent": sum(x["sent"] for x in sents),
            "accepted": sum(x["accepted"] for x in sents),
            "statuses": sents[0]["statuses"] if n == 1 else {
                k: sum(x["statuses"].get(k, 0) for x in sents)
                for k in {k for x in sents for k in x["statuses"]}
            },
            "p50_ms": max(x["p50_ms"] for x in sents),
            "p95_ms": max(x["p95_ms"] for x in sents),
            "p99_ms": max(x["p99_ms"] for x in sents),
            "accepted_ranges": sents[0]["accepted_ranges"],
        }
        accepted = sent["accepted"]
        topic_raw, topic_deliver = "lt.raw", "lt.deliver"

        _log(
            f"fase 2 — {args.processors} processor(s) + {max(1, args.dispatchers)} dispatcher(s) "
            f"drenando {accepted} eventos"
        )
        t0 = time.monotonic()
        if args.outage_seconds:
            stack.sinks[0].refuse_until = t0 + args.outage_seconds
            _log(f"destino 0 FORA DO AR por {args.outage_seconds:.0f} s")
        if args.slow_ms:
            stack.sinks[0].delay_s = args.slow_ms / 1000.0
            _log(f"destino 0 LENTO: {args.slow_ms:.0f} ms por quadro")
        await asyncio.to_thread(stack.start_data_plane)
        if args.scale_out:
            await asyncio.sleep(args.scale_out_after)
            _log(f"escala: +{args.scale_out} processor(s) com a drenagem em curso")
            await asyncio.to_thread(stack.scale_out, args.scale_out)

        parked_peak = 0

        async def _watch_parked() -> None:
            nonlocal parked_peak
            while True:
                try:
                    parked_peak = max(parked_peak, await group_lag(
                        stack.bootstrap, "centralops-parked", "lt.deliver.parked"))
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(2)

        watcher = asyncio.create_task(_watch_parked())
        t_proc = await wait_lag_zero(stack.bootstrap, PROCESSOR_GROUP, topic_raw, args.timeout)
        per_dest = []
        for i, (sink, sd) in enumerate(zip(stack.sinks, sents)):
            t_done = await wait_received(sink, sd["run_id"], sd["accepted"], args.timeout)
            per_dest.append({
                "destination": i,
                "accepted": sd["accepted"],
                "e2e_seconds": None if t_done is None else t_done - t0,
                "refused_connections": sink.refused,
                "inversions": count_inversions(sink.order.get(sd["run_id"].encode(), [])),
                "verify": verify_counts(
                    dict(sink.seen.get(sd["run_id"].encode(), {})), sd["run_id"],
                    sd["accepted_ranges"],
                ),
            })
        watcher.cancel()
        dispatcher_lag = await group_lag(stack.bootstrap, DISPATCHER_GROUP, topic_deliver)
        done = [d["e2e_seconds"] for d in per_dest]
        total = {k: sum(d["verify"][k] for d in per_dest)
                 for k in ("expected", "delivered_unique", "lost", "duplicated", "unexpected")}
        return {
            "mode": "local",
            "processors": args.processors + args.scale_out,
            "ingest": sent,
            "processor_seconds": None if t_proc is None else t_proc - t0,
            "e2e_seconds": None if any(x is None for x in done) else max(done),
            "dispatcher_lag_end": dispatcher_lag,
            "crashed": stack.crashed(),
            "verify": total,
            "destinations": per_dest,
            "dlq_rows": dlq_rows(stack),
            "parked_peak_records": parked_peak,
        }
    finally:
        await asyncio.to_thread(stack.down)


# ── Modo external ────────────────────────────────────────────────────────────


async def run_external(args: argparse.Namespace) -> Dict[str, Any]:
    push = _load_push_module()
    out_dir = pathlib.Path(args.output_dir) if args.output_dir else None
    before = count_delivered(out_dir) if out_dir else 0
    load = argparse.Namespace(url=args.url, token=args.token, eps=args.eps,
                              duration=args.duration, batch=args.batch,
                              concurrency=args.concurrency, gzip=args.gzip)
    t0 = time.monotonic()
    lag_peak = 0

    async def _watch() -> None:
        nonlocal lag_peak
        while True:
            try:
                lag_peak = max(lag_peak, await group_lag(
                    args.bootstrap, PROCESSOR_GROUP, f"{args.topic_prefix}.raw"))
            except Exception:  # noqa: BLE001 — medir nunca derruba a carga
                pass
            await asyncio.sleep(5)

    watcher = asyncio.create_task(_watch())
    try:
        sent = await push.send(load)
    finally:
        watcher.cancel()
    t_proc = await wait_lag_zero(args.bootstrap, PROCESSOR_GROUP, f"{args.topic_prefix}.raw",
                                 args.timeout)
    result: Dict[str, Any] = {
        "mode": "external",
        "ingest": sent,
        "processor_seconds": None if t_proc is None else t_proc - t0,
        "processor_lag_peak_records": lag_peak,
        "crashed": [],
    }
    if out_dir:
        t_e2e = await wait_delivered(out_dir, before + sent["accepted"], args.timeout)
        result["e2e_seconds"] = None if t_e2e is None else t_e2e - t0
        result["verify"] = verify(out_dir, sent["run_id"], sent["accepted_ranges"])
    return result


# ── Veredito ─────────────────────────────────────────────────────────────────


def verdict(args: argparse.Namespace, r: Dict[str, Any]) -> List[str]:
    """Lista de limiares que falharam (vazia = gate verde)."""
    failures: List[str] = []
    accepted = r["ingest"]["accepted"]
    ingest_eps = accepted / r["ingest"]["elapsed"] if r["ingest"]["elapsed"] else 0
    r["ingest_eps"] = ingest_eps
    if not accepted:
        failures.append("nenhum evento aceito na ingestão")
    if r.get("crashed"):
        failures.append(f"processos caíram: {r['crashed']}")
    if r.get("processor_seconds") is None:
        failures.append("o lag do processor não zerou dentro do prazo")
    else:
        if r["mode"] == "local":
            proc_eps = accepted / r["processor_seconds"]
            r["processor_eps"] = proc_eps
            r["processor_eps_per_process"] = proc_eps / max(1, r["processors"])
            # Com processos entrando no meio, "por processo" não tem denominador
            # honesto: o limiar só vale com o número fixo do começo ao fim.
            if not args.scale_out and r["processor_eps_per_process"] < args.min_processor_eps:
                failures.append(
                    f"processor {r['processor_eps_per_process']:,.0f} eps/processo "
                    f"< {args.min_processor_eps:,.0f}"
                )
    if "e2e_seconds" in r:
        if r["e2e_seconds"] is None:
            failures.append("nem todo evento aceito chegou ao destino")
        else:
            e2e_eps = accepted / r["e2e_seconds"]
            r["e2e_eps"] = e2e_eps
            if e2e_eps < args.min_e2e_eps:
                failures.append(f"ponta a ponta {e2e_eps:,.0f} eps < {args.min_e2e_eps:,.0f}")
    elif args.min_e2e_eps:
        failures.append("--min-e2e-eps pede --output-dir (sem ele não há como medir a entrega)")
    if r.get("dlq_rows"):
        failures.append(f"{r['dlq_rows']} linha(s) na DLQ do Postgres (destino fora não grava)")
    if args.mode == "local" and len(r.get("destinations") or []) > 1 and args.max_isolation_s:
        healthy = [d["e2e_seconds"] for d in r["destinations"][1:]]
        if any(h is None or h > args.max_isolation_s for h in healthy):
            failures.append(
                f"destinos saudáveis terminaram em {healthy} s > {args.max_isolation_s:.0f} s "
                "(isolamento)"
            )
    if args.mode == "local" and args.ordered:
        bad = {d["destination"]: d["inversions"] for d in r.get("destinations") or []
               if d["inversions"]}
        if bad:
            failures.append(f"entrega fora de ordem (inversões por destino): {bad}")
    v = r.get("verify")
    if v is not None:
        if v["lost"]:
            failures.append(f"{v['lost']} evento(s) aceito(s) não chegaram ao destino")
        if v["duplicated"] and not args.allow_duplicates:
            failures.append(f"{v['duplicated']} duplicata(s) no destino")
    return failures


def report(r: Dict[str, Any], failures: List[str]) -> None:
    ing = r["ingest"]
    print()
    print("── resultado ──────────────────────────────────────────")
    print(f"ingestão             {ing['accepted']:,} aceitos em {ing['elapsed']:.1f} s "
          f"= {r['ingest_eps']:,.0f} eps  (p95 {ing['p95_ms']:.0f} ms, {ing['statuses']})")
    if r.get("processor_seconds") is not None:
        line = f"processor            lag zerou em {r['processor_seconds']:.1f} s"
        if "processor_eps" in r:
            line += (f" = {r['processor_eps']:,.0f} eps "
                     f"({r['processor_eps_per_process']:,.0f} por processo, "
                     f"{r['processors']} processo(s))")
        print(line)
    if r.get("e2e_eps") is not None:
        print(f"ponta a ponta        último evento no destino em {r['e2e_seconds']:.1f} s "
              f"= {r['e2e_eps']:,.0f} eps")
    if r.get("verify") is not None:
        v = r["verify"]
        print(f"conferência          esperados {v['expected']:,}, entregues {v['delivered_unique']:,}, "
              f"perdidos {v['lost']}, duplicados {v['duplicated']}, estranhos {v['unexpected']}")
    for d in r.get("destinations") or []:
        if len(r["destinations"]) > 1 or d["refused_connections"] or d["inversions"]:
            vv = d["verify"]
            e2e = "—" if d["e2e_seconds"] is None else f"{d['e2e_seconds']:.1f} s"
            print(f"  destino {d['destination']}: {d['accepted']:,} em {e2e}, perdidos {vv['lost']}, "
                  f"duplicados {vv['duplicated']}, conexões recusadas {d['refused_connections']}, "
                  f"fora de ordem {d['inversions']}")
    if "dlq_rows" in r:
        print(f"DLQ do Postgres      {r['dlq_rows']} linha(s); estacionamento no pico: "
              f"{r.get('parked_peak_records', 0)} registro(s)")
    print("GATE VERDE" if not failures else "GATE VERMELHO:\n  - " + "\n  - ".join(failures))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--duration", type=float, default=60, help="segundos de ingestão")
        p.add_argument("--eps", type=float, default=0, help="ritmo alvo (0 = o mais rápido possível)")
        # O token de ingestão aceita 600 requisições/min: o teto por token é
        # 10 × batch eventos/s (2.000 → 20 mil eps).
        p.add_argument("--batch", type=int, default=2000, help="eventos por requisição")
        p.add_argument("--concurrency", type=int, default=8, help="requisições simultâneas")
        p.add_argument("--timeout", type=float, default=1800, help="prazo para drenar, em segundos")
        p.add_argument("--min-e2e-eps", type=float, default=0, help="limiar de ponta a ponta")
        p.add_argument("--allow-duplicates", action="store_true",
                       help="duplicata não reprova (entrega é at-least-once)")
        p.add_argument("--json", help="grava o resultado completo neste arquivo")

    lp = sub.add_parser("local", help="sobe a stack nesta máquina e mede em duas fases")
    common(lp)
    lp.add_argument("--processors", type=int, default=1, help="processos processor")
    lp.add_argument("--scale-out", type=int, default=0,
                    help="processors a mais que entram durante a drenagem (força rebalance)")
    lp.add_argument("--scale-out-after", type=float, default=15,
                    help="segundos de drenagem antes de escalar")
    lp.add_argument("--destinations", type=int, default=1,
                    help="destinos (cada um numa org, com token e receptor próprios)")
    lp.add_argument("--dispatchers", type=int, default=1, help="processos dispatcher")
    lp.add_argument("--outage-seconds", type=float, default=0,
                    help="destino 0 recusa conexões nos primeiros S s da drenagem")
    lp.add_argument("--slow-ms", type=float, default=0,
                    help="destino 0 lê cada quadro com este atraso (destino lento)")
    lp.add_argument("--parked-hold", type=float, default=0,
                    help="KAFKA_PARKED_HOLD_SECONDS dos dispatchers (0 = padrão do produto)")
    lp.add_argument("--max-isolation-s", type=float, default=0,
                    help="destinos saudáveis têm de terminar em até S s (com --slow-ms)")
    lp.add_argument("--ordered", action="store_true",
                    help="destinos com entrega em ordem; ingestão serial e raw num shard, "
                         "e o gate reprova qualquer evento fora de ordem")
    lp.add_argument("--partitions", type=int, default=16,
                    help="partições do tópico raw (o padrão do produto)")
    lp.add_argument("--broker-cpus", type=int, default=2, help="--smp do Redpanda")
    lp.add_argument("--min-processor-eps", type=float, default=3000,
                    help="limiar de processamento por processo")
    lp.add_argument("--keep", action="store_true", help="mantém os logs (e os perfis)")
    lp.add_argument("--profile", action="append", choices=("processor", "dispatcher"),
                    help="roda o papel sob cProfile (.prof no diretório de trabalho; use com --keep)")

    ep = sub.add_parser("external", help="contra uma instalação já de pé")
    common(ep)
    ep.add_argument("--url", required=True, help="URL do endpoint de ingestão")
    ep.add_argument("--token", required=True, help="token de ingestão da integração push")
    ep.add_argument("--bootstrap", required=True, help="bootstrap do Kafka/Redpanda")
    ep.add_argument("--topic-prefix", default="centralops", help="KAFKA_TOPIC_PREFIX da instalação")
    ep.add_argument("--output-dir", help="diretório do destino JSONL (habilita a conferência)")
    ep.add_argument("--gzip", action="store_true")

    args = parser.parse_args()
    if args.mode == "local":
        result = asyncio.run(run_local(args))
    else:
        args.min_processor_eps, args.scale_out, args.max_isolation_s = 0, 0, 0
        result = asyncio.run(run_external(args))
    failures = verdict(args, result)
    report(result, failures)
    if args.json:
        result.get("ingest", {}).pop("accepted_ranges", None)
        pathlib.Path(args.json).write_text(json.dumps({**result, "failures": failures}, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
