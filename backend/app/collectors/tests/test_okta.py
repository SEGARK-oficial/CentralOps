"""Okta System Log collector — paginação por Link header.

Cobre: SSWS auth (via _load_conn patchado), paginação seguindo ``Link; rel=next``,
cursor = URL do next link, parada no array vazio (modo polling), dedupe por uuid,
collect→OCSF. Vendor 1-módulo zero-core.
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
from ..vendors.okta import OktaSystemLogCollector, _next_link

_LOGS_RE = re.compile(r"^https://acme\.okta\.com/api/v1/logs")
_CONN = {"base_url": "https://acme.okta.com", "token": "ssws-tok"}


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
        integration_id=91,
        organization_id=4,
        platform="okta",
        headers={},
        session=session,
        cursor=cursor,
        domain_limiter=_NoopDomainLimiter(),
        rate_limiter=_NoopRateLimiter(),
        redis=MagicMock(),
    )


def _event(uuid: str, etype: str = "user.session.start", result: str = "SUCCESS"):
    return {
        "uuid": uuid,
        "published": "2026-06-20T21:05:32.000Z",
        "eventType": etype,
        "severity": "INFO",
        "displayMessage": "User login to Okta",
        "outcome": {"result": result},
        "actor": {"id": "00u", "type": "User", "alternateId": "admin@okta.com", "displayName": "Admin"},
        "client": {"ipAddress": "142.126.158.61"},
    }


def test_next_link_parser():
    class _H:
        def __init__(self, v):
            self._v = v

        def get(self, k, d=None):
            return self._v if k == "Link" else d

    link = '<https://acme.okta.com/api/v1/logs?after=tok2>; rel="next"'
    assert _next_link(_H(link)) == "https://acme.okta.com/api/v1/logs?after=tok2"
    assert _next_link(_H('<https://x>; rel="self"')) is None


@pytest.mark.asyncio
async def test_paginates_via_link_header_and_stops_on_empty() -> None:
    next1 = "https://acme.okta.com/api/v1/logs?after=tok2&sortOrder=ASCENDING&limit=200"
    next2 = "https://acme.okta.com/api/v1/logs?after=tok3&sortOrder=ASCENDING&limit=200"
    with aioresponses() as m:
        # página 1: 2 eventos + Link next
        m.get(_LOGS_RE, payload=[_event("u1"), _event("u2", result="FAILURE")],
              headers={"Link": f'<{next1}>; rel="next"'})
        # página 2: vazia (polling) + Link next → encerra retomando deste ponto
        m.get(_LOGS_RE, payload=[], headers={"Link": f'<{next2}>; rel="next"'})
        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, cursor=None)
            with patch.object(OktaSystemLogCollector, "_load_conn", return_value=dict(_CONN)):
                collector = OktaSystemLogCollector(ctx)
                collected = [ev async for ev in collector.collect()]

    assert [e["uuid"] for e in collected] == ["u1", "u2"]
    # cursor = URL do next link (retomada incremental)
    assert ctx.cursor == {"next_url": next2}
    assert collector.domain == "acme.okta.com"
    assert collector.extract_message_id(collected[0]) == "u1"

    norm = E.apply_compiled(
        E.compile_rules(load_default_rules("okta", "okta.system_log")), collected[0]
    ).output["normalized"]
    assert norm["class_uid"] == 3002
    assert norm["user"]["name"] == "admin@okta.com"
    assert norm["status_id"] == 1  # SUCCESS
    assert norm["metadata"]["uid"] == "u1"


def test_registered_zero_core_with_ssws_probe() -> None:
    from ..registry import get, get_platform, has
    from ..capabilities import invalid_capabilities

    assert has("okta", "system_log")
    plat = get_platform("okta")
    assert plat is not None and plat.test_fn is not None
    assert invalid_capabilities(plat.capabilities) == []
    # secret = api_token (SSWS) no store; refresher no-op (não OAuth)
    assert "api_token" in {f.key for f in plat.auth_fields if f.type == "secret"}
    assert get("okta", "system_log").refresh_fn.__name__ == "_okta_refresher"
