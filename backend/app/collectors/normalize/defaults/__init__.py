"""Mappings default seedados na primeira subida.

Cada arquivo ``<vendor>_<event_kind>.json`` é uma lista de regras DSL
pronta para alimentar :class:`MappingVersion.rules`. Os mappings são
**conservadores** — só marcam ``required`` os campos OCSF críticos
(class_uid, category_uid, severity_id, finding_info.uid, time).
Campos ausentes no payload do vendor caem em ``default: null``, sem
quarentena.

Esses defaults são uma **base mínima** para que o pipeline funcione
end-to-end. Engenheiros podem refiná-los via UI de mapping editor;
as melhorias geram MappingVersion v2, v3, ... no banco — os arquivos
aqui ficam como referência histórica.

A função :func:`load_default_rules` é usada pela seed em
``database._run_lightweight_migrations``.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any, List


# Mapeamento (vendor, event_type) → arquivo no recurso.
DEFAULT_MAPPING_FILES = {
    ("sophos", "sophos.alert"): "sophos_alert.json",
    ("sophos", "sophos.case"): "sophos_case.json",
    ("sophos", "sophos.detection"): "sophos_detection.json",
    ("microsoft_defender", "defender.alert"): "defender_alert.json",
    ("microsoft_defender", "defender.incident"): "defender_incident.json",
    ("ninjaone", "ninjaone.activity"): "ninjaone_activity.json",
    # Wazuh como FONTE — detecções do Indexer → OCSF Detection Finding.
    ("wazuh", "wazuh.detection"): "wazuh_detection.json",
    # CrowdStrike Falcon — Alerts API v2 → OCSF Detection Finding.
    ("crowdstrike", "crowdstrike.detection"): "crowdstrike_detection.json",
    # Microsoft Entra ID — sign-in + directory audit (Graph).
    ("entra_id", "entra_id.signin"): "entra_signin.json",
    ("entra_id", "entra_id.audit"): "entra_audit.json",
    # Okta System Log → OCSF Authentication.
    ("okta", "okta.system_log"): "okta_system_log.json",
    # AWS CloudTrail → OCSF API Activity.
    ("aws_cloudtrail", "aws_cloudtrail.event"): "aws_cloudtrail_event.json",
    # push/ingest: FortiGate syslog → OCSF Network Activity.
    ("fortinet_fortigate", "fortinet_fortigate.traffic"): "fortinet_fortigate_traffic.json",
    # push/ingest: Windows Event Log/WEC (Security) → OCSF Authentication.
    ("windows_event_log", "windows_event_log.security"): "windows_event_log_security.json",
}


def load_default_rules(vendor: str, event_type: str) -> List[dict[str, Any]]:
    """Carrega regras default para (vendor, event_type).

    Levanta ``FileNotFoundError`` se o par não tiver arquivo registrado
    em :data:`DEFAULT_MAPPING_FILES`.
    """
    filename = DEFAULT_MAPPING_FILES.get((vendor, event_type))
    if filename is None:
        raise FileNotFoundError(
            f"sem mapping default para {vendor!r}/{event_type!r}"
        )
    text = resources.files(__name__).joinpath(filename).read_text(encoding="utf-8")
    return json.loads(text)
