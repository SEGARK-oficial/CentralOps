"""Quarentena de eventos que falham normalização (RF2.6).

Eventos com erro de parse, mapping ou validação são persistidos em
``quarantine_events`` em vez de ir para o Wazuh. Retenção default 7
dias (``expires_at = now + 7d``) — a UI inspeciona,
reprocessa ou descarta.

Esse módulo expõe apenas o writer. O purge por retenção é uma task
periódica adicionada mais adiante (não há urgência: eventos
vivem até ``expires_at`` mesmo sem prune).

O writer NÃO levanta — falha em escrever quarentena loga e segue.
Bloquear o pipeline porque o DB caiu agravaria o incidente.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Mapping, Optional

from sqlalchemy.exc import SQLAlchemyError

from ..core.config import settings
from ..db import database, models

logger = logging.getLogger(__name__)


# Tipos de erro suportados — vide docstring de
# ``models.QuarantineEvent``.
ERROR_KIND_PARSE = "parse"
ERROR_KIND_MAP = "map"
ERROR_KIND_VALIDATE = "validate"
ERROR_KIND_MISSING_CUSTOMER_ID = "missing_customer_id"
ERROR_KIND_MISSING_MAPPING = "missing_mapping"

_DEFAULT_RETENTION_DAYS = 7

# Passes de redução (str_cap, array_cap) aplicados, em ordem, quando o payload
# excede o limite. Apertam progressivamente até caber. A redução preserva a
# ESTRUTURA e os escalares de topo (time, id, sensorGeneratedAt…) e mantém
# JSON VÁLIDO — diferente do antigo corte de string cru, que gerava JSON
# quebrado e impedia o reprocesso.
_REDUCTION_PASSES = ((8192, 200), (2048, 50), (512, 20), (256, 5))


def _reduce_structure(obj: Any, *, str_cap: int, array_cap: int) -> Any:
    """Reduz recursivamente: clipa strings longas e limita listas.

    Mantém dicts e escalares intactos; só encurta strings maiores que
    ``str_cap`` e listas maiores que ``array_cap``, anexando um marcador
    legível do que foi cortado. O resultado é sempre serializável.
    """
    if isinstance(obj, str):
        if len(obj) > str_cap:
            return obj[:str_cap] + f"…[+{len(obj) - str_cap} chars]"
        return obj
    if isinstance(obj, list):
        extra = len(obj) - array_cap if len(obj) > array_cap else 0
        items = obj[:array_cap] if extra else obj
        reduced = [
            _reduce_structure(i, str_cap=str_cap, array_cap=array_cap) for i in items
        ]
        if extra:
            reduced.append(f"…[+{extra} items truncados]")
        return reduced
    if isinstance(obj, dict):
        return {
            k: _reduce_structure(v, str_cap=str_cap, array_cap=array_cap)
            for k, v in obj.items()
        }
    return obj


def _serialize_raw(raw: Mapping[str, Any], *, max_bytes: Optional[int] = None) -> str:
    """Serializa o payload preservando integridade e mantendo JSON VÁLIDO.

    Acima de ``max_bytes`` (default ``settings.QUARANTINE_RAW_MAX_BYTES``), em
    vez de cortar a string serializada (que produzia JSON inválido e quebrava
    o reprocesso), reduz a ESTRUTURA: clipa strings longas e limita listas,
    apertando os caps até caber. Preserva os escalares de topo (timestamps,
    ids) para que a inspeção e o reprocesso parcial funcionem, e marca
    ``_truncated: true``. Este limite é de armazenamento — NÃO afeta o
    caminho de saída ao Wazuh.
    """
    limit = max_bytes if max_bytes is not None else settings.QUARANTINE_RAW_MAX_BYTES

    try:
        serialized = json.dumps(raw, default=str, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        # Nem JSON conseguimos. Persistimos a representação repr (clipada) para
        # o operador investigar — ainda como JSON válido.
        logger.warning("quarantine: payload não-serializável: %s", exc)
        return json.dumps(
            {"_truncated": True, "_repr": repr(raw)[:limit]},
            default=str,
            separators=(",", ":"),
        )

    if len(serialized) <= limit:
        return serialized

    # Acima do limite: redução estruturada (JSON válido), apertando os caps.
    for str_cap, array_cap in _REDUCTION_PASSES:
        reduced = _reduce_structure(raw, str_cap=str_cap, array_cap=array_cap)
        if isinstance(reduced, dict):
            reduced = {"_truncated": True, **reduced}
        else:
            reduced = {"_truncated": True, "_payload": reduced}
        candidate = json.dumps(reduced, default=str, separators=(",", ":"))
        if len(candidate) <= limit:
            return candidate

    # Último recurso: só os escalares de topo (sempre JSON válido e pequeno).
    if isinstance(raw, Mapping):
        scalars = {
            k: v
            for k, v in raw.items()
            if isinstance(v, (int, float, bool, type(None)))
            or (isinstance(v, str) and len(v) <= 512)
        }
        candidate = json.dumps(
            {"_truncated": True, "_reduced": "scalars_only", **scalars},
            default=str,
            separators=(",", ":"),
        )
        if len(candidate) <= limit:
            return candidate

    return json.dumps(
        {"_truncated": True, "_reduced": "oversized"}, separators=(",", ":")
    )


def send_to_quarantine(
    *,
    integration_id: Optional[int],
    vendor: str,
    event_type: Optional[str],
    raw: Mapping[str, Any],
    error_kind: str,
    error_detail: Optional[str] = None,
    mapping_version_id: Optional[str] = None,
    organization_id: Optional[int] = None,
    retention_days: int = _DEFAULT_RETENTION_DAYS,
) -> Optional[str]:
    """Persiste um evento em quarentena. Devolve o ID, ou ``None`` se falhou.

    Síncrono — o pipeline async chama isto via ``await asyncio.to_thread``
    para não bloquear o event loop em DBs lentos.

    ``organization_id``: tenant da quarentena, eixo de
    pruning/erase por tenant+tempo. Se não for informado, é resolvido a partir
    de ``integration_id`` (a integração filha carrega o org materializado). Pode
    ficar ``None`` quando nem a integração resolve uma org (ex.: erro
    ``missing_customer_id`` antes da resolução de tenant) — nullable por design.
    """
    now = datetime.utcnow()
    raw_payload = _serialize_raw(raw)
    error_detail_clipped = (error_detail or "")[:2000] or None

    try:
        with database.SessionLocal() as db:
            resolved_org_id = organization_id
            if resolved_org_id is None and integration_id is not None:
                _integ = db.get(models.Integration, integration_id)
                if _integ is not None:
                    resolved_org_id = _integ.organization_id
            event = models.QuarantineEvent(
                organization_id=resolved_org_id,
                integration_id=integration_id,
                vendor=vendor,
                event_type=event_type,
                raw_payload=raw_payload,
                error_kind=error_kind,
                error_detail=error_detail_clipped,
                mapping_version_id=mapping_version_id,
                expires_at=now + timedelta(days=retention_days),
            )
            db.add(event)
            db.commit()
            db.refresh(event)
            return event.id
    except SQLAlchemyError as exc:
        # Não propaga — failure aqui não pode parar a coleta. O evento
        # é perdido (por design: já estava com erro, é melhor que
        # bloquear o pipeline).
        logger.error(
            "quarantine: falha ao persistir vendor=%s event_type=%s kind=%s: %s",
            vendor, event_type, error_kind, exc,
        )
        return None
