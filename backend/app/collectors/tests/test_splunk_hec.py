"""Testes do destino Splunk HEC (dormant).

Cobre:
(a) ``format_hec_event`` — wrapper correto, omissão de campos None.
(b) ``send_batch`` com sessão aiohttp mockada:
    - 200 code=0 → DeliveryResult.ok
    - 403 → rejected non-retryable, error_kind="auth"
    - 503 → retryable=True
(c) ``test()`` com 403 → TestResult.failed
(d) O kind está registrado e ``build()`` devolve um Destination com kind="splunk_hec".
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.collectors.output.splunk_hec_sender import (
    SplunkHecClient,
    format_hec_event,
)
from backend.app.collectors.output.base import DeliveryResult
from backend.app.collectors.output.destinations import registry
from backend.app.collectors.output.destinations.registry import (
    DestinationConfig,
    compute_config_version,
)


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def client() -> SplunkHecClient:
    """Cliente HEC com token e URL de teste."""
    return SplunkHecClient(
        url="https://splunk.test.local:8088",
        token="test-hec-token-abc123",
        index="centralops",
        sourcetype="centralops",
        source="centralops-collector",
        host="centralops-host",
        verify_tls=False,
    )


@pytest.fixture
def sample_event() -> dict:
    """Evento canônico mínimo com namespace _centralops."""
    return {
        "_centralops": {
            "vendor": "sophos",
            "integration_id": 1,
            "customer_id": 7,
            "stream": "alerts",
            "event_type": "sophos.alert",
            "event_id": "evt-abc123",
        },
        "data": {"id": "evt-1", "severity": "Critical"},
    }


def _mock_response(status: int, body: dict) -> MagicMock:
    """Cria um mock de aiohttp.ClientResponse para uso como context manager."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=body)
    # Suporta uso como async context manager (async with session.post(...) as resp).
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=None)
    return resp


def _mock_session(response: MagicMock) -> MagicMock:
    """Cria um mock de aiohttp.ClientSession com .post() retornando o response."""
    session = MagicMock()
    session.closed = False
    session.post = MagicMock(return_value=response)
    session.close = AsyncMock()
    return session


# ── (a) format_hec_event ─────────────────────────────────────────────────


def test_format_hec_event_wraps_under_event_key(sample_event: dict) -> None:
    wrapper = format_hec_event(sample_event, sourcetype="centralops")
    assert wrapper["event"] is sample_event
    assert wrapper["sourcetype"] == "centralops"


def test_format_hec_event_omits_none_index(sample_event: dict) -> None:
    wrapper = format_hec_event(sample_event, sourcetype="centralops", index=None)
    assert "index" not in wrapper


def test_format_hec_event_omits_none_source(sample_event: dict) -> None:
    wrapper = format_hec_event(sample_event, sourcetype="centralops", source=None)
    assert "source" not in wrapper


def test_format_hec_event_omits_none_host(sample_event: dict) -> None:
    wrapper = format_hec_event(sample_event, sourcetype="centralops", host=None)
    assert "host" not in wrapper


def test_format_hec_event_includes_non_none_fields(sample_event: dict) -> None:
    wrapper = format_hec_event(
        sample_event,
        sourcetype="centralops",
        index="my-index",
        source="my-source",
        host="my-host",
    )
    assert wrapper["index"] == "my-index"
    assert wrapper["source"] == "my-source"
    assert wrapper["host"] == "my-host"


def test_format_hec_event_does_not_set_time(sample_event: dict) -> None:
    """Campo 'time' nunca é definido — Splunk usa tempo de recepção."""
    wrapper = format_hec_event(sample_event, sourcetype="centralops")
    assert "time" not in wrapper


@pytest.mark.parametrize(
    "index,source,host,expected_keys",
    [
        (None, None, None, {"event", "sourcetype"}),
        ("idx", None, None, {"event", "sourcetype", "index"}),
        (None, "src", "h1", {"event", "sourcetype", "source", "host"}),
        ("idx", "src", "h1", {"event", "sourcetype", "index", "source", "host"}),
    ],
)
def test_format_hec_event_key_presence(
    index: Any, source: Any, host: Any, expected_keys: set
) -> None:
    """Testa presença de chaves opcionais (index/source/host) com evento SEM
    event_id, para isolar o comportamento de fields."""
    ev_no_id = {"data": {"id": "x"}}  # sem _centralops → sem fields
    wrapper = format_hec_event(
        ev_no_id, sourcetype="centralops", index=index, source=source, host=host
    )
    assert set(wrapper.keys()) == expected_keys


# ── (b) send_batch ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_batch_success_returns_ok(
    client: SplunkHecClient, sample_event: dict
) -> None:
    """HTTP 200 + code=0 → DeliveryResult.ok(1)."""
    resp = _mock_response(200, {"text": "Success", "code": 0})
    session = _mock_session(resp)
    client._session = session

    result = await client.send_batch([sample_event])

    assert isinstance(result, DeliveryResult)
    assert result.accepted == 1
    assert result.all_accepted
    assert not result.retryable


@pytest.mark.asyncio
async def test_send_batch_403_returns_rejected_non_retryable(
    client: SplunkHecClient, sample_event: dict
) -> None:
    """HTTP 403 → rejected com error_kind='auth', retryable=False."""
    resp = _mock_response(403, {"text": "Token disabled", "code": 4})
    session = _mock_session(resp)
    client._session = session

    result = await client.send_batch([sample_event])

    assert result.accepted == 0
    assert not result.retryable
    assert len(result.rejected) == 1
    rej = result.rejected[0]
    assert rej.error_kind == "auth"
    assert rej.retryable is False
    assert rej.event_id == "evt-abc123"


@pytest.mark.asyncio
async def test_send_batch_401_returns_rejected_auth(
    client: SplunkHecClient, sample_event: dict
) -> None:
    """HTTP 401 → rejected com error_kind='auth'."""
    resp = _mock_response(401, {"text": "Unauthorized", "code": 3})
    session = _mock_session(resp)
    client._session = session

    result = await client.send_batch([sample_event])

    assert result.accepted == 0
    assert result.rejected[0].error_kind == "auth"
    assert not result.retryable


@pytest.mark.asyncio
async def test_send_batch_400_returns_schema_rejected(
    client: SplunkHecClient, sample_event: dict
) -> None:
    """HTTP 400 → rejected com error_kind='schema_rejected'."""
    resp = _mock_response(400, {"text": "Invalid data format", "code": 6})
    session = _mock_session(resp)
    client._session = session

    result = await client.send_batch([sample_event])

    assert result.accepted == 0
    assert result.rejected[0].error_kind == "schema_rejected"
    assert not result.retryable


@pytest.mark.asyncio
async def test_send_batch_503_returns_retryable(
    client: SplunkHecClient, sample_event: dict
) -> None:
    """HTTP 503 → retryable=True (erro transitório de servidor)."""
    resp = _mock_response(503, {})
    session = _mock_session(resp)
    client._session = session

    result = await client.send_batch([sample_event])

    assert result.accepted == 0
    assert result.retryable is True
    assert not result.rejected


@pytest.mark.parametrize("status", [429, 502, 503, 504])
@pytest.mark.asyncio
async def test_send_batch_retryable_statuses(
    client: SplunkHecClient, sample_event: dict, status: int
) -> None:
    """Todos os status transitórios devem resultar em retryable=True."""
    resp = _mock_response(status, {})
    session = _mock_session(resp)
    client._session = session

    result = await client.send_batch([sample_event])

    assert result.retryable is True
    assert result.accepted == 0


@pytest.mark.asyncio
async def test_send_batch_connection_error_returns_retryable(
    client: SplunkHecClient, sample_event: dict
) -> None:
    """Erro de conexão aiohttp → retryable=True (sem raise)."""
    import aiohttp

    session = MagicMock()
    session.closed = False
    session.post = MagicMock(
        side_effect=aiohttp.ClientConnectionError("conexão recusada")
    )
    client._session = session

    result = await client.send_batch([sample_event])

    assert result.retryable is True
    assert result.accepted == 0


@pytest.mark.asyncio
async def test_send_batch_empty_returns_ok_zero(client: SplunkHecClient) -> None:
    """Lote vazio → DeliveryResult.ok(0) sem nenhuma chamada HTTP."""
    result = await client.send_batch([])
    assert result.accepted == 0
    assert result.all_accepted


@pytest.mark.asyncio
async def test_send_batch_payload_is_ndjson(
    client: SplunkHecClient, sample_event: dict
) -> None:
    """O payload enviado deve ser NDJSON (objetos JSON separados por \\n,
    NÃO um array JSON)."""
    resp = _mock_response(200, {"text": "Success", "code": 0})
    session = _mock_session(resp)
    client._session = session

    event2 = dict(sample_event)
    await client.send_batch([sample_event, event2])

    call_args = session.post.call_args
    payload: str = call_args[1]["data"] if "data" in call_args[1] else call_args.kwargs["data"]

    lines = payload.strip().split("\n")
    assert len(lines) == 2, f"esperado 2 linhas NDJSON, obteve {len(lines)}: {payload!r}"
    # Cada linha deve ser JSON válido.
    for line in lines:
        parsed = json.loads(line)
        assert "event" in parsed


@pytest.mark.asyncio
async def test_send_batch_rejected_event_id_fallback(client: SplunkHecClient) -> None:
    """Evento sem _centralops.event_id usa '?' como fallback."""
    event_no_id = {"data": "algo", "_centralops": {}}
    resp = _mock_response(403, {"text": "Forbidden", "code": 4})
    session = _mock_session(resp)
    client._session = session

    result = await client.send_batch([event_no_id])
    assert result.rejected[0].event_id == "?"


# ── (c) test() ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_test_403_returns_failed(client: SplunkHecClient) -> None:
    """probe com 403 → TestResult.failed com 'token HEC inválido'."""
    resp = _mock_response(403, {"text": "Token disabled", "code": 4})
    session = _mock_session(resp)
    client._session = session

    result = await client.test()

    assert result.ok is False
    assert "token HEC" in result.detail.lower() or "inválido" in result.detail.lower()


@pytest.mark.asyncio
async def test_test_200_returns_passed(client: SplunkHecClient) -> None:
    """probe com 200 code=0 → TestResult.passed."""
    resp = _mock_response(200, {"text": "Success", "code": 0})
    session = _mock_session(resp)
    client._session = session

    result = await client.test()

    assert result.ok is True


@pytest.mark.asyncio
async def test_test_connection_error_returns_failed(client: SplunkHecClient) -> None:
    """Erro de conexão → TestResult.failed (nunca levanta exceção)."""
    import aiohttp

    session = MagicMock()
    session.closed = False
    session.post = MagicMock(
        side_effect=aiohttp.ClientConnectionError("sem rota para o host")
    )
    client._session = session

    result = await client.test()

    assert result.ok is False
    assert "conexão" in result.detail.lower() or "erro" in result.detail.lower()


# ── (d) Registry ─────────────────────────────────────────────────────────


def test_splunk_hec_registered() -> None:
    """O kind 'splunk_hec' deve estar no registry."""
    assert "splunk_hec" in registry.all_kinds()


def test_splunk_hec_build_returns_destination_with_correct_kind() -> None:
    """``build()`` deve devolver um objeto com kind='splunk_hec'."""
    config = {"url": "https://splunk.test.local:8088", "sourcetype": "centralops"}
    dest_config = DestinationConfig(
        destination_id="test-splunk-registry",
        kind="splunk_hec",
        config=config,
        config_version=compute_config_version(config, {}),
    )
    dest = registry.build(dest_config)
    assert dest.kind == "splunk_hec"
    assert isinstance(dest, SplunkHecClient)


def test_splunk_hec_build_without_secret_has_none_token() -> None:
    """Sem secret_ref/secrets, token=None (dormant fail-closed)."""
    config = {"url": "https://splunk.test.local:8088"}
    dest_config = DestinationConfig(
        destination_id="dormant-splunk",
        kind="splunk_hec",
        config=config,
        secret_ref=None,
    )
    dest = registry.build(dest_config, secrets=None)
    assert isinstance(dest, SplunkHecClient)
    assert dest._token is None


def test_splunk_hec_build_with_secret_resolves_token() -> None:
    """Com secrets e secret_ref, o token é decifrado."""
    config = {"url": "https://splunk.test.local:8088"}
    dest_config = DestinationConfig(
        destination_id="secret-splunk",
        kind="splunk_hec",
        config=config,
        secret_ref="enc::abc",
    )
    mock_secrets = MagicMock()
    mock_secrets.decrypt.return_value = "plain-token-xyz"

    dest = registry.build(dest_config, secrets=mock_secrets)

    assert isinstance(dest, SplunkHecClient)
    assert dest._token == "plain-token-xyz"
    mock_secrets.decrypt.assert_called_once_with("enc::abc")


def test_splunk_hec_registration_metadata() -> None:
    """Verifica metadados do registro no catálogo.

    "idempotent" foi removido — o HEC não tem dedup
    nativo no sender. A capability correta é "at_least_once".
    """
    reg = registry.get("splunk_hec")
    assert reg.default_queue == "dispatch.splunk_hec"
    # "at_least_once" é a capability honesta: reentrega pode duplicar no Splunk.
    assert "at_least_once" in reg.capabilities
    assert "idempotent" not in reg.capabilities
    assert "tls" in reg.capabilities
    assert "hec_token" in reg.required_secrets
    assert reg.label == "Splunk HEC"


# ── close() ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_close_closes_session(client: SplunkHecClient) -> None:
    session = MagicMock()
    session.closed = False
    session.close = AsyncMock()
    client._session = session

    await client.close()

    session.close.assert_awaited_once()
    assert client._session is None


@pytest.mark.asyncio
async def test_close_noop_when_no_session(client: SplunkHecClient) -> None:
    client._session = None
    await client.close()  # não deve levantar


@pytest.mark.asyncio
async def test_close_noop_when_session_already_closed(client: SplunkHecClient) -> None:
    session = MagicMock()
    session.closed = True
    client._session = session
    await client.close()  # não deve levantar nem chamar close()
    session.close.assert_not_called()
