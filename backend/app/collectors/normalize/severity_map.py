"""OCSF severity_id → syslog severity (RFC 5424 §6.2.1) mapping.

Calcula o PRI (priority) do syslog a partir do severity_id normalizado do
envelope canônico. Permite que decoders/regras downstream filtrem por
PRI sem inspecionar o JSON do MSG.

PRI = facility * 8 + severity.

OCSF severity_id | nome           | syslog severity | PRI (local0=16)
-----------------|----------------|-----------------|----------------
0                | unknown        | 6 (info)        | 134
1                | informational  | 6 (info)        | 134
2                | low            | 5 (notice)      | 133
3                | medium         | 4 (warning)     | 132
4                | high           | 3 (err)         | 131
5                | critical       | 2 (crit)        | 130
6                | fatal          | 0 (emerg)       | 128
99               | other          | 6 (info)        | 134
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

# facility=local0 — alinhado com legacy.
SYSLOG_FACILITY_LOCAL0 = 16

# Default conservador quando severity é ausente/desconhecida.
PRI_DEFAULT = SYSLOG_FACILITY_LOCAL0 * 8 + 6  # 134

OCSF_TO_SYSLOG_SEVERITY: Mapping[int, int] = {
    0: 6,   # unknown       → info
    1: 6,   # informational → info
    2: 5,   # low           → notice
    3: 4,   # medium        → warning
    4: 3,   # high          → err
    5: 2,   # critical      → crit
    6: 0,   # fatal         → emerg
    99: 6,  # other         → info
}


def pri_for_severity_id(severity_id: Optional[int]) -> int:
    """Calcula PRI syslog a partir do OCSF severity_id."""
    if severity_id is None:
        return PRI_DEFAULT
    try:
        sev = OCSF_TO_SYSLOG_SEVERITY.get(int(severity_id))
    except (TypeError, ValueError):
        return PRI_DEFAULT
    if sev is None:
        return PRI_DEFAULT
    return SYSLOG_FACILITY_LOCAL0 * 8 + sev


def pri_for_event(event: Mapping[str, Any]) -> int:
    """Extrai severity_id do envelope canônico e calcula PRI."""
    if not isinstance(event, Mapping):
        return PRI_DEFAULT
    normalized = event.get("normalized")
    if isinstance(normalized, Mapping):
        sid = normalized.get("severity_id")
        if sid is not None:
            return pri_for_severity_id(sid)
    return PRI_DEFAULT
