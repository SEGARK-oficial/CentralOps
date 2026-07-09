"""Testes do destino Generic Webhook (kind="webhook").

Cobre:
(a) ``format`` — envelope inteiro vs só ``normalized`` (config ``body``).
(b) ``_serialize`` — array JSON vs NDJSON (config ``wrap``).
(c) ``_auth_header`` — none / bearer / basic (base64).
(d) ``send_batch`` com sessão aiohttp mockada — 2xx ok, 401/403 auth,
    400 schema_rejected, 5xx retryable, erro de conexão retryable, vazio.
(e) ``test()`` — probe alcançável / 403 auth / conexão.
(f) Registry — kind registrado, build() resolve secret, metadados.

CRÍTICO: nenhuma conexão real. ``client._session`` é injetado (MagicMock) e o
``WebhookClient`` usa ``session.request(method, url, data=...)``.
"""

from __future__ import annotations

import base64
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.collectors.output.base import DeliveryResult
from backend.app.collectors.output.destinations import registry
from backend.app.collectors.output.destinations.registry import (
    DestinationConfig,
    compute_config_version,
)
from backend.app.collectors.output.destinations.webhook import WebhookClient


# ── Helpers de mock ──────────────────────────────────────────────────────


def _mock_response(status: int) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=None)
    return resp


def _mock_session(response: MagicMock) -> MagicMock:
    session = MagicMock()
    session.closed = False
    session.request = MagicMock(return_value=response)
    session.close = AsyncMock()
    return session


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def client() -> WebhookClient:
    return WebhookClient(url="https://hook.test.local/in", wrap="array", body="envelope")


@pytest.fixture
def sample_event() -> dict:
    return {
        "_centralops": {"event_id": "evt-abc123", "organization_id": 7},
        "normalized": {"class_uid": 2004, "message": "alerta"},
        "data": {"id": "evt-1"},
    }


# ── (a) format ───────────────────────────────────────────────────────────


def test_format_envelope_returns_full(client: WebhookClient, sample_event: dict) -> None:
    out = client.format(sample_event)
    assert out["data"] == {"id": "evt-1"}
    assert "_centralops" in out


def test_format_normalized_returns_only_ocsf(sample_event: dict) -> None:
    c = WebhookClient(url="https://h/in", body="normalized")
    out = c.format(sample_event)
    assert out == {"class_uid": 2004, "message": "alerta"}


# ── (b) serialize ────────────────────────────────────────────────────────


def test_serialize_array(client: WebhookClient, sample_event: dict) -> None:
    payload = client._serialize([sample_event, sample_event])
    parsed = json.loads(payload)
    assert isinstance(parsed, list) and len(parsed) == 2


def test_serialize_ndjson(sample_event: dict) -> None:
    c = WebhookClient(url="https://h/in", wrap="ndjson")
    payload = c._serialize([sample_event, sample_event])
    lines = payload.split("\n")
    assert len(lines) == 2
    for line in lines:
        json.loads(line)


# ── (c) auth header ──────────────────────────────────────────────────────


def test_auth_header_none() -> None:
    c = WebhookClient(url="https://h/in", auth_mode="none")
    assert c._auth_header() == {}


def test_auth_header_bearer() -> None:
    c = WebhookClient(url="https://h/in", auth_mode="bearer", secret="tok-xyz")
    assert c._auth_header() == {"Authorization": "Bearer tok-xyz"}


def test_auth_header_basic() -> None:
    c = WebhookClient(url="https://h/in", auth_mode="basic", secret="user:pass")
    expected = base64.b64encode(b"user:pass").decode("ascii")
    assert c._auth_header() == {"Authorization": f"Basic {expected}"}


# ── (d) send_batch ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_batch_2xx_ok(client: WebhookClient, sample_event: dict) -> None:
    client._session = _mock_session(_mock_response(200))
    result = await client.send_batch([sample_event])
    assert isinstance(result, DeliveryResult)
    assert result.accepted == 1
    assert result.all_accepted


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.asyncio
async def test_send_batch_auth(client: WebhookClient, sample_event: dict, status: int) -> None:
    client._session = _mock_session(_mock_response(status))
    result = await client.send_batch([sample_event])
    assert result.accepted == 0
    assert not result.retryable
    assert result.rejected[0].error_kind == "auth"
    assert result.rejected[0].event_id == "evt-abc123"


@pytest.mark.asyncio
async def test_send_batch_400_schema_rejected(client: WebhookClient, sample_event: dict) -> None:
    client._session = _mock_session(_mock_response(400))
    result = await client.send_batch([sample_event])
    assert result.rejected[0].error_kind == "schema_rejected"
    assert not result.retryable


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
@pytest.mark.asyncio
async def test_send_batch_retryable(client: WebhookClient, sample_event: dict, status: int) -> None:
    client._session = _mock_session(_mock_response(status))
    result = await client.send_batch([sample_event])
    assert result.retryable is True
    assert result.accepted == 0


@pytest.mark.asyncio
async def test_send_batch_connection_error_retryable(client: WebhookClient, sample_event: dict) -> None:
    import aiohttp

    session = MagicMock()
    session.closed = False
    session.request = MagicMock(side_effect=aiohttp.ClientConnectionError("recusado"))
    client._session = session
    result = await client.send_batch([sample_event])
    assert result.retryable is True


@pytest.mark.asyncio
async def test_send_batch_empty_ok_zero(client: WebhookClient) -> None:
    result = await client.send_batch([])
    assert result.accepted == 0 and result.all_accepted


# ── (e) test() ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_test_reachable_passed(client: WebhookClient) -> None:
    client._session = _mock_session(_mock_response(200))
    result = await client.test()
    assert result.ok is True


@pytest.mark.asyncio
async def test_test_403_failed(client: WebhookClient) -> None:
    client._session = _mock_session(_mock_response(403))
    result = await client.test()
    assert result.ok is False


@pytest.mark.asyncio
async def test_test_connection_error_failed(client: WebhookClient) -> None:
    import aiohttp

    session = MagicMock()
    session.closed = False
    session.request = MagicMock(side_effect=aiohttp.ClientConnectionError("sem rota"))
    client._session = session
    result = await client.test()
    assert result.ok is False


# ── (f) Registry ─────────────────────────────────────────────────────────


def test_webhook_registered() -> None:
    assert "webhook" in registry.all_kinds()


def test_webhook_build_resolves_secret() -> None:
    config = {"url": "https://h/in", "auth_mode": "bearer"}
    dest_config = DestinationConfig(
        destination_id="wh-1",
        kind="webhook",
        config=config,
        secret_ref="enc::abc",
        config_version=compute_config_version(config, {}),
    )
    secrets = MagicMock()
    secrets.decrypt.return_value = "tok-plain"
    dest = registry.build(dest_config, secrets=secrets)
    assert isinstance(dest, WebhookClient)
    assert dest._secret == "tok-plain"
    secrets.decrypt.assert_called_once_with("enc::abc")


def test_webhook_build_without_secret_dormant() -> None:
    config = {"url": "https://h/in"}
    dest_config = DestinationConfig(destination_id="wh-2", kind="webhook", config=config, secret_ref=None)
    dest = registry.build(dest_config, secrets=None)
    assert isinstance(dest, WebhookClient)
    assert dest._secret is None


def test_webhook_registration_metadata() -> None:
    reg = registry.get("webhook")
    assert reg.default_queue == "dispatch.webhook"
    assert "at_least_once" in reg.capabilities
    assert "tls" in reg.capabilities
    assert reg.required_secrets == ()
    assert reg.label == "Generic Webhook"


# ── close() ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_close_closes_session(client: WebhookClient) -> None:
    session = MagicMock()
    session.closed = False
    session.close = AsyncMock()
    client._session = session
    await client.close()
    session.close.assert_awaited_once()
    assert client._session is None
