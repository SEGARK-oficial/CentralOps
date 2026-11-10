---
sidebar_position: 7
title: AWS WAF
description: Collect the log of every request evaluated by AWS WAF web ACLs, read from S3 or from CloudWatch Logs
---

# AWS WAF

Connect AWS WAF to CentralOps to collect the **log of every request evaluated by a web ACL**: what WAF decided (allow, block, CAPTCHA, challenge), which rule decided, the client's IP and country, the URI and the User-Agent. CentralOps reads the log where WAF itself delivers it: in an S3 bucket or in a CloudWatch Logs log group.

## When to use

- **See attacks blocked at the edge.** SQLi, XSS, bots, AWS managed rules.
- **Find what got through.** A rule in **COUNT** mode matched and the request reached the application. This is the event that calls for investigation.
- **Track a client.** All requests from an IP or from a TLS fingerprint (JA4) that changes IP.
- **Tune rules.** Which rules block legitimate traffic, by URI and country.

## How it works

```
Request ────► web ACL (WAF) ─► logging ─► bucket S3 aws-waf-logs-*      ─┐
                                      └─► log group aws-waf-logs-*      ─┴─► CentralOps (every 5 min)
```

WAF has no API that returns the full log; the log only exists after logging is turned on for the web ACL. Each web ACL delivers to **one** destination, whose name must start with `aws-waf-logs-`.

| Item | S3 | CloudWatch Logs |
|---|---|---|
| Stream | `events` (type `aws_waf.event`) | the same |
| Where it reads | `AWSLogs/<account>/WAFLogs/<region>/<web-acl>/AAAA/MM/dd/HH/mm/*.log.gz` | the log group you entered |
| Format | one JSON record per line, 5-minute files | one JSON record per log event |
| Web ACLs | all that deliver to the bucket, found on their own | all that deliver to the log group |
| Regions | all, or the ones you list (includes `cloudfront`) | the log group's region |
| Collection position | `LastModified` of the last file, with a 15-minute re-read | time of the last event read |
| Cap per cycle | 500 files | 20,000 events |
| When to prefer it | high volume (cheaper) | low volume, or if the log is already in CloudWatch |

Both paths deliver the same record. An S3 file that **cannot be read** (no `s3:GetObject`, no `kms:Decrypt`) stops the cycle without moving the position forward, and the error shows up in **Pipeline health**. A **corrupted** file is skipped and recorded in `skipped_objects` in the collection state. An invalid line inside a good file is dropped on its own.

To receive the WAF log **in near real time**, with the web ACL's **Firehose** destination, use the [AWS via Firehose](./aws-firehose.md) integration.

## Prerequisites

- **Logging turned on in the web ACL**, to an S3 bucket or a log group named `aws-waf-logs-…`. In the console: **WAF & Shield → Web ACLs → your ACL → Logging and metrics → Enable**.
- **Read access** to the destination: the CentralOps cross-account role (recommended) or an access key.

## Step 1: Turn on web ACL logging

If it is not on yet, choose the destination and turn it on. With the CLI, for S3:

```bash
aws s3api create-bucket --bucket aws-waf-logs-minha-empresa --region us-east-1
aws wafv2 put-logging-configuration --logging-configuration \
  ResourceArn=arn:aws:wafv2:us-east-1:111122223333:regional/webacl/minha-acl/ID-DA-ACL,LogDestinationConfigs=arn:aws:s3:::aws-waf-logs-minha-empresa
```

To see where a web ACL already delivers:

```bash
aws wafv2 get-logging-configuration \
  --resource-arn arn:aws:wafv2:us-east-1:111122223333:regional/webacl/minha-acl/ID-DA-ACL \
  --query "LoggingConfiguration.LogDestinationConfigs"
```

The value returned is exactly what goes in the **WAF log destination** field in Step 3.

:::tip[Cut the volume at the source]

WAF also logs the allowed requests, which are most of them. To send only what matters, use the WAF **logging filter** (keep `BLOCK` and `COUNT`, drop the rest) and **redacted fields** so the `Authorization` and `Cookie` headers are not written.

:::

## Step 2: Give CentralOps read access

Use the same **cross-account role** as CloudTrail. The step by step in the console is in [AWS: give CentralOps access](./aws-acesso.md). In the integration form, the **AWS role access** panel shows the organization's External ID, the CentralOps principal and the CloudFormation template. The step by step is in [AWS CloudTrail: Option A](./aws-cloudtrail.md#opção-a-recomendada-papel-cross-account).

When you create the stack, enter the WAF bucket:

```bash
aws cloudformation deploy \
  --template-file centralops-readonly-role.json \
  --stack-name centralops-readonly \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides WafLogsBucketName=aws-waf-logs-minha-empresa
```

The template grants `s3:ListBucket`/`s3:GetObject` on the WAF bucket and CloudWatch Logs read access (`logs:FilterLogEvents`). If the CloudTrail bucket is also read, pass both parameters in the same stack. With SSE-KMS, also enter `KmsKeyArn`.

With an **access key** (legacy), or with a role created by hand, this is the minimum policy. Change the bucket (S3) or the region, account and log group (CloudWatch), and use only the block for the destination you use:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "WafNoS3Listar",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::aws-waf-logs-minha-empresa"
    },
    {
      "Sid": "WafNoS3Ler",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::aws-waf-logs-minha-empresa/*"
    },
    {
      "Sid": "WafNoCloudWatch",
      "Effect": "Allow",
      "Action": "logs:FilterLogEvents",
      "Resource": "arn:aws:logs:us-east-1:111122223333:log-group:aws-waf-logs-api:*"
    }
  ]
}
```

## Step 3: Create the integration in CentralOps

1. Go to **Collect -> Integrations** and add an **AWS WAF** integration.
2. Fill in:

   | Field | What to enter |
   |---|---|
   | Role ARN or Access Key ID | The stack's `RoleArn` (e.g. `arn:aws:iam::111122223333:role/CentralOpsReadOnly`) or the `AccessKeyId`. |
   | Secret Access Key | Only with an Access Key ID. |
   | WAF log destination | The ARN that WAF shows: `arn:aws:s3:::aws-waf-logs-minha-empresa` (or `s3://aws-waf-logs-minha-empresa/prefixo`) for S3, or `arn:aws:logs:us-east-1:111122223333:log-group:aws-waf-logs-api` for CloudWatch. A bare name is rejected: the bucket and the log group use the same `aws-waf-logs-` prefix. |
   | AWS regions | Only for S3. Empty or `*` = all regions in the bucket, including `cloudfront`. Or a list: `us-east-1, sa-east-1`. |
   | Account ID | The account that owns the logs. Empty = the account in the role ARN (S3) or in the log group (CloudWatch). |

3. Click **Test connection**. With a role, the test assumes the role. Then it lists the bucket (S3) or reads 1 event from the log group (CloudWatch).
4. Save. Collection starts on the next cycle (up to 5 minutes).

## What is collected

Each record becomes an OCSF 1.8 event of class **HTTP Activity (4002)**, with the original record in `raw`.

| WAF action | `disposition` (`disposition_id`) | `action` | Severity | Alert |
|---|---|---|---|---|
| `ALLOW`, no rule matched | Allowed (1) | Allowed | Informational | no |
| `ALLOW` with a rule in COUNT mode | Count (20) | Allowed | **Medium** | yes |
| `BLOCK` | Blocked (2) | Denied | Low | yes |
| `CAPTCHA` | Captcha (22) | Other | Low | yes |
| `CHALLENGE` | Challenge (23) | Other | Low | yes |

The allowed request that matched a rule in COUNT gets the higher severity on purpose: it reached the application.

| OCSF field | Source in the WAF log |
|---|---|
| `time` | `timestamp` (epoch in ms) |
| `activity_id` / `type_uid` | `httpRequest.httpMethod` (GET = 3, POST = 6, …) |
| `src_endpoint.ip` / `.location.country` | `httpRequest.clientIp` / `httpRequest.country` (`-` = empty) |
| `http_request.url.hostname`, `dst_endpoint.hostname` | `Host` header (or `:authority` in HTTP/2) |
| `http_request.url.path` / `.query_string` | `httpRequest.uri` / `httpRequest.args` |
| `http_request.user_agent` | `User-Agent` header |
| `http_request.http_method` / `.version` / `.uid` | `httpMethod` / `httpVersion` / `requestId` |
| `http_response.code` | `responseCodeSent` (WAF custom response) |
| `firewall_rule.uid` / `.type` | `terminatingRuleId` / `terminatingRuleType` |
| `firewall_rule.match_location` / `.match_details` | `terminatingRuleMatchDetails[0]` (SQLi/XSS) |
| `policy.uid` | `webaclId` |
| `metadata.labels` | `labels[].name` (e.g. `awswaf:managed:aws:bot-control:…`) |
| `ja4_fingerprint_list` | `ja4Fingerprint` (CloudFront and ALB) |
| `cloud.region` / `cloud.account.uid` | S3 path or log group ARN |
| `unmapped.aws_waf_count_rules` | `nonTerminatingMatchingRules[].ruleId` |
| `unmapped.aws_waf_group_rule` | the rule inside the managed group that decided |
| `unmapped.aws_waf_attack_type` | `terminatingRuleMatchDetails[0].conditionType` |

The other fields (`ruleGroupList`, `rateBasedRuleList`, full headers) are kept in `raw`. Duplicates are dropped by the combination of web ACL + `requestId` + `timestamp`.

## Troubleshooting

**`Informe o destino de logging da web ACL como ARN…`.** The field got a bare name. Use the ARN from Step 1 or `s3://bucket`.

**The test passes, but nothing arrives (S3).** Check the path WAF uses:

```bash
aws s3 ls s3://aws-waf-logs-minha-empresa/AWSLogs/111122223333/WAFLogs/ --recursive | tail -3
```

Empty? Logging is not on, or the Account ID is not the account of the web ACLs. If the folders are from another region, check the **AWS regions** field.

**`waf: falha ao ler s3://… — AccessDenied`.** The role lists the bucket but does not read the file: `s3:GetObject` is missing or, with the `kms:Decrypt` hint, permission on the KMS key. Collection stays stopped at that point, without losing anything, until the permission is fixed.

**Events in quarantine with `unparsedMessage`.** The log group you entered has events that are not from WAF. Check the log group ARN.

## Known limitations

- **This integration does not read Firehose.** For the web ACL's Firehose destination, use the [AWS via Firehose](./aws-firehose.md) integration: WAF delivers straight to CentralOps, with no bucket.
- **One account per integration on S3.** A central bucket with logs from several accounts needs one integration per account.
- **No discovery through the WAF API.** Web ACLs are found through the bucket's folders, not through `wafv2:ListWebACLs`.
- **Volume.** A web ACL with a lot of allowed traffic goes over the cap of 500 files per cycle. Filter at the source (see Step 1).
