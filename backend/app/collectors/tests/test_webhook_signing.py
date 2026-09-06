"""Webhook assinado e idempotente — o que um SOAR exige para confiar no lote.

Sem assinatura o receptor só tinha o Bearer; sem chave de idempotência o
retry após um 503 abria dois casos. Os dois cabeçalhos são POR REQUISIÇÃO
(mudam a cada corpo), não de sessão.

Nenhuma conexão real: ``client._session`` é injetado (MagicMock) e o cliente
usa ``session.request(method, url, data=..., headers=...)``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.collectors.output.destinations import registry
from backend.app.collectors.output.destinations.registry import DestinationConfig
from backend.app.collectors.output.destinations.webhook import (
    IDEMPOTENCY_HEADER,
    SIGNATURE_HEADER,
    WebhookClient,
    WebhookConfig,
    batch_idempotency_key,
    sign_payload,
    verify_signature,
)


def _mock_response(status: int) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=None)
    return resp


def _mock_session(status: int = 200) -> MagicMock:
    session = MagicMock()
    session.closed = False
    session.request = MagicMock(return_value=_mock_response(status))
    session.close = AsyncMock()
    return session


def _event(event_id: str) -> dict:
    return {"_centralops": {"event_id": event_id}, "normalized": {"class_uid": 2004}}


# ── primitivas ────────────────────────────────────────────────────────


def test_signature_is_hmac_sha256_over_timestamp_dot_body() -> None:
    header = sign_payload("s3cr3t", '[{"a":1}]', 1_700_000_000)
    expected = hmac.new(b"s3cr3t", b'1700000000.[{"a":1}]', hashlib.sha256).hexdigest()
    assert header == f"t=1700000000,v1={expected}"


def test_verify_accepts_fresh_and_rejects_replay_tamper_and_garbage() -> None:
    body = '[{"a":1}]'
    header = sign_payload("s3cr3t", body, 1_700_000_000)
    assert verify_signature("s3cr3t", body, header, now=1_700_000_010)
    # fora da janela = replay de um corpo legítimo capturado
    assert not verify_signature("s3cr3t", body, header, now=1_700_000_000 + 301)
    # corpo alterado
    assert not verify_signature("s3cr3t", '[{"a":2}]', header, now=1_700_000_010)
    # segredo errado
    assert not verify_signature("other", body, header, now=1_700_000_010)
    # lixo
    assert not verify_signature("s3cr3t", body, "nope", now=1_700_000_010)
    assert not verify_signature("s3cr3t", body, "t=abc,v1=00", now=1_700_000_010)


def test_idempotency_key_is_deterministic_and_order_sensitive() -> None:
    assert batch_idempotency_key(["a", "b"]) == batch_idempotency_key(["a", "b"])
    assert batch_idempotency_key(["a", "b"]) != batch_idempotency_key(["b", "a"])
    assert batch_idempotency_key(["a"]) == hashlib.sha256(b"a").hexdigest()


# ── no fio ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_batch_signs_the_exact_body_and_keys_the_batch() -> None:
    client = WebhookClient(url="https://h/in", secret="s3cr3t", signing="hmac_sha256")
    client._session = _mock_session(200)

    result = await client.send_batch([_event("e1"), _event("e2")])
    assert result.accepted == 2

    _, kwargs = client._session.request.call_args
    body = kwargs["data"]
    headers = kwargs["headers"]
    assert verify_signature("s3cr3t", body, headers[SIGNATURE_HEADER])
    assert headers[IDEMPOTENCY_HEADER] == batch_idempotency_key(["e1", "e2"])
    # A assinatura é sobre o corpo que FOI (array serializado), não sobre outra
    # forma do mesmo lote.
    assert json.loads(body)[0]["_centralops"]["event_id"] == "e1"


@pytest.mark.asyncio
async def test_retry_of_the_same_batch_repeats_the_key() -> None:
    client = WebhookClient(url="https://h/in", secret="s", signing="hmac_sha256")
    client._session = _mock_session(503)
    batch = [_event("e1")]
    await client.send_batch(batch)
    first = client._session.request.call_args.kwargs["headers"][IDEMPOTENCY_HEADER]
    await client.send_batch(batch)
    second = client._session.request.call_args.kwargs["headers"][IDEMPOTENCY_HEADER]
    assert first == second


@pytest.mark.asyncio
async def test_default_client_sends_key_but_no_signature() -> None:
    client = WebhookClient(url="https://h/in", secret="tok")
    client._session = _mock_session(200)
    await client.send_batch([_event("e1")])
    headers = client._session.request.call_args.kwargs["headers"]
    assert SIGNATURE_HEADER not in headers
    assert headers[IDEMPOTENCY_HEADER] == batch_idempotency_key(["e1"])


@pytest.mark.asyncio
async def test_everything_off_sends_no_per_request_headers() -> None:
    client = WebhookClient(url="https://h/in", idempotency_key=False)
    client._session = _mock_session(200)
    await client.send_batch([_event("e1")])
    assert client._session.request.call_args.kwargs["headers"] is None


@pytest.mark.asyncio
async def test_signing_without_secret_delivers_unsigned_and_warns_once(caplog) -> None:
    client = WebhookClient(url="https://h/in", signing="hmac_sha256")
    client._session = _mock_session(200)
    with caplog.at_level(logging.WARNING):
        await client.send_batch([_event("e1")])
        await client.send_batch([_event("e2")])
    headers = client._session.request.call_args.kwargs["headers"]
    assert SIGNATURE_HEADER not in headers
    assert sum("SEM assinatura" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.asyncio
async def test_probe_is_signed_and_fails_early_without_secret() -> None:
    signed = WebhookClient(url="https://h/in", secret="s", signing="hmac_sha256")
    signed._session = _mock_session(200)
    result = await signed.test()
    assert result.ok
    headers = signed._session.request.call_args.kwargs["headers"]
    assert verify_signature("s", "[]", headers[SIGNATURE_HEADER])

    unsigned = WebhookClient(url="https://h/in", signing="hmac_sha256")
    unsigned._session = _mock_session(200)
    result = await unsigned.test()
    assert not result.ok
    assert "sem credencial" in result.detail
    unsigned._session.request.assert_not_called()


# ── config e registry ─────────────────────────────────────────────────


def test_config_defaults_and_enum() -> None:
    cfg = WebhookConfig(url="https://h/in")
    assert cfg.signing == "none"
    assert cfg.idempotency_key is True
    with pytest.raises(ValueError):
        WebhookConfig(url="https://h/in", signing="md5")
    schema = WebhookConfig.model_json_schema()
    # Literal → enum no JSON Schema → Select na UI (o mesmo motivo de auth_mode).
    assert schema["properties"]["signing"]["enum"] == ["none", "hmac_sha256"]


def test_factory_wires_signing_from_config() -> None:
    secrets = MagicMock()
    secrets.decrypt.return_value = "shared"
    client = registry.build(
        DestinationConfig(
            destination_id="d1",
            kind="webhook",
            config={"url": "https://h/in", "signing": "hmac_sha256", "idempotency_key": False},
            secret_ref="enc",
        ),
        secrets,
    )
    assert client._signing == "hmac_sha256"
    assert client._idempotency is False
    assert client._secret == "shared"
    assert "signed" in registry.get("webhook").capabilities
