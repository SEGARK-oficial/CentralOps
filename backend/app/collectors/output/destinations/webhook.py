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
from typing import Any, Dict, List, Literal, Mapping, Optional

import aiohttp
from pydantic import BaseModel, Field, field_validator, model_validator

from ..base import DeliveryResult, RejectedEvent, TestResult
from ..payload_shape import (
    DESCRICAO as PAYLOAD_DESCRICAO,
    DESCRICAO_EVENT_KEY,
    DESCRICAO_ROW_FIELDS,
    DESCRICAO_ROW_FIELDS_FROM,
    DESCRICAO_ROW_SHAPE,
    PayloadShape,
    RowFieldSource,
    RowShape,
    render_row,
)
from .registry import DestinationConfig, DestinationRegistration, register

logger = logging.getLogger(__name__)

KIND = "webhook"
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class WebhookConfig(BaseModel):
    """Schema de config do destino Webhook (catálogo da UI). Credencial NÃO aqui."""

    url: str = Field(description="URL HTTP de destino (POST)")
    method: str = Field(default="POST", description="Método HTTP (POST|PUT)")
    # Literal, e não ``str``, para o JSON Schema sair com ``enum``: a UI
    # renderiza Select só quando encontra enum, e com ``str`` isto virava
    # caixa de texto livre. Um typo ("Bearer", "berer") passava pelo
    # Pydantic e caía no ramo neutro de ``_auth_header``, que devolve {}:
    # a autenticação sumia sem erro nenhum, e o destino respondia 401.
    auth_mode: Literal["none", "bearer", "basic"] = Field(
        default="none",
        description="Como autenticar: sem autenticação, Bearer token ou Basic",
    )
    # ``Literal`` pelo mesmo motivo de ``auth_mode``: é o que faz o JSON Schema
    # sair com ``enum`` e a UI renderizar Select em vez de caixa de texto, que é
    # o que impede um valor novo errado de nascer.
    #
    # O validador ``mode="before"`` existe para NÃO quebrar destino já gravado:
    # esta config é revalidada a cada entrega, na fábrica, não só no save. Um
    # ``wrap`` fora da lista (a caixa de texto antiga aceitava qualquer coisa)
    # hoje cai em array silenciosamente; se o ``Literal`` passasse a levantar, a
    # atualização derrubaria a entrega desse destino em vez de corrigi-la. O
    # normalizador mantém o comportamento antigo e registra no log.
    wrap: Literal["array", "ndjson"] = Field(
        default="array", description="Formato do corpo do lote: array JSON ou NDJSON"
    )
    payload: Optional[PayloadShape] = Field(
        default=None,
        description=PAYLOAD_DESCRICAO,
    )
    row_shape: RowShape = Field(default="flat", description=DESCRICAO_ROW_SHAPE)
    event_key: str = Field(default="event", description=DESCRICAO_EVENT_KEY)
    # ``Dict[str, str]`` e não ``dict`` cru: é o que faz o JSON Schema sair com
    # ``additionalProperties: {"type": "string"}``. O ``headers`` ao lado é dict
    # cru por histórico, e o resultado é ``additionalProperties: true``; as duas
    # formas caem no mesmo editor de pares na UI, mas só a tipada recusa um
    # valor não-textual antes de virar corpo de requisição.
    row_fields: Dict[str, str] = Field(default_factory=dict, description=DESCRICAO_ROW_FIELDS)
    row_fields_from: Dict[str, RowFieldSource] = Field(
        default_factory=dict, description=DESCRICAO_ROW_FIELDS_FROM
    )

    @field_validator("wrap", mode="before")
    @classmethod
    def _normaliza_wrap(cls, valor: Any) -> Any:
        if not isinstance(valor, str):
            return valor
        v = valor.strip().lower()
        if v in {"array", "ndjson"}:
            return v
        logger.warning(
            "webhook: wrap=%r fora da lista — usando 'array' (comportamento anterior)", valor
        )
        return "array"

    @model_validator(mode="after")
    def _barreiras(self) -> "WebhookConfig":
        if self.row_shape == "wrapped":
            if not self.event_key.strip():
                raise ValueError("com row_shape='wrapped', event_key é obrigatório")
        elif self.row_fields or self.row_fields_from:
            raise ValueError(
                "row_fields/row_fields_from só têm efeito com row_shape='wrapped'; hoje "
                "seriam ignorados em silêncio. Mude row_shape para 'wrapped' ou limpe-os"
            )
        for chave in (*self.row_fields, *self.row_fields_from):
            if not str(chave).strip():
                raise ValueError("row_fields tem uma coluna sem nome")
            if chave == self.event_key:
                raise ValueError(
                    f"row_fields não pode redefinir a chave do evento ({self.event_key!r})"
                )
        return self
    #: Nome histórico de ``payload``, que aceitava "normalized". Continua
    #: declarado para que config já gravada siga valendo: se ele sumisse do
    #: schema, o Pydantic descartaria a chave e todo destino configurado com
    #: "normalized" voltaria a mandar o envelope, em silêncio.
    body: Optional[str] = Field(default=None, deprecated=True, description="Use payload.")
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
        row_shape: str = "flat",
        event_key: str = "event",
        row_fields: Optional[Mapping[str, str]] = None,
        row_fields_from: Optional[Mapping[str, str]] = None,
        headers: Optional[dict] = None,
        verify_tls: bool = True,
        secret: Optional[str] = None,
    ) -> None:
        self._url = url
        self._method = (method or "POST").upper()
        self._auth_mode = auth_mode
        self._wrap = wrap
        self._body = body
        self._row_shape = row_shape
        self._event_key = event_key
        self._row_fields = dict(row_fields or {})
        self._row_fields_from = dict(row_fields_from or {})
        self._extra_headers = dict(headers or {})
        self._verify_tls = verify_tls
        self._secret = secret
        self._session: Optional[aiohttp.ClientSession] = None

    def format(self, envelope: Mapping[str, Any]) -> Any:
        """Canônico → wire.

        ``body``/``payload`` decide o conteúdo (envelope canônico ou OCSF puro);
        ``row_shape`` decide a forma (o conteúdo no topo, ou aninhado sob
        ``event_key`` com as chaves de ``row_fields`` ao lado). Serve endpoint
        que espera o evento embrulhado, incluindo um Vector configurado assim.
        """
        return render_row(
            envelope,
            payload=self._body,
            row_shape=self._row_shape,
            event_key=self._event_key,
            row_fields=self._row_fields,
            row_fields_from=self._row_fields_from,
        )

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
        # ``payload`` vence; ``body`` é o nome histórico e cobre config antiga.
        body=cfg.payload or cfg.body,
        row_shape=cfg.row_shape, event_key=cfg.event_key, row_fields=cfg.row_fields,
        row_fields_from=cfg.row_fields_from,
        headers=cfg.headers, verify_tls=cfg.verify_tls, secret=secret,
    )


register(
    DestinationRegistration(
        kind=KIND,
        factory=_factory,
        config_schema=WebhookConfig,
        default_queue="dispatch.webhook",
        capabilities=frozenset({"tls", "batch", "test", "at_least_once"}),
        required_secrets=(),
        # Aceita credencial sem exigir: com ``auth_mode`` em bearer ou
        # basic o segredo é necessário, com "none" não. Ver a nota em
        # ``DestinationRegistration.optional_secrets``.
        optional_secrets=("auth_token",),
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
