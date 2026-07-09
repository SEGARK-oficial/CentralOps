"""Cliente OTLP/HTTP para exportação de logs.

Exporta eventos OCSF do CentralOps via ``POST /v1/logs`` usando o protocolo
OTLP/HTTP JSON (``ExportLogsServiceRequest``). Compatível com qualquer
coletor/backend OTLP: Grafana Alloy, OpenTelemetry Collector, Jaeger,
Datadog OTLP endpoint, Honeycomb, SigNoz, etc.

**Sem dependência de ``opentelemetry-*``:** o JSON OTLP é construído
manualmente — o proto serializado em JSON segue exatamente a spec OTel
(opentelemetry-proto/opentelemetry/proto/logs/v1/logs.proto). Isso mantém
o core do CentralOps livre de dependências pesadas de SDK OTLP.

Referência: https://opentelemetry.io/docs/specs/otel/protocol/exporter/#otlphttp

**Isolamento de falha por item:**
O endpoint OTLP/HTTP ``/v1/logs`` responde com ``ExportLogsServiceResponse``
que pode incluir ``partial_success.rejected_log_records`` (gRPC-JSON). Quando
presente e > 0, fazemos re-envio individual (1 LogRecord por POST) para isolar
o(s) evento(s) problemático(s). Para batches grandes (> _MAX_INDIVIDUAL_FALLBACK)
a estratégia de isolamento seria cara demais — todo o lote é marcado ``rejected``
sem retry cego.

**Capability declarada: at_least_once (não idempotent):**
OTLP/HTTP não expõe mecanismo de dedup nativo no endpoint. O ``event_id`` é
incluído como atributo ``centralops.event_id`` para correlação downstream.

**OCSF → OTLP mapeamento de severity:**
OCSF severity_id → OTLP SeverityNumber (enum spec §2.2.2):
  0 unknown     → 0 (UNSPECIFIED)
  1 information → 9 (INFO)
  2 low         → 5 (DEBUG)
  3 medium      → 13 (WARN)
  4 high        → 17 (ERROR)
  5 critical    → 21 (FATAL)
  6 fatal       → 24 (FATAL4)
  99 other      → 9 (INFO)

**Design de sessão:** aiohttp.ClientSession reutilizada entre chamadas
(espelha SplunkHecClient / ElasticBulkClient). Criada lazily, fechada em
``close()``.
"""

from __future__ import annotations

import logging
import time as _time
from typing import Any, Dict, List, Mapping, Optional

import aiohttp

from ._fastjson import dumps_bytes as _json_bytes
from ._fastjson import dumps_str as _json_str
from .base import DeliveryResult, RejectedEvent, TestResult

logger = logging.getLogger(__name__)

# ── OTLP severity mapping ──────────────────────────────────────────────────────

# OTLP SeverityNumber values (opentelemetry-proto logs.proto §SeverityNumber).
# Ref: https://opentelemetry.io/docs/specs/otel/logs/data-model/#field-severitynumber
_OCSF_TO_OTLP_SEVERITY: Mapping[int, int] = {
    0: 0,   # unknown      → SEVERITY_NUMBER_UNSPECIFIED (0)
    1: 9,   # informational → SEVERITY_NUMBER_INFO  (9)
    2: 5,   # low           → SEVERITY_NUMBER_DEBUG (5)
    3: 13,  # medium        → SEVERITY_NUMBER_WARN  (13)
    4: 17,  # high          → SEVERITY_NUMBER_ERROR (17)
    5: 21,  # critical      → SEVERITY_NUMBER_FATAL (21)
    6: 24,  # fatal         → SEVERITY_NUMBER_FATAL4 (24)
    99: 9,  # other         → SEVERITY_NUMBER_INFO  (9)
}

_OTLP_SEVERITY_TEXT: Mapping[int, str] = {
    0: "UNSPECIFIED",
    5: "DEBUG",
    9: "INFO",
    13: "WARN",
    17: "ERROR",
    21: "FATAL",
    24: "FATAL4",
}

# Limite máximo de re-envio individual.  Batches maiores → rejected direto.
_MAX_INDIVIDUAL_FALLBACK = 50

# Códigos HTTP que indicam erro transitório (retry).
_RETRYABLE_STATUS = {429, 502, 503, 504}

# Escopo/instrumentação padrão para os LogRecords.
_SCOPE_NAME = "centralops"
_SCOPE_VERSION = "1.0.0"


# ── Mapeamento canônico → OTLP LogRecord ─────────────────────────────────────


def _severity_number(severity_id: Any) -> int:
    """Mapeia OCSF severity_id → OTLP SeverityNumber."""
    if severity_id is None:
        return 0
    try:
        return _OCSF_TO_OTLP_SEVERITY.get(int(severity_id), 0)
    except (TypeError, ValueError):
        return 0


def _event_id(envelope: Mapping[str, Any]) -> Optional[str]:
    """Extrai event_id do namespace _centralops, ou None."""
    meta = envelope.get("_centralops") or {}
    ev = meta.get("event_id")
    return str(ev) if ev else None


def _time_unix_nano(envelope: Mapping[str, Any]) -> int:
    """Extrai ou gera timeUnixNano (int, nanossegundos desde epoch Unix).

    Tenta extrair de ``normalized.time`` (Unix milliseconds, OCSF §field-time)
    e converte para nanossegundos. Fallback: ``time.time_ns()`` (wall clock).
    """
    normalized = envelope.get("normalized") or {}
    t_ms = normalized.get("time")
    if t_ms is not None:
        try:
            return int(t_ms) * 1_000_000  # ms → ns
        except (TypeError, ValueError):
            pass
    # Fallback para wall clock em nanos (Python 3.7+).
    return _time.time_ns()


def _extract_attributes(envelope: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Constrói a lista de atributos OTLP KeyValue a partir do envelope.

    Atributos exportados:
      centralops.vendor         — _centralops.vendor
      centralops.event_id       — _centralops.event_id
      centralops.event_type     — _centralops.event_type
      centralops.stream         — _centralops.stream
      centralops.integration_id — _centralops.integration_id (string)
      centralops.customer_id    — _centralops.customer_id (string)
      ocsf.class_uid            — normalized.class_uid (string)
      ocsf.severity_id          — normalized.severity_id (string)

    Apenas atributos com valor não-None são incluídos.
    """
    meta = envelope.get("_centralops") or {}
    normalized = envelope.get("normalized") or {}

    pairs: list[tuple[str, Any]] = [
        ("centralops.vendor",         meta.get("vendor")),
        ("centralops.event_id",       meta.get("event_id")),
        ("centralops.event_type",     meta.get("event_type")),
        ("centralops.stream",         meta.get("stream")),
        ("centralops.integration_id", meta.get("integration_id")),
        ("centralops.customer_id",    meta.get("customer_id")),
        ("ocsf.class_uid",            normalized.get("class_uid")),
        ("ocsf.severity_id",          normalized.get("severity_id")),
    ]

    attrs: list[dict[str, Any]] = []
    for key, value in pairs:
        if value is None:
            continue
        # OTLP KeyValue: {"key": str, "value": {"stringValue": str}}
        # (simplificado: todos como strings — não requer AnyValue binário).
        attrs.append({"key": key, "value": {"stringValue": str(value)}})
    return attrs


def format_otlp_log_record(envelope: Mapping[str, Any]) -> Dict[str, Any]:
    """Converte um envelope canônico em um OTLP LogRecord (dict serializável).

    O LogRecord segue o schema ``opentelemetry.proto.logs.v1.LogRecord``:
      - timeUnixNano  — timestamp em nanossegundos (string numérica, per spec JSON)
      - severityNumber — int (OTLP SeverityNumber enum)
      - severityText  — string legível
      - body          — AnyValue wrapping o JSON do envelope como string
      - attributes    — lista de KeyValue com metadados do envelope
      - traceId / spanId — omitidos (não temos tracing neste fluxo)
    """
    normalized = envelope.get("normalized") or {}
    severity_id = normalized.get("severity_id")
    sev_num = _severity_number(severity_id)
    sev_text = _OTLP_SEVERITY_TEXT.get(sev_num, "UNSPECIFIED")

    # OTLP JSON proto3: timeUnixNano é uint64 → representado como string numérica.
    time_ns = _time_unix_nano(envelope)

    # body: AnyValue wrapping o envelope completo como JSON string.
    body_str = _json_str(dict(envelope))

    return {
        "timeUnixNano": str(time_ns),
        "severityNumber": sev_num,
        "severityText": sev_text,
        "body": {"stringValue": body_str},
        "attributes": _extract_attributes(envelope),
    }


def format_otlp_request(
    batch: List[Mapping[str, Any]],
    *,
    resource_attrs: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Constrói um ``ExportLogsServiceRequest`` OTLP/HTTP JSON.

    Estrutura:
        {
          "resourceLogs": [{
            "resource": {"attributes": [...]},
            "scopeLogs": [{
              "scope": {"name": "centralops", "version": "1.0.0"},
              "logRecords": [ ...um por evento... ]
            }]
          }]
        }

    ``resource_attrs`` é um dict str→str com atributos de recurso customizados
    (ex.: ``{"service.name": "centralops", "host.name": "centralops-01"}``).
    O resource identifica a origem do serviço para backends OTLP.
    """
    res_kv: list[dict[str, Any]] = [
        {"key": k, "value": {"stringValue": v}}
        for k, v in (resource_attrs or {}).items()
    ]

    log_records = [format_otlp_log_record(ev) for ev in batch]

    return {
        "resourceLogs": [
            {
                "resource": {"attributes": res_kv},
                "scopeLogs": [
                    {
                        "scope": {
                            "name": _SCOPE_NAME,
                            "version": _SCOPE_VERSION,
                        },
                        "logRecords": log_records,
                    }
                ],
            }
        ]
    }


# ── OtlpHttpClient ────────────────────────────────────────────────────────────


class OtlpHttpClient:
    """Cliente OTLP/HTTP com sessão aiohttp persistente.

    Satisfaz o protocolo ``Destination`` diretamente: define ``kind``,
    ``format``, ``send_batch``, ``test`` e ``close``.

    ``endpoint`` deve incluir o path ``/v1/logs`` (ex.:
    ``https://otel.exemplo.com:4318/v1/logs``).

    ``headers`` adicionais são mesclados com ``Content-Type: application/json``
    — útil para headers de autenticação (``Authorization: Bearer ...``).

    ``resource_attrs`` são os atributos de recurso OTel (service.name,
    host.name, etc.) incluídos em todos os batches.
    """

    kind: str = "otlp"

    def __init__(
        self,
        endpoint: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        resource_attrs: Optional[Mapping[str, str]] = None,
        verify_tls: bool = True,
        ca_bundle: Optional[str] = None,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._extra_headers = dict(headers or {})
        self._resource_attrs = dict(resource_attrs or {})
        self._verify_tls = verify_tls
        self._ca_bundle = ca_bundle
        self._session: Optional[aiohttp.ClientSession] = None

    def format(self, envelope: Mapping[str, Any]) -> Dict[str, Any]:
        """Converte envelope canônico em OTLP LogRecord dict (canônico → wire dict).

        Retorna o LogRecord isolado (não um ExportLogsServiceRequest completo).
        O request completo é montado em send_batch para o lote inteiro.
        Para shadow/preview, este dict é serializável diretamente.
        """
        return format_otlp_log_record(envelope)

    def _build_ssl(self) -> Any:
        """Retorna o parâmetro ``ssl`` para aiohttp."""
        if not self._verify_tls:
            return False
        if self._ca_bundle:
            import ssl

            ctx = ssl.create_default_context(cafile=self._ca_bundle)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            return ctx
        return True

    def _get_session(self) -> aiohttp.ClientSession:
        """Retorna a sessão existente ou cria uma nova (lazy init)."""
        if self._session is None or self._session.closed:
            headers: dict[str, str] = {"Content-Type": "application/json"}
            headers.update(self._extra_headers)
            connector = aiohttp.TCPConnector(ssl=self._build_ssl())
            self._session = aiohttp.ClientSession(
                headers=headers,
                connector=connector,
            )
        return self._session

    def _serialize_request(self, batch: List[Mapping[str, Any]]) -> bytes:
        """Serializa o ExportLogsServiceRequest como bytes JSON."""
        return _json_bytes(format_otlp_request(batch, resource_attrs=self._resource_attrs))

    async def _send_single(
        self, ev: Mapping[str, Any], session: aiohttp.ClientSession
    ) -> tuple[bool, bool, str, str]:
        """Envia um único evento e retorna (accepted, retryable, reason, error_kind)."""
        payload = self._serialize_request([ev])
        try:
            async with session.post(self._endpoint, data=payload) as resp:
                status = resp.status
                if status in _RETRYABLE_STATUS:
                    return False, True, f"HTTP {status} transitório", "unknown"
                if status in {401, 403}:
                    return False, False, f"auth HTTP {status}", "auth"
                if status in {200, 204}:
                    return True, False, "", ""
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    body = {}
                reason = (
                    body.get("message") or body.get("error") or f"HTTP {status}"
                    if isinstance(body, dict) else f"HTTP {status}"
                )
                return False, False, str(reason), "schema_rejected"
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            return False, True, str(exc), "unknown"

    async def _fallback_individual(
        self,
        batch: List[Mapping[str, Any]],
        batch_reason: str,
        batch_error_kind: str,
    ) -> DeliveryResult:
        """Fallback de isolamento: re-envia cada evento individualmente."""
        if len(batch) > _MAX_INDIVIDUAL_FALLBACK:
            logger.error(
                "otlp: lote grande demais para fallback individual "
                "(%d > %d) — todo lote rejected sem isolamento",
                len(batch),
                _MAX_INDIVIDUAL_FALLBACK,
            )
            return DeliveryResult(
                accepted=0,
                rejected=[
                    RejectedEvent(
                        event_id=_event_id(ev) or "?",
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
            "otlp: fallback individual para %d eventos (isolamento E2)", len(batch)
        )
        for ev in batch:
            ok, retryable, reason, error_kind = await self._send_single(ev, session)
            if ok:
                accepted += 1
            elif retryable:
                any_retryable = True
            else:
                rejected.append(
                    RejectedEvent(
                        event_id=_event_id(ev) or "?",
                        reason=reason,
                        error_kind=error_kind,
                        retryable=False,
                    )
                )

        if any_retryable:
            return DeliveryResult(accepted=accepted, retryable=True)
        return DeliveryResult(accepted=accepted, rejected=rejected, retryable=False)

    async def send_batch(self, batch: List[Mapping[str, Any]]) -> DeliveryResult:
        """Envia lote de eventos ao endpoint OTLP ``/v1/logs``.

        Monta um ``ExportLogsServiceRequest`` com todos os eventos e
        faz um único POST. Quando há ``partial_success.rejected_log_records``
        na resposta (isolamento parcial nativo do OTLP), ativa o fallback
        individual para identificar o(s) evento(s) problemático(s).

        Devolve ``DeliveryResult`` sem levantar exceção: erros transitórios
        → retryable=True; erros determinísticos → fallback individual.
        """
        if not batch:
            return DeliveryResult.ok(0)

        payload = self._serialize_request(batch)
        session = self._get_session()
        try:
            async with session.post(self._endpoint, data=payload) as resp:
                status = resp.status

                if status in _RETRYABLE_STATUS:
                    logger.warning(
                        "otlp: status transitório %s — retryable", status
                    )
                    return DeliveryResult(accepted=0, retryable=True)

                if status in {401, 403}:
                    rejected = [
                        RejectedEvent(
                            event_id=_event_id(ev) or "?",
                            reason=f"auth HTTP {status}",
                            error_kind="auth",
                            retryable=False,
                        )
                        for ev in batch
                    ]
                    return DeliveryResult(accepted=0, rejected=rejected, retryable=False)

                if status not in {200, 204}:
                    try:
                        body = await resp.json(content_type=None)
                    except Exception:
                        body = {}
                    reason = (
                        body.get("message") or body.get("error") or f"HTTP {status}"
                        if isinstance(body, dict) else f"HTTP {status}"
                    )
                    logger.warning(
                        "otlp: rejeição de lote status=%s reason=%r — "
                        "iniciando fallback individual (%d eventos)",
                        status,
                        reason,
                        len(batch),
                    )
                    return await self._fallback_individual(batch, str(reason), "schema_rejected")

                # 200 ou 204 → verifica partial_success (OTLP spec).
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    body = {}

                if isinstance(body, dict):
                    ps = body.get("partialSuccess") or {}
                    rejected_count = ps.get("rejectedLogRecords") if isinstance(ps, dict) else None
                    if rejected_count:
                        logger.warning(
                            "otlp: partial_success.rejected_log_records=%s — "
                            "iniciando fallback individual (%d eventos)",
                            rejected_count,
                            len(batch),
                        )
                        error_msg = ps.get("errorMessage") or "rejectedLogRecords"
                        return await self._fallback_individual(
                            batch, str(error_msg), "schema_rejected"
                        )

                return DeliveryResult.ok(len(batch))

        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            logger.warning("otlp: erro de conexão transitório: %s", exc)
            return DeliveryResult(accepted=0, retryable=True)

    async def test(self) -> TestResult:
        """Probe de conexão: envia um LogRecord mínimo ao ``/v1/logs``.

        200/204 → passou; 401/403 → credencial inválida; erro de conexão →
        falha de rede. Nunca levanta exceção.
        """
        probe_envelope: dict[str, Any] = {
            "_centralops": {"event_id": "probe", "vendor": "centralops"},
            "normalized": {"class_uid": 0, "severity_id": 1},
            "raw": {},
        }
        payload = self._serialize_request([probe_envelope])
        session = self._get_session()
        try:
            async with session.post(self._endpoint, data=payload) as resp:
                status = resp.status
                if status in {401, 403}:
                    return TestResult.failed(f"credencial inválida ({status})")
                if status in {200, 204}:
                    return TestResult.passed(f"OTLP ok: {self._endpoint}")
                return TestResult.failed(
                    f"endpoint OTLP respondeu HTTP {status}"
                )
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            return TestResult.failed(f"erro de conexão OTLP: {exc}")

    async def close(self) -> None:
        """Fecha a sessão aiohttp."""
        if self._session is not None and not self._session.closed:
            try:
                await self._session.close()
            except Exception:  # pragma: no cover — best-effort
                logger.exception("otlp: erro ao fechar sessão")
            finally:
                self._session = None
