"""Payloads sintéticos do Google Workspace (Reports API e Alert Center).

Formato copiado da referência da API (``activities.list`` / ``alerts.list``),
com domínio e pessoas fictícios (``example.com``).
"""

from __future__ import annotations

import itertools
from typing import Any, Dict, List, Optional

CUSTOMER = "C01abc23d"
ADMIN = "auditoria@example.com"

_seq = itertools.count(1000)


def param(name: str, value: Any) -> Dict[str, Any]:
    if isinstance(value, bool):
        return {"name": name, "boolValue": value}
    if isinstance(value, int):
        return {"name": name, "intValue": str(value)}
    if isinstance(value, list):
        return {"name": name, "multiValue": value}
    return {"name": name, "value": value}


def activity(
    app: str,
    *events: Dict[str, Any],
    time: str,
    actor: str = "ana.silva@example.com",
    ip: Optional[str] = "203.0.113.10",
    unique: Optional[str] = None,
) -> Dict[str, Any]:
    """Uma atividade como ``activities.list`` devolve em ``items[]``."""
    item: Dict[str, Any] = {
        "kind": "admin#reports#activity",
        "id": {
            "time": time,
            "uniqueQualifier": unique or str(next(_seq)),
            "applicationName": app,
            "customerId": CUSTOMER,
        },
        "etag": '"etag-de-teste"',
        "actor": {"callerType": "USER", "email": actor, "profileId": "100000000000000000001"},
        "events": list(events),
    }
    if ip:
        item["ipAddress"] = ip
    return item


def event(name: str, type_: str, **params: Any) -> Dict[str, Any]:
    return {"type": type_, "name": name, "parameters": [param(k, v) for k, v in params.items()]}


def page(*items: Dict[str, Any], token: Optional[str] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"kind": "admin#reports#activities", "items": list(items)}
    if token:
        out["nextPageToken"] = token
    return out


def explode(item: Dict[str, Any], index: int = 0) -> Dict[str, Any]:
    """O registro que o coletor emite para ``events[index]`` (sem importar o coletor)."""
    base = {k: v for k, v in item.items() if k != "events"}
    ev = dict(item["events"][index])
    params: Dict[str, Any] = {}
    for p in ev.get("parameters") or []:
        for key in ("value", "intValue", "boolValue", "multiValue"):
            if key in p:
                params[p["name"]] = p[key]
                break
    ev["params"] = params
    return {**base, "event": ev, "_event_index": index}


# ── Registros prontos (emitidos pelo coletor) ────────────────────────────

LOGIN_SUCCESS = explode(activity(
    "login",
    event("login_success", "login", login_type="google_password", is_suspicious=False,
          login_challenge_method=["password", "google_prompt"]),
    time="2026-10-27T12:00:00.123Z", unique="-111",
))
LOGIN_FAILURE = explode(activity(
    "login",
    event("login_failure", "login", login_type="google_password", login_failure_type="login_failure_invalid_password"),
    time="2026-10-27T12:01:00.000Z", unique="-112",
))
SUSPICIOUS_LOGIN = explode(activity(
    "login",
    event("suspicious_login", "login", login_type="google_password",
          affected_email_address="ana.silva@example.com"),
    time="2026-10-27T12:02:00.000Z", ip="198.51.100.77", unique="-113",
))
CHALLENGE_FAILED = explode(activity(
    "login",
    event("login_challenge", "login", login_challenge_status="Challenge Failed",
          login_challenge_method="idv_preregistered_phone"),
    time="2026-10-27T12:03:00.000Z", unique="-114",
))
LOGOUT = explode(activity("login", event("logout", "logout"), time="2026-10-27T12:04:00.000Z"))
GRANT_ADMIN = explode(activity(
    "admin",
    event("ASSIGN_ROLE", "DELEGATED_ADMIN_SETTINGS", ROLE_NAME="_SEED_ADMIN_ROLE",
          USER_EMAIL="bruno.costa@example.com"),
    time="2026-10-27T12:05:00.000Z", actor=ADMIN, unique="-115",
))
SUSPEND_USER = explode(activity(
    "admin",
    event("SUSPEND_USER", "USER_SETTINGS", USER_EMAIL="carla.dias@example.com"),
    time="2026-10-27T12:06:00.000Z", actor=ADMIN,
))
TWO_SV_DISABLE = explode(activity(
    "user_accounts",
    event("2sv_disable", "2sv_change"),
    time="2026-10-27T12:07:00.000Z",
))
OAUTH_GRANT = explode(activity(
    "token",
    event("authorize", "auth", app_name="App de Teste", client_id="123456789012.apps.googleusercontent.com",
          scope=["https://mail.google.com/"]),
    time="2026-10-27T12:08:00.000Z", unique="-116",
))
DRIVE_DOWNLOAD = explode(activity(
    "drive",
    event("download", "access", doc_id="1AbCdEf", doc_title="planilha.xlsx", doc_type="spreadsheet",
          owner="ana.silva@example.com"),
    time="2026-10-27T12:09:00.000Z",
))
GROUP_SETTING = explode(activity(
    "groups",
    event("change_acl_permission", "acl_change", group_email="time@example.com"),
    time="2026-10-27T12:10:00.000Z",
))

# ── Alert Center ─────────────────────────────────────────────────────────


def alert(alert_id: str, create_time: str, *, type_: str = "Suspicious login",
          severity: Optional[str] = "HIGH", status: str = "NOT_STARTED") -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "customerId": CUSTOMER[1:],
        "alertId": alert_id,
        "createTime": create_time,
        "startTime": create_time,
        "updateTime": create_time,
        "type": type_,
        "source": "Google identity",
        "data": {
            "@type": "type.googleapis.com/google.apps.alertcenter.type.AccountWarning",
            "email": "ana.silva@example.com",
            "loginDetails": [{"loginTime": create_time, "ipAddress": "198.51.100.77"}],
        },
        "securityInvestigationToolLink": "https://admin.google.com/ac/sc/investigation?query=x",
        "metadata": {"customerId": CUSTOMER[1:], "alertId": alert_id, "status": status},
    }
    if severity:
        out["metadata"]["severity"] = severity
    return out


def alerts_page(*alerts: Dict[str, Any], token: Optional[str] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"alerts": list(alerts)}
    if token:
        out["nextPageToken"] = token
    return out


SUSPICIOUS_LOGIN_ALERT = alert("alerta-0001", "2026-10-27T12:00:00.000Z")
LEAKED_PASSWORD_ALERT = alert("alerta-0002", "2026-10-27T12:30:00.000Z", type_="Leaked password",
                              severity=None, status="CLOSED")

ALL_ACTIVITY: List[Dict[str, Any]] = [
    LOGIN_SUCCESS, LOGIN_FAILURE, SUSPICIOUS_LOGIN, CHALLENGE_FAILED, LOGOUT, GRANT_ADMIN,
    SUSPEND_USER, TWO_SV_DISABLE, OAUTH_GRANT, DRIVE_DOWNLOAD, GROUP_SETTING,
]
