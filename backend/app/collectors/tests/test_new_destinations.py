"""Testes dos destinos novos (ClickHouse, CrowdStrike LogScale, NG-SIEM) e do
contrato de metadado de catálogo self-describing do registry de destinos.

Cobre:
(a) Os 3 kinds novos se auto-registram e o catálogo expõe icon_id/category/
    description/tier/order (simetria com /providers/platforms).
(b) Factory constrói o cliente com e sem secret (dormant).
(c) ClickHouse: endpoint INSERT … JSONEachRow; send_batch 200/400/503; test().
(d) LogScale/NG-SIEM (HEC Bearer): send_batch 200/401/503; test() 401.
(e) Nenhum send_batch levanta exceção — sempre devolve DeliveryResult.
"""

from __future__ import annotations

from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.collectors.output.base import DeliveryResult, TestResult
from backend.app.collectors.output.clickhouse_sender import ClickHouseClient
from backend.app.collectors.output.logscale_sender import LogScaleHecClient, format_hec_event
from backend.app.collectors.output.destinations import registry
from backend.app.collectors.output.destinations.registry import DestinationConfig


# ── Helpers de mock aiohttp ──────────────────────────────────────────────


def _mock_response(status: int, *, text: str = "", json_body: Optional[dict] = None) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=text)
    resp.json = AsyncMock(return_value=(json_body or {}))
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=None)
    return resp


def _mock_session(response: MagicMock) -> MagicMock:
    session = MagicMock()
    session.closed = False
    session.post = MagicMock(return_value=response)
    session.get = MagicMock(return_value=response)
    session.close = AsyncMock()
    return session


@pytest.fixture
def sample_event() -> dict:
    return {
        "_centralops": {
            "vendor": "fortinet_fortigate",
            "integration_id": 3,
            "customer_id": 7,
            "stream": "traffic",
            "event_type": "fortigate.traffic",
            "event_id": "evt-xyz",
        },
        "data": {"action": "deny", "srcip": "10.0.0.5"},
    }


# ── (a) Catálogo self-describing ─────────────────────────────────────────

NEW_KINDS = ("clickhouse", "crowdstrike_logscale", "crowdstrike_ngsiem")


def test_new_kinds_registered() -> None:
    for k in NEW_KINDS:
        assert registry.has(k), f"kind {k} não registrado"


def test_catalog_exposes_self_describing_metadata() -> None:
    """Todo destino do catálogo declara os campos de catálogo (sem None em campos
    obrigatórios) — garante simetria com ProviderPlatformRead."""
    catalog = {d["kind"]: d for d in registry.describe_all()}
    for kind, d in catalog.items():
        assert isinstance(d["category"], str) and d["category"], f"{kind} sem category"
        assert isinstance(d["tier"], str) and d["tier"] in {"stable", "beta", "generic"}, f"{kind} tier inválido"
        assert isinstance(d["order"], int), f"{kind} order não-int"
        assert "icon_id" in d and "description" in d and "docs_url" in d
    # os novos têm ícone de marca e descrição preenchida
    for k in NEW_KINDS:
        assert catalog[k]["icon_id"], f"{k} sem icon_id"
        assert catalog[k]["description"], f"{k} sem description"


def test_catalog_sorted_by_order() -> None:
    orders = [d["order"] for d in registry.describe_all()]
    assert orders == sorted(orders)


# ── (b) Factory build (com / sem secret) ─────────────────────────────────


class _FakeSecrets:
    def __init__(self, value: str) -> None:
        self._value = value

    def decrypt(self, ref: str) -> str:
        return self._value


def test_clickhouse_factory_with_and_without_secret() -> None:
    cfg = DestinationConfig(destination_id="d1", kind="clickhouse", config={"url": "https://ch:8443", "table": "t"}, secret_ref="ref")
    client = registry.get("clickhouse").factory(cfg, _FakeSecrets("pw"))
    assert isinstance(client, ClickHouseClient)
    # dormant: sem secrets backend → password None, ainda constrói
    cfg2 = DestinationConfig(destination_id="d1", kind="clickhouse", config={"url": "https://ch:8443"}, secret_ref=None)
    assert isinstance(registry.get("clickhouse").factory(cfg2, None), ClickHouseClient)


def test_logscale_and_ngsiem_factory_build() -> None:
    for k in ("crowdstrike_logscale", "crowdstrike_ngsiem"):
        cfg = DestinationConfig(destination_id="d", kind=k, config={"endpoint": "https://hec.test/api/v1/ingest/hec"}, secret_ref="ref")
        client = registry.get(k).factory(cfg, _FakeSecrets("tok"))
        assert isinstance(client, LogScaleHecClient)
        assert client.kind == k


# ── (c) ClickHouse client ────────────────────────────────────────────────


def test_clickhouse_endpoint_quotes_identifiers() -> None:
    c = ClickHouseClient(url="https://ch:8443", password=None, database="db", table="events")
    ep = c._endpoint()
    assert "INSERT+INTO" in ep or "INSERT%20INTO" in ep
    assert "JSONEachRow" in ep
    assert "input_format_skip_unknown_fields=1" in ep


@pytest.mark.asyncio
async def test_clickhouse_send_batch_paths(sample_event: dict) -> None:
    c = ClickHouseClient(url="https://ch:8443", password="pw", table="events", verify_tls=False)

    c._session = _mock_session(_mock_response(200))
    res = await c.send_batch([sample_event])
    assert isinstance(res, DeliveryResult) and res.accepted == 1 and res.all_accepted

    c._session = _mock_session(_mock_response(400, text="Cannot parse input"))
    res = await c.send_batch([sample_event])
    assert res.accepted == 0 and res.rejected and res.rejected[0].error_kind == "schema_rejected"
    assert res.rejected[0].retryable is False

    c._session = _mock_session(_mock_response(503))
    res = await c.send_batch([sample_event])
    assert res.accepted == 0 and res.retryable is True

    c._session = _mock_session(_mock_response(403, text="auth"))
    res = await c.send_batch([sample_event])
    assert res.rejected and res.rejected[0].error_kind == "auth"


@pytest.mark.asyncio
async def test_clickhouse_test_probe() -> None:
    c = ClickHouseClient(url="https://ch:8443", password="pw")
    c._session = _mock_session(_mock_response(200, text=""))
    assert (await c.test()).ok is True
    c._session = _mock_session(_mock_response(401, text="bad"))
    assert (await c.test()).ok is False


@pytest.mark.asyncio
async def test_clickhouse_empty_batch_noop() -> None:
    c = ClickHouseClient(url="https://ch:8443", password="pw")
    res = await c.send_batch([])
    assert res.accepted == 0 and res.all_accepted


# ── (d) LogScale / NG-SIEM HEC client ────────────────────────────────────


def test_format_hec_event_bearer_family(sample_event: dict) -> None:
    wrapper = format_hec_event(sample_event, sourcetype="centralops")
    assert wrapper["event"] == sample_event
    assert wrapper["sourcetype"] == "centralops"
    assert wrapper["fields"]["_centralops_event_id"] == "evt-xyz"


@pytest.mark.asyncio
async def test_logscale_send_batch_paths(sample_event: dict) -> None:
    c = LogScaleHecClient(endpoint="https://hec.test/api/v1/ingest/hec", token="tok", kind="crowdstrike_logscale", verify_tls=False)

    c._session = _mock_session(_mock_response(200))
    res = await c.send_batch([sample_event])
    assert res.accepted == 1 and res.all_accepted

    c._session = _mock_session(_mock_response(503))
    res = await c.send_batch([sample_event])
    assert res.retryable is True

    # 401 dispara fallback individual → rejected auth non-retryable
    c._session = _mock_session(_mock_response(401))
    res = await c.send_batch([sample_event])
    assert res.rejected and res.rejected[0].error_kind == "auth" and res.rejected[0].retryable is False


@pytest.mark.asyncio
async def test_sinks_never_raise_on_serialization_failure() -> None:
    """Contrato 'nunca levanta': um envelope com referência circular (json.dumps
    ValueError) vira DeliveryResult rejected schema_rejected, não exceção."""
    circular: dict = {"_centralops": {"event_id": "x"}}
    circular["loop"] = circular  # referência circular → json.dumps levanta

    ch = ClickHouseClient(url="https://ch:8443", password="pw", table="t")
    ch._session = _mock_session(_mock_response(200))
    res = await ch.send_batch([circular])
    assert res.accepted == 0 and res.rejected and res.rejected[0].error_kind == "schema_rejected"

    ls = LogScaleHecClient(endpoint="https://hec.test/api/v1/ingest/hec", token="t", kind="crowdstrike_logscale")
    ls._session = _mock_session(_mock_response(200))
    res = await ls.send_batch([circular])
    assert res.accepted == 0 and res.rejected and res.rejected[0].error_kind == "schema_rejected"


@pytest.mark.asyncio
async def test_logscale_test_probe_auth_fail() -> None:
    c = LogScaleHecClient(endpoint="https://hec.test/api/v1/ingest/hec", token="bad", kind="crowdstrike_ngsiem")
    c._session = _mock_session(_mock_response(403))
    assert (await c.test()).ok is False


@pytest.mark.asyncio
async def test_bearer_header_set() -> None:
    c = LogScaleHecClient(endpoint="https://hec.test/api/v1/ingest/hec", token="tok123", kind="crowdstrike_logscale", verify_tls=False)
    sess = c._get_session()
    try:
        assert sess._default_headers.get("Authorization") == "Bearer tok123"
    finally:
        await c.close()
