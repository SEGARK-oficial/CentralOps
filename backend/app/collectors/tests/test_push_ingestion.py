"""Testes do subsistema de ingestão push: tokens de ingestão, coletor push e
seed de mapping OCSF. O evento push vai direto ao tópico raw (ADR-0018 Onda 2);
a lista Redis e o coletor que a drenava saíram na Onda 5.

Cobre:
(b) ingest_tokens: gera/parse/verifica + rotação invalida o anterior.
(c) PushSourceCollector: não busca nada; dá ``event_type`` e id de dedupe.
(e) catálogo: fortinet_fortigate/windows_event_log são transport=push e têm
    mapping default OCSF seedável.
(f) normalização do evento push com o mapping seedado.
(g) push não tem ciclo agendado.
"""

from __future__ import annotations

import pytest

from backend.app.collectors import registry as collector_registry
from backend.app.collectors.normalize.defaults import DEFAULT_MAPPING_FILES, load_default_rules


# ── (b) ingest_tokens ─────────────────────────────────────────────────────


def test_token_generate_parse_hash():
    from backend.app.services import ingest_tokens as it

    tok, digest = it.generate(123)
    assert tok.startswith("coi_123_")
    assert it.parse_integration_id(tok) == 123
    assert len(digest) == 64
    assert it.parse_integration_id("nope") is None
    assert it.parse_integration_id("coi_abc_x") is None


# ── (c) PushSourceCollector ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_push_collector_fetches_nothing():
    """Fonte push não tem o que buscar: o evento já está no raw."""
    from backend.app.collectors.vendors.push_ingest import FortiGateTrafficCollector

    coll = FortiGateTrafficCollector.__new__(FortiGateTrafficCollector)
    assert [e async for e in coll.collect()] == []
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


# ── (f) Normalização do evento push ────────────────────────────────────────


def test_push_event_normalizes_with_the_seeded_mapping():
    """O evento como o ingest o grava no raw → id de dedupe do carimbo → mapping
    seedado → OCSF válido. É o que o processor faz com ele."""
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
    coll = FortiGateTrafficCollector.__new__(FortiGateTrafficCollector)
    assert coll.extract_message_id(raw) == "evt-deadbeef"

    rules = load_default_rules("fortinet_fortigate", "fortinet_fortigate.traffic")
    out = apply_compiled(compile_rules(rules), raw).output
    n = out.get("normalized", out)
    assert n["class_uid"] == 4001  # OCSF Network Activity
    assert n["time"]
    assert n["src_endpoint"]["ip"] == "10.0.0.5"
    assert n["dst_endpoint"]["ip"] == "8.8.8.8"


def test_push_platform_has_no_scheduled_cycle():
    """Push não tem ciclo de coleta (ADR-0018 Onda 5): o registry marca a
    plataforma e o agendador não cria entry para ela."""
    for platform in ("fortinet_fortigate", "windows_event_log", "custom_json"):
        assert collector_registry.is_push_platform(platform), platform
    assert not collector_registry.is_push_platform("sophos")
