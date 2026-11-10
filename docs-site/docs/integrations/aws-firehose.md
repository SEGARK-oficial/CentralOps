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

- **O CentralOps acessível pela internet em HTTPS, na porta 443.** O Firehose só entrega nesse tipo de endereço.
- **Na conta AWS, permissão para criar** o stream do Firehose, um bucket S3 de backup e os papéis IAM que a AWS usa para entregar no stream. O console cria esses papéis sozinho; a CLI precisa deles prontos (veja [Papéis que a AWS usa](#papéis-que-a-aws-usa)).
- **Nenhum acesso do CentralOps à conta.** Aqui é a AWS que envia: não há papel do CentralOps nem External ID.

## Configuração

O caminho é sempre o mesmo: **(1)** criar a integração e pegar o token, **(2)** criar um stream do Firehose que entrega no CentralOps, **(3)** mandar o log para esse stream.

### 1. Criar a integração e pegar o token

1. No CentralOps, vá em **Coleta -> Integrações** e adicione **AWS via Firehose (push)**. Não há credencial AWS para informar.
2. Abra a integração. No painel **Ingestão push**, clique em **Emitir token** e copie o token na hora: ele aparece uma vez só.
3. O mesmo painel lista a **URL de cada tipo de log** (`…/api/ingest/firehose/waf`, `…/cloudtrail` e as outras). Você usa uma URL por stream.

Um token só serve para todos os streams da integração. Se ele vazar, clique em **Rotacionar token** e atualize a access key dos streams.

### 2. Criar o stream do Firehose

Crie **um stream para cada tipo de log**. Pelo console:

1. Abra **Amazon Data Firehose** e clique em **Create Firehose stream**.
2. Em **Source**, escolha **Direct PUT**. Em **Destination**, escolha **HTTP Endpoint**.
3. Em **Firehose stream name**, dê um nome. **Para o WAF, o nome precisa começar com `aws-waf-logs-`** (ex.: `aws-waf-logs-centralops`).
4. Em **Destination settings**, preencha:

   | Campo | Valor |
   |---|---|
   | HTTP endpoint name | `CentralOps` |
   | HTTP endpoint URL | a URL do tipo de log, copiada do painel **Ingestão push** |
   | Authentication | **Use access key**, e cole o token. (Ou **Use AWS Secrets Manager**, com um segredo no formato `{"api_key": "<token>"}`.) |
   | Content encoding | **GZIP** |
   | Retry duration | `3600` segundos (o máximo é 7200) |
   | Buffer hints | **Buffer size** de 1 a 5 MiB e **Buffer interval** de 60 segundos |

5. Em **Backup settings**, escolha **Failed data only** e um bucket S3 seu (ou crie um ali mesmo). Esse bucket guarda o que o CentralOps não conseguir receber depois de esgotar o retry. Nada se perde.
6. Em **Advanced settings → Service access**, deixe **Create or update IAM role**. O console cria o papel que o Firehose usa para gravar no bucket de backup.
7. Clique em **Create Firehose stream** e espere o status **Active**.

Pela AWS CLI, o stream do WAF fica assim. O painel da integração mostra o mesmo comando, já com a URL:

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

### 3. Mandar o log para o stream

**WAF** (stream `waf`). O stream precisa estar na mesma conta e na mesma região da web ACL. Para CloudFront, use `us-east-1`.

1. Abra **WAF & Shield → Web ACLs**, escolha a região e a web ACL.
2. Na aba **Logging and metrics**, clique em **Enable** (ou **Edit**) em **Logging**.
3. Em **Logging destination**, escolha **Amazon Data Firehose stream** e selecione o stream `aws-waf-logs-…`.
4. Salve. O WAF cria sozinho o papel de serviço `AWSServiceRoleForWAFV2Logging`. Para isso, quem salva precisa da permissão `iam:CreateServiceLinkedRole`.

Pela CLI:

```bash
aws wafv2 put-logging-configuration --logging-configuration \
  ResourceArn=<arn-da-web-acl>,LogDestinationConfigs=<arn-do-stream>
```

**CloudTrail** (stream `cloudtrail`). O CloudTrail não grava direto no Firehose: ele precisa mandar os eventos para um log group do CloudWatch Logs, que repassa ao stream.

1. Abra **CloudTrail → Trails → sua trail**. Em **CloudWatch Logs**, clique em **Edit**, ligue **Enabled** e deixe o console criar o log group e o papel. Salve.
2. Abra **CloudWatch → Log groups** e marque o log group da trail.
3. Em **Actions → Subscription filters**, escolha **Create Amazon Data Firehose subscription filter**.
4. Selecione o stream do Firehose. Em **Grant permission**, escolha o papel que deixa o CloudWatch Logs gravar no stream (veja [Papéis que a AWS usa](#papéis-que-a-aws-usa)).
5. Em **Subscription filter pattern**, deixe vazio (manda tudo). Dê um nome ao filtro e clique em **Start streaming**.

Outra opção é uma regra do EventBridge com o padrão `{ "detail-type": ["AWS API Call via CloudTrail"] }` e o stream como alvo, como no GuardDuty abaixo. Ela só recebe os eventos de escrita (as chamadas de leitura não passam pelo EventBridge).

**Qualquer outro log group** (stream `cloudwatch_logs`). Os passos 2 a 5 do CloudTrail, no log group que você quer.

Pela CLI, a assinatura de um log group fica assim:

```bash
aws logs put-subscription-filter \
  --log-group-name <log-group> \
  --filter-name centralops \
  --filter-pattern "" \
  --destination-arn <arn-do-stream> \
  --role-arn arn:aws:iam::<conta>:role/<papel-do-cloudwatch-logs>
```

**GuardDuty e Security Hub** (streams `guardduty` e `securityhub`). Os achados passam pelo EventBridge:

1. Abra **Amazon EventBridge → Rules** e clique em **Create rule**. Escolha **Rule with an event pattern**.
2. Em **Event pattern**, escolha **Custom pattern (JSON editor)** e cole o padrão do serviço:

   ```json
   { "source": ["aws.guardduty"], "detail-type": ["GuardDuty Finding"] }
   ```

   ```json
   { "source": ["aws.securityhub"], "detail-type": ["Security Hub Findings - Imported"] }
   ```

3. Em **Target**, escolha **AWS service → Firehose stream** e selecione o stream do serviço.
4. Em **Execution role**, deixe **Create a new role for this specific resource**. Crie a regra.

Crie a regra em cada região onde o serviço está ligado. Com a agregação entre regiões do Security Hub, basta a região de agregação.

### 4. Conferir

- Depois de 1 a 2 minutos, os eventos aparecem em **Coleta -> Coletores**, no stream da integração.
- No Firehose, a aba **Monitoring** do stream mostra as entregas. Se algo falhar, a aba **Destination error logs** mostra a mensagem do CentralOps (por exemplo, token inválido ou lote grande demais).

### Papéis que a AWS usa

O console cria os papéis abaixo sozinho. Pela CLI, crie-os antes. Nenhum deles dá acesso ao CentralOps: são permissões da própria AWS para entregar os logs no stream.

| Quem grava | Confia em | Permissões |
|---|---|---|
| O Firehose, no bucket de backup | `firehose.amazonaws.com` | `s3:AbortMultipartUpload`, `s3:GetBucketLocation`, `s3:GetObject`, `s3:ListBucket`, `s3:ListBucketMultipartUploads` e `s3:PutObject` no bucket de backup |
| O CloudWatch Logs, no stream | `logs.amazonaws.com` | `firehose:PutRecord` e `firehose:PutRecordBatch` no stream |
| O EventBridge, no stream | `events.amazonaws.com` | `firehose:PutRecord` e `firehose:PutRecordBatch` no stream |
| O WAF, no stream | papel de serviço `AWSServiceRoleForWAFV2Logging` | criado pelo WAF |

Exemplo do papel do CloudWatch Logs (troque região, conta e stream):

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

O primeiro documento é a *trust policy* do papel. O segundo é a permissão.

## Entrega, retry e o que acontece numa falha

O CentralOps só responde 200 depois que os eventos estão gravados no data-plane. Fora isso, o Firehose re-tenta o mesmo lote até acabar o **retry duration** e então grava o lote no bucket de backup. Nada se perde em silêncio.

| Situação | Resposta | O que o Firehose faz |
|---|---|---|
| Lote gravado | 200 | conta como entregue |
| Token errado ou revogado | 401 | re-tenta e depois grava no backup |
| Stream que a integração não tem | 404 | re-tenta e depois grava no backup |
| Data-plane (tópico raw) fora do ar | 503 | re-tenta até o data-plane voltar |
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
- **Sem teste de conexão.** A integração não tem credencial para testar. Ela está funcionando quando os eventos chegam. No Firehose, as métricas de entrega do stream e os logs de erro do destino mostram cada recusa com a mensagem do CentralOps.
- **Região dos log events do CloudWatch.** A região é a do stream do Firehose, porque o envelope do CloudWatch Logs não a informa.
