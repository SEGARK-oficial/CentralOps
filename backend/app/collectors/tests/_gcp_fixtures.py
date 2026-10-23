"""LogEntries SINTÉTICOS do Cloud Logging, no formato que o sink publica no Pub/Sub.

Forma copiada da documentação do Google (AuditLog, audit do GKE e
``MysqlAuditEntry`` do Cloud SQL); nomes, projetos e IPs são inventados (faixas
de documentação 192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24). Nada aqui vem
de ambiente real.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Dict

PROJECT = "projeto-exemplo"

SET_IAM_POLICY: Dict[str, Any] = {
    "insertId": "-a1b2c3d4e5",
    "logName": f"projects/{PROJECT}/logs/cloudaudit.googleapis.com%2Factivity",
    "protoPayload": {
        "@type": "type.googleapis.com/google.cloud.audit.AuditLog",
        "authenticationInfo": {"principalEmail": "ana.silva@example.com"},
        "requestMetadata": {
            "callerIp": "203.0.113.10",
            "callerSuppliedUserAgent": "google-cloud-sdk gcloud/480.0.0",
        },
        "serviceName": "cloudresourcemanager.googleapis.com",
        "methodName": "SetIamPolicy",
        "resourceName": f"projects/{PROJECT}",
        "serviceData": {
            "@type": "type.googleapis.com/google.iam.v1.logging.AuditData",
            "policyDelta": {
                "bindingDeltas": [
                    {"action": "ADD", "role": "roles/owner", "member": "user:externo@example.org"}
                ]
            },
        },
        "status": {},
    },
    "receiveTimestamp": "2026-10-23T12:00:01.234567890Z",
    "resource": {"type": "project", "labels": {"project_id": PROJECT}},
    "severity": "NOTICE",
    # 9 casas de fração: é como o Cloud Logging grava, e a imagem roda Python 3.11.
    "timestamp": "2026-10-23T12:00:00.123456789Z",
}

DENIED_SA_KEY: Dict[str, Any] = {
    "insertId": "-f6g7h8",
    "logName": f"projects/{PROJECT}/logs/cloudaudit.googleapis.com%2Fpolicy",
    "protoPayload": {
        "@type": "type.googleapis.com/google.cloud.audit.AuditLog",
        "authenticationInfo": {"principalEmail": "deploy@projeto-exemplo.iam.gserviceaccount.com"},
        "requestMetadata": {"callerIp": "private"},
        "serviceName": "iam.googleapis.com",
        "methodName": "google.iam.admin.v1.CreateServiceAccountKey",
        "resourceName": f"projects/-/serviceAccounts/deploy@{PROJECT}.iam.gserviceaccount.com",
        "status": {"code": 7, "message": "Permission 'iam.serviceAccountKeys.create' denied"},
    },
    "resource": {"type": "service_account", "labels": {"project_id": PROJECT}},
    "severity": "ERROR",
    "timestamp": "2026-10-23T12:05:00Z",
}

WORKSPACE_LOGIN_FAILURE: Dict[str, Any] = {
    "insertId": "login-1",
    "logName": "organizations/123456789/logs/cloudaudit.googleapis.com%2Fdata_access",
    "protoPayload": {
        "@type": "type.googleapis.com/google.cloud.audit.AuditLog",
        "authenticationInfo": {"principalEmail": "bruno.costa@example.com"},
        "requestMetadata": {"callerIp": "198.51.100.23"},
        "serviceName": "login.googleapis.com",
        "methodName": "google.login.LoginService.loginFailure",
        "metadata": {"event": [{"eventName": "login_failure", "eventType": "login"}]},
    },
    "resource": {"type": "audited_resource", "labels": {"service": "login.googleapis.com"}},
    "severity": "NOTICE",
    "timestamp": "2026-10-23T12:10:00Z",
}

GKE_EXEC: Dict[str, Any] = {
    "insertId": "gke-exec-1",
    "logName": f"projects/{PROJECT}/logs/cloudaudit.googleapis.com%2Factivity",
    "protoPayload": {
        "@type": "type.googleapis.com/google.cloud.audit.AuditLog",
        "authenticationInfo": {"principalEmail": "ana.silva@example.com"},
        "authorizationInfo": [
            {"granted": True, "permission": "io.k8s.core.v1.pods.exec.create",
             "resource": "core/v1/namespaces/producao/pods/api-7d9f/exec"}
        ],
        "requestMetadata": {"callerIp": "203.0.113.10", "callerSuppliedUserAgent": "kubectl/v1.31.0"},
        "serviceName": "k8s.io",
        "methodName": "io.k8s.core.v1.pods.exec.create",
        "resourceName": "core/v1/namespaces/producao/pods/api-7d9f/exec",
        "status": {},
    },
    "resource": {
        "type": "k8s_cluster",
        "labels": {"project_id": PROJECT, "location": "southamerica-east1", "cluster_name": "cluster-exemplo"},
    },
    "severity": "NOTICE",
    "timestamp": "2026-10-23T12:15:00Z",
}

GKE_SECRET_READ_BY_SA: Dict[str, Any] = {
    "insertId": "gke-secret-1",
    "logName": f"projects/{PROJECT}/logs/cloudaudit.googleapis.com%2Fdata_access",
    "protoPayload": {
        "@type": "type.googleapis.com/google.cloud.audit.AuditLog",
        "authenticationInfo": {"principalEmail": "system:serviceaccount:default:app"},
        "requestMetadata": {"callerIp": "10.0.0.5"},
        "serviceName": "k8s.io",
        "methodName": "io.k8s.core.v1.secrets.get",
        "resourceName": "core/v1/namespaces/default/secrets/db-password",
        "status": {},
    },
    "resource": {
        "type": "k8s_cluster",
        "labels": {"project_id": PROJECT, "location": "southamerica-east1", "cluster_name": "cluster-exemplo"},
    },
    "severity": "INFO",
    "timestamp": "2026-10-23T12:16:00Z",
}


def _mysql(request: Dict[str, Any], ts: str, insert_id: str) -> Dict[str, Any]:
    return {
        "insertId": insert_id,
        "logName": f"projects/{PROJECT}/logs/cloudaudit.googleapis.com%2Fdata_access",
        "protoPayload": {
            "@type": "type.googleapis.com/google.cloud.audit.AuditLog",
            "serviceName": "cloudsql.googleapis.com",
            "request": {
                "@type": "type.googleapis.com/google.cloud.sql.audit.v1.MysqlAuditEntry",
                "msgType": "activity",
                **request,
            },
        },
        "resource": {
            "type": "cloudsql_database",
            "labels": {"project_id": PROJECT, "database_id": f"{PROJECT}:mysql-prod", "region": "southamerica-east1"},
        },
        "severity": "INFO",
        "timestamp": ts,
    }


MYSQL_LOGIN_FAILURE = _mysql(
    {"status": "unsuccessful", "user": "root", "host": "", "ip": "198.51.100.7",
     "errCode": "1045", "cmd": "connect", "threadId": "812", "queryId": "0"},
    "2026-10-23T12:20:00Z", "sql-1",
)

MYSQL_GRANT = _mysql(
    {"status": "success", "user": "admin", "privUser": "admin", "ip": "203.0.113.40",
     "cmd": "grant", "objects": [{"db": "vendas", "name": "pedidos", "objType": "TABLE"}],
     "query": "GRANT SELECT ON vendas.pedidos TO 'relatorio'@'%'", "threadId": "813", "queryId": "9"},
    "2026-10-23T12:21:00Z", "sql-2",
)


def pubsub_message(entry: Dict[str, Any], *, ack_id: str, published: str) -> Dict[str, Any]:
    """Um ``receivedMessages[]`` do ``pull``, com o LogEntry em base64."""
    return {
        "ackId": ack_id,
        "message": {
            "data": base64.b64encode(json.dumps(entry).encode()).decode(),
            "messageId": f"msg-{ack_id}",
            "publishTime": published,
        },
    }
