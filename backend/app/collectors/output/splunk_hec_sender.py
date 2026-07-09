"""Cliente HTTP Event Collector (HEC) do Splunk.

Envia eventos em lote via POST para ``/services/collector`` usando o protocolo
HEC do Splunk. Payload: múltiplos objetos JSON concatenados (NDJSON — **não**
array JSON), separados por ``\\n``. O HEC aceita este formato nativamente.

Referência: https://docs.splunk.com/Documentation/Splunk/latest/Data/HECExamples

**Ativo quando há um destino kind=splunk_hec configurado** (multi-destino
é GA).

Design de sessão: uma única ``aiohttp.ClientSession`` é reutilizada entre
chamadas (espelha a conexão persistente TCP dos senders de syslog). A sessão
é criada lazily no primeiro ``send_batch`` ou ``test`` e fechada em ``close``.

**Idempotência — honesta:**
O HEC não fornece mecanismo de dedup nativo via sender. A capability registrada
é "at_least_once" (não "idempotent"). O event_id do namespace _centralops é
incluído no payload HEC (campo "fields._centralops_event_id") para que o indexer
Splunk possa usar lookup/dedup na query. Dedup real é responsabilidade do
administrador Splunk — este sender NÃO garante exactly-once.

**Isolamento de falha por item:**
O HEC responde por lote (pass/fail) — não por item como o Elasticsearch _bulk.
Quando o lote inteiro é rejeitado com 4xx determinístico (não-retryable), o
sender faz envio individual em fallback (um POST por evento) para isolar o
poison event. Eventos aceitos individualmente → accepted; 4xx → rejected;
5xx/429 no individual → retryable. Máximo de _MAX_INDIVIDUAL_FALLBACK eventos
no fallback (batches maiores: lote todo rejected para evitar explosão de POSTs).
"""

from __future__ import annotations

import json
import logging
from typing import Any, List, Mapping, Optional

import aiohttp

from .base import DeliveryResult, RejectedEvent, TestResult

logger = logging.getLogger(__name__)

# Códigos HEC de resposta (Splunk HEC response reference).
_HEC_OK = 0
# Conjunto de status HTTP que indicam erro transitório (retry).
_RETRYABLE_STATUS = {429, 502, 503, 504}
# Limite de eventos no fallback individual (E2): batches maiores do que isso
# são rejeitados no atacado em vez de explodir em N POSTs individuais.
_MAX_INDIVIDUAL_FALLBACK = 50


def format_hec_event(
    envelope: Mapping[str, Any],
    *,
    sourcetype: str = "centralops",
    index: Optional[str] = None,
    source: Optional[str] = None,
    host: Optional[str] = None,
) -> dict:
    """Embala um envelope canônico no wrapper HEC do Splunk.

    Omite campos opcionais (index, source, host) quando ``None`` — o HEC
    usa os defaults do token quando ausentes. **Não define "time"**: deixa
    o Splunk usar o tempo de recepção. Isso evita problemas de fuso/epoch
    em eventos cujo timestamp canônico pode estar em formato ISO 8601 e não
    em Unix epoch float.

    Inclui o ``event_id`` do namespace ``_centralops`` no campo
    ``fields._centralops_event_id`` (dedup no indexer):
    o HEC não possui dedup nativo no sender; o event_id é exposto como
    campo indexed para que queries Splunk possam detectar duplicatas.
    Quando ausente, o campo ``fields`` é omitido (sem payload espúrio).

    Args:
        envelope: dicionário do evento (envelope canônico CentralOps).
        sourcetype: sourcetype HEC (default: ``centralops``).
        index: nome do índice Splunk (omitido quando None).
        source: campo source do HEC (omitido quando None).
        host: campo host do HEC (omitido quando None).

    Returns:
        Dict pronto para ``json.dumps`` e POST ao HEC.
    """
    wrapper: dict[str, Any] = {
        "event": envelope,
        "sourcetype": sourcetype,
    }
    if index is not None:
        wrapper["index"] = index
    if source is not None:
        wrapper["source"] = source
    if host is not None:
        wrapper["host"] = host
    # Expõe o event_id como campo indexado para dedup no indexer.
    # O HEC não faz dedup automático: este campo é para uso em queries Splunk.
    meta = envelope.get("_centralops") or {}
    event_id = meta.get("event_id")
    if event_id:
        wrapper["fields"] = {"_centralops_event_id": str(event_id)}
    return wrapper


class SplunkHecClient:
    """Cliente HEC do Splunk com sessão aiohttp persistente.

    Satisfaz o protocolo ``Destination`` diretamente (sem
    ``LegacyTargetDestination``): define ``kind``, ``format``,
    ``send_batch``, ``test`` e ``close``.

    Uso típico (dormant — instanciado pela factory do kind splunk_hec):

        client = SplunkHecClient(
            url="https://splunk.exemplo.com:8088",
            token="abcd-1234",
            index="centralops",
            sourcetype="centralops",
        )
        result = await client.send_batch([event1, event2])
        await client.close()
    """

    kind: str = "splunk_hec"

    def __init__(
        self,
        url: str,
        token: Optional[str],
        index: Optional[str] = None,
        sourcetype: str = "centralops",
        source: Optional[str] = None,
        host: Optional[str] = None,
        verify_tls: bool = True,
        ca_bundle: Optional[str] = None,
    ) -> None:
        self._url = url.rstrip("/") + "/services/collector"
        self._token = token
        self._index = index
        self._sourcetype = sourcetype
        self._source = source
        self._host = host
        self._verify_tls = verify_tls
        self._ca_bundle = ca_bundle
        self._session: Optional[aiohttp.ClientSession] = None

    def format(self, envelope: Mapping[str, Any]) -> dict:
        """Converte envelope canônico no wrapper HEC (canônico → wire dict)."""
        return format_hec_event(
            envelope,
            sourcetype=self._sourcetype,
            index=self._index,
            source=self._source,
            host=self._host,
        )

    def _build_ssl(self) -> Any:
        """Retorna o parâmetro ``ssl`` para aiohttp: False, True ou SSLContext."""
        if not self._verify_tls:
            return False
        if self._ca_bundle:
            import ssl

            ctx = ssl.create_default_context(cafile=self._ca_bundle)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            return ctx
        return True  # verificação padrão do aiohttp

    def _get_session(self) -> aiohttp.ClientSession:
        """Retorna a sessão existente ou cria uma nova (lazy init)."""
        if self._session is None or self._session.closed:
            headers = {"Content-Type": "application/json"}
            if self._token:
                headers["Authorization"] = f"Splunk {self._token}"
            connector = aiohttp.TCPConnector(ssl=self._build_ssl())
            self._session = aiohttp.ClientSession(
                headers=headers,
                connector=connector,
            )
        return self._session

    def _event_id(self, event: Mapping[str, Any]) -> str:
        """Extrai o event_id do namespace _centralops, ou '?' se ausente."""
        meta = event.get("_centralops") or {}
        return str(meta.get("event_id") or "?")

    def _serialize_event(self, ev: Mapping[str, Any]) -> str:
        """Serializa um evento canônico como linha NDJSON do HEC."""
        return json.dumps(
            format_hec_event(
                ev,
                sourcetype=self._sourcetype,
                index=self._index,
                source=self._source,
                host=self._host,
            ),
            separators=(",", ":"),
            default=str,
            ensure_ascii=False,
        )

    async def _send_single(
        self, ev: Mapping[str, Any], session: Any
    ) -> tuple[bool, bool, str, str]:
        """Envia um único evento via POST e retorna (accepted, retryable, reason, error_kind).

        Usado no fallback de isolamento: chamado por _fallback_individual.
        Nunca levanta exceção — retorna retryable=True em erros de conexão.
        """
        payload = self._serialize_event(ev)
        try:
            async with session.post(self._url, data=payload) as resp:
                status = resp.status
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    body = {}

                if status in _RETRYABLE_STATUS:
                    return False, True, f"HTTP {status} transitório", "unknown"

                hec_code = body.get("code", -1) if isinstance(body, dict) else -1
                hec_text = (
                    body.get("text", f"HTTP {status}") if isinstance(body, dict) else f"HTTP {status}"
                )

                if status == 200 and hec_code == _HEC_OK:
                    return True, False, "", ""

                error_kind = "auth" if status in {401, 403} else "schema_rejected"
                return False, False, hec_text, error_kind

        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            return False, True, str(exc), "unknown"

    async def _fallback_individual(
        self,
        batch: List[Mapping[str, Any]],
        batch_reason: str,
        batch_error_kind: str,
    ) -> DeliveryResult:
        """Fallback de isolamento: envia cada evento individualmente.

        Chamado quando o lote inteiro é rejeitado com 4xx determinístico e o
        lote é pequeno o suficiente (≤ _MAX_INDIVIDUAL_FALLBACK) para amortizar
        N POSTs. Classifica cada evento: aceito, rejeitado ou retryable.

        Batches maiores que _MAX_INDIVIDUAL_FALLBACK: rejeitados no atacado
        (todos rejected) para evitar explosão de requisições HTTP — o chamador
        deve encaminhar para DLQ sem retry.
        """
        if len(batch) > _MAX_INDIVIDUAL_FALLBACK:
            logger.error(
                "splunk_hec: lote grande demais para fallback individual "
                "(%d > %d) — todo lote rejected sem isolamento",
                len(batch),
                _MAX_INDIVIDUAL_FALLBACK,
            )
            return DeliveryResult(
                accepted=0,
                rejected=[
                    RejectedEvent(
                        event_id=self._event_id(ev),
                        reason=batch_reason,
                        error_kind=batch_error_kind,
                        retryable=False,
                    )
                    for ev in batch
                ],
                retryable=False,
            )

        session = self._get_session()
        accepted = 0
        rejected: list[RejectedEvent] = []
        any_retryable = False

        logger.info(
            "splunk_hec: fallback individual para %d eventos (isolamento E2)", len(batch)
        )
        for ev in batch:
            ok, retryable, reason, error_kind = await self._send_single(ev, session)
            if ok:
                accepted += 1
            elif retryable:
                # Erro transitório no individual → marca lote como retryable;
                # os já-aceitos podem ser re-enviados (at_least_once — sem dedup).
                any_retryable = True
            else:
                rejected.append(
                    RejectedEvent(
                        event_id=self._event_id(ev),
                        reason=reason,
                        error_kind=error_kind,
                        retryable=False,
                    )
                )

        if any_retryable:
            return DeliveryResult(accepted=accepted, retryable=True)
        return DeliveryResult(accepted=accepted, rejected=rejected, retryable=False)

    async def send_batch(self, batch: List[Mapping[str, Any]]) -> DeliveryResult:
        """Envia lote de eventos ao HEC do Splunk.

        Serializa cada evento como wrapper HEC e concatena com ``\\n``
        (NDJSON — o HEC aceita múltiplos objetos concatenados, não array).
        Devolve ``DeliveryResult`` sem levantar exceção: erros transitórios
        → retryable=True; erros determinísticos → fallback individual (E2).

        **Isolamento:** quando o lote inteiro recebe rejeição 4xx
        determinística, dispara ``_fallback_individual`` para isolar poison
        events — cada evento é enviado individualmente e classificado como
        accepted/rejected/retryable.
        """
        if not batch:
            return DeliveryResult.ok(0)

        # Serializa: cada evento → wrapper HEC → JSON compacto, separados por \n.
        payload = "\n".join(self._serialize_event(ev) for ev in batch)

        session = self._get_session()
        try:
            async with session.post(self._url, data=payload) as resp:
                status = resp.status
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    body = {}

                if status in _RETRYABLE_STATUS:
                    logger.warning(
                        "splunk_hec: status transitório %s — retryable", status
                    )
                    return DeliveryResult(accepted=0, retryable=True)

                hec_code = body.get("code", -1) if isinstance(body, dict) else -1
                hec_text = (
                    body.get("text", f"HTTP {status}") if isinstance(body, dict) else f"HTTP {status}"
                )

                if status == 200 and hec_code == _HEC_OK:
                    return DeliveryResult.ok(len(batch))

                # 4xx determinístico → isolamento: fallback individual
                # para identificar o(s) poison event(s) do lote.
                error_kind = "auth" if status in {401, 403} else "schema_rejected"
                logger.warning(
                    "splunk_hec: rejeição de lote status=%s code=%s text=%r "
                    "— iniciando fallback individual (%d eventos)",
                    status,
                    hec_code,
                    hec_text,
                    len(batch),
                )
                return await self._fallback_individual(batch, hec_text, error_kind)

        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            logger.warning("splunk_hec: erro de conexão transitório: %s", exc)
            return DeliveryResult(accepted=0, retryable=True)

    async def test(self) -> TestResult:
        """Probe de conexão: envia um evento mínimo e verifica a resposta HEC.

        ``code == 0`` → passou; 401/403 → token inválido; erro de conexão →
        falha de rede. Nunca levanta exceção.
        """
        probe_event = {"event": {"probe": True}, "sourcetype": self._sourcetype}
        payload = json.dumps(probe_event, separators=(",", ":"))

        session = self._get_session()
        try:
            async with session.post(self._url, data=payload) as resp:
                status = resp.status
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    body = {}

                if status in {401, 403}:
                    return TestResult.failed("token HEC inválido (401/403)")

                hec_code = body.get("code", -1) if isinstance(body, dict) else -1
                hec_text = body.get("text", f"HTTP {status}") if isinstance(body, dict) else f"HTTP {status}"

                if status == 200 and hec_code == _HEC_OK:
                    return TestResult.passed(f"HEC ok: {self._url}")

                return TestResult.failed(
                    f"HEC respondeu status={status} code={hec_code} text={hec_text!r}"
                )

        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            return TestResult.failed(f"erro de conexão ao HEC: {exc}")

    async def close(self) -> None:
        """Fecha a sessão aiohttp."""
        if self._session is not None and not self._session.closed:
            try:
                await self._session.close()
            except Exception:  # pragma: no cover — best-effort
                logger.exception("splunk_hec: erro ao fechar sessão")
            finally:
                self._session = None
