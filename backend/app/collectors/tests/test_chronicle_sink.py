"""Testes do destino Google SecOps / Chronicle (kind="chronicle").

Cobre:
(a) ``format`` — log entry com ``data`` = base64 do OCSF ``normalized`` (JSON).
(b) URL ``logs:import`` derivada de project/instance/region/location/log_type.
(c) ``send_batch`` — 2xx ok, 401/403 auth, 400 schema_rejected, 5xx retryable,
    token ausente → auth, SDK ausente (RuntimeError) → unknown, conexão retryable.
(d) ``test()`` — 2xx passed, 403 failed, token inválido failed.
(e) Registry — registrado, build() resolve SA JSON, metadados.

CRÍTICO: nenhum ``google-auth`` importado. O seam ``_load_token`` é sobrescrito
para devolver um token fake; ``client._session`` é injetado (MagicMock).
"""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.collectors.output.base import DeliveryResult
from backend.app.collectors.output.destinations import registry
from backend.app.collectors.output.destinations.registry import (
    DestinationConfig,
    compute_config_version,
)
from backend.app.collectors.output.destinations.chronicle import ChronicleClient


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
def client(monkeypatch: pytest.MonkeyPatch) -> ChronicleClient:
    c = ChronicleClient(project="proj-1", instance="inst-guid", region="us",
                        location="us", log_type="UDM", sa_json='{"fake":"sa"}')
    monkeypatch.setattr(c, "_load_token", lambda: "fake-token-xyz")
    return c


@pytest.fixture
def sample_event() -> dict:
    return {
        "_centralops": {"event_id": "evt-abc123", "organization_id": 7},
        "normalized": {"class_uid": 2004, "message": "alerta"},
    }


# ── (a) format / (b) URL ─────────────────────────────────────────────────


def test_format_base64_of_normalized(client: ChronicleClient, sample_event: dict) -> None:
    entry = client.format(sample_event)
    raw = base64.b64decode(entry["data"]).decode("utf-8")
    assert json.loads(raw) == {"class_uid": 2004, "message": "alerta"}


def test_url_logs_import() -> None:
    c = ChronicleClient(project="p1", instance="i1", region="europe", location="europe", log_type="OKTA")
    assert c._url == (
        "https://europe-chronicle.googleapis.com/v1alpha"
        "/projects/p1/locations/europe/instances/i1/logTypes/OKTA/logs:import"
    )


# ── (c) send_batch ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_batch_2xx_ok(client: ChronicleClient, sample_event: dict) -> None:
    client._session = _mock_session(_mock_response(200))
    result = await client.send_batch([sample_event])
    assert isinstance(result, DeliveryResult)
    assert result.accepted == 1 and result.all_accepted


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.asyncio
async def test_send_batch_auth(client: ChronicleClient, sample_event: dict, status: int) -> None:
    client._session = _mock_session(_mock_response(status))
    result = await client.send_batch([sample_event])
    assert result.rejected[0].error_kind == "auth"
    assert not result.retryable


@pytest.mark.asyncio
async def test_send_batch_400_schema_rejected(client: ChronicleClient, sample_event: dict) -> None:
    client._session = _mock_session(_mock_response(400))
    result = await client.send_batch([sample_event])
    assert result.rejected[0].error_kind == "schema_rejected"


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
@pytest.mark.asyncio
async def test_send_batch_retryable(client: ChronicleClient, sample_event: dict, status: int) -> None:
    client._session = _mock_session(_mock_response(status))
    result = await client.send_batch([sample_event])
    assert result.retryable is True and result.accepted == 0


@pytest.mark.asyncio
async def test_send_batch_token_error_auth(monkeypatch: pytest.MonkeyPatch, sample_event: dict) -> None:
    c = ChronicleClient(project="p", instance="i", sa_json="invalid")

    def _boom() -> str:
        raise ValueError("SA inválido")

    monkeypatch.setattr(c, "_load_token", _boom)
    result = await c.send_batch([sample_event])
    assert result.accepted == 0 and not result.retryable
    assert result.rejected[0].error_kind == "auth"


@pytest.mark.asyncio
async def test_send_batch_sdk_missing_unknown(monkeypatch: pytest.MonkeyPatch, sample_event: dict) -> None:
    c = ChronicleClient(project="p", instance="i", sa_json="x")

    def _boom() -> str:
        raise RuntimeError("google-auth não instalado")

    monkeypatch.setattr(c, "_load_token", _boom)
    result = await c.send_batch([sample_event])
    assert result.accepted == 0 and not result.retryable
    assert result.rejected[0].error_kind == "unknown"


@pytest.mark.asyncio
async def test_send_batch_connection_error_retryable(client: ChronicleClient, sample_event: dict) -> None:
    import aiohttp

    session = MagicMock()
    session.closed = False
    session.post = MagicMock(side_effect=aiohttp.ClientConnectionError("recusado"))
    client._session = session
    result = await client.send_batch([sample_event])
    assert result.retryable is True


@pytest.mark.asyncio
async def test_send_batch_body_inline_source(client: ChronicleClient, sample_event: dict) -> None:
    client._session = _mock_session(_mock_response(200))
    await client.send_batch([sample_event])
    call = client._session.post.call_args
    body = call.kwargs["json"]
    assert "inline_source" in body
    assert len(body["inline_source"]["logs"]) == 1
    assert "data" in body["inline_source"]["logs"][0]


@pytest.mark.asyncio
async def test_send_batch_empty_ok_zero(client: ChronicleClient) -> None:
    result = await client.send_batch([])
    assert result.accepted == 0 and result.all_accepted


# ── (d) test() ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_test_2xx_passed(client: ChronicleClient) -> None:
    client._session = _mock_session(_mock_response(200))
    result = await client.test()
    assert result.ok is True


@pytest.mark.asyncio
async def test_test_403_failed(client: ChronicleClient) -> None:
    client._session = _mock_session(_mock_response(403))
    result = await client.test()
    assert result.ok is False


@pytest.mark.asyncio
async def test_test_token_error_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    c = ChronicleClient(project="p", instance="i", sa_json="invalid")

    def _boom() -> str:
        raise ValueError("SA inválido")

    monkeypatch.setattr(c, "_load_token", _boom)
    result = await c.test()
    assert result.ok is False


# ── (e) Registry ─────────────────────────────────────────────────────────


def test_chronicle_registered() -> None:
    assert "chronicle" in registry.all_kinds()


def test_chronicle_build_resolves_secret() -> None:
    config = {"project": "p1", "instance": "i1", "region": "us"}
    dest_config = DestinationConfig(
        destination_id="ch-1",
        kind="chronicle",
        config=config,
        secret_ref="enc::abc",
        config_version=compute_config_version(config, {}),
    )
    secrets = MagicMock()
    secrets.decrypt.return_value = '{"type":"service_account"}'
    dest = registry.build(dest_config, secrets=secrets)
    assert isinstance(dest, ChronicleClient)
    assert dest._sa_json == '{"type":"service_account"}'


def test_chronicle_build_without_secret_dormant() -> None:
    config = {"project": "p1", "instance": "i1"}
    dest_config = DestinationConfig(destination_id="ch-2", kind="chronicle", config=config, secret_ref=None)
    dest = registry.build(dest_config, secrets=None)
    assert isinstance(dest, ChronicleClient)
    assert dest._sa_json is None


def test_chronicle_registration_metadata() -> None:
    reg = registry.get("chronicle")
    assert reg.default_queue == "dispatch.chronicle"
    assert "at_least_once" in reg.capabilities
    assert "service_account_json" in reg.required_secrets
    assert reg.label == "Google SecOps (Chronicle)"


@pytest.mark.asyncio
async def test_close_closes_session(client: ChronicleClient) -> None:
    session = MagicMock()
    session.closed = False
    session.close = AsyncMock()
    client._session = session
    await client.close()
    session.close.assert_awaited_once()
    assert client._session is None
