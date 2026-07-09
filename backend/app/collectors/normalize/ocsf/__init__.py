"""OCSF v1.3.0 — Open Cybersecurity Schema Framework.

Subset usado pelo CentralOps. Cobre as classes mapeadas pelos streams
atuais (Detection Finding, Incident Finding, API Activity) com seus
``class_uid``, ``category_uid`` e enums (severity, status, activity).

OCSF foi escolhido como schema canônico para garantir
interoperabilidade com Splunk/Elastic/Snowflake
no futuro. Os mappings por (vendor, event_type) emitem objetos que
satisfazem a forma mínima da classe-alvo.

Para adicionar suporte a uma classe nova: estender ``classes.py`` com a
constante e qualquer enum específico, atualizar
``ALLOWED_CLASS_UIDS``.
"""

from __future__ import annotations

from .classes import (
    ALLOWED_CLASS_UIDS,
    ACTIVITY_ID_DETECTION_FINDING,
    ACTIVITY_ID_INCIDENT_FINDING,
    CATEGORY_UID_APPLICATION_ACTIVITY,
    CATEGORY_UID_FINDINGS,
    CATEGORY_UID_IAM,
    CATEGORY_UID_NETWORK_ACTIVITY,
    CLASS_UID_ACCOUNT_CHANGE,
    CLASS_UID_API_ACTIVITY,
    CLASS_UID_AUTHENTICATION,
    CLASS_UID_DETECTION_FINDING,
    CLASS_UID_INCIDENT_FINDING,
    CLASS_UID_NETWORK_ACTIVITY,
    SEVERITY_ID,
    STATUS_ID,
    class_name_for,
    is_valid_class_uid,
    is_valid_severity_id,
)
from .validator import (  # noqa: E402
    DEFAULT_OCSF_VERSION,
    OCSF_REASONS,
    OcsfClassSpec,
    OcsfValidationResult,
    OcsfValidatorRegistry,
    derive_type_uid,
    get_registry,
    structural_gate,
    validate_normalized,
)

__all__ = [
    "ALLOWED_CLASS_UIDS",
    "ACTIVITY_ID_DETECTION_FINDING",
    "ACTIVITY_ID_INCIDENT_FINDING",
    "CATEGORY_UID_APPLICATION_ACTIVITY",
    "CATEGORY_UID_FINDINGS",
    "CATEGORY_UID_IAM",
    "CATEGORY_UID_NETWORK_ACTIVITY",
    "CLASS_UID_ACCOUNT_CHANGE",
    "CLASS_UID_API_ACTIVITY",
    "CLASS_UID_AUTHENTICATION",
    "CLASS_UID_DETECTION_FINDING",
    "CLASS_UID_INCIDENT_FINDING",
    "CLASS_UID_NETWORK_ACTIVITY",
    "SEVERITY_ID",
    "STATUS_ID",
    "class_name_for",
    "is_valid_class_uid",
    "is_valid_severity_id",
    # structural validator
    "DEFAULT_OCSF_VERSION",
    "OCSF_REASONS",
    "OcsfClassSpec",
    "OcsfValidationResult",
    "OcsfValidatorRegistry",
    "derive_type_uid",
    "get_registry",
    "structural_gate",
    "validate_normalized",
]
