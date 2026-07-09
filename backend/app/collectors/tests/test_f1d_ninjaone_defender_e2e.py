"""retrofit ninjaone/defender end-to-end: collect → OCSF.

Os vendors genéricos viraram criáveis; collectors, refreshers (lendo do
store), filas/schedule e mappings OCSF já existem. Esta fatia VALIDA a ponta
que faltava: que o ciclo de coleta de ninjaone (activities) e defender
(incidents) realmente produz eventos, persiste cursor, e que o evento cru
NORMALIZA para OCSF via o mapping REGISTRADO (binding event_type → default).

NB: os mappings em si já têm contrato em test_normalize_contract.py; aqui amarramos
o output do COLLECTOR ao mapping (os dois collectors não tinham teste algum).
"""

from __future__ import annotations

import re
from typing import Any, Dict
from unittest.mock import MagicMock

import aiohttp
import pytest

from ._aiohttp_mock import aioresponses
from ..base import CollectorContext
from ..normalize import engine as E
from ..normalize.defaults import load_default_rules
from ..vendors.defender import DefenderIncidentsCollector
from ..vendors.ninjaone import NinjaOneActivitiesCollector

_NINJA_RE = re.compile(r"^https://app\.ninjarmm\.com/v2/activities(\?.*)?$")
_DEFENDER_RE = re.compile(r"^https://graph\.microsoft\.com/v1\.0/security/incidents(\?.*)?$")


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


def _ctx(session, platform: str, cursor: Dict[str, Any] | None = None) -> CollectorContext:
    return CollectorContext(
        integration_id=77,
        organization_id=3,
        platform=platform,
        headers={"Authorization": "Bearer test-token"},
        session=session,
        cursor=cursor,
        domain_limiter=_NoopDomainLimiter(),
        rate_limiter=_NoopRateLimiter(),
        redis=MagicMock(),
    )


def _to_ocsf(vendor: str, event_type: str, raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normaliza um evento cru via o mapping default REGISTRADO (binding real)."""
    rules = load_default_rules(vendor, event_type)
    res = E.apply_compiled(E.compile_rules(rules), raw)
    return res.output["normalized"]


# ── NinjaOne: collect → cursor → OCSF (activity, class_uid 6003) ────────


@pytest.mark.asyncio
async def test_ninjaone_collect_persists_cursor_and_normalizes_ocsf() -> None:
    activities = [
        {"id": 1001, "activityType": "ALERT", "activityTime": 1718960000,
         "severity": "warning", "user": {"id": "u1"}, "device": {"id": "d1", "name": "host-a"}},
        {"id": 1002, "activityType": "CONDITION", "activityTime": 1718960500,
         "severity": "info", "user": {"id": "u2"}, "device": {"id": "d2", "name": "host-b"}},
    ]
    with aioresponses() as m:
        m.get(_NINJA_RE, payload=activities)  # lista < pageSize → uma página
        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, "ninjaone", cursor=None)
            collected = [ev async for ev in NinjaOneActivitiesCollector(ctx).collect()]

    assert [e["id"] for e in collected] == [1001, 1002]
    # cursor avança pelo maior id visto (paginação incremental por after_id)
    assert ctx.cursor["after_id"] == 1002

    # evento cru normaliza para OCSF via o mapping REGISTRADO ninjaone.activity
    norm = _to_ocsf("ninjaone", "ninjaone.activity", collected[0])
    assert norm["class_uid"] == 6003
    assert norm["category_uid"] == 6
    # metadata.uid espelha o id nativo (int no NinjaOne — o mapping não força str)
    assert norm["metadata"]["uid"] == collected[0]["id"] == 1001


# ── Defender: collect → cursor → OCSF (incident, class_uid 2005) ────────


@pytest.mark.asyncio
async def test_defender_incidents_collect_persists_cursor_and_normalizes_ocsf() -> None:
    incidents = {
        "value": [
            {"id": "inc-1", "lastUpdateDateTime": "2024-06-21T10:00:00Z",
             "severity": "high", "displayName": "Suspicious sign-in", "status": "active"},
            {"id": "inc-2", "lastUpdateDateTime": "2024-06-21T11:00:00Z",
             "severity": "medium", "displayName": "Malware detected", "status": "active"},
        ]
        # sem @odata.nextLink → uma página
    }
    with aioresponses() as m:
        m.get(_DEFENDER_RE, payload=incidents)
        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, "microsoft_defender",
                       cursor={"lastUpdateDateTime": "2024-06-21T09:00:00Z"})
            collected = [ev async for ev in DefenderIncidentsCollector(ctx).collect()]

    assert [e["id"] for e in collected] == ["inc-1", "inc-2"]
    # cursor avança para o maior lastUpdateDateTime visto; nextLink zerado
    assert ctx.cursor["lastUpdateDateTime"] == "2024-06-21T11:00:00Z"
    assert ctx.cursor["@odata.nextLink"] is None

    # evento cru normaliza para OCSF via o mapping REGISTRADO defender.incident
    norm = _to_ocsf("microsoft_defender", "defender.incident", collected[0])
    assert norm["class_uid"] == 2005
    assert norm["category_uid"] == 2
    assert norm["finding_info"]["uid"] == "inc-1"
    assert norm["time"]  # lastUpdateDateTime → epoch (required)


# ── dedup key capta updates de incidente ──────


def test_defender_dedup_key_composes_last_update() -> None:
    """A dedup key compõe id + lastUpdateDateTime — um incidente que muda de
    estado (active→resolved) gera chave DISTINTA, não é descartado pela dedup 7d."""
    coll = DefenderIncidentsCollector.__new__(DefenderIncidentsCollector)
    active = {"id": "inc-9", "status": "active", "lastUpdateDateTime": "2024-06-21T10:00:00Z"}
    resolved = {"id": "inc-9", "status": "resolved", "lastUpdateDateTime": "2024-06-21T12:00:00Z"}

    k_active = coll.extract_message_id(active)
    k_resolved = coll.extract_message_id(resolved)

    assert k_active != k_resolved, "update de incidente deve produzir dedup key distinta"
    assert k_active == "inc-9@2024-06-21T10:00:00Z"
    # mesmo id + mesmo update (borda inclusiva do filtro `ge`) → MESMA key (dedup ok)
    assert coll.extract_message_id(dict(active)) == k_active
    # sem lastUpdateDateTime → fallback ao id cru (sem '@')
    assert coll.extract_message_id({"id": "inc-x"}) == "inc-x"


# ── Smoke: o registry expõe os 2 vendors como criáveis + coletáveis ────


def test_ninjaone_defender_registered_and_collectable() -> None:
    from ..registry import get, get_platform, has

    for platform, stream, dialect_probe in (
        ("ninjaone", "activities", True),
        ("microsoft_defender", "incidents", True),
    ):
        assert has(platform, stream), f"{platform}/{stream} não registrado"
        reg = get(platform, stream)
        assert reg.refresh_fn is not None and reg.queue and reg.task_name
        plat = get_platform(platform)
        assert plat is not None and plat.test_fn is not None  # probe pré-save (test-connection)
        # secret declarado p/ o store vendor-neutro — sem coluna legada
        assert "client_secret" in {f.key for f in plat.auth_fields if f.type == "secret"}
