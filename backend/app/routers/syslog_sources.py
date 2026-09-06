"""Fontes syslog do receptor nativo (W3.2/W3.3) — ``/api/syslog``.

A fonte diz QUEM pode falar syslog conosco (CIDR de origem, porta/transporte de
escuta) e PARA ONDE vai (integração push + stream, decidido por conteúdo pelo
classificador). Toda validação que o receptor não pode fazer a tempo acontece
aqui, na escrita: CIDR válido e não-aberto, stream que a plataforma registra,
JMESPath que compila. O receptor recarrega a tabela a cada
``SYSLOG_SOURCE_REFRESH_S`` — não há hot-path entre esta API e ele.
"""

from __future__ import annotations

import ipaddress
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..collectors import classify
from ..collectors import registry as collector_registry
from ..core import auth as app_auth
from ..core import tenant
from ..core.errors import ApiError
from ..db import database, models
from ..syslog.parser import parse_syslog_line

router = APIRouter(prefix="/syslog", tags=["syslog"])

_TRANSPORTS = ("any", "udp", "tcp", "tls")


# ── schemas ────────────────────────────────────────────────────────────────

class ClassifierRule(BaseModel):
    when: str = Field(..., min_length=1, max_length=2000, description="JMESPath ou @detector")
    stream: str = Field(..., min_length=1, max_length=63)


class ClassifierConfig(BaseModel):
    rules: List[ClassifierRule] = Field(default_factory=list, max_length=50)


class SyslogSourceCreate(BaseModel):
    integration_id: int
    name: str = Field(..., min_length=1, max_length=120)
    source_cidr: str = Field(..., min_length=1, max_length=64)
    listen_port: Optional[int] = Field(None, ge=1, le=65535)
    transport: str = "any"
    default_stream: str = Field(..., min_length=1, max_length=63)
    classifier: Optional[ClassifierConfig] = None
    enabled: bool = True


class SyslogSourceUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    source_cidr: Optional[str] = Field(None, min_length=1, max_length=64)
    listen_port: Optional[int] = Field(None, ge=0, le=65535)  # 0 = qualquer porta
    transport: Optional[str] = None
    default_stream: Optional[str] = Field(None, min_length=1, max_length=63)
    classifier: Optional[ClassifierConfig] = None
    enabled: Optional[bool] = None


class SyslogSourceRead(BaseModel):
    id: int
    organization_id: int
    integration_id: int
    platform: str
    name: str
    source_cidr: str
    listen_port: Optional[int]
    transport: str
    default_stream: str
    classifier: ClassifierConfig
    enabled: bool
    created_at: datetime
    updated_at: datetime


class FactoryDetectorRead(BaseModel):
    name: str
    label: str
    when: str


class ClassifyTestRequest(BaseModel):
    line: str = Field(..., min_length=1, max_length=65536)
    classifier: Optional[ClassifierConfig] = None
    default_stream: Optional[str] = None


class ClassifyTestResponse(BaseModel):
    parsed: Dict[str, Any]
    stream: Optional[str]
    matched_rule: bool
    trace: List[Dict[str, Any]]


# ── helpers ────────────────────────────────────────────────────────────────

def _err(code: str, http: int, pt: str, en: str, es: str, **params: Any) -> ApiError:
    return ApiError(code, http, messages={"pt": pt, "en": en, "es": es}, params=params or None)


def _load_push_integration(db: Session, integration_id: int, user: models.AppUser) -> models.Integration:
    integ = db.get(models.Integration, integration_id)
    if integ is None:
        raise _err("syslog.integration_not_found", 404, "integração não encontrada", "integration not found", "integración no encontrada")
    tenant.require_subtree_access(user, integ.organization_id)
    reg = collector_registry.get_platform(integ.platform)
    if reg is None or reg.transport != "push":
        raise _err(
            "syslog.not_push_integration", status.HTTP_422_UNPROCESSABLE_ENTITY,
            "só integrações push (FortiGate, WEC, fonte genérica) recebem syslog",
            "only push integrations (FortiGate, WEC, generic source) accept syslog",
            "solo las integraciones push (FortiGate, WEC, fuente genérica) reciben syslog",
        )
    return integ


def _validate_cidr(raw: str) -> str:
    try:
        net = ipaddress.ip_network(str(raw).strip(), strict=False)
    except ValueError:
        raise _err("syslog.invalid_cidr", status.HTTP_422_UNPROCESSABLE_ENTITY,
                   "CIDR inválido: {cidr}", "invalid CIDR: {cidr}", "CIDR inválido: {cidr}", cidr=raw)
    if net.prefixlen == 0:
        # Porta 514 não tem cabeçalho de autenticação: a rede É a credencial.
        raise _err("syslog.open_cidr", status.HTTP_422_UNPROCESSABLE_ENTITY,
                   "CIDR aberto ({cidr}) aceitaria syslog da internet inteira — restrinja à rede da fonte",
                   "open CIDR ({cidr}) would accept syslog from the whole internet — restrict it to the source network",
                   "CIDR abierto ({cidr}) aceptaría syslog de toda internet — restrínjalo a la red de la fuente", cidr=raw)
    return str(net)


def _validate_transport(t: Optional[str]) -> str:
    v = (t or "any").strip().lower()
    if v not in _TRANSPORTS:
        raise _err("syslog.invalid_transport", status.HTTP_422_UNPROCESSABLE_ENTITY,
                   "transporte inválido: {t} (use any, udp, tcp ou tls)", "invalid transport: {t} (use any, udp, tcp or tls)",
                   "transporte inválido: {t} (use any, udp, tcp o tls)", t=v)
    return v


def _validate_streams(platform: str, default_stream: str, classifier: Optional[ClassifierConfig]) -> None:
    known = set(collector_registry.supported_streams(platform))
    wanted = {default_stream, *[r.stream for r in (classifier.rules if classifier else [])]}
    missing = sorted(s for s in wanted if s not in known)
    if missing:
        raise _err("syslog.unknown_stream", status.HTTP_422_UNPROCESSABLE_ENTITY,
                   "stream(s) {streams} não existem para {platform}; crie-os antes (fonte genérica: Streams desta fonte)",
                   "stream(s) {streams} do not exist for {platform}; create them first (generic source: Streams of this source)",
                   "stream(s) {streams} no existen para {platform}; créelos antes",
                   streams=", ".join(missing), platform=platform)


def _validate_classifier(classifier: Optional[ClassifierConfig], default_stream: str) -> Optional[str]:
    cfg = classifier.model_dump() if classifier else None
    try:
        classify.compile_classifier(cfg, default_stream=default_stream)
    except classify.ClassifierError as exc:
        raise _err("syslog.invalid_classifier", status.HTTP_422_UNPROCESSABLE_ENTITY,
                   "classificador inválido: {error}", "invalid classifier: {error}", "clasificador inválido: {error}", error=str(exc))
    return json.dumps(cfg, separators=(",", ":")) if cfg and cfg.get("rules") else None


def _read(row: models.SyslogSource, platform: str) -> SyslogSourceRead:
    cfg = json.loads(row.classifier_json) if row.classifier_json else {}
    return SyslogSourceRead(
        id=int(row.id), organization_id=int(row.organization_id), integration_id=int(row.integration_id), platform=platform,
        name=row.name, source_cidr=row.source_cidr, listen_port=row.listen_port, transport=row.transport,
        default_stream=row.default_stream, classifier=ClassifierConfig(**cfg) if cfg else ClassifierConfig(),
        enabled=bool(row.enabled), created_at=row.created_at, updated_at=row.updated_at,
    )


def _get_visible(db: Session, source_id: int, user: models.AppUser) -> models.SyslogSource:
    row = db.get(models.SyslogSource, source_id)
    if row is None:
        raise _err("syslog.source_not_found", 404, "fonte syslog não encontrada", "syslog source not found", "fuente syslog no encontrada")
    tenant.require_subtree_access(user, row.organization_id)
    return row


# ── catálogo de detectores + teste de linha ────────────────────────────────

@router.get("/classifiers", response_model=List[FactoryDetectorRead])
def list_factory_detectors(_: models.AppUser = Depends(app_auth.require_authenticated_user)) -> List[FactoryDetectorRead]:
    return [FactoryDetectorRead(name=k, label=v["label"], when=v["when"]) for k, v in classify.FACTORY_DETECTORS.items()]


@router.post("/classify-test", response_model=ClassifyTestResponse)
def classify_test(payload: ClassifyTestRequest, _: models.AppUser = Depends(app_auth.require_admin_user)) -> ClassifyTestResponse:
    """Dry-run: parseia UMA linha e mostra qual regra casou. Não persiste nada."""
    cfg = payload.classifier.model_dump() if payload.classifier else None
    try:
        clf = classify.compile_classifier(cfg, default_stream=payload.default_stream)
    except classify.ClassifierError as exc:
        raise _err("syslog.invalid_classifier", status.HTTP_422_UNPROCESSABLE_ENTITY,
                   "classificador inválido: {error}", "invalid classifier: {error}", "clasificador inválido: {error}", error=str(exc))
    parsed = parse_syslog_line(payload.line)
    ex = clf.explain(parsed)
    return ClassifyTestResponse(parsed=parsed, stream=ex["stream"], matched_rule=ex["matched_rule"], trace=ex["trace"])


# ── CRUD ───────────────────────────────────────────────────────────────────

@router.get("/sources", response_model=List[SyslogSourceRead])
def list_sources(
    integration_id: Optional[int] = Query(None),
    organization_id: Optional[int] = Query(None),
    user: models.AppUser = Depends(app_auth.require_admin_user),
    db: Session = Depends(database.get_session),
) -> List[SyslogSourceRead]:
    q = db.query(models.SyslogSource, models.Integration).join(models.Integration, models.Integration.id == models.SyslogSource.integration_id)
    if integration_id is not None:
        q = q.filter(models.SyslogSource.integration_id == integration_id)
    if organization_id is not None:
        q = q.filter(models.SyslogSource.organization_id == organization_id)
    out = []
    for row, integ in q.order_by(models.SyslogSource.id.asc()).all():
        if not tenant.can_access_subtree(user, row.organization_id):
            continue
        out.append(_read(row, integ.platform))
    return out


@router.post("/sources", response_model=SyslogSourceRead, status_code=status.HTTP_201_CREATED)
def create_source(
    payload: SyslogSourceCreate,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    db: Session = Depends(database.get_session),
) -> SyslogSourceRead:
    integ = _load_push_integration(db, payload.integration_id, user)
    cidr = _validate_cidr(payload.source_cidr)
    transport = _validate_transport(payload.transport)
    _validate_streams(integ.platform, payload.default_stream, payload.classifier)
    classifier_json = _validate_classifier(payload.classifier, payload.default_stream)
    row = models.SyslogSource(
        organization_id=integ.organization_id, integration_id=integ.id, name=payload.name.strip(),
        source_cidr=cidr, listen_port=payload.listen_port, transport=transport,
        default_stream=payload.default_stream, classifier_json=classifier_json, enabled=payload.enabled,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _read(row, integ.platform)


@router.patch("/sources/{source_id}", response_model=SyslogSourceRead)
def update_source(
    source_id: int,
    payload: SyslogSourceUpdate,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    db: Session = Depends(database.get_session),
) -> SyslogSourceRead:
    row = _get_visible(db, source_id, user)
    integ = db.get(models.Integration, row.integration_id)
    fields = payload.model_dump(exclude_unset=True)
    if "source_cidr" in fields and fields["source_cidr"] is not None:
        row.source_cidr = _validate_cidr(fields["source_cidr"])
    if "transport" in fields and fields["transport"] is not None:
        row.transport = _validate_transport(fields["transport"])
    if "listen_port" in fields:
        row.listen_port = None if not fields["listen_port"] else int(fields["listen_port"])
    if "name" in fields and fields["name"]:
        row.name = fields["name"].strip()
    if "enabled" in fields and fields["enabled"] is not None:
        row.enabled = bool(fields["enabled"])
    default_stream = fields.get("default_stream") or row.default_stream
    classifier = payload.classifier if "classifier" in fields else (
        ClassifierConfig(**json.loads(row.classifier_json)) if row.classifier_json else None
    )
    if "default_stream" in fields or "classifier" in fields:
        _validate_streams(integ.platform, default_stream, classifier)
        row.classifier_json = _validate_classifier(classifier, default_stream)
        row.default_stream = default_stream
    db.commit()
    db.refresh(row)
    return _read(row, integ.platform)


@router.delete("/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_source(
    source_id: int,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    db: Session = Depends(database.get_session),
) -> None:
    row = _get_visible(db, source_id, user)
    db.delete(row)
    db.commit()
    return None
