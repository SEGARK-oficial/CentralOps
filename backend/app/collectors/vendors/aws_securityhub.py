"""AWS Security Hub — achados no formato ASFF (``GetFindings``).

O Security Hub é o agregador de achados da AWS: GuardDuty, Inspector, Macie, IAM
Access Analyzer, Config e os controles do próprio Security Hub (CSPM) chegam aqui
no mesmo formato (AWS Security Finding Format). Ligado o *cross-Region aggregation*,
a região de agregação devolve os achados de todas — basta ela no campo de regiões.

API: ``GetFindings`` com ``Filters.UpdatedAt`` (janela fechada), ordenado por
``UpdatedAt`` ascendente, 100 por página.
https://docs.aws.amazon.com/securityhub/1.0/APIReference/API_GetFindings.html

Normalização: OCSF **Detection Finding (2004)**. Volume: os controles CSPM geram
achados ``PASSED``/``INFORMATIONAL`` em massa; eles entram com ``is_alert=false``
para a rota decidir — o coletor não descarta nada.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from ..auth import aws as aws_auth
from .aws_cloudtrail import client_region, parse_regions
from .aws_findings_base import AwsRegionalFindingsCollector, iso

_PAGE_SIZE = 100


class AWSSecurityHubCollector(AwsRegionalFindingsCollector):
    platform = "aws_securityhub"
    stream = "findings"
    event_type = "aws_securityhub.finding"
    _service = "securityhub"
    _label = "securityhub"

    async def _fetch_page(
        self, client, region: str, start: datetime, end: datetime, token: Optional[str]
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        kwargs: Dict[str, Any] = {
            "Filters": {"UpdatedAt": [{"Start": iso(start), "End": iso(end)}]},
            "SortCriteria": [{"Field": "UpdatedAt", "SortOrder": "asc"}],
            "MaxResults": _PAGE_SIZE,
        }
        if token:
            kwargs["NextToken"] = token
        resp = await client.get_findings(**kwargs)
        return list(resp.get("Findings") or []), resp.get("NextToken") or None


# ── Self-registration ─────────────────────────────────────────────────────


async def _refresher(integration_id: int) -> Dict[str, object]:
    return {"access_token": "", "expires_in": 3600}


async def _probe(cfg: Dict[str, Any]):
    """Assume o papel (se for papel) e lê 1 achado da primeira região."""
    from ..output.base import TestResult

    regions = parse_regions(cfg.get("region"), default=("us-east-1",))
    region = client_region(regions)
    t0 = time.perf_counter()
    try:
        auth = await asyncio.to_thread(aws_auth.auth_for_probe, cfg)
        kwargs = await aws_auth.client_kwargs(auth, region)
    except aws_auth.AwsAuthError as exc:
        return TestResult.failed(str(exc))
    try:
        import aioboto3  # noqa: PLC0415

        async with aioboto3.Session().client("securityhub", **kwargs) as sh:
            await sh.get_findings(MaxResults=1)
        ms = (time.perf_counter() - t0) * 1000.0
        how = "papel assumido" if auth.mode == aws_auth.MODE_ROLE else "chave fixa"
        return TestResult.passed(f"Conexão OK ({how}) — Security Hub respondeu em {region}.", latency_ms=ms)
    except Exception as exc:  # noqa: BLE001
        msg = aws_auth.describe_error(exc, operation="GetFindings")
        if "InvalidAccessException" in msg:
            msg += f" O Security Hub não está habilitado em {region}."
        return TestResult.failed(f"Falha ao ler achados: {msg}")


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
            platform="aws_securityhub",
            display_name="AWS Security Hub",
            category="Cloud-audit",
            description=(
                "AWS Security Hub — achados agregados (GuardDuty, Inspector, Macie, Access "
                "Analyzer, controles CSPM) no formato ASFF."
            ),
            icon_id="aws",
            docs_url="https://docs.aws.amazon.com/securityhub/1.0/APIReference/API_GetFindings.html",
            order=53,
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
                          help_text="Com agregação entre regiões ligada, só a região de "
                                    "agregação. Sem ela: lista separada por vírgula ou * para "
                                    "todas as habilitadas na conta."),
            ),
        )
    )
    register(
        CollectorRegistration(
            platform=AWSSecurityHubCollector.platform,
            stream=AWSSecurityHubCollector.stream,
            collector_cls=AWSSecurityHubCollector,
            refresh_fn=_refresher,
            schedule=timedelta(minutes=5),
            queue=Q_BULK,
            task_name=T_COLLECT_BULK,
        )
    )


_register()
