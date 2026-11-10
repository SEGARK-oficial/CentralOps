"""AWS via Amazon Data Firehose — recebimento PUSH em quase tempo real.

Os coletores AWS de pull (``aws_cloudtrail``, ``aws_waf``, ``aws_securityhub``,
``aws_guardduty``, ``aws_cloudwatch``) leem do S3 ou de APIs a cada ciclo: o atraso
é o do ciclo mais o da entrega da AWS no bucket (5 a 15 min no CloudTrail). Aqui a
própria AWS EMPURRA: um stream do Firehose com destino "HTTP endpoint" aponta para
``POST /api/ingest/firehose/<stream>`` e o token de ingestão da integração vai como
a "access key" do destino (header ``X-Amz-Firehose-Access-Key``).

Contrato do HTTP endpoint (docs da AWS, "HTTP endpoint delivery request and
response specifications"):

- corpo ``{"requestId", "timestamp", "records": [{"data": <base64>}]}``, até 10.000
  registros; ``Content-Encoding: gzip`` quando a compressão está ligada no destino;
- a resposta é SEMPRE ``application/json`` com ``requestId`` (o mesmo do pedido) e
  ``timestamp``; só o 200 conta como entregue;
- **413 é falha permanente** e o lote NÃO vai para o bucket de backup. Por isso
  nenhuma recusa nossa sai como 413: lote grande demais vira 500 (retentável) com
  a instrução de baixar o buffer — esgotado o retry, o Firehose grava no backup.

Cada registro é decodificado aqui, no recebimento, ANTES de ir para o tópico raw
(mesmo modelo do HEC e do OTLP no ``routers/ingest``); o ``processor`` recebe o
evento já na forma das integrações de pull:

- base64 → bytes; gzip (assinatura ``1f 8b``) é descompactado — é como chegam os
  registros de uma assinatura do CloudWatch Logs;
- o texto pode ter um ou vários JSON concatenados (com ou sem quebra de linha);
- envelope do CloudWatch Logs (``DATA_MESSAGE``) vira um evento por ``logEvent``;
  ``CONTROL_MESSAGE`` é descartado;
- envelope do EventBridge (``detail-type``/``detail``) entrega o ``detail``: o
  achado do GuardDuty, a lista ``findings`` do Security Hub, o registro do
  CloudTrail ("AWS API Call via CloudTrail");
- o achado do GuardDuty no EventBridge vem em ``camelCase`` e a API devolve
  ``PascalCase``: as chaves são convertidas para o mesmo mapping servir aos dois.

Os streams reaproveitam os mappings das integrações de pull (mesmo formato de
registro), registrados para ``aws_firehose`` em ``normalize/defaults``.
"""

from __future__ import annotations

import base64
import binascii
import json
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .aws_waf import unwrap_cloudwatch_event, waf_message_id
from .push_ingest import PushSourceCollector, _push_refresher

PLATFORM = "aws_firehose"

STREAM_WAF = "waf"
STREAM_CLOUDTRAIL = "cloudtrail"
STREAM_GUARDDUTY = "guardduty"
STREAM_SECURITYHUB = "securityhub"
STREAM_CLOUDWATCH = "cloudwatch_logs"

#: Valor de ``centralops.source`` nos registros do WAF que chegam por aqui.
SOURCE_FIREHOSE = "firehose"

_GZIP_MAGIC = b"\x1f\x8b"


class FirehoseRequestError(ValueError):
    """O corpo não é um pedido do Firehose (nem JSON, ou sem ``records``)."""


class FirehoseTooLarge(ValueError):
    """Os registros descompactados passaram do orçamento do pedido."""


@dataclass
class FirehoseBatch:
    request_id: str = ""
    records: int = 0
    events: List[Dict[str, Any]] = field(default_factory=list)
    errors: int = 0
    first_error: Optional[str] = None

    def reject(self, msg: str) -> None:
        self.errors += 1
        if self.first_error is None:
            self.first_error = msg


# ── Contexto da origem ───────────────────────────────────────────────────


def arn_region_account(arn: Any) -> tuple[Optional[str], Optional[str]]:
    """``arn:aws:<svc>:<região>:<conta>:...`` → (região, conta); vazio = None."""
    if not isinstance(arn, str) or not arn.startswith("arn:"):
        return None, None
    parts = arn.split(":", 5)
    if len(parts) < 6:
        return None, None
    return (parts[3] or None), (parts[4] or None)


def source_context(source_arn: str) -> Dict[str, Optional[str]]:
    """Região, conta e nome do stream a partir do ``X-Amz-Firehose-Source-Arn``
    (``arn:aws:firehose:<região>:<conta>:deliverystream/<nome>``)."""
    region, account = arn_region_account(source_arn)
    name = None
    if isinstance(source_arn, str) and ":deliverystream/" in source_arn:
        name = source_arn.split(":deliverystream/", 1)[1] or None
    return {"region": region, "account": account, "stream": name}


# ── Decodificação ────────────────────────────────────────────────────────


def _gunzip(raw: bytes, limit: int) -> bytes:
    """gzip com teto no tamanho DESCOMPACTADO (um registro pequeno não pode virar
    bomba de memória). Passou do teto ⇒ :class:`FirehoseTooLarge`."""
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = d.decompress(raw, max(limit, 0) + 1)
    if len(out) > limit or d.unconsumed_tail:
        raise FirehoseTooLarge("registros descompactados acima do teto do pedido")
    return out


def split_payloads(text: str) -> List[Any]:
    """Um ou vários JSON no mesmo registro, concatenados com ou sem quebra de
    linha (o Firehose não põe delimitador; quem escreve no stream escolhe). Um
    trecho que não é JSON sai como texto até o fim da linha."""
    out: List[Any] = []
    dec = json.JSONDecoder()
    i, n = 0, len(text)
    while True:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            return out
        try:
            obj, i = dec.raw_decode(text, i)
        except ValueError:
            j = text.find("\n", i)
            j = n if j < 0 else j
            line = text[i:j].strip()
            if line:
                out.append(line)
            i = j
            continue
        out.append(obj)


def pascalize(value: Any) -> Any:
    """Chaves ``camelCase`` → ``PascalCase`` em profundidade (achado do GuardDuty no
    EventBridge → forma da API ``GetFindings``, que é a do mapping)."""
    if isinstance(value, dict):
        return {
            (k[:1].upper() + k[1:] if isinstance(k, str) else k): pascalize(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [pascalize(v) for v in value]
    return value


def _iso_ms(ts: Any) -> Optional[str]:
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    try:
        return (
            datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    except (OverflowError, OSError, ValueError):
        return None


def _cloudwatch_event(le: Dict[str, Any], env: Dict[str, Any], ctx: Dict[str, Optional[str]]) -> Dict[str, Any]:
    """``logEvent`` de uma assinatura → a mesma forma do coletor ``aws_cloudwatch``
    (``FilterLogEvents`` + contexto), para o mapping dele servir aqui."""
    ev: Dict[str, Any] = {
        "eventId": le.get("id"),
        "timestamp": le.get("timestamp"),
        "message": le.get("message"),
        "logGroupName": env.get("logGroup"),
        "logStreamName": env.get("logStream"),
        "awsRegion": ctx.get("region"),
        "awsAccountId": env.get("owner") or ctx.get("account"),
    }
    iso = _iso_ms(le.get("timestamp"))
    if iso:
        ev["eventTime"] = iso
    return {k: v for k, v in ev.items() if v is not None}


def _is_cloudwatch_envelope(obj: Any) -> bool:
    return isinstance(obj, dict) and "messageType" in obj and isinstance(obj.get("logEvents"), list)


def _is_eventbridge(obj: Dict[str, Any]) -> bool:
    return "detail-type" in obj and "detail" in obj and "source" in obj


def _waf_record(rec: Dict[str, Any], ctx: Dict[str, Optional[str]]) -> Dict[str, Any]:
    """Registro do WAF + bloco ``centralops``. A conta e a região saem do
    ``webaclId`` (ARN da web ACL); sem ele, as do stream do Firehose."""
    region, account = arn_region_account(rec.get("webaclId"))
    meta = rec.get("centralops") if isinstance(rec.get("centralops"), dict) else {}
    meta = dict(meta)
    meta["source"] = SOURCE_FIREHOSE
    if not meta.get("region"):
        meta["region"] = region or ctx.get("region")
    if not meta.get("accountId"):
        meta["accountId"] = account or ctx.get("account")
    if ctx.get("stream"):
        meta["deliveryStream"] = ctx["stream"]
    out = dict(rec)
    out["centralops"] = {k: v for k, v in meta.items() if v is not None}
    return out


def _guardduty_finding(detail: Dict[str, Any]) -> Dict[str, Any]:
    """Achado do GuardDuty do EventBridge → forma do coletor de pull: chaves da
    API (``PascalCase``) + o mesmo bloco ``centralops`` (detector e região)."""
    out = pascalize(detail)
    service = out.get("Service") if isinstance(out.get("Service"), dict) else {}
    meta = {"detectorId": service.get("DetectorId"), "region": out.get("Region")}
    out.setdefault("centralops", {k: v for k, v in meta.items() if v})
    return out


def _looks_lower_camel_guardduty(obj: Dict[str, Any]) -> bool:
    return "AccountId" not in obj and ("accountId" in obj or "schemaVersion" in obj)


def _unwrap(obj: Any, stream: str, ctx: Dict[str, Optional[str]], aws: bool, batch: FirehoseBatch, depth: int = 0) -> None:
    """Um JSON (ou texto) de um registro → zero ou mais eventos no ``batch``."""
    if depth > 3:
        batch.reject("envelope aninhado demais")
        return

    if _is_cloudwatch_envelope(obj):
        if obj.get("messageType") != "DATA_MESSAGE":
            return  # CONTROL_MESSAGE: o teste de alcance do CloudWatch, sem dado
        for le in obj["logEvents"]:
            if not isinstance(le, dict):
                batch.reject("logEvent do CloudWatch não é objeto")
                continue
            cw = _cloudwatch_event(le, obj, ctx)
            if not aws or stream == STREAM_CLOUDWATCH:
                batch.events.append(cw)
            elif stream == STREAM_WAF:
                rec = unwrap_cloudwatch_event(cw)
                batch.events.append(_waf_record(rec, ctx))
            else:
                try:
                    inner = json.loads(le.get("message") or "")
                except (TypeError, ValueError):
                    batch.reject(f"mensagem do CloudWatch não é JSON (stream {stream})")
                    continue
                _unwrap(inner, stream, ctx, aws, batch, depth + 1)
        return

    if not isinstance(obj, dict):
        text = obj if isinstance(obj, str) else json.dumps(obj)
        if not aws or stream == STREAM_CLOUDWATCH:
            batch.events.append({"message": text})
        else:
            batch.reject(f"registro sem JSON no stream {stream}")
        return

    if not aws or stream == STREAM_CLOUDWATCH:
        batch.events.append(obj)
        return

    if _is_eventbridge(obj):
        detail = obj.get("detail")
        if stream == STREAM_SECURITYHUB and isinstance(detail, dict) and isinstance(detail.get("findings"), list):
            for finding in detail["findings"]:
                if isinstance(finding, dict):
                    batch.events.append(finding)
                else:
                    batch.reject("achado do Security Hub não é objeto")
        elif stream == STREAM_GUARDDUTY and isinstance(detail, dict):
            batch.events.append(_guardduty_finding(detail))
        elif stream == STREAM_CLOUDTRAIL and isinstance(detail, dict):
            batch.events.append(detail)
        else:
            batch.reject(f"evento do EventBridge ({obj.get('detail-type')!r}) não serve ao stream {stream}")
        return

    if stream == STREAM_CLOUDTRAIL and isinstance(obj.get("Records"), list):
        for rec in obj["Records"]:
            if isinstance(rec, dict):
                batch.events.append(rec)
            else:
                batch.reject("registro do CloudTrail não é objeto")
        return
    if stream == STREAM_SECURITYHUB and isinstance(obj.get("findings"), list):
        for finding in obj["findings"]:
            if isinstance(finding, dict):
                batch.events.append(finding)
            else:
                batch.reject("achado do Security Hub não é objeto")
        return
    if stream == STREAM_GUARDDUTY and _looks_lower_camel_guardduty(obj):
        batch.events.append(_guardduty_finding(obj))
        return
    if stream == STREAM_WAF:
        batch.events.append(_waf_record(obj, ctx))
        return
    batch.events.append(obj)


def decode_request(
    body: bytes,
    *,
    stream: str,
    aws_specific: bool = True,
    source_arn: str = "",
    budget: int = 64 * 1024 * 1024,
) -> FirehoseBatch:
    """Corpo do pedido do Firehose → eventos prontos para o buffer.

    ``aws_specific`` liga as regras por stream (integração ``aws_firehose``); sem
    ele (outra fonte push recebendo por Firehose) só o base64, o gzip e o
    envelope do CloudWatch são desfeitos. Um registro ruim é contado e pulado;
    o pedido inteiro só falha quando não é um pedido do Firehose ou quando o que
    os registros descompactam passa de ``budget`` bytes."""
    try:
        doc = json.loads(body or b"")
    except ValueError:
        raise FirehoseRequestError("corpo não é JSON") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("records"), list):
        raise FirehoseRequestError("corpo sem a lista 'records' do Firehose")

    batch = FirehoseBatch(request_id=str(doc.get("requestId") or ""))
    ctx = source_context(source_arn)
    remaining = budget
    for rec in doc["records"]:
        batch.records += 1
        data = rec.get("data") if isinstance(rec, dict) else None
        if isinstance(data, dict):
            # O exemplo de CloudWatch Logs da própria AWS mostra ``data`` como
            # objeto, não base64. Aceito igual.
            payloads: List[Any] = [data]
        elif isinstance(data, str):
            try:
                raw = base64.b64decode(data, validate=True)
            except (binascii.Error, ValueError):
                batch.reject("registro com 'data' que não é base64")
                continue
            if raw[:2] == _GZIP_MAGIC:
                try:
                    raw = _gunzip(raw, remaining)
                except zlib.error:
                    batch.reject("registro gzip inválido")
                    continue
            remaining -= len(raw)
            if remaining < 0:
                raise FirehoseTooLarge("registros descompactados acima do teto do pedido")
            payloads = split_payloads(raw.decode("utf-8", errors="replace"))
        else:
            batch.reject("registro sem 'data'")
            continue
        for payload in payloads:
            _unwrap(payload, stream, ctx, aws_specific, batch)
    return batch


# ── Coletores (só event_type e id de dedupe para o processor) ────────────


class _FirehoseCollector(PushSourceCollector):
    platform = PLATFORM

    def _native_id(self, event: Dict[str, Any]) -> str:
        return ""

    def extract_message_id(self, event: Dict[str, Any]) -> str:
        # Id nativo da AWS quando existe: o mesmo evento reentregue pelo retry do
        # Firehose (ou por dois streams) cai no dedupe. Sem ele, o hash do
        # conteúdo carimbado no recebimento.
        return self._native_id(event) or super().extract_message_id(event)


class FirehoseWafCollector(_FirehoseCollector):
    stream = STREAM_WAF
    event_type = "aws_firehose.waf"

    def _native_id(self, event: Dict[str, Any]) -> str:
        return waf_message_id(event)


class FirehoseCloudTrailCollector(_FirehoseCollector):
    stream = STREAM_CLOUDTRAIL
    event_type = "aws_firehose.cloudtrail"

    def _native_id(self, event: Dict[str, Any]) -> str:
        return str(event.get("eventID") or "")


def _finding_id(event: Dict[str, Any]) -> str:
    fid = event.get("Id")
    return f"{fid}|{event.get('UpdatedAt', '')}" if fid else ""


class FirehoseGuardDutyCollector(_FirehoseCollector):
    stream = STREAM_GUARDDUTY
    event_type = "aws_firehose.guardduty"

    def _native_id(self, event: Dict[str, Any]) -> str:
        return _finding_id(event)


class FirehoseSecurityHubCollector(_FirehoseCollector):
    stream = STREAM_SECURITYHUB
    event_type = "aws_firehose.securityhub"

    def _native_id(self, event: Dict[str, Any]) -> str:
        return _finding_id(event)


class FirehoseCloudWatchCollector(_FirehoseCollector):
    stream = STREAM_CLOUDWATCH
    event_type = "aws_firehose.cloudwatch_logs"

    def _native_id(self, event: Dict[str, Any]) -> str:
        return str(event.get("eventId") or "")


COLLECTORS = (
    FirehoseWafCollector,
    FirehoseCloudTrailCollector,
    FirehoseGuardDutyCollector,
    FirehoseSecurityHubCollector,
    FirehoseCloudWatchCollector,
)


def _register() -> None:
    from datetime import timedelta

    from ..queues import Q_BULK, T_COLLECT_BULK
    from ..registry import CollectorRegistration, PlatformRegistration, register, register_platform

    register_platform(
        PlatformRegistration(
            platform=PLATFORM,
            display_name="AWS via Firehose (push)",
            category="Cloud-audit",
            description=(
                "A AWS empurra os logs em quase tempo real: um stream do Amazon Data "
                "Firehose com destino HTTP endpoint entrega WAF, CloudTrail (via "
                "CloudWatch Logs ou EventBridge), achados do GuardDuty e do Security "
                "Hub (via EventBridge) e qualquer log group do CloudWatch."
            ),
            icon_id="aws",
            docs_url="https://docs.aws.amazon.com/firehose/latest/dev/httpdeliveryrequestresponse.html",
            order=58,
            transport="push",
            capabilities=frozenset({"catalog"} | {f"collect:{c.stream}" for c in COLLECTORS}),
            # A credencial é o token de ingestão, que vai como "access key" do
            # destino HTTP do Firehose (emitido depois de criar a integração).
            auth_fields=(),
        )
    )
    for cls in COLLECTORS:
        register(
            CollectorRegistration(
                platform=PLATFORM,
                stream=cls.stream,
                collector_cls=cls,
                refresh_fn=_push_refresher,
                schedule=timedelta(seconds=20),
                queue=Q_BULK,
                task_name=T_COLLECT_BULK,
            )
        )


_register()
