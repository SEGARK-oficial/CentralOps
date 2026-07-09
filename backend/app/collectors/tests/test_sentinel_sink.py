"""Testes do destino Microsoft Sentinel — Logs Ingestion API / DCR.

Cobre o fluxo de duas pernas (OAuth2 client-credentials → ingestão no stream do
DCR) com o mock HTTP interno ``_aiohttp_mock`` (substituto do ``aioresponses``):

(a) ``format`` — item do array JSON é o envelope.
(b) ``send_batch`` feliz: token adquirido 1x e **reusado** no 2º batch (cache).
(c) ``401`` → rejected ``auth`` (não-retryable); cache invalidado.
(d) ``429`` → retryable=True.
(e) ``400`` → rejected ``schema_rejected`` (não-retryable).
(f) ``test()`` passed (token ok) / failed (credencial inválida).
(g) Registry: kind registrado, ``build()`` resolve secret, metadados.

NÃO faz chamada de rede real: o ``_aiohttp_mock`` faz monkeypatch de
``aiohttp.ClientSession._request``. O cliente cria a sessão lazily dentro dos
métodos async, então todas as requisições são interceptadas.
"""

from __future__ import annotations

import asyncio
import json
import re
from unittest.mock import MagicMock

import pytest

from ._aiohttp_mock import aioresponses
from ..output.base import DeliveryResult
from ..output.destinations import registry
from ..output.destinations.registry import DestinationConfig, compute_config_version
from ..output.destinations.sentinel import SentinelClient

# Endpoint de token AAD (client-credentials) — match exato por URL.
_TENANT_ID = "11111111-2222-3333-4444-555555555555"
_TOKEN_URL = f"https://login.microsoftonline.com/{_TENANT_ID}/oauth2/v2.0/token"
# Endpoint de ingestão (tem query ?api-version=...) — match por regex.
_INGEST_RE = re.compile(
    r"^https://dce\.ingest\.monitor\.azure\.com/dataCollectionRules/"
    r"dcr-abc123/streams/Custom-CentralOps_CL(\?.*)?$"
)


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def client() -> SentinelClient:
    """Cliente Sentinel com endpoint/DCR/credencial de teste."""
    return SentinelClient(
        dce_endpoint="https://dce.ingest.monitor.azure.com",
        dcr_immutable_id="dcr-abc123",
        stream_name="Custom-CentralOps_CL",
        tenant_id=_TENANT_ID,
        client_id="app-client-id",
        client_secret="super-secret-value",
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
            "event_id": "evt-abc123",
        },
        "data": {"id": "evt-1", "severity": "Critical"},
    }


def _token_payload(expires_in: int = 3600) -> dict:
    return {
        "token_type": "Bearer",
        "expires_in": expires_in,
        "access_token": "aad-access-token-xyz",
    }


# ── (a) format ───────────────────────────────────────────────────────────


def test_format_returns_envelope_dict(client: SentinelClient, sample_event: dict) -> None:
    item = client.format(sample_event)
    assert item == sample_event
    # Cópia defensiva: format() não deve devolver a MESMA referência.
    assert item is not sample_event


# ── (b) send_batch feliz + cache de token ────────────────────────────────


@pytest.mark.asyncio
async def test_send_batch_success_returns_ok(
    client: SentinelClient, sample_event: dict
) -> None:
    """204 na ingestão → DeliveryResult.ok(len)."""
    with aioresponses() as m:
        m.post(_TOKEN_URL, payload=_token_payload())
        m.post(_INGEST_RE, status=204, repeat=True)

        result = await client.send_batch([sample_event])

    assert isinstance(result, DeliveryResult)
    assert result.accepted == 1
    assert result.all_accepted
    assert not result.retryable


@pytest.mark.asyncio
async def test_send_batch_200_also_ok(client: SentinelClient, sample_event: dict) -> None:
    """200 (além de 204) também conta como lote aceito."""
    with aioresponses() as m:
        m.post(_TOKEN_URL, payload=_token_payload())
        m.post(_INGEST_RE, status=200, repeat=True)

        result = await client.send_batch([sample_event])

    assert result.accepted == 1
    assert result.all_accepted


@pytest.mark.asyncio
async def test_token_acquired_once_and_reused_across_batches(
    client: SentinelClient, sample_event: dict
) -> None:
    """O token é obtido 1x e **reusado** no 2º batch (cache até expires_in-60s).

    O endpoint de token é registrado SEM ``repeat`` (consumido uma única vez):
    uma 2ª aquisição falharia no mock por falta de registro. A ingestão é
    ``repeat=True`` para aceitar os dois batches.
    """
    with aioresponses() as m:
        m.post(_TOKEN_URL, payload=_token_payload(expires_in=3600))
        m.post(_INGEST_RE, status=204, repeat=True)

        r1 = await client.send_batch([sample_event])
        r2 = await client.send_batch([sample_event])

    assert r1.accepted == 1 and r2.accepted == 1

    # Exatamente UMA chamada ao endpoint de token (reuso via cache).
    token_calls = [
        calls for (method, url), calls in m.requests.items()
        if method == "POST" and "oauth2/v2.0/token" in str(url)
    ]
    flat = [c for sub in token_calls for c in sub]
    assert len(flat) == 1, f"esperado 1 POST de token, obteve {len(flat)}"

    # E duas chamadas de ingestão (uma por batch).
    ingest_calls = [
        calls for (method, url), calls in m.requests.items()
        if method == "POST" and "dataCollectionRules" in str(url)
    ]
    flat_ingest = [c for sub in ingest_calls for c in sub]
    assert len(flat_ingest) == 2, f"esperado 2 POSTs de ingestão, obteve {len(flat_ingest)}"


@pytest.mark.asyncio
async def test_send_batch_posts_json_array_with_bearer(
    client: SentinelClient, sample_event: dict
) -> None:
    """O corpo da ingestão é um array JSON e o header carrega o Bearer token."""
    with aioresponses() as m:
        m.post(_TOKEN_URL, payload=_token_payload())
        m.post(_INGEST_RE, status=204, repeat=True)

        await client.send_batch([sample_event, dict(sample_event)])

    # Localiza a chamada de ingestão e inspeciona body/headers.
    ingest_call = None
    for (method, url), calls in m.requests.items():
        if method == "POST" and "dataCollectionRules" in str(url):
            ingest_call = calls[0]
            break
    assert ingest_call is not None

    body = ingest_call.kwargs["data"]
    parsed = json.loads(body)
    assert isinstance(parsed, list)
    assert len(parsed) == 2

    headers = ingest_call.kwargs.get("headers") or {}
    assert headers.get("Authorization") == "Bearer aad-access-token-xyz"
    assert headers.get("Content-Type") == "application/json"


@pytest.mark.asyncio
async def test_send_batch_empty_returns_ok_zero_no_token(client: SentinelClient) -> None:
    """Lote vazio → ok(0) sem sequer adquirir token (sem chamada HTTP)."""
    with aioresponses() as m:
        # Nenhum mock registrado: qualquer POST levantaria ClientConnectionError.
        result = await client.send_batch([])
        assert not m.requests  # nada foi à rede

    assert result.accepted == 0
    assert result.all_accepted


# ── (c) 401 → auth ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_batch_401_returns_rejected_auth(
    client: SentinelClient, sample_event: dict
) -> None:
    """401 na ingestão → rejected error_kind='auth', não-retryable; cache zerado."""
    with aioresponses() as m:
        m.post(_TOKEN_URL, payload=_token_payload(), repeat=True)
        m.post(_INGEST_RE, status=401, repeat=True)

        result = await client.send_batch([sample_event])

    assert result.accepted == 0
    assert not result.retryable
    assert len(result.rejected) == 1
    rej = result.rejected[0]
    assert rej.error_kind == "auth"
    assert rej.retryable is False
    assert rej.event_id == "evt-abc123"
    # Cache invalidado (próxima tentativa readquire token).
    assert client._token is None


@pytest.mark.asyncio
async def test_send_batch_403_returns_rejected_auth(
    client: SentinelClient, sample_event: dict
) -> None:
    """403 (sem permissão Monitoring Metrics Publisher) → rejected auth."""
    with aioresponses() as m:
        m.post(_TOKEN_URL, payload=_token_payload(), repeat=True)
        m.post(_INGEST_RE, status=403, repeat=True)

        result = await client.send_batch([sample_event])

    assert result.accepted == 0
    assert not result.retryable
    assert result.rejected[0].error_kind == "auth"


# ── (d) 429/5xx → retryable ──────────────────────────────────────────────


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
@pytest.mark.asyncio
async def test_send_batch_retryable_statuses(
    client: SentinelClient, sample_event: dict, status: int
) -> None:
    """429 e 5xx → retryable=True, nada rejeitado."""
    with aioresponses() as m:
        m.post(_TOKEN_URL, payload=_token_payload(), repeat=True)
        m.post(_INGEST_RE, status=status, repeat=True)

        result = await client.send_batch([sample_event])

    assert result.accepted == 0
    assert result.retryable is True
    assert not result.rejected


# ── (e) 400 → schema_rejected ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_batch_400_returns_schema_rejected(
    client: SentinelClient, sample_event: dict
) -> None:
    """400 → rejected error_kind='schema_rejected', não-retryable."""
    with aioresponses() as m:
        m.post(_TOKEN_URL, payload=_token_payload(), repeat=True)
        m.post(_INGEST_RE, status=400, body="InvalidStream: stream not found", repeat=True)

        result = await client.send_batch([sample_event])

    assert result.accepted == 0
    assert not result.retryable
    assert len(result.rejected) == 1
    assert result.rejected[0].error_kind == "schema_rejected"


# ── token failures ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_batch_token_401_rejects_auth(
    client: SentinelClient, sample_event: dict
) -> None:
    """Falha de credencial no endpoint de token → rejected auth (não-retryable)."""
    with aioresponses() as m:
        m.post(
            _TOKEN_URL,
            status=401,
            payload={"error": "invalid_client", "error_description": "AADSTS7000215"},
            repeat=True,
        )

        result = await client.send_batch([sample_event])

    assert result.accepted == 0
    assert not result.retryable
    assert result.rejected[0].error_kind == "auth"


@pytest.mark.asyncio
async def test_send_batch_missing_secret_rejects_auth(sample_event: dict) -> None:
    """Sem client_secret (dormant) → rejected auth sem tocar a rede."""
    dormant = SentinelClient(
        dce_endpoint="https://dce.ingest.monitor.azure.com",
        dcr_immutable_id="dcr-abc123",
        stream_name="Custom-CentralOps_CL",
        tenant_id=_TENANT_ID,
        client_id="app-client-id",
        client_secret=None,
        verify_tls=False,
    )
    with aioresponses() as m:
        result = await dormant.send_batch([sample_event])
        assert not m.requests  # nenhuma chamada de rede

    assert result.accepted == 0
    assert result.rejected[0].error_kind == "auth"


# ── (f) test() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_test_passed_when_token_acquired(client: SentinelClient) -> None:
    """test() adquire o token → passed com latency_ms."""
    with aioresponses() as m:
        m.post(_TOKEN_URL, payload=_token_payload())

        result = await client.test()

    assert result.ok is True
    assert result.latency_ms is not None
    assert _TENANT_ID in result.detail


@pytest.mark.asyncio
async def test_test_failed_when_credential_invalid(client: SentinelClient) -> None:
    """test() com 401 no token → failed descritivo (nunca levanta)."""
    with aioresponses() as m:
        m.post(
            _TOKEN_URL,
            status=401,
            payload={"error": "invalid_client", "error_description": "AADSTS7000215"},
        )

        result = await client.test()

    assert result.ok is False
    assert "token" in result.detail.lower() or "aad" in result.detail.lower()


@pytest.mark.asyncio
async def test_test_failed_when_secret_missing() -> None:
    """test() sem client_secret → failed (destino dormant)."""
    dormant = SentinelClient(
        dce_endpoint="https://dce.ingest.monitor.azure.com",
        dcr_immutable_id="dcr-abc123",
        stream_name="Custom-CentralOps_CL",
        tenant_id=_TENANT_ID,
        client_id="app-client-id",
        client_secret=None,
    )
    result = await dormant.test()
    assert result.ok is False
    assert "dormant" in result.detail.lower() or "secret" in result.detail.lower()


# ── close() ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_close_is_safe_without_session(client: SentinelClient) -> None:
    """close() sem sessão aberta não levanta."""
    assert client._session is None
    await client.close()  # no-op


@pytest.mark.asyncio
async def test_close_closes_open_session(
    client: SentinelClient, sample_event: dict
) -> None:
    """Após um send_batch, close() fecha a sessão e zera o atributo."""
    with aioresponses() as m:
        m.post(_TOKEN_URL, payload=_token_payload())
        m.post(_INGEST_RE, status=204, repeat=True)
        await client.send_batch([sample_event])

    assert client._session is not None
    await client.close()
    assert client._session is None


# ── concorrência: refresh de token serializado (lock) ─────────────────────


@pytest.mark.asyncio
async def test_concurrent_token_refresh_issues_single_post(
    client: SentinelClient, sample_event: dict
) -> None:
    """~8 send_batch concorrentes com cache vazio → 1 POST único ao endpoint de token.

    O ``SentinelClient`` é cacheado/reusado e vários ``send_batch`` rodam
    concorrentes (concurrency=8). Sem o ``_token_lock``, N coroutines passam o
    check do cache e disparam N POSTs simultâneos ao endpoint AAD. Com o lock,
    só o 1º refresh ocorre; os demais pegam o token recém-escrito no double-check.
    """
    assert client._token is None  # cache vazio no início
    with aioresponses() as m:
        # Token registrado SEM repeat: um 2º POST de token falharia (mock sem
        # registro) → prova que houve exatamente UMA aquisição sob concorrência.
        m.post(_TOKEN_URL, payload=_token_payload(expires_in=3600))
        m.post(_INGEST_RE, status=204, repeat=True)

        results = await asyncio.gather(
            *(client.send_batch([sample_event]) for _ in range(8))
        )

    assert all(r.accepted == 1 for r in results)

    # Exatamente UM POST ao endpoint de token (refresh serializado pelo lock).
    token_calls = [
        c
        for (method, url), calls in m.requests.items()
        if method == "POST" and "oauth2/v2.0/token" in str(url)
        for c in calls
    ]
    assert len(token_calls) == 1, f"esperado 1 POST de token, obteve {len(token_calls)}"

    # As 8 ingestões ocorreram (uma por send_batch).
    ingest_calls = [
        c
        for (method, url), calls in m.requests.items()
        if method == "POST" and "dataCollectionRules" in str(url)
        for c in calls
    ]
    assert len(ingest_calls) == 8


# ── verify_tls=True: caminho de SSL ───────────────────────────────────────


def test_build_ssl_returns_true_when_verify_tls() -> None:
    """Com verify_tls=True, o parâmetro ``ssl`` do aiohttp é ``True`` (verificação padrão)."""
    secure = SentinelClient(
        dce_endpoint="https://dce.ingest.monitor.azure.com",
        dcr_immutable_id="dcr-abc123",
        stream_name="Custom-CentralOps_CL",
        tenant_id=_TENANT_ID,
        client_id="app-client-id",
        client_secret="super-secret-value",
        verify_tls=True,
    )
    assert secure._build_ssl() is True


# ── at-least-once: reentrega de lote após retry duplica o POST ────────────


@pytest.mark.asyncio
async def test_retry_reposts_batch_at_least_once(
    client: SentinelClient, sample_event: dict
) -> None:
    """429 → retryable; reentrega do MESMO lote → 204. Exatamente 2 POSTs de ingestão.

    Comprova a entrega at-least-once documentada (a Logs Ingestion API não
    deduplica): após um 429 retryável o orquestrador reentrega o lote, gerando
    um 2º POST do mesmo evento — que PODE duplicar registros na tabela _CL.
    """
    with aioresponses() as m:
        m.post(_TOKEN_URL, payload=_token_payload(), repeat=True)
        # Consumidos na ordem: 1º POST de ingestão → 429; 2º (reentrega) → 204.
        m.post(_INGEST_RE, status=429)
        m.post(_INGEST_RE, status=204)

        r1 = await client.send_batch([sample_event])
        assert r1.retryable is True
        assert r1.accepted == 0

        r2 = await client.send_batch([sample_event])
        assert r2.accepted == 1
        assert r2.all_accepted

    # Exatamente DOIS POSTs de ingestão (lote reentregue → at-least-once).
    ingest_calls = [
        c
        for (method, url), calls in m.requests.items()
        if method == "POST" and "dataCollectionRules" in str(url)
        for c in calls
    ]
    assert len(ingest_calls) == 2, f"esperado 2 POSTs de ingestão, obteve {len(ingest_calls)}"


# ── (g) Registry ──────────────────────────────────────────────────────────


def test_sentinel_registered() -> None:
    """O kind 'sentinel' deve estar no registry (auto-registro no import)."""
    assert "sentinel" in registry.all_kinds()


def test_sentinel_build_returns_destination_with_correct_kind() -> None:
    config = {
        "dce_endpoint": "https://dce.ingest.monitor.azure.com",
        "dcr_immutable_id": "dcr-abc123",
        "stream_name": "Custom-CentralOps_CL",
        "tenant_id": _TENANT_ID,
        "client_id": "app-client-id",
    }
    dest_config = DestinationConfig(
        destination_id="test-sentinel-registry",
        kind="sentinel",
        config=config,
        config_version=compute_config_version(config, {}),
    )
    dest = registry.build(dest_config)
    assert dest.kind == "sentinel"
    assert isinstance(dest, SentinelClient)


def test_sentinel_build_without_secret_has_none(sample_event: dict) -> None:
    """Sem secret_ref/secrets → client_secret=None (dormant fail-closed)."""
    config = {
        "dce_endpoint": "https://dce.ingest.monitor.azure.com",
        "dcr_immutable_id": "dcr-abc123",
        "stream_name": "Custom-CentralOps_CL",
        "tenant_id": _TENANT_ID,
        "client_id": "app-client-id",
    }
    dest_config = DestinationConfig(
        destination_id="dormant-sentinel",
        kind="sentinel",
        config=config,
        secret_ref=None,
    )
    dest = registry.build(dest_config, secrets=None)
    assert isinstance(dest, SentinelClient)
    assert dest._client_secret is None


def test_sentinel_build_with_secret_resolves_client_secret() -> None:
    """Com secrets e secret_ref, o client_secret é decifrado."""
    config = {
        "dce_endpoint": "https://dce.ingest.monitor.azure.com",
        "dcr_immutable_id": "dcr-abc123",
        "stream_name": "Custom-CentralOps_CL",
        "tenant_id": _TENANT_ID,
        "client_id": "app-client-id",
    }
    dest_config = DestinationConfig(
        destination_id="secret-sentinel",
        kind="sentinel",
        config=config,
        secret_ref="enc::sentinel",
    )
    mock_secrets = MagicMock()
    mock_secrets.decrypt.return_value = "plain-client-secret"

    dest = registry.build(dest_config, secrets=mock_secrets)

    assert isinstance(dest, SentinelClient)
    assert dest._client_secret == "plain-client-secret"
    mock_secrets.decrypt.assert_called_once_with("enc::sentinel")


def test_sentinel_registration_metadata() -> None:
    """Metadados do catálogo: queue, capabilities (sem idempotent), secret, label."""
    reg = registry.get("sentinel")
    assert reg.default_queue == "dispatch.sentinel"
    assert "tls" in reg.capabilities
    assert "batch" in reg.capabilities
    assert "test" in reg.capabilities
    # NÃO idempotent: Logs Ingestion API não deduplica (at-least-once).
    assert "idempotent" not in reg.capabilities
    assert "client_secret" in reg.required_secrets
    assert reg.label == "Microsoft Sentinel (Logs Ingestion)"
