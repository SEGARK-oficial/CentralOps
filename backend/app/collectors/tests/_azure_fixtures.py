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
        "resourceId": f"/subscriptions/{sub}/resourceGroups/rg-app/providers/{'/'.join(operation.split('/')[:2])}/res-1",
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


# ── Log Analytics workspace ──────────────────────────────────────────────
#
# Cada linha abaixo é o MESMO evento de um fixture acima, na forma que a tabela
# do workspace o guarda (colunas e tipos da referência "Azure Monitor Logs
# reference"). O teste de equivalência exige que a conversão de volta dê o mesmo
# OCSF que o registro original.

WORKSPACE = "0f0f0f0f-0000-4000-8000-0000000000aa"

_STATUS_TO_LA = {"Succeeded": "Success", "Failed": "Failure", "Started": "Start", "Accepted": "Accept"}


def _pascal(key: str) -> str:
    return key[:1].upper() + key[1:]


def la_activity_row(event: Dict[str, Any], ingested: str = "2026-10-27T12:09:00.0000001Z") -> Dict[str, Any]:
    """Evento REST do Activity Log → linha do ``AzureActivity``."""
    op = event["operationName"]["value"]
    props = {**event["properties"], "entity": event["resourceId"]}
    return {
        "TimeGenerated": event["eventTimestamp"],
        "EventSubmissionTimestamp": event["submissionTimestamp"],
        "OperationName": event["operationName"]["localizedValue"],
        "OperationNameValue": op.upper(),
        "Level": "Information" if event["level"] == "Informational" else event["level"],
        "ActivityStatusValue": _STATUS_TO_LA.get(event["status"]["value"], event["status"]["value"]),
        "ActivitySubstatusValue": event["subStatus"]["value"],
        "ResourceGroup": event["resourceGroupName"].upper(),
        "SubscriptionId": event["subscriptionId"],
        "CorrelationId": event["correlationId"],
        "Caller": event["caller"],
        "CallerIpAddress": event["httpRequest"]["clientIpAddress"],
        "CategoryValue": event["category"]["value"],
        "HTTPRequest": json.dumps(event["httpRequest"]),
        "Properties": json.dumps(props),
        "Properties_d": props,
        "Authorization": json.dumps(event["authorization"]),
        "Authorization_d": event["authorization"],
        "Claims": json.dumps(event["claims"]),
        "Claims_d": event["claims"],
        "ResourceProviderValue": event["resourceProviderName"]["value"].upper(),
        "EventDataId": event["eventDataId"],
        "TenantId": WORKSPACE,
        "SourceSystem": "Azure",
        "Hierarchy": f"{TENANT}/{event['subscriptionId']}",
        "Type": "AzureActivity",
        "_ResourceId": event["resourceId"].lower(),
        "_ord": ingested,
    }


_ENTRA_TABLE = {
    "SignInLogs": "SigninLogs",
    "NonInteractiveUserSignInLogs": "AADNonInteractiveUserSignInLogs",
    "ServicePrincipalSignInLogs": "AADServicePrincipalSignInLogs",
    "AuditLogs": "AuditLogs",
}
_ENTRA_RENAME = {"location": "LocationDetails", "ipAddress": "IPAddress", "operationType": "AADOperationType"}


def la_entra_row(record: Dict[str, Any], ingested: str = "2026-10-27T12:25:00.0000001Z") -> Dict[str, Any]:
    """Registro do diagnostic setting do Entra → linha da tabela do workspace.

    Na tabela não interativa e na de service principal as colunas de objeto são
    TEXTO JSON (``string``), não ``dynamic`` — ``la_payload`` as serializa.
    """
    table = _ENTRA_TABLE[record["category"]]
    props = dict(record["properties"])
    row: Dict[str, Any] = {
        "TimeGenerated": record["time"],
        "ResourceId": record["resourceId"],
        "OperationName": record["operationName"],
        "OperationVersion": record["operationVersion"],
        "Category": props.pop("category", None) or record["category"],
        "ResultType": record.get("resultType"),
        "ResultSignature": record.get("resultSignature"),
        "ResultDescription": record.get("resultDescription"),
        "DurationMs": record.get("durationMs"),
        "CorrelationId": record.get("correlationId"),
        "Identity": record.get("identity"),
        "Level": str(record.get("Level")),
        "Location": record.get("location"),
        "AADTenantId": record["tenantId"],
        "Type": table,
        "_ord": ingested,
    }
    if table != "SigninLogs" and table != "AuditLogs":
        row["TenantId"] = WORKSPACE
    for key, value in props.items():
        if key == "correlationId":
            continue
        row[_ENTRA_RENAME.get(key, _pascal(key))] = value
    if table == "AuditLogs":
        row["CallerIpAddress"] = record["callerIpAddress"]
    return {k: v for k, v in row.items() if v is not None}


#: Colunas de objeto que são ``string`` (texto JSON) nestas tabelas.
STRING_JSON_TABLES = {"AADNonInteractiveUserSignInLogs", "AADServicePrincipalSignInLogs"}


def la_keyvault_row(record: Dict[str, Any], ingested: str = "2026-10-27T12:35:00.0000001Z") -> Dict[str, Any]:
    props = record["properties"]
    return {
        "TimeGenerated": record["time"],
        "OperationName": record["operationName"],
        "OperationVersion": record["operationVersion"],
        "_ResourceId": record["resourceId"],
        "ResultType": record["resultType"],
        "ResultSignature": record["resultSignature"],
        "DurationMs": int(record["durationMs"]),
        "CallerIpAddress": record["callerIpAddress"],
        "CorrelationId": record["correlationId"],
        "Identity": record["identity"],
        "Id": props["id"],
        "ClientInfo": props["clientInfo"],
        "HttpStatusCode": props["httpStatusCode"],
        "RequestUri": props["requestUri"],
        "IsAccessPolicyMatch": props["isAccessPolicyMatch"],
        "IsRbacAuthorized": props["isRbacAuthorized"],
        "TenantId": WORKSPACE,
        "Type": "AZKVAuditLogs",
        "_ord": ingested,
    }


def la_aks_row(ingested: str = "2026-10-27T12:45:00.0000001Z") -> Dict[str, Any]:
    log = _KUBE_LOG
    return {
        "TimeGenerated": AKS_EXEC["time"],
        "AuditId": log["auditID"],
        "Stage": log["stage"],
        "RequestUri": log["requestURI"],
        "Verb": log["verb"],
        "User": log["user"],
        "SourceIps": log["sourceIPs"],
        "UserAgent": log["userAgent"],
        "ObjectRef": log["objectRef"],
        "ResponseStatus": log["responseStatus"],
        "RequestReceivedTime": log["requestReceivedTimestamp"],
        "Level": log["level"],
        "PodName": "kube-apiserver-0",
        "_ResourceId": AKS_EXEC["resourceId"],
        "TenantId": WORKSPACE,
        "Type": "AKSAudit",
        "_ord": ingested,
    }


def la_sql_legacy_row(record: Dict[str, Any], ingested: str = "2026-10-27T12:55:00.0000001Z") -> Dict[str, Any]:
    """Auditoria do Azure SQL na tabela legada ``AzureDiagnostics``: cada
    propriedade vira coluna com sufixo de tipo."""
    row: Dict[str, Any] = {
        "TimeGenerated": record["time"],
        "Category": record["category"],
        "OperationName": record["operationName"],
        "ResourceId": record["resourceId"],
        "Resource": "VENDAS",
        "ResourceGroup": "RG-APP",
        "ResourceProvider": "MICROSOFT.SQL",
        "ResourceType": "SERVERS/DATABASES",
        "SubscriptionId": SUB_A,
        "TenantId": WORKSPACE,
        "Type": "AzureDiagnostics",
        "_ord": ingested,
    }
    for key, value in record["properties"].items():
        row[f"{key}_s"] = value
    return row


STORAGE_DELETE: Dict[str, Any] = {
    "time": "2026-10-27T13:10:00.0000000Z",
    "resourceId": f"/subscriptions/{SUB_A}/resourceGroups/rg-app/providers/Microsoft.Storage/storageAccounts/stbackup/blobServices/default",
    "category": "StorageDelete",
    "operationName": "DeleteBlob",
    "operationVersion": "2021-08-06",
    "schemaVersion": "1.0",
    "statusCode": 403,
    "statusText": "AuthorizationPermissionMismatch",
    "durationMs": 4,
    "callerIpAddress": "203.0.113.7:51234",
    "correlationId": "c0ffee00-0000-4000-8000-000000000009",
    "identity": {
        "type": "OAuth",
        "requester": {
            "appId": "66666666-6666-4666-8666-666666666666",
            "objectId": "77777777-7777-4777-8777-777777777777",
            "tenantId": TENANT,
            "upn": "ana.silva@example.com",
        },
    },
    "location": "brazilsouth",
    "properties": {
        "accountName": "stbackup",
        "userAgentHeader": "azsdk-python-storage-blob/12.19.0",
        "serviceType": "blob",
        "objectKey": "/stbackup/backups/db-2026-10-26.bak",
    },
    "uri": "https://stbackup.blob.core.windows.net/backups/db-2026-10-26.bak",
    "protocol": "HTTPS",
}


def la_storage_row(ingested: str = "2026-10-27T13:15:00.0000001Z") -> Dict[str, Any]:
    rec = STORAGE_DELETE
    req = rec["identity"]["requester"]
    return {
        "TimeGenerated": rec["time"],
        "AccountName": "stbackup",
        "Location": rec["location"],
        "Protocol": rec["protocol"],
        "OperationName": rec["operationName"],
        "OperationVersion": rec["operationVersion"],
        "SchemaVersion": rec["schemaVersion"],
        "AuthenticationType": rec["identity"]["type"],
        "StatusCode": str(rec["statusCode"]),
        "StatusText": rec["statusText"],
        "DurationMs": float(rec["durationMs"]),
        "CallerIpAddress": rec["callerIpAddress"],
        "CorrelationId": rec["correlationId"],
        "Uri": rec["uri"],
        "UserAgentHeader": rec["properties"]["userAgentHeader"],
        "RequesterAppId": req["appId"],
        "RequesterObjectId": req["objectId"],
        "RequesterTenantId": req["tenantId"],
        "RequesterUpn": req["upn"],
        "ServiceType": "blob",
        "ObjectKey": rec["properties"]["objectKey"],
        "Category": rec["category"],
        "_ResourceId": rec["resourceId"],
        "TenantId": WORKSPACE,
        "Type": "StorageBlobLogs",
        "_ord": ingested,
    }


def _la_type(name: str, value: Any, table: str) -> str:
    if name in ("TimeGenerated", "_ord") or name.endswith("Time"):
        return "datetime"
    if isinstance(value, (dict, list)):
        return "string" if table in STRING_JSON_TABLES else "dynamic"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "long"
    if isinstance(value, float):
        return "real"
    return "string"


def la_payload(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Resposta da API de consulta a um ``union``, como o Log Analytics a monta.

    Colunas tipadas, ``dynamic`` serializado como texto JSON, nulo onde a tabela
    da linha não tem a coluna e, quando o TIPO de uma coluna difere entre as
    tabelas, uma coluna por tipo (``Status_dynamic``/``Status_string``) — o que
    a união real faz. A linha ganha ``_table`` (o ``withsource``) e ``_key``.
    """
    full = [{**row, "_table": row["Type"], "_key": f"{i + 1:064x}"} for i, row in enumerate(rows)]
    kinds: Dict[str, List[str]] = {}
    order: List[str] = []
    for row in full:
        for name, value in row.items():
            if name not in kinds:
                kinds[name] = []
                order.append(name)
            kind = _la_type(name, value, row["Type"])
            if kind not in kinds[name]:
                kinds[name].append(kind)
    columns = [
        (name, kind, name if len(kinds[name]) == 1 else f"{name}_{kind}")
        for name in order for kind in kinds[name]
    ]
    out_rows = []
    for row in full:
        out = []
        for name, kind, _ in columns:
            value = row.get(name)
            if value is not None and _la_type(name, value, row["Type"]) != kind:
                value = None
            if isinstance(value, (dict, list)):
                value = json.dumps(value)
            out.append(value)
        out_rows.append(out)
    return {"tables": [{"name": "PrimaryResult",
                        "columns": [{"name": col, "type": kind} for _, kind, col in columns],
                        "rows": out_rows}]}
