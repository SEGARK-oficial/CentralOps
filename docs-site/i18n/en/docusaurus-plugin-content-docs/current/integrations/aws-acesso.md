---
sidebar_position: 6
title: "AWS: give CentralOps access"
description: Step by step to create the service role that lets CentralOps read the logs in your AWS account, with the minimum permissions for each integration
---

# AWS: give CentralOps access

This page shows, step by step, how to let CentralOps read the logs of an AWS account. It applies to every AWS integration that reads logs: [CloudTrail](./aws-cloudtrail.md), [CloudWatch Logs](./aws-cloudwatch.md), [WAF](./aws-waf.md), [Security Hub and GuardDuty](./aws-findings.md). The [Firehose](./aws-firehose.md) integration works the other way around (AWS sends the logs) and does not use this access.

## In a nutshell

In your AWS account, you create a **role** (*IAM role*), which works like a read-only service account. CentralOps "puts on" this role when it goes to fetch the logs and receives a credential that is valid for 1 hour. No password or key is stored in CentralOps.

Two locks protect this access:

- **Only CentralOps can use the role.** The role trusts a single AWS identity: the one of the CentralOps server, called the **CentralOps principal** here.
- **Only your organization can use the role.** CentralOps also sends a code, the **External ID**, which is unique for each organization registered in it. If another CentralOps organization tries to use your role, AWS refuses, because the code does not match.

To cut off access at any time, delete the role (or the stack that created it). Collection stops on the next cycle.

## Before you start

You will need:

- **Administrator access to the AWS account**, or at least permission to create roles in IAM and stacks in CloudFormation.
- **Administrator access in CentralOps**, to create the integration.
- **The names of what will be read:** the CloudTrail bucket, the WAF bucket or log group, the CloudWatch log group. Each integration page tells you where to find these names.
- **If the logs are encrypted with a KMS key of your own:** the ARN of that key.

## Step 1: Get the External ID and the template in CentralOps

1. In CentralOps, go to **Collect -> Integrations** and click to add an AWS integration (for example, **AWS CloudTrail**).
2. Choose the **organization** that owns this AWS account.
3. The **AWS role access (recommended)** panel appears. It shows:
   - the organization's **External ID** (example: `centralops-42`);
   - the **CentralOps principal** (example: `arn:aws:iam::999999999999:role/centralops-server`);
   - the **Download template** button, a CloudFormation file that already comes with these two values filled in.
4. Click **Download template** and keep the `centralops-readonly-role.json` file.

Leave this screen open: you come back to it in Step 3.

:::info[The External ID does not change]

The External ID is created by CentralOps once for each organization and never changes. All the AWS accounts of the same customer can use the same role with the same External ID.

:::

## Step 2: Create the role in the AWS account

Choose one of the three paths. The result is the same: a role named `CentralOpsReadOnly`.

### Path A (easiest): in the console, with the template

1. Sign in to the AWS console, in the account the logs come from.
2. Open the **CloudFormation** service. The region does not matter, because the role applies to the whole account.
3. Click **Create stack** and then **With new resources (standard)**.
4. Under **Prerequisite - Prepare template**, leave **Choose an existing template**. Under **Specify template**, choose **Upload a template file**, select `centralops-readonly-role.json` and click **Next**.
5. In **Stack name**, type `centralops-readonly`.
6. Under **Parameters**, fill in only what you will use and leave the rest as it is:

   | Parameter | What to enter |
   |---|---|
   | `CloudTrailBucketName` | The name of the CloudTrail bucket (just the name, without `s3://`). Empty if you will not collect CloudTrail. |
   | `WafLogsBucketName` | The name of the WAF `aws-waf-logs-…` bucket. Empty if WAF does not write to S3. |
   | `KmsKeyArn` | The ARN of the KMS key that encrypts the logs, if there is one. Empty if there is no key of your own. |
   | `AllowListAccounts` | `true` only in the account used by the [AWS Organization mode](#várias-contas-de-uma-aws-organization). In the others, `false`. |
   | `ExternalId`, `TrustedPrincipalArn`, `RoleName` | Already filled in. **Do not change them.** |

7. Click **Next** twice.
8. At the bottom of the page, check **I acknowledge that AWS CloudFormation might create IAM resources with custom names** and click **Submit**.
9. Wait for the status to become **CREATE_COMPLETE**, which takes about 1 minute.
10. Open the **Outputs** tab and copy the value of **RoleArn** (example: `arn:aws:iam::111122223333:role/CentralOpsReadOnly`).

### Path B: with the AWS CLI, with the template

The CentralOps panel has the **Copy AWS CLI command** button with the command ready to use. It looks like this:

```bash
aws cloudformation deploy \
  --template-file centralops-readonly-role.json \
  --stack-name centralops-readonly \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides CloudTrailBucketName=meu-bucket-cloudtrail

aws cloudformation describe-stacks --stack-name centralops-readonly \
  --query "Stacks[0].Outputs[?OutputKey=='RoleArn'].OutputValue" --output text
```

The second command prints the `RoleArn`. To change a parameter later (for example, to add the WAF bucket), run `deploy` again with all the parameters you use.

### Path C: by hand, in the IAM console {#caminho-c-à-mão-no-console-do-iam}

Use this path if your company does not allow CloudFormation.

1. Open **IAM -> Roles** and click **Create role**.
2. Under **Trusted entity type**, choose **AWS account** and then **Another AWS account**.
3. In **Account ID**, type the 12 digits of the account shown in the **CentralOps principal** (in the example, `999999999999`).
4. Check **Require external ID** and paste the External ID from the panel. Click **Next**.
5. On the permissions screen, do not select anything and click **Next**. The permissions come in step 8.
6. In **Role name**, type `CentralOpsReadOnly` and click **Create role**.
7. Open the role you created, go to the **Trust relationships** tab, click **Edit trust policy** and replace the content with this one. This way only the exact CentralOps identity can use the role, and not the whole account:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Principal": { "AWS": "arn:aws:iam::999999999999:role/centralops-server" },
         "Action": "sts:AssumeRole",
         "Condition": { "StringEquals": { "sts:ExternalId": "centralops-42" } }
       }
     ]
   }
   ```

   Replace the ARN with the **CentralOps principal** and `centralops-42` with your External ID.

8. In the **Permissions** tab, click **Add permissions -> Create inline policy**, choose **JSON** and paste the policy for the integrations you will use, built from the [minimum permissions table](#permissões-mínimas-de-cada-integração).
9. In **Maximum session duration**, leave **1 hour**.
10. Copy the role's **ARN**, at the top of the page.

:::warning[Do not remove the External ID condition]

Without the `sts:ExternalId` condition, anyone who finds out your role's ARN can register it in another CentralOps organization and read your logs. CentralOps does not accept a typed-in External ID: it always uses the organization's own.

:::

## Step 3: Paste the ARN and test

1. Go back to the integration screen in CentralOps.
2. Paste the `RoleArn` into the **Role ARN or Access Key ID** field. Leave **Secret Access Key** empty: with a role, it is not used.
3. Fill in the other fields of the integration (bucket, regions and the rest). The page for each integration explains each field.
4. Click **Test connection**. CentralOps really uses the role and tries to read a sample. The expected response starts with `Conexão OK (papel assumido)`.
5. Click **Save**. Collection starts on the next cycle, within 5 minutes.

If the test fails, see the [common problems](#problemas-comuns).

## Minimum permissions for each integration {#permissões-mínimas-de-cada-integração}

The role only needs read access. The table shows exactly what each integration calls in AWS:

| Integration | Permissions | On which resource |
|---|---|---|
| CloudTrail | `s3:ListBucket` | the trail's bucket |
| | `s3:GetObject` | the files in the bucket (`arn:aws:s3:::bucket/*`) |
| CloudWatch Logs | `logs:FilterLogEvents` | the log group (`arn:aws:logs:region:account:log-group:name:*`) |
| WAF (logs in S3) | `s3:ListBucket` and `s3:GetObject` | the `aws-waf-logs-…` bucket |
| WAF (logs in CloudWatch) | `logs:FilterLogEvents` | the `aws-waf-logs-…` log group |
| Security Hub | `securityhub:GetFindings` | `*` |
| GuardDuty | `guardduty:ListDetectors`, `guardduty:ListFindings` and `guardduty:GetFindings` | `*` |
| Security Hub or GuardDuty with regions `*` | `ec2:DescribeRegions` (finds the regions enabled in the account) | `*` |
| Logs encrypted with KMS | `kms:Decrypt` | the KMS key |
| AWS Organization mode (parent) | `organizations:ListAccounts` | `*` |

The CentralOps template grants these permissions plus two more, also read-only for metadata: `s3:GetBucketLocation` (which region the bucket is in) and `logs:DescribeLogGroups` (lists the names of the log groups). No write permission is granted.

Example policy for someone who collects CloudTrail (with KMS) and GuardDuty. Replace the names with yours:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ListarOBucketDaTrail",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::meu-bucket-cloudtrail"
    },
    {
      "Sid": "LerOsArquivosDaTrail",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::meu-bucket-cloudtrail/*"
    },
    {
      "Sid": "DecifrarOsArquivos",
      "Effect": "Allow",
      "Action": "kms:Decrypt",
      "Resource": "arn:aws:kms:us-east-1:111122223333:key/ID-DA-CHAVE"
    },
    {
      "Sid": "LerAchadosDoGuardDuty",
      "Effect": "Allow",
      "Action": ["guardduty:ListDetectors", "guardduty:ListFindings", "guardduty:GetFindings"],
      "Resource": "*"
    }
  ]
}
```

### When the bucket or the key is in another account

It is common to keep the logs in a central account (*log archive*). In that case, the role's permission is not enough, and the bucket owner also needs to allow the access:

- **Bucket in another account:** add this statement to the *bucket policy* (S3 -> the bucket -> **Permissions** -> **Bucket policy**):

  ```json
  {
    "Sid": "CentralOpsLeitura",
    "Effect": "Allow",
    "Principal": { "AWS": "arn:aws:iam::111122223333:role/CentralOpsReadOnly" },
    "Action": ["s3:ListBucket", "s3:GetObject"],
    "Resource": ["arn:aws:s3:::meu-bucket-cloudtrail", "arn:aws:s3:::meu-bucket-cloudtrail/*"]
  }
  ```

- **KMS key in another account:** the key's *key policy* (KMS -> the key -> **Key policy**) needs to allow `kms:Decrypt` for the same role ARN.

Simpler: create the role in the central account itself, where the bucket and the key are.

## Multiple accounts in an AWS Organization {#várias-contas-de-uma-aws-organization}

If the customer has many AWS accounts, do not create the role one account at a time. Use a **StackSet**, which creates the same role in all the accounts at once and also in accounts created later.

1. Sign in to the Organization's **management account** (or to a CloudFormation delegated administrator).
2. Open **CloudFormation -> StackSets -> Create StackSet**.
3. Under **Permissions**, choose **Service-managed permissions**.
4. Upload the same `centralops-readonly-role.json` and fill in the parameters as in Path A.
5. Under **Deployment options**, choose the organizational units (or the root `r-…` for all accounts), turn on **Automatic deployment** and choose **one** region. The role applies to the whole account.
6. Finish. The management account does not receive the role through the StackSet: if you will collect from it, create a regular stack in it (Path A).

With the CLI:

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

With the role in every account, the **AWS Organization mode** (Enterprise edition) creates the integrations for you. You register only a "parent", and CentralOps lists the accounts and creates a "child" integration for each account you approve:

1. In the management account, or in an AWS Organizations delegated administrator, create the stack with `AllowListAccounts=true`. This role is the only one that needs to list the accounts.
2. In CentralOps, add the **AWS CloudTrail — AWS Organization**, **Amazon GuardDuty — AWS Organization** or **AWS Security Hub — AWS Organization** integration, and paste the ARN of this role.
3. **Test connection** confirms that the role lists the accounts.
4. Save. On the parent integration's page, the tenants panel shows the accounts found. Approve the ones you want to collect, or turn on automatic approval.
5. Each approved account becomes its own integration, which collects from all regions (`*`). To restrict this, edit the regions in the child integration.

How each child reaches its account:

- **GuardDuty and Security Hub:** the child uses the role with the **same name and path** in the member account, the one the StackSet created.
- **CloudTrail:** the child uses the **parent's role** and reads its account's part of the Organization trail bucket (`AWSLogs/o-…/<account>/`). That is why the parent's role also needs to read this bucket: set `CloudTrailBucketName` (and `KmsKeyArn`, if there is one) in the stack of the account where the bucket is, usually the *log archive* account.

An account that is suspended, or that leaves the Organization, has its child disabled on the next sync. In the Community edition, the parent is created but no account is imported.

## Self-hosted installation: the server identity {#instalação-própria-a-identidade-do-servidor}

In a self-hosted installation, the CentralOps server needs an AWS identity to use the customers' roles. It comes from the standard AWS chain: the `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` variables, an instance profile (EC2) or IRSA (EKS).

- This identity only needs the `sts:AssumeRole` permission on the customers' roles, for example `arn:aws:iam::*:role/CentralOpsReadOnly`.
- The panel discovers its ARN on its own.
- If the server's role has a path (`role/caminho/Nome`), set `AWS_TRUST_PRINCIPAL_ARN` to the full ARN.
- Without an identity, the panel warns **CentralOps AWS identity not found**.

## Access key (legacy)

CentralOps still accepts an **Access Key ID** and a **Secret Access Key** from an IAM user. It is a credential that does not expire and is stored (encrypted) in CentralOps. Use it only if the role is not possible.

1. In IAM, create a user just for CentralOps, without console access.
2. Attach a policy with the integration's [minimum permissions](#permissões-mínimas-de-cada-integração).
3. Create an access key (**Security credentials -> Create access key -> Third-party service**).
4. In CentralOps, paste the Access Key ID into the **Role ARN or Access Key ID** field and the secret key into **Secret Access Key**.

Temporary credentials (which start with `ASIA` and have a session token) are not accepted. The AWS Organization mode requires a role.

## Common problems {#problemas-comuns}

| The message says | What to do |
|---|---|
| `AssumeRole: AccessDenied` | The role's trust policy does not trust the CentralOps principal, or the External ID is different. Compare both with the **AWS role access** panel. |
| `CentralOps AWS identity not found` | Self-hosted installations only: the server has no AWS credential. See [Self-hosted installation](#instalação-própria-a-identidade-do-servidor). |
| `AccessDenied` when listing or reading the bucket | The role is missing `s3:ListBucket` or `s3:GetObject`, or the bucket policy in another account does not allow the role. |
| `KMS` or `kms:Decrypt` in the error | The files are encrypted. Set `KmsKeyArn` in the stack and, if the key belongs to another account, allow the role in the key policy. |
| `ResourceNotFoundException` | The log group does not exist in the region you entered. Check the exact name (with the slashes) and the region. |
| `InvalidAccessException` (Security Hub) | Security Hub is not enabled in that region. |
| `AWSOrganizationsNotInUse` or `AccessDenied` on `ListAccounts` | The Organization mode role is not in the management account (nor in a delegated administrator), or the stack was created with `AllowListAccounts=false`. |

## How to cut off access

- **From AWS:** delete the `centralops-readonly` stack (or the `CentralOpsReadOnly` role). Access ends right away; a credential already issued is valid for at most 1 hour.
- **From CentralOps:** disable or delete the integration.
