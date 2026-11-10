---
sidebar_position: 7
title: AWS CloudWatch Logs
description: Collect the events of a CloudWatch Logs log group (Lambda, ECS, API Gateway, RDS, applications) with FilterLogEvents
---

# AWS CloudWatch Logs

Connect a CloudWatch Logs **log group** to CentralOps to collect the logs that services and applications write there: Lambda functions, ECS tasks, API Gateway, logs exported from RDS, appliances with the CloudWatch agent. It is AWS's "catch-all" source. For AWS's own API events (IAM, console), use [AWS CloudTrail](./aws-cloudtrail.md).

## When to use it

- **Application logs on AWS.** Errors and access denials from an API that runs on Lambda or ECS.
- **Managed service logs.** API Gateway, error and audit logs exported from RDS.
- **Sources without their own integration** that already write to CloudWatch through an agent.

## How it works

```
service / app ──────► log group ──FilterLogEvents (every 5 min) ──► CentralOps
```

| Item | Value |
|---|---|
| Stream | `events` (event type `aws_cloudwatch.event`) |
| API | [`FilterLogEvents`](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_FilterLogEvents.html) on `logs.<region>.amazonaws.com`, in ascending time order |
| Frequency | every 5 minutes |
| Scope | **one log group per integration**, all of its log streams |
| Collection position | event time (ms) up to where the window was read; each new window goes back 5 minutes to catch events that came in late |
| Duplicates | dropped by `eventId` (unique per event, guaranteed by AWS) |
| First collection | events from the last hour |
| Window per cycle | at most 6 hours. A larger lag is drained in chunks, cycle by cycle |
| Cap per cycle | 20 pages of 1,000 events (20,000 events). When the cap is hit, the cycle saves the `nextToken` and the window, and the next one continues from the same page |

The `FilterLogEvents` response does not include the log group, region, account or a readable time. CentralOps adds the `logGroupName`, `awsRegion`, `awsAccountId` (when entered) and `eventTime` (ISO 8601, converted from the `timestamp` in milliseconds) fields to the raw event.

## Who can do what

- **Platform administrator:** creates and edits the integration (menu **Collect -> Integrations**).
- **In AWS:** creating the role (or the user) and the policy requires IAM permissions.

## Prerequisites

- The **exact name of the log group** (e.g. `/aws/lambda/faturamento-api`) and its **region**.
- A **cross-account role** (recommended) or an **IAM user access key**. Temporary credentials with a session token typed in by hand are not accepted.

## Step 1: Give CentralOps read access

**Recommended: cross-account role.** The step by step in the console is in [AWS: give CentralOps access](./aws-acesso.md). It is the same role as for CloudTrail: the **AWS role access** panel, in the integration form, shows the organization's External ID, the CentralOps principal and the CloudFormation template. The template already includes `logs:DescribeLogGroups` and `logs:FilterLogEvents` on all the log groups in the account. To limit it to a single log group, create the role by hand ([Path C](./aws-acesso.md#caminho-c-à-mão-no-console-do-iam)) with the policy below. The step by step, including the server identity in a self-hosted installation, is in [AWS CloudTrail — Option A](./aws-cloudtrail.md#opção-a-recomendada-papel-cross-account). Keep the stack's `RoleArn`.

**Legacy: IAM user access key.** The only permission used is `logs:FilterLogEvents` on the log group. Replace the region, account and name:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "LerOLogGroup",
      "Effect": "Allow",
      "Action": "logs:FilterLogEvents",
      "Resource": "arn:aws:logs:sa-east-1:111122223333:log-group:/aws/lambda/faturamento-api:*"
    }
  ]
}
```

```bash
aws iam create-user --user-name centralops-cloudwatch
aws iam put-user-policy --user-name centralops-cloudwatch \
  --policy-name centralops-cloudwatch-leitura --policy-document file://policy.json
aws iam create-access-key --user-name centralops-cloudwatch
```

Keep the `AccessKeyId` and the `SecretAccessKey` from the output. To read several log groups, repeat the `Resource` in the policy (or use `log-group:/aws/lambda/*`) and create one integration per log group.

## Step 2: Create the integration in CentralOps

1. In the side menu, go to **Collect -> Integrations** and add an **AWS CloudWatch Logs** integration.
2. Fill in:

   | Field | What to enter |
   |---|---|
   | Role ARN or Access Key ID | The role's `RoleArn` (e.g. `arn:aws:iam::111122223333:role/CentralOpsReadOnly`) or the `AccessKeyId`. CentralOps recognizes the mode from the format. |
   | Secret Access Key | Only with an Access Key ID. It is stored encrypted. With a role, leave it empty. |
   | Log Group | The exact name, with the slashes (e.g. `/aws/lambda/faturamento-api`). No wildcard. |
   | AWS region | The log group's region (e.g. `sa-east-1`). |
   | Account ID | Optional. It only fills in `cloud.account.uid` in the normalized event, because the API does not return the account. With a role, empty = the account in the ARN. |

3. Click **Test connection**. The test runs a `FilterLogEvents` for 1 event over the log group's last hour. It checks the credential, the permission and that the log group exists, all at once. With a role, it first really assumes the role. It replies `Conexão OK (papel assumido)…` or `Conexão OK (chave fixa)…`, or the reason for the failure (e.g. `Falha ao consultar o log group: ResourceNotFoundException — o recurso (ex.: log group) não existe nesta região.`).
4. Save. Collection starts on the next cycle (within 5 minutes).

## What is collected

Each event in the log group becomes an event normalized to OCSF 1.8, class **Base Event (0)**, because the content of a log group is free-form. The original event stays in `raw`.

If the `message` field is JSON, CentralOps reads it and uses some common fields. If it is not, the event is still normalized, with just the text message.

| OCSF field | Source |
|---|---|
| `time`, `metadata.original_time` | `eventTime` (comes from the CloudWatch `timestamp`) |
| `message` | `message`, in full |
| `severity_id` / `severity` | `level` or `severity` inside the JSON: `trace`/`debug`/`info`/`notice` → 1; `warn`/`warning` → 3; `error`/`err` → 4; `critical`/`crit`/`alert` → 5; `fatal`/`emerg`/`emergency` → 6. Without the field: 1 |
| `metadata.uid` | `eventId` |
| `metadata.log_name`, `unmapped.log_group` | `logGroupName` |
| `metadata.correlation_uid`, `unmapped.log_stream` | `logStreamName` |
| `cloud.region` / `cloud.account.uid` | region from the form / Account ID from the form |
| `unmapped.operation` | `eventName`, `operation` or `action` from the JSON |
| `unmapped.user_name` | `user`, `userName` or `userIdentity.userName` from the JSON |
| `unmapped.src_ip` | `sourceIPAddress`, `srcAddr`, `client_ip` or `clientIp` from the JSON |
| `unmapped.error_code` / `unmapped.error_message` | `errorCode` or `error` / `errorMessage` from the JSON |
| `unmapped.status`, `unmapped.user_agent`, `unmapped.user_uid` | `status`, `userAgent`, `userId` or `userIdentity.principalId` from the JSON |
| `observables` | the user and IP above, when they exist |

For a specific source (an application format of your own), create your own mapping in **Normalize -> Mappings** that puts the fields in the right OCSF class. See [Normalization](../normalization/overview).

### Example

Raw event (synthetic data), already with the fields CentralOps adds:

```json
{
  "eventId": "38451234567890123456789012345678901234567890123456789012",
  "timestamp": 1792503727000,
  "ingestionTime": 1792503728113,
  "logStreamName": "2026/10/20/[$LATEST]0a1b2c3d4e5f",
  "message": "{\"level\":\"ERROR\",\"operation\":\"DeleteInvoice\",\"user\":\"ana.souza@example.com\",\"clientIp\":\"198.51.100.17\",\"status\":403,\"errorMessage\":\"forbidden\"}",
  "logGroupName": "/aws/lambda/faturamento-api",
  "awsRegion": "sa-east-1",
  "awsAccountId": "111122223333",
  "eventTime": "2026-10-20T13:42:07.000Z"
}
```

Normalized event:

```json
{
  "normalized": {
    "class_uid": 0,
    "class_name": "Base Event",
    "category_uid": 0,
    "category_name": "Uncategorized",
    "activity_id": 0,
    "activity_name": "Unknown",
    "type_uid": 0,
    "time": 1792503727000,
    "metadata": {
      "original_time": "2026-10-20T13:42:07.000Z",
      "version": "1.8.0",
      "product": {
        "name": "CloudWatch Logs",
        "vendor_name": "AWS"
      },
      "uid": "38451234567890123456789012345678901234567890123456789012",
      "log_name": "/aws/lambda/faturamento-api",
      "log_provider": "AWS CloudWatch Logs",
      "correlation_uid": "2026/10/20/[$LATEST]0a1b2c3d4e5f"
    },
    "severity_id": 4,
    "severity": "High",
    "message": "{\"level\":\"ERROR\",\"operation\":\"DeleteInvoice\",\"user\":\"ana.souza@example.com\",\"clientIp\":\"198.51.100.17\",\"status\":403,\"errorMessage\":\"forbidden\"}",
    "cloud": {
      "provider": "AWS",
      "region": "sa-east-1",
      "account": {
        "uid": "111122223333"
      }
    },
    "unmapped": {
      "log_group": "/aws/lambda/faturamento-api",
      "log_stream": "2026/10/20/[$LATEST]0a1b2c3d4e5f",
      "operation": "DeleteInvoice",
      "error_code": null,
      "error_message": "forbidden",
      "user_name": "ana.souza@example.com",
      "user_uid": null,
      "src_ip": "198.51.100.17",
      "user_agent": null,
      "status": 403
    },
    "observables": [
      {
        "name": "actor_user",
        "type": "User Name",
        "type_id": 4,
        "value": "ana.souza@example.com"
      },
      {
        "name": "src_ip",
        "type": "IP Address",
        "type_id": 2,
        "value": "198.51.100.17"
      }
    ]
  }
}
```

## Troubleshooting

**`Falha ao consultar o log group: ResourceNotFoundException`.** The log group does not exist in that region. Check the name (with the leading slash) and the region: `aws logs describe-log-groups --log-group-name-prefix /aws/lambda/faturamento --region sa-east-1`.

**`Falha ao consultar o log group: AccessDeniedException`.** `logs:FilterLogEvents` is missing on that log group's ARN, or the policy points to another region or account.

**`AssumeRole: AccessDenied`.** The role's trust policy does not trust the principal shown in the panel or does not require the organization's External ID. See [AWS CloudTrail — Troubleshooting](./aws-cloudtrail.md#solução-de-problemas).

**The test passes, but nothing arrives.** The log group may not have received any events since the integration was created. Check with `aws logs filter-log-events --log-group-name /aws/lambda/faturamento-api --start-time $(( ($(date +%s) - 3600) * 1000 )) --limit 5 --region sa-east-1`.

**"Backlog" in Pipeline health.** The cycle read 20,000 events and there was still a queue left. If this always happens, the log group produces more than about 4,000 events per minute, which is what collection drains on an ongoing basis.

**Throttling errors (`ThrottlingException`).** The `FilterLogEvents` quota is per account and region, and it is shared with any other tool that reads logs in the same account. The cycle that fails is retried on the next one, from the saved position.

## Known limitations

- **One log group per integration**, by exact name. There is no wildcard or list.
- **One role per integration, with no log group discovery.** The log group name is typed in.
- **Generic class.** Every event comes out as Base Event (0); the fields extracted from the JSON go in `unmapped`, not in OCSF class attributes.
- **High volume builds up lag.** The cap of 20,000 events per 5-minute cycle limits throughput. Log groups such as the VPC Flow Logs of a busy VPC go beyond it.
- **Historical re-collection (backfill) does not respect the requested period.** The collector starts from the default window (last hour), not from the start of the chosen period.
