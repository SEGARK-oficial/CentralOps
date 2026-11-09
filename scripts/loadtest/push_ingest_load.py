"""Carga de ingestão push contra um CentralOps de verdade. FORA DO CI.

Mede o lado de ENTRADA da Onda 2 do ADR-0018: quantos eventos por segundo o
``POST /api/ingest/<stream>`` aceita com o 200 saindo só depois do ack do tópico
raw, a latência de cada requisição e quantas foram recusadas (503 = data-plane
fora; 429 = rate limit do token). O lado de PROCESSAMENTO aparece no lag do
consumer group do processor (métrica ``collector_raw_consumer_lag``, ou
``rpk group describe centralops-processor``).

Gera eventos parecidos com tráfego de firewall (~300 bytes) em lotes NDJSON, com
N conexões concorrentes, num ritmo alvo (``--eps``) ou o mais rápido possível
(``--eps 0``). Cada evento leva um id sequencial (``seq``) para conferir perda e
duplicata no destino depois.

COMO RODAR

    python scripts/loadtest/push_ingest_load.py \\
        --url https://centralops.exemplo/api/ingest/traffic \\
        --token coi_<id>_xxxx --eps 20000 --duration 3600 --batch 1000 --concurrency 8

O rate limit padrão do token é 600 requisições/min: para 20k eps use lotes de
pelo menos 2.000 eventos ou suba o limite.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import random
import statistics
import time
from collections import Counter


def _event(seq: int, run_id: str) -> dict:
    return {
        "seq": seq,
        "run": run_id,
        "srcip": f"10.{random.randint(0, 255)}.{random.randint(0, 255)}.{random.randint(1, 254)}",
        "dstip": f"172.16.{random.randint(0, 255)}.{random.randint(1, 254)}",
        "srcport": random.randint(1024, 65535),
        "dstport": random.choice([22, 53, 80, 443, 3389, 8080]),
        "action": random.choice(["accept", "deny", "close", "timeout"]),
        "proto": 6,
        "policyid": random.randint(1, 500),
        "sentbyte": random.randint(40, 1_000_000),
        "rcvdbyte": random.randint(40, 1_000_000),
        "devname": "fw-loadtest",
        "logid": "0000000013",
        "type": "traffic",
        "subtype": "forward",
    }


async def run(args: argparse.Namespace) -> int:
    stats = await send(args)
    return 0 if stats["accepted"] else 1


async def send(args: argparse.Namespace, run_id: str | None = None) -> dict:
    """Envia a carga e devolve as contagens (também usado por ``dataplane_gate``)."""
    import aiohttp

    run_id = run_id or f"lt-{int(time.time())}"
    seq = 0
    statuses: Counter = Counter()
    latencies: list[float] = []
    accepted = 0
    # Lotes aceitos INTEIROS (200): [primeiro seq, tamanho]. É o que o destino
    # tem de receber, um a um — base da conferência de perda e duplicata.
    accepted_ranges: list[tuple[int, int]] = []
    deadline = time.monotonic() + args.duration
    started = time.monotonic()
    interval = (args.batch / args.eps) if args.eps > 0 else 0.0
    next_slot = time.monotonic()
    lock = asyncio.Lock()

    headers = {"Authorization": f"Bearer {args.token}", "Content-Type": "application/x-ndjson"}
    if args.gzip:
        headers["Content-Encoding"] = "gzip"

    async def worker(session: "aiohttp.ClientSession") -> None:
        nonlocal seq, accepted, next_slot
        while time.monotonic() < deadline:
            async with lock:
                first = seq
                seq += args.batch
                if interval:
                    next_slot = max(next_slot + interval, time.monotonic())
                    wait = next_slot - interval - time.monotonic()
                else:
                    wait = 0.0
            if wait > 0:
                await asyncio.sleep(wait)
            body = "\n".join(json.dumps(_event(first + i, run_id)) for i in range(args.batch)).encode()
            if args.gzip:
                body = gzip.compress(body)
            t0 = time.perf_counter()
            try:
                async with session.post(args.url, data=body, headers=headers) as resp:
                    payload = await resp.read()
                    statuses[resp.status] += 1
                    if resp.status in (200, 207):
                        n = json.loads(payload).get("accepted", 0)
                        accepted += n
                        if resp.status == 200 and n == args.batch:
                            accepted_ranges.append((first, args.batch))
                    elif resp.status in (429, 503):
                        retry = float(resp.headers.get("Retry-After", "1"))
                        await asyncio.sleep(min(retry, 5.0))
            except aiohttp.ClientError as exc:
                statuses[type(exc).__name__] += 1
            latencies.append(time.perf_counter() - t0)

    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        await asyncio.gather(*(worker(session) for _ in range(args.concurrency)))

    elapsed = time.monotonic() - started
    lat = sorted(latencies) or [0.0]

    def pct(p: float) -> float:
        return lat[min(len(lat) - 1, int(p * len(lat)))] * 1000

    print(f"run_id               {run_id}")
    print(f"duração              {elapsed:.1f} s")
    print(f"eventos enviados     {seq}")
    print(f"eventos aceitos      {accepted}  ({accepted / elapsed:,.0f} eps)")
    print(f"requisições          {dict(statuses)}")
    print(f"latência ms          p50={pct(0.50):.0f} p95={pct(0.95):.0f} p99={pct(0.99):.0f} "
          f"média={statistics.mean(lat) * 1000:.0f}")
    return {
        "run_id": run_id,
        "elapsed": elapsed,
        "sent": seq,
        "accepted": accepted,
        "statuses": dict(statuses),
        "accepted_ranges": accepted_ranges,
        "p50_ms": pct(0.50),
        "p95_ms": pct(0.95),
        "p99_ms": pct(0.99),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True, help="URL completa do endpoint, ex.: http://host/api/ingest/traffic")
    parser.add_argument("--token", required=True, help="token de ingestão da integração push")
    parser.add_argument("--eps", type=float, default=0, help="eventos/s alvo (0 = o mais rápido possível)")
    parser.add_argument("--duration", type=float, default=60, help="segundos")
    parser.add_argument("--batch", type=int, default=1000, help="eventos por requisição")
    parser.add_argument("--concurrency", type=int, default=4, help="requisições simultâneas")
    parser.add_argument("--gzip", action="store_true", help="comprimir o corpo (Content-Encoding: gzip)")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
