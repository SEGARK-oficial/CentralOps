"""Benchmark do pipeline de normalização.

Mede latência média de ``compile + apply + build_envelope`` sobre os
5 mappings default. Não é teste de regressão estrita (CI não bloqueia
por número absoluto), mas grava baseline para comparação futura.

Alvo: p50 < 60s, p95 < 5min — esses são *end-to-end* (vendor
→ Wazuh), incluindo HTTP. A normalização local deve ficar bem abaixo
disso. Baseline esperado no laptop dev: < 200µs/evento.

O test só falha se a média ultrapassar um teto absurdo (50ms/event)
— protege contra regressão catastrófica (ex: alguém remove o cache).
"""

from __future__ import annotations

import time
from statistics import mean

import pytest

from backend.app.collectors.normalize.defaults import (
    DEFAULT_MAPPING_FILES,
    load_default_rules,
)
from backend.app.collectors.normalize.engine import (
    apply_compiled,
    compile_rules,
)
from backend.app.collectors.normalize.envelope import (
    EnvelopeContext,
    build_envelope,
)


# Fixtures sintéticas reaproveitadas do contract test — mantemos um
# pequeno conjunto representativo aqui para o benchmark ser self-contained.
_FIXTURES = {
    ("sophos", "sophos.alert"): {
        "id": "alert-uuid-001",
        "createdAt": "2026-04-23T14:22:10Z",
        "severity": "critical",
        "type": "malware",
        "description": "Trojan.GenericKD detected",
        "managedAgent": {"id": "a1", "name": "WIN-01", "type": "computer"},
        "person": {"id": "u1", "name": "alice"},
        "product": "Endpoint",
    },
    ("sophos", "sophos.case"): {
        "id": "case-001",
        "createdAt": "2026-04-23T10:00:00Z",
        "updatedAt": "2026-04-23T11:00:00Z",
        "name": "Investigation",
        "description": "Multiple failed logons",
        "severity": "high",
        "status": "investigating",
    },
    ("microsoft_defender", "defender.alert"): {
        "id": "da-001",
        "createdDateTime": "2026-04-23T09:00:00Z",
        "lastUpdateDateTime": "2026-04-23T09:30:00Z",
        "title": "Suspicious sign-in",
        "severity": "high",
        "status": "new",
        "category": "Initial Access",
    },
    ("microsoft_defender", "defender.incident"): {
        "id": "inc-1",
        "createdDateTime": "2026-04-23T08:00:00Z",
        "lastUpdateDateTime": "2026-04-23T08:30:00Z",
        "displayName": "Multi-stage incident",
        "severity": "high",
        "status": "active",
    },
    ("ninjaone", "ninjaone.activity"): {
        "id": 100001,
        "activityTime": 1776940800,
        "activityType": "DEVICE_ALERT",
        "subject": "Device offline",
        "severity": "info",
        "user": {"id": 5, "name": "tech-1"},
        "device": {"id": 22, "systemName": "WIN-FIN", "dnsName": "fin.local"},
    },
    ("sophos", "sophos.detection"): {
        "id": "det-bench-001",
        "detectionRule": "WIN-MITRE-Behavioral-TA0011-T1105",
        "severity": 5,
        "type": "Threat",
        "time": "2026-04-23T14:22:10Z",
        "device": {"id": "dev-1", "type": "computer", "entity": "WIN-DESKTOP-01"},
        "sensor": {"id": "SophosSensorID", "type": "cloud", "source": "Sophos", "version": "1.18.1"},
        "mitreAttacks": [{"tactic": "Command and Control", "technique": "T1105"}],
    },
    # Shapes idênticos aos que os collectors entregam (validados
    # nos testes de cada vendor: test_crowdstrike/test_entra_id/test_okta/
    # test_aws_cloudtrail/test_wazuh_detections).
    ("crowdstrike", "crowdstrike.detection"): {
        "composite_id": "cid-bench-1",
        "created_timestamp": "2026-06-21T14:22:10Z",
        "severity": 70,
        "severity_name": "High",
        "display_name": "SuspiciousActivity",
        "description": "masquerading behavior",
        "status": "new",
        "tactic": "Defense Evasion",
        "user_name": "jdoe",
        "device": {"device_id": "9f8a", "hostname": "WS-1", "local_ip": "10.0.0.9"},
    },
    ("entra_id", "entra_id.signin"): {
        "id": "s-bench-1",
        "createdDateTime": "2026-06-20T10:00:00Z",
        "userPrincipalName": "a@x.com",
        "ipAddress": "1.1.1.1",
        "status": {"errorCode": 0},
    },
    ("entra_id", "entra_id.audit"): {
        "id": "a-bench-1",
        "activityDateTime": "2026-06-20T10:01:19Z",
        "activityDisplayName": "Add member to group",
        "category": "GroupManagement",
        "result": "success",
        "initiatedBy": {"user": {"userPrincipalName": "bob@x.com", "id": "u-1", "ipAddress": "3.3.3.3"}},
        "targetResources": [{"displayName": "Group A", "userPrincipalName": "carol@x.com"}],
    },
    ("okta", "okta.system_log"): {
        "uuid": "okta-bench-1",
        "published": "2026-06-20T21:05:32.000Z",
        "eventType": "user.session.start",
        "severity": "INFO",
        "displayMessage": "User login to Okta",
        "outcome": {"result": "SUCCESS"},
        "actor": {"id": "00u", "type": "User", "alternateId": "admin@okta.com", "displayName": "Admin"},
        "client": {"ipAddress": "142.126.158.61"},
    },
    ("aws_cloudtrail", "aws_cloudtrail.event"): {
        "eventID": "ct-bench-1",
        "eventTime": "2026-06-21T10:00:00Z",
        "eventName": "CreateUser",
        "eventSource": "iam.amazonaws.com",
        "awsRegion": "us-east-1",
        "sourceIPAddress": "192.0.2.0",
        "userAgent": "aws-cli/2.13.5",
        "userIdentity": {
            "type": "IAMUser", "principalId": "AIDA", "arn": "arn:aws:iam::888:user/Mary",
            "accountId": "888", "userName": "Mary",
        },
        "recipientAccountId": "888",
        "readOnly": False,
    },
    ("wazuh", "wazuh.detection"): {
        "timestamp": "2026-06-21T10:00:00Z",
        "id": "1700000000.123",
        "rule": {"level": 7, "description": "Multiple failed logins", "groups": ["authentication_failed"]},
        "agent": {"id": "001", "name": "host-a", "ip": "10.0.0.5"},
        "data": {"srcip": "1.2.3.4", "srcuser": "root"},
        "location": "/var/log/auth.log",
    },
    # Fontes push/ingest — amostras sintéticas para o benchmark.
    ("fortinet_fortigate", "fortinet_fortigate.traffic"): {
        "timestamp": "2026-06-21T10:00:00Z",
        "devname": "FGT-EDGE-01",
        "logid": "0000000013",
        "type": "traffic",
        "subtype": "forward",
        "level": "notice",
        "srcip": "10.0.0.5",
        "srcport": 51514,
        "dstip": "8.8.8.8",
        "dstport": 443,
        "proto": 6,
        "action": "accept",
        "service": "HTTPS",
        "app": "HTTPS.BROWSER",
        "sentbyte": 1024,
        "rcvdbyte": 4096,
        "srcintf": "lan",
        "dstintf": "wan1",
        "user": "alice",
    },
    ("windows_event_log", "windows_event_log.security"): {
        "TimeCreated": "2026-06-21T10:00:00Z",
        "EventID": 4624,
        "Channel": "Security",
        "Provider": "Microsoft-Windows-Security-Auditing",
        "Level": "Information",
        "Computer": "WIN-DC-01",
        "Message": "An account was successfully logged on.",
        "TargetUserName": "alice",
        "TargetDomainName": "ACME",
        "IpAddress": "10.0.0.5",
        "IpPort": "51514",
        "LogonType": "3",
    },
}


_ITERATIONS = 1000  # eventos por benchmark
_REGRESSION_CEILING_US = 50_000  # 50ms/evento — sinal de bug grave


@pytest.mark.parametrize("vendor,event_type", list(DEFAULT_MAPPING_FILES.keys()))
def test_normalize_throughput_baseline(vendor, event_type, capsys) -> None:
    raw = _FIXTURES[(vendor, event_type)]
    rules = load_default_rules(vendor, event_type)
    compiled = compile_rules(rules)
    ctx = EnvelopeContext(
        vendor=vendor,
        integration_id=1,
        customer_id=42,
        stream=event_type.split(".", 1)[1],
        event_type=event_type,
        mapping_version_id="bench",
    )

    # Warmup — primeira chamada inclui caches misses e JIT-y stuff.
    for _ in range(50):
        applied = apply_compiled(compiled, raw)
        build_envelope(raw, applied.output, ctx)

    durations: list[float] = []
    for _ in range(_ITERATIONS):
        start = time.perf_counter()
        applied = apply_compiled(compiled, raw)
        build_envelope(raw, applied.output, ctx)
        durations.append(time.perf_counter() - start)

    avg_us = mean(durations) * 1_000_000
    # Print no stdout pra registrar baseline em CI logs.
    with capsys.disabled():
        print(
            f"\n[bench] {vendor}/{event_type}: "
            f"avg={avg_us:.1f}µs/evt over {_ITERATIONS} iters"
        )

    # Hard ceiling — só dispara em regressão patológica.
    assert avg_us < _REGRESSION_CEILING_US, (
        f"normalize regrediu para {avg_us:.0f}µs/evt — "
        f"investigar antes de fazer merge"
    )
