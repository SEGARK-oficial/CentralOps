"""Receptor syslog (W3.2): fonte → integração, classificação → buffer, sem rede
externa. Sobe UDP e TCP em portas efêmeras com Redis falso e prova o contrato
com o pipeline: o evento chega ao MESMO buffer do POST /api/ingest, com o
carimbo ``_ingest`` (+ transporte e peer), e o que não tem dono é descartado
e contado — nunca aceito.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket

import fakeredis.aioredis
import pytest

from backend.app.collectors import registry
from backend.app.collectors.classify import compile_classifier
from backend.app.collectors.ingest_buffer import buffer_key
from backend.app.syslog import server as srv


def _sources(*specs):
    entries = []
    for cidr, port, transport, integ, platform, clf_cfg, default in specs:
        entries.append(srv._SourceEntry(
            network=ipaddress.ip_network(cidr, strict=False), listen_port=port, transport=transport,
            match=srv.SourceMatch(
                source_id=1, integration_id=integ, organization_id=1, platform=platform,
                classifier=compile_classifier(clf_cfg, default_stream=default), name="t",
            ),
        ))
    return srv.SourceTable(entries)


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def _receiver(redis, sources, *, has=None, batch_max=500, flush_ms=50):
    return srv.Receiver(redis=redis, sources=sources, registry_has=has or (lambda p, s: True),
                        batch_max=batch_max, flush_ms=flush_ms, max_line=64 * 1024)


# ── resolução de fonte ─────────────────────────────────────────────────────

def test_resolve_mais_especifico_vence_e_respeita_porta_e_transporte():
    t = _sources(
        ("10.0.0.0/8", None, "any", 1, "custom_json", None, "wide"),
        ("10.0.5.0/24", None, "any", 2, "custom_json", None, "narrow"),
        ("10.0.5.7/32", 1514, "tcp", 3, "custom_json", None, "exact"),
    )
    assert t.resolve("10.0.5.7", 1514, "tcp").integration_id == 3
    assert t.resolve("10.0.5.7", 514, "udp").integration_id == 2   # porta/transporte não batem no /32
    assert t.resolve("10.9.9.9", 514, "udp").integration_id == 1
    assert t.resolve("192.0.2.1", 514, "udp") is None
    assert t.resolve("not-an-ip", 514, "udp") is None
    assert t.resolve("2001:db8::1", 514, "udp") is None  # v6 não casa v4


# ── handle_line: aceita, descarta, classifica ──────────────────────────────

def test_fonte_desconhecida_e_descartada_e_contada(redis):
    r = _receiver(redis, _sources(("10.0.5.0/24", None, "any", 7, "custom_json", None, "s")))
    assert r.handle_line(b"<13>Sep  6 01:02:03 h app: x", peer_ip="203.0.113.9", port=514, transport="udp") == "unknown_source"
    assert r.pending() == 0 and r.stats == {"unknown_source": 1}


def test_stream_que_a_plataforma_nao_registra_e_unclassified(redis):
    r = _receiver(redis, _sources(("10.0.5.0/24", None, "any", 7, "custom_json", None, "sumiu")), has=lambda p, s: s != "sumiu")
    assert r.handle_line(b"<13>Sep  6 01:02:03 h app: x", peer_ip="10.0.5.7", port=514, transport="udp") == "unclassified"
    assert r.pending() == 0 and r.stats == {"unclassified": 1}


def test_evento_aceito_ganha_o_carimbo_do_ingest_e_vai_para_o_buffer_certo(redis):
    cfg = {"rules": [{"when": "@linux_auth", "stream": "auth"}]}
    r = _receiver(redis, _sources(("10.0.5.0/24", None, "any", 7, "custom_json", cfg, "other")))
    assert r.handle_line(b"<86>Sep  6 01:02:03 srv sshd[1]: Failed password for root from 203.0.113.9 port 1 ssh2",
                         peer_ip="10.0.5.7", port=514, transport="udp") == "accepted"
    assert r.handle_line(b"<13>Sep  6 01:02:03 srv cron[2]: job", peer_ip="10.0.5.7", port=514, transport="udp") == "accepted"
    assert r.pending() == 2
    n = asyncio.run(r.flush())
    assert n == 2 and r.pending() == 0

    async def _read(stream):
        return [json.loads(x) for x in await redis.lrange(buffer_key(7, stream), 0, -1)]
    auth, other = asyncio.run(_read("auth")), asyncio.run(_read("other"))
    assert len(auth) == 1 and len(other) == 1
    ev = auth[0]
    assert ev["format"] == "rfc3164" and ev["app"] == "sshd"
    assert ev["_ingest"]["stream"] == "auth" and ev["_ingest"]["transport"] == "udp" and ev["_ingest"]["peer"] == "10.0.5.7"
    assert len(ev["_ingest"]["id"]) == 64 and ev["_ingest"]["received_at"].endswith("Z")


def test_redis_fora_retem_em_memoria_com_teto(redis, monkeypatch):
    r = _receiver(redis, _sources(("10.0.5.0/24", None, "any", 7, "custom_json", None, "s")), batch_max=2)

    async def _boom(*a, **k):
        raise ConnectionError("redis down")
    monkeypatch.setattr("backend.app.collectors.ingest_buffer.push_events", _boom)
    for i in range(60):
        r.handle_line(f"<13>1 - h app - - - m{i}".encode(), peer_ip="10.0.5.7", port=514, transport="tcp")
    assert asyncio.run(r.flush()) == 0
    assert 0 < r.pending() <= 2 * 20  # retido, mas com teto (não vira OOM)
    assert r.stats.get("dropped_memory")


# ── ponta a ponta: UDP e TCP reais em porta efêmera ────────────────────────

def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def test_udp_e_tcp_de_verdade_incluindo_octet_count(redis):
    cfg = {"rules": [{"when": "@fortigate", "stream": "traffic"}]}
    r = _receiver(redis, _sources(("127.0.0.0/8", None, "any", 9, "fortinet_fortigate", cfg, "traffic")), flush_ms=30)

    async def _run():
        loop = asyncio.get_running_loop()
        uport, tport = _free_port(), _free_port()
        udp, _ = await loop.create_datagram_endpoint(lambda: srv._UdpProtocol(r, uport), local_addr=("127.0.0.1", uport))
        tcp = await asyncio.start_server(lambda rd, wr: srv._serve_stream(rd, wr, receiver=r, port=tport, transport="tcp"), "127.0.0.1", tport, limit=65536)
        stop = asyncio.Event()
        flusher = asyncio.create_task(r.run_flusher(stop))

        fg = b'<134>date=2026-09-06 time=01:02:03 devname="fw" devid="FG1" logid="0000000013" type="traffic" srcip=10.0.5.7'
        # UDP: um datagrama
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.sendto(fg, ("127.0.0.1", uport)); s.close()
        # TCP: newline-delimited + octet-count com \n DENTRO da mensagem
        rd, wr = await asyncio.open_connection("127.0.0.1", tport)
        msg2 = b"<13>1 2026-09-06T01:02:03Z h app - - - linha um\ncontinuacao"
        wr.write(fg + b"\n" + f"{len(msg2)} ".encode() + msg2 + b"\n")
        await wr.drain(); wr.close()
        for _ in range(50):
            await asyncio.sleep(0.05)
            depth_t = await redis.llen(buffer_key(9, "traffic"))
            if depth_t >= 3:
                break
        stop.set(); await flusher
        udp.close(); tcp.close(); await tcp.wait_closed()
        return [json.loads(x) for x in await redis.lrange(buffer_key(9, "traffic"), 0, -1)]

    events = asyncio.run(_run())
    assert len(events) == 3
    transports = sorted(e["_ingest"]["transport"] for e in events)
    assert transports == ["tcp", "tcp", "udp"]
    multi = next(e for e in events if e["format"] == "rfc5424")
    assert multi["msg"] == "linha um\ncontinuacao"  # octet-count preservou o \n interno
    assert r.stats["accepted"] == 3


# ── carga da tabela de fontes a partir do banco ────────────────────────────

def test_load_source_table_le_do_banco_e_ignora_fonte_invalida(monkeypatch, tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from backend.app.db import database as _db, models
    engine = create_engine(f"sqlite:///{tmp_path/'t.db'}", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Session = sessionmaker(bind=engine); _db.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(_db, "SessionLocal", Session)
    with Session() as db:
        org = models.Organization(name="o", slug="o"); db.add(org); db.flush()
        it = models.Integration(organization_id=org.id, name="fg", platform="fortinet_fortigate", is_active=True); db.add(it); db.flush()
        db.add(models.SyslogSource(organization_id=org.id, integration_id=it.id, name="ok", source_cidr="10.0.5.0/24", default_stream="traffic",
                                   classifier_json='{"rules":[{"when":"@fortigate","stream":"traffic"}]}'))
        db.add(models.SyslogSource(organization_id=org.id, integration_id=it.id, name="quebrada", source_cidr="10.0.6.0/24", default_stream="traffic",
                                   classifier_json='{"rules":[{"when":"msg ==","stream":"traffic"}]}'))
        db.add(models.SyslogSource(organization_id=org.id, integration_id=it.id, name="desligada", source_cidr="10.0.7.0/24", default_stream="traffic", enabled=False))
        db.commit()
    t = srv.load_source_table()
    assert len(t) == 1
    m = t.resolve("10.0.5.9", 514, "udp")
    assert m is not None and m.platform == "fortinet_fortigate" and m.classifier.default_stream == "traffic"
    assert t.resolve("10.0.6.9", 514, "udp") is None and t.resolve("10.0.7.9", 514, "udp") is None
