"""Kind ``webhook`` — saída HTTP genérica.

Destino UNIVERSAL: faz POST dos eventos para qualquer endpoint HTTP, sem plugin
dedicado por serviço (espelha ``to_http`` do Tenzir / "Generic Webhook" do Axoflow).
Maior cobertura de cauda-longa com menor esforço; habilita SOAR/ad-hoc.

Auth opcional via ``secret_ref`` (bearer token ou ``user:pass`` para Basic). Sem
SDK externo (aiohttp puro). ``send_batch`` devolve ``DeliveryResult`` sem levantar:
5xx/429 → retryable; 401/403 → ``auth``; demais 4xx → ``schema_rejected``.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any, List, Mapping, Optional

import aiohttp
from pydantic import BaseModel, Field

from ..base import DeliveryResult, RejectedEvent, TestResult
from .registry import DestinationConfig, DestinationRegistration, register

logger = logging.getLogger(__name__)

KIND = "webhook"
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class WebhookConfig(BaseModel):
    """Schema de config do destino Webhook (catálogo da UI). Credencial NÃO aqui."""

    url: str = Field(description="URL HTTP de destino (POST)")
    method: str = Field(default="POST", description="Método HTTP (POST|PUT)")
    auth_mode: str = Field(default="none", description="none | bearer | basic")
    wrap: str = Field(default="array", description="array | ndjson — formato do corpo do lote")
    body: str = Field(default="envelope", description="envelope | normalized — o que enviar por evento")
    headers: dict = Field(default_factory=dict, description="Headers extras (ex: X-Api-Key)")
    verify_tls: bool = Field(default=True, description="Verificar certificado TLS")


class WebhookClient:
    """Sender HTTP genérico — satisfaz o protocolo ``Destination``."""

    kind: str = KIND

    def __init__(
        self,
        url: str,
        *,
        method: str = "POST",
        auth_mode: str = "none",
        wrap: str = "array",
        body: str = "envelope",
        headers: Optional[dict] = None,
        verify_tls: bool = True,
        secret: Optional[str] = None,
    ) -> None:
        self._url = url
        self._method = (method or "POST").upper()
        self._auth_mode = auth_mode
        self._wrap = wrap
        self._body = body
        self._extra_headers = dict(headers or {})
        self._verify_tls = verify_tls
        self._secret = secret
        self._session: Optional[aiohttp.ClientSession] = None

    def format(self, envelope: Mapping[str, Any]) -> dict:
        """Canônico → wire: envelope inteiro ou só o OCSF ``normalized``."""
        if self._body == "normalized":
            return dict(envelope.get("normalized") or {})
        return dict(envelope)

    def _auth_header(self) -> dict:
        if self._auth_mode == "bearer" and self._secret:
            return {"Authorization": f"Bearer {self._secret}"}
        if self._auth_mode == "basic" and self._secret:
            token = base64.b64encode(self._secret.encode("utf-8")).decode("ascii")
            return {"Authorization": f"Basic {token}"}
        return {}

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {"Content-Type": "application/json", **self._extra_headers, **self._auth_header()}
            connector = aiohttp.TCPConnector(ssl=None if self._verify_tls else False)
            self._session = aiohttp.ClientSession(headers=headers, connector=connector)
        return self._session

    def _serialize(self, batch: List[Mapping[str, Any]]) -> str:
        items = [self.format(ev) for ev in batch]
        if self._wrap == "ndjson":
            return "\n".join(json.dumps(it, separators=(",", ":"), default=str, ensure_ascii=False) for it in items)
        return json.dumps(items, separators=(",", ":"), default=str, ensure_ascii=False)

    @staticmethod
    def _event_id(ev: Mapping[str, Any]) -> str:
        return str((ev.get("_centralops") or {}).get("event_id") or "?")

    async def send_batch(self, batch: List[Mapping[str, Any]]) -> DeliveryResult:
        if not batch:
            return DeliveryResult.ok(0)
        payload = self._serialize(batch)
        session = self._get_session()
        try:
            async with session.request(self._method, self._url, data=payload) as resp:
                status = resp.status
                if status in _RETRYABLE_STATUS:
                    return DeliveryResult(accepted=0, retryable=True)
                if 200 <= status < 300:
                    return DeliveryResult.ok(len(batch))
                error_kind = "auth" if status in {401, 403} else "schema_rejected"
                reason = f"HTTP {status}"
                return DeliveryResult(
                    accepted=0,
                    rejected=[
                        RejectedEvent(event_id=self._event_id(ev), reason=reason,
                                      error_kind=error_kind, retryable=False)
                        for ev in batch
                    ],
                    retryable=False,
                )
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            logger.warning("webhook: erro de conexão transitório: %s", exc)
            return DeliveryResult(accepted=0, retryable=True)

    async def test(self) -> TestResult:
        """Probe: POST de um array vazio. 2xx/4xx = alcançável; 401/403 = auth."""
        session = self._get_session()
        try:
            async with session.request(self._method, self._url, data="[]") as resp:
                if resp.status in {401, 403}:
                    return TestResult.failed(f"autenticação rejeitada (HTTP {resp.status})")
                if resp.status in _RETRYABLE_STATUS:
                    return TestResult.failed(f"endpoint indisponível (HTTP {resp.status})")
                return TestResult.passed(f"endpoint alcançável (HTTP {resp.status})")
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            return TestResult.failed(f"erro de conexão: {exc}")

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            try:
                await self._session.close()
            finally:
                self._session = None


def _factory(config: DestinationConfig, secrets: Optional[Any] = None) -> WebhookClient:
    cfg = WebhookConfig(**dict(config.config or {}))
    secret: Optional[str] = None
    if secrets is not None and config.secret_ref:
        try:
            secret = secrets.decrypt(config.secret_ref)
        except Exception as exc:  # noqa: BLE001
            logger.warning("webhook: falha ao decifrar credencial (%s) — sem auth", type(exc).__name__)
    return WebhookClient(
        url=cfg.url, method=cfg.method, auth_mode=cfg.auth_mode, wrap=cfg.wrap,
        body=cfg.body, headers=cfg.headers, verify_tls=cfg.verify_tls, secret=secret,
    )


register(
    DestinationRegistration(
        kind=KIND,
        factory=_factory,
        config_schema=WebhookConfig,
        default_queue="dispatch.webhook",
        capabilities=frozenset({"tls", "batch", "test", "at_least_once"}),
        required_secrets=(),  # auth é opcional
        label="Generic Webhook",
        delivery_defaults={"concurrency": 8},
        # Campos de catálogo self-describing (galeria de destinos).
        category="Webhook",
        icon_id="webhook",
        tier="generic",
        order=120,
        description="Webhook HTTP genérico — POST de JSON/NDJSON para qualquer endpoint.",
    )
)
