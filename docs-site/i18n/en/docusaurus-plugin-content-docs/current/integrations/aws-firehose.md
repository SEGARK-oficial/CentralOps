---
sidebar_position: 8
title: AWS via Firehose (push)
description: Receive WAF, CloudTrail, GuardDuty, Security Hub and CloudWatch Logs in near real time, pushed by AWS itself through an Amazon Data Firehose stream
---

# AWS via Firehose (push)

The other AWS integrations **fetch** the logs: they read the S3 bucket or call the API on each cycle. This integration **receives**: AWS itself pushes the logs to CentralOps through an **Amazon Data Firehose** stream with an **HTTP endpoint** destination. The delay drops from the collection cycle plus delivery to the bucket (5 to 15 minutes for CloudTrail) to the Firehose buffer (60 seconds or less).

## When to use

- **Latency matters.** Detection on WAF or on CloudTrail that cannot wait 15 minutes.
- **No read access to the account.** The customer does not need to create a role or open up a bucket: they only set up Firehose pointing to CentralOps.
- **The log already goes through Firehose.** WAF with a Firehose destination, CloudWatch Logs log group subscriptions and EventBridge rules.

When latency does not matter and you already have the read role, the pull integrations ([CloudTrail](./aws-cloudtrail.md), [WAF](./aws-waf.md), [Security Hub and GuardDuty](./aws-findings.md)) are simpler: there is no Firehose stream to maintain and no Firehose cost.

## How it works

```
web ACL (WAF) ───────────────────────────────┐
log group ─► CloudWatch Logs subscription ───┤
EventBridge (GuardDuty, Security Hub, API) ──┼─► Firehose stream ─► HTTP endpoint ─► CentralOps
                                             │      (buffer, gzip, retry, backup S3)
                                             ┘
```

Each log type has its own **stream** in the integration and its own **URL**:

| Stream | URL | What comes in | Event type | OCSF class |
|---|---|---|---|---|
| `waf` | `/api/ingest/firehose/waf` | web ACL logging straight to Firehose, or a subscribed WAF log group | `aws_firehose.waf` | 4002 HTTP Activity |
| `cloudtrail` | `/api/ingest/firehose/cloudtrail` | the trail's subscribed log group, or an EventBridge rule "AWS API Call via CloudTrail" | `aws_firehose.cloudtrail` | 6003 API Activity |
| `guardduty` | `/api/ingest/firehose/guardduty` | EventBridge rule "GuardDuty Finding" | `aws_firehose.guardduty` | 2004 Detection Finding |
| `securityhub` | `/api/ingest/firehose/securityhub` | EventBridge rule "Security Hub Findings - Imported" (ASFF format) | `aws_firehose.securityhub` | 2004 Detection Finding |
| `cloudwatch_logs` | `/api/ingest/firehose/cloudwatch_logs` | any subscribed log group | `aws_firehose.cloudwatch_logs` | 0 Base Event |

The mappings are the same as in the pull integrations. When it receives the data, CentralOps undoes what the Firehose path adds:

- **base64 and gzip.** The Firehose record comes in base64. The CloudWatch Logs subscription also compresses it with gzip, and CentralOps decompresses it. The Firehose decompression option can stay off.
- **CloudWatch Logs envelope.** Each `logEvent` becomes an event. The control message CloudWatch sends when the subscription is created is dropped.
- **EventBridge envelope.** Only the `detail` comes in: the GuardDuty finding, each item of the Security Hub `findings`, the CloudTrail record.
- **GuardDuty format.** In EventBridge the finding comes in `camelCase` (`accountId`), in the API in `PascalCase` (`AccountId`). CentralOps converts it, and the same mapping works for both.
- **WAF origin.** The `centralops` block gets `source: firehose`, the account and region taken from the `webaclId`, and the name of the Firehose stream.

## Prerequisites

- **CentralOps reachable from the internet over HTTPS, on port 443.** Firehose only delivers to this kind of address.
- **In the AWS account, permission to create** the Firehose stream, an S3 backup bucket and the IAM roles AWS uses to deliver to the stream. The console creates these roles on its own; the CLI needs them ready (see [Roles AWS uses](#papéis-que-a-aws-usa)).
- **No CentralOps access to the account.** Here it is AWS that sends: there is no CentralOps role and no External ID.

## Setup

The path is always the same: **(1)** create the integration and get the token, **(2)** create a Firehose stream that delivers to CentralOps, **(3)** send the log to that stream.

### 1. Create the integration and get the token

1. In CentralOps, go to **Collect -> Integrations** and add **AWS via Firehose (push)**. There is no AWS credential to enter.
2. Open the integration. In the **Push ingestion** panel, click **Issue token** and copy the token right away: it appears only once.
3. The same panel lists the **URL for each log type** (`…/api/ingest/firehose/waf`, `…/cloudtrail` and the others). You use one URL per stream.

A single token works for all of the integration's streams. If it leaks, click **Rotate token** and update the access key of the streams.

### 2. Create the Firehose stream

Create **one stream for each log type**. In the console:

1. Open **Amazon Data Firehose** and click **Create Firehose stream**.
2. Under **Source**, choose **Direct PUT**. Under **Destination**, choose **HTTP Endpoint**.
3. Under **Firehose stream name**, enter a name. **For WAF, the name must start with `aws-waf-logs-`** (e.g. `aws-waf-logs-centralops`).
4. Under **Destination settings**, fill in:

   | Field | Value |
   |---|---|
   | HTTP endpoint name | `CentralOps` |
   | HTTP endpoint URL | the URL for the log type, copied from the **Push ingestion** panel |
   | Authentication | **Use access key**, and paste the token. (Or **Use AWS Secrets Manager**, with a secret in the format `{"api_key": "<token>"}`.) |
   | Content encoding | **GZIP** |
   | Retry duration | `3600` seconds (the maximum is 7200) |
   | Buffer hints | **Buffer size** from 1 to 5 MiB and **Buffer interval** of 60 seconds |

5. Under **Backup settings**, choose **Failed data only** and an S3 bucket of yours (or create one right there). This bucket keeps whatever CentralOps could not receive after the retry runs out. Nothing is lost.
6. Under **Advanced settings → Service access**, leave **Create or update IAM role**. The console creates the role Firehose uses to write to the backup bucket.
7. Click **Create Firehose stream** and wait for the **Active** status.

With the AWS CLI, the WAF stream looks like this. The integration panel shows the same command, with the URL already filled in:

```bash
aws firehose create-delivery-stream \
  --delivery-stream-name aws-waf-logs-centralops \
  --delivery-stream-type DirectPut \
  --http-endpoint-destination-configuration '{
    "EndpointConfiguration": {"Url": "https://<host>/api/ingest/firehose/waf", "Name": "CentralOps", "AccessKey": "<token>"},
    "RequestConfiguration": {"ContentEncoding": "GZIP"},
    "BufferingHints": {"SizeInMBs": 1, "IntervalInSeconds": 60},
    "RetryOptions": {"DurationInSeconds": 3600},
    "S3BackupMode": "FailedDataOnly",
    "RoleARN": "arn:aws:iam::<account>:role/<firehose-role>",
    "S3Configuration": {
      "RoleARN": "arn:aws:iam::<account>:role/<firehose-role>",
      "BucketARN": "arn:aws:s3:::<bucket-de-backup>"
    }
  }'
```

### 3. Send the log to the stream

**WAF** (stream `waf`). The stream must be in the same account and the same region as the web ACL. For CloudFront, use `us-east-1`.

1. Open **WAF & Shield → Web ACLs**, choose the region and the web ACL.
2. On the **Logging and metrics** tab, click **Enable** (or **Edit**) under **Logging**.
3. Under **Logging destination**, choose **Amazon Data Firehose stream** and select the `aws-waf-logs-…` stream.
4. Save. WAF creates the `AWSServiceRoleForWAFV2Logging` service role on its own. For that, whoever saves needs the `iam:CreateServiceLinkedRole` permission.

With the CLI:

```bash
aws wafv2 put-logging-configuration --logging-configuration \
  ResourceArn=<arn-da-web-acl>,LogDestinationConfigs=<arn-do-stream>
```

**CloudTrail** (stream `cloudtrail`). CloudTrail does not write straight to Firehose: it needs to send the events to a CloudWatch Logs log group, which passes them on to the stream.

1. Open **CloudTrail → Trails → your trail**. Under **CloudWatch Logs**, click **Edit**, turn on **Enabled** and let the console create the log group and the role. Save.
2. Open **CloudWatch → Log groups** and select the trail's log group.
3. Under **Actions → Subscription filters**, choose **Create Amazon Data Firehose subscription filter**.
4. Select the Firehose stream. Under **Grant permission**, choose the role that lets CloudWatch Logs write to the stream (see [Roles AWS uses](#papéis-que-a-aws-usa)).
5. Leave **Subscription filter pattern** empty (it sends everything). Give the filter a name and click **Start streaming**.

Another option is an EventBridge rule with the pattern `{ "detail-type": ["AWS API Call via CloudTrail"] }` and the stream as the target, as in GuardDuty below. It only receives write events (read calls do not go through EventBridge).

**Any other log group** (stream `cloudwatch_logs`). Steps 2 to 5 of CloudTrail, on the log group you want.

With the CLI, a log group subscription looks like this:

```bash
aws logs put-subscription-filter \
  --log-group-name <log-group> \
  --filter-name centralops \
  --filter-pattern "" \
  --destination-arn <arn-do-stream> \
  --role-arn arn:aws:iam::<account>:role/<cloudwatch-logs-role>
```

**GuardDuty and Security Hub** (streams `guardduty` and `securityhub`). The findings go through EventBridge:

1. Open **Amazon EventBridge → Rules** and click **Create rule**. Choose **Rule with an event pattern**.
2. Under **Event pattern**, choose **Custom pattern (JSON editor)** and paste the service's pattern:

   ```json
   { "source": ["aws.guardduty"], "detail-type": ["GuardDuty Finding"] }
   ```

   ```json
   { "source": ["aws.securityhub"], "detail-type": ["Security Hub Findings - Imported"] }
   ```

3. Under **Target**, choose **AWS service → Firehose stream** and select the service's stream.
4. Under **Execution role**, leave **Create a new role for this specific resource**. Create the rule.

Create the rule in each region where the service is turned on. With Security Hub cross-Region aggregation, the aggregation region is enough.

### 4. Check

- After 1 to 2 minutes, the events show up in **Collect -> Collectors**, in the integration's stream.
- In Firehose, the stream's **Monitoring** tab shows the deliveries. If something fails, the **Destination error logs** tab shows the CentralOps message (for example, invalid token or batch too large).

### Roles AWS uses {#papéis-que-a-aws-usa}

The console creates the roles below on its own. With the CLI, create them first. None of them gives CentralOps access: they are AWS's own permissions to deliver the logs to the stream.

| Who writes | Trusts | Permissions |
|---|---|---|
| Firehose, to the backup bucket | `firehose.amazonaws.com` | `s3:AbortMultipartUpload`, `s3:GetBucketLocation`, `s3:GetObject`, `s3:ListBucket`, `s3:ListBucketMultipartUploads` and `s3:PutObject` on the backup bucket |
| CloudWatch Logs, to the stream | `logs.amazonaws.com` | `firehose:PutRecord` and `firehose:PutRecordBatch` on the stream |
| EventBridge, to the stream | `events.amazonaws.com` | `firehose:PutRecord` and `firehose:PutRecordBatch` on the stream |
| WAF, to the stream | service role `AWSServiceRoleForWAFV2Logging` | created by WAF |

Example of the CloudWatch Logs role (change the region, account and stream):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "logs.amazonaws.com" },
      "Action": "sts:AssumeRole",
      "Condition": { "StringLike": { "aws:SourceArn": "arn:aws:logs:us-east-1:111122223333:*" } }
    }
  ]
}
```

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["firehose:PutRecord", "firehose:PutRecordBatch"],
      "Resource": "arn:aws:firehose:us-east-1:111122223333:deliverystream/centralops-cloudtrail"
    }
  ]
}
```

The first document is the role's *trust policy*. The second is the permission.

## Delivery, retry and what happens on a failure

CentralOps only answers 200 after the events are written to the data-plane. Otherwise, Firehose retries the same batch until the **retry duration** runs out and then writes the batch to the backup bucket. Nothing is lost silently.

| Situation | Response | What Firehose does |
|---|---|---|
| Batch written | 200 | counts it as delivered |
| Wrong or revoked token | 401 | retries and then writes to the backup |
| Stream the integration does not have | 404 | retries and then writes to the backup |
| Data-plane (raw topic) down | 503 | retries until the data-plane is back |
| Batch too large (body above 16 MiB, more than 64 MiB decompressed, or more than 50,000 events) | 500 with the instruction to lower the buffer | retries and then writes to the backup |

CentralOps **never answers 413** to Firehose. For Firehose, 413 is a permanent failure and the batch does **not** go to the backup. If the "lote grande demais" (batch too large) message shows up in the stream's error logs, lower the **buffer size**.

A record that does not decode (invalid base64, corrupted gzip, text that is not JSON in a stream that expects JSON) does not get better on retry. It is counted in `collector_ingest_malformed_total` (reason `parse`), reported in the log and skipped, and the rest of the batch goes on.

## Deduplication

Firehose may deliver a batch again: on retry, or when the 200 gets lost on the network. Each event uses the native AWS id as its deduplication key:

| Stream | Key |
|---|---|
| `waf` | `webaclId` + `requestId` + `timestamp` |
| `cloudtrail` | `eventID` |
| `guardduty`, `securityhub` | `Id` + `UpdatedAt` (each update of the finding is an event) |
| `cloudwatch_logs` | `eventId` |

Without a native id, the key is the hash of the event's content.

## Limitations

- **New Security Hub (OCSF).** Only the ASFF format comes in ("Security Hub Findings - Imported"). The findings the new Security Hub already delivers in OCSF do not match the mapping and go to quarantine.
- **No connection test.** The integration has no credential to test. It is working when the events arrive. In Firehose, the stream's delivery metrics and the destination error logs show each rejection with the CentralOps message.
- **Region of CloudWatch log events.** The region is the one of the Firehose stream, because the CloudWatch Logs envelope does not include it.
