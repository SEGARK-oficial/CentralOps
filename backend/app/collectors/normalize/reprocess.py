"""Helper de reprocessamento de eventos de quarentena.

Função pura ``attempt_reprocess`` — sem side effects (não faz dispatch,
não atualiza DB). Encapsula a lógica de:

1. Parsear o ``raw_payload`` JSON.
2. Resolver o ``MappingDefinition`` ativo para (vendor, event_type).
3. Aplicar regras do ``MappingVersion`` corrente via ``default_engine``.
4. Construir o envelope via ``build_envelope``.
5. Checar ``has_customer_id``.

Retorna :class:`ReprocessResult` com o envelope pronto (se sucesso) ou
o erro categorizado (se falha). Quem chama decide o que fazer:
persistir, enfileirar para dispatch, atualizar quarentena etc.

Uso típico (no router de quarentena):

    result = attempt_reprocess(
        raw_payload=ev.raw_payload,
        vendor=ev.vendor,
        event_type=ev.event_type,
        integration_id=ev.integration_id,
        organization_id=org.id,
        db=db,
    )
    if result.success:
        _enqueue_dispatch([result.envelope])
    else:
        ev.error_kind = result.error_kind
        ev.error_detail = result.error_detail
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import models
from ..normalize.engine import (
    MappingError,
    MappingRequiredFieldError,
    default_engine,
)
from ..normalize.envelope import EnvelopeContext, build_envelope, has_customer_id

logger = logging.getLogger(__name__)


@dataclass
class ReprocessResult:
    """Resultado de uma tentativa de reprocessamento.

    ``success=True`` implica ``envelope`` preenchido e
    ``error_kind=None``. ``success=False`` implica ``envelope=None``
    e ``error_kind`` categorizado.
    """

    success: bool
    envelope: Optional[Dict[str, Any]]
    error_kind: Optional[str]
    error_detail: Optional[str]
    # ID da MappingVersion que foi tentada (mesmo em falha — útil para audit).
    mapping_version_id: Optional[str]


def attempt_reprocess(
    *,
    raw_payload: str,
    vendor: str,
    event_type: Optional[str],
    integration_id: int,
    organization_id: int,
    db: Session,
    stream: Optional[str] = None,
) -> ReprocessResult:
    """Tenta aplicar o mapping atual sobre um raw payload e produzir envelope.

    Função pura em relação ao DB: faz apenas leituras (SELECT) para
    resolver o mapping. Não persiste nada, não enfileira nada.

    Args:
        raw_payload: JSON serializado do evento original (campo
            ``QuarantineEvent.raw_payload``).
        vendor: identificador do vendor (ex: "sophos").
        event_type: tipo de evento (ex: "sophos.alert"). Pode ser None
            se o evento falhou antes de ser classificado — neste caso
            retorna erro ``missing_mapping`` imediatamente.
        integration_id: ID da ``Integration`` que originou o evento.
        organization_id: ID da ``Organization`` — usado para resolver
            ``Organization.iris_customer_id``, que vai como ``customer_id``
            no envelope (padronização de IDs do IRIS DFIR no
            envelope downstream). Quando a Org não tem ``iris_customer_id``
            populado, o evento volta pra quarentena com
            ``error_kind="missing_customer_id"``.
        db: sessão SQLAlchemy ativa (somente leitura).
        stream: stream do collector (ex: "alerts"). Se None, é
            derivado de ``event_type`` (parte após o último ponto).

    Returns:
        :class:`ReprocessResult` com envelope pronto se sucesso, ou
        error_kind/error_detail se falha.
    """
    # ── 1. Parse JSON do raw payload ─────────────────────────────────
    try:
        raw: Dict[str, Any] = json.loads(raw_payload)
    except (TypeError, ValueError) as exc:
        return ReprocessResult(
            success=False,
            envelope=None,
            error_kind="parse",
            error_detail=f"raw_payload não é JSON válido: {exc}"[:2000],
            mapping_version_id=None,
        )

    # Evento truncado no armazenamento (_truncated=true): o payload original
    # foi podado para caber no limite da quarentena. Reprocessar normalizaria o
    # WRAPPER e re-quarentenaria com erro enganoso ("normalized.time resolved to
    # None"). Falha explícita e honesta em vez de produzir envelope parcial.
    if isinstance(raw, dict) and raw.get("_truncated") is True:
        return ReprocessResult(
            success=False,
            envelope=None,
            error_kind="map",
            error_detail=(
                "raw_payload truncado no armazenamento (_truncated=true) — payload "
                "original indisponível para reprocesso. Aumente "
                "QUARANTINE_RAW_MAX_BYTES e recolha o evento na origem."
            ),
            mapping_version_id=None,
        )

    # ── 2. Resolver MappingDefinition ativa ──────────────────────────
    if not event_type:
        return ReprocessResult(
            success=False,
            envelope=None,
            error_kind="missing_mapping",
            error_detail="event_type ausente — não é possível resolver mapping",
            mapping_version_id=None,
        )

    defn = db.scalar(
        select(models.MappingDefinition).where(
            models.MappingDefinition.vendor == vendor,
            models.MappingDefinition.event_type == event_type,
        )
    )
    if defn is None or not defn.current_version_id:
        return ReprocessResult(
            success=False,
            envelope=None,
            error_kind="missing_mapping",
            error_detail=(
                f"Sem MappingDefinition/versão ativa para"
                f" vendor={vendor!r} event_type={event_type!r}"
            ),
            mapping_version_id=None,
        )

    version = db.scalar(
        select(models.MappingVersion).where(
            models.MappingVersion.id == defn.current_version_id
        )
    )
    if version is None:
        return ReprocessResult(
            success=False,
            envelope=None,
            error_kind="missing_mapping",
            error_detail=f"MappingVersion id={defn.current_version_id!r} não encontrada",
            mapping_version_id=defn.current_version_id,
        )

    try:
        rules: List[Dict[str, Any]] = json.loads(version.rules)
    except (TypeError, ValueError) as exc:
        return ReprocessResult(
            success=False,
            envelope=None,
            error_kind="map",
            error_detail=f"rules da versão {version.id!r} são JSON inválido: {exc}"[:2000],
            mapping_version_id=version.id,
        )

    # ── 3. Aplicar regras ────────────────────────────────────────────
    # lê dsl_version da linha MappingVersion (default 1 para legado).
    dsl_version: int = getattr(version, "dsl_version", 1) or 1
    try:
        applied = default_engine.apply(
            version.id, rules, raw, dsl_version=dsl_version,
            # timestamp_t do OCSF é em MILISSEGUNDOS.
            ingest_time_epoch=int(time.time() * 1000),
        )
    except MappingRequiredFieldError as exc:
        return ReprocessResult(
            success=False,
            envelope=None,
            error_kind="map",
            error_detail=str(exc)[:2000],
            mapping_version_id=version.id,
        )
    except MappingError as exc:
        return ReprocessResult(
            success=False,
            envelope=None,
            error_kind="map",
            error_detail=str(exc)[:2000],
            mapping_version_id=version.id,
        )

    # ── 4. Construir envelope ────────────────────────────────────────
    # Stream derivado do event_type (parte após último ponto) quando
    # não fornecido explicitamente — convenção dos collectors internos.
    effective_stream = stream or (event_type.rsplit(".", 1)[-1] if event_type else "events")

    # customer_id do envelope = Organization.id interno (não mais o id
    # do IRIS). Eventos que caíram em quarentena pelo antigo "missing_customer_id"
    # agora SEMPRE resolvem ao reprocessar (org.id sempre existe).
    envelope_customer_id: Optional[int] = organization_id

    ctx = EnvelopeContext(
        vendor=vendor,
        integration_id=integration_id,
        customer_id=envelope_customer_id,
        stream=effective_stream,
        event_type=event_type,
        mapping_version_id=version.id,
    )
    envelope = build_envelope(applied.reduced_raw or raw, applied.output, ctx)
    if applied.ingest_fallback_targets:
        envelope["_centralops"]["degraded_fields"] = list(
            applied.ingest_fallback_targets
        )

    # ── 5. Validar customer_id ───────────────────────────────
    if not has_customer_id(envelope):
        return ReprocessResult(
            success=False,
            envelope=None,
            error_kind="missing_customer_id",
            error_detail="customer_id resolveu para vazio após reprocessamento",
            mapping_version_id=version.id,
        )

    return ReprocessResult(
        success=True,
        envelope=envelope,
        error_kind=None,
        error_detail=None,
        mapping_version_id=version.id,
    )
