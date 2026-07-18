"""Mapping default wazuh.detection → OCSF 2004 (Detection Finding).

Cobre o gap de produção "no current MappingVersion configured" (31k+ eventos
em quarentena): o arquivo default existia mas a tupla NÃO estava em
``seed_definitions`` (database.py), então a seed nunca criava a v1.

Valida com payloads REAIS da quarentena do lab (dois shapes dominantes):

- Windows EventChannel via agente (``data.win.eventdata/system``, compliance
  arrays pci_dss/hipaa/nist/gdpr/tsc no ``rule``);
- Syslog decodificado no manager (UniFi: ``predecoder`` + ``full_log`` +
  ``data.wireless``, agente 000 sem ``agent.ip``, sem compliance arrays).

Cada shape passa pelo engine REAL (``default_engine.apply``, DSL v2 como a
seed persiste) e pelo structural gate OCSF de verdade (valid=True, zero
missing required — manifest 1.8.0 exige activity_id, category_uid, class_uid,
finding_info, metadata, severity_id, time, type_uid).

Tabela de severidade (rule.level 0-15 → severity_id OCSF):
    0-3 → 1 Informational | 4-6 → 2 Low | 7-11 → 3 Medium
    12-14 → 4 High | 15+ → 5 Critical
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from ..normalize import engine as E
from ..normalize.defaults import DEFAULT_MAPPING_FILES, load_default_rules
from ..normalize.engine import MappingRequiredFieldError, default_engine
from ..normalize.ocsf import validator as V


def _seed_payload() -> dict:
    """Shape v2 EXATAMENTE como a seed persiste em ``mapping_versions.rules``."""
    dsl = load_default_rules("wazuh", "wazuh.detection")
    return {"preprocess": list(dsl.get("preprocess") or []), "rules": list(dsl["rules"])}


def _apply(raw: dict) -> dict:
    """Aplica o engine real (mesmo caminho do pipeline: DSL v2 + cache LRU)."""
    result = default_engine.apply(
        "test-wazuh-detection-v1", _seed_payload(), raw, dsl_version=2
    )
    return result.output["normalized"]


# ── payload real: Windows EventChannel (logoff 4634) via agente ─────────────

_WINDOWS_LOGOFF = {
    "agent": {"ip": "10.0.5.201", "name": "SRV-AD02", "id": "001"},
    "manager": {"name": "wazuh.manager"},
    "data": {
        "win": {
            "eventdata": {
                "targetUserName": "julia.strello",
                "targetDomainName": "ZAFFARINET",
                "logonType": "3",
            },
            "system": {
                "eventID": "4634",
                "channel": "Security",
                "systemTime": "2026-07-16T20:02:38Z",
                "computer": "SRV-AD02.zaffarinet.interno",
                "severityValue": "AUDIT_SUCCESS",
                "providerName": "Microsoft-Windows-Security-Auditing",
            },
        }
    },
    "rule": {
        "firedtimes": 7341,
        "level": 3,
        "description": "Windows User Logoff",
        "groups": ["windows", "windows_security"],
        "id": "60137",
        "pci_dss": ["10.2.5"],
        "hipaa": ["164.312.b"],
        "nist_800_53": ["AU.14", "AC.7"],
        "gdpr": ["IV_32.2"],
        "tsc": ["CC6.8", "CC7.2", "CC7.3"],
    },
    "decoder": {"name": "windows_eventchannel"},
    "input": {"type": "log"},
    "@timestamp": "2026-07-16T20:03:02.432Z",
    "location": "EventChannel",
    "id": "1784232182.1068439132",
    "timestamp": "2026-07-16T20:03:02.432+0000",
}

# ── payload real: syslog UniFi decodificado no manager (agente 000) ─────────

_UNIFI_SYSLOG = {
    "predecoder": {
        "hostname": "UNIFI13RK02L159-corredormatinai",
        "timestamp": "Jul 16 17:11:34",
    },
    "agent": {"name": "wazuh.manager", "id": "000"},
    "manager": {"name": "wazuh.manager"},
    "data": {
        "srcmac": "e2:87:c5:d8:e4:36",
        "wireless": {
            "vap": "wifi0ap3",
            "bssid": "82:83:c2:b7:e5:39",
            "satisfaction": "53",
            "radio": "wifi0",
        },
        "event": {"reason": "tcp_latency"},
    },
    "rule": {
        "firedtimes": 1681,
        "level": 5,
        "description": "UniFi client experiencing elevated TCP latency",
        "groups": ["unifi", "network", "latency", "tcp"],
        "id": "110143",
    },
    "decoder": {"name": "unifi-ap"},
    "full_log": (
        "Jul 16 17:11:34 UNIFI13RK02L159-corredormatinai mcad[4702]: "
        "wireless_agg_stats.log_sta_anomalies(): bssid=82:83:c2:b7:e5:39 "
        "sta=e2:87:c5:d8:e4:36 satisfaction_now=53 anomalies=tcp_latency"
    ),
    "input": {"type": "log"},
    "@timestamp": "2026-07-16T20:11:34.580Z",
    "location": "192.168.50.147",
    "id": "1784232694.1076923353",
    "timestamp": "2026-07-16T20:11:34.580+0000",
}


# ── carga + shape v2 + registro na seed ──────────────────────────────────────

def test_default_rules_load_and_compile_as_v2() -> None:
    assert ("wazuh", "wazuh.detection") in DEFAULT_MAPPING_FILES
    dsl = load_default_rules("wazuh", "wazuh.detection")
    assert isinstance(dsl, dict) and isinstance(dsl["rules"], list) and dsl["rules"]
    # auto-detect (dict → v2) e shape persistido pela seed compilam.
    assert E.detect_dsl_version(dsl) == 2
    compiled = E.compile_rules(_seed_payload(), dsl_version=2)
    assert compiled.rules


def test_seed_definitions_include_wazuh_detection() -> None:
    """Guarda de fonte: a tupla TEM que estar em ``seed_definitions`` senão a
    seed nunca cria a MappingVersion v1 e 100% do stream vai pra quarentena
    (missing_mapping) — exatamente o incidente do lab. Na imagem compilada
    (.so) não há fonte para inspecionar — skip (o sweep dev/CI sempre roda)."""
    src_path = Path(__file__).resolve().parents[2] / "db" / "database.py"
    if not src_path.exists():
        pytest.skip("database.py fonte ausente (imagem compilada) — nada a inspecionar")
    src = src_path.read_text("utf-8")
    assert '("wazuh", "wazuh.detection", 2004' in src, (
        "tupla wazuh.detection ausente de seed_definitions em database.py"
    )


# ── shape 1: Windows EventChannel via agente ─────────────────────────────────

def test_windows_eventchannel_maps_and_passes_structural_gate() -> None:
    norm = _apply(_WINDOWS_LOGOFF)

    gate = V.validate_normalized(norm)
    assert gate.valid, gate.reason
    assert gate.reason == V.REASON_OK
    assert gate.missing_required == (), gate.missing_required

    # identidade OCSF 2004
    assert norm["class_uid"] == 2004
    assert norm["category_uid"] == 2
    assert norm["activity_id"] == 1
    assert norm["type_uid"] == 200401
    assert norm["status_id"] == 1

    # time = epoch em MILISSEGUNDOS (timestamp_t do OCSF) do timestamp do alerta
    expected_epoch = int(
        round(datetime.fromisoformat("2026-07-16T20:03:02.432+00:00").timestamp() * 1000)
    )
    assert norm["time"] == expected_epoch
    assert len(str(norm["time"])) == 13  # ms, não segundos

    # severidade: rule.level 3 → 1 Informational
    assert norm["severity_id"] == 1
    assert norm["severity"] == "Informational"

    # finding_info + rule metadata (analytic)
    assert norm["finding_info"]["uid"] == "1784232182.1068439132"
    assert norm["finding_info"]["title"] == "Windows User Logoff"
    assert norm["finding_info"]["types"] == ["windows", "windows_security"]
    assert norm["finding_info"]["analytic"]["uid"] == "60137"
    assert norm["finding_info"]["analytic"]["type_id"] == 1

    # device ← agent
    assert norm["device"]["hostname"] == "SRV-AD02"
    assert norm["device"]["ip"] == "10.0.5.201"
    assert norm["device"]["uid"] == "001"

    assert norm["message"] == "Windows User Logoff"
    assert norm["metadata"]["product"]["name"] == "Wazuh"
    assert norm["metadata"]["event_code"] == "60137"

    # unmapped: compliance arrays + data.* variável preservados
    assert norm["unmapped"]["rule"]["pci_dss"] == ["10.2.5"]
    assert norm["unmapped"]["rule"]["hipaa"] == ["164.312.b"]
    assert norm["unmapped"]["rule"]["nist_800_53"] == ["AU.14", "AC.7"]
    assert norm["unmapped"]["rule"]["gdpr"] == ["IV_32.2"]
    assert norm["unmapped"]["rule"]["tsc"] == ["CC6.8", "CC7.2", "CC7.3"]
    assert norm["unmapped"]["rule"]["firedtimes"] == 7341
    assert norm["unmapped"]["data"]["win"]["system"]["eventID"] == "4634"
    assert norm["unmapped"]["decoder"] == "windows_eventchannel"

    # observables: hostname do agente presente
    obs = {(o["type_id"], o["value"]) for o in norm["observables"]}
    assert (1, "SRV-AD02") in obs


# ── shape 2: syslog UniFi via manager (não-windows) ──────────────────────────

def test_unifi_syslog_variant_maps_and_passes_structural_gate() -> None:
    norm = _apply(_UNIFI_SYSLOG)

    gate = V.validate_normalized(norm)
    assert gate.valid, gate.reason
    assert gate.missing_required == (), gate.missing_required

    # severidade: rule.level 5 → 2 Low
    assert norm["severity_id"] == 2
    assert norm["severity"] == "Low"

    assert norm["finding_info"]["uid"] == "1784232694.1076923353"
    assert norm["finding_info"]["analytic"]["uid"] == "110143"
    assert norm["finding_info"]["types"] == ["unifi", "network", "latency", "tcp"]

    # agente 000 (manager) não tem ip — default null, sem quarentena
    assert norm["device"]["hostname"] == "wazuh.manager"
    assert norm["device"]["ip"] is None

    # shape syslog preservado em unmapped
    assert norm["unmapped"]["predecoder"]["hostname"] == "UNIFI13RK02L159-corredormatinai"
    assert norm["unmapped"]["data"]["wireless"]["bssid"] == "82:83:c2:b7:e5:39"
    assert norm["unmapped"]["full_log"].startswith("Jul 16 17:11:34")
    # sem compliance arrays neste shape → null (nunca KeyError/quarentena)
    assert norm["unmapped"]["rule"]["pci_dss"] is None

    # observable de MAC do cliente
    obs = {(o["type_id"], o["value"]) for o in norm["observables"]}
    assert (3, "e2:87:c5:d8:e4:36") in obs


# ── tabela de severidade + guardas de required ───────────────────────────────

@pytest.mark.parametrize(
    "level,expected_id,expected_label",
    [
        (0, 1, "Informational"),
        (3, 1, "Informational"),
        (4, 2, "Low"),
        (6, 2, "Low"),
        (7, 3, "Medium"),
        (11, 3, "Medium"),
        (12, 4, "High"),
        (14, 4, "High"),
        (15, 5, "Critical"),
    ],
)
def test_severity_table_covers_wazuh_levels(
    level: int, expected_id: int, expected_label: str
) -> None:
    raw = {
        "id": f"sev-{level}",
        "timestamp": "2026-07-16T12:00:00.000+0000",
        "rule": {"level": level, "description": "x", "groups": []},
        "agent": {"id": "001", "name": "host-a"},
    }
    norm = _apply(raw)
    assert norm["severity_id"] == expected_id
    assert norm["severity"] == expected_label
    assert V.validate_normalized(norm).valid


def test_missing_rule_level_defaults_to_informational() -> None:
    raw = {
        "id": "sev-none",
        "timestamp": "2026-07-16T12:00:00.000+0000",
        "rule": {"description": "sem level", "groups": []},
        "agent": {"id": "001", "name": "host-a"},
    }
    norm = _apply(raw)
    assert norm["severity_id"] == 1
    assert V.validate_normalized(norm).valid


def test_missing_alert_id_quarantines_via_required() -> None:
    raw = {
        "timestamp": "2026-07-16T12:00:00.000+0000",
        "rule": {"level": 3, "description": "x", "groups": []},
        "agent": {"id": "001", "name": "host-a"},
    }
    with pytest.raises(MappingRequiredFieldError):
        _apply(raw)
