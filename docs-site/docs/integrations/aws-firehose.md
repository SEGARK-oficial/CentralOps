---
sidebar_position: 8
title: AWS via Firehose (push)
description: Receba WAF, CloudTrail, GuardDuty, Security Hub e CloudWatch Logs em quase tempo real, empurrados pela própria AWS por um stream do Amazon Data Firehose
---

# AWS via Firehose (push)

As outras integrações AWS **buscam** os logs: leem o bucket S3 ou chamam a API a cada ciclo. Esta integração **recebe**: a própria AWS empurra os logs para o CentralOps por um stream do **Amazon Data Firehose** com destino **HTTP endpoint**. O atraso cai do ciclo de coleta mais a entrega no bucket (5 a 15 minutos no CloudTrail) para o buffer do Firehose (60 segundos ou menos).

## Quando usar

- **Latência importa.** Detecção sobre o WAF ou sobre o CloudTrail que não pode esperar 15 minutos.
- **Sem acesso de leitura à conta.** O cliente não precisa criar papel nem liberar bucket: ele só configura o Firehose apontando para o CentralOps.
- **O log já passa pelo Firehose.** WAF com destino Firehose, assinaturas de log group do CloudWatch Logs e regras do EventBridge.

Quando a latência não importa e você já tem o papel de leitura, as integrações de pull ([CloudTrail](./aws-cloudtrail.md), [WAF](./aws-waf.md), [Security Hub e GuardDuty](./aws-findings.md)) são mais simples: não há stream do Firehose para manter nem custo de Firehose.

## Como funciona

```
web ACL (WAF) ───────────────────────────────┐
log group ─► assinatura do CloudWatch Logs ──┤
EventBridge (GuardDuty, Security Hub, API) ──┼─► stream do Firehose ─► HTTP endpoint ─► CentralOps
                                             │      (buffer, gzip, retry, backup S3)
                                             ┘
```

Cada tipo de log tem o seu **stream** na integração e a sua **URL**:

| Stream | URL | O que entra | Tipo de evento | Classe OCSF |
|---|---|---|---|---|
| `waf` | `/api/ingest/firehose/waf` | logging da web ACL direto no Firehose, ou um log group do WAF assinado | `aws_firehose.waf` | 4002 HTTP Activity |
| `cloudtrail` | `/api/ingest/firehose/cloudtrail` | log group da trail assinado, ou regra do EventBridge "AWS API Call via CloudTrail" | `aws_firehose.cloudtrail` | 6003 API Activity |
| `guardduty` | `/api/ingest/firehose/guardduty` | regra do EventBridge "GuardDuty Finding" | `aws_firehose.guardduty` | 2004 Detection Finding |
| `securityhub` | `/api/ingest/firehose/securityhub` | regra do EventBridge "Security Hub Findings - Imported" (formato ASFF) | `aws_firehose.securityhub` | 2004 Detection Finding |
| `cloudwatch_logs` | `/api/ingest/firehose/cloudwatch_logs` | qualquer log group assinado | `aws_firehose.cloudwatch_logs` | 0 Base Event |

Os mappings são os mesmos das integrações de pull. O CentralOps desfaz, no recebimento, o que o caminho pelo Firehose acrescenta:

- **base64 e gzip.** O registro do Firehose vem em base64. A assinatura do CloudWatch Logs ainda o compacta em gzip, e o CentralOps descompacta. A opção de descompressão do Firehose pode ficar desligada.
- **Envelope do CloudWatch Logs.** Cada `logEvent` vira um evento. A mensagem de controle que o CloudWatch manda ao criar a assinatura é descartada.
- **Envelope do EventBridge.** Entra só o `detail`: o achado do GuardDuty, cada item de `findings` do Security Hub, o registro do CloudTrail.
- **Formato do GuardDuty.** No EventBridge o achado vem em `camelCase` (`accountId`), na API em `PascalCase` (`AccountId`). O CentralOps converte, e o mesmo mapping serve aos dois.
- **Origem do WAF.** O bloco `centralops` recebe `source: firehose`, a conta e a região tiradas do `webaclId` e o nome do stream do Firehose.

## Pré-requisitos

- O CentralOps acessível pela internet em **HTTPS na porta 443**, que é o único destino que o Firehose aceita.
- Na conta AWS, permissão para criar o stream do Firehose, o papel que ele assume e o bucket de backup.

## Configuração

### 1. Criar a integração e emitir o token

1. Em **Integrações**, adicione **AWS via Firehose (push)**. Não há credencial AWS para informar.
2. No painel **Ingestão** da integração, clique em **Emitir token** e copie o token. Ele aparece uma vez só.
3. O painel lista a URL de cada stream.

### 2. Criar o stream do Firehose

Crie um stream por tipo de log, com destino **HTTP endpoint**:

| Campo | Valor |
|---|---|
| Source | **Direct PUT** |
| HTTP endpoint URL | a URL do stream, copiada do painel |
| Access key | o token de ingestão (ou um segredo do Secrets Manager no formato `{"api_key": "<token>"}`) |
| Content encoding | **GZIP** |
| Retry duration | 3600 segundos (o máximo é 7200) |
| Buffer hints | **1 a 5 MiB** e 60 segundos |
| S3 backup | **Failed data only**, num bucket seu |

Pela AWS CLI, o stream do WAF fica assim. O painel da integração mostra o mesmo comando já com a URL:

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
    "RoleARN": "arn:aws:iam::<conta>:role/<papel-do-firehose>",
    "S3Configuration": {
      "RoleARN": "arn:aws:iam::<conta>:role/<papel-do-firehose>",
      "BucketARN": "arn:aws:s3:::<bucket-de-backup>"
    }
  }'
```

### 3. Ligar a origem ao stream

**WAF.** O nome do stream precisa começar com `aws-waf-logs-`, e o stream precisa estar na mesma conta e na mesma região da web ACL (para CloudFront, `us-east-1`):

```bash
aws wafv2 put-logging-configuration --logging-configuration \
  ResourceArn=<arn-da-web-acl>,LogDestinationConfigs=<arn-do-stream>
```

**CloudTrail ou qualquer log group.** Crie uma assinatura do log group para o stream. O CloudWatch Logs precisa de um papel que ele assuma para gravar no Firehose:

```bash
aws logs put-subscription-filter \
  --log-group-name <log-group-da-trail> \
  --filter-name centralops \
  --filter-pattern "" \
  --destination-arn <arn-do-stream> \
  --role-arn arn:aws:iam::<conta>:role/<papel-do-cloudwatch-logs>
```

**GuardDuty e Security Hub.** Crie uma regra do EventBridge com o stream como alvo:

```json
{ "source": ["aws.guardduty"], "detail-type": ["GuardDuty Finding"] }
```

```json
{ "source": ["aws.securityhub"], "detail-type": ["Security Hub Findings - Imported"] }
```

## Entrega, retry e o que acontece numa falha

O CentralOps só responde 200 depois que os eventos estão gravados no data-plane. Fora isso, o Firehose re-tenta o mesmo lote até acabar o **retry duration** e então grava o lote no bucket de backup. Nada se perde em silêncio.

| Situação | Resposta | O que o Firehose faz |
|---|---|---|
| Lote gravado | 200 | conta como entregue |
| Token errado ou revogado | 401 | re-tenta e depois grava no backup |
| Stream que a integração não tem | 404 | re-tenta e depois grava no backup |
| Data-plane fora do ar ou buffer cheio | 503 | re-tenta até o data-plane voltar |
| Lote grande demais (corpo acima de 16 MiB, mais de 64 MiB descompactado, ou mais de 50.000 eventos) | 500 com a instrução de baixar o buffer | re-tenta e depois grava no backup |

O CentralOps **nunca responde 413** ao Firehose. Para o Firehose, 413 é falha permanente e o lote **não** vai para o backup. Se a mensagem "lote grande demais" aparecer nos logs de erro do stream, baixe o **buffer size**.

Um registro que não decodifica (base64 inválido, gzip corrompido, texto que não é JSON num stream que espera JSON) não melhora no retry. Ele é contado em `collector_ingest_malformed_total` (motivo `parse`), avisado no log e pulado, e o resto do lote segue.

## Deduplicação

O Firehose pode reentregar um lote: no retry, ou quando o 200 se perde na rede. Cada evento usa o id nativo da AWS como chave de deduplicação:

| Stream | Chave |
|---|---|
| `waf` | `webaclId` + `requestId` + `timestamp` |
| `cloudtrail` | `eventID` |
| `guardduty`, `securityhub` | `Id` + `UpdatedAt` (cada atualização do achado é um evento) |
| `cloudwatch_logs` | `eventId` |

Sem id nativo, a chave é o hash do conteúdo do evento.

## Limitações

- **Security Hub novo (OCSF).** Só entra o formato ASFF ("Security Hub Findings - Imported"). Os achados que o Security Hub novo já entrega em OCSF não casam com o mapping e vão para a quarentena.
- **Sem teste de conexão.** A integração não tem credencial para testar. Ela está funcionando quando os eventos chegam, e a profundidade do buffer aparece no painel de ingestão.
- **Região dos log events do CloudWatch.** A região é a do stream do Firehose, porque o envelope do CloudWatch Logs não a informa.
