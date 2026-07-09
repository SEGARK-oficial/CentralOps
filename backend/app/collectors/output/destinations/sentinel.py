"""Kind ``sentinel`` — destino Microsoft Sentinel via Logs Ingestion API.

Envia eventos ao **Microsoft Sentinel / Azure Monitor** pela **Logs Ingestion
API** (substituta moderna da HTTP Data Collector API legada), que entrega num
**DCR — Data Collection Rule** através de um **DCE — Data Collection Endpoint**.

Fluxo de duas pernas (ambas HTTP puro com ``aiohttp`` — SEM dependência nova):

1. **OAuth2 client-credentials** (Azure AD / Entra ID): ``POST`` a
   ``https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token`` com
   ``grant_type=client_credentials``, ``client_id``, ``client_secret`` e
   ``scope=https://monitor.azure.com/.default``. Retorna ``access_token`` +
   ``expires_in``; o token é **cacheado** até ``expires_in - 60s`` (renovação
   automática). A app registration (Entra) precisa do papel **Monitoring
   Metrics Publisher** sobre o DCR.
2. **Ingestão**: ``POST`` do **array JSON** de eventos formatados a
   ``{dce_endpoint}/dataCollectionRules/{dcr_immutable_id}/streams/{stream_name}?api-version=2023-01-01``
   com ``Authorization: Bearer <token>`` e ``Content-Type: application/json``.
   ``204``/``200`` → tudo aceito.

**Credencial:** o ``client_secret`` (segredo da app Entra) **não** está na
config — vem de ``secret_ref`` (cofre), decifrado na factory. Sem ele (destino
dormant), ``send_batch``/``test`` falham de forma descritiva sem levantar.

**Entrega at-least-once (NÃO idempotente):** a Logs Ingestion API **não**
deduplica — uma reentrega de lote (após 429/5xx) PODE duplicar registros na
tabela ``_CL`` do workspace. Dedup real é responsabilidade de queries KQL sobre
o ``event_id`` carregado no envelope. Por isso a capability registrada é
``"batch"``/``"test"``/``"tls"`` — **sem** ``"idempotent"``.

Design de sessão: uma ``aiohttp.ClientSession`` persistente, criada **lazily**
no primeiro ``send_batch``/``test`` (nunca no ``__init__`` síncrono sem loop) e
fechada em ``close`` — espelha ``SplunkHecClient``/``ElasticBulkClient``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, List, Mapping, Optional

import aiohttp
from pydantic import BaseModel, Field

from ..base import DeliveryResult, RejectedEvent, TestResult
from .registry import DestinationConfig, DestinationRegistration, register

logger = logging.getLogger(__name__)

KIND = "sentinel"

# Status HTTP de erro transitório no nível do request (retry do lote inteiro).
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# Margem (s) subtraída de ``expires_in`` para renovar o token com folga.
# 120s (não 60s): cobre a latência de um POST de ingestão lento — evita usar um
# token que expira no meio do POST (>60s) → 401 não-retryável → DLQ indevido (TOCTOU).
_TOKEN_REFRESH_SKEW_S = 120.0
# Versão da Logs Ingestion API (fixa — contrato estável do DCR stream).
_INGEST_API_VERSION = "2023-01-01"
# Escopo OAuth2 para Azure Monitor (Logs Ingestion).
_MONITOR_SCOPE = "https://monitor.azure.com/.default"


class SentinelConfig(BaseModel):
    """Schema de config do destino Microsoft Sentinel (Logs Ingestion API).

    Campos expostos no catálogo da UI (``GET /collectors/destination-types``).
    O ``client_secret`` da app Entra **não** está aqui: fica em ``secret_ref``
    (cofre de secrets). ``tenant_id``/``client_id`` identificam a app AAD.
    """

    dce_endpoint: str = Field(
        description="Data Collection Endpoint (ex: https://xxx.ingest.monitor.azure.com)",
    )
    dcr_immutable_id: str = Field(
        description="Immutable ID do Data Collection Rule (ex: dcr-xxxxxxxx)",
    )
    stream_name: str = Field(
        description="Nome do stream do DCR (ex: Custom-CentralOps_CL)",
    )
    tenant_id: str = Field(description="Tenant ID do Azure AD / Entra ID")
    client_id: str = Field(description="Application (client) ID da app Entra")
    verify_tls: bool = Field(default=True, description="Verificar certificado TLS")


def _event_id(event: Mapping[str, Any]) -> str:
    """event_id do namespace ``_centralops`` (para rastreio em rejeições), ou '?'."""
    meta = event.get("_centralops") or {}
    return str(meta.get("event_id") or "?")


class SentinelClient:
    """Cliente Microsoft Sentinel (Logs Ingestion API) com sessão aiohttp persistente.

    Satisfaz o protocolo ``Destination`` diretamente: define ``kind``,
    ``format``, ``send_batch``, ``test`` e ``close``.

    Adquire um token AAD (client-credentials) sob demanda e o **cacheia** até
    ~``expires_in - 60s``; depois posta o array JSON de eventos formatados ao
    stream do DCR. Nunca levanta exceção nos métodos públicos — falhas viram
    ``DeliveryResult.retryable``/``rejected`` ou ``TestResult.failed``.
    """

    kind: str = "sentinel"

    def __init__(
        self,
        *,
        dce_endpoint: str,
        dcr_immutable_id: str,
        stream_name: str,
        tenant_id: str,
        client_id: str,
        client_secret: Optional[str],
        verify_tls: bool = True,
    ) -> None:
        self._dce_endpoint = dce_endpoint.rstrip("/")
        self._dcr_immutable_id = dcr_immutable_id
        self._stream_name = stream_name
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._verify_tls = verify_tls

        self._ingest_url = (
            f"{self._dce_endpoint}/dataCollectionRules/{self._dcr_immutable_id}"
            f"/streams/{self._stream_name}?api-version={_INGEST_API_VERSION}"
        )
        self._token_url = (
            f"https://login.microsoftonline.com/{self._tenant_id}/oauth2/v2.0/token"
        )

        self._session: Optional[aiohttp.ClientSession] = None
        # Cache do token AAD: (access_token, expiry_monotonic).
        self._token: Optional[str] = None
        self._token_expiry: float = 0.0
        # Serializa o refresh do token: o client é cacheado/reusado e vários
        # ``send_batch`` rodam concorrentes (concurrency=8). Sem o lock, N
        # coroutines passam o check do cache e disparam N POSTs simultâneos ao
        # endpoint de token AAD (rate-limit/429, tokens inconsistentes).
        self._token_lock = asyncio.Lock()

    # ── formatação (canônico → wire) ──────────────────────────────────────
    def format(self, envelope: Mapping[str, Any]) -> dict:
        """Item do array JSON enviado ao stream do DCR (canônico → wire dict).

        A Logs Ingestion API recebe um array de objetos; cada envelope vira um
        item. O mapeamento de colunas (``_CL``) é responsabilidade do DCR no
        Azure — aqui devolvemos o envelope inteiro.
        """
        return dict(envelope)

    # ── sessão aiohttp lazy ───────────────────────────────────────────────
    def _build_ssl(self) -> Any:
        """Parâmetro ``ssl`` para aiohttp: ``False`` (sem verificação) ou ``True``."""
        if not self._verify_tls:
            return False
        return True  # verificação padrão do aiohttp

    def _get_session(self) -> aiohttp.ClientSession:
        """Retorna a sessão existente ou cria uma nova (lazy init).

        Criada apenas dentro de um método ``async`` (há loop rodando) — nunca
        no ``__init__`` síncrono. Sem ``Authorization`` global: o header é
        passado por requisição (token rotativo / endpoint de token sem auth).
        """
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=self._build_ssl())
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    # ── OAuth2 client-credentials (com cache) ─────────────────────────────
    async def _acquire_token(self) -> str:
        """Retorna um token AAD válido, do cache ou recém-adquirido.

        Renova quando ausente ou a < ``_TOKEN_REFRESH_SKEW_S`` do vencimento.
        Levanta ``RuntimeError`` em falha (credencial/tenant/permissão) — os
        chamadores (``send_batch``/``test``) capturam e traduzem para resultado.

        Toda a lógica (check do cache + POST + escrita do token) roda sob
        ``self._token_lock`` para que apenas UM refresh ocorra sob concorrência;
        os demais aguardam e, no double-check pós-lock, pegam o token recém-escrito.
        """
        async with self._token_lock:
            now = time.monotonic()
            if self._token is not None and now < self._token_expiry:
                return self._token

            if not self._client_secret:
                raise RuntimeError(
                    "sentinel: client_secret ausente (secret_ref não resolvido) — destino dormant"
                )

            session = self._get_session()
            form = {
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "scope": _MONITOR_SCOPE,
            }
            async with session.post(self._token_url, data=form) as resp:
                status = resp.status
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    body = {}

                if status != 200 or not isinstance(body, dict):
                    err = ""
                    if isinstance(body, dict):
                        err = body.get("error_description") or body.get("error") or ""
                    raise RuntimeError(
                        f"sentinel: falha ao adquirir token AAD (HTTP {status}): {err or 'sem detalhe'}"
                    )

                access_token = body.get("access_token")
                if not access_token:
                    raise RuntimeError("sentinel: resposta de token sem 'access_token'")

                expires_in = float(body.get("expires_in") or 0.0)
                self._token = str(access_token)
                self._token_expiry = (
                    time.monotonic() + max(expires_in - _TOKEN_REFRESH_SKEW_S, 0.0)
                )
                return self._token

    # ── entrega ───────────────────────────────────────────────────────────
    async def send_batch(self, batch: List[Mapping[str, Any]]) -> DeliveryResult:
        """Posta o lote ao stream do DCR; mapeia status HTTP para DeliveryResult.

        - token indisponível (credencial/permissão) → ``rejected`` ``auth``
          (não-retryable) — destino dormant/mal configurado.
        - ``204``/``200`` → lote inteiro aceito.
        - ``401``/``403`` → ``rejected`` ``auth`` (token/permissão), não-retryable
          (invalida o cache → próxima tentativa readquire).
        - ``400`` → ``rejected`` ``schema_rejected``, não-retryable (DLQ por item).
        - ``429``/``5xx`` → ``retryable=True`` (lote re-tentado com backoff).
        Nunca levanta exceção.
        """
        if not batch:
            return DeliveryResult.ok(0)

        try:
            token = await self._acquire_token()
        except RuntimeError as exc:
            logger.warning("sentinel: %s", exc)
            return DeliveryResult(
                accepted=0,
                rejected=[
                    RejectedEvent(
                        event_id=_event_id(ev),
                        reason=str(exc),
                        error_kind="auth",
                        retryable=False,
                    )
                    for ev in batch
                ],
                retryable=False,
            )
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            logger.warning("sentinel: erro de conexão ao endpoint de token: %s", exc)
            return DeliveryResult(accepted=0, retryable=True)

        payload = json.dumps(
            [self.format(ev) for ev in batch],
            separators=(",", ":"),
            default=str,
            ensure_ascii=False,
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        session = self._get_session()
        try:
            async with session.post(self._ingest_url, data=payload, headers=headers) as resp:
                status = resp.status

                if status in (200, 204):
                    return DeliveryResult.ok(len(batch))

                if status in _RETRYABLE_STATUS:
                    logger.warning("sentinel: status transitório %s — retryable", status)
                    return DeliveryResult(accepted=0, retryable=True)

                if status in (401, 403):
                    # Invalida o cache: token expirado/revogado → readquire no retry.
                    self._token = None
                    self._token_expiry = 0.0
                    return DeliveryResult(
                        accepted=0,
                        rejected=[
                            RejectedEvent(
                                event_id=_event_id(ev),
                                reason=f"auth HTTP {status}",
                                error_kind="auth",
                                retryable=False,
                            )
                            for ev in batch
                        ],
                        retryable=False,
                    )

                # 400 (ou outro 4xx determinístico) → schema/payload rejeitado.
                try:
                    body_text = await resp.text()
                except Exception:
                    body_text = ""
                reason = f"HTTP {status}" + (f": {body_text[:300]}" if body_text else "")
                return DeliveryResult(
                    accepted=0,
                    rejected=[
                        RejectedEvent(
                            event_id=_event_id(ev),
                            reason=reason,
                            error_kind="schema_rejected",
                            retryable=False,
                        )
                        for ev in batch
                    ],
                    retryable=False,
                )

        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            logger.warning("sentinel: erro de conexão transitório na ingestão: %s", exc)
            return DeliveryResult(accepted=0, retryable=True)

    async def test(self) -> TestResult:
        """Probe: adquire o token AAD — prova credencial/tenant/app registration.

        Sucesso na aquisição do token já valida o caminho crítico (a app Entra
        existe, o segredo está correto e o tenant é alcançável). Não posta na
        ingestão para não escrever lixo no workspace. Nunca levanta exceção.
        """
        started = time.monotonic()
        try:
            await self._acquire_token()
        except RuntimeError as exc:
            return TestResult.failed(str(exc))
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            return TestResult.failed(f"erro de conexão ao endpoint de token AAD: {exc}")

        latency_ms = (time.monotonic() - started) * 1000.0
        return TestResult.passed(
            f"token AAD adquirido (tenant={self._tenant_id}, dcr={self._dcr_immutable_id})",
            latency_ms=latency_ms,
        )

    async def close(self) -> None:
        """Fecha a sessão aiohttp."""
        if self._session is not None and not self._session.closed:
            try:
                await self._session.close()
            except Exception:  # pragma: no cover — best-effort
                logger.exception("sentinel: erro ao fechar sessão")
            finally:
                self._session = None


def _factory(config: DestinationConfig, secrets: Optional[Any] = None) -> SentinelClient:
    """Constrói um ``SentinelClient`` a partir da config resolvida.

    O ``client_secret`` (segredo da app Entra) é decifrado via
    ``secrets.decrypt(config.secret_ref)`` quando ambos presentes. Ausente
    (destino dormant) → ``client_secret=None``; ``send_batch``/``test`` falham
    de forma descritiva sem levantar aqui (fail-closed controlado).
    """
    cfg = SentinelConfig(**dict(config.config or {}))

    client_secret: Optional[str] = None
    if secrets is not None and config.secret_ref:
        try:
            client_secret = secrets.decrypt(config.secret_ref)
        except Exception as exc:
            # NÃO logar ``secret_ref`` nem ``exc``: a exceção do decrypt pode
            # conter o PATH da master key (KMS) e a estrutura do secret. Só o
            # tipo da exceção — suficiente para diagnóstico, sem vazar credencial.
            logger.warning(
                "sentinel: falha ao decifrar credencial (%s) — client_secret=None (dormant)",
                type(exc).__name__,
            )

    return SentinelClient(
        dce_endpoint=cfg.dce_endpoint,
        dcr_immutable_id=cfg.dcr_immutable_id,
        stream_name=cfg.stream_name,
        tenant_id=cfg.tenant_id,
        client_id=cfg.client_id,
        client_secret=client_secret,
        verify_tls=cfg.verify_tls,
    )


register(
    DestinationRegistration(
        kind=KIND,
        factory=_factory,
        config_schema=SentinelConfig,
        default_queue="dispatch.sentinel",
        # "at_least_once" implícito: a Logs Ingestion API NÃO deduplica — uma
        # reentrega de lote (429/5xx) PODE duplicar registros na tabela _CL.
        # Por isso NÃO declaramos "idempotent": dedup é via KQL sobre event_id.
        capabilities=frozenset({"tls", "batch", "test"}),
        required_secrets=("client_secret",),
        label="Microsoft Sentinel (Logs Ingestion)",
        # HTTP/JSON é paralelizável — concorrência maior por destino.
        delivery_defaults={"concurrency": 8},
        # Campos de catálogo self-describing (galeria de destinos).
        category="SIEM",
        icon_id="microsoftsentinel",
        tier="beta",
        order=30,
        description="Microsoft Sentinel via Logs Ingestion API (DCR/DCE).",
    )
)
