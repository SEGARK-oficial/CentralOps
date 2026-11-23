"""AWS CloudTrail pelo Event history (``LookupEvents``): auditoria SEM bucket.

O ``aws_cloudtrail`` lê o bucket S3 de uma trail. Este coletor serve à conta que
não tem trail, ou ao cliente que não quer dar acesso ao bucket: o CloudTrail guarda
os management events dos últimos 90 dias de cada região, sem configurar nada, e a
API ``LookupEvents`` os devolve. Basta a permissão ``cloudtrail:LookupEvents``.
https://docs.aws.amazon.com/awscloudtrail/latest/APIReference/API_LookupEvents.html

**O que vem e o que NÃO vem.** Vêm os management events, de leitura e de escrita:
IAM, STS, login no console, EC2/VPC/security group, KMS, Organizations e também as
chamadas de inferência do Bedrock no endpoint ``bedrock-runtime`` (``InvokeModel``,
``InvokeModelWithResponseStream``, ``Converse``, ``ConverseStream``), que a AWS
registra como management event. NÃO vêm os data events (``s3:GetObject``, invoke de
Lambda, ``InvokeAgent``, inferência no endpoint ``bedrock-mantle``), nem Insights,
nem o conteúdo dos prompts. Para isso é preciso uma trail (``aws_cloudtrail``) ou o
model invocation logging do Bedrock (``aws_cloudwatch``).

**Limites da API.**

- 2 chamadas por segundo, por conta e por região. Ao passar disso a AWS devolve
  ``ThrottlingException``. O coletor espaça as chamadas (``_MIN_INTERVAL_S``) e o
  retry do botocore absorve o resto.
- Até 50 eventos por página, do mais NOVO para o mais antigo. O ``NextToken`` só vale
  com os mesmos parâmetros da primeira chamada: a janela do ciclo é FECHADA.
- Por região. Eventos de serviço global (IAM, STS global, login no console) caem em
  ``us-east-1``: inclua essa região, ou use ``*``.

**Motor.** O mesmo dos achados (``aws_findings_base``): janela fechada por região,
sobreposição para o evento que aparece atrasado no Event history, teto por ciclo
com posição exata (janela + token) e watermark que só avança com a janela drenada.
O dedupe é por ``eventID``, o mesmo do bucket.

**Registro.** Cada item da API traz o evento completo em ``CloudTrailEvent`` (string
JSON), no MESMO formato de um registro do bucket. Por isso o mapping é o do
``aws_cloudtrail`` (OCSF API Activity 6003) e uma regra que já funciona para a trail
funciona aqui. O registro ganha um bloco ``centralops`` com a origem e a região.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from ..auth import aws as aws_auth
from .aws_cloudtrail import _error_code, client_region, parse_regions
from .aws_findings_base import AwsRegionalFindingsCollector, iso, parse_ts

logger = logging.getLogger(__name__)

_PAGE_SIZE = 50  # máximo do LookupEvents
#: Intervalo mínimo entre duas chamadas (limite da AWS: 2/s por conta e região).
_MIN_INTERVAL_S = 0.55
#: 400 páginas × 50 = até 20 mil eventos por ciclo, ~220 s no ritmo acima: cabe com
#: folga no soft-timeout do ciclo (720 s).
_MAX_PAGES = 400

SOURCE = "lookup_events"


class AWSCloudTrailLookupCollector(AwsRegionalFindingsCollector):
    platform = "aws_cloudtrail_lookup"
    stream = "events"
    event_type = "aws_cloudtrail_lookup.event"
    _service = "cloudtrail"
    _label = "cloudtrail_lookup"
    _max_pages_per_cycle = _MAX_PAGES

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._last_call = 0.0

    async def _pace(self) -> None:
        wait = _MIN_INTERVAL_S - (time.monotonic() - self._last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_call = time.monotonic()

    async def _lookup(self, client, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        await self._pace()
        return await client.lookup_events(**kwargs)

    async def _fetch_page(
        self, client, region: str, start: datetime, end: datetime, token: Optional[str]
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        kwargs: Dict[str, Any] = {"StartTime": start, "EndTime": end, "MaxResults": _PAGE_SIZE}
        if token:
            kwargs["NextToken"] = token
        try:
            resp = await self._lookup(client, kwargs)
        except Exception as exc:  # noqa: BLE001 — reclassificado abaixo
            if token and _error_code(exc) == "InvalidNextTokenException":
                # Token de um ciclo capado que a AWS não aceita mais. Recomeça a MESMA
                # janela do início: o watermark não andou, o dedupe absorve o relido.
                logger.warning(
                    "%s: NextToken recusado em %s — relendo a janela (integration=%s)",
                    self._label, region, self.ctx.integration_id,
                )
                kwargs.pop("NextToken")
                try:
                    resp = await self._lookup(client, kwargs)
                except Exception as retry_exc:  # noqa: BLE001
                    raise self._error(region, retry_exc) from None
            else:
                raise self._error(region, exc) from None
        records = [to_record(item, region) for item in resp.get("Events") or []]
        return [r for r in records if r], resp.get("NextToken") or None

    @staticmethod
    def _error(region: str, exc: BaseException) -> RuntimeError:
        return RuntimeError(
            f"cloudtrail_lookup: LookupEvents em {region} falhou — "
            f"{aws_auth.describe_error(exc, operation='LookupEvents')}"
        )

    def _updated_at(self, item: Dict[str, Any]) -> Optional[datetime]:
        return parse_ts(item.get("eventTime"))

    def extract_message_id(self, event: Dict[str, Any]) -> str:
        return str(event.get("eventID") or "")


def to_record(item: Dict[str, Any], region: str) -> Optional[Dict[str, Any]]:
    """Item do ``LookupEvents`` → registro CloudTrail (o mesmo formato do bucket).

    O registro vem inteiro em ``CloudTrailEvent``. Se faltar ou não for JSON, o
    evento NÃO some: é montado dos campos do topo do item, que a API sempre traz.
    """
    raw = item.get("CloudTrailEvent")
    record: Optional[Dict[str, Any]] = None
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
            record = parsed if isinstance(parsed, dict) else None
        except ValueError:
            record = None
    if record is None:
        if not item.get("EventId"):
            return None
        logger.warning(
            "cloudtrail_lookup: evento %s sem CloudTrailEvent legível — registro mínimo",
            item.get("EventId"),
        )
        when = item.get("EventTime")
        record = {
            "eventID": item.get("EventId"),
            "eventName": item.get("EventName"),
            "eventSource": item.get("EventSource"),
            "eventTime": iso(when) if isinstance(when, datetime) else when,
            "awsRegion": region,
            "userIdentity": {"userName": item.get("Username")} if item.get("Username") else {},
            "readOnly": str(item.get("ReadOnly", "")).lower() == "true",
        }
    record.setdefault("awsRegion", region)
    record["centralops"] = {"source": SOURCE, "region": region}
    return record


# ── Self-registration ─────────────────────────────────────────────────────


async def _refresher(integration_id: int) -> Dict[str, object]:
    return {"access_token": "", "expires_in": 3600}


async def _probe(cfg: Dict[str, Any]):
    """AssumeRole (ou chave) + 1 evento do Event history: prova credencial E permissão."""
    from ..output.base import TestResult

    region = client_region(parse_regions(cfg.get("region"), default=("us-east-1",)))
    t0 = time.perf_counter()
    try:
        auth = await asyncio.to_thread(aws_auth.auth_for_probe, cfg)
        kwargs = await aws_auth.client_kwargs(auth, region)
    except aws_auth.AwsAuthError as exc:
        return TestResult.failed(str(exc))
    try:
        import aioboto3  # noqa: PLC0415

        async with aioboto3.Session().client("cloudtrail", **kwargs) as ct:
            await ct.lookup_events(MaxResults=1)
        ms = (time.perf_counter() - t0) * 1000.0
        how = "papel assumido" if auth.mode == aws_auth.MODE_ROLE else "chave fixa"
        return TestResult.passed(
            f"Conexão OK ({how}) — Event history do CloudTrail em {region}.", latency_ms=ms
        )
    except Exception as exc:  # noqa: BLE001
        return TestResult.failed(
            "Falha ao consultar o Event history do CloudTrail: "
            f"{aws_auth.describe_error(exc, operation='LookupEvents')}"
        )


def _register() -> None:
    from datetime import timedelta

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
            platform="aws_cloudtrail_lookup",
            display_name="AWS CloudTrail — Event history",
            category="Cloud-audit",
            description="AWS CloudTrail sem bucket — management events dos últimos 90 dias "
                        "pela API LookupEvents (IAM, login, rede, KMS, Bedrock).",
            icon_id="aws",
            docs_url="https://docs.aws.amazon.com/awscloudtrail/latest/userguide/view-cloudtrail-events.html",
            order=50,
            auth_setup="aws_assume_role",
            test_fn=_probe,
            required_secrets=(),
            capabilities=frozenset({"catalog", "auth:test", "collect:events"}),
            auth_fields=(
                AuthField(key="client_id", label="ARN do papel ou Access Key ID", type="string",
                          required=True,
                          help_text="Recomendado: o ARN do papel criado pelo template "
                                    "(arn:aws:iam::<conta>:role/CentralOpsReadOnly). "
                                    "Precisa de cloudtrail:LookupEvents."),
                AuthField(key="secret_access_key", label="Secret Access Key", type="secret",
                          required=False, help_text="Só com Access Key ID."),
                AuthField(key="region", label="Regiões AWS", type="string", required=True,
                          help_text="Lista separada por vírgula ou * para todas as habilitadas "
                                    "na conta. IAM, STS e login no console ficam em us-east-1: "
                                    "inclua essa região."),
            ),
        )
    )
    register(
        CollectorRegistration(
            platform=AWSCloudTrailLookupCollector.platform,
            stream=AWSCloudTrailLookupCollector.stream,
            collector_cls=AWSCloudTrailLookupCollector,
            refresh_fn=_refresher,
            schedule=timedelta(minutes=5),
            queue=Q_BULK,
            task_name=T_COLLECT_BULK,
        )
    )


_register()
