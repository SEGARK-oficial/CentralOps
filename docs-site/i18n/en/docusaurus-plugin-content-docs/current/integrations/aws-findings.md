---
sidebar_position: 7
title: AWS Security Hub and GuardDuty
description: Collect AWS security findings (Security Hub in ASFF and GuardDuty) as OCSF Detection Finding
---

# AWS Security Hub and GuardDuty

Two integrations to bring AWS **security findings** into CentralOps, normalized as OCSF 1.8 **Detection Finding (2004)**.

| Integration | What it brings | When to use |
|---|---|---|
| **AWS Security Hub** | Findings aggregated in ASFF: GuardDuty, Inspector, Macie, IAM Access Analyzer, Config and Security Hub's own controls (CSPM) | The account uses Security Hub. It is the default choice: one integration, every product |
| **Amazon GuardDuty** | Threat findings straight from the GuardDuty API (accounts, EC2, S3, EKS, RDS, Lambda) | The account uses GuardDuty without Security Hub |

With both turned on, GuardDuty findings arrive twice (through Security Hub and directly). Pick one.

## How it works

| Item | Security Hub | GuardDuty |
|---|---|---|
| Stream | `findings` (type `aws_securityhub.finding`) | `findings` (type `aws_guardduty.finding`) |
| API | `GetFindings`, filter `UpdatedAt`, 100 per page | `ListDetectors` → `ListFindings` (filter `updatedAt`, 50 IDs) → `GetFindings` |
| Frequency | every 5 minutes | every 5 minutes |
| Regions | a list or `*` (the ones enabled in the account, via `ec2:DescribeRegions`) | a list or `*`; a region without GuardDuty is skipped |
| First collection | findings updated in the last 24 hours | same |
| Collection position | per region: the newest `UpdatedAt` read | same |
| Cap per cycle | 50 pages, adding up all regions; the current region saves the exact position | same |
| Duplicates | `Id` + `UpdatedAt` | same |

**Each update of a finding is an event.** GuardDuty adds occurrences to an existing finding, and Security Hub changes the workflow or archives it. That is why CentralOps treats `Id` + `UpdatedAt` as the identity: the same threat happening again creates a new event, with `activity_id` **Update**.

**A finding indexed late is not lost.** Each query starts 15 minutes before the saved position, and the position never goes past the end of the window minus 15 minutes. Whatever is read again is dropped by the dedupe.

**Security Hub with cross-Region aggregation.** If *cross-Region aggregation* is on, the aggregation region returns the findings of all regions. Enter only that one; listing the others would bring duplicates.

## Prerequisites

- **The service turned on in the account and in the regions you will list.** In the console: **Security Hub → Go to Security Hub → Enable Security Hub**, or **GuardDuty → Get Started → Enable GuardDuty**, in each region. GuardDuty only creates findings after it is turned on; there is no earlier history.
- Read access through the CentralOps **cross-account role**. The step by step in the console is in [AWS: give CentralOps access](./aws-acesso.md). The template in the **AWS role access** panel already includes `securityhub:GetFindings`, `guardduty:ListDetectors`, `guardduty:ListFindings`, `guardduty:GetFindings` and `ec2:DescribeRegions`. The step by step for the role is in [AWS CloudTrail: Option A](./aws-cloudtrail.md#opção-a-recomendada-papel-cross-account).

With a role created by hand (or with an access key, legacy), this is the minimum policy. `ec2:DescribeRegions` is only used with `*` regions:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "LerAchados",
      "Effect": "Allow",
      "Action": [
        "securityhub:GetFindings",
        "guardduty:ListDetectors",
        "guardduty:ListFindings",
        "guardduty:GetFindings",
        "ec2:DescribeRegions"
      ],
      "Resource": "*"
    }
  ]
}
```

### Several accounts in an AWS Organization

The full step by step, in the console and with the CLI, is in [AWS: give CentralOps access](./aws-acesso.md#várias-contas-de-uma-aws-organization). Summary:

The same template works as a **StackSet**: the External ID is per CentralOps organization, not per AWS account, so all of a customer's accounts use the same role with the same External ID. In the management account (or the CloudFormation delegated administrator):

```bash
aws cloudformation create-stack-set \
  --stack-set-name centralops-readonly \
  --template-body file://centralops-readonly-role.json \
  --capabilities CAPABILITY_NAMED_IAM \
  --permission-model SERVICE_MANAGED \
  --auto-deployment Enabled=true,RetainStacksOnAccountRemoval=false

aws cloudformation create-stack-instances \
  --stack-set-name centralops-readonly \
  --deployment-targets OrganizationalUnitIds=r-abcd \
  --regions us-east-1
```

The role is global (IAM), so one region is enough. With `auto-deployment`, new accounts in the Organization get the role on their own. The management account does not get an instance of a *service-managed* StackSet; create the stack there separately if you will collect from it.

Two ways to cover the accounts:

- **The service's delegated administrator account.** With Security Hub or GuardDuty set up with a delegated administrator, a single integration in that account receives the findings of all member accounts.
- **AWS Organization mode (Enterprise).** The **Security Hub — AWS Organization** and **GuardDuty — AWS Organization** cards create a *parent* that lists the accounts (`organizations:ListAccounts`) and, for each approved account, a child integration in this same organization, assuming the role with the **same name and path** in the member account (the one the StackSet above created). Accounts that are suspended or leave the Organization have their child turned off at the next sync. The parent's role lives in the management account or in an AWS Organizations delegated administrator; create the stack there with `AllowListAccounts=true`. In Community the parent is created, but no account is imported. Organization mode requires a role; a fixed key is not accepted.

## Create the integration

1. Go to **Collect -> Integrations** and add **AWS Security Hub** or **Amazon GuardDuty**.
2. Fill in:

   | Field | What to enter |
   |---|---|
   | Role ARN or Access Key ID | The stack's `RoleArn` (e.g. `arn:aws:iam::111122223333:role/CentralOpsReadOnly`). |
   | Secret Access Key | Only with an Access Key ID. |
   | AWS regions | Security Hub with aggregation: only the aggregation region. Without aggregation, or for GuardDuty: a list (`us-east-1, sa-east-1`) or `*`. |

3. **Test connection.** Security Hub: reads 1 finding from the first region (`InvalidAccessException` = Security Hub is not enabled there). GuardDuty: looks for the detector in the first region.
4. Save.

## What is collected

### Security Hub (ASFF)

| OCSF field | Source |
|---|---|
| `time`, `finding_info.modified_time` | `UpdatedAt` |
| `finding_info.created_time` / `first_seen_time` / `last_seen_time` | `CreatedAt` / `FirstObservedAt` / `LastObservedAt` |
| `finding_info.uid` / `.title` / `.desc` / `.types` | `Id` / `Title` / `Description` / `Types` |
| `finding_info.product.name`, `metadata.product.name` | `ProductName` (GuardDuty, Inspector, Security Hub…) |
| `finding_info.analytic.uid` | `GeneratorId` (the rule or the control) |
| `severity_id` | `Severity.Label`: INFORMATIONAL 1, LOW 2, MEDIUM 3, HIGH 4, CRITICAL 5 |
| `status_id` | `Workflow.Status`: NEW 1, NOTIFIED 2, SUPPRESSED 3, RESOLVED 4; `RecordState` ARCHIVED 5 |
| `activity_id` | Create (never updated), Update, Close (ARCHIVED) |
| `resources[]` | `Resources[]` (`Type`, `Id`, `Region`) |
| `remediation.desc` / `.references` | `Remediation.Recommendation.Text` / `.Url` |
| `cloud.account.uid` / `cloud.region` | `AwsAccountId` / `Region` |
| `unmapped.aws_compliance_status` | `Compliance.Status` (PASSED, FAILED…) |

**INFORMATIONAL** findings come in with `is_alert=false`: the CSPM controls create many `PASSED` findings. Drop or sample them in the route, if you want.

### GuardDuty

| OCSF field | Source |
|---|---|
| `severity_id` | `Severity`: 1.0–3.9 Low, 4.0–6.9 Medium, 7.0–8.9 High, 9.0+ Critical |
| `finding_info.types` | `Type` (e.g. `UnauthorizedAccess:IAMUser/TorIPCaller`) |
| `finding_info.first_seen_time` / `last_seen_time`, `count` | `Service.EventFirstSeen` / `EventLastSeen`, `Service.Count` |
| `status_id`, `activity_id`, `is_alert` | `Service.Archived`: archived = Archived, Close, no alert |
| `src_endpoint.ip` / `.location.country` | Remote IP of the action (API call, connection, port probe, RDS login, Kubernetes) |
| `actor.user.name` / `.type` | `Resource.AccessKeyDetails.UserName` / `UserType` |
| `resources[]` | `Resource.ResourceType` + ID of the instance, key, bucket, EKS cluster, RDS database or Lambda function |
| `unmapped.aws_guardduty_api` | `Service.Action.AwsApiCallAction.Api` |

The full finding is kept in `raw`.

## Troubleshooting

**`InvalidAccessException` in Security Hub.** The service is not enabled in the region queried, or the account is not the administrator.

**GuardDuty: `Credencial OK, mas o GuardDuty não está habilitado em <region>`.** There is no detector in that region. During collection, the region is skipped without an error.

**Duplicate findings.** Security Hub and GuardDuty are both turned on for the same account, or Security Hub has aggregation and you listed several regions.

## Known limitations

- **Creating one integration per account automatically is Enterprise** (AWS Organization mode). In Community, use the service's delegated administrator or one integration per account.
- **Security Hub in ASFF.** The native OCSF format of the new Security Hub (`GetFindingsV2`) is not used.
- **No write-back.** CentralOps does not change the workflow or archive findings in AWS.
