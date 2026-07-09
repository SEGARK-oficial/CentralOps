"""WazuhDetectionsCollector — pull do Indexer (OpenSearch DSL).

Cobre: basic auth montado do store (via _load_conn patchado), paginação
from/size, atualização de cursor {from_ts}, dedupe id (injeta _id quando o
alerta não traz ``id`` nativo), e o domain derivado do indexer_url.
"""

from __future__ import annotations

import re
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import aiohttp
import pytest

from ._aiohttp_mock import aioresponses
from ..base import CollectorContext
from ..vendors.wazuh_detections import WazuhDetectionsCollector

_SEARCH_RE = re.compile(r"^https://indexer\.example:9200/wazuh-alerts-.*/_search$")

_FAKE_CONN = {
    "base_url": "https://indexer.example:9200",
    "username": "wazuh-ro",
    "password": "s3cr3t",
    "verify_ssl": False,
}


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


def _ctx(session: aiohttp.ClientSession, cursor: Dict[str, Any] | None = None) -> CollectorContext:
    return CollectorContext(
        integration_id=42,
        organization_id=7,
        platform="wazuh",
        headers={},  # ignorado pelo collector (basic auth próprio)
        session=session,
        cursor=cursor,
        domain_limiter=_NoopDomainLimiter(),
        rate_limiter=_NoopRateLimiter(),
        redis=MagicMock(),
    )


def _hit(doc_id: str, ts: str, *, native_id: str | None = None, level: int = 7):
    src: Dict[str, Any] = {
        "timestamp": ts,
        "rule": {"level": level, "description": "x"},
        "agent": {"id": "001", "name": "host-a"},
    }
    if native_id is not None:
        src["id"] = native_id
    return {"_id": doc_id, "_source": src}


@pytest.mark.asyncio
async def test_collects_single_page_and_updates_cursor() -> None:
    with aioresponses() as m:
        m.post(
            _SEARCH_RE,
            payload={
                "hits": {
                    "hits": [
                        _hit("doc1", "2024-06-21T10:00:00.000+0000", native_id="a1"),
                        # sem id nativo → o collector injeta o _id do doc p/ dedupe
                        _hit("doc2", "2024-06-21T10:05:00.000+0000"),
                    ]
                }
            },
        )
        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, cursor={"from_ts": "2024-06-21T09:00:00.000+0000"})
            with patch.object(WazuhDetectionsCollector, "_load_conn", return_value=dict(_FAKE_CONN)):
                collector = WazuhDetectionsCollector(ctx)
                collected = [ev async for ev in collector.collect()]

    assert [e["id"] for e in collected] == ["a1", "doc2"]  # _id injetado no 2º
    assert collector.extract_message_id(collected[1]) == "doc2"
    # cursor avança para o timestamp mais recente visto.
    assert ctx.cursor == {"from_ts": "2024-06-21T10:05:00.000+0000"}
    # domain derivado do indexer_url (semáforo por host).
    assert collector.domain == "indexer.example"


@pytest.mark.asyncio
async def test_empty_result_keeps_lookback_cursor() -> None:
    with aioresponses() as m:
        m.post(_SEARCH_RE, payload={"hits": {"hits": []}})
        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, cursor={"from_ts": "2024-06-21T09:00:00.000+0000"})
            with patch.object(WazuhDetectionsCollector, "_load_conn", return_value=dict(_FAKE_CONN)):
                collected = [ev async for ev in WazuhDetectionsCollector(ctx).collect()]

    assert collected == []
    # sem hits: nada a avançar (cursor permanece no from_ts inicial).
    assert ctx.cursor == {"from_ts": "2024-06-21T09:00:00.000+0000"}


@pytest.mark.asyncio
async def test_sends_basic_auth_header() -> None:
    captured: Dict[str, Any] = {}

    with aioresponses() as m:
        m.post(_SEARCH_RE, payload={"hits": {"hits": []}})
        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session)
            with patch.object(WazuhDetectionsCollector, "_load_conn", return_value=dict(_FAKE_CONN)):
                async for _ in WazuhDetectionsCollector(ctx).collect():
                    pass
            # o mock registra as chamadas em m.requests[(method, url)]
            for (method, _url), calls in m.requests.items():
                if method == "POST":
                    captured = calls[0].kwargs
                    break

    import base64
    expected = "Basic " + base64.b64encode(b"wazuh-ro:s3cr3t").decode()
    assert captured["headers"]["Authorization"] == expected
    # verify_ssl=False ⇒ ssl desabilitado no request.
    assert captured["ssl"] is False
