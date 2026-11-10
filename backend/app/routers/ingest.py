"""Ingestão push — FortiGate syslog, Windows Event Log/WEC, …

Dois grupos de rotas:

1. **Endpoint de ingestão** (autenticado por **token de ingestão**, não por sessão
   de usuário): ``POST /api/ingest/{stream}``. É o que o edge-collector
   (Vector/OTel/agente) chama. Grava no tópico raw e responde só depois do ack;
   o papel ``processor`` faz o resto (ADR-0018).

2. **Gestão do token** (autenticada por **admin**): emitir/rotacionar o token e
   consultar o endpoint. Path com 3 segmentos
   (``/api/ingest/integrations/{id}/...``) — não colide com ``/{stream}``.

Segurança: o token nunca é persistido em claro (só o SHA-256, cifrado pelo cofre);
a verificação é por hash em tempo constante. O endpoint de ingestão NÃO exige
sessão (fica fora de ``protected_api`` no ``main``), mas exige o token válido.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import time
import zlib
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..collectors import registry as collector_registry
from ..collectors.vendors import aws_firehose
from ..collectors.dataplane.raw import OversizedRawEventError, produce_raw_async
from ..collectors.metrics import INGEST_ACCEPTED, INGEST_MALFORMED
from ..core import auth as app_auth
from ..core import tenant
from ..core.errors import ApiError
from ..core.rate_limiter import ingest_rate_limiter
from ..db import database, models
from ..services import ingest_tokens

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ingest", tags=["ingest"])

# Caps por requisição (defesa contra payloads abusivos antes do data-plane).
_MAX_BODY_BYTES = 16 * 1024 * 1024  # 16 MiB
_MAX_EVENTS_PER_REQUEST = 20_000
# Espera sugerida ao edge quando o data-plane não gravou (broker fora).
_RETRY_AFTER_S = 20
# Teto por-evento individual (após o teto do corpo). Um único evento gigante trava a
# normalização e estoura o teto de payload do DB/destino a jusante — os concorrentes
# (Cribl/Vector) truncam ou dropam a linha, não o lote. Aqui: dropa+conta a linha, o
# resto do lote segue. 1 MiB é folgado p/ syslog (tipicamente < 64 KiB) e JSON de EDR.
_MAX_EVENT_BYTES = 1 * 1024 * 1024  # 1 MiB


# ── Schemas ────────────────────────────────────────────────────────────────


class IngestResponse(BaseModel):
    accepted: int
    # Rejeições no PARSE (borda mandou algo inválido) — SUCESSO PARCIAL, não erro-total.
    # Espelham os concorrentes: uma linha ruim no NDJSON não derruba o lote inteiro.
    # Backward-compat: default 0 → clientes antigos ignoram; edge-collectors novos podem
    # alarmar/re-enfileirar por linha. Ver status 207 no handler.
    parse_errors: int = 0     # linhas NDJSON que não são JSON válido
    type_errors: int = 0      # JSON válido mas não é objeto (ex.: número/string/null soltos)
    oversized: int = 0        # eventos individuais acima de _MAX_EVENT_BYTES
    error_detail: Optional[str] = None  # amostra do 1º erro (diagnóstico, sem vazar payload)


@dataclasses.dataclass
class _ParsedBatch:
    """Resultado tolerante do parse: eventos aproveitáveis + contadores de rejeição.

    O parse NÃO aborta o lote por causa de uma linha ruim (modelo dos concorrentes de
    data-pipeline). Só levanta HTTP quando NADA é aproveitável e o corpo é irrecuperável
    (JSON único malformado) ou quando limites duros de tamanho/contagem são excedidos.
    """

    events: List[dict] = dataclasses.field(default_factory=list)
    parse_errors: int = 0
    type_errors: int = 0
    oversized: int = 0
    first_error: Optional[str] = None

    @property
    def rejected(self) -> int:
        return self.parse_errors + self.type_errors + self.oversized

    def _note(self, msg: str) -> None:
        if self.first_error is None:
            self.first_error = msg


class IngestTokenResponse(BaseModel):
    """O token em claro é devolvido UMA vez (na emissão/rotação)."""

    token: str
    endpoint: str  # path relativo do POST de ingestão (sem stream)


class IngestInfo(BaseModel):
    integration_id: int
    platform: str
    transport: str
    streams: List[str]
    has_token: bool
    endpoint_base: str  # ex.: "/api/ingest"
    icon_id: Optional[str] = None  # ícone de marca (plugin-driven, do catálogo)


# ── Helpers ────────────────────────────────────────────────────────────────


def _extract_token(authorization: Optional[str], x_token: Optional[str]) -> Optional[str]:
    """Token de ``Authorization: Bearer <t>``, ``Authorization: Splunk <t>`` (clientes
    HEC) ou ``X-CentralOps-Ingest-Token``. É sempre o token de ingestão da integração."""
    if authorization:
        parts = authorization.split(None, 1)
        if len(parts) == 2 and parts[0].lower() in ("bearer", "splunk"):
            return parts[1].strip()
    if x_token:
        return x_token.strip()
    return None


def _authenticate(db: Session, token: Optional[str]) -> models.Integration:
    """Resolve + valida a integração a partir do token. 401 genérico (anti-enum)."""
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="token de ingestão inválido",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not token:
        raise unauthorized
    integration_id = ingest_tokens.parse_integration_id(token)
    if integration_id is None:
        raise unauthorized
    integ = db.get(models.Integration, integration_id)
    if integ is None or not integ.is_active:
        raise unauthorized
    if not ingest_tokens.verify(integ, token):
        raise unauthorized
    return integ


def _collect(batch: _ParsedBatch, obj: Any) -> None:
    """Acrescenta ``obj`` a ``batch`` aplicando teto por-evento e contando o tipo errado.

    Um item que não é objeto (dict) é rejeitado como ``type_error`` — ele não tem chaves
    p/ mapear no drift explorer e envenenaria o normalizador. Um objeto acima do teto
    por-evento é rejeitado como ``oversized`` (o resto do lote sobrevive)."""
    if not isinstance(obj, dict):
        batch.type_errors += 1
        batch._note(f"item não-objeto ({type(obj).__name__}) ignorado")
        return
    # Tamanho serializado do evento — barato e determinístico (o corpo já está em memória).
    try:
        size = len(json.dumps(obj, separators=(",", ":"), default=str).encode("utf-8"))
    except (TypeError, ValueError):
        size = _MAX_EVENT_BYTES + 1  # inserializável → trata como oversized (fail-closed)
    if size > _MAX_EVENT_BYTES:
        batch.oversized += 1
        batch._note(f"evento de {size} bytes acima do teto por-evento ({_MAX_EVENT_BYTES})")
        return
    batch.events.append(obj)


def _parse_events(body: bytes, content_type: str) -> _ParsedBatch:
    """Parse TOLERANTE de NDJSON (1 objeto/linha) ou JSON (objeto único / array / envelope).

    Modelo dos data-pipelines de mercado (Cribl/Vector/Fluent Bit): uma linha ruim NÃO
    derruba o lote — ela é contada e pulada, o resto é aproveitado (sucesso parcial). O
    handler decide o status (200 limpo / 207 parcial). Só levanta HTTP em:
      * 413 — corpo acima de ``_MAX_BODY_BYTES`` ou contagem acima de ``_MAX_EVENTS_PER_REQUEST``;
      * 400 — JSON ÚNICO (não-NDJSON) irrecuperável, i.e. nada pôde ser aproveitado.
    """
    if len(body) > _MAX_BODY_BYTES:
        raise ApiError(
            "ingest.payload_too_large",
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            messages={
                "pt": "payload grande demais",
                "en": "payload too large",
                "es": "payload demasiado grande",
            },
        )
    text = body.decode("utf-8", errors="replace").strip()
    batch = _ParsedBatch()
    if not text:
        return batch

    ct = (content_type or "").lower()
    # NDJSON quando o content-type indica, OU quando o corpo não começa com '['/'{'
    # de um JSON único — heurística tolerante (a maioria dos edge-collectors manda NDJSON).
    is_ndjson = "ndjson" in ct or "text/plain" in ct or "\n" in text and not text.lstrip().startswith("[")

    if is_ndjson:
        # Cada linha é independente: uma inválida vira parse_error e o loop continua.
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError as exc:
                batch.parse_errors += 1
                batch._note(f"linha NDJSON inválida: {exc}")
                continue
            if isinstance(obj, list):
                for item in obj:
                    _collect(batch, item)
            else:
                _collect(batch, obj)
    else:
        # JSON único: se o corpo inteiro não parseia, não há sucesso parcial possível → 400.
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise ApiError(
                "ingest.invalid_json",
                status.HTTP_400_BAD_REQUEST,
                messages={
                    "pt": "JSON inválido: {error}",
                    "en": "Invalid JSON: {error}",
                    "es": "JSON inválido: {error}",
                },
                params={"error": str(exc)},
            ) from None
        if isinstance(parsed, dict):
            # Pode ser um envelope {"events": [...]} ou um evento único.
            inner = parsed.get("events")
            if isinstance(inner, list):
                for item in inner:
                    _collect(batch, item)
            else:
                _collect(batch, parsed)
        elif isinstance(parsed, list):
            for item in parsed:
                _collect(batch, item)
        else:
            # JSON válido mas escalar solto (número/string/bool) — não é evento.
            _collect(batch, parsed)

    if len(batch.events) > _MAX_EVENTS_PER_REQUEST:
        raise ApiError(
            "ingest.too_many_events",
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            messages={
                "pt": "máx. {n} eventos por requisição",
                "en": "max. {n} events per request",
                "es": "máx. {n} eventos por solicitud",
            },
            params={"n": _MAX_EVENTS_PER_REQUEST},
        )
    return batch


def _stamp(event: dict, stream: str) -> dict:
    """Carimba ``_ingest`` com um id estável por CONTEÚDO (dedupe idempotente sob
    retry) + timestamp de recepção. O id é computado ANTES de adicionar metadados
    voláteis para não envenenar o hash."""
    digest = hashlib.sha256(
        json.dumps(event, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    from ..collectors.base import utcnow_iso

    event["_ingest"] = {"id": digest, "received_at": utcnow_iso(), "stream": stream}
    return event


# ── Núcleo comum dos endpoints de ingestão ─────────────────────────────────


def _admit(integ: models.Integration, stream: str) -> None:
    """Rate limit + plataforma push + stream registrado. Levanta HTTP ao recusar."""
    # Rate-limit por integração/token: protege o data-plane de flood
    # por token vazado. Checagem barata, ANTES de ler o corpo. 429 + Retry-After.
    retry_after = ingest_rate_limiter.check(integ.id)
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="taxa de ingestão excedida — reduza a cadência do edge-collector",
            headers={"Retry-After": str(retry_after)},
        )

    reg = collector_registry.get_platform(integ.platform)
    if reg is None or reg.transport != "push":
        raise ApiError(
            "ingest.not_push_source",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            messages={
                "pt": "plataforma {platform!r} não é uma fonte push",
                "en": "platform {platform!r} is not a push source",
                "es": "la plataforma {platform!r} no es una fuente push",
            },
            params={"platform": integ.platform},
        )
    if not collector_registry.has(integ.platform, stream):
        raise ApiError(
            "ingest.unknown_stream",
            status.HTTP_404_NOT_FOUND,
            messages={
                "pt": "stream {stream!r} desconhecido para {platform!r}",
                "en": "unknown stream {stream!r} for {platform!r}",
                "es": "stream {stream!r} desconocido para {platform!r}",
            },
            params={"stream": stream, "platform": integ.platform},
        )



async def _read_body(request: Request) -> bytes:
    """Lê o corpo; descompacta ``Content-Encoding: gzip`` com teto no tamanho
    DESCOMPACTADO (sem isto, um gzip pequeno viraria uma bomba de memória).
    Exporters OTLP e o HEC do Vector mandam gzip por padrão."""
    body = await request.body()
    encoding = (request.headers.get("content-encoding") or "").strip().lower()
    if encoding in ("", "identity"):
        return body
    if encoding != "gzip":
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Content-Encoding não suportado: use gzip ou nenhum",
        )
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = decompressor.decompress(body, _MAX_BODY_BYTES + 1)
    except zlib.error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="corpo gzip inválido"
        ) from None
    if len(out) > _MAX_BODY_BYTES or decompressor.unconsumed_tail:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="corpo descompactado grande demais",
        )
    return out


def _count_rejections(integ: models.Integration, stream: str, batch: _ParsedBatch) -> None:
    # Rejeições de parse (linha ruim / não-objeto / oversized) NÃO derrubam o lote —
    # são contabilizadas por motivo p/ o Grafana e devolvidas ao edge (que pode alarmar
    # ou re-enfileirar seletivamente). Espelha Cribl/Vector: sucesso parcial é 1ª classe.
    if batch.parse_errors:
        INGEST_MALFORMED.labels(vendor=integ.platform, stream=stream, reason="parse").inc(batch.parse_errors)
    if batch.type_errors:
        INGEST_MALFORMED.labels(vendor=integ.platform, stream=stream, reason="type").inc(batch.type_errors)
    if batch.oversized:
        INGEST_MALFORMED.labels(vendor=integ.platform, stream=stream, reason="oversize").inc(batch.oversized)
    if batch.rejected:
        logger.warning(
            "ingest: %d evento(s) rejeitado(s) no parse integration_id=%s stream=%s "
            "(parse=%d type=%d oversize=%d) — 1º: %s",
            batch.rejected, integ.id, stream,
            batch.parse_errors, batch.type_errors, batch.oversized, batch.first_error,
        )



async def _store_events(integ: models.Integration, stream: str, stamped: List[dict]) -> int:
    """Grava os eventos no tópico raw e devolve quantos foram aceitos.

    ADR-0018 Onda 2: o 200 só sai DEPOIS do ack do tópico raw (acks=all). O Kafka
    é o buffer durável; broker fora = 503 e a borda re-tenta com o buffer de
    disco dela. Nunca um 200 de evento que não está gravado. A lista Redis de
    antes (sem broker) saiu na Onda 5."""
    try:
        accepted = await produce_raw_async(integ.id, stream, stamped)
    except OversizedRawEventError as exc:
        logger.warning("ingest: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="evento maior que o teto do data-plane (KAFKA_MAX_REQUEST_BYTES)",
        ) from None
    except Exception as exc:  # noqa: BLE001 — qualquer falha do broker = não gravado
        logger.warning(
            "ingest: tópico raw indisponível integration_id=%s (%s) — 503",
            integ.id, type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="data-plane indisponível — re-tente após o Retry-After",
            headers={"Retry-After": str(_RETRY_AFTER_S)},
        ) from None
    INGEST_ACCEPTED.labels(vendor=integ.platform, stream=stream).inc(accepted)
    return accepted


# ── 1. Endpoint de ingestão (token-auth) ────────────────────────────────────


@router.post("/{stream}", response_model=IngestResponse)
async def ingest(
    stream: str,
    request: Request,
    response: Response,
    authorization: Optional[str] = Header(default=None),
    x_centralops_ingest_token: Optional[str] = Header(default=None),
    db: Session = Depends(database.get_session),
) -> IngestResponse:
    """Recebe eventos crus de um edge-collector e os grava no tópico raw.

    Autenticado pelo **token de ingestão** da integração (Bearer) — NÃO por
    sessão de usuário. O token é a credencial e é escopado a UMA integração: ele
    concede acesso a todos os ``stream`` registrados DESSA integração (o ``stream``
    no path é só o canal de dados, não um vetor de escopo). Não há acesso
    cross-org: o token resolve exatamente a integração que o emitiu. Valida que a
    plataforma é ``transport="push"`` e que o ``stream`` existe para ela."""
    token = _extract_token(authorization, x_centralops_ingest_token)
    integ = _authenticate(db, token)
    _admit(integ, stream)

    body = await _read_body(request)
    batch = _parse_events(body, request.headers.get("content-type", ""))
    _count_rejections(integ, stream, batch)

    def _resp(accepted: int) -> IngestResponse:
        # 207 Multi-Status quando houve rejeição parcial no parse (a borda deve inspecionar
        # os contadores); 200 quando tudo foi aproveitado. Mantém corpo IngestResponse.
        if batch.rejected:
            response.status_code = status.HTTP_207_MULTI_STATUS
        return IngestResponse(
            accepted=accepted,
            parse_errors=batch.parse_errors, type_errors=batch.type_errors,
            oversized=batch.oversized, error_detail=batch.first_error,
        )

    if not batch.events:
        return _resp(accepted=0)

    stamped = [_stamp(ev, stream) for ev in batch.events]
    return _resp(accepted=await _store_events(integ, stream, stamped))


# ── 1b. Splunk HEC e OTLP/HTTP (ADR-0018 Onda 2b) ───────────────────────────
#
# Mesmos tokens, mesma admissão e mesma gravação durável do endpoint genérico:
# HEC e OTLP são só formatos de fio. O ADR previa um gateway Vector para eles;
# ficou nativo porque o caminho de ingestão já tem o que o gateway traria (auth
# por integração, ack do Kafka antes do 200, 503 com Retry-After), sem um
# componente a mais, sem mapear token HEC → integração (o token HEC É o token de
# ingestão) e sem a revisão de licença de um binário a mais na distribuição.
# O Vector segue como o agente de BORDA recomendado (compose/edge).

_HEC_OK = {"text": "Success", "code": 0}


def _parse_hec(body: bytes, *, raw: bool) -> _ParsedBatch:
    """HEC: objetos JSON CONCATENADOS (com ou sem quebra de linha), cada um com
    ``event`` (objeto ou texto) e metadados opcionais. ``raw``: uma linha = um
    evento ``{"message": ...}``. Metadados do HEC vão para ``_hec``."""
    if len(body) > _MAX_BODY_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="payload grande demais")
    text = body.decode("utf-8", errors="replace")
    batch = _ParsedBatch()
    if raw:
        for line in text.splitlines():
            if line.strip():
                _collect(batch, {"message": line})
    else:
        decoder = json.JSONDecoder()
        i, n = 0, len(text)
        while True:
            while i < n and text[i].isspace():
                i += 1
            if i >= n:
                break
            try:
                obj, i = decoder.raw_decode(text, i)
            except ValueError as exc:
                # Sem como ressincronizar depois de um objeto quebrado: o resto
                # do corpo é contado como um erro de parse.
                batch.parse_errors += 1
                batch._note(f"HEC: JSON inválido: {exc}")
                break
            if not isinstance(obj, dict) or "event" not in obj:
                batch.type_errors += 1
                batch._note("HEC: objeto sem campo 'event'")
                continue
            event = obj["event"]
            if isinstance(event, dict):
                out = dict(event)
            elif isinstance(event, str):
                out = {"message": event}
            else:
                out = {"message": json.dumps(event, default=str)}
            meta = {k: obj[k] for k in ("time", "host", "source", "sourcetype", "index", "fields") if k in obj}
            if meta:
                out["_hec"] = meta
            _collect(batch, out)
    if len(batch.events) > _MAX_EVENTS_PER_REQUEST:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"máx. {_MAX_EVENTS_PER_REQUEST} eventos por requisição",
        )
    return batch


async def _ingest_hec(
    stream: str, request: Request, authorization: Optional[str], x_token: Optional[str], db: Session, *, raw: bool
) -> Dict[str, Any]:
    integ = _authenticate(db, _extract_token(authorization, x_token))
    _admit(integ, stream)
    batch = _parse_hec(await _read_body(request), raw=raw)
    _count_rejections(integ, stream, batch)
    if not batch.events:
        if batch.rejected:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid data format")
        return dict(_HEC_OK)
    stamped = [_stamp(ev, stream) for ev in batch.events]
    await _store_events(integ, stream, stamped)
    return dict(_HEC_OK)


@router.post("/hec/{stream}/services/collector")
@router.post("/hec/{stream}/services/collector/event")
@router.post("/hec/{stream}/services/collector/event/1.0")
async def ingest_hec_event(
    stream: str,
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_centralops_ingest_token: Optional[str] = Header(default=None),
    db: Session = Depends(database.get_session),
) -> Dict[str, Any]:
    """Splunk HEC (``/services/collector/event``). Configure o cliente com a URL base
    ``https://<host>/api/ingest/hec/<stream>`` e o token de ingestão como token HEC.
    200 só depois do ack do data-plane; 503 com Retry-After quando não gravou."""
    return await _ingest_hec(stream, request, authorization, x_centralops_ingest_token, db, raw=False)


@router.post("/hec/{stream}/services/collector/raw")
@router.post("/hec/{stream}/services/collector/raw/1.0")
async def ingest_hec_raw(
    stream: str,
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_centralops_ingest_token: Optional[str] = Header(default=None),
    db: Session = Depends(database.get_session),
) -> Dict[str, Any]:
    """Splunk HEC raw: uma linha do corpo = um evento ``{"message": ...}``."""
    return await _ingest_hec(stream, request, authorization, x_centralops_ingest_token, db, raw=True)


@router.get("/hec/{stream}/services/collector/health")
@router.get("/hec/{stream}/services/collector/health/1.0")
async def ingest_hec_health(stream: str) -> Dict[str, Any]:
    """Healthcheck que clientes HEC (ex.: Vector) chamam antes de enviar."""
    return {"text": "HEC is healthy", "code": 17}


def _otlp_value(value: Any) -> Any:
    """``AnyValue`` do OTLP (JSON, camelCase ou snake_case) → valor Python."""
    if not isinstance(value, dict):
        return value
    for key in ("stringValue", "string_value"):
        if key in value:
            return value[key]
    for key in ("boolValue", "bool_value", "doubleValue", "double_value", "bytesValue", "bytes_value"):
        if key in value:
            return value[key]
    for key in ("intValue", "int_value"):
        if key in value:
            try:
                return int(value[key])
            except (TypeError, ValueError):
                return value[key]
    for key in ("arrayValue", "array_value"):
        if key in value:
            return [_otlp_value(v) for v in (value[key] or {}).get("values", [])]
    for key in ("kvlistValue", "kvlist_value"):
        if key in value:
            return _otlp_attrs((value[key] or {}).get("values", []))
    return None


def _otlp_attrs(items: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for kv in items or []:
        if isinstance(kv, dict) and "key" in kv:
            out[str(kv["key"])] = _otlp_value(kv.get("value"))
    return out


def _g(obj: Dict[str, Any], camel: str, snake: str) -> Any:
    return obj.get(camel, obj.get(snake))


def _flatten_otlp_logs(doc: Any) -> _ParsedBatch:
    """``ExportLogsServiceRequest`` (forma JSON) → um evento por ``logRecord``.
    Corpo em mapa vira os campos do evento; corpo texto vira ``message``. O resto
    (atributos, recurso, escopo, severidade, tempos, trace) vai para ``_otlp``."""
    batch = _ParsedBatch()
    if not isinstance(doc, dict):
        batch.type_errors += 1
        batch._note("OTLP: corpo não é um objeto")
        return batch
    for rl in _g(doc, "resourceLogs", "resource_logs") or []:
        resource = _otlp_attrs(((rl or {}).get("resource") or {}).get("attributes"))
        for sl in _g(rl, "scopeLogs", "scope_logs") or []:
            scope = (sl or {}).get("scope") or {}
            for rec in _g(sl, "logRecords", "log_records") or []:
                if not isinstance(rec, dict):
                    batch.type_errors += 1
                    continue
                body = _otlp_value(rec.get("body"))
                event: Dict[str, Any] = dict(body) if isinstance(body, dict) else {}
                if body is not None and not isinstance(body, dict):
                    event["message"] = body
                meta = {
                    "time_unix_nano": _g(rec, "timeUnixNano", "time_unix_nano"),
                    "observed_time_unix_nano": _g(rec, "observedTimeUnixNano", "observed_time_unix_nano"),
                    "severity_number": _g(rec, "severityNumber", "severity_number"),
                    "severity_text": _g(rec, "severityText", "severity_text"),
                    "attributes": _otlp_attrs(rec.get("attributes")),
                    "resource": resource,
                    "scope": {k: scope[k] for k in ("name", "version") if scope.get(k)},
                    "trace_id": _g(rec, "traceId", "trace_id"),
                    "span_id": _g(rec, "spanId", "span_id"),
                }
                event["_otlp"] = {k: v for k, v in meta.items() if v not in (None, "", {}, [])}
                _collect(batch, event)
    if len(batch.events) > _MAX_EVENTS_PER_REQUEST:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"máx. {_MAX_EVENTS_PER_REQUEST} eventos por requisição",
        )
    return batch


def _otlp_protobuf_to_dict(body: bytes) -> Dict[str, Any]:
    """OTLP/HTTP protobuf → a mesma forma do JSON. Usa o ``opentelemetry-proto`` que
    vem com o exporter OTLP da imagem; sem ele, 415 (mande JSON)."""
    try:
        from google.protobuf import json_format
        from opentelemetry.proto.collector.logs.v1 import logs_service_pb2
    except ImportError:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="OTLP protobuf indisponível nesta imagem — use application/json",
        ) from None
    message = logs_service_pb2.ExportLogsServiceRequest()
    try:
        message.ParseFromString(body)
    except Exception:  # noqa: BLE001 — protobuf malformado
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="OTLP protobuf inválido") from None
    return json_format.MessageToDict(message)


def _otlp_protobuf_response() -> bytes:
    try:
        from opentelemetry.proto.collector.logs.v1 import logs_service_pb2

        return logs_service_pb2.ExportLogsServiceResponse().SerializeToString()
    except ImportError:  # pragma: no cover — só chega aqui quem já parseou protobuf
        return b""


@router.post("/otlp/{stream}/v1/logs")
async def ingest_otlp_logs(
    stream: str,
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_centralops_ingest_token: Optional[str] = Header(default=None),
    db: Session = Depends(database.get_session),
) -> Response:
    """OTLP/HTTP logs (JSON ou protobuf, gzip opcional). Configure o exporter com
    ``endpoint: https://<host>/api/ingest/otlp/<stream>`` e o header
    ``Authorization: Bearer <token de ingestão>``. 200 só depois do ack do
    data-plane; 503 com Retry-After (retentável pelo spec OTLP) quando não gravou."""
    integ = _authenticate(db, _extract_token(authorization, x_centralops_ingest_token))
    _admit(integ, stream)
    body = await _read_body(request)
    content_type = (request.headers.get("content-type") or "").lower()
    is_protobuf = "protobuf" in content_type
    if is_protobuf:
        doc: Any = _otlp_protobuf_to_dict(body)
    else:
        try:
            doc = json.loads(body or b"{}")
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="OTLP JSON inválido") from None
    batch = _flatten_otlp_logs(doc)
    _count_rejections(integ, stream, batch)
    if batch.events:
        stamped = [_stamp(ev, stream) for ev in batch.events]
        await _store_events(integ, stream, stamped)
    if is_protobuf:
        return Response(content=_otlp_protobuf_response(), media_type="application/x-protobuf")
    payload: Dict[str, Any] = {}
    if batch.rejected:
        payload["partialSuccess"] = {
            "rejectedLogRecords": str(batch.rejected),
            "errorMessage": batch.first_error or "registros rejeitados",
        }
    return Response(content=json.dumps(payload), media_type="application/json")

# ── 1c. Amazon Data Firehose (HTTP endpoint) ────────────────────────────────
#
# A AWS empurra direto: destino "HTTP endpoint" do Firehose com a URL
# ``https://<host>/api/ingest/firehose/<stream>`` e o token de ingestão como
# "access key" (chega em ``X-Amz-Firehose-Access-Key``). A decodificação dos
# registros mora em ``collectors.vendors.aws_firehose``.
#
# O Firehose só entende resposta JSON com ``requestId``/``timestamp`` — também nos
# erros — e trata 413 como falha PERMANENTE, sem gravar o lote no bucket de
# backup. Nenhuma recusa daqui sai como 413: o lote grande demais vira 500, que
# ele re-tenta e, esgotado o prazo, grava no backup em vez de perder.

# Teto do que os registros descompactam num pedido (os da assinatura do
# CloudWatch Logs vêm em gzip e crescem ~10x). O corpo em si segue o teto comum.
_FIREHOSE_MAX_DECODED_BYTES = 64 * 1024 * 1024
# Um registro do CloudWatch Logs carrega vários log events; o teto é por pedido.
_FIREHOSE_MAX_EVENTS = 50_000
_FIREHOSE_LOWER_BUFFER = (
    "lote grande demais para o CentralOps: reduza o 'buffer size' do destino HTTP "
    "endpoint no Firehose (recomendado: 1 a 5 MiB)"
)


def _firehose_response(
    request_id: str, status_code: int = 200, error: Optional[str] = None, retry_after: Optional[str] = None
) -> Response:
    payload: Dict[str, Any] = {"requestId": request_id, "timestamp": int(time.time() * 1000)}
    if error:
        payload["errorMessage"] = error[:8192]
    headers = {"Retry-After": retry_after} if retry_after else None
    return Response(
        content=json.dumps(payload), status_code=status_code, media_type="application/json", headers=headers
    )


@router.post("/firehose/{stream}")
async def ingest_firehose(
    stream: str,
    request: Request,
    x_amz_firehose_access_key: Optional[str] = Header(default=None),
    x_amz_firehose_request_id: Optional[str] = Header(default=None),
    x_amz_firehose_source_arn: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    x_centralops_ingest_token: Optional[str] = Header(default=None),
    db: Session = Depends(database.get_session),
) -> Response:
    """Destino HTTP endpoint do Amazon Data Firehose. 200 só depois do ack do
    data-plane; erro = JSON no formato do Firehose, que re-tenta (e grava no
    backup S3 quando o prazo de retry acaba)."""
    request_id = (x_amz_firehose_request_id or "").strip()
    try:
        token = (x_amz_firehose_access_key or "").strip() or _extract_token(
            authorization, x_centralops_ingest_token
        )
        integ = _authenticate(db, token)
        _admit(integ, stream)
        body = await _read_body(request)
        try:
            decoded = aws_firehose.decode_request(
                body,
                stream=stream,
                aws_specific=integ.platform == aws_firehose.PLATFORM,
                source_arn=x_amz_firehose_source_arn or "",
                budget=_FIREHOSE_MAX_DECODED_BYTES,
            )
        except aws_firehose.FirehoseRequestError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
        except aws_firehose.FirehoseTooLarge:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=_FIREHOSE_LOWER_BUFFER
            ) from None
        request_id = request_id or decoded.request_id

        batch = _ParsedBatch()
        for event in decoded.events:
            _collect(batch, event)
        if decoded.errors:
            # Registro que não decodifica não melhora no retry: conta, avisa e
            # segue com o resto do lote (o Firehose não tem sucesso parcial).
            batch.parse_errors += decoded.errors
            batch._note(decoded.first_error or "registro inválido")
        _count_rejections(integ, stream, batch)
        if len(batch.events) > _FIREHOSE_MAX_EVENTS:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=_FIREHOSE_LOWER_BUFFER
            )
        if batch.events:
            await _store_events(integ, stream, [_stamp(ev, stream) for ev in batch.events])
    except HTTPException as exc:
        code = exc.status_code
        if code == status.HTTP_413_REQUEST_ENTITY_TOO_LARGE:
            code = status.HTTP_500_INTERNAL_SERVER_ERROR
        detail = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail, default=str)
        if exc.status_code == status.HTTP_413_REQUEST_ENTITY_TOO_LARGE and detail != _FIREHOSE_LOWER_BUFFER:
            detail = f"{detail} — {_FIREHOSE_LOWER_BUFFER}"
        return _firehose_response(request_id, code, detail, (exc.headers or {}).get("Retry-After"))
    except ApiError as exc:
        code = exc.status_code
        detail = exc.render()
        if code == status.HTTP_413_REQUEST_ENTITY_TOO_LARGE:
            code, detail = status.HTTP_500_INTERNAL_SERVER_ERROR, f"{detail} — {_FIREHOSE_LOWER_BUFFER}"
        return _firehose_response(request_id, code, detail)
    return _firehose_response(request_id)


# ── 2. Gestão de token (admin) ──────────────────────────────────────────────


def _load_push_integration(
    db: Session, integration_id: int, user: models.AppUser
) -> models.Integration:
    integ = db.get(models.Integration, integration_id)
    if integ is None:
        raise ApiError(
            "ingest.integration_not_found",
            status.HTTP_404_NOT_FOUND,
            messages={
                "pt": "integração não encontrada",
                "en": "integration not found",
                "es": "integración no encontrada",
            },
        )
    # Isolamento multi-tenant: um admin escopado a uma org NÃO pode emitir/ler
    # token de integração de OUTRA org (mesmo padrão de integrations.py e
    # destinations.py). Global vê tudo.
    tenant.require_subtree_access(user, integ.organization_id)
    reg = collector_registry.get_platform(integ.platform)
    if reg is None or reg.transport != "push":
        raise ApiError(
            "ingest.not_push_integration",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            messages={
                "pt": "integração não é uma fonte push (sem token de ingestão)",
                "en": "integration is not a push source (no ingestion token)",
                "es": "la integración no es una fuente push (sin token de ingesta)",
            },
        )
    return integ


@router.post("/integrations/{integration_id}/token", response_model=IngestTokenResponse)
def issue_ingest_token(
    integration_id: int,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    db: Session = Depends(database.get_session),
) -> IngestTokenResponse:
    """Emite (ou ROTACIONA) o token de ingestão da integração. Devolve o token em
    claro UMA vez — o operador o injeta no edge-collector."""
    integ = _load_push_integration(db, integration_id, user)
    token = ingest_tokens.issue(integ)
    db.commit()
    logger.info("ingest: token emitido/rotacionado integration_id=%s", integration_id)
    return IngestTokenResponse(token=token, endpoint="/api/ingest")


@router.delete("/integrations/{integration_id}/token", status_code=status.HTTP_204_NO_CONTENT)
def revoke_ingest_token(
    integration_id: int,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    db: Session = Depends(database.get_session),
) -> Response:
    """Revoga o token de ingestão (mata um token vazado SEM precisar rotacionar).
    Após isso, o endpoint de ingestão passa a recusar (401) requisições com o token
    revogado. 404 se não havia token ativo."""
    integ = _load_push_integration(db, integration_id, user)
    revoked = ingest_tokens.revoke(integ)
    if not revoked:
        raise ApiError(
            "ingest.no_active_token",
            status.HTTP_404_NOT_FOUND,
            messages={
                "pt": "nenhum token de ingestão ativo",
                "en": "no active ingestion token",
                "es": "ningún token de ingesta activo",
            },
        )
    db.commit()
    logger.info("ingest: token revogado integration_id=%s", integration_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/integrations/{integration_id}", response_model=IngestInfo)
async def get_ingest_info(
    integration_id: int,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    db: Session = Depends(database.get_session),
) -> IngestInfo:
    """Metadados de ingestão: streams, endpoint e se há token."""
    integ = _load_push_integration(db, integration_id, user)
    reg = collector_registry.get_platform(integ.platform)
    streams = collector_registry.supported_streams(integ.platform)
    return IngestInfo(
        integration_id=integ.id,
        platform=integ.platform,
        transport="push",
        streams=streams,
        has_token=ingest_tokens.has_token(integ),
        endpoint_base="/api/ingest",
        icon_id=(reg.icon_id if reg is not None else None),
    )
