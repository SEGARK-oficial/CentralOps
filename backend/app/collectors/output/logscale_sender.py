"""Cliente HEC da família LogScale — base de ``crowdstrike_logscale`` e
``crowdstrike_ngsiem``.

CrowdStrike Falcon LogScale (ex-Humio) e o CrowdStrike Falcon Next-Gen SIEM
(construído sobre o LogScale) ingerem dados de terceiros via um endpoint
**HEC-compatível** (HTTP Event Collector): NDJSON de ``{"event": <evento>}`` com
``Authorization: Bearer <ingest-token>``. Diferente do Splunk HEC (que usa o
esquema ``Splunk <token>``), a família LogScale usa **Bearer**.

Referências:
- LogScale HEC:  https://library.humio.com/integrations/ingesting-hec.html
- CrowdStrike NG-SIEM (third-party via HEC connector)

Design idêntico ao ``SplunkHecClient``: sessão ``aiohttp`` persistente lazy,
NDJSON, ``DeliveryResult`` por lote com **fallback individual** (E2) para isolar
poison events quando o lote inteiro é rejeitado com 4xx determinístico. O
``kind`` é injetado no construtor porque dois destinos compartilham este cliente.
"""

from __future__ import annotations

import json
import logging
from typing import Any, List, Mapping, Optional

import aiohttp

from .base import DeliveryResult, RejectedEvent, TestResult

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# Limite de eventos no fallback individual (E2): lotes maiores são rejeitados no
# atacado em vez de explodir em N POSTs.
_MAX_INDIVIDUAL_FALLBACK = 50


def format_hec_event(
    envelope: Mapping[str, Any],
    *,
    sourcetype: Optional[str] = None,
    source: Optional[str] = None,
) -> dict:
    """Embala um envelope canônico no wrapper HEC da família LogScale.

    ``{"event": <envelope>}`` com ``sourcetype``/``source`` opcionais. Expõe o
    ``event_id`` do namespace ``_centralops`` em ``fields._centralops_event_id``
    (dedup no lado do índice — LogScale/NG-SIEM não fazem dedup nativo no ingest).
    """
    wrapper: dict[str, Any] = {"event": dict(envelope)}
    if sourcetype is not None:
        wrapper["sourcetype"] = sourcetype
    if source is not None:
        wrapper["source"] = source
    meta = envelope.get("_centralops") or {}
    event_id = meta.get("event_id")
    if event_id:
        wrapper["fields"] = {"_centralops_event_id": str(event_id)}
    return wrapper


class LogScaleHecClient:
    """Cliente HEC Bearer (LogScale / CrowdStrike NG-SIEM).

    Satisfaz o protocolo ``Destination`` diretamente. O ``endpoint`` é a URL
    completa de ingestão HEC fornecida pelo console (ex.:
    ``https://cloud.community.humio.com/api/v1/ingest/hec`` para LogScale, ou a
    URL do conector HEC do NG-SIEM).
    """

    def __init__(
        self,
        endpoint: str,
        token: Optional[str],
        *,
        kind: str = "logscale_hec",
        sourcetype: Optional[str] = None,
        source: Optional[str] = None,
        verify_tls: bool = True,
        ca_bundle: Optional[str] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.kind = kind
        self._url = endpoint.rstrip("/")
        self._token = token
        self._sourcetype = sourcetype
        self._source = source
        self._verify_tls = verify_tls
        self._ca_bundle = ca_bundle
        self._extra_headers = dict(extra_headers or {})
        self._session: Optional[aiohttp.ClientSession] = None

    # ── Wire ──────────────────────────────────────────────────────────────
    def format(self, envelope: Mapping[str, Any]) -> dict:
        return format_hec_event(envelope, sourcetype=self._sourcetype, source=self._source)

    def _serialize_event(self, ev: Mapping[str, Any]) -> str:
        return json.dumps(self.format(ev), separators=(",", ":"), default=str, ensure_ascii=False)

    @staticmethod
    def _event_id(event: Mapping[str, Any]) -> str:
        meta = event.get("_centralops") or {}
        return str(meta.get("event_id") or "?")

    # ── Sessão ────────────────────────────────────────────────────────────
    def _build_ssl(self) -> Any:
        if not self._verify_tls:
            return False
        if self._ca_bundle:
            import ssl

            ctx = ssl.create_default_context(cafile=self._ca_bundle)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            return ctx
        return True

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {"Content-Type": "application/json"}
            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"
            headers.update(self._extra_headers)
            connector = aiohttp.TCPConnector(ssl=self._build_ssl())
            self._session = aiohttp.ClientSession(headers=headers, connector=connector)
        return self._session

    # ── Entrega ───────────────────────────────────────────────────────────
    async def _send_single(self, ev: Mapping[str, Any], session: Any) -> tuple[bool, bool, str, str]:
        """Envia um evento. Retorna ``(accepted, retryable, reason, error_kind)``. Nunca levanta."""
        try:
            async with session.post(self._url, data=self._serialize_event(ev)) as resp:
                status = resp.status
                if status in _RETRYABLE_STATUS:
                    return False, True, f"HTTP {status} transitório", "unknown"
                if 200 <= status < 300:
                    return True, False, "", ""
                error_kind = "auth" if status in {401, 403} else "schema_rejected"
                return False, False, f"HTTP {status}", error_kind
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            return False, True, str(exc), "unknown"

    async def _fallback_individual(
        self, batch: List[Mapping[str, Any]], reason: str, error_kind: str
    ) -> DeliveryResult:
        if len(batch) > _MAX_INDIVIDUAL_FALLBACK:
            logger.error(
                "%s: lote grande demais para fallback individual (%d > %d) — rejeitado no atacado",
                self.kind, len(batch), _MAX_INDIVIDUAL_FALLBACK,
            )
            return DeliveryResult(
                accepted=0,
                rejected=[
                    RejectedEvent(event_id=self._event_id(ev), reason=reason, error_kind=error_kind, retryable=False)
                    for ev in batch
                ],
                retryable=False,
            )
        session = self._get_session()
        accepted = 0
        rejected: list[RejectedEvent] = []
        any_retryable = False
        logger.info("%s: fallback individual para %d eventos (isolamento E2)", self.kind, len(batch))
        for ev in batch:
            ok, retryable, why, kind = await self._send_single(ev, session)
            if ok:
                accepted += 1
            elif retryable:
                any_retryable = True
            else:
                rejected.append(
                    RejectedEvent(event_id=self._event_id(ev), reason=why, error_kind=kind, retryable=False)
                )
        if any_retryable:
            return DeliveryResult(accepted=accepted, retryable=True)
        return DeliveryResult(accepted=accepted, rejected=rejected, retryable=False)

    async def send_batch(self, batch: List[Mapping[str, Any]]) -> DeliveryResult:
        if not batch:
            return DeliveryResult.ok(0)

        # Serialização DENTRO do try: o contrato é "nunca levanta".
        session = self._get_session()
        try:
            payload = "\n".join(self._serialize_event(ev) for ev in batch)
            async with session.post(self._url, data=payload) as resp:
                status = resp.status
                if status in _RETRYABLE_STATUS:
                    logger.warning("%s: status transitório %s — retryable", self.kind, status)
                    return DeliveryResult(accepted=0, retryable=True)
                if 200 <= status < 300:
                    return DeliveryResult.ok(len(batch))
                error_kind = "auth" if status in {401, 403} else "schema_rejected"
                logger.warning(
                    "%s: rejeição de lote status=%s — iniciando fallback individual (%d eventos)",
                    self.kind, status, len(batch),
                )
                return await self._fallback_individual(batch, f"HTTP {status}", error_kind)
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            logger.warning("%s: erro de conexão transitório: %s", self.kind, exc)
            return DeliveryResult(accepted=0, retryable=True)
        except (TypeError, ValueError) as exc:
            logger.warning("%s: evento não-serializável (%s) — schema_rejected", self.kind, type(exc).__name__)
            return DeliveryResult(
                accepted=0,
                rejected=[
                    RejectedEvent(event_id=self._event_id(ev), reason="serialização falhou", error_kind="schema_rejected", retryable=False)
                    for ev in batch
                ],
                retryable=False,
            )

    async def test(self) -> TestResult:
        """Probe: envia um evento mínimo. ``2xx`` → ok; ``401/403`` → token inválido;
        conexão/timeout → falha de rede. Nunca levanta."""
        probe = json.dumps({"event": {"probe": True}}, separators=(",", ":"))
        session = self._get_session()
        try:
            async with session.post(self._url, data=probe) as resp:
                if resp.status in {401, 403}:
                    return TestResult.failed("token de ingestão inválido (401/403)")
                if 200 <= resp.status < 300:
                    return TestResult.passed(f"ingest ok: {self._url}")
                detail = ""
                try:
                    detail = (await resp.text())[:300]
                except Exception:  # pragma: no cover
                    detail = f"HTTP {resp.status}"
                return TestResult.failed(f"ingest respondeu status={resp.status}: {detail!r}")
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            return TestResult.failed(f"erro de conexão ao endpoint de ingestão: {exc}")

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            try:
                await self._session.close()
            except Exception:  # pragma: no cover — best-effort
                logger.exception("%s: erro ao fechar sessão", self.kind)
            finally:
                self._session = None
