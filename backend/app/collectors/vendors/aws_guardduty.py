"""Amazon GuardDuty — achados de ameaça direto da API do GuardDuty.

Para quem não usa o Security Hub (que já agrega o GuardDuty). Fluxo por região:
``ListDetectors`` (um detector por região) → ``ListFindings`` filtrando ``updatedAt``
na janela, ordenado ascendente, 50 IDs por página → ``GetFindings`` desses IDs.
https://docs.aws.amazon.com/guardduty/latest/APIReference/API_ListFindings.html

Região sem GuardDuty habilitado (sem detector) é pulada sem erro: é configuração
comum ter o serviço só em algumas regiões e listar ``*``.

Normalização: OCSF **Detection Finding (2004)**. Severidade do GuardDuty (número):
1.0–3.9 Low, 4.0–6.9 Medium, 7.0–8.9 High, 9.0+ Critical.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from ..auth import aws as aws_auth
from .aws_cloudtrail import client_region, parse_regions
from .aws_findings_base import AwsRegionalFindingsCollector

logger = logging.getLogger(__name__)

_PAGE_SIZE = 50  # máximo do ListFindings e do GetFindings


class AWSGuardDutyCollector(AwsRegionalFindingsCollector):
    platform = "aws_guardduty"
    stream = "findings"
    event_type = "aws_guardduty.finding"
    _service = "guardduty"
    _label = "guardduty"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._detectors: Dict[str, Optional[str]] = {}

    async def _detector(self, client, region: str) -> Optional[str]:
        if region not in self._detectors:
            resp = await client.list_detectors()
            ids = resp.get("DetectorIds") or []
            self._detectors[region] = ids[0] if ids else None
            if not ids:
                logger.info(
                    "guardduty: sem detector em %s (integration=%s) — região pulada",
                    region, self.ctx.integration_id,
                )
        return self._detectors[region]

    async def _fetch_page(
        self, client, region: str, start: datetime, end: datetime, token: Optional[str]
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        detector = await self._detector(client, region)
        if detector is None:
            return [], None
        kwargs: Dict[str, Any] = {
            "DetectorId": detector,
            "FindingCriteria": {
                "Criterion": {
                    "updatedAt": {
                        "GreaterThanOrEqual": int(start.timestamp() * 1000),
                        "LessThanOrEqual": int(end.timestamp() * 1000),
                    }
                }
            },
            "SortCriteria": {"AttributeName": "updatedAt", "OrderBy": "ASC"},
            "MaxResults": _PAGE_SIZE,
        }
        if token:
            kwargs["NextToken"] = token
        listed = await client.list_findings(**kwargs)
        ids = listed.get("FindingIds") or []
        next_token = listed.get("NextToken") or None
        if not ids:
            return [], next_token
        got = await client.get_findings(
            DetectorId=detector,
            FindingIds=ids,
            SortCriteria={"AttributeName": "updatedAt", "OrderBy": "ASC"},
        )
        findings = list(got.get("Findings") or [])
        for f in findings:
            f.setdefault("centralops", {"detectorId": detector, "region": region})
        return findings, next_token


# ── Self-registration ─────────────────────────────────────────────────────


async def _refresher(integration_id: int) -> Dict[str, object]:
    return {"access_token": "", "expires_in": 3600}


async def _probe(cfg: Dict[str, Any]):
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

        async with aioboto3.Session().client("guardduty", **kwargs) as gd:
            resp = await gd.list_detectors()
        if not resp.get("DetectorIds"):
            return TestResult.failed(f"Credencial OK, mas o GuardDuty não está habilitado em {region}.")
        ms = (time.perf_counter() - t0) * 1000.0
        how = "papel assumido" if auth.mode == aws_auth.MODE_ROLE else "chave fixa"
        return TestResult.passed(f"Conexão OK ({how}) — detector do GuardDuty em {region}.", latency_ms=ms)
    except Exception as exc:  # noqa: BLE001
        return TestResult.failed(
            f"Falha ao consultar o GuardDuty: {aws_auth.describe_error(exc, operation='ListDetectors')}"
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
            platform="aws_guardduty",
            display_name="Amazon GuardDuty",
            category="Cloud-audit",
            description="Amazon GuardDuty — achados de ameaça (contas, EC2, S3, EKS, RDS) pela API.",
            icon_id="aws",
            docs_url="https://docs.aws.amazon.com/guardduty/latest/ug/guardduty_finding-format.html",
            order=54,
            auth_setup="aws_assume_role",
            test_fn=_probe,
            required_secrets=(),
            capabilities=frozenset({"catalog", "auth:test", "collect:findings"}),
            auth_fields=(
                AuthField(key="client_id", label="ARN do papel ou Access Key ID", type="string",
                          required=True,
                          help_text="Recomendado: o ARN do papel criado pelo template "
                                    "(arn:aws:iam::<conta>:role/CentralOpsReadOnly)."),
                AuthField(key="secret_access_key", label="Secret Access Key", type="secret",
                          required=False, help_text="Só com Access Key ID."),
                AuthField(key="region", label="Regiões AWS", type="string", required=True,
                          help_text="Lista separada por vírgula ou * para todas as habilitadas "
                                    "na conta. Região sem GuardDuty é pulada."),
            ),
        )
    )
    register(
        CollectorRegistration(
            platform=AWSGuardDutyCollector.platform,
            stream=AWSGuardDutyCollector.stream,
            collector_cls=AWSGuardDutyCollector,
            refresh_fn=_refresher,
            schedule=timedelta(minutes=5),
            queue=Q_BULK,
            task_name=T_COLLECT_BULK,
        )
    )


_register()
