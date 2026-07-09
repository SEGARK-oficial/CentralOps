"""Kind ``datadog`` — Datadog Logs Intake.

Destino de observabilidade: rotear eventos para o Datadog, ampliando o alcance
além de casos de uso puramente de SOC/SIEM.

Envio: ``POST https://http-intake.logs.{site}/api/v2/logs`` com header
``DD-API-KEY`` (via ``secret_ref``), corpo = array JSON de log entries. Sem SDK
(aiohttp puro). 202 Accepted = ok; 5xx/429 → retryable; 401/403 → ``auth``;
413/4xx → ``schema_rejected``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, List, Mapping, Optional

import aiohttp
from pydantic import BaseModel, Field

from ..base import DeliveryResult, RejectedEvent, TestResult
from .registry import DestinationConfig, DestinationRegistration, register

logger = logging.getLogger(__name__)

KIND = "datadog"
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class DatadogConfig(BaseModel):
    """Schema de config do destino Datadog. API key NÃO aqui (vai em secret_ref)."""

    site: str = Field(default="datadoghq.com",
                      description="Site Datadog (datadoghq.com | datadoghq.eu | us3.datadoghq.com | ...)")
    service: str = Field(default="centralops", description="Campo `service` do Datadog")
    ddsource: str = Field(default="centralops", description="Campo `ddsource` (parser)")
    tags: Optional[str] = Field(default=None, description="`ddtags` (ex: env:prod,team:soc)")


class DatadogClient:
    """Sender do Datadog Logs Intake — satisfaz o protocolo ``Destination``."""

    kind: str = KIND

    def __init__(
        self,
        *,
        site: str = "datadoghq.com",
        service: str = "centralops",
        ddsource: str = "centralops",
        tags: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> None:
        self._url = f"https://http-intake.logs.{site.strip().lstrip('.')}/api/v2/logs"
        self._service = service
        self._ddsource = ddsource
        self._tags = tags
        self._api_key = api_key
        self._session: Optional[aiohttp.ClientSession] = None

    def format(self, envelope: Mapping[str, Any]) -> dict:
        """Canônico → log entry do Datadog (OCSF aninhado em ``ocsf``)."""
        norm = dict(envelope.get("normalized") or {})
        entry: dict[str, Any] = {
            "ddsource": self._ddsource,
            "service": self._service,
            "message": norm.get("message") or norm.get("class_name") or "centralops event",
            "ocsf": norm,
        }
        if self._tags:
            entry["ddtags"] = self._tags
        return entry

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {"Content-Type": "application/json"}
            if self._api_key:
                headers["DD-API-KEY"] = self._api_key
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    @staticmethod
    def _event_id(ev: Mapping[str, Any]) -> str:
        return str((ev.get("_centralops") or {}).get("event_id") or "?")

    async def send_batch(self, batch: List[Mapping[str, Any]]) -> DeliveryResult:
        if not batch:
            return DeliveryResult.ok(0)
        if not self._api_key:
            return DeliveryResult(
                accepted=0,
                rejected=[RejectedEvent(event_id=self._event_id(ev), reason="DD-API-KEY ausente",
                                        error_kind="auth", retryable=False) for ev in batch],
                retryable=False,
            )
        payload = json.dumps([self.format(ev) for ev in batch], separators=(",", ":"),
                             default=str, ensure_ascii=False)
        session = self._get_session()
        try:
            async with session.post(self._url, data=payload) as resp:
                status = resp.status
                if status in _RETRYABLE_STATUS:
                    return DeliveryResult(accepted=0, retryable=True)
                if 200 <= status < 300:
                    return DeliveryResult.ok(len(batch))
                error_kind = "auth" if status in {401, 403} else "schema_rejected"
                return DeliveryResult(
                    accepted=0,
                    rejected=[RejectedEvent(event_id=self._event_id(ev), reason=f"HTTP {status}",
                                            error_kind=error_kind, retryable=False) for ev in batch],
                    retryable=False,
                )
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            logger.warning("datadog: erro de conexão transitório: %s", exc)
            return DeliveryResult(accepted=0, retryable=True)

    async def test(self) -> TestResult:
        if not self._api_key:
            return TestResult.failed("DD-API-KEY ausente (secret_ref)")
        session = self._get_session()
        probe = json.dumps([{"ddsource": self._ddsource, "service": self._service, "message": "centralops probe"}])
        try:
            async with session.post(self._url, data=probe) as resp:
                if resp.status in {401, 403}:
                    return TestResult.failed("DD-API-KEY inválida (401/403)")
                if 200 <= resp.status < 300:
                    return TestResult.passed(f"Datadog ok ({self._url})")
                return TestResult.failed(f"Datadog respondeu HTTP {resp.status}")
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            return TestResult.failed(f"erro de conexão ao Datadog: {exc}")

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            try:
                await self._session.close()
            finally:
                self._session = None


def _factory(config: DestinationConfig, secrets: Optional[Any] = None) -> DatadogClient:
    cfg = DatadogConfig(**dict(config.config or {}))
    api_key: Optional[str] = None
    if secrets is not None and config.secret_ref:
        try:
            api_key = secrets.decrypt(config.secret_ref)
        except Exception as exc:  # noqa: BLE001
            logger.warning("datadog: falha ao decifrar API key (%s) — dormant", type(exc).__name__)
    return DatadogClient(site=cfg.site, service=cfg.service, ddsource=cfg.ddsource,
                         tags=cfg.tags, api_key=api_key)


register(
    DestinationRegistration(
        kind=KIND,
        factory=_factory,
        config_schema=DatadogConfig,
        default_queue="dispatch.datadog",
        capabilities=frozenset({"tls", "batch", "test", "at_least_once"}),
        required_secrets=("api_key",),
        label="Datadog (Logs)",
        delivery_defaults={"concurrency": 8},
        # Campos de catálogo self-describing (galeria de destinos).
        category="Observabilidade",
        icon_id="datadog",
        tier="stable",
        order=50,
        description="Datadog Logs intake via HTTP — coleta e correlação de logs.",
    )
)
