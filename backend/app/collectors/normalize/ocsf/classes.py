"""Constantes OCSF v1.3.0 — class_uids, category_uids e enums.

Referência: https://schema.ocsf.io/1.3.0
"""

from __future__ import annotations

from typing import Mapping


# ── Categories ────────────────────────────────────────────────────────
# https://schema.ocsf.io/1.3.0/categories
CATEGORY_UID_IAM = 3
CATEGORY_UID_NETWORK_ACTIVITY = 4
CATEGORY_UID_FINDINGS = 2
CATEGORY_UID_APPLICATION_ACTIVITY = 6


# ── Class UIDs ────────────────────────────────────────────────────────
# Cada class_uid = category_uid * 1000 + class_index. Manter em sincronia
# com o schema oficial.
#
# Este conjunto DEVE conter TODA class_uid que os
# mapeamentos default (``normalize/defaults/*.json``) emitem — senão uma futura
# validação OCSF fail-closed quarentenaria 100% desses streams. O guard
# ``test_ocsf_allowed_classes_cover_defaults`` falha o CI se um mapeamento novo
# emitir uma classe ausente aqui.

# Findings (category 2)
CLASS_UID_DETECTION_FINDING = 2004
CLASS_UID_INCIDENT_FINDING = 2005

# Identity & Access Management (category 3)
CLASS_UID_ACCOUNT_CHANGE = 3001
CLASS_UID_AUTHENTICATION = 3002

# Network Activity (category 4)
CLASS_UID_NETWORK_ACTIVITY = 4001

# Application Activity (category 6)
CLASS_UID_API_ACTIVITY = 6003


CLASS_NAMES: Mapping[int, str] = {
    CLASS_UID_DETECTION_FINDING: "Detection Finding",
    CLASS_UID_INCIDENT_FINDING: "Incident Finding",
    CLASS_UID_ACCOUNT_CHANGE: "Account Change",
    CLASS_UID_AUTHENTICATION: "Authentication",
    CLASS_UID_NETWORK_ACTIVITY: "Network Activity",
    CLASS_UID_API_ACTIVITY: "API Activity",
}

ALLOWED_CLASS_UIDS = frozenset(CLASS_NAMES.keys())


# ── Severity (universal — válido para qualquer classe OCSF) ───────────
# https://schema.ocsf.io/1.3.0/objects/severity_id
SEVERITY_ID: Mapping[str, int] = {
    "unknown": 0,
    "informational": 1,
    "low": 2,
    "medium": 3,
    "high": 4,
    "critical": 5,
    "fatal": 6,
    "other": 99,
}

_SEVERITY_VALUES = frozenset(SEVERITY_ID.values())


# ── Status (Findings — Detection/Incident) ────────────────────────────
# https://schema.ocsf.io/1.3.0/classes/detection_finding (campo status_id)
STATUS_ID: Mapping[str, int] = {
    "unknown": 0,
    "new": 1,
    "in_progress": 2,
    "suppressed": 3,
    "resolved": 4,
    "other": 99,
}


# ── Activity IDs ──────────────────────────────────────────────────────
# Detection Finding (2004) — activity_id semântica de ciclo de vida.
ACTIVITY_ID_DETECTION_FINDING: Mapping[str, int] = {
    "unknown": 0,
    "create": 1,
    "update": 2,
    "close": 3,
    "other": 99,
}

# Incident Finding (2005) — mesma semântica.
ACTIVITY_ID_INCIDENT_FINDING: Mapping[str, int] = {
    "unknown": 0,
    "create": 1,
    "update": 2,
    "close": 3,
    "other": 99,
}


# ── Helpers ───────────────────────────────────────────────────────────

def class_name_for(class_uid: int) -> str:
    """Devolve o nome da classe OCSF; ``""`` se desconhecida."""
    return CLASS_NAMES.get(class_uid, "")


def is_valid_class_uid(class_uid: int) -> bool:
    return class_uid in ALLOWED_CLASS_UIDS


def is_valid_severity_id(severity_id: int) -> bool:
    return severity_id in _SEVERITY_VALUES
