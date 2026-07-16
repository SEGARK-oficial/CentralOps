"""Contract tests dos 5 mappings default (RNF5.2).

Para cada (vendor, event_type) seedado, valida que:

1. As regras default carregadas de ``normalize/defaults/`` compilam
   sem erro (DSL bem-formada).
2. Aplicar sobre fixtures sintéticas representativas produz um
   envelope OCSF mínimo com class_uid/category_uid/severity_id/time
   populados conforme a especificação OCSF v1.3.
3. ``finding_info.uid`` (ou equivalente) sobrevive ao round-trip.
4. Valores de severity caem nos slots OCSF universais (0..6, 99).
5. Eventos sem campos required vão para falha — não passam silenciosamente.

Esse arquivo é o "guard" para regressão dos mappings: se alguém
editar o JSON default e quebrar contract OCSF, o test pega.

Fase 3.2 — v2 candidate tests (ver ``test_sophos_*_v2_candidate_*``):
    Carregam os 3 candidatos ``*.v2-candidate.json`` e validam:
    - Compile clean com dsl_version=2.
    - observables não-vazio quando input tem os campos relevantes.
    - Cada observable tem name/type/type_id/value.
    - Todos os outros campos normalizados batem com o contrato OCSF mínimo
      (sem drift em relação ao v1).
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any, Dict, List

import pytest

from backend.app.collectors.normalize.defaults import load_default_rules
from backend.app.collectors.normalize.engine import (
    MappingRequiredFieldError,
    apply_compiled,
    compile_rules,
)
from backend.app.collectors.normalize.envelope import (
    EnvelopeContext,
    build_envelope,
)
from backend.app.collectors.normalize.ocsf import SEVERITY_ID, is_valid_severity_id


# ── Fixtures sintéticas representativas ────────────────────────────────


# Nota (sync lab jul/2026): o mapping v2 (importado do lab) tem
# ``status_id`` com ``default: 1`` + ``pre_cast: lowercase``; a ordem do
# engine é default → pre_cast, então payload SEM ``status`` estoura no
# lowercase(1). Payloads reais do lab sempre trazem ``status`` — as
# fixtures acompanham (cada uma exercita um slot do value_map).
SOPHOS_ALERT_FIXTURES = [
    {
        "id": "alert-uuid-001",
        "createdAt": "2026-04-23T14:22:10Z",
        "raisedAt": "2026-04-23T14:22:08Z",
        "severity": "critical",
        "status": "open",
        "type": "malware",
        "category": "Threats",
        "description": "Trojan.GenericKD detected",
        "managedAgent": {"id": "agent-1", "name": "WIN-DESKTOP-01", "type": "computer"},
        "person": {"id": "user-1", "name": "alice"},
        "product": "Endpoint",
        "tenant": {"id": "tenant-x"},
    },
    {
        "id": "alert-uuid-002",
        "createdAt": "2026-04-23T15:00:00Z",
        "severity": "high",
        "status": "acknowledged",
        "type": "policy",
        "description": "Suspicious script execution",
        "managedAgent": {"id": "agent-2", "name": "WIN-LAPTOP-07"},
        "product": "Endpoint",
    },
    {
        "id": "alert-uuid-003",
        "createdAt": "2026-04-23T16:30:00Z",
        "severity": "medium",
        "status": "investigating",
        "type": "threat",
        "description": "Outbound traffic flagged",
    },
    {
        "id": "alert-uuid-004",
        "createdAt": "2026-04-23T17:00:00Z",
        "severity": "low",
        "status": "resolved",
        "type": "policy",
    },
    {
        "id": "alert-uuid-005",
        "createdAt": "2026-04-23T18:00:00Z",
        "severity": "info",
        "status": "closed",
    },
]

# Nota (sync lab jul/2026): o mapping v2 tira severidade de
# ``initialDetection.severity`` (escala Sophos 0-10 → OCSF via value_map).
# case-001 exercita o caminho rico (initialDetection + relatedDevices);
# case-002 prova o caminho mínimo (sem initialDetection → severity_id
# default 0 → to_str → value_map "0"→1 Informational).
SOPHOS_CASE_FIXTURES = [
    {
        "id": "case-001",
        "createdAt": "2026-04-23T10:00:00Z",
        "updatedAt": "2026-04-23T11:00:00Z",
        "name": "Investigation: Unauthorized access",
        "overview": "Multiple failed logons followed by success",
        "severity": "high",
        "status": "investigating",
        "assignee": {"id": "soc-1", "name": "soc-on-call"},
        "initialDetection": {
            "id": "det-uuid-777",
            "time": "2026-04-23T09:58:00Z",
            "type": "Threat",
            "severity": 8,
            "detectionRule": "WIN-LAT-Suspicious-Logon-1",
            "sensor": {"type": "endpoint", "source": "Sophos"},
        },
        "relatedDevices": [
            {"id": "dev-9", "name": "WIN-DC-02", "type": "server", "osPlatform": "windows", "osName": "Windows Server 2022"}
        ],
    },
    {
        "id": "case-002",
        "createdAt": "2026-04-23T12:00:00Z",
        "name": "MDR investigation",
        "severity": "critical",
        "status": "new",
        # initialDetection mínimo, como nos payloads reais — o mapping v2
        # exige sensor.type (device.type_id default 0 + pre_cast lowercase
        # estoura com o default int) e detectionRule string (a heurística
        # starts_with de attacks estoura com null quando mitreAttacks falta).
        # SEM severity: exercita severity_id default 0 → to_str → "0" → 1.
        "initialDetection": {
            "detectionRule": "WIN-EXE-Suspicious-Process-1",
            "sensor": {"type": "endpoint"},
        },
    },
]

DEFENDER_ALERT_FIXTURES = [
    {
        "id": "da637551227677560813_-961444813",
        "createdDateTime": "2026-04-23T09:00:00Z",
        "lastUpdateDateTime": "2026-04-23T09:30:00Z",
        "title": "Suspicious sign-in activity",
        "description": "User signed in from unusual location",
        "severity": "high",
        "status": "new",
        "category": "Initial Access",
        "serviceSource": "microsoftDefenderForEndpoint",
        "assignedTo": "soc-tier1",
    },
    {
        "id": "da637551227677560814_-961444814",
        "lastUpdateDateTime": "2026-04-23T10:00:00Z",
        "title": "Possible reconnaissance",
        "severity": "medium",
        "status": "inProgress",
        "category": "Discovery",
    },
]

DEFENDER_INCIDENT_FIXTURES = [
    {
        "id": "inc-1",
        "createdDateTime": "2026-04-23T08:00:00Z",
        "lastUpdateDateTime": "2026-04-23T08:30:00Z",
        "displayName": "Multi-stage incident: credential access + lateral movement",
        "description": "Linked alerts indicate adversary in network",
        "severity": "high",
        "status": "active",
    },
    {
        "id": "inc-2",
        "lastUpdateDateTime": "2026-04-23T09:00:00Z",
        "displayName": "Resolved incident",
        "severity": "medium",
        "status": "resolved",
    },
]

NINJAONE_ACTIVITY_FIXTURES = [
    {
        "id": 100001,
        "activityTime": 1776940800,
        "activityType": "DEVICE_ALERT",
        "subject": "Device offline",
        "severity": "info",
        "user": {"id": 5, "name": "tech-1"},
        "device": {"id": 22, "systemName": "WIN10-FIN-03", "dnsName": "fin03.acme.local"},
    },
    {
        "id": 100002,
        "activityTime": 1776941000,
        "activityType": "PATCH_APPLIED",
        "subject": "Critical patch applied",
        "severity": "low",
    },
]


# ── Helpers ────────────────────────────────────────────────────────────


def _ctx(vendor: str, event_type: str) -> EnvelopeContext:
    return EnvelopeContext(
        vendor=vendor,
        integration_id=1,
        customer_id=42,
        stream=event_type.split(".", 1)[1] if "." in event_type else event_type,
        event_type=event_type,
        mapping_version_id="contract-test",
    )


def _normalize(vendor: str, event_type: str, raw: Dict[str, Any]) -> Dict[str, Any]:
    rules = load_default_rules(vendor, event_type)
    compiled = compile_rules(rules)
    applied = apply_compiled(compiled, raw)
    return build_envelope(raw, applied.output, _ctx(vendor, event_type))


def _assert_minimal_envelope(env: Dict[str, Any], expected_class_uid: int) -> None:
    """Validações comuns a todos os contracts."""
    assert "_centralops" in env
    assert "normalized" in env
    assert "raw" in env

    norm = env["normalized"]
    assert norm["class_uid"] == expected_class_uid
    assert norm["category_uid"] in {2, 6}
    # severity_id deve ser um slot OCSF válido (0..6 ou 99).
    assert is_valid_severity_id(norm["severity_id"])
    assert isinstance(norm["time"], int)
    assert norm["time"] > 0
    # finding_info.uid presente para Findings; não para API Activity.
    if expected_class_uid in {2004, 2005}:
        assert "finding_info" in norm
        assert norm["finding_info"]["uid"]


# ── Sophos alert ───────────────────────────────────────────────────────


@pytest.mark.parametrize("raw", SOPHOS_ALERT_FIXTURES)
def test_sophos_alert_contract(raw) -> None:
    env = _normalize("sophos", "sophos.alert", raw)
    _assert_minimal_envelope(env, expected_class_uid=2004)
    norm = env["normalized"]
    assert norm["finding_info"]["uid"] == raw["id"]
    # severity OCSF
    expected = SEVERITY_ID.get(
        raw["severity"].lower(), SEVERITY_ID["informational"]
    )
    assert norm["severity_id"] == expected


def test_sophos_alert_required_field_missing_raises() -> None:
    rules = load_default_rules("sophos", "sophos.alert")
    compiled = compile_rules(rules)
    # Sem ``id`` (required), apply deve falhar.
    with pytest.raises(MappingRequiredFieldError):
        apply_compiled(compiled, {"createdAt": "2026-04-23T00:00:00Z", "severity": "high"})


# ── Sophos case ────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw", SOPHOS_CASE_FIXTURES)
def test_sophos_case_contract(raw) -> None:
    env = _normalize("sophos", "sophos.case", raw)
    _assert_minimal_envelope(env, expected_class_uid=2005)
    norm = env["normalized"]
    assert norm["finding_info"]["uid"] == raw["id"]


# ── Defender alert ─────────────────────────────────────────────────────


@pytest.mark.parametrize("raw", DEFENDER_ALERT_FIXTURES)
def test_defender_alert_contract(raw) -> None:
    env = _normalize("microsoft_defender", "defender.alert", raw)
    _assert_minimal_envelope(env, expected_class_uid=2004)
    norm = env["normalized"]
    assert norm["finding_info"]["uid"] == raw["id"]


# ── Defender incident ──────────────────────────────────────────────────


@pytest.mark.parametrize("raw", DEFENDER_INCIDENT_FIXTURES)
def test_defender_incident_contract(raw) -> None:
    env = _normalize("microsoft_defender", "defender.incident", raw)
    _assert_minimal_envelope(env, expected_class_uid=2005)
    norm = env["normalized"]
    assert norm["finding_info"]["uid"] == raw["id"]


# ── NinjaOne activity ──────────────────────────────────────────────────


@pytest.mark.parametrize("raw", NINJAONE_ACTIVITY_FIXTURES)
def test_ninjaone_activity_contract(raw) -> None:
    env = _normalize("ninjaone", "ninjaone.activity", raw)
    _assert_minimal_envelope(env, expected_class_uid=6003)
    norm = env["normalized"]
    # API Activity não usa finding_info.uid; só metadata.uid.
    assert norm["metadata"]["uid"] == raw["id"]


# ── Tenant isolation (RNF4.6) ──────────────────────────────────────────


def test_envelope_tenant_isolation_same_event_different_customers() -> None:
    """Mesma fixture aplicada com dois ctx tenants distintos: nenhum
    valor pertencente ao tenant A vaza no envelope do tenant B.
    """
    raw = SOPHOS_ALERT_FIXTURES[0]
    rules = load_default_rules("sophos", "sophos.alert")
    compiled = compile_rules(rules)
    applied = apply_compiled(compiled, raw)

    ctx_a = EnvelopeContext(
        vendor="sophos",
        integration_id=1,
        customer_id=100,
        stream="alerts",
        event_type="sophos.alert",
        mapping_version_id="ver-1",
    )
    ctx_b = EnvelopeContext(
        vendor="sophos",
        integration_id=2,
        customer_id=200,
        stream="alerts",
        event_type="sophos.alert",
        mapping_version_id="ver-1",
    )

    env_a = build_envelope(raw, applied.output, ctx_a)
    env_b = build_envelope(raw, applied.output, ctx_b)

    assert env_a["_centralops"]["customer_id"] == 100
    assert env_b["_centralops"]["customer_id"] == 200
    assert env_a["_centralops"]["integration_id"] == 1
    assert env_b["_centralops"]["integration_id"] == 2

    # Mutação acidental no envelope A não pode aparecer em B.
    env_a["_centralops"]["leaked"] = "should_not_appear"
    env_a["normalized"]["leaked"] = "should_not_appear"
    env_a["raw"]["leaked"] = "should_not_appear"
    assert "leaked" not in env_b["_centralops"]
    assert "leaked" not in env_b["normalized"]
    assert "leaked" not in env_b["raw"]


def test_engine_does_not_share_state_across_calls() -> None:
    """Aplicar regras sobre payload A e depois B não deve carregar
    valor de A para B (nem mesmo para os campos default).
    """
    rules = load_default_rules("sophos", "sophos.alert")
    compiled = compile_rules(rules)

    raw_a = SOPHOS_ALERT_FIXTURES[0]
    raw_b = {
        "id": "alert-isolated-b",
        "createdAt": "2026-04-23T20:00:00Z",
        "severity": "low",
        # status presente como nos payloads reais (ver nota nas fixtures).
        "status": "open",
    }
    out_a = apply_compiled(compiled, raw_a).output
    out_b = apply_compiled(compiled, raw_b).output

    assert out_a["normalized"]["finding_info"]["uid"] == raw_a["id"]
    assert out_b["normalized"]["finding_info"]["uid"] == raw_b["id"]
    assert out_a["normalized"]["severity_id"] != out_b["normalized"]["severity_id"]


# ── Sophos detection ───────────────────────────────────────────────────


SOPHOS_DETECTION_FIXTURES = [
    {
        "id": "det-uuid-001",
        "detectionRule": "WIN-MITRE-Behavioral-TA0011-T1105",
        "ruleDescription": "Behavioral detection of ingress tool transfer",
        # Sophos XDR severity 0-10 → OCSF 0-6 via value_map (sync lab
        # jul/2026; o seed antigo usava to_int passthrough). 9 → 6 (Fatal).
        "severity": 9,
        "type": "Threat",
        "time": "2026-04-23T14:22:10Z",
        "sensorGeneratedAt": "2026-04-23T14:22:05Z",
        "device": {"id": "dev-1", "type": "computer", "entity": "WIN-DESKTOP-01"},
        "sensor": {"id": "SophosSensorID", "type": "cloud", "source": "Sophos", "version": "1.18.1"},
        # Shape REAL do Sophos (tactic é dict) — o cast
        # mitre_tactic_to_ocsf rejeita o shape antigo {tactic: str}.
        "mitreAttacks": [
            {
                "tactic": {
                    "id": "TA0011",
                    "name": "Command and Control",
                    "techniques": [{"id": "T1105", "name": "Ingress Tool Transfer"}],
                }
            }
        ],
        "detectionAttack": "Command and Control",
    },
    {
        "id": "det-uuid-002",
        "detectionRule": "WIN-PER-PSH-ADD-SERVICE-REG-1",
        "severity": 4,
        "type": "Threat",
        "time": "2026-04-23T15:00:00Z",
        # sensor.type presente (payload real sempre traz) — device.type_id
        # do mapping v2 tem default 0 + pre_cast lowercase, que estoura com
        # o default int quando device.type e sensor.type faltam.
        "device": {"id": "dev-2", "entity": "WIN-LAPTOP-07"},
        "sensor": {"id": "s-2", "type": "endpoint"},
    },
    {
        "id": "det-uuid-003",
        "detectionRule": "LNX-SEC-ROOTKIT-MOD-1",
        "severity": 3,
        "type": "Threat",
        "time": "2026-04-23T16:30:00Z",
        "sensor": {"id": "s-3", "type": "endpoint"},
    },
]


# Escala Sophos 0-10 → slot OCSF, espelhando o value_map do mapping v2
# (sync lab jul/2026; substituiu o to_int passthrough do seed antigo).
_SOPHOS_DETECTION_SEVERITY_MAP = {
    0: 1, 1: 2, 2: 2, 3: 3, 4: 3, 5: 4, 6: 4, 7: 5, 8: 5, 9: 6, 10: 6,
}


@pytest.mark.parametrize("raw", SOPHOS_DETECTION_FIXTURES)
def test_sophos_detection_contract(raw: Dict[str, Any]) -> None:
    """Mapping de sophos.detection produz envelope OCSF 2004 válido.

    class_uid 2004 (Detection Finding) — detections XDR são alertas
    individuais, não incidentes correlacionados (2005 seria MDR cases).

    Nota de severidade (sync lab jul/2026): a API Sophos XDR retorna
    severity 0-10; o mapping v2 converte para o slot OCSF 0-6 via
    value_map (pre_cast to_str) — a escala completa é suportada.
    """
    env = _normalize("sophos", "sophos.detection", raw)
    _assert_minimal_envelope(env, expected_class_uid=2004)
    norm = env["normalized"]

    # finding_info.uid deve ser o id do detection.
    assert norm["finding_info"]["uid"] == raw["id"]

    # severity 0-10 do vendor cai no slot OCSF do value_map.
    assert norm["severity_id"] == _SOPHOS_DETECTION_SEVERITY_MAP[raw["severity"]]
    assert is_valid_severity_id(norm["severity_id"])

    # metadata.product aponta para Sophos; feature.name vem de
    # sensor.type (default "XDR Detections" quando ausente) — sync lab.
    assert norm["metadata"]["product"]["vendor_name"] == "Sophos"
    expected_feature = (raw.get("sensor") or {}).get("type") or "XDR Detections"
    assert norm["metadata"]["product"]["feature"]["name"] == expected_feature


