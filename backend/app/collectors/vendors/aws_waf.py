"""AWS WAF — logs de requisição das web ACLs (WAFv2), lidos onde o WAF os entrega.

O WAF não tem API que devolva o log completo (``GetSampledRequests`` é amostra de
no máximo 500 requisições em 3h). O log só existe depois que o cliente liga o
*logging* da web ACL para UM destino, cujo nome começa com ``aws-waf-logs-``:

- **S3** — ``AWSLogs/<conta>/WAFLogs/<região>/<web-acl>/AAAA/MM/dd/HH/mm/``, arquivos
  ``.log.gz`` de 5 minutos, um JSON por linha. Mais barato com volume alto.
  https://docs.aws.amazon.com/waf/latest/developerguide/logging-s3.html
- **CloudWatch Logs** — um log group; cada log event é um registro JSON.

(O terceiro destino, Firehose, entrega no formato do Firehose e não é lido aqui.)

**Qual destino?** ``base_url`` recebe o destino como o próprio WAF o mostra na
configuração de logging: o ARN (``arn:aws:s3:::aws-waf-logs-x`` ou
``arn:aws:logs:<região>:<conta>:log-group:aws-waf-logs-x``), ou ``s3://bucket[/prefixo]``.
Nome solto é recusado: bucket e log group usam o MESMO prefixo ``aws-waf-logs-``, e
adivinhar errado faria o coletor ler o lugar errado em silêncio.

**S3** reusa o motor do CloudTrail (``AWSCloudTrailCollector``): mesmo cursor
resumível, mesmo teto por ciclo, mesma regra de que falha de leitura derruba o ciclo.
Muda a descoberta (região → web ACL) e o parser (NDJSON). **CloudWatch** reusa o
``AWSCloudWatchCollector`` e só desembrulha o ``message``.

Os dois caminhos entregam o MESMO formato — o registro do WAF — com um bloco
``centralops`` (origem, conta, região, bucket/chave ou log group) para o mapping saber
de onde veio. Normalização: OCSF **HTTP Activity (4002)**.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import re
import time
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Optional

from ..auth import aws as aws_auth
from ..base import BaseCollector
from .aws_cloudtrail import (
    AWSCloudTrailCollector,
    client_region,
    parse_regions,
    split_s3_location,
)
from .aws_cloudwatch import AWSCloudWatchCollector

logger = logging.getLogger(__name__)

MODE_S3 = "s3"
MODE_CLOUDWATCH = "cloudwatch"

_LOGS_ARN_RE = re.compile(
    r"^arn:aws[a-z-]*:logs:([a-z0-9-]+):(\d{12}):log-group:([^:]+)(?::\*)?$"
)
#: ``…/WAFLogs/<região>/<web-acl>/AAAA/MM/dd/HH/mm/<arquivo>``
_WAF_KEY_RE = re.compile(r"/WAFLogs/([^/]+)/([^/]+)/\d{4}/")
#: requestId que a AWS grava quando não há um (exemplos oficiais trazem os dois).
_NO_REQUEST_ID = {"", "-", "null", "None"}


class WafDestinationError(ValueError):
    pass


def parse_destination(value: str) -> Dict[str, str]:
    """Destino de logging da web ACL → ``{"mode", ...}``. Levanta se ambíguo."""
    v = (value or "").strip()
    m = _LOGS_ARN_RE.match(v)
    if m:
        return {
            "mode": MODE_CLOUDWATCH,
            "region": m.group(1),
            "account_id": m.group(2),
            "log_group": m.group(3),
        }
    if v.lower().startswith("s3://") or re.match(r"^arn:aws[a-z-]*:s3:::", v):
        bucket, key_prefix = split_s3_location(v)
        if bucket:
            return {"mode": MODE_S3, "bucket": bucket, "key_prefix": key_prefix}
    raise WafDestinationError(
        "Informe o destino de logging da web ACL como ARN (arn:aws:s3:::aws-waf-logs-… "
        "ou arn:aws:logs:<região>:<conta>:log-group:aws-waf-logs-…) ou s3://bucket."
    )


# ── S3 ────────────────────────────────────────────────────────────────────


class _WafS3Collector(AWSCloudTrailCollector):
    """Motor S3 do CloudTrail com descoberta e parser do WAF. Não é registrado."""

    platform = "aws_waf"
    stream = "events"
    event_type = "aws_waf.event"
    _label = "waf"

    def __init__(self, ctx, conn: Dict[str, Any]) -> None:
        super().__init__(ctx)
        self._conn = conn

    def _load_conn(self) -> Dict[str, Any]:
        return self._conn

    async def _discover_bases(self, s3, conn: Dict[str, Any]) -> List[str]:
        """``WAFLogs/<região>/<web-acl>/`` que existem para a conta.

        Toda web ACL que entrega no bucket entra — o filtro do que logar é a
        configuração de logging do próprio WAF. Região ``cloudfront`` (web ACL
        global) aparece como uma pasta a mais com ``*``."""
        bucket = conn["bucket"]
        root = f"{conn['key_prefix']}AWSLogs/{conn['account_id']}/WAFLogs/"
        if conn["regions"] == ["*"]:
            regions = await self._list_dirs(s3, bucket, root)
        else:
            regions = [f"{root}{r}/" for r in conn["regions"]]
        bases: List[str] = []
        for region_prefix in regions:
            bases.extend(await self._list_dirs(s3, bucket, region_prefix))
        return sorted(set(bases))

    def _parse_object(self, raw: bytes, bucket: str, key: str) -> List[Dict[str, Any]]:
        """``.log.gz`` = um registro JSON por linha. gzip inválido = objeto
        corrompido (levanta); uma linha inválida é descartada sozinha."""
        text = gzip.decompress(raw).decode("utf-8", errors="replace")
        m = _WAF_KEY_RE.search(key)
        region = m.group(1) if m else None
        out: List[Dict[str, Any]] = []
        bad = 0
        for lineno, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if not isinstance(rec, dict):
                bad += 1
                continue
            rec["centralops"] = {
                "source": MODE_S3,
                "bucket": bucket,
                "key": key,
                "line": lineno,
                "region": region,
                "accountId": self._conn.get("account_id"),
            }
            out.append(rec)
        if bad:
            logger.warning(
                "waf: %d linha(s) inválida(s) descartada(s) em s3://%s/%s (integration=%s)",
                bad, bucket, key, self.ctx.integration_id,
            )
        return out


# ── CloudWatch Logs ───────────────────────────────────────────────────────


class _WafCloudWatchCollector(AWSCloudWatchCollector):
    """Motor do CloudWatch Logs com o ``message`` desembrulhado. Não é registrado."""

    platform = "aws_waf"
    stream = "events"
    event_type = "aws_waf.event"

    def __init__(self, ctx, conn: Dict[str, Any]) -> None:
        super().__init__(ctx)
        self._conn = conn

    def _load_conn(self) -> Dict[str, Any]:
        return self._conn

    async def collect(self) -> AsyncIterator[Dict[str, Any]]:
        async for ev in super().collect():
            yield unwrap_cloudwatch_event(ev)


def unwrap_cloudwatch_event(ev: Dict[str, Any]) -> Dict[str, Any]:
    """Log event do CloudWatch → registro do WAF (+ bloco ``centralops``)."""
    meta = {
        "source": MODE_CLOUDWATCH,
        "logGroupName": ev.get("logGroupName"),
        "logStreamName": ev.get("logStreamName"),
        "eventId": ev.get("eventId"),
        "region": ev.get("awsRegion"),
        "accountId": ev.get("awsAccountId"),
    }
    try:
        rec = json.loads(ev.get("message") or "")
    except (TypeError, ValueError):
        rec = None
    if not isinstance(rec, dict):
        # Não é registro do WAF (log group errado?). Sai assim mesmo, marcado:
        # o mapping exige ``timestamp`` e o evento vai para a quarentena, visível.
        rec = {"timestamp": ev.get("timestamp"), "unparsedMessage": ev.get("message")}
    rec["centralops"] = meta
    return rec


def waf_message_id(event: Dict[str, Any]) -> str:
    """Web ACL + requestId + timestamp; sem requestId, a posição na origem.

    O requestId é o ID do serviço protegido (CloudFront, trace do ALB, API
    Gateway) e vem como ``"null"``/``"-"`` em alguns casos — aí vale a chave S3
    + linha ou o ``eventId`` do CloudWatch, que são únicos por registro. Também
    usado pelo recebimento via Firehose (``aws_firehose``)."""
    req = event.get("httpRequest") if isinstance(event.get("httpRequest"), dict) else {}
    rid = str(req.get("requestId") or "")
    if rid not in _NO_REQUEST_ID:
        return f"{event.get('webaclId', '')}|{rid}|{event.get('timestamp', '')}"
    meta = event.get("centralops") if isinstance(event.get("centralops"), dict) else {}
    if meta.get("eventId"):
        return f"cw|{meta['eventId']}"
    if meta.get("key"):
        return f"s3|{meta['key']}|{meta.get('line')}"
    return ""


# ── Coletor registrado ────────────────────────────────────────────────────


class AWSWafCollector(BaseCollector):
    """Escolhe o motor (S3 ou CloudWatch) pelo destino configurado."""

    platform = "aws_waf"
    stream = "events"
    event_type = "aws_waf.event"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._impl: Optional[BaseCollector] = None

    @property
    def domain(self) -> str:
        return self._impl.domain if self._impl is not None else "amazonaws.com"

    def _load_conn(self) -> Dict[str, Any]:
        from ...db import database, models

        with database.SessionLocal() as db:
            integ = db.get(models.Integration, self.ctx.integration_id)
            if integ is None:
                raise RuntimeError(f"waf: integração {self.ctx.integration_id} não encontrada")
            try:
                auth = aws_auth.auth_for_integration(db, integ)
                dest = parse_destination(integ.base_url or "")
            except (aws_auth.AwsAuthError, WafDestinationError) as exc:
                raise RuntimeError(
                    f"waf: integração {self.ctx.integration_id} incompleta — {exc}"
                ) from None
            return build_conn(auth, dest, integ.region, integ.tenant_id)

    def _engine(self, conn: Dict[str, Any]) -> BaseCollector:
        if conn["mode"] == MODE_CLOUDWATCH:
            return _WafCloudWatchCollector(self.ctx, conn)
        return _WafS3Collector(self.ctx, conn)

    async def collect(self) -> AsyncIterator[Dict[str, Any]]:
        conn = await asyncio.to_thread(self._load_conn)
        self._impl = self._engine(conn)
        async for ev in self._impl.collect():
            yield ev

    def extract_message_id(self, event: Dict[str, Any]) -> str:
        return waf_message_id(event)

    @classmethod
    def watermark_at(cls, cursor: Optional[Dict[str, Any]]) -> Optional[datetime]:
        """Cursor do motor ativo: ``start_time_ms`` (CloudWatch, epoch ms) ou
        ``last_modified`` (S3, ISO)."""
        if cursor and "start_time_ms" in cursor:
            return cls.watermark_from_epoch_ms(cursor, "start_time_ms")
        return cls.watermark_from_iso(cursor, "last_modified")


def build_conn(auth, dest: Dict[str, str], region: Optional[str], tenant_id: Optional[str]) -> Dict[str, Any]:
    account_id = (tenant_id or "").strip() or dest.get("account_id") or (
        aws_auth.role_account_id(auth.role_arn) or ""
    )
    if dest["mode"] == MODE_CLOUDWATCH:
        return {
            "mode": MODE_CLOUDWATCH,
            "auth": auth,
            "log_group": dest["log_group"],
            "region": dest["region"],
            "account_id": account_id,
        }
    if not account_id:
        raise RuntimeError("waf: informe o Account ID (com chave fixa ele não sai do papel)")
    regions = parse_regions(region, default=("*",))
    return {
        "mode": MODE_S3,
        "auth": auth,
        "bucket": dest["bucket"],
        "key_prefix": dest["key_prefix"],
        "account_id": account_id,
        "regions": regions,
        "region": client_region(regions),
    }


# ── Self-registration ─────────────────────────────────────────────────────


async def _waf_refresher(integration_id: int) -> Dict[str, object]:
    """No-op p/ o framework: credencial AWS (papel ou chave), não OAuth."""
    return {"access_token": "", "expires_in": 3600}


async def _waf_probe(cfg: Dict[str, Any]):
    """Teste pré-save: com papel, assume o papel; depois lê 1 item do destino."""
    from ..output.base import TestResult

    try:
        dest = parse_destination(cfg.get("base_url") or "")
    except WafDestinationError as exc:
        return TestResult.failed(str(exc))
    t0 = time.perf_counter()
    try:
        auth = await asyncio.to_thread(aws_auth.auth_for_probe, cfg)
        region = dest.get("region") or client_region(
            parse_regions(cfg.get("region"), default=("*",))
        )
        client_kwargs = await aws_auth.client_kwargs(auth, region)
    except aws_auth.AwsAuthError as exc:
        return TestResult.failed(str(exc))
    how = "papel assumido" if auth.mode == aws_auth.MODE_ROLE else "chave fixa"
    try:
        import aioboto3  # noqa: PLC0415

        if dest["mode"] == MODE_CLOUDWATCH:
            async with aioboto3.Session().client("logs", **client_kwargs) as logs:
                await logs.filter_log_events(logGroupName=dest["log_group"], limit=1)
            what = "log group"
        else:
            async with aioboto3.Session().client("s3", **client_kwargs) as s3:
                await s3.list_objects_v2(
                    Bucket=dest["bucket"], Prefix=f"{dest['key_prefix']}AWSLogs/", MaxKeys=1
                )
            what = "bucket"
        ms = (time.perf_counter() - t0) * 1000.0
        return TestResult.passed(f"Conexão OK ({how}) — acesso ao {what} validado.", latency_ms=ms)
    except Exception as exc:  # noqa: BLE001
        op = "FilterLogEvents" if dest["mode"] == MODE_CLOUDWATCH else "ListObjectsV2"
        return TestResult.failed(
            f"Falha ao ler o destino dos logs: {aws_auth.describe_error(exc, operation=op)}"
        )


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
            platform="aws_waf",
            display_name="AWS WAF",
            category="Rede / Firewall",
            description=(
                "AWS WAF — log de cada requisição avaliada pelas web ACLs (ação, regra, "
                "IP, país, URI), lido do S3 ou do CloudWatch Logs."
            ),
            icon_id="aws",
            docs_url="https://docs.aws.amazon.com/waf/latest/developerguide/logging-fields.html",
            order=52,
            auth_setup="aws_assume_role",
            test_fn=_waf_probe,
            required_secrets=(),
            capabilities=frozenset({"catalog", "auth:test", "collect:events"}),
            auth_fields=(
                AuthField(key="client_id", label="ARN do papel ou Access Key ID", type="string",
                          required=True,
                          help_text="Recomendado: o ARN do papel criado pelo template "
                                    "(arn:aws:iam::<conta>:role/CentralOpsReadOnly)."),
                AuthField(key="secret_access_key", label="Secret Access Key", type="secret",
                          required=False,
                          help_text="Só com Access Key ID. Deixe vazio ao usar papel."),
                AuthField(key="base_url", label="Destino dos logs do WAF", type="string",
                          required=True,
                          help_text="Como aparece na configuração de logging da web ACL: "
                                    "arn:aws:s3:::aws-waf-logs-… (ou s3://bucket/prefixo) ou "
                                    "arn:aws:logs:<região>:<conta>:log-group:aws-waf-logs-…"),
                AuthField(key="region", label="Regiões AWS", type="string", required=False,
                          help_text="Só para S3: uma região, várias separadas por vírgula, ou "
                                    "vazio/* para todas (inclui cloudfront). No CloudWatch, a "
                                    "região vem do ARN."),
                AuthField(key="tenant_id", label="Account ID", type="string", required=False,
                          help_text="Conta dona dos logs (compõe AWSLogs/<conta>/WAFLogs/). "
                                    "Vazio = a conta do ARN do papel ou do log group."),
            ),
        )
    )

    register(
        CollectorRegistration(
            platform=AWSWafCollector.platform,
            stream=AWSWafCollector.stream,
            collector_cls=AWSWafCollector,
            refresh_fn=_waf_refresher,
            schedule=_td(minutes=5),
            queue=Q_BULK,
            task_name=T_COLLECT_BULK,
        )
    )


_register()
