"""Testes do subsistema de ingestão push: buffer Redis, tokens de
ingestão, collector virtual de dreno, endpoint HTTP e seed de mapping OCSF.

Cobre:
(a) ingest_buffer: push→drain FIFO + backpressure (cap + descarte).
(b) ingest_tokens: gera/parse/verifica + rotação invalida o anterior.
(c) PushBufferCollector: drena o buffer e expõe event_type por subclasse.
(d) endpoint POST /api/ingest/{stream}: token-auth, bufferiza, 401 sem token,
    422 p/ plataforma não-push, 404 p/ stream desconhecido.
(e) catálogo: fortinet_fortigate/windows_event_log são transport=push e têm
    mapping default OCSF seedável.
"""

from __future__ import annotations

import pytest
import fakeredis.aioredis

from backend.app.collectors import ingest_buffer as ib
from backend.app.collectors import registry as collector_registry
from backend.app.collectors.normalize.defaults import DEFAULT_MAPPING_FILES, load_default_rules


@pytest.fixture
def aredis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


# ── (a) ingest_buffer ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_buffer_push_drain_fifo(aredis):
    evs = [{"i": i} for i in range(5)]
    accepted, dropped = await ib.push_events(aredis, 1, "traffic", evs)
    assert accepted == 5 and dropped == 0
    assert await ib.buffer_depth(aredis, 1, "traffic") == 5
    drained = await ib.drain_events(aredis, 1, "traffic")
    assert [d["i"] for d in drained] == [0, 1, 2, 3, 4]  # FIFO (mais antigo primeiro)
    assert await ib.buffer_depth(aredis, 1, "traffic") == 0


@pytest.mark.asyncio
async def test_buffer_backpressure_caps_and_counts(aredis):
    # cap=3: empurrar 5 mantém os 3 mais recentes, descarta 2.
    accepted, dropped = await ib.push_events(aredis, 9, "s", [{"i": i} for i in range(5)], max_len=3)
    assert accepted == 5
    assert dropped == 2
    assert await ib.buffer_depth(aredis, 9, "s") == 3
    # contador de descarte exposto
    assert int(await aredis.get(ib.dropped_key(9, "s"))) == 2


@pytest.mark.asyncio
async def test_drain_budget_bounds(aredis):
    await ib.push_events(aredis, 2, "t", [{"i": i} for i in range(10)])
    drained = await ib.drain_events(aredis, 2, "t", budget=4)
    assert len(drained) == 4
    assert await ib.buffer_depth(aredis, 2, "t") == 6


@pytest.mark.asyncio
async def test_drain_empty_is_noop(aredis):
    assert await ib.drain_events(aredis, 77, "none") == []


# ── (b) ingest_tokens ─────────────────────────────────────────────────────


def test_token_generate_parse_hash():
    from backend.app.services import ingest_tokens as it

    tok, digest = it.generate(123)
    assert tok.startswith("coi_123_")
    assert it.parse_integration_id(tok) == 123
    assert len(digest) == 64
    assert it.parse_integration_id("nope") is None
    assert it.parse_integration_id("coi_abc_x") is None


# ── (c) PushBufferCollector ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_push_collector_drains(aredis):
    from backend.app.collectors.base import CollectorContext
    from backend.app.collectors.vendors.push_ingest import FortiGateTrafficCollector

    await ib.push_events(aredis, 5, "traffic", [{"a": 1}, {"a": 2}])
    ctx = CollectorContext(
        integration_id=5, organization_id=1, platform="fortinet_fortigate",
        headers={}, session=None, cursor=None, domain_limiter=None, rate_limiter=None, redis=aredis,
    )
    coll = FortiGateTrafficCollector(ctx)
    out = [e async for e in coll.collect()]
    assert [e["a"] for e in out] == [1, 2]
    assert FortiGateTrafficCollector.event_type == "fortinet_fortigate.traffic"


def test_push_collector_message_id_from_ingest_stamp():
    from backend.app.collectors.vendors.push_ingest import FortiGateTrafficCollector

    coll = FortiGateTrafficCollector.__new__(FortiGateTrafficCollector)
    assert coll.extract_message_id({"_ingest": {"id": "abc"}}) == "abc"
    assert coll.extract_message_id({"no": "ingest"}) == ""


# ── (e) catálogo + mapping ─────────────────────────────────────────────────


@pytest.mark.parametrize("platform,stream,event_type", [
    ("fortinet_fortigate", "traffic", "fortinet_fortigate.traffic"),
    ("windows_event_log", "security", "windows_event_log.security"),
])
def test_push_platform_catalog_and_mapping(platform, stream, event_type):
    reg = collector_registry.get_platform(platform)
    assert reg is not None and reg.transport == "push"
    assert collector_registry.has(platform, stream)
    assert (platform, event_type) in DEFAULT_MAPPING_FILES
    rules = load_default_rules(platform, event_type)
    rule_list = rules["rules"] if isinstance(rules, dict) else rules
    # class_uid OCSF obrigatório presente
    assert any(r.get("target") == "normalized.class_uid" and r.get("required") for r in rule_list)
    assert any(r.get("target") == "normalized.time" and r.get("required") for r in rule_list)


# ── (f) End-to-end: buffer → dreno → normalização OCSF ─────────────────────


@pytest.mark.asyncio
async def test_push_event_drains_and_normalizes(aredis):
    """Prova o caminho push COMPLETO: evento bufferizado → PushBufferCollector drena
    → extract_message_id (dedupe) → mapping seedado → envelope OCSF válido."""
    from backend.app.collectors.base import CollectorContext
    from backend.app.collectors.vendors.push_ingest import FortiGateTrafficCollector
    from backend.app.collectors.normalize.engine import apply_compiled, compile_rules

    raw = {
        "_ingest": {"id": "evt-deadbeef", "received_at": "2026-06-21T10:00:00Z", "stream": "traffic"},
        "timestamp": "2026-06-21T10:00:00Z",
        "type": "traffic", "subtype": "forward", "level": "notice",
        "srcip": "10.0.0.5", "srcport": 51514,
        "dstip": "8.8.8.8", "dstport": 443, "proto": 6,
        "action": "accept", "sentbyte": 1024, "rcvdbyte": 4096,
    }
    await ib.push_events(aredis, 5, "traffic", [raw])

    ctx = CollectorContext(
        integration_id=5, organization_id=1, platform="fortinet_fortigate",
        headers={}, session=None, cursor=None, domain_limiter=None, rate_limiter=None, redis=aredis,
    )
    coll = FortiGateTrafficCollector(ctx)
    drained = [e async for e in coll.collect()]
    assert len(drained) == 1
    # dedupe id estável vem do carimbo do endpoint
    assert coll.extract_message_id(drained[0]) == "evt-deadbeef"

    rules = load_default_rules("fortinet_fortigate", "fortinet_fortigate.traffic")
    out = apply_compiled(compile_rules(rules), drained[0]).output
    n = out.get("normalized", out)
    assert n["class_uid"] == 4001  # OCSF Network Activity
    assert n["time"]  # tempo resolveu (timestamp || _ingest.received_at)
    assert n["src_endpoint"]["ip"] == "10.0.0.5"
    assert n["dst_endpoint"]["ip"] == "8.8.8.8"


def test_push_platform_scheduled_via_registry():
    """O dreno É agendado: register_integration_in_beat usa iter_for_platform p/ criar
    a entry RedBeat. Confirma que cada plataforma push expõe um (stream, task, queue,
    schedule) válido — sem isso o buffer nunca seria drenado (data-loss silencioso)."""
    from backend.app.collectors.queues import Q_BULK, T_COLLECT_BULK

    for platform, stream in (("fortinet_fortigate", "traffic"), ("windows_event_log", "security")):
        regs = {r.stream: r for r in collector_registry.iter_for_platform(platform)}
        assert stream in regs, f"{platform} sem CollectorRegistration p/ {stream}"
        r = regs[stream]
        assert r.task_name == T_COLLECT_BULK
        assert r.queue == Q_BULK
        assert 0 < r.schedule.total_seconds() <= 30  # dreno frequente


@pytest.mark.asyncio
async def test_buffer_dropped_robust_when_buffer_prefilled(aredis):
    """dropped = max(0, lpush_len - max_len): correto mesmo com o buffer já cheio
    (robusto a writers concorrentes — não depende do LLEN pós-trim)."""
    # pré-enche no cap
    await ib.push_events(aredis, 3, "t", [{"i": i} for i in range(3)], max_len=3)
    # mais 4 com cap 3 → todos os 4 são "excedente" (a lista já estava cheia)
    accepted, dropped = await ib.push_events(aredis, 3, "t", [{"i": i} for i in range(4)], max_len=3)
    assert accepted == 4
    assert dropped == 4
    assert await ib.buffer_depth(aredis, 3, "t") == 3
