"""CrowdStrike Falcon collector — Alerts API v2 (combined/v1).

Cobre: paginação por cursor ``after`` (mesma URL, body diferente), cursor
incremental {created_after, after}, dedupe por ``composite_id``, base region-aware
(via _load_base_url patchado), e a normalização collect→OCSF via o mapping
registrado. Vendor 1-módulo zero-core (smoke de registro).
"""

from __future__ import annotations

import re
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import aiohttp
import pytest

from ._aiohttp_mock import aioresponses
from ..base import CollectorContext
from ..normalize import engine as E
from ..normalize.defaults import load_default_rules
from ..vendors.crowdstrike import CrowdStrikeDetectionsCollector

_COMBINED_RE = re.compile(r"^https://api\.crowdstrike\.com/alerts/combined/alerts/v1$")
_BASE = "https://api.crowdstrike.com"


class _NoopDomainLimiter:
    def slot(self, domain):
        class _Ctx:
            async def __aenter__(self_inner):
                return None

            async def __aexit__(self_inner, *a):
                return False

        return _Ctx()


class _NoopRateLimiter:
    async def acquire(self, tenant_id, vendor):
        return None

    async def backoff(self, vendor, retry_after):
        return None


def _ctx(session, cursor: Dict[str, Any] | None = None) -> CollectorContext:
    return CollectorContext(
        integration_id=55,
        organization_id=2,
        platform="crowdstrike",
        headers={"Authorization": "Bearer cs-token"},
        session=session,
        cursor=cursor,
        domain_limiter=_NoopDomainLimiter(),
        rate_limiter=_NoopRateLimiter(),
        redis=MagicMock(),
    )


def _alert(cid: str, ts: str, *, severity_name: str = "High"):
    return {
        "composite_id": cid,
        "created_timestamp": ts,
        "severity": 70,
        "severity_name": severity_name,
        "display_name": "SuspiciousActivity",
        "description": "masquerading behavior",
        "status": "new",
        "tactic": "Defense Evasion",
        "user_name": "jdoe",
        "device": {"device_id": "9f8a", "hostname": "WS-1", "local_ip": "10.0.0.9"},
    }


@pytest.mark.asyncio
async def test_paginates_via_after_and_persists_cursor() -> None:
    with aioresponses() as m:
        # página 1: traz cursor ``after`` → continua
        m.post(_COMBINED_RE, payload={
            "resources": [
                _alert("cid-1", "2026-06-20T10:00:00.000Z"),
                _alert("cid-2", "2026-06-20T10:01:00.000Z"),
            ],
            "meta": {"pagination": {"after": "tok-2"}},
        })
        # página 2: sem ``after`` → encerra
        m.post(_COMBINED_RE, payload={
            "resources": [_alert("cid-3", "2026-06-20T10:02:00.000Z")],
            "meta": {"pagination": {}},
        })
        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, cursor={"created_after": "2026-06-20T09:00:00.000Z"})
            with patch.object(CrowdStrikeDetectionsCollector, "_load_base_url", return_value=_BASE):
                collector = CrowdStrikeDetectionsCollector(ctx)
                collected = [ev async for ev in collector.collect()]

    assert [e["composite_id"] for e in collected] == ["cid-1", "cid-2", "cid-3"]
    # cursor avança para o maior created_timestamp; after zerado no fim do ciclo
    assert ctx.cursor == {"created_after": "2026-06-20T10:02:00.000Z", "after": None}
    assert collector.domain == "api.crowdstrike.com"
    # dedupe key = composite_id
    assert collector.extract_message_id(collected[0]) == "cid-1"


@pytest.mark.asyncio
async def test_empty_result_keeps_cursor() -> None:
    with aioresponses() as m:
        m.post(_COMBINED_RE, payload={"resources": [], "meta": {"pagination": {}}})
        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, cursor={"created_after": "2026-06-20T09:00:00.000Z"})
            with patch.object(CrowdStrikeDetectionsCollector, "_load_base_url", return_value=_BASE):
                collected = [ev async for ev in CrowdStrikeDetectionsCollector(ctx).collect()]
    assert collected == []
    assert ctx.cursor == {"created_after": "2026-06-20T09:00:00.000Z", "after": None}


@pytest.mark.asyncio
async def test_caps_pages_per_cycle_and_saves_resumable_cursor(monkeypatch) -> None:
    """Teto por ciclo (regressão do poison-loop de soft-timeout): com backlog MAIOR que o
    teto, ``collect()`` PARA após ``_MAX_PAGES_PER_CYCLE`` páginas em vez de drenar tudo num
    único run, e salva o cursor RESUMÍVEL — o token ``after`` da PRÓXIMA página com o MESMO
    ``created_after`` — p/ o próximo ciclo RETOMAR (sem pular). CRÍTICO: no cap-hit NÃO cai no
    cursor final {latest_seen, after:None} — isso avançaria o watermark ``created_after`` e
    descartaria o ``after``, jogando fora as páginas ainda não lidas. Sem o teto, um backlog
    grande estoura o soft-timeout (720s) → rollback total do cursor → loop sem progresso."""
    from ..vendors import crowdstrike as cs

    monkeypatch.setattr(cs, "_MAX_PAGES_PER_CYCLE", 3)

    # Cada página traz um cursor ``after`` NÃO-vazio → a paginação por cursor nunca vê
    # "página curta" e continuaria indefinidamente sem o teto. O teto corta em 3 páginas.
    def _page(n: int) -> Dict[str, Any]:
        return {
            "resources": [
                _alert(f"cid-{n}a", f"2026-06-20T10:{n:02d}:00.000Z"),
                _alert(f"cid-{n}b", f"2026-06-20T10:{n:02d}:30.000Z"),
            ],
            "meta": {"pagination": {"after": f"tok-{n}"}},
        }

    with aioresponses() as m:
        for n in range(1, 7):  # registra páginas de sobra; o teto corta em 3
            m.post(_COMBINED_RE, payload=_page(n))
        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, cursor={"created_after": "2026-06-20T09:00:00.000Z"})
            with patch.object(CrowdStrikeDetectionsCollector, "_load_base_url", return_value=_BASE):
                collected = [ev async for ev in CrowdStrikeDetectionsCollector(ctx).collect()]

    # PAROU no teto: 3 páginas × 2 alertas = 6 eventos (não drenou o backlog inteiro).
    assert len(collected) == 3 * 2
    # cursor RESUMÍVEL: ``created_after`` INALTERADO + ``after`` = token da PRÓXIMA página
    # (tok-3, retornado pela pág. 3 = token p/ buscar a pág. 4). NÃO é o cursor final
    # {latest_seen, after:None} → o próximo ciclo retoma exatamente daqui, sem perda.
    assert ctx.cursor == {"created_after": "2026-06-20T09:00:00.000Z", "after": "tok-3"}
    # a armadilha (perda de dados): created_after NÃO avançou p/ o latest_seen, after NÃO zerou.
    assert ctx.cursor["after"] is not None


@pytest.mark.asyncio
async def test_collect_then_normalizes_ocsf() -> None:
    with aioresponses() as m:
        m.post(_COMBINED_RE, payload={
            "resources": [_alert("cid-x", "2026-06-20T10:00:00.000Z", severity_name="Critical")],
            "meta": {"pagination": {}},
        })
        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, cursor=None)
            with patch.object(CrowdStrikeDetectionsCollector, "_load_base_url", return_value=_BASE):
                collected = [ev async for ev in CrowdStrikeDetectionsCollector(ctx).collect()]

    rules = load_default_rules("crowdstrike", "crowdstrike.detection")
    norm = E.apply_compiled(E.compile_rules(rules), collected[0]).output["normalized"]
    assert norm["class_uid"] == 2004
    assert norm["severity_id"] == 5  # Critical
    assert norm["finding_info"]["uid"] == "cid-x"
    assert norm["device"]["hostname"] == "WS-1"
    assert norm["time"]


def test_registered_zero_core_with_probe_and_store_secret() -> None:
    from ..registry import get, get_platform, has
    from ..capabilities import invalid_capabilities

    assert has("crowdstrike", "detections")
    reg = get("crowdstrike", "detections")
    assert reg.refresh_fn is not None and reg.queue == "collect.priority"
    plat = get_platform("crowdstrike")
    assert plat is not None and plat.test_fn is not None  # probe pré-save
    assert "client_secret" in {f.key for f in plat.auth_fields if f.type == "secret"}
    # capabilities no vocabulário canônico
    assert invalid_capabilities(plat.capabilities) == []
    assert "collect:detections" in plat.capabilities
