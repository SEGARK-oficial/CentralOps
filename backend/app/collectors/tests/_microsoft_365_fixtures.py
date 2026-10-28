"""Eventos sintéticos da Office 365 Management Activity API (schema comum + workloads).

Formato conferido no schema oficial (``office-365-management-activity-api-schema``):
``CreationTime`` SEM sufixo de fuso (UTC), ``RecordType``/``UserType`` inteiros,
``ResultStatus`` em texto. Nenhum dado real: tenant e usuários de exemplo
(``example.com``, IPs de documentação RFC 5737/3849).
"""

from __future__ import annotations

from typing import Any, Dict

TENANT = "00000000-0000-4000-8000-0000000000a1"
CLIENT = "00000000-0000-4000-8000-0000000000c1"
ORG = TENANT


def _common(op: str, workload: str, record_type: int, *, uid: str, when: str, **extra: Any) -> Dict[str, Any]:
    event: Dict[str, Any] = {
        "CreationTime": when,
        "Id": uid,
        "Operation": op,
        "OrganizationId": ORG,
        "RecordType": record_type,
        "UserKey": "10032001A2B3C4D5",
        "UserType": 0,
        "Version": 1,
        "Workload": workload,
        "ClientIP": "203.0.113.10",
        "UserId": "ana.silva@example.com",
    }
    event.update(extra)
    return event


USER_LOGGED_IN = _common(
    "UserLoggedIn", "AzureActiveDirectory", 15,
    uid="aad-login-ok-0001", when="2026-10-27T13:00:00",
    ResultStatus="Success",
    ApplicationId="00000002-0000-0ff1-ce00-000000000000",
    ExtendedProperties=[
        {"Name": "UserAgent", "Value": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        {"Name": "RequestType", "Value": "Login:login"},
        {"Name": "ResultStatusDetail", "Value": "Success"},
    ],
)

USER_LOGIN_FAILED = _common(
    "UserLoginFailed", "AzureActiveDirectory", 15,
    uid="aad-login-fail-0001", when="2026-10-27T13:01:00",
    ResultStatus="Success",  # o STS marca Success mesmo na falha: o status sai da operação
    ClientIP="198.51.100.77",
    LogonError="InvalidUserNameOrPassword",
    ExtendedProperties=[{"Name": "ResultStatusDetail", "Value": "Redirect"}],
)

ADD_MEMBER_TO_ROLE = _common(
    "Add member to role.", "AzureActiveDirectory", 8,
    uid="aad-role-0001", when="2026-10-27T13:02:00",
    ResultStatus="Success", UserType=2, ClientIP="",
    ObjectId="bruno.costa@example.com",
    ModifiedProperties=[{"Name": "Role.DisplayName", "NewValue": "Global Administrator", "OldValue": ""}],
)

RESET_PASSWORD = _common(
    "Reset user password.", "AzureActiveDirectory", 8,
    uid="aad-reset-0001", when="2026-10-27T13:03:00",
    ResultStatus="Success", UserType=2, ObjectId="carla.dias@example.com",
)

FILE_DOWNLOADED = _common(
    "FileDownloaded", "OneDrive", 6,
    uid="spo-dl-0001", when="2026-10-27T13:04:00",
    ClientIP="[2001:db8::10]:443",
    ObjectId="https://contoso-my.sharepoint.example.com/personal/ana/Documents/plano.xlsx",
    SourceFileName="plano.xlsx",
    SiteUrl="https://contoso-my.sharepoint.example.com/personal/ana/",
    UserAgent="OneDrive/23.0",
)

NEW_INBOX_RULE = _common(
    "New-InboxRule", "Exchange", 1,
    uid="exo-rule-0001", when="2026-10-27T13:05:00",
    ResultStatus="True", ClientIP="203.0.113.10:51234",
    ObjectId="ana.silva@example.com\\Encaminhar tudo",
    Parameters=[
        {"Name": "ForwardTo", "Value": "externo@example.net"},
        {"Name": "Name", "Value": "Encaminhar tudo"},
    ],
)

MAIL_ITEMS_ACCESSED = _common(
    "MailItemsAccessed", "Exchange", 50,
    uid="exo-mia-0001", when="2026-10-27T13:06:00",
    ResultStatus="Succeeded",
    AppAccessContext={"ClientAppId": "00000000-0000-4000-8000-0000000000d1"},
)

TEAMS_MEMBER_ADDED = _common(
    "MemberAdded", "MicrosoftTeams", 25,
    uid="teams-0001", when="2026-10-27T13:07:00",
)

DLP_RULE_MATCH: Dict[str, Any] = {
    **_common(
        "DlpRuleMatch", "SharePoint", 11,
        uid="dlp-0001", when="2026-10-27T13:08:00", ClientIP=None,
    ),
    "IncidentId": "inc-0001",
    "PolicyDetails": [
        {
            "PolicyId": "pol-0001",
            "PolicyName": "Dados de cartão (PCI)",
            "Rules": [
                {
                    "RuleId": "rule-0001",
                    "RuleName": "Muitos números de cartão",
                    "Severity": "High",
                    "Actions": ["NotifyUser", "BlockAccess"],
                    "ConditionsMatched": {
                        "SensitiveInformation": [
                            {
                                "SensitiveType": "50842eb7-edc8-4019-85dd-5a5c1f2bb085",
                                "SensitiveInformationTypeName": "Credit Card Number",
                                "Count": 2,
                                "Confidence": 85,
                                "SensitiveInformationDetections": {
                                    "DetectedValues": [
                                        {"Name": "Credit Card Number", "Value": "VALOR-SENSIVEL-1"},
                                        {"Name": "Credit Card Number", "Value": "VALOR-SENSIVEL-2"},
                                    ],
                                    "ResultsTruncated": False,
                                },
                            }
                        ]
                    },
                }
            ],
        }
    ],
    "SensitiveInfoDetectionIsIncluded": True,
    "SharePointMetaInformation": {
        "FileName": "faturas.xlsx",
        "FilePathUrl": "https://contoso.sharepoint.example.com/sites/fin/faturas.xlsx",
        "SiteCollectionUrl": "https://contoso.sharepoint.example.com/sites/fin",
    },
}
