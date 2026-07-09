"""Kind ``chronicle`` — Google SecOps (Chronicle) unstructured log import.

Destino SIEM next-gen (Google SecOps). Envia o evento canônico como log bruto
via a API REST nova ``logs:import`` (substitui a malachite/v1 legada):

    POST https://{region}-chronicle.googleapis.com/v1alpha/projects/{project}/
         locations/{location}/instances/{instance}/logTypes/{log_type}/logs:import

Auth = OAuth2 Bearer de **service account** (JSON em ``secret_ref``), escopo
``cloud-platform``. Cada log entry carrega ``data`` = base64 do payload. Lote
≤ ~4MB / ≤ ~1000 entries (limite da API).

**Mockabilidade:** ``google-auth`` é importado TARDIAMENTE, isolado em
``_load_token()``. Testes sobrescrevem ``_load_token`` (devolve um token fake) e
mockam o ``aiohttp`` via ``aioresponses`` — nunca importam ``google-auth``.
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

KIND = "chronicle"
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)
_NO_SDK_MSG = "google-auth não instalado — instale: pip install -r requirements-sinks.txt"


class ChronicleConfig(BaseModel):
    """Schema de config do destino Chronicle. SA JSON NÃO aqui (vai em secret_ref)."""

    project: str = Field(description="GCP project id da instância Chronicle")
    instance: str = Field(description="Customer/instance id do Chronicle (GUID)")
    region: str = Field(default="us", description="Região (us | europe | asia-southeast1 | ...)")
    location: str = Field(default="us", description="Location do recurso (geralmente = region)")
    log_type: str = Field(default="UDM", description="logType de destino (ex: UDM, OKTA, WINEVTLOG)")
    forwarder: Optional[str] = Field(default=None, description="Nome do forwarder (opcional)")


class ChronicleClient:
    """Sender do Chronicle ``logs:import`` — satisfaz o protocolo ``Destination``."""

    kind: str = KIND

    def __init__(
        self,
        *,
        project: str,
        instance: str,
        region: str = "us",
        location: str = "us",
        log_type: str = "UDM",
        forwarder: Optional[str] = None,
        sa_json: Optional[str] = None,
    ) -> None:
        self._project = project
        self._instance = instance
        self._region = region
        self._location = location
        self._log_type = log_type
        self._forwarder = forwarder
        self._sa_json = sa_json
        self._url = (
            f"https://{region}-chronicle.googleapis.com/v1alpha"
            f"/projects/{project}/locations/{location}/instances/{instance}"
            f"/logTypes/{log_type}/logs:import"
        )
        self._session: Optional[aiohttp.ClientSession] = None

    # ── Credencial / SDK (seam mockável) ─────────────────────────────────

    def _load_token(self) -> str:
        """Token OAuth2 do service account (``google-auth`` importado lazy).

        Ponto de override dos testes: devolvem um token fake sem tocar a lib.
        Em runtime, ausência de ``google-auth`` → ``RuntimeError`` descritivo;
        SA JSON inválido → ``ValueError`` (tratado como auth pelo caller).
        """
        if not self._sa_json:
            raise ValueError("service account JSON ausente (secret_ref)")
        try:
            from google.oauth2 import service_account  # noqa: PLC0415 — lazy p/ mockabilidade
            import google.auth.transport.requests  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover — depende do ambiente
            raise RuntimeError(_NO_SDK_MSG) from exc

        info = json.loads(self._sa_json)
        creds = service_account.Credentials.from_service_account_info(info, scopes=list(_SCOPES))
        creds.refresh(google.auth.transport.requests.Request())
        return creds.token

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    # ── Formatação ───────────────────────────────────────────────────────

    def format(self, envelope: Mapping[str, Any]) -> dict:
        """Canônico → log entry: ``data`` = base64 do OCSF ``normalized`` (JSON)."""
        norm = envelope.get("normalized")
        payload = norm if norm not in (None, {}) else dict(envelope)
        raw = json.dumps(payload, separators=(",", ":"), default=str, ensure_ascii=False)
        return {"data": base64.b64encode(raw.encode("utf-8")).decode("ascii")}

    @staticmethod
    def _event_id(ev: Mapping[str, Any]) -> str:
        return str((ev.get("_centralops") or {}).get("event_id") or "?")

    def _reject_all(self, batch: List[Mapping[str, Any]], reason: str,
                    kind: str, retryable: bool) -> DeliveryResult:
        return DeliveryResult(
            accepted=0,
            rejected=[RejectedEvent(event_id=self._event_id(ev), reason=reason,
                                    error_kind=kind, retryable=retryable) for ev in batch],
            retryable=retryable,
        )

    # ── Entrega ──────────────────────────────────────────────────────────

    async def send_batch(self, batch: List[Mapping[str, Any]]) -> DeliveryResult:
        if not batch:
            return DeliveryResult.ok(0)
        try:
            token = self._load_token()
        except RuntimeError as exc:  # SDK ausente — não-retryable, descritivo
            return self._reject_all(batch, str(exc), "unknown", False)
        except Exception as exc:  # noqa: BLE001 — SA inválido/JSON → auth
            logger.warning("chronicle: falha ao obter token (%s)", type(exc).__name__)
            return self._reject_all(batch, f"auth: {type(exc).__name__}", "auth", False)

        body: dict[str, Any] = {"inline_source": {"logs": [self.format(ev) for ev in batch]}}
        if self._forwarder:
            body["inline_source"]["forwarder"] = self._forwarder
        session = self._get_session()
        try:
            async with session.post(self._url, json=body,
                                    headers={"Authorization": f"Bearer {token}"}) as resp:
                status = resp.status
                if status in _RETRYABLE_STATUS:
                    return DeliveryResult(accepted=0, retryable=True)
                if 200 <= status < 300:
                    return DeliveryResult.ok(len(batch))
                error_kind = "auth" if status in {401, 403} else "schema_rejected"
                return self._reject_all(batch, f"HTTP {status}", error_kind, False)
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            logger.warning("chronicle: erro de conexão transitório: %s", exc)
            return DeliveryResult(accepted=0, retryable=True)

    async def test(self) -> TestResult:
        try:
            token = self._load_token()
        except RuntimeError as exc:
            return TestResult.failed(str(exc))
        except Exception as exc:  # noqa: BLE001
            return TestResult.failed(f"credencial inválida ({type(exc).__name__})")
        session = self._get_session()
        probe = {"inline_source": {"logs": []}}
        try:
            async with session.post(self._url, json=probe,
                                    headers={"Authorization": f"Bearer {token}"}) as resp:
                if resp.status in {401, 403}:
                    return TestResult.failed(f"autenticação rejeitada (HTTP {resp.status})")
                if resp.status in _RETRYABLE_STATUS:
                    return TestResult.failed(f"Chronicle indisponível (HTTP {resp.status})")
                return TestResult.passed(f"Chronicle alcançável (HTTP {resp.status})")
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            return TestResult.failed(f"erro de conexão ao Chronicle: {exc}")

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            try:
                await self._session.close()
            finally:
                self._session = None


def _factory(config: DestinationConfig, secrets: Optional[Any] = None) -> ChronicleClient:
    cfg = ChronicleConfig(**dict(config.config or {}))
    sa_json: Optional[str] = None
    if secrets is not None and config.secret_ref:
        try:
            sa_json = secrets.decrypt(config.secret_ref)
        except Exception as exc:  # noqa: BLE001
            logger.warning("chronicle: falha ao decifrar SA JSON (%s) — dormant", type(exc).__name__)
    return ChronicleClient(
        project=cfg.project, instance=cfg.instance, region=cfg.region, location=cfg.location,
        log_type=cfg.log_type, forwarder=cfg.forwarder, sa_json=sa_json,
    )


register(
    DestinationRegistration(
        kind=KIND,
        factory=_factory,
        config_schema=ChronicleConfig,
        default_queue="dispatch.chronicle",
        capabilities=frozenset({"tls", "batch", "test", "at_least_once"}),
        required_secrets=("service_account_json",),
        label="Google SecOps (Chronicle)",
        delivery_defaults={"concurrency": 4},
        # Campos de catálogo self-describing (galeria de destinos).
        category="SIEM",
        icon_id="chronicle",
        tier="stable",
        order=40,
        description="Google Security Operations (Chronicle) — ingestão de logs unstructured/UDM.",
    )
)
