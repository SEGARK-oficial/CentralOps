---
sidebar_position: 7
title: AWS Security Hub e GuardDuty
description: Colete os achados de segurança da AWS (Security Hub em ASFF e GuardDuty) como OCSF Detection Finding
---

# AWS Security Hub e GuardDuty

Duas integrações para trazer os **achados de segurança** da AWS para o CentralOps, normalizados como OCSF 1.8 **Detection Finding (2004)**.

| Integração | O que traz | Quando usar |
|---|---|---|
| **AWS Security Hub** | Achados agregados em ASFF: GuardDuty, Inspector, Macie, IAM Access Analyzer, Config e os controles do próprio Security Hub (CSPM) | A conta usa o Security Hub. É a escolha padrão: uma integração, todos os produtos |
| **Amazon GuardDuty** | Achados de ameaça direto da API do GuardDuty (contas, EC2, S3, EKS, RDS, Lambda) | A conta usa o GuardDuty sem o Security Hub |

Com os dois ligados, os achados do GuardDuty chegam duas vezes (pelo Security Hub e direto). Escolha um.

## Como funciona

| Item | Security Hub | GuardDuty |
|---|---|---|
| Stream | `findings` (tipo `aws_securityhub.finding`) | `findings` (tipo `aws_guardduty.finding`) |
| API | `GetFindings`, filtro `UpdatedAt`, 100 por página | `ListDetectors` → `ListFindings` (filtro `updatedAt`, 50 IDs) → `GetFindings` |
| Frequência | a cada 5 minutos | a cada 5 minutos |
| Regiões | lista ou `*` (as habilitadas na conta, via `ec2:DescribeRegions`) | lista ou `*`; região sem GuardDuty é pulada |
| Primeira coleta | achados atualizados nas últimas 24 horas | idem |
| Posição da coleta | por região: o `UpdatedAt` mais novo lido | idem |
| Teto por ciclo | 50 páginas, somando as regiões; a região corrente grava a posição exata | idem |
| Duplicados | `Id` + `UpdatedAt` | idem |

**Cada atualização de um achado é um evento.** O GuardDuty soma ocorrências num achado existente e o Security Hub muda o workflow ou arquiva. Por isso o CentralOps trata `Id` + `UpdatedAt` como identidade: a mesma ameaça que se repete gera um evento novo, com `activity_id` **Update**.

**Achado indexado com atraso não se perde.** Cada consulta começa 15 minutos antes da posição salva, e a posição nunca passa do fim da janela menos 15 minutos. O que for relido é descartado pelo dedupe.

**Security Hub com agregação entre regiões.** Se a *cross-Region aggregation* está ligada, a região de agregação devolve os achados de todas. Informe só ela; listar as outras traria duplicados.

## Pré-requisitos

- O serviço habilitado na conta e nas regiões que você vai listar.
- Leitura pelo **papel cross-account** do CentralOps. O template do painel **Acesso por papel AWS** já inclui `securityhub:GetFindings`, `guardduty:ListDetectors`, `guardduty:ListFindings`, `guardduty:GetFindings` e `ec2:DescribeRegions`. O passo a passo do papel está em [AWS CloudTrail: Opção A](./aws-cloudtrail.md#opção-a-recomendada-papel-cross-account).

### Várias contas de uma AWS Organization

O mesmo template serve como **StackSet**: o External ID é por organização do CentralOps, não por conta AWS, então todas as contas de um cliente usam o mesmo papel com o mesmo External ID. Na conta de gerenciamento (ou administradora delegada do CloudFormation):

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

O papel é global (IAM), então uma região basta. Com `auto-deployment`, contas novas da Organization ganham o papel sozinhas. A conta de gerenciamento não recebe instância de um StackSet *service-managed*; crie a stack nela à parte, se for coletar dela.

Hoje é uma integração do CentralOps por conta AWS. Com o Security Hub ou o GuardDuty configurados com **conta administradora delegada**, uma única integração na conta administradora já recebe os achados de todas as contas membro.

## Criar a integração

1. Vá em **Coleta -> Integrações** e adicione **AWS Security Hub** ou **Amazon GuardDuty**.
2. Preencha:

   | Campo | O que informar |
   |---|---|
   | ARN do papel ou Access Key ID | O `RoleArn` da stack (ex.: `arn:aws:iam::111122223333:role/CentralOpsReadOnly`). |
   | Secret Access Key | Só com Access Key ID. |
   | Regiões AWS | Security Hub com agregação: só a região de agregação. Sem agregação, ou no GuardDuty: lista (`us-east-1, sa-east-1`) ou `*`. |

3. **Testar conexão.** Security Hub: lê 1 achado da primeira região (`InvalidAccessException` = o Security Hub não está habilitado ali). GuardDuty: procura o detector da primeira região.
4. Salve.

## O que é coletado

### Security Hub (ASFF)

| Campo OCSF | Origem |
|---|---|
| `time`, `finding_info.modified_time` | `UpdatedAt` |
| `finding_info.created_time` / `first_seen_time` / `last_seen_time` | `CreatedAt` / `FirstObservedAt` / `LastObservedAt` |
| `finding_info.uid` / `.title` / `.desc` / `.types` | `Id` / `Title` / `Description` / `Types` |
| `finding_info.product.name`, `metadata.product.name` | `ProductName` (GuardDuty, Inspector, Security Hub…) |
| `finding_info.analytic.uid` | `GeneratorId` (a regra ou o controle) |
| `severity_id` | `Severity.Label`: INFORMATIONAL 1, LOW 2, MEDIUM 3, HIGH 4, CRITICAL 5 |
| `status_id` | `Workflow.Status`: NEW 1, NOTIFIED 2, SUPPRESSED 3, RESOLVED 4; `RecordState` ARCHIVED 5 |
| `activity_id` | Create (nunca atualizado), Update, Close (ARCHIVED) |
| `resources[]` | `Resources[]` (`Type`, `Id`, `Region`) |
| `remediation.desc` / `.references` | `Remediation.Recommendation.Text` / `.Url` |
| `cloud.account.uid` / `cloud.region` | `AwsAccountId` / `Region` |
| `unmapped.aws_compliance_status` | `Compliance.Status` (PASSED, FAILED…) |

Achados **INFORMATIONAL** entram com `is_alert=false`: os controles CSPM geram muitos achados `PASSED`. Descarte ou amostre na rota, se quiser.

### GuardDuty

| Campo OCSF | Origem |
|---|---|
| `severity_id` | `Severity`: 1.0–3.9 Low, 4.0–6.9 Medium, 7.0–8.9 High, 9.0+ Critical |
| `finding_info.types` | `Type` (ex.: `UnauthorizedAccess:IAMUser/TorIPCaller`) |
| `finding_info.first_seen_time` / `last_seen_time`, `count` | `Service.EventFirstSeen` / `EventLastSeen`, `Service.Count` |
| `status_id`, `activity_id`, `is_alert` | `Service.Archived`: arquivado = Archived, Close, sem alerta |
| `src_endpoint.ip` / `.location.country` | IP remoto da ação (chamada de API, conexão, port probe, login RDS, Kubernetes) |
| `actor.user.name` / `.type` | `Resource.AccessKeyDetails.UserName` / `UserType` |
| `resources[]` | `Resource.ResourceType` + ID da instância, chave, bucket, cluster EKS, banco RDS ou função Lambda |
| `unmapped.aws_guardduty_api` | `Service.Action.AwsApiCallAction.Api` |

O achado completo fica no `raw`.

## Solução de problemas

**`InvalidAccessException` no Security Hub.** O serviço não está habilitado na região consultada, ou a conta não é a administradora.

**GuardDuty: `Credencial OK, mas o GuardDuty não está habilitado em <região>`.** Não há detector nessa região. Na coleta, a região é pulada sem erro.

**Achados duplicados.** O Security Hub e o GuardDuty estão os dois ligados para a mesma conta, ou o Security Hub tem agregação e você listou várias regiões.

## Limitações conhecidas

- **Uma integração por conta AWS.** O CentralOps ainda não cria integrações para as contas de uma Organization sozinho; use a conta administradora delegada do serviço ou uma integração por conta.
- **Security Hub em ASFF.** O formato OCSF nativo do novo Security Hub (`GetFindingsV2`) não é usado.
- **Sem atualização de volta.** O CentralOps não muda o workflow nem arquiva achados na AWS.
