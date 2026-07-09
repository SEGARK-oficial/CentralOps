"""Testes do destino Datadog Logs Intake (kind="datadog").

Cobre:
(a) ``format`` — log entry com OCSF aninhado em ``ocsf`` + reservados ddsource/service/ddtags.
(b) URL derivada de ``site``.
(c) ``send_batch`` — 2xx ok, sem api_key → auth, 401/403 auth, 400 schema_rejected,
    5xx retryable, conexão retryable, vazio.
(d) ``test()`` — sem api_key failed, 2xx passed, 403 failed.
(e) Registry — registrado, build() resolve secret, metadados.

CRÍTICO: nenhuma conexão real. ``client._session`` é injetado (MagicMock).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.collectors.output.base import DeliveryResult
from backend.app.collectors.output.destinations import registry
from backend.app.collectors.output.destinations.registry import (
    DestinationConfig,
    compute_config_version,
)
from backend.app.collectors.output.destinations.datadog import DatadogClient


def _mock_response(status: int) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=None)
    return resp


def _mock_session(response: MagicMock) -> MagicMock:
    session = MagicMock()
    session.closed = False
    session.post = MagicMock(return_value=response)
    session.close = AsyncMock()
    return session


@pytest.fixture
def client() -> DatadogClient:
    return DatadogClient(site="datadoghq.com", service="centralops", ddsource="centralops",
                         tags="env:prod", api_key="dd-key-abc")


@pytest.fixture
def sample_event() -> dict:
    return {
        "_centralops": {"event_id": "evt-abc123", "organization_id": 7},
        "normalized": {"class_uid": 2004, "message": "alerta crítico"},
    }


# ── (a) format / (b) URL ─────────────────────────────────────────────────


def test_format_nests_ocsf_and_reserved(client: DatadogClient, sample_event: dict) -> None:
    entry = client.format(sample_event)
    assert entry["ddsource"] == "centralops"
    assert entry["service"] == "centralops"
    assert entry["ddtags"] == "env:prod"
    assert entry["message"] == "alerta crítico"
    assert entry["ocsf"] == {"class_uid": 2004, "message": "alerta crítico"}


def test_format_message_fallback_class_name() -> None:
    c = DatadogClient(api_key="k")
    entry = c.format({"normalized": {"class_name": "Detection Finding"}})
    assert entry["message"] == "Detection Finding"


def test_url_from_site() -> None:
    c = DatadogClient(site="datadoghq.eu", api_key="k")
    assert c._url == "https://http-intake.logs.datadoghq.eu/api/v2/logs"


# ── (c) send_batch ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_batch_2xx_ok(client: DatadogClient, sample_event: dict) -> None:
    client._session = _mock_session(_mock_response(202))
    result = await client.send_batch([sample_event])
    assert isinstance(result, DeliveryResult)
    assert result.accepted == 1 and result.all_accepted


@pytest.mark.asyncio
async def test_send_batch_no_api_key_auth() -> None:
    c = DatadogClient(api_key=None)
    result = await c.send_batch([{"_centralops": {"event_id": "e1"}}])
    assert result.accepted == 0 and not result.retryable
    assert result.rejected[0].error_kind == "auth"
    assert result.rejected[0].event_id == "e1"


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.asyncio
async def test_send_batch_auth(client: DatadogClient, sample_event: dict, status: int) -> None:
    client._session = _mock_session(_mock_response(status))
    result = await client.send_batch([sample_event])
    assert result.rejected[0].error_kind == "auth"
    assert not result.retryable


@pytest.mark.asyncio
async def test_send_batch_400_schema_rejected(client: DatadogClient, sample_event: dict) -> None:
    client._session = _mock_session(_mock_response(400))
    result = await client.send_batch([sample_event])
    assert result.rejected[0].error_kind == "schema_rejected"


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
@pytest.mark.asyncio
async def test_send_batch_retryable(client: DatadogClient, sample_event: dict, status: int) -> None:
    client._session = _mock_session(_mock_response(status))
    result = await client.send_batch([sample_event])
    assert result.retryable is True and result.accepted == 0


@pytest.mark.asyncio
async def test_send_batch_connection_error_retryable(client: DatadogClient, sample_event: dict) -> None:
    import aiohttp

    session = MagicMock()
    session.closed = False
    session.post = MagicMock(side_effect=aiohttp.ClientConnectionError("recusado"))
    client._session = session
    result = await client.send_batch([sample_event])
    assert result.retryable is True


@pytest.mark.asyncio
async def test_send_batch_payload_is_json_array(client: DatadogClient, sample_event: dict) -> None:
    client._session = _mock_session(_mock_response(202))
    await client.send_batch([sample_event, sample_event])
    call = client._session.post.call_args
    payload = call.kwargs.get("data") if call.kwargs.get("data") else call[1]["data"]
    parsed = json.loads(payload)
    assert isinstance(parsed, list) and len(parsed) == 2


@pytest.mark.asyncio
async def test_send_batch_empty_ok_zero(client: DatadogClient) -> None:
    result = await client.send_batch([])
    assert result.accepted == 0 and result.all_accepted


# ── (d) test() ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_test_no_api_key_failed() -> None:
    c = DatadogClient(api_key=None)
    result = await c.test()
    assert result.ok is False


@pytest.mark.asyncio
async def test_test_2xx_passed(client: DatadogClient) -> None:
    client._session = _mock_session(_mock_response(202))
    result = await client.test()
    assert result.ok is True


@pytest.mark.asyncio
async def test_test_403_failed(client: DatadogClient) -> None:
    client._session = _mock_session(_mock_response(403))
    result = await client.test()
    assert result.ok is False


# ── (e) Registry ─────────────────────────────────────────────────────────


def test_datadog_registered() -> None:
    assert "datadog" in registry.all_kinds()


def test_datadog_build_resolves_secret() -> None:
    config = {"site": "datadoghq.com", "service": "soc"}
    dest_config = DestinationConfig(
        destination_id="dd-1",
        kind="datadog",
        config=config,
        secret_ref="enc::abc",
        config_version=compute_config_version(config, {}),
    )
    secrets = MagicMock()
    secrets.decrypt.return_value = "dd-key-plain"
    dest = registry.build(dest_config, secrets=secrets)
    assert isinstance(dest, DatadogClient)
    assert dest._api_key == "dd-key-plain"


def test_datadog_build_without_secret_dormant() -> None:
    config = {"site": "datadoghq.com"}
    dest_config = DestinationConfig(destination_id="dd-2", kind="datadog", config=config, secret_ref=None)
    dest = registry.build(dest_config, secrets=None)
    assert isinstance(dest, DatadogClient)
    assert dest._api_key is None


def test_datadog_registration_metadata() -> None:
    reg = registry.get("datadog")
    assert reg.default_queue == "dispatch.datadog"
    assert "at_least_once" in reg.capabilities
    assert "api_key" in reg.required_secrets
    assert reg.label == "Datadog (Logs)"


@pytest.mark.asyncio
async def test_close_closes_session(client: DatadogClient) -> None:
    session = MagicMock()
    session.closed = False
    session.close = AsyncMock()
    client._session = session
    await client.close()
    session.close.assert_awaited_once()
    assert client._session is None
