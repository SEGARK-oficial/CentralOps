"""A triagem de uma Detection como evento OCSF 2004 (Update / Close).

Uma Detection nascia como Detection Finding (2004, ``activity_id`` Create) e
morria em silêncio: ``PATCH /detections/{id}`` mudava ``status`` no banco e
nenhum destino ficava sabendo. Um SOAR que abriu caso a partir do Create nunca
recebia o Close; o analista fechava dos dois lados, ou de um só.

Este módulo emite, pela mesma ``_enqueue_dispatch`` de todo produtor interno,
um 2004 por transição com o MESMO ``finding_info.uid`` do achado original —
que é a ``dedup_key`` da Detection, tanto para a detecção em voo quanto para o
achado por linha da scheduled query. É o que permite ao consumidor casar
Create → Update → Close sem tabela de correspondência.

Mapeamento (OCSF 1.8, Detection Finding):

    ack     → activity_id 2 (Update),  status_id 2 (In Progress)
    closed  → activity_id 3 (Close),   status_id 4 (Resolved)
    open    → activity_id 2 (Update),  status_id 1 (New)   — reabertura

Best-effort e atrás de ``DETECTION_LIFECYCLE_EVENTS`` (OFF por default): a
triagem no banco é a verdade e nunca falha por causa do fio.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Mapping, Optional

from sqlalchemy.orm import Session

from ..core.config import settings
from ..db import models

logger = logging.getLogger(__name__)

__all__ = [
    "LIFECYCLE_EVENT_TYPE",
    "LIFECYCLE_STREAM",
    "TRANSITIONS",
    "build_status_event",
    "emit_status_event",
]

LIFECYCLE_EVENT_TYPE = "centralops.detection.status"
LIFECYCLE_STREAM = "detection_lifecycle"
VENDOR = "centralops"

#: status da Detection → (activity_id, status_id) do Detection Finding.
TRANSITIONS: Mapping[str, tuple[int, int]] = {
    "ack": (2, 2),
    "closed": (3, 4),
    "open": (2, 1),
}


def build_status_event(
    detection: models.Detection,
    *,
    previous_status: Optional[str],
    actor_user_id: Optional[int],
    integration: Optional[Any],
    organization: Optional[Any],
    now_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """Envelope do 2004 de transição. Puro: recebe as linhas, não abre sessão."""
    from .normalize import OCSF_VERSION
    from .normalize.envelope import EnvelopeContext, build_envelope
    from .normalize.ocsf.classes import (
        CATEGORY_UID_FINDINGS,
        CLASS_UID_DETECTION_FINDING,
        SEVERITY_ID,
        is_valid_severity_id,
    )

    status = str(detection.status or "open")
    activity_id, status_id = TRANSITIONS.get(status, TRANSITIONS["open"])
    ts = int(datetime.utcnow().timestamp() * 1000) if now_ms is None else int(now_ms)
    severity_id = (
        int(detection.severity_id)
        if is_valid_severity_id(detection.severity_id)
        else SEVERITY_ID["high"]
    )
    title = detection.rule_name or f"Detection {detection.id}"

    ctx = EnvelopeContext(
        vendor=VENDOR,
        integration_id=getattr(detection, "integration_id", None),
        customer_id=detection.organization_id,
        customer_name=getattr(organization, "name", None),
        organization_slug=getattr(organization, "slug", None),
        stream=LIFECYCLE_STREAM,
        event_type=LIFECYCLE_EVENT_TYPE,
        mapping_version_id=None,
        platform=getattr(integration, "platform", None) or VENDOR,
        organization_id=detection.organization_id,
        data_geography=getattr(integration, "data_geography", None),
    )

    message = f"Detection '{title}' mudou de {previous_status or '?'} para {status}"
    normalized: Dict[str, Any] = {
        "class_uid": CLASS_UID_DETECTION_FINDING,
        "category_uid": CATEGORY_UID_FINDINGS,
        "activity_id": activity_id,
        "type_uid": CLASS_UID_DETECTION_FINDING * 100 + activity_id,
        "time": ts,
        "status_id": status_id,
        "severity_id": severity_id,
        "count": int(detection.count or 1),
        "metadata": {
            "version": OCSF_VERSION,
            "product": {"name": "CentralOps", "vendor_name": "CentralOps"},
            "logged_time": ts,
        },
        "finding_info": {
            # A identidade do achado original: é o que o consumidor usa para
            # encontrar o caso que abriu no Create.
            "uid": detection.dedup_key,
            "title": title,
            "desc": message,
            "modified_time": ts,
            "types": [detection.source or "detection"],
        },
        "message": message,
        "unmapped": {
            # Marca de auto-identificação para o guard de laço da detecção em
            # voo (``_is_self_emitted``): este evento nunca vira Detection.
            "centralops_detection": True,
            "detection_id": detection.id,
            "dedup_key": detection.dedup_key,
            "source": detection.source,
            "rule_id": detection.rule_id,
            "rule_name": detection.rule_name,
            "status": status,
            "previous_status": previous_status,
            "triaged_by_user_id": actor_user_id,
            "organization_id": detection.organization_id,
            "integration_id": detection.integration_id,
            # O evento que anunciou o achado (Create), quando a Detection o
            # guardou: é a outra ponta da trilha para quem correlaciona por
            # event_id em vez de uid.
            "ocsf_ref": detection.ocsf_ref,
            "search_result_id": detection.search_result_id,
            "first_seen_time": _to_ms(detection.first_seen),
            "last_seen_time": _to_ms(detection.last_seen),
        },
    }
    return build_envelope(
        {}, normalized, ctx, vendor_msg_id=f"detection-{detection.id}-{status}-{ts}"
    )


def _to_ms(value: Optional[datetime]) -> Optional[int]:
    return int(value.timestamp() * 1000) if isinstance(value, datetime) else None


def emit_status_event(
    db: Session,
    detection: models.Detection,
    *,
    previous_status: Optional[str],
    actor_user_id: Optional[int],
) -> Optional[Dict[str, Any]]:
    """Monta e despacha o evento da transição. Devolve o envelope, ou ``None``
    quando a flag está OFF ou o despacho falhou (logado, nunca propagado)."""
    if not settings.DETECTION_LIFECYCLE_EVENTS:
        return None
    try:
        organization = (
            db.get(models.Organization, detection.organization_id)
            if detection.organization_id is not None
            else None
        )
        integration = (
            db.get(models.Integration, detection.integration_id)
            if detection.integration_id is not None
            else None
        )
        envelope = build_status_event(
            detection,
            previous_status=previous_status,
            actor_user_id=actor_user_id,
            integration=integration,
            organization=organization,
        )
        from .pipeline import _enqueue_dispatch

        _enqueue_dispatch([envelope])
        return envelope
    except Exception:  # noqa: BLE001 — o fio nunca desfaz a triagem
        logger.exception(
            "detections: falha ao emitir evento de transição da Detection %s", detection.id
        )
        return None
