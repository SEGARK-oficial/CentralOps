---
sidebar_position: 6
title: AWS CloudTrail
description: Collect AWS API events (IAM, console, resource changes) by reading the files that CloudTrail delivers to S3
---

# AWS CloudTrail

Connect an AWS account to CentralOps to collect the **API events recorded by CloudTrail**: who called which API, from where, with which credential, and whether the call failed. CentralOps reads the `.json.gz` files that a *trail* delivers to an S3 bucket.

## When to use it

- **Watch IAM.** `CreateAccessKey`, `AttachUserPolicy` with `AdministratorAccess`, `CreateUser`, `UpdateAssumeRolePolicy`.
- **See console logins.** `ConsoleLogin` with and without MFA, and failed logins.
- **Detect defenses being turned off.** `StopLogging`, `DeleteTrail`, `PutBucketPolicy` making a bucket public, `DisableKey`.
- **Investigate a leaked key.** All the calls made with an `accessKeyId`, and from which IPs.

## How it works

```
AWS API ─────► CloudTrail (trail) ──► bucket S3 ──list + get (every 5 min) ──► CentralOps
```

| Item | Value |
|---|---|
| Stream | `events` (event type `aws_cloudtrail.event`) |
| Frequency | every 5 minutes |
| Where it reads | `s3://<bucket>/[prefixo/]AWSLogs/<Account ID>/CloudTrail/<region>/AAAA/MM/DD/` and, if it is an Organization trail, `…/AWSLogs/o-<org>/<Account ID>/CloudTrail/<region>/…` |
| Layout | discovered on every cycle by listing the bucket: Organization trails and regions (with `*`) need no configuration |
| Collection position | `LastModified` of the last file read; each cycle re-reads the previous 15 minutes to catch late deliveries |
| Duplicates | dropped by `eventID` (AWS repeats the same `eventID` when it delivers twice) |
| First collection | files delivered in the last 2 hours |
| Cap per cycle | 500 files. When the cap is hit, the cycle saves the exact position (day + last file) and the next one continues from there |
| Days per cycle | from the day of the last file read (minus 1 day of margin) up to today, at most 14 days |

CloudTrail partitions by the file's **delivery** date, not by the event time. That is why the position is the object's `LastModified`, and the lag shown in **Pipeline health** is CentralOps's lag behind the bucket, not AWS's own delivery delay.

Each file carries a `Records[]`. Each item becomes an event. A file that **cannot be read** (no `s3:GetObject`, no `kms:Decrypt`, network) **stops the cycle**: the collection position does not move forward and the error shows in **Pipeline health**, so that no file is lost. Only a **corrupted** file (invalid gzip or JSON, which re-reading will not fix) is skipped; it is recorded in the collection state (see [Troubleshooting](#solução-de-problemas)).

## What comes in and what does not

- **Comes in:** everything the trail writes to the bucket: management events and, if the trail also records *data events* (S3, Lambda), those too, because they go into the same files.
- **Does not come in:** the console's **Event history** (90 days, no trail) is not read. CloudTrail Insights and the *digest* files live under other prefixes (`CloudTrail-Insight/`, `CloudTrail-Digest/`) and are ignored. CloudTrail Lake is not used.

## Who can do what

- **Platform administrator:** creates and edits the integration (menu **Collect -> Integrations**).
- **In AWS:** creating the trail requires CloudTrail and S3 permissions in the account. Creating the user and the policy requires IAM permissions.

## Prerequisites {#pré-requisitos}

- **A trail delivering to an S3 bucket.** Without a trail, there is no file to read.
- **S3 key prefix.** If the trail uses an *S3 key prefix* (`meu-prefixo/AWSLogs/...`), enter the bucket as `bucket/meu-prefixo`.
- **An Organization trail works with no configuration.** It writes to `AWSLogs/o-<org>/<account>/...`; CentralOps finds this path by listing the bucket and reads only the account you entered. A trail converted to an Organization trail leaves files in the old path: both are read. For several accounts of an Organization, create one integration per account or use the [AWS Organization mode](./aws-acesso.md#várias-contas-de-uma-aws-organization) (Enterprise), which creates one integration per account on its own.
- **Regions.** An integration reads one region, a list (`us-east-1, sa-east-1`) or all the regions that exist in the bucket (`*`).
- **Listing the bucket.** Discovery lists `AWSLogs/` with a delimiter. An old policy that restricts `s3:ListBucket` by prefix denies this listing; the collector then falls back to the classic path `AWSLogs/<account>/CloudTrail/<region>/` (no Organization and no `*`).

:::info[IAM events arrive in us-east-1]

In a multi-region trail, events from global services (IAM, global STS, root user console login) are delivered under the `us-east-1` prefix. Even if your resources are in `sa-east-1`, include `us-east-1` in the regions (or use `*`) if you want to see IAM changes.

:::

## Step 1: Have a trail delivering to S3

If you already have a trail, write down the bucket name and whether it uses a key prefix (**CloudTrail → Trails → your trail → General details**).

If you do not, in the console:

1. Open **CloudTrail → Trails** and click **Create trail**.
2. Give it a name (e.g. `centralops`), choose **Create new S3 bucket** and write down the bucket name. The console creates the bucket with the policy the trail needs already in place.
3. **Log file SSE-KMS encryption** comes turned on in the console. If you leave it on, write down the key ARN (**AWS KMS alias**/**New**): CentralOps will need `kms:Decrypt` on it, and you enter the key in the role's `KmsKeyArn` parameter.
4. Click **Next**. Under **Event type**, leave **Management events** checked. Click **Next** and then **Create trail**.

With the CLI:

```bash
aws s3api create-bucket --bucket meu-bucket-cloudtrail --region us-east-1
# The trail needs a bucket policy that lets it write; the console creates
# this policy on its own. With the CLI, apply the "Amazon S3 bucket policy for
# CloudTrail" policy from the AWS documentation before the next command.
aws cloudtrail create-trail --name centralops --s3-bucket-name meu-bucket-cloudtrail \
  --is-multi-region-trail
aws cloudtrail start-logging --name centralops
```

AWS delivers the first file a few minutes after the trail starts logging.

## Step 2: Give CentralOps read access

There are two ways. Prefer the **role**: CentralOps does not store any long-lived key, and the customer can revoke access by deleting the role.

:::tip[Step by step in the console]

The [AWS: give CentralOps access](./aws-acesso.md) page shows how to create the role click by click (with CloudFormation or by hand in IAM), the minimum permissions for each integration, and what to do when the bucket is in another account. This is the summary.

:::

### Option A (recommended): cross-account role {#opção-a-recomendada-papel-cross-account}

In their own AWS account, the customer creates a read-only role that trusts the CentralOps AWS identity and requires an **External ID**. CentralOps assumes this role on every cycle (`sts:AssumeRole`) and works with temporary credentials valid for 1 hour.

1. In CentralOps, open **Collect -> Integrations**, choose **AWS CloudTrail** and select the organization. The **AWS role access** panel shows:
   - the organization's **External ID**. It is generated by CentralOps, is unique per organization and does not change;
   - the **CentralOps principal**, the ARN the role must trust;
   - the **Download template** button, a CloudFormation template with these two values already filled in.
2. In the customer's account, create the stack with the template. Enter the trail's bucket and, if the files are encrypted with SSE-KMS, the key ARN:

   ```bash
   aws cloudformation deploy \
     --template-file centralops-readonly-role.json \
     --stack-name centralops-readonly \
     --capabilities CAPABILITY_NAMED_IAM \
     --parameter-overrides CloudTrailBucketName=meu-bucket-cloudtrail \
       KmsKeyArn=arn:aws:kms:us-east-1:111122223333:key/ID-DA-CHAVE-DA-TRAIL

   aws cloudformation describe-stacks --stack-name centralops-readonly \
     --query "Stacks[0].Outputs[?OutputKey=='RoleArn'].OutputValue" --output text
   ```

   The template creates the `CentralOpsReadOnly` role with `s3:ListBucket`/`s3:GetObject` on the bucket, `kms:Decrypt` on the key (if you entered one) and the read access the other AWS integrations use (CloudWatch Logs, Security Hub, GuardDuty). No write permission. The full list is in [minimum permissions](./aws-acesso.md#permissões-mínimas-de-cada-integração).
3. Keep the `RoleArn` from the output for Step 3.

Prefer to create the role by hand? The panel shows the trust policy ready to use. The condition that matters is this one, with the organization's External ID:

```json
"Condition": { "StringEquals": { "sts:ExternalId": "centralops-<organization-id>" } }
```

:::warning[The External ID is the lock between customers]

Without it, anyone who knew the role ARN of another customer could register it in an integration of their own. That is why CentralOps generates the value, does not accept a typed-in value and never changes it. Do not remove the `sts:ExternalId` condition from the trust policy.

:::

:::info[Self-hosted installation: the server identity]

To assume roles, the CentralOps server needs an AWS identity of its own, through the standard SDK chain: the `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` variables, a profile, an instance profile (EC2) or IRSA (EKS). This identity only needs `sts:AssumeRole` on the customers' roles. The panel discovers its ARN on its own (`sts:GetCallerIdentity`). If the server's role has a *path* (`role/caminho/Nome`), set `AWS_TRUST_PRINCIPAL_ARN` to the full ARN, because STS does not return the path. Without an identity, the panel warns you and the connection test replies that the server has no AWS identity of its own.

:::

### Option B (legacy): IAM user access key

CentralOps also accepts an **IAM user access key** (Access Key ID + Secret Access Key). It is a long-lived credential: use it only when the role is not possible. Temporary credentials (`ASIA...` with a session token) are not accepted.

Create a dedicated user and attach this policy, replacing the bucket and the Account ID:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ListarOBucket",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::meu-bucket-cloudtrail"
    },
    {
      "Sid": "LerOsArquivosDaTrail",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::meu-bucket-cloudtrail/AWSLogs/111122223333/CloudTrail/*"
    }
  ]
}
```

If the trail encrypts the files with **SSE-KMS** (a KMS key of your own), add:

```json
{
  "Sid": "DecifrarOsArquivosDaTrail",
  "Effect": "Allow",
  "Action": "kms:Decrypt",
  "Resource": "arn:aws:kms:us-east-1:111122223333:key/ID-DA-CHAVE-DA-TRAIL"
}
```

:::warning[Do not restrict ListBucket by prefix]

The connection test lists the root of the bucket (one object, no prefix). An `s3:prefix` condition on `s3:ListBucket` makes the test fail with access denied, even when collection works. Restrict `s3:GetObject` instead, as in the example.

:::

```bash
aws iam create-user --user-name centralops-cloudtrail
aws iam put-user-policy --user-name centralops-cloudtrail \
  --policy-name centralops-cloudtrail-leitura --policy-document file://policy.json
aws iam create-access-key --user-name centralops-cloudtrail
```

Keep the `AccessKeyId` and the `SecretAccessKey` from the output. The secret is not shown again.

If the bucket is in **another account** (a central logging account), that account's bucket policy also needs to allow `s3:ListBucket` and `s3:GetObject` for the ARN of the role or the user.

## Step 3: Create the integration in CentralOps

1. In the side menu, go to **Collect -> Integrations** and add an **AWS CloudTrail** integration.
2. Fill in:

   | Field | What to enter |
   |---|---|
   | Role ARN or Access Key ID | Option A: the `RoleArn` from Step 2 (e.g. `arn:aws:iam::111122223333:role/CentralOpsReadOnly`). Option B: the `AccessKeyId` (e.g. `AKIAIOSFODNN7EXAMPLE`). CentralOps recognizes the mode from the format. |
   | Secret Access Key | Option B only: the `SecretAccessKey`. It is stored encrypted. With a role, leave it empty. |
   | S3 bucket | The bucket name (e.g. `meu-bucket-cloudtrail`). With a key prefix: `meu-bucket-cloudtrail/meu-prefixo`. `s3://…` and `arn:aws:s3:::…` also work. |
   | AWS regions | `us-east-1`, a list (`us-east-1, sa-east-1`) or `*` for all the regions in the bucket. The first region in the list is the S3 client's region (the SDK follows the redirect if the bucket is in another one). |
   | Account ID | The 12 digits of the account whose events are in the bucket (e.g. `111122223333`). It makes up the prefix that is read. With a role, it can stay empty when the logs' account is the same as the role's. |

3. Click **Test connection**. With a role, the test really assumes the role (it catches a wrong trust policy or External ID before you save). Then it lists one object in the bucket and replies `Conexão OK (papel assumido)…` or `Conexão OK (chave fixa)…`, or the reason for the failure (e.g. `AssumeRole: AccessDenied — confira se a trust policy do papel confia no principal do CentralOps e exige o External ID desta organização.`).

   :::note[The test checks the bucket, not the path]

   The test only proves that the credential can list the bucket. It does not check the Account ID, the region or the `s3:GetObject` permission. After saving, confirm in Step 4 that events arrive.

   :::

4. Save. Collection starts on the next cycle (within 5 minutes).

## Step 4: Check the first collection

- In **Collect -> Collectors**, the integration's `events` stream should show events after the first cycle.
- In **Pipeline health**, the data lag is the time since the last file read. See [Pipeline health](../operations/pipeline-health).
- To see the raw event and the normalized one side by side, use [live capture](../operations/live-capture).

## What is collected

Each item in `Records[]` becomes an event normalized to OCSF 1.8, class **API Activity (6003)**, with the original record kept in `raw`.

| OCSF field | Source in CloudTrail |
|---|---|
| `class_uid` / `activity_id` / `type_uid` | `6003` / `0` (Unknown) / `600300` on every event |
| `time` | `eventTime` |
| `api.operation`, `message`, `metadata.event_code` | `eventName` |
| `api.service.name` | `eventSource` |
| `api.response.error` / `api.response.message` | `errorCode` / `errorMessage` |
| `status` | `errorCode`; `Success` when there is no error |
| `actor.user.name` | `userIdentity.userName`; without it (roles, root), `userIdentity.arn` |
| `actor.user.uid` / `actor.user.type` | `userIdentity.principalId` / `userIdentity.type` |
| `src_endpoint.ip` | `sourceIPAddress` |
| `http_request.user_agent` | `userAgent` |
| `cloud.region` / `cloud.account.uid` | `awsRegion` / `recipientAccountId` |
| `metadata.uid` | `eventID` |
| `severity_id` | `1` (Informational) on every event |

`requestParameters`, `responseElements` and `userIdentity.accessKeyId` stay only in `raw`. Detection rules that look at, for example, which policy was attached should read `raw.requestParameters.policyArn`.

### Example

Raw event (synthetic data), as it comes out of the trail file:

```json
{
  "eventVersion": "1.09",
  "userIdentity": {
    "type": "IAMUser",
    "principalId": "AIDAEXAMPLEPRINCIPAL",
    "arn": "arn:aws:iam::111122223333:user/ana.souza",
    "accountId": "111122223333",
    "accessKeyId": "AKIAIOSFODNN7EXAMPLE",
    "userName": "ana.souza"
  },
  "eventTime": "2026-10-20T13:42:07Z",
  "eventSource": "iam.amazonaws.com",
  "eventName": "AttachUserPolicy",
  "awsRegion": "us-east-1",
  "sourceIPAddress": "203.0.113.24",
  "userAgent": "aws-cli/2.17.0 Python/3.11.9 Linux/6.8 exe/x86_64",
  "requestParameters": {
    "userName": "deploy-bot",
    "policyArn": "arn:aws:iam::aws:policy/AdministratorAccess"
  },
  "responseElements": null,
  "requestID": "6a1f8c2e-0b7d-4c55-9f3e-1d2a3b4c5d6e",
  "eventID": "3f9b6c1a-2d4e-4f8a-9b0c-7e6d5c4b3a21",
  "readOnly": false,
  "eventType": "AwsApiCall",
  "managementEvent": true,
  "recipientAccountId": "111122223333",
  "eventCategory": "Management"
}
```

Normalized event:

```json
{
  "normalized": {
    "class_uid": 6003,
    "class_name": "API Activity",
    "category_uid": 6,
    "category_name": "Application Activity",
    "activity_id": 0,
    "type_uid": 600300,
    "time": 1792503727000,
    "severity_id": 1,
    "severity": "Informational",
    "message": "AttachUserPolicy",
    "api": {
      "operation": "AttachUserPolicy",
      "service": {
        "name": "iam.amazonaws.com"
      },
      "response": {
        "error": null,
        "message": null
      }
    },
    "actor": {
      "user": {
        "name": "ana.souza",
        "uid": "AIDAEXAMPLEPRINCIPAL",
        "type": "IAMUser"
      }
    },
    "src_endpoint": {
      "ip": "203.0.113.24"
    },
    "http_request": {
      "user_agent": "aws-cli/2.17.0 Python/3.11.9 Linux/6.8 exe/x86_64"
    },
    "cloud": {
      "provider": "AWS",
      "region": "us-east-1",
      "account": {
        "uid": "111122223333"
      }
    },
    "status": "Success",
    "metadata": {
      "version": "1.8.0",
      "product": {
        "name": "CloudTrail",
        "vendor_name": "AWS"
      },
      "uid": "3f9b6c1a-2d4e-4f8a-9b0c-7e6d5c4b3a21",
      "event_code": "AttachUserPolicy"
    },
    "observables": [
      {
        "name": "actor_user",
        "type": "User Name",
        "type_id": 4,
        "value": "ana.souza"
      },
      {
        "name": "src_ip",
        "type": "IP Address",
        "type_id": 2,
        "value": "203.0.113.24"
      }
    ]
  }
}
```

## Troubleshooting {#solução-de-problemas}

**The test passes, but nothing arrives.**

1. Check whether there are files in the path CentralOps reads, with the same account, region and today's date:

   ```bash
   aws s3 ls s3://meu-bucket-cloudtrail/AWSLogs/111122223333/CloudTrail/us-east-1/2026/10/20/ | tail -3
   ```

   Empty? The Account ID or the region in the form does not match the trail, or the trail uses a key prefix that was not entered in the Bucket field (see [Prerequisites](#pré-requisitos)). Organization trail: the path is `AWSLogs/o-<org>/<account>/CloudTrail/…`.
2. Check the stream's error in **Pipeline health**.

**Error `cloudtrail: falha ao ler s3://… — AccessDenied`.** CentralOps can list the bucket, but cannot read a file. With the `kms:Decrypt` hint, the permission on the KMS key that encrypts the trail is missing; without it, `s3:GetObject` is missing. Collection stays stopped at that point, **without losing anything**: once the permission is fixed, the next cycle reads the file and moves on.

**`AssumeRole: AccessDenied` in the test or in collection.** The role's trust policy does not trust the principal shown in the panel, or does not require exactly the organization's External ID. Compare both values with the panel. In a self-hosted installation, the server identity also needs `sts:AssumeRole` on the role ARN.

**`o servidor do CentralOps não tem identidade AWS própria`.** A self-hosted installation with no AWS credentials in the server environment. See the *Self-hosted installation* note in Step 2.

**Corrupted file.** A file with invalid gzip or JSON is skipped (re-reading will not fix it) and recorded in `skipped_objects` in the stream's collection state, with the key and the reason (the 20 most recent). The worker log shows `cloudtrail: objeto <chave> corrompido`.

**`Falha ao acessar o bucket: AccessDenied` in the test.** Access denied on `s3:ListBucket` (see the Option B warning about the prefix condition) or a bucket in another account with no bucket policy. `NoSuchBucket`: wrong name or bucket in another region.

**IAM events do not show up.** They are under the `us-east-1` prefix. Include this region in the AWS regions field (or use `*`).

**"Backlog" in Pipeline health.** Each cycle processed 500 files and there was still a queue left. This is normal on the first load and after a stop. If it stays on for a long time, the trail's volume is more than one 5-minute cycle can drain.

## Known limitations

- **One account per integration.** In an Organization trail, each account needs its own integration. The **CloudTrail — AWS Organization** card (Enterprise) creates a parent that lists the accounts and one child per approved account, all reading the central bucket with the parent's role. This role needs to list the accounts (`organizations:ListAccounts`, with `AllowListAccounts=true` in the template) and read the bucket: usually it is the log archive account registered as a delegated administrator.
- **The bucket is typed in.** CentralOps discovers the layout inside the bucket, but does not discover which bucket the trail uses (`DescribeTrails`).
- **All events come out as API Activity with `activity_id` 0 and Informational severity.** CentralOps does not classify `Create`/`Read`/`Update`/`Delete` from the API name, and success or failure is in `status` (text), not in `status_id`.
- **Historical re-collection (backfill) does not respect the requested period.** The collector starts from the default window (files from the last 2 hours), not from the start of the chosen period.
- **A corrupted file is skipped** and only recorded in the collection state (see Troubleshooting).
