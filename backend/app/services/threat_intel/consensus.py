"""Motor de consenso: combina métricas de OTX/AbuseIPDB num threat_level final."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ...db import models


THREAT_CRITICAL = "CRITICAL"
THREAT_HIGH = "HIGH"
THREAT_LOW = "LOW"
THREAT_SAFE = "SAFE"

THREAT_LEVELS = (THREAT_CRITICAL, THREAT_HIGH, THREAT_LOW, THREAT_SAFE)


@dataclass
class ConsensusInput:
    otx_pulse_count: Optional[int] = None
    abuse_score: Optional[int] = None
    abuse_country: Optional[str] = None
    abuse_usage_type: Optional[str] = None
    otx_failed: bool = False
    abuse_failed: bool = False
    quota_exceeded: bool = False


def calculate_threat_level(
    data: ConsensusInput,
    config: models.ThreatIntelConfig,
) -> str:
    """Aplica os thresholds configurados para classificar a ameaça.

    Regras (em ordem de precedência):
    - ``abuse_score`` ≥ ``threat_score_critical`` ⇒ CRITICAL
    - ``otx_pulse_count`` ≥ ``otx_pulse_high`` e ``abuse_score`` ≥ ``threat_score_high`` ⇒ CRITICAL
    - ``abuse_score`` ≥ ``threat_score_high`` ⇒ HIGH
    - ``otx_pulse_count`` ≥ ``otx_pulse_high`` ⇒ HIGH
    - alguma evidência (qualquer pulse ou score > 0) ⇒ LOW
    - sem evidência ⇒ SAFE
    """
    abuse_score = data.abuse_score or 0
    pulse_count = data.otx_pulse_count or 0

    if abuse_score >= config.threat_score_critical:
        return THREAT_CRITICAL
    if (
        pulse_count >= config.otx_pulse_high
        and abuse_score >= config.threat_score_high
    ):
        return THREAT_CRITICAL
    if abuse_score >= config.threat_score_high:
        return THREAT_HIGH
    if pulse_count >= config.otx_pulse_high:
        return THREAT_HIGH
    if pulse_count > 0 or abuse_score > 0:
        return THREAT_LOW
    return THREAT_SAFE


__all__ = [
    "ConsensusInput",
    "calculate_threat_level",
    "THREAT_CRITICAL",
    "THREAT_HIGH",
    "THREAT_LOW",
    "THREAT_SAFE",
    "THREAT_LEVELS",
]
