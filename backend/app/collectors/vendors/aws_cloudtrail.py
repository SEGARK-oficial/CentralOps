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

# Teto de OBJETOS S3 processados por ciclo. Cada objeto custa GET + gzip.decompress +
# json.loads (pesado), então o teto é por objeto, não por página de listagem. Sem ele,
# um catch-up drena a janela inteira num run e estoura o ``task_soft_time_limit`` (720s)
# → o pipeline reverte o cursor → loop sem progresso. Ao atingir, gravamos a POSIÇÃO
# (prefixo + última chave) e retomamos no próximo ciclo.
_MAX_OBJECTS_PER_CYCLE = 500

# Teto de dias de prefixo gerados num ciclo. Evita lista ilimitada de prefixos quando o
# cursor está MUITO atrasado (o watermark avança a cada ciclo até alcançar o presente).
_MAX_LOOKBACK_DAYS = 14


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
        # Retomada MID-BACKLOG (teto do ciclo anterior): continua no MESMO prefixo, a
        # partir da última chave processada. Sem isto o cursor era só um watermark
        # gravado no FIM — um return no meio não retomava (daí o redesenho).
        resume_prefix: Optional[str] = cursor.get("prefix") or None
        resume_after: Optional[str] = cursor.get("start_after") or None

        # Prefixos de dia do watermark até hoje (antes: só hoje+ontem → um backlog de
        # >2 dias era silenciosamente PULADO). Do mais ANTIGO p/ o mais novo, p/ drenar
        # cronologicamente e a posição de retomada fazer sentido.
        prefixes = _date_prefixes(conn["prefix_base"], last_mod, datetime.now(timezone.utc))
        if resume_prefix and resume_prefix in prefixes:
            prefixes = prefixes[prefixes.index(resume_prefix):]  # pula os já drenados

        objects_done = 0
        async with self._s3_client(conn) as s3:
            for prefix in prefixes:
                # ``StartAfter`` (chave, lexicográfico) em vez de ContinuationToken
                # (opaco/expirável): é o que torna a posição PERSISTÍVEL e resumível.
                start_after: Optional[str] = resume_after if prefix == resume_prefix else None
                resume_after = None  # só vale p/ o primeiro prefixo retomado
                while True:
                    await self.ctx.rate_limiter.acquire(self.ctx.integration_id, self.platform)
                    list_kwargs: Dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
                    if start_after:
                        list_kwargs["StartAfter"] = start_after

                    started = time.monotonic()
                    async with self.ctx.domain_limiter.slot(self.domain):
                        resp = await s3.list_objects_v2(**list_kwargs)
                    API_LATENCY.labels(vendor=self.platform, stream=self.stream).observe(
                        time.monotonic() - started
                    )

                    contents = resp.get("Contents", []) or []
                    if not contents:
                        break
                    for obj in contents:
                        key = obj["Key"]
                        lm = obj.get("LastModified")
                        start_after = key  # avança SEMPRE (mesmo se pulado) → progresso
                        if lm is None or lm <= overlap:
                            continue
                        records = await self._read_records(s3, bucket, key)
                        for ev in records:
                            yield ev
                        if lm > latest:
                            latest = lm
                        objects_done += 1
                        if self.ctx.bounded_per_cycle and objects_done >= _MAX_OBJECTS_PER_CYCLE:
                            # Teto do ciclo: grava a POSIÇÃO e retoma no próximo ciclo.
                            # O watermark NÃO avança — senão a retomada pularia os
                            # objetos ainda não lidos desta janela (perda de dado).
                            self.ctx.cursor = {
                                "last_modified": last_mod.isoformat(),
                                "prefix": prefix,
                                "start_after": key,
                            }
                            logger.info(
                                "cloudtrail: teto de %d objetos/ciclo — retomando em "
                                "prefix=%s start_after=%s (integration=%s)",
                                _MAX_OBJECTS_PER_CYCLE, prefix, key, self.ctx.integration_id,
                            )
                            return

                    if not resp.get("IsTruncated"):
                        break

        # Drenou a janela inteira: só AQUI o watermark avança (e a posição é limpa).
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


def _date_prefixes(base_prefix: str, since: datetime, now: datetime) -> List[str]:
    """Prefixos de dia (UTC) do watermark até hoje, do mais ANTIGO p/ o mais novo.

    A partição CloudTrail é por data de ENTREGA. Antes gerávamos só hoje+ontem, então um
    backlog de mais de 2 dias (cursor atrasado, downtime, primeira carga) era
    silenciosamente PULADO — data-gap. Agora cobrimos do watermark até hoje, com 1 dia de
    folga p/ entrega atrasada e teto de ``_MAX_LOOKBACK_DAYS`` (o watermark avança a cada
    ciclo até alcançar o presente, então um cursor muito velho converge sem lista infinita).
    """
    start = min(since, now) - timedelta(days=1)  # folga p/ entrega atrasada
    days = (now.date() - start.date()).days
    days = max(0, min(days, _MAX_LOOKBACK_DAYS))
    return [
        f"{base_prefix}{d.year:04d}/{d.month:02d}/{d.day:02d}/"
        for d in (now - timedelta(days=delta) for delta in range(days, -1, -1))
    ]


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
