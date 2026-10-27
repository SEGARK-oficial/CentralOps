"""Eventos SINTÉTICOS do Azure, no formato que cada fonte entrega.

- Activity Log: o objeto ``EventData`` da API REST do Azure Monitor
  (``eventtypes/management/values``).
- Entra ID e resource logs: os registros do schema comum de diagnostic settings,
  como chegam ao Event Hub dentro de ``{"records": [...]}``.

Forma copiada da documentação da Microsoft; tenants, assinaturas, nomes e IPs são
inventados (GUIDs de exemplo, faixas de documentação 192.0.2.0/24,
198.51.100.0/24, 203.0.113.0/24). Nada aqui vem de ambiente real.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

TENANT = "00000000-0000-4000-8000-000000000001"
CLIENT = "00000000-0000-4000-8000-0000000000c1"
SUB_A = "11111111-1111-4111-8111-111111111111"
SUB_B = "22222222-2222-4222-8222-222222222222"

_UPN_CLAIM = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/upn"
_OID_CLAIM = "http://schemas.microsoft.com/identity/claims/objectidentifier"


def activity(
    event_id: str,
    ts: str,
    *,
    sub: str = SUB_A,
    operation: str = "Microsoft.Authorization/roleAssignments/write",
    status: str = "Succeeded",
    level: str = "Informational",
    category: str = "Administrative",
    submitted: str | None = None,
) -> Dict[str, Any]:
    provider = operation.split("/")[0]
    return {
        "authorization": {
            "action": operation,
            "scope": f"/subscriptions/{sub}/resourceGroups/rg-app",
        },
        "caller": "ana.silva@example.com",
        "channels": "Operation",
        "claims": {
            _UPN_CLAIM: "ana.silva@example.com",
            _OID_CLAIM: "33333333-3333-4333-8333-333333333333",
            "appid": "04b07795-8ddb-461a-bbee-02f9e1bf7b46",
            "ipaddr": "203.0.113.10",
        },
        "correlationId": f"corr-{event_id}",
        "description": "",
        "eventDataId": event_id,
        "eventName": {"value": "EndRequest", "localizedValue": "End request"},
        "category": {"value": category, "localizedValue": category},
        "eventTimestamp": ts,
        "httpRequest": {
            "clientRequestId": f"req-{event_id}",
            "clientIpAddress": "203.0.113.10",
            "method": "PUT",
        },
        "id": f"/subscriptions/{sub}/resourceGroups/rg-app/providers/Microsoft.Authorization/roleAssignments/ra-1/events/{event_id}/ticks/1",
        "level": level,
        "operationId": f"op-{event_id}",
        "operationName": {"value": operation, "localizedValue": "Create role assignment"},
        "resourceGroupName": "rg-app",
        "resourceProviderName": {"value": provider, "localizedValue": provider},
        "resourceType": {"value": "/".join(operation.split("/")[:2]), "localizedValue": ""},
        "resourceId": f"/subscriptions/{sub}/resourceGroups/rg-app/providers/Microsoft.Authorization/roleAssignments/ra-1",
        "status": {"value": status, "localizedValue": status},
        "subStatus": {"value": "Created" if status == "Succeeded" else "", "localizedValue": ""},
        "submissionTimestamp": submitted or ts,
        "subscriptionId": sub,
        "tenantId": TENANT,
        "properties": {
            "statusCode": "Created" if status == "Succeeded" else "Forbidden",
            "eventCategory": category,
            "entity": f"/subscriptions/{sub}/resourceGroups/rg-app",
            "message": operation,
            "hierarchy": f"{TENANT}/{sub}",
        },
        "relatedEvents": [],
    }


ROLE_ASSIGNMENT = activity("aaaaaaaa-0000-4000-8000-000000000001", "2026-10-27T12:00:00.1234567Z")
STORAGE_LISTKEYS = activity(
    "aaaaaaaa-0000-4000-8000-000000000002",
    "2026-10-27T12:01:00Z",
    operation="MICROSOFT.STORAGE/STORAGEACCOUNTS/LISTKEYS/ACTION",
)
FAILED_DELETE = activity(
    "aaaaaaaa-0000-4000-8000-000000000003",
    "2026-10-27T12:02:00Z",
    operation="Microsoft.KeyVault/vaults/delete",
    status="Failed",
    level="Error",
)

# ── Entra ID (diagnostic settings) ───────────────────────────────────────


def signin(
    category: str = "SignInLogs",
    *,
    result: str = "0",
    risk: str = "none",
    mfa: bool = True,
    uid: str = "sid-0001",
) -> Dict[str, Any]:
    return {
        "time": "2026-10-27T12:10:00.0000000Z",
        "resourceId": f"/tenants/{TENANT}/providers/Microsoft.aadiam",
        "operationName": "Sign-in activity",
        "operationVersion": "1.0",
        "category": category,
        "tenantId": TENANT,
        "resultType": result,
        "resultSignature": "None",
        "resultDescription": "" if result == "0" else "Invalid username or password or Invalid on-premise username or password.",
        "durationMs": 0,
        "callerIpAddress": "198.51.100.23",
        "correlationId": "c0ffee00-0000-4000-8000-000000000001",
        "identity": "Bruno Costa",
        "Level": 4,
        "location": "BR",
        "properties": {
            "id": uid,
            "createdDateTime": "2026-10-27T12:10:00.0000000+00:00",
            "userDisplayName": "Bruno Costa",
            "userPrincipalName": "bruno.costa@example.com",
            "userId": "44444444-4444-4444-8444-444444444444",
            "appId": "00000003-0000-0000-c000-000000000000",
            "appDisplayName": "Microsoft Graph",
            "ipAddress": "198.51.100.23",
            "status": {"errorCode": int(result), "failureReason": "Other."},
            "clientAppUsed": "Browser",
            "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "isInteractive": category == "SignInLogs",
            "authenticationRequirement": "multiFactorAuthentication" if mfa else "singleFactorAuthentication",
            "riskLevelDuringSignIn": risk,
            "riskState": "none",
            "conditionalAccessStatus": "success",
            "deviceDetail": {"deviceId": "", "displayName": "", "operatingSystem": "Windows10", "browser": "Edge 130.0"},
            "location": {"city": "São Paulo", "state": "São Paulo", "countryOrRegion": "BR"},
        },
    }


def sp_signin() -> Dict[str, Any]:
    rec = signin("ServicePrincipalSignInLogs", uid="sid-sp-0001")
    props = rec["properties"]
    for key in ("userDisplayName", "userPrincipalName", "userId", "userAgent"):
        props.pop(key, None)
    props.update(
        servicePrincipalId="55555555-5555-4555-8555-555555555555",
        servicePrincipalName="ci-deploy",
        authenticationRequirement="singleFactorAuthentication",
    )
    return rec


def entra_audit(activity_name: str, operation_type: str = "Add", uid: str = "aud-0001") -> Dict[str, Any]:
    return {
        "time": "2026-10-27T12:20:00.0000000Z",
        "resourceId": f"/tenants/{TENANT}/providers/Microsoft.aadiam",
        "operationName": activity_name,
        "operationVersion": "1.0",
        "category": "AuditLogs",
        "tenantId": TENANT,
        "resultSignature": "None",
        "durationMs": 0,
        "callerIpAddress": "203.0.113.10",
        "correlationId": "c0ffee00-0000-4000-8000-000000000002",
        "Level": 4,
        "properties": {
            "id": uid,
            "category": "RoleManagement",
            "correlationId": "c0ffee00-0000-4000-8000-000000000002",
            "result": "success",
            "resultReason": "",
            "activityDisplayName": activity_name,
            "activityDateTime": "2026-10-27T12:20:00.0000000+00:00",
            "loggedByService": "Core Directory",
            "operationType": operation_type,
            "initiatedBy": {
                "user": {
                    "id": "33333333-3333-4333-8333-333333333333",
                    "userPrincipalName": "ana.silva@example.com",
                    "ipAddress": "203.0.113.10",
                }
            },
            "targetResources": [
                {
                    "id": "44444444-4444-4444-8444-444444444444",
                    "displayName": "Bruno Costa",
                    "type": "User",
                    "userPrincipalName": "bruno.costa@example.com",
                }
            ],
        },
    }


SIGNIN_OK = signin()
SIGNIN_FAILED_RISKY = signin(result="50126", risk="high", mfa=False, uid="sid-0002")
NONINTERACTIVE = signin("NonInteractiveUserSignInLogs", uid="sid-0003")
SP_SIGNIN = sp_signin()
ADD_ROLE_MEMBER = entra_audit("Add member to role")
RESET_PASSWORD = entra_audit("Reset password (by admin)", "Update", uid="aud-0002")
ADD_SP_CREDENTIAL = entra_audit(
    "Add service principal credentials", "Update", uid="aud-0003"
)

# ── Resource logs ────────────────────────────────────────────────────────

_RG = f"/SUBSCRIPTIONS/{SUB_A.upper()}/RESOURCEGROUPS/RG-APP/PROVIDERS"

KEYVAULT_SECRET_GET: Dict[str, Any] = {
    "time": "2026-10-27T12:30:00.0000000Z",
    "category": "AuditEvent",
    "operationName": "SecretGet",
    "operationVersion": "7.4",
    "resourceId": f"{_RG}/MICROSOFT.KEYVAULT/VAULTS/KV-PROD",
    "resultType": "Success",
    "resultSignature": "OK",
    "resultDescription": "",
    "durationMs": "12",
    "callerIpAddress": "203.0.113.50",
    "correlationId": "c0ffee00-0000-4000-8000-000000000003",
    "identity": {
        "claim": {
            "appid": "66666666-6666-4666-8666-666666666666",
            "http://schemas.microsoft.com/identity/claims/objectidentifier": "77777777-7777-4777-8777-777777777777",
            "upn": "ana.silva@example.com",
        }
    },
    "properties": {
        "id": "https://kv-prod.vault.azure.net/secrets/db-password/abc123",
        "clientInfo": "azsdk-python-keyvault-secrets/4.8.0",
        "httpStatusCode": 200,
        "requestUri": "https://kv-prod.vault.azure.net/secrets/db-password/?api-version=7.4",
        "isAccessPolicyMatch": False,
        "isRbacAuthorized": True,
    },
    "location": "brazilsouth",
}

KEYVAULT_FORBIDDEN: Dict[str, Any] = {
    **KEYVAULT_SECRET_GET,
    "operationName": "SecretList",
    "resultType": "Success",
    "resultSignature": "Forbidden",
    "properties": {**KEYVAULT_SECRET_GET["properties"], "httpStatusCode": 403},
}

_KUBE_LOG = {
    "kind": "Event",
    "apiVersion": "audit.k8s.io/v1",
    "level": "RequestResponse",
    "auditID": "f0000000-0000-4000-8000-000000000001",
    "stage": "ResponseComplete",
    "requestURI": "/api/v1/namespaces/payments/pods/api-7d9/exec?command=sh&stdin=true&tty=true",
    "verb": "create",
    "user": {"username": "carla.dias@example.com", "groups": ["system:authenticated"]},
    "sourceIPs": ["198.51.100.77"],
    "userAgent": "kubectl/v1.31.0",
    "objectRef": {"resource": "pods", "namespace": "payments", "name": "api-7d9", "subresource": "exec"},
    "responseStatus": {"code": 101},
    "requestReceivedTimestamp": "2026-10-27T12:40:00.000000Z",
}

AKS_EXEC: Dict[str, Any] = {
    "time": "2026-10-27T12:40:00.0000000Z",
    "category": "kube-audit",
    "operationName": "Microsoft.ContainerService/managedClusters/diagnosticLogs/Read",
    "resourceId": f"{_RG}/MICROSOFT.CONTAINERSERVICE/MANAGEDCLUSTERS/AKS-PROD",
    # Como chega do Event Hub: ``log`` é TEXTO JSON (o coletor decodifica).
    "properties": {"log": json.dumps(_KUBE_LOG), "stream": "stdout", "pod": "kube-apiserver-0"},
}

SQL_FAILED_LOGIN: Dict[str, Any] = {
    "time": "2026-10-27T12:50:00.0000000Z",
    "category": "SQLSecurityAuditEvents",
    "operationName": "AuditEvent",
    "resourceId": f"{_RG}/MICROSOFT.SQL/SERVERS/SQL-PROD/DATABASES/VENDAS",
    "properties": {
        "action_name": "DATABASE AUTHENTICATION FAILED",
        "action_id": "DBAF",
        "succeeded": "false",
        "server_principal_name": "app_user",
        "client_ip": "203.0.113.99",
        "application_name": "sqlcmd",
        "database_name": "vendas",
        "server_instance_name": "sql-prod",
        "statement": "",
        "additional_information": "<login_information><error_code>18456</error_code></login_information>",
    },
}

SQL_BATCH: Dict[str, Any] = {
    **SQL_FAILED_LOGIN,
    "properties": {
        **SQL_FAILED_LOGIN["properties"],
        "action_name": "BATCH COMPLETED",
        "action_id": "BCM",
        "succeeded": "true",
        "statement": "GRANT CONTROL ON DATABASE::vendas TO app_user",
        "object_name": "vendas",
        "additional_information": "",
    },
}

FIREWALL_RULE: Dict[str, Any] = {
    "time": "2026-10-27T13:00:00.0000000Z",
    "category": "AZFWNetworkRule",
    "operationName": "AZFWNetworkRule",
    "resourceId": f"{_RG}/MICROSOFT.NETWORK/AZUREFIREWALLS/FW-HUB",
    "properties": {
        "Protocol": "TCP",
        "SourceIp": "10.0.0.4",
        "SourcePort": 52344,
        "DestinationIp": "192.0.2.80",
        "DestinationPort": 443,
        "Action": "Deny",
    },
}


def eventhub_body(records: List[Dict[str, Any]]) -> bytes:
    """Corpo de UMA mensagem do Event Hub, como o Azure Monitor publica."""
    return json.dumps({"records": records}).encode("utf-8")
