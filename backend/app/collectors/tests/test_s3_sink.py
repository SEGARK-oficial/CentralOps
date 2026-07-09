"""Testes do destino S3 / object store NDJSON (kind="s3").

Cobre:
(a) ``format`` — envelope → dict (uma linha NDJSON).
(b) ``send_batch`` feliz — 1 put_object, key DETERMINÍSTICA, NDJSON gzip round-trip.
(c) idempotência — mesmo lote → MESMA key.
(d) erros — sem credencial (auth), 4xx auth (rejected), 5xx (retryable).
(e) ``test()`` — head_bucket ok / sem credencial failed.
(f) ``prune_expired`` — deleta os objetos antigos, preserva os novos.
(g) ``erase_by_org`` — deleta todos os objetos da org.
(h) Registry — kind 'ns3' registrado, build(), metadados (capabilities/tier).

CRÍTICO: nenhum import de ``aioboto3``. O fake async client é injetado via
monkeypatch de ``S3Client._client`` (override-ável por design) e captura as
chamadas put_object/head_bucket/list_objects_v2/delete_objects.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from typing import Any, List
from unittest.mock import MagicMock

import pytest

from backend.app.collectors.output.base import DeliveryResult, ErasureResult, TestResult
from backend.app.collectors.output.destinations import registry
from backend.app.collectors.output.destinations.registry import (
    DestinationConfig,
    compute_config_version,
)
from backend.app.collectors.output.destinations.s3 import S3Client, S3Config


# ── Fake async S3 client ─────────────────────────────────────────────────


class FakeS3Client:
    """Fake do client S3 do aioboto3: async context manager que captura chamadas.

    Pode ser pré-carregado com ``objects`` (mapa key → {"Body":bytes,
    "LastModified":dt}) para simular o estado do bucket em list/delete. Levanta
    ``raise_on_put`` em ``put_object`` quando configurado (teste de erro).
    """

    def __init__(
        self,
        *,
        objects: dict[str, dict[str, Any]] | None = None,
        raise_on_put: Exception | None = None,
        raise_on_head: Exception | None = None,
    ) -> None:
        self.objects: dict[str, dict[str, Any]] = dict(objects or {})
        self.put_calls: List[dict[str, Any]] = []
        self.head_calls: List[dict[str, Any]] = []
        self.list_calls: List[dict[str, Any]] = []
        self.delete_calls: List[dict[str, Any]] = []
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
        self.objects[kwargs["Key"]] = {
            "Body": kwargs["Body"],
            "LastModified": datetime.now(timezone.utc),
        }
        return {"ETag": "fake-etag"}

    async def head_bucket(self, **kwargs: Any) -> dict:
        if self._raise_on_head is not None:
            raise self._raise_on_head
        self.head_calls.append(kwargs)
        return {}

    async def list_objects_v2(self, **kwargs: Any) -> dict:
        self.list_calls.append(kwargs)
        prefix = kwargs.get("Prefix", "")
        contents = [
            {"Key": k, "LastModified": v["LastModified"]}
            for k, v in self.objects.items()
            if k.startswith(prefix)
        ]
        return {"Contents": contents, "IsTruncated": False}

    async def delete_objects(self, **kwargs: Any) -> dict:
        self.delete_calls.append(kwargs)
        for obj in kwargs["Delete"]["Objects"]:
            self.objects.pop(obj["Key"], None)
        return {"Deleted": [{"Key": o["Key"]} for o in kwargs["Delete"]["Objects"]]}


def _patch_client(client: S3Client, fake: FakeS3Client) -> None:
    """Substitui ``_client`` para devolver SEMPRE o mesmo fake (CM reutilizável)."""
    client._client = lambda: fake  # type: ignore[method-assign]


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def client() -> S3Client:
    """Cliente S3 com credencial explícita e gzip (default)."""
    return S3Client(
        bucket="centralops-lake",
        region="us-east-1",
        prefix="centralops",
        access_key_id="AKIAFAKE",
        secret_access_key="fake-secret",
        compression="gzip",
    )


@pytest.fixture
def sample_batch() -> List[dict]:
    """Lote canônico com event_id e organization_id no namespace _centralops."""
    return [
        {
            "_centralops": {
                "event_id": "evt-aaa",
                "organization_id": 42,
                "received_at": "2026-04-23T14:22:10Z",
            },
            "data": {"id": "1", "severity": "high"},
        },
        {
            "_centralops": {
                "event_id": "evt-bbb",
                "organization_id": 42,
                "received_at": "2026-04-23T15:00:00Z",
            },
            "data": {"id": "2", "severity": "low"},
        },
    ]


# ── (a) format ───────────────────────────────────────────────────────────


def test_format_returns_envelope_dict(client: S3Client, sample_batch: List[dict]) -> None:
    out = client.format(sample_batch[0])
    assert isinstance(out, dict)
    assert out["_centralops"]["event_id"] == "evt-aaa"


# ── (b) send_batch feliz ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_batch_success_single_put(
    client: S3Client, sample_batch: List[dict]
) -> None:
    """Lote feliz → 1 put_object, DeliveryResult.ok(2)."""
    fake = FakeS3Client()
    _patch_client(client, fake)

    result = await client.send_batch(sample_batch)

    assert isinstance(result, DeliveryResult)
    assert result.accepted == 2
    assert result.all_accepted
    assert not result.retryable
    assert len(fake.put_calls) == 1
    assert fake.put_calls[0]["Bucket"] == "centralops-lake"


@pytest.mark.asyncio
async def test_send_batch_deterministic_key(
    client: S3Client, sample_batch: List[dict]
) -> None:
    """Key segue o layout Hive determinístico: prefix/org=/YYYY/MM/DD/hash.ndjson.gz."""
    fake = FakeS3Client()
    _patch_client(client, fake)

    await client.send_batch(sample_batch)
    key = fake.put_calls[0]["Key"]

    # Partição vem do received_at do 1º evento (2026-04-23), org=42.
    assert key.startswith("centralops/org=42/2026/04/23/")
    assert key.endswith(".ndjson.gz")
    # ContentEncoding gzip + ContentType ndjson.
    assert fake.put_calls[0]["ContentEncoding"] == "gzip"
    assert fake.put_calls[0]["ContentType"] == "application/x-ndjson"


@pytest.mark.asyncio
async def test_send_batch_ndjson_gzip_roundtrip(
    client: S3Client, sample_batch: List[dict]
) -> None:
    """O Body é gzip; descomprimido = NDJSON (uma linha JSON por evento)."""
    fake = FakeS3Client()
    _patch_client(client, fake)

    await client.send_batch(sample_batch)
    body = fake.put_calls[0]["Body"]

    raw = gzip.decompress(body)
    lines = raw.decode("utf-8").strip().split("\n")
    assert len(lines) == 2
    parsed = [json.loads(ln) for ln in lines]
    assert parsed[0]["_centralops"]["event_id"] == "evt-aaa"
    assert parsed[1]["_centralops"]["event_id"] == "evt-bbb"


@pytest.mark.asyncio
async def test_send_batch_no_compression_plain_ndjson() -> None:
    """compression='none' → Body é NDJSON cru, key termina em .ndjson."""
    client = S3Client(
        bucket="b",
        access_key_id="k",
        secret_access_key="s",
        compression="none",
    )
    fake = FakeS3Client()
    _patch_client(client, fake)

    batch = [{"_centralops": {"event_id": "x", "organization_id": 1}, "v": 1}]
    await client.send_batch(batch)

    key = fake.put_calls[0]["Key"]
    assert key.endswith(".ndjson")
    assert "ContentEncoding" not in fake.put_calls[0]
    body = fake.put_calls[0]["Body"]
    assert json.loads(body.decode("utf-8").strip())["v"] == 1


@pytest.mark.asyncio
async def test_send_batch_org_global_when_missing() -> None:
    """Sem organization_id → partição org=global."""
    client = S3Client(bucket="b", access_key_id="k", secret_access_key="s")
    fake = FakeS3Client()
    _patch_client(client, fake)

    await client.send_batch([{"_centralops": {"event_id": "z"}, "v": 1}])
    assert "/org=global/" in fake.put_calls[0]["Key"]


@pytest.mark.asyncio
async def test_send_batch_empty_returns_ok_zero(client: S3Client) -> None:
    """Lote vazio → ok(0) sem put_object."""
    fake = FakeS3Client()
    _patch_client(client, fake)
    result = await client.send_batch([])
    assert result.accepted == 0
    assert result.all_accepted
    assert not fake.put_calls


# ── (c) idempotência ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_idempotent_same_batch_same_key(
    client: S3Client, sample_batch: List[dict]
) -> None:
    """Reentrega do MESMO lote → MESMA key (sobrescreve, não duplica)."""
    fake = FakeS3Client()
    _patch_client(client, fake)

    await client.send_batch(sample_batch)
    await client.send_batch(sample_batch)

    assert len(fake.put_calls) == 2
    assert fake.put_calls[0]["Key"] == fake.put_calls[1]["Key"]
    # Idempotência byte-a-byte: gzip determinístico (mtime=0) → mesmo Body.
    assert fake.put_calls[0]["Body"] == fake.put_calls[1]["Body"]
    # Só 1 objeto no bucket após 2 entregas.
    assert len(fake.objects) == 1


@pytest.mark.asyncio
async def test_different_batch_different_key(
    client: S3Client, sample_batch: List[dict]
) -> None:
    """Lotes com event_ids distintos → keys distintas."""
    fake = FakeS3Client()
    _patch_client(client, fake)

    other = [
        {
            "_centralops": {
                "event_id": "evt-ccc",
                "organization_id": 42,
                "received_at": "2026-04-23T16:00:00Z",
            },
            "data": {"id": "3"},
        }
    ]
    await client.send_batch(sample_batch)
    await client.send_batch(other)

    assert fake.put_calls[0]["Key"] != fake.put_calls[1]["Key"]


# ── (d) erros ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_batch_no_credentials_rejected_auth(
    sample_batch: List[dict],
) -> None:
    """Sem credencial e sem IAM role → rejected error_kind='auth', não-retryable."""
    client = S3Client(bucket="b")  # sem access_key/secret, use_iam_role=False
    # nem precisa de fake: falha antes de tocar o client.
    result = await client.send_batch(sample_batch)

    assert result.accepted == 0
    assert not result.retryable
    assert len(result.rejected) == 2
    assert result.rejected[0].error_kind == "auth"
    assert result.rejected[0].event_id == "evt-aaa"


@pytest.mark.asyncio
async def test_send_batch_iam_role_no_secret_attempts_put(
    sample_batch: List[dict],
) -> None:
    """use_iam_role=True sem secret → tenta entregar (credencial via role)."""
    client = S3Client(bucket="b", use_iam_role=True)
    fake = FakeS3Client()
    _patch_client(client, fake)

    result = await client.send_batch(sample_batch)
    assert result.accepted == 2
    assert len(fake.put_calls) == 1


@pytest.mark.asyncio
async def test_send_batch_auth_error_rejected(
    client: S3Client, sample_batch: List[dict]
) -> None:
    """4xx AccessDenied no put → rejected error_kind='auth', não-retryable."""
    err = Exception("AccessDenied")
    err.response = {  # type: ignore[attr-defined]
        "Error": {"Code": "AccessDenied"},
        "ResponseMetadata": {"HTTPStatusCode": 403},
    }
    fake = FakeS3Client(raise_on_put=err)
    _patch_client(client, fake)

    result = await client.send_batch(sample_batch)
    assert result.accepted == 0
    assert not result.retryable
    assert result.rejected[0].error_kind == "auth"


@pytest.mark.asyncio
async def test_send_batch_5xx_retryable(
    client: S3Client, sample_batch: List[dict]
) -> None:
    """5xx no put → retryable=True (lote inteiro), sem rejected."""
    err = Exception("InternalError")
    err.response = {  # type: ignore[attr-defined]
        "Error": {"Code": "InternalError"},
        "ResponseMetadata": {"HTTPStatusCode": 503},
    }
    fake = FakeS3Client(raise_on_put=err)
    _patch_client(client, fake)

    result = await client.send_batch(sample_batch)
    assert result.accepted == 0
    assert result.retryable is True
    assert not result.rejected


@pytest.mark.asyncio
async def test_send_batch_connection_error_retryable(
    client: S3Client, sample_batch: List[dict]
) -> None:
    """Erro de conexão sem status → retryable=True."""

    class EndpointConnectionError(Exception):
        pass

    fake = FakeS3Client(raise_on_put=EndpointConnectionError("no route"))
    _patch_client(client, fake)

    result = await client.send_batch(sample_batch)
    assert result.retryable is True


# ── (e) test() ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_test_head_bucket_ok(client: S3Client) -> None:
    """head_bucket ok → TestResult.passed com latency_ms."""
    fake = FakeS3Client()
    _patch_client(client, fake)

    result = await client.test()
    assert isinstance(result, TestResult)
    assert result.ok is True
    assert result.latency_ms is not None
    assert len(fake.head_calls) == 1


@pytest.mark.asyncio
async def test_test_no_credentials_failed() -> None:
    """Sem credencial → TestResult.failed descritivo (sem tocar o client)."""
    client = S3Client(bucket="b")
    result = await client.test()
    assert result.ok is False
    assert "credencial" in result.detail.lower()


@pytest.mark.asyncio
async def test_test_head_bucket_error_failed(client: S3Client) -> None:
    """head_bucket 403 → TestResult.failed."""
    err = Exception("Forbidden")
    err.response = {  # type: ignore[attr-defined]
        "Error": {"Code": "AccessDenied"},
        "ResponseMetadata": {"HTTPStatusCode": 403},
    }
    fake = FakeS3Client(raise_on_head=err)
    _patch_client(client, fake)

    result = await client.test()
    assert result.ok is False
    assert "auth" in result.detail.lower()


# ── (f) prune_expired ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prune_expired_deletes_old_keeps_new(client: S3Client) -> None:
    """Objetos mais antigos que retention são deletados; recentes preservados."""
    now = datetime.now(timezone.utc)
    fake = FakeS3Client(
        objects={
            "centralops/org=42/2020/01/01/old.ndjson.gz": {
                "Body": b"x",
                "LastModified": now - timedelta(days=400),
            },
            "centralops/org=42/2026/06/01/new.ndjson.gz": {
                "Body": b"y",
                "LastModified": now - timedelta(days=1),
            },
        }
    )
    _patch_client(client, fake)

    deleted = await client.prune_expired(retention_days=90)

    assert deleted == 1
    assert "centralops/org=42/2020/01/01/old.ndjson.gz" not in fake.objects
    assert "centralops/org=42/2026/06/01/new.ndjson.gz" in fake.objects


@pytest.mark.asyncio
async def test_prune_expired_zero_is_noop(client: S3Client) -> None:
    """retention_days<=0 → no-op (não lista nem deleta)."""
    fake = FakeS3Client(objects={"centralops/org=1/2020/01/01/a.ndjson.gz": {
        "Body": b"x", "LastModified": datetime.now(timezone.utc) - timedelta(days=999),
    }})
    _patch_client(client, fake)

    assert await client.prune_expired(0) == 0
    assert not fake.list_calls
    assert not fake.delete_calls


@pytest.mark.asyncio
async def test_prune_expired_uses_key_date_not_lastmodified(client: S3Client) -> None:
    """Reentrega/sobrescrita: LastModified=agora mas data da KEY é antiga.

    A data de partição da key (derivada do received_at) é a fonte da verdade —
    o objeto DEVE ser deletado mesmo com LastModified recente (compliance LGPD:
    um PUT recente não pode ressuscitar dado vencido).
    """
    now = datetime.now(timezone.utc)
    fake = FakeS3Client(
        objects={
            # Key codifica 2020/01/01 (vencido), mas LastModified=agora (reentrega).
            "centralops/org=42/2020/01/01/reentregue.ndjson.gz": {
                "Body": b"x",
                "LastModified": now,
            },
        }
    )
    _patch_client(client, fake)

    deleted = await client.prune_expired(retention_days=90)

    assert deleted == 1
    assert "centralops/org=42/2020/01/01/reentregue.ndjson.gz" not in fake.objects


@pytest.mark.asyncio
async def test_prune_expired_fallback_to_lastmodified_when_key_has_no_date(
    client: S3Client,
) -> None:
    """Key sem padrão de data → cai no fallback de LastModified."""
    now = datetime.now(timezone.utc)
    fake = FakeS3Client(
        objects={
            # Key sem .../YYYY/MM/DD/... → usa LastModified (antigo → vencido).
            "centralops/org=42/legado/sem-data.ndjson.gz": {
                "Body": b"x",
                "LastModified": now - timedelta(days=400),
            },
            # Mesma forma, mas LastModified recente → preservado.
            "centralops/org=42/legado/recente.ndjson.gz": {
                "Body": b"y",
                "LastModified": now - timedelta(days=1),
            },
        }
    )
    _patch_client(client, fake)

    deleted = await client.prune_expired(retention_days=90)

    assert deleted == 1
    assert "centralops/org=42/legado/sem-data.ndjson.gz" not in fake.objects
    assert "centralops/org=42/legado/recente.ndjson.gz" in fake.objects


@pytest.mark.asyncio
async def test_prune_expired_propagates_on_client_failure(client: S3Client) -> None:
    """Falha real (list levanta) DEVE propagar — não retornar 0 (sucesso falso).

    O caller (enforce_destination_retention) isola por destino e marca -1 em
    exceção; um 0 silencioso seria indistinguível de "nada a podar".
    """
    fake = FakeS3Client(
        objects={
            "centralops/org=42/2020/01/01/a.ndjson.gz": {
                "Body": b"x",
                "LastModified": datetime.now(timezone.utc),
            }
        }
    )

    async def _boom(**kwargs: Any) -> dict:
        raise RuntimeError("bucket inacessível")

    fake.list_objects_v2 = _boom  # type: ignore[assignment]
    _patch_client(client, fake)

    with pytest.raises(RuntimeError, match="bucket inacessível"):
        await client.prune_expired(retention_days=90)


def test_extract_partition_date_parses_valid_key() -> None:
    """Helper extrai .../YYYY/MM/DD/... como datetime UTC."""
    dt = S3Client._extract_partition_date(
        "centralops/org=42/2020/01/01/abc.ndjson.gz"
    )
    assert dt == datetime(2020, 1, 1, tzinfo=timezone.utc)


def test_extract_partition_date_none_when_no_date() -> None:
    """Key sem padrão de data → None (cai no fallback de LastModified)."""
    assert S3Client._extract_partition_date("centralops/org=42/legado/x.gz") is None
    assert S3Client._extract_partition_date("") is None
    assert S3Client._extract_partition_date(None) is None
    # Faixa inválida (mês 13) não casa.
    assert S3Client._extract_partition_date("p/org=1/2020/13/01/x") is None


# ── (g) erase_by_org ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_erase_by_org_deletes_all_org_objects(client: S3Client) -> None:
    """erase_by_org deleta tudo sob prefix/org={oid}/, preserva outras orgs."""
    now = datetime.now(timezone.utc)
    fake = FakeS3Client(
        objects={
            "centralops/org=42/2026/01/01/a.ndjson.gz": {"Body": b"a", "LastModified": now},
            "centralops/org=42/2026/01/02/b.ndjson.gz": {"Body": b"b", "LastModified": now},
            "centralops/org=99/2026/01/01/c.ndjson.gz": {"Body": b"c", "LastModified": now},
        }
    )
    _patch_client(client, fake)

    result = await client.erase_by_org(42)

    assert isinstance(result, ErasureResult)
    assert result.ok
    assert len(result.erased) == 2
    # Org 99 preservada.
    assert "centralops/org=99/2026/01/01/c.ndjson.gz" in fake.objects
    assert "centralops/org=42/2026/01/01/a.ndjson.gz" not in fake.objects


@pytest.mark.asyncio
async def test_erase_by_org_empty_is_success(client: S3Client) -> None:
    """Org sem objetos → sucesso vazio (idempotente)."""
    fake = FakeS3Client(objects={})
    _patch_client(client, fake)

    result = await client.erase_by_org(7)
    assert result.ok
    assert result.erased == []


@pytest.mark.asyncio
async def test_erase_by_org_error_returns_failed(client: S3Client) -> None:
    """Erro ao listar → ErasureResult.error (best-effort, sem raise)."""
    fake = FakeS3Client()

    async def _boom(**kwargs: Any) -> dict:
        raise Exception("ListBucket denied")

    fake.list_objects_v2 = _boom  # type: ignore[assignment]
    _patch_client(client, fake)

    result = await client.erase_by_org(42)
    assert not result.ok
    assert result.failed == ["org:42"]


# ── (h) close ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_close_is_noop(client: S3Client) -> None:
    await client.close()  # não deve levantar


# ── (i) Registry ─────────────────────────────────────────────────────────


def test_s3_registered() -> None:
    assert "s3" in registry.all_kinds()


def test_s3_build_returns_destination_with_correct_kind() -> None:
    config = {"bucket": "my-lake", "region": "eu-west-1"}
    dest_config = DestinationConfig(
        destination_id="test-s3-registry",
        kind="s3",
        config=config,
        config_version=compute_config_version(config, {}),
    )
    dest = registry.build(dest_config)
    assert dest.kind == "s3"
    assert isinstance(dest, S3Client)


def test_s3_build_without_secret_has_none_secret() -> None:
    """Sem secret_ref/secrets, secret_access_key=None (dormant fail-closed)."""
    config = {"bucket": "b"}
    dest_config = DestinationConfig(
        destination_id="dormant-s3", kind="s3", config=config, secret_ref=None
    )
    dest = registry.build(dest_config, secrets=None)
    assert isinstance(dest, S3Client)
    assert dest._secret_access_key is None


def test_s3_build_with_secret_resolves_credential() -> None:
    """Com secrets e secret_ref, a secret_access_key é decifrada."""
    config = {"bucket": "b", "access_key_id": "AKIA"}
    dest_config = DestinationConfig(
        destination_id="secret-s3", kind="s3", config=config, secret_ref="enc::xyz"
    )
    mock_secrets = MagicMock()
    mock_secrets.decrypt.return_value = "plain-secret-key"

    dest = registry.build(dest_config, secrets=mock_secrets)

    assert isinstance(dest, S3Client)
    assert dest._secret_access_key == "plain-secret-key"
    assert dest._access_key_id == "AKIA"
    mock_secrets.decrypt.assert_called_once_with("enc::xyz")


def test_s3_build_decrypt_failure_does_not_leak_secret_ref_or_exc(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """decrypt levanta → dormant (secret=None) e o log NÃO vaza secret_ref nem a
    mensagem da exceção (pode conter path da master key KMS); só o tipo."""
    import logging

    config = {"bucket": "b"}
    dest_config = DestinationConfig(
        destination_id="leak-s3",
        kind="s3",
        config=config,
        secret_ref="enc::kms://master-key/SUPER-SENSITIVE-PATH",
    )
    mock_secrets = MagicMock()
    mock_secrets.decrypt.side_effect = RuntimeError(
        "KMS decrypt failed: arn:aws:kms:us-east-1:123:key/leaky-master"
    )

    with caplog.at_level(logging.WARNING):
        dest = registry.build(dest_config, secrets=mock_secrets)

    assert isinstance(dest, S3Client)
    assert dest._secret_access_key is None  # dormant fail-closed
    log_text = caplog.text
    assert "SUPER-SENSITIVE-PATH" not in log_text  # secret_ref não vaza
    assert "leaky-master" not in log_text  # mensagem da exceção não vaza
    assert "RuntimeError" in log_text  # só o tipo


def test_s3_registration_metadata() -> None:
    reg = registry.get("s3")
    assert reg.default_queue == "dispatch.s3"
    assert "idempotent" in reg.capabilities
    assert "erasure_by_query" in reg.capabilities
    assert "retention" in reg.capabilities
    assert "tls" in reg.capabilities
    assert "aws_secret_access_key" in reg.required_secrets
    assert reg.label == "S3 / Object Store (NDJSON)"
    # Sink de lago frio: tier cold por default.
    assert reg.delivery_defaults.get("tier") == "cold"


def test_s3_config_schema_defaults() -> None:
    """S3Config aplica os defaults do contrato."""
    cfg = S3Config(bucket="b")
    assert cfg.region == "us-east-1"
    assert cfg.prefix == "centralops"
    assert cfg.compression == "gzip"
    assert cfg.use_iam_role is False
    assert cfg.endpoint_url is None
