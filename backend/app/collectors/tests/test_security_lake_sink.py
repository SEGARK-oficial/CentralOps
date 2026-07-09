"""Testes do destino Amazon Security Lake (kind="security_lake").

Cobre:
(a) ``format`` — envelope → OCSF ``normalized``.
(b) ``_event_day`` — deriva YYYYMMDD do campo OCSF ``time`` (epoch ms e s).
(c) ``_object_key`` — layout de partição do Security Lake (determinístico via hash).
(d) ``send_batch`` — feliz (1 put_object Parquet), idempotência (mesma key),
    sem credencial (auth), 5xx (retryable), 4xx auth (rejected), parquet ausente
    (unknown), schema heterogêneo (schema_rejected).
(e) ``test()`` — head_bucket ok / sem credencial failed.
(f) Registry — registrado, build() resolve secret, metadados (idempotent/cold).

CRÍTICO: nenhum ``pyarrow``/``aioboto3`` importado. Os seams ``_to_parquet`` e
``_client`` são sobrescritos (fake bytes + FakeS3Client).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, List
from unittest.mock import MagicMock

import pytest

from backend.app.collectors.output.base import DeliveryResult
from backend.app.collectors.output.destinations import registry
from backend.app.collectors.output.destinations.registry import (
    DestinationConfig,
    compute_config_version,
)
from backend.app.collectors.output.destinations.security_lake import (
    SecurityLakeClient,
    SecurityLakeConfig,
)


# ── Fake async S3 client ─────────────────────────────────────────────────


class FakeS3Client:
    def __init__(self, *, raise_on_put: Exception | None = None,
                 raise_on_head: Exception | None = None) -> None:
        self.objects: dict[str, Any] = {}
        self.put_calls: List[dict[str, Any]] = []
        self.head_calls: List[dict[str, Any]] = []
        self._raise_on_put = raise_on_put
        self._raise_on_head = raise_on_head

    async def __aenter__(self) -> "FakeS3Client":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def put_object(self, **kwargs: Any) -> dict:
        if self._raise_on_put is not None:
            raise self._raise_on_put
        self.put_calls.append(kwargs)
        self.objects[kwargs["Key"]] = kwargs["Body"]
        return {"ETag": "fake"}

    async def head_bucket(self, **kwargs: Any) -> dict:
        if self._raise_on_head is not None:
            raise self._raise_on_head
        self.head_calls.append(kwargs)
        return {}


def _patch(client: SecurityLakeClient, fake: FakeS3Client) -> None:
    client._client = lambda: fake  # type: ignore[method-assign]
    client._to_parquet = lambda rows: b"PAR1-fake-parquet"  # type: ignore[method-assign]


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def client() -> SecurityLakeClient:
    return SecurityLakeClient(
        bucket="aws-security-data-lake-us-east-1-abc",
        account_id="123456789012",
        region="us-east-1",
        source="centralops",
        access_key_id="AKIAFAKE",
        secret_access_key="fake-secret",
    )


@pytest.fixture
def sample_batch() -> List[dict]:
    return [
        {
            "_centralops": {"event_id": "evt-aaa", "organization_id": 42},
            # 2026-06-21 00:00:00 UTC = 1782000000 s = 1782000000000 ms
            "normalized": {"class_uid": 2004, "time": 1782000000000, "message": "a"},
        },
        {
            "_centralops": {"event_id": "evt-bbb", "organization_id": 42},
            "normalized": {"class_uid": 2004, "time": 1782000000000, "message": "b"},
        },
    ]


# ── (a) format / (b) event_day / (c) object_key ──────────────────────────


def test_format_returns_normalized(client: SecurityLakeClient, sample_batch: List[dict]) -> None:
    out = client.format(sample_batch[0])
    assert out == {"class_uid": 2004, "time": 1782000000000, "message": "a"}


def test_event_day_from_epoch_ms() -> None:
    rows = [{"time": 1782000000000}]
    assert SecurityLakeClient._event_day(rows) == "20260621"


def test_event_day_from_epoch_seconds() -> None:
    rows = [{"time": 1782000000}]
    assert SecurityLakeClient._event_day(rows) == "20260621"


def test_event_day_fallback_when_missing() -> None:
    rows = [{"message": "sem time"}]
    out = SecurityLakeClient._event_day(rows)
    assert len(out) == 8 and out.isdigit()


def test_object_key_partition_layout(client: SecurityLakeClient, sample_batch: List[dict]) -> None:
    rows = [client.format(ev) for ev in sample_batch]
    key = client._object_key(sample_batch, rows)
    assert key.startswith(
        "ext/centralops/region=us-east-1/accountId=123456789012/eventDay=20260621/"
    )
    assert key.endswith(".parquet")


# ── (d) send_batch ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_batch_success_one_put(client: SecurityLakeClient, sample_batch: List[dict]) -> None:
    fake = FakeS3Client()
    _patch(client, fake)
    result = await client.send_batch(sample_batch)
    assert isinstance(result, DeliveryResult)
    assert result.accepted == 2 and result.all_accepted
    assert len(fake.put_calls) == 1
    assert fake.put_calls[0]["Body"] == b"PAR1-fake-parquet"
    assert fake.put_calls[0]["ContentType"] == "application/vnd.apache.parquet"


@pytest.mark.asyncio
async def test_send_batch_idempotent_same_key(client: SecurityLakeClient, sample_batch: List[dict]) -> None:
    fake = FakeS3Client()
    _patch(client, fake)
    await client.send_batch(sample_batch)
    await client.send_batch(sample_batch)
    # Mesma key → 1 objeto (sobrescrito), apesar de 2 puts.
    assert len(fake.objects) == 1
    assert len(fake.put_calls) == 2


@pytest.mark.asyncio
async def test_send_batch_no_credentials_auth(sample_batch: List[dict]) -> None:
    c = SecurityLakeClient(bucket="b", account_id="1", access_key_id=None, secret_access_key=None)
    result = await c.send_batch(sample_batch)
    assert result.accepted == 0 and not result.retryable
    assert all(r.error_kind == "auth" for r in result.rejected)


@pytest.mark.asyncio
async def test_send_batch_5xx_retryable(client: SecurityLakeClient, sample_batch: List[dict]) -> None:
    err = Exception("boom")
    err.response = {"ResponseMetadata": {"HTTPStatusCode": 503}}  # type: ignore[attr-defined]
    fake = FakeS3Client(raise_on_put=err)
    _patch(client, fake)
    result = await client.send_batch(sample_batch)
    assert result.retryable is True and result.accepted == 0


@pytest.mark.asyncio
async def test_send_batch_4xx_auth_rejected(client: SecurityLakeClient, sample_batch: List[dict]) -> None:
    err = Exception("denied")
    err.response = {"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403}}  # type: ignore[attr-defined]
    fake = FakeS3Client(raise_on_put=err)
    _patch(client, fake)
    result = await client.send_batch(sample_batch)
    assert result.accepted == 0 and not result.retryable
    assert result.rejected[0].error_kind == "auth"


@pytest.mark.asyncio
async def test_send_batch_pyarrow_missing_unknown(client: SecurityLakeClient, sample_batch: List[dict]) -> None:
    fake = FakeS3Client()
    client._client = lambda: fake  # type: ignore[method-assign]

    def _boom(rows: Any) -> bytes:
        raise RuntimeError("pyarrow não instalado")

    client._to_parquet = _boom  # type: ignore[method-assign]
    result = await client.send_batch(sample_batch)
    assert result.accepted == 0 and not result.retryable
    assert result.rejected[0].error_kind == "unknown"
    assert len(fake.put_calls) == 0


@pytest.mark.asyncio
async def test_send_batch_parquet_schema_error_rejected(client: SecurityLakeClient, sample_batch: List[dict]) -> None:
    fake = FakeS3Client()
    client._client = lambda: fake  # type: ignore[method-assign]

    def _boom(rows: Any) -> bytes:
        raise ValueError("schema heterogêneo")

    client._to_parquet = _boom  # type: ignore[method-assign]
    result = await client.send_batch(sample_batch)
    assert result.rejected[0].error_kind == "schema_rejected"
    assert not result.retryable


@pytest.mark.asyncio
async def test_send_batch_empty_ok_zero(client: SecurityLakeClient) -> None:
    result = await client.send_batch([])
    assert result.accepted == 0 and result.all_accepted


# ── (e) test() ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_test_head_bucket_ok(client: SecurityLakeClient) -> None:
    fake = FakeS3Client()
    _patch(client, fake)
    result = await client.test()
    assert result.ok is True
    assert len(fake.head_calls) == 1


@pytest.mark.asyncio
async def test_test_no_credentials_failed() -> None:
    c = SecurityLakeClient(bucket="b", account_id="1", access_key_id=None, secret_access_key=None)
    result = await c.test()
    assert result.ok is False


@pytest.mark.asyncio
async def test_test_head_error_failed(client: SecurityLakeClient) -> None:
    err = Exception("nope")
    err.response = {"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403}}  # type: ignore[attr-defined]
    fake = FakeS3Client(raise_on_head=err)
    _patch(client, fake)
    result = await client.test()
    assert result.ok is False


# ── (f) Registry ─────────────────────────────────────────────────────────


def test_security_lake_registered() -> None:
    assert "security_lake" in registry.all_kinds()


def test_security_lake_build_resolves_secret() -> None:
    config = {"bucket": "b", "account_id": "123456789012", "region": "us-east-1"}
    dest_config = DestinationConfig(
        destination_id="sl-1",
        kind="security_lake",
        config=config,
        secret_ref="enc::abc",
        config_version=compute_config_version(config, {}),
    )
    secrets = MagicMock()
    secrets.decrypt.return_value = "aws-secret-plain"
    dest = registry.build(dest_config, secrets=secrets)
    assert isinstance(dest, SecurityLakeClient)
    assert dest._secret_access_key == "aws-secret-plain"


def test_security_lake_build_without_secret_dormant() -> None:
    config = {"bucket": "b", "account_id": "1"}
    dest_config = DestinationConfig(destination_id="sl-2", kind="security_lake", config=config, secret_ref=None)
    dest = registry.build(dest_config, secrets=None)
    assert isinstance(dest, SecurityLakeClient)
    assert dest._secret_access_key is None


def test_security_lake_registration_metadata() -> None:
    reg = registry.get("security_lake")
    assert reg.default_queue == "dispatch.security_lake"
    assert "idempotent" in reg.capabilities
    assert "tls" in reg.capabilities
    assert "aws_secret_access_key" in reg.required_secrets
    assert reg.label == "Amazon Security Lake (OCSF Parquet)"
    assert reg.delivery_defaults.get("tier") == "cold"


@pytest.mark.asyncio
async def test_close_noop(client: SecurityLakeClient) -> None:
    await client.close()  # não deve levantar
