"""Receptor syslog: UDP/TCP/TLS → parse → classifica → buffer de ingestão.

Processo próprio (``python -c "from app.syslog.server import main; main()"``),
como o dispatcher Kafka: não é worker Celery nem parte da API. O que ele sabe:

* **Quem é a fonte** — pela tabela ``syslog_sources`` (recarregada a cada
  ``SYSLOG_SOURCE_REFRESH_S``): o par (IP de origem ∈ CIDR, porta de escuta)
  resolve a integração, o mais específico vence. Fonte desconhecida é
  DESCARTADA e contada (``collector_syslog_received_total{outcome="unknown_source"}``)
  — porta 514 aberta recebe lixo da rede inteira, e aceitar tudo seria um
  buffer envenenado sem dono.
* **Qual stream** — pelo classificador da fonte (:mod:`app.collectors.classify`).
  Stream que a plataforma não registra é ``unclassified`` (contado, descartado)
  — a API recusa salvar uma fonte cujo stream não existe, então isso só
  acontece se alguém apagou o stream depois.
* **Para onde** — ``ingest_buffer.push_events`` da integração, com o MESMO
  carimbo ``_ingest`` do endpoint HTTP (+ ``transport="syslog"`` e ``peer``).
  Dali em diante o pipeline não distingue.

Lotes: eventos acumulam por (integração, stream) e vão ao Redis a cada
``SYSLOG_FLUSH_MS`` ou ``SYSLOG_BATCH_MAX`` — um LPUSH por lote, não por linha.
SIGTERM drena o que está em memória antes de sair.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import signal
import ssl
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..collectors.classify import Classifier, compile_classifier
from ..core.config import settings
from .parser import parse_syslog_line

logger = logging.getLogger(__name__)


# ── fontes ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SourceMatch:
    source_id: int
    integration_id: int
    organization_id: int
    platform: str
    classifier: Classifier
    name: str


@dataclass
class _SourceEntry:
    network: Any  # ipaddress network
    listen_port: Optional[int]
    transport: str  # any|udp|tcp|tls
    match: SourceMatch


class SourceTable:
    """Snapshot imutável das fontes habilitadas; ``resolve`` é puro."""

    def __init__(self, entries: List[_SourceEntry]) -> None:
        # Mais específico primeiro: prefixo maior, depois porta explícita, depois transporte explícito.
        self._entries = sorted(
            entries,
            key=lambda e: (-e.network.prefixlen, e.listen_port is None, e.transport == "any"),
        )

    def __len__(self) -> int:
        return len(self._entries)

    def resolve(self, peer_ip: str, port: int, transport: str) -> Optional[SourceMatch]:
        try:
            ip = ipaddress.ip_address(peer_ip)
        except ValueError:
            return None
        for e in self._entries:
            if ip.version != e.network.version or ip not in e.network:
                continue
            if e.listen_port is not None and e.listen_port != port:
                continue
            if e.transport != "any" and e.transport != transport:
                continue
            return e.match
        return None


def load_source_table() -> SourceTable:
    """Lê ``syslog_sources`` habilitadas com a integração ativa. SÍNCRONO — o
    servidor chama via ``to_thread``. Falha de banco ⇒ levanta; o chamador
    mantém a tabela anterior."""
    from ..db import database, models

    entries: List[_SourceEntry] = []
    with database.SessionLocal() as db:
        rows = (
            db.query(models.SyslogSource, models.Integration)
            .join(models.Integration, models.Integration.id == models.SyslogSource.integration_id)
            .filter(models.SyslogSource.enabled.is_(True), models.Integration.is_active.is_(True))
            .all()
        )
        for src, integ in rows:
            try:
                net = ipaddress.ip_network(src.source_cidr, strict=False)
                cfg = json.loads(src.classifier_json) if src.classifier_json else {}
                clf = compile_classifier(cfg, default_stream=src.default_stream)
            except Exception:  # noqa: BLE001 — uma fonte ruim não derruba as outras
                logger.warning("syslog: fonte %s ignorada (config inválida)", src.id, exc_info=True)
                continue
            entries.append(_SourceEntry(
                network=net, listen_port=src.listen_port, transport=str(src.transport or "any"),
                match=SourceMatch(
                    source_id=int(src.id), integration_id=int(integ.id),
                    organization_id=int(integ.organization_id), platform=str(integ.platform),
                    classifier=clf, name=str(src.name),
                ),
            ))
    return SourceTable(entries)


# ── métricas ───────────────────────────────────────────────────────────────

def _count(transport: str, outcome: str, n: int = 1) -> None:
    try:
        from ..collectors import metrics

        metrics.SYSLOG_RECEIVED.labels(transport=transport, outcome=outcome).inc(n)
    except Exception:  # noqa: BLE001
        pass


def _count_ingest(platform: str, stream: str, accepted: int, dropped: int, integration_id: int, depth: Optional[int]) -> None:
    try:
        from ..collectors import metrics

        if accepted:
            metrics.INGEST_ACCEPTED.labels(vendor=platform, stream=stream).inc(accepted)
        if dropped:
            metrics.INGEST_DROPPED.labels(vendor=platform, stream=stream).inc(dropped)
        if depth is not None:
            metrics.INGEST_BUFFER_DEPTH.labels(integration_id=str(integration_id), stream=stream).set(depth)
    except Exception:  # noqa: BLE001
        pass


# ── receptor ───────────────────────────────────────────────────────────────

def stamp_event(event: Dict[str, Any], stream: str, *, transport: str, peer: str) -> Dict[str, Any]:
    """Mesmo carimbo do ``POST /api/ingest`` (id estável por CONTEÚDO, antes dos
    metadados voláteis) + de onde veio."""
    digest = hashlib.sha256(
        json.dumps(event, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    from ..collectors.base import utcnow_iso

    event["_ingest"] = {"id": digest, "received_at": utcnow_iso(), "stream": stream, "transport": transport, "peer": peer}
    return event


class Receiver:
    """Estado compartilhado pelos protocolos: tabela de fontes, filas por
    (integração, stream) e o flusher."""

    def __init__(self, *, redis: Any, sources: SourceTable, registry_has, batch_max: int, flush_ms: int, max_line: int) -> None:
        self.redis = redis
        self.sources = sources
        self._registry_has = registry_has
        self.batch_max = batch_max
        self.flush_s = flush_ms / 1000.0
        self.max_line = max_line
        self._queues: Dict[Tuple[int, str, str], List[Dict[str, Any]]] = {}
        self._unknown_log_at: Dict[str, float] = {}
        self.stats: Dict[str, int] = {}

    # ── entrada ──
    def handle_line(self, raw: bytes, *, peer_ip: str, port: int, transport: str) -> Optional[str]:
        """Uma linha (sem framing). Devolve o outcome (para testes)."""
        if not raw.strip():
            return None
        if len(raw) > self.max_line:
            raw = raw[: self.max_line]
            self._bump("truncated")
        match = self.sources.resolve(peer_ip, port, transport)
        if match is None:
            self._bump("unknown_source"); _count(transport, "unknown_source")
            self._log_unknown(peer_ip, port, transport)
            return "unknown_source"
        event = parse_syslog_line(raw)
        stream = match.classifier.classify(event)
        if not stream or not self._registry_has(match.platform, stream):
            self._bump("unclassified"); _count(transport, "unclassified")
            return "unclassified"
        stamp_event(event, stream, transport=transport, peer=peer_ip)
        key = (match.integration_id, stream, match.platform)
        q = self._queues.setdefault(key, [])
        q.append(event)
        self._bump("accepted"); _count(transport, "accepted")
        return "accepted"

    def handle_datagram(self, data: bytes, *, peer_ip: str, port: int) -> None:
        # Um datagrama = uma mensagem (pode vir com \\n no fim; nunca multi-linha por padrão).
        for line in data.split(b"\n") if b"\n" in data.strip() else (data,):
            self.handle_line(line, peer_ip=peer_ip, port=port, transport="udp")

    # ── saída ──
    def pending(self) -> int:
        return sum(len(q) for q in self._queues.values())

    async def flush(self) -> int:
        """Empurra tudo que está nas filas. Um LPUSH por (integração, stream)."""
        if not self._queues:
            return 0
        from ..collectors.ingest_buffer import buffer_depth, push_goes_to_kafka, write_push_events

        to_kafka = push_goes_to_kafka()

        queues, self._queues = self._queues, {}
        total = 0
        for (integration_id, stream, platform), events in queues.items():
            try:
                # Kafka (deploy): tópico raw com ack (ADR-0018 Onda 2). Celery (dev):
                # lista Redis. Falha em qualquer um → retém em memória e re-tenta.
                accepted, refused = await write_push_events(self.redis, integration_id, stream, events)
                depth = 0 if to_kafka else await buffer_depth(self.redis, integration_id, stream)
            except Exception:  # noqa: BLE001 — Redis fora: devolve para a fila, tenta no próximo tick
                logger.warning("syslog: falha ao gravar (raw/buffer) integration_id=%s stream=%s — %d evento(s) retidos",
                               integration_id, stream, len(events), exc_info=True)
                self._retain((integration_id, stream, platform), events)
                continue
            if refused:
                # Buffer cheio: o push é tudo-ou-nada e NADA foi gravado
                # (ADR-0018 Onda 0). Retém em memória e tenta no próximo tick,
                # como com o Redis fora.
                logger.warning("syslog: buffer cheio integration_id=%s stream=%s — %d evento(s) retidos",
                               integration_id, stream, refused)
                self._retain((integration_id, stream, platform), events)
            total += accepted
            # Recusados ficam retidos (não são perda): o contador de descarte só
            # conta o que o teto de retenção em memória realmente jogou fora.
            _count_ingest(platform, stream, accepted, 0, integration_id, depth)
        return total

    def _retain(self, key: Tuple[int, str, str], events: List[Dict[str, Any]]) -> None:
        """Devolve eventos não gravados à fila em memória. Teto de retenção: não
        vira OOM se o Redis ficar fora (ou cheio) por horas — o excedente mais
        antigo é descartado e CONTADO (syslog não tem ack para devolver)."""
        q = self._queues.setdefault(key, [])
        q[:0] = events
        if len(q) > self.batch_max * 20:
            dropped_mem = len(q) - self.batch_max * 20
            del q[: dropped_mem]
            self._bump("dropped_memory"); _count("any", "dropped_memory", dropped_mem)

    async def run_flusher(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.flush_s)
            except asyncio.TimeoutError:
                pass
            if self.pending():
                await self.flush()
        await self.flush()

    def maybe_flush_soon(self) -> bool:
        return self.pending() >= self.batch_max

    # ── util ──
    def _bump(self, k: str) -> None:
        self.stats[k] = self.stats.get(k, 0) + 1

    def _log_unknown(self, ip: str, port: int, transport: str) -> None:
        now = time.monotonic()
        if now - self._unknown_log_at.get(ip, 0.0) < 60:
            return
        self._unknown_log_at[ip] = now
        logger.warning(
            "syslog: mensagem de fonte DESCONHECIDA %s → :%d/%s descartada — cadastre a fonte "
            "(CIDR) na integração para aceitar", ip, port, transport,
            extra={"event": "syslog.unknown_source", "peer": ip, "port": port, "transport": transport},
        )


class _UdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, receiver: Receiver, port: int) -> None:
        self.receiver, self.port = receiver, port

    def datagram_received(self, data: bytes, addr: Tuple[str, int]) -> None:
        self.receiver.handle_datagram(data, peer_ip=addr[0], port=self.port)


#: RFC 6587 limita o MSG-LEN a ``NONZERO-DIGIT *DIGIT``; 9 dígitos já passam de
#: qualquer ``max_line`` razoável. Mais que isso não é octet-counting.
_OCTET_MAX_DIGITS = 9


async def _read_line_rest(reader: asyncio.StreamReader, receiver: Receiver) -> Optional[bytes]:
    """Lê até o próximo LF (framing não-transparente). ``None`` = linha grande
    demais, consumida e descartada."""
    try:
        return await reader.readuntil(b"\n")
    except asyncio.IncompleteReadError as exc:
        return exc.partial
    except asyncio.LimitOverrunError as exc:
        # Descarta a linha INTEIRA até o LF — consumir só ``max_line`` deixava o
        # resto da linha ser lido como se fosse o próximo frame.
        receiver._bump("truncated")
        await reader.read(exc.consumed)
        while True:
            try:
                await reader.readuntil(b"\n")
                return None
            except asyncio.IncompleteReadError:
                return None
            except asyncio.LimitOverrunError as more:
                await reader.read(more.consumed)


async def _read_octet_frame(reader: asyncio.StreamReader, receiver: Receiver, size: int) -> bytes:
    """Lê exatamente ``size`` octetos. Acima de ``max_line`` guarda só o começo
    e descarta o resto do frame, para não desalinhar o próximo."""
    keep = min(size, receiver.max_line)
    body = await reader.readexactly(keep)
    rest = size - keep
    if rest:
        receiver._bump("truncated")
        while rest:
            rest -= len(await reader.readexactly(min(rest, 65536)))
    return body


async def _serve_stream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *, receiver: Receiver, port: int, transport: str) -> None:
    """Uma conexão TCP/TLS: separa as mensagens e entrega cada uma ao receiver.

    Framing por frame, como a RFC 6587 manda (ADR-0018 Onda 0):
      * começa com dígito e os dígitos terminam em espaço → octet-counting:
        lê exatamente MSG-LEN octetos. Frames podem vir colados, sem LF entre
        eles (rsyslog ``octet-counted``, syslog-ng ``syslog()``);
      * qualquer outra coisa → não-transparente: a mensagem vai até o LF.
    Antes o servidor lia até o LF e cortava o corpo no MSG-LEN, jogando fora
    todos os frames seguintes colados na mesma "linha".
    """
    peer = writer.get_extra_info("peername") or ("?", 0)
    peer_ip = str(peer[0])
    try:
        while True:
            try:
                first = await reader.readexactly(1)
            except asyncio.IncompleteReadError:
                break  # EOF limpo entre frames
            if first in b"\r\n\t ":
                continue  # separador/trailer entre frames
            body: Optional[bytes]
            if first.isdigit() and first != b"0":
                digits = first
                tail = b""
                while True:
                    try:
                        c = await reader.readexactly(1)
                    except asyncio.IncompleteReadError:
                        c = b""
                    if c.isdigit() and len(digits) < _OCTET_MAX_DIGITS:
                        digits += c
                        continue
                    tail = c
                    break
                if tail == b" ":
                    # Só é octet-counting se a mensagem começa pelo PRI (``<``);
                    # uma linha crua tipo ``200 OK ...`` segue até o LF.
                    try:
                        lead = await reader.readexactly(1)
                    except asyncio.IncompleteReadError:
                        lead = b""
                    if lead == b"<":
                        try:
                            body = lead + await _read_octet_frame(
                                reader, receiver, int(digits) - 1
                            )
                        except asyncio.IncompleteReadError as exc:
                            body = lead + exc.partial  # conexão caiu no meio do frame
                    elif lead in (b"\n", b""):
                        body = digits
                    else:
                        rest = await _read_line_rest(reader, receiver)
                        body = None if rest is None else digits + b" " + lead + rest
                elif tail in (b"\n", b""):
                    body = digits
                else:
                    rest = await _read_line_rest(reader, receiver)
                    body = None if rest is None else digits + tail + rest
            else:
                rest = await _read_line_rest(reader, receiver)
                body = None if rest is None else first + rest
            if body is None:
                continue
            body = body.rstrip(b"\r\n")
            if body:
                receiver.handle_line(body, peer_ip=peer_ip, port=port, transport=transport)
            if receiver.maybe_flush_soon():
                await receiver.flush()
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


def _tls_context() -> Optional[ssl.SSLContext]:
    cert, key = settings.SYSLOG_TLS_CERT, settings.SYSLOG_TLS_KEY
    if not cert or not key:
        return None
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cert, key)
    if settings.SYSLOG_TLS_CA:
        # mTLS: só quem apresenta certificado assinado pela CA fala com a porta.
        ctx.load_verify_locations(settings.SYSLOG_TLS_CA)
        ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


async def _refresh_sources(receiver: Receiver, stop: asyncio.Event, interval_s: float) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass
        if stop.is_set():
            break
        try:
            receiver.sources = await asyncio.to_thread(load_source_table)
        except Exception:  # noqa: BLE001 — banco fora: mantém a tabela anterior
            logger.warning("syslog: falha ao recarregar fontes — mantendo as %d atuais", len(receiver.sources), exc_info=True)


async def _amain() -> None:
    import redis.asyncio as redis_async

    from ..collectors import registry

    redis = redis_async.from_url(settings.REDIS_URL or "redis://localhost:6379/0", decode_responses=True,
                                 socket_timeout=5, socket_connect_timeout=5)
    try:
        sources = await asyncio.to_thread(load_source_table)
    except Exception:  # noqa: BLE001
        logger.error("syslog: não consegui ler as fontes no boot — subindo sem nenhuma (tudo será unknown_source até o banco voltar)", exc_info=True)
        sources = SourceTable([])
    receiver = Receiver(
        redis=redis, sources=sources, registry_has=registry.has,
        batch_max=settings.SYSLOG_BATCH_MAX, flush_ms=settings.SYSLOG_FLUSH_MS, max_line=settings.SYSLOG_MAX_LINE_BYTES,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover — Windows
            pass

    servers: List[Any] = []
    bind = settings.SYSLOG_BIND
    if settings.SYSLOG_UDP_PORT:
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _UdpProtocol(receiver, settings.SYSLOG_UDP_PORT), local_addr=(bind, settings.SYSLOG_UDP_PORT),
        )
        servers.append(transport)
        logger.info("syslog: UDP em %s:%d", bind, settings.SYSLOG_UDP_PORT)
    if settings.SYSLOG_TCP_PORT:
        srv = await asyncio.start_server(
            lambda r, w: _serve_stream(r, w, receiver=receiver, port=settings.SYSLOG_TCP_PORT, transport="tcp"),
            bind, settings.SYSLOG_TCP_PORT, limit=settings.SYSLOG_MAX_LINE_BYTES,
        )
        servers.append(srv)
        logger.info("syslog: TCP em %s:%d", bind, settings.SYSLOG_TCP_PORT)
    ctx = _tls_context()
    if settings.SYSLOG_TLS_PORT and ctx is not None:
        srv = await asyncio.start_server(
            lambda r, w: _serve_stream(r, w, receiver=receiver, port=settings.SYSLOG_TLS_PORT, transport="tls"),
            bind, settings.SYSLOG_TLS_PORT, ssl=ctx, limit=settings.SYSLOG_MAX_LINE_BYTES,
        )
        servers.append(srv)
        logger.info("syslog: TLS em %s:%d (mTLS=%s)", bind, settings.SYSLOG_TLS_PORT, bool(settings.SYSLOG_TLS_CA))
    elif settings.SYSLOG_TLS_PORT:
        logger.warning("syslog: SYSLOG_TLS_PORT definido sem SYSLOG_TLS_CERT/KEY — porta TLS NÃO aberta")
    logger.info("syslog: %d fonte(s) cadastrada(s); recarga a cada %ss", len(sources), settings.SYSLOG_SOURCE_REFRESH_S)

    tasks = [
        asyncio.create_task(receiver.run_flusher(stop)),
        asyncio.create_task(_refresh_sources(receiver, stop, settings.SYSLOG_SOURCE_REFRESH_S)),
    ]
    await stop.wait()
    logger.info("syslog: encerrando — drenando %d evento(s) em memória", receiver.pending())
    for s in servers:
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass
    await asyncio.gather(*tasks, return_exceptions=True)
    try:
        await redis.aclose()
    except Exception:  # noqa: BLE001
        pass


def _init_observability() -> None:
    import importlib

    for label, mod, fn in (("tracing", "tracing", "init_tracing"), ("metrics", "otel_metrics", "init_metrics"), ("logs", "otel_logs", "init_logs")):
        try:
            getattr(importlib.import_module(f"..collectors.{mod}", __package__), fn)()
        except Exception:  # pragma: no cover
            logger.warning("syslog: init OTel %s falhou (segue sem)", label, exc_info=True)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    _init_observability()
    logger.info("syslog: iniciando receptor")
    asyncio.run(_amain())


if __name__ == "__main__":  # pragma: no cover
    main()
