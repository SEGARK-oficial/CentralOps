"""AWS CloudTrail — collector de cloud-audit via polling de bucket S3.

Vendor novo = 1 módulo, ZERO core. Cloud-audit é categoria canônica ausente;
CloudTrail é a fonte cloud #1. Exercita o caminho de **creds exóticas** (IAM key)
sobre o capability/credential model.

**Creds (reuso GENÉRICO das colunas — client_id/base_url/tenant_id/region são
genéricos):** ``client_id`` = Access Key ID (público), ``secret_access_key`` no store,
``base_url`` = nome do bucket S3, ``region`` = região AWS, ``tenant_id`` = Account ID
(compõe o prefixo). Sem coluna nova → zero-core. (Limite: org trails têm o OU ID no
path — config single-account; documentar.)

**Coleta:** ``aioboto3`` (import lazy, modelo do destino ``s3.py``) — ``list_objects_v2``
sob o prefixo ``AWSLogs/<account>/CloudTrail/<region>/<YYYY>/<MM>/<DD>/`` (hoje+ontem
UTC, pois a partição é por hora de ENTREGA), baixa cada ``.json.gz``, descomprime e
emite cada item de ``Records[]``. Cursor: ``LastModified`` do último objeto + overlap
(~15min); dedupe por ``eventID`` (a AWS endossa: duplicados têm o mesmo eventID).
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, List, Optional

from ..base import BaseCollector
from ..metrics import API_LATENCY
import time

logger = logging.getLogger(__name__)


class AWSCloudTrailCollector(BaseCollector):
    """Pull de eventos CloudTrail de um bucket S3 (aioboto3)."""

    platform = "aws_cloudtrail"
    stream = "events"
    event_type = "aws_cloudtrail.event"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._region: Optional[str] = None

    @property
    def domain(self) -> str:
        return f"s3.{self._region}.amazonaws.com" if self._region else "s3.amazonaws.com"

    def _load_conn(self) -> Dict[str, Any]:
        """Creds exóticas (reuso genérico de colunas) + config do bucket (sync, em thread)."""
        from ...db import database, models
        from ...services import integration_secrets

        with database.SessionLocal() as db:
            integ = db.get(models.Integration, self.ctx.integration_id)
            if integ is None:
                raise RuntimeError(f"cloudtrail: integração {self.ctx.integration_id} não encontrada")
            access_key_id = (integ.client_id or "").strip()
            secret = integration_secrets.read_secret(integ, "secret_access_key") or ""
            bucket = (integ.base_url or "").strip()
            region = (integ.region or "us-east-1").strip()
            account_id = (integ.tenant_id or "").strip()
            if not access_key_id or not secret or not bucket or not account_id:
                raise RuntimeError(
                    f"cloudtrail: integração {self.ctx.integration_id} incompleta "
                    "(precisa de Access Key ID, Secret Access Key, bucket e Account ID)"
                )
            return {
                "access_key_id": access_key_id,
                "secret": secret,
                "bucket": bucket,
                "region": region,
                "prefix_base": f"AWSLogs/{account_id}/CloudTrail/{region}/",
            }

    def _s3_client(self, conn: Dict[str, Any]):
        """Seam mockável (modelo s3.py): import lazy + client aioboto3."""
        import aioboto3  # noqa: PLC0415 — import tardio proposital (mockabilidade)

        return aioboto3.Session().client(
            "s3",
            aws_access_key_id=conn["access_key_id"],
            aws_secret_access_key=conn["secret"],
            region_name=conn["region"],
        )

    async def collect(self) -> AsyncIterator[Dict[str, Any]]:
        conn = await asyncio.to_thread(self._load_conn)
        self._region = conn["region"]
        bucket = conn["bucket"]

        cursor = self.ctx.cursor or {}
        last_mod = _parse_iso(cursor.get("last_modified")) or _default_lookback_dt()
        overlap = last_mod - timedelta(minutes=15)  # cobre entrega atrasada / ts empatado
        latest = last_mod

        prefixes = _date_prefixes(conn["prefix_base"], datetime.now(timezone.utc))

        async with self._s3_client(conn) as s3:
            for prefix in prefixes:
                token: Optional[str] = None
                while True:
                    await self.ctx.rate_limiter.acquire(self.ctx.integration_id, self.platform)
                    list_kwargs: Dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
                    if token:
                        list_kwargs["ContinuationToken"] = token

                    started = time.monotonic()
                    async with self.ctx.domain_limiter.slot(self.domain):
                        resp = await s3.list_objects_v2(**list_kwargs)
                    API_LATENCY.labels(vendor=self.platform, stream=self.stream).observe(
                        time.monotonic() - started
                    )

                    for obj in resp.get("Contents", []) or []:
                        lm = obj.get("LastModified")
                        if lm is None or lm <= overlap:
                            continue
                        records = await self._read_records(s3, bucket, obj["Key"])
                        for ev in records:
                            yield ev
                        if lm > latest:
                            latest = lm

                    if not resp.get("IsTruncated"):
                        break
                    token = resp.get("NextContinuationToken")

        self.ctx.cursor = {"last_modified": latest.isoformat()}

    async def _read_records(self, s3, bucket: str, key: str) -> List[Dict[str, Any]]:
        """Baixa o objeto ``.json.gz``, descomprime e devolve ``Records[]``. Objeto
        corrompido/ilegível é pulado (não derruba o ciclo)."""
        try:
            got = await s3.get_object(Bucket=bucket, Key=key)
            async with got["Body"] as body:
                raw = await body.read()
            payload = json.loads(gzip.decompress(raw))
        except Exception as exc:  # noqa: BLE001
            logger.warning("cloudtrail: objeto %s ilegível (%s) — pulando", key, type(exc).__name__)
            return []
        return payload.get("Records") or []

    def extract_message_id(self, event: Dict[str, Any]) -> str:
        return str(event.get("eventID") or "")


def _default_lookback_dt() -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=2)


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _date_prefixes(base_prefix: str, now: datetime) -> List[str]:
    """Hoje + ontem (UTC) — a partição CloudTrail é por data de ENTREGA."""
    out = []
    for delta in (0, 1):
        d = now - timedelta(days=delta)
        out.append(f"{base_prefix}{d.year:04d}/{d.month:02d}/{d.day:02d}/")
    return out


# ── Self-registration (sem OAuth — IAM key) ────────────────────────────


async def _cloudtrail_refresher(integration_id: int) -> Dict[str, object]:
    """No-op p/ o framework: CloudTrail usa IAM key (assinatura SigV4 no boto3),
    não OAuth. O collector lê a key/secret do store no collect()."""
    return {"access_token": "", "expires_in": 3600}


async def _cloudtrail_probe(cfg: Dict[str, Any]):
    """Teste STATELESS pré-save: lista 1 objeto do bucket com as creds digitadas."""
    from ..output.base import TestResult

    bucket = (cfg.get("base_url") or "").strip()
    access_key_id = (cfg.get("client_id") or "").strip()
    secret = (cfg.get("secret_access_key") or "").strip()
    region = (cfg.get("region") or "us-east-1").strip()
    if not bucket or not access_key_id or not secret:
        return TestResult.failed("Informe Access Key ID, Secret Access Key e bucket.")
    t0 = time.perf_counter()
    try:
        import aioboto3  # noqa: PLC0415

        session = aioboto3.Session()
        async with session.client(
            "s3", aws_access_key_id=access_key_id, aws_secret_access_key=secret, region_name=region
        ) as s3:
            await s3.list_objects_v2(Bucket=bucket, MaxKeys=1)
        ms = (time.perf_counter() - t0) * 1000.0
        return TestResult.passed("Conexão OK — acesso ao bucket validado.", latency_ms=ms)
    except Exception as exc:  # noqa: BLE001
        return TestResult.failed(f"Falha ao acessar o bucket: {type(exc).__name__}")


def _register() -> None:
    from datetime import timedelta as _td

    from ..queues import Q_BULK, T_COLLECT_BULK
    from ..registry import (
        AuthField,
        CollectorRegistration,
        PlatformRegistration,
        register,
        register_platform,
    )

    register_platform(
        PlatformRegistration(
            platform="aws_cloudtrail",
            display_name="AWS CloudTrail",
            category="Cloud-audit",
            description="AWS CloudTrail — eventos de API/management (polling do bucket S3).",
            icon_id="aws",
            docs_url="https://docs.aws.amazon.com/awscloudtrail/latest/userguide/cloudtrail-event-reference-record-contents.html",
            order=50,
            test_fn=_cloudtrail_probe,
            required_secrets=("secret_access_key",),
            capabilities=frozenset({"catalog", "auth:test", "collect:events"}),
            auth_fields=(
                AuthField(key="client_id", label="Access Key ID", type="string", required=True,
                          help_text="IAM Access Key ID (público) com leitura no bucket CloudTrail"),
                AuthField(key="secret_access_key", label="Secret Access Key", type="secret", required=True),
                AuthField(key="base_url", label="Bucket S3", type="string", required=True,
                          help_text="Nome do bucket onde o CloudTrail entrega os logs"),
                AuthField(key="region", label="Região AWS", type="string", required=True,
                          help_text="Ex: us-east-1"),
                AuthField(key="tenant_id", label="Account ID", type="string", required=True,
                          help_text="AWS Account ID (compõe o prefixo AWSLogs/<account>/CloudTrail/)"),
            ),
        )
    )

    register(
        CollectorRegistration(
            platform=AWSCloudTrailCollector.platform,
            stream=AWSCloudTrailCollector.stream,
            collector_cls=AWSCloudTrailCollector,
            refresh_fn=_cloudtrail_refresher,
            schedule=_td(minutes=5),
            queue=Q_BULK,
            task_name=T_COLLECT_BULK,
        )
    )


_register()
