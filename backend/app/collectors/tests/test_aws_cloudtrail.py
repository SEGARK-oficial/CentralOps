"""AWS CloudTrail collector — polling de bucket S3.

Cobre: listagem + download (mock do client aioboto3 via seam _s3_client),
descompressão gzip → Records[], cursor por LastModified, dedupe por eventID,
collect→OCSF. Creds exóticas (reuso genérico de colunas). aioboto3 NÃO é
importado (mock no seam — modelo do test_s3_sink). Zero-core.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from ..base import CollectorContext
from ..normalize import engine as E
from ..normalize.defaults import load_default_rules
from ..vendors import aws_cloudtrail as ct
from ..vendors.aws_cloudtrail import AWSCloudTrailCollector

_CONN = {
    "access_key_id": "AKIA_TEST",
    "secret": "secret-test",
    "bucket": "my-cloudtrail-bucket",
    "region": "us-east-1",
    "prefix_base": "AWSLogs/888/CloudTrail/us-east-1/",
}
_PREFIX = "AWSLogs/888/CloudTrail/us-east-1/2026/06/21/"


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


def _ctx(session=None, cursor: Dict[str, Any] | None = None) -> CollectorContext:
    return CollectorContext(
        integration_id=99,
        organization_id=5,
        platform="aws_cloudtrail",
        headers={},
        session=session or MagicMock(),
        cursor=cursor,
        domain_limiter=_NoopDomainLimiter(),
        rate_limiter=_NoopRateLimiter(),
        redis=MagicMock(),
    )


def _event(eid: str, name: str = "CreateUser"):
    return {
        "eventID": eid,
        "eventTime": "2026-06-21T10:00:00Z",
        "eventName": name,
        "eventSource": "iam.amazonaws.com",
        "awsRegion": "us-east-1",
        "sourceIPAddress": "192.0.2.0",
        "userAgent": "aws-cli/2.13.5",
        "userIdentity": {
            "type": "IAMUser", "principalId": "AIDA", "arn": "arn:aws:iam::888:user/Mary",
            "accountId": "888", "userName": "Mary",
        },
        "recipientAccountId": "888",
        "readOnly": False,
    }


def _gz(records: List[Dict[str, Any]]) -> bytes:
    return gzip.compress(json.dumps({"Records": records}).encode("utf-8"))


class _FakeBody:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def read(self) -> bytes:
        return self._data


class _FakeS3:
    """Mock do client aioboto3: async CM + list_objects_v2/get_object async."""

    def __init__(self, listing: Dict[str, Dict[str, Any]], objects: Dict[str, bytes]) -> None:
        self._listing = listing
        self._objects = objects

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def list_objects_v2(self, **kw):
        return self._listing.get(kw.get("Prefix"), {"Contents": [], "IsTruncated": False})

    async def get_object(self, Bucket, Key):  # noqa: N803
        return {"Body": _FakeBody(self._objects[Key])}


@pytest.mark.asyncio
async def test_collects_records_from_s3_and_normalizes_ocsf() -> None:
    now = datetime.now(timezone.utc)
    key = f"{_PREFIX}888_CloudTrail_us-east-1_20260621T1000Z_abc.json.gz"
    listing = {_PREFIX: {"Contents": [{"Key": key, "LastModified": now}], "IsTruncated": False}}
    objects = {key: _gz([_event("ev-1"), _event("ev-2", name="DeleteUser")])}
    fake = _FakeS3(listing, objects)

    ctx = _ctx(cursor=None)
    with patch.object(AWSCloudTrailCollector, "_load_conn", return_value=dict(_CONN)), \
         patch.object(AWSCloudTrailCollector, "_s3_client", return_value=fake), \
         patch.object(ct, "_date_prefixes", return_value=[_PREFIX]):
        collector = AWSCloudTrailCollector(ctx)
        collected = [ev async for ev in collector.collect()]

    assert [e["eventID"] for e in collected] == ["ev-1", "ev-2"]
    assert collector.extract_message_id(collected[0]) == "ev-1"
    # cursor avança para o LastModified do objeto processado
    assert ctx.cursor["last_modified"] == now.isoformat()
    assert collector.domain == "s3.us-east-1.amazonaws.com"

    norm = E.apply_compiled(
        E.compile_rules(load_default_rules("aws_cloudtrail", "aws_cloudtrail.event")), collected[0]
    ).output["normalized"]
    assert norm["class_uid"] == 6003
    assert norm["api"]["operation"] == "CreateUser"
    assert norm["actor"]["user"]["name"] == "Mary"
    assert norm["cloud"]["region"] == "us-east-1"
    assert norm["metadata"]["uid"] == "ev-1"
    assert norm["time"]


@pytest.mark.asyncio
async def test_skips_objects_at_or_before_cursor_overlap() -> None:
    # objeto bem antigo (LastModified < overlap) é ignorado
    old = datetime(2000, 1, 1, tzinfo=timezone.utc)
    key = f"{_PREFIX}old.json.gz"
    listing = {_PREFIX: {"Contents": [{"Key": key, "LastModified": old}], "IsTruncated": False}}
    fake = _FakeS3(listing, {key: _gz([_event("ev-old")])})

    ctx = _ctx(cursor={"last_modified": datetime.now(timezone.utc).isoformat()})
    with patch.object(AWSCloudTrailCollector, "_load_conn", return_value=dict(_CONN)), \
         patch.object(AWSCloudTrailCollector, "_s3_client", return_value=fake), \
         patch.object(ct, "_date_prefixes", return_value=[_PREFIX]):
        collected = [ev async for ev in AWSCloudTrailCollector(ctx).collect()]

    assert collected == []


@pytest.mark.asyncio
async def test_caps_objects_per_cycle_and_saves_resumable_position(monkeypatch) -> None:
    """Teto por ciclo + cursor RESUMÍVEL. Com backlog maior que o teto, para no teto e
    grava a POSIÇÃO (prefix + start_after) SEM avançar o watermark — o próximo ciclo
    retoma exatamente daí. Antes o cursor era só um watermark gravado no FIM: um return
    no meio não retomava, e o catch-up drenava tudo num run (soft-timeout → rollback →
    loop sem progresso)."""
    monkeypatch.setattr(ct, "_MAX_OBJECTS_PER_CYCLE", 2)
    now = datetime.now(timezone.utc)
    keys = [f"{_PREFIX}obj{i}.json.gz" for i in range(4)]
    listing = {_PREFIX: {
        "Contents": [{"Key": k, "LastModified": now} for k in keys],
        "IsTruncated": False,
    }}
    fake = _FakeS3(listing, {k: _gz([_event(f"ev-{i}")]) for i, k in enumerate(keys)})

    watermark = datetime(2026, 6, 20, tzinfo=timezone.utc)
    ctx = _ctx(cursor={"last_modified": watermark.isoformat()})
    with patch.object(AWSCloudTrailCollector, "_load_conn", return_value=dict(_CONN)), \
         patch.object(AWSCloudTrailCollector, "_s3_client", return_value=fake), \
         patch.object(ct, "_date_prefixes", return_value=[_PREFIX]):
        collected = [ev async for ev in AWSCloudTrailCollector(ctx).collect()]

    # PAROU no teto: só 2 dos 4 objetos processados.
    assert [e["eventID"] for e in collected] == ["ev-0", "ev-1"]
    # Posição resumível gravada…
    assert ctx.cursor["prefix"] == _PREFIX
    assert ctx.cursor["start_after"] == keys[1]
    # …e o watermark NÃO avançou (senão obj2/obj3 seriam pulados = perda de dado).
    assert ctx.cursor["last_modified"] == watermark.isoformat()


@pytest.mark.asyncio
async def test_resumes_from_saved_position() -> None:
    """Retomada: com cursor de posição, a listagem continua da chave salva (StartAfter)
    em vez de recomeçar o prefixo; ao drenar, o watermark avança e a posição é limpa."""
    now = datetime.now(timezone.utc)
    key = f"{_PREFIX}obj9.json.gz"
    listing = {_PREFIX: {"Contents": [{"Key": key, "LastModified": now}], "IsTruncated": False}}
    fake = _FakeS3(listing, {key: _gz([_event("ev-9")])})
    seen: List[Dict[str, Any]] = []
    _orig = fake.list_objects_v2

    async def _spy(**kw):
        seen.append(kw)
        return await _orig(**kw)

    fake.list_objects_v2 = _spy  # type: ignore[method-assign]

    ctx = _ctx(cursor={
        "last_modified": datetime(2026, 6, 20, tzinfo=timezone.utc).isoformat(),
        "prefix": _PREFIX,
        "start_after": f"{_PREFIX}obj5.json.gz",
    })
    with patch.object(AWSCloudTrailCollector, "_load_conn", return_value=dict(_CONN)), \
         patch.object(AWSCloudTrailCollector, "_s3_client", return_value=fake), \
         patch.object(ct, "_date_prefixes", return_value=[_PREFIX]):
        collected = [ev async for ev in AWSCloudTrailCollector(ctx).collect()]

    assert [e["eventID"] for e in collected] == ["ev-9"]
    assert seen[0].get("StartAfter") == f"{_PREFIX}obj5.json.gz"  # retomou da posição
    assert ctx.cursor == {"last_modified": now.isoformat()}  # drenou → posição limpa


def test_date_prefixes_cover_backlog_not_just_two_days() -> None:
    """Data-gap corrigido: antes só hoje+ontem, então um backlog de >2 dias (cursor
    atrasado/downtime/primeira carga) era silenciosamente PULADO."""
    now = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)
    since = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)  # 4 dias atrás
    out = ct._date_prefixes("B/", since, now)
    assert out[0] == "B/2026/06/16/"   # 1 dia de folga antes do watermark
    assert out[-1] == "B/2026/06/21/"  # até hoje (ordem: antigo → novo)
    assert len(out) == 6
    # cursor MUITO velho não gera lista infinita (teto de lookback).
    ancient = datetime(2020, 1, 1, tzinfo=timezone.utc)
    assert len(ct._date_prefixes("B/", ancient, now)) == ct._MAX_LOOKBACK_DAYS + 1


def test_registered_zero_core_exotic_creds() -> None:
    from ..registry import get, get_platform, has
    from ..capabilities import invalid_capabilities

    assert has("aws_cloudtrail", "events")
    plat = get_platform("aws_cloudtrail")
    assert plat is not None and plat.test_fn is not None
    assert invalid_capabilities(plat.capabilities) == []
    # cred exótica: secret_access_key no store; access_key_id reusa coluna client_id
    assert "secret_access_key" in {f.key for f in plat.auth_fields if f.type == "secret"}
    keys = {f.key for f in plat.auth_fields}
    assert {"client_id", "base_url", "region", "tenant_id"} <= keys  # reuso genérico de colunas
    assert get("aws_cloudtrail", "events").refresh_fn.__name__ == "_cloudtrail_refresher"
