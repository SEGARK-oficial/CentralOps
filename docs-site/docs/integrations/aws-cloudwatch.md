---
sidebar_position: 7
title: AWS CloudWatch Logs
description: Colete os eventos de um log group do CloudWatch Logs (Lambda, ECS, API Gateway, RDS, aplicações) com FilterLogEvents
---

# AWS CloudWatch Logs

Conecte um **log group** do CloudWatch Logs ao CentralOps para coletar os logs que serviços e aplicações gravam ali: funções Lambda, tarefas ECS, API Gateway, logs exportados do RDS, appliances com o agente do CloudWatch. É a fonte "catch-all" da AWS. Para os eventos de API da própria AWS (IAM, console), use o [AWS CloudTrail](./aws-cloudtrail.md).

## Quando usar

- **Logs de aplicação na AWS.** Erros e acessos negados de uma API que roda em Lambda ou ECS.
- **Logs de serviços gerenciados.** API Gateway, logs de erro e auditoria exportados do RDS.
- **Fontes sem integração própria** que já gravam no CloudWatch por agente.

## Como funciona

```
serviço / aplicação ──► log group ──FilterLogEvents (a cada 5 min)──► CentralOps
```

| Item | Valor |
|---|---|
| Stream | `events` (tipo de evento `aws_cloudwatch.event`) |
| API | [`FilterLogEvents`](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_FilterLogEvents.html) em `logs.<região>.amazonaws.com`, ordem crescente de horário |
| Frequência | a cada 5 minutos |
| Escopo | **um log group por integração**, todos os log streams dele |
| Posição da coleta | horário do evento (ms) até onde a janela foi lida; cada janela nova recua 5 minutos para pegar evento que entrou atrasado |
| Duplicados | descartados pelo `eventId` (único por evento, garantido pela AWS) |
| Primeira coleta | eventos da última hora |
| Janela por ciclo | no máximo 6 horas. Um atraso maior é drenado em pedaços, ciclo a ciclo |
| Teto por ciclo | 20 páginas de 1.000 eventos (20.000 eventos). Ao bater o teto, o ciclo grava o `nextToken` e a janela, e o seguinte continua da mesma página |

A resposta do `FilterLogEvents` não traz log group, região, conta nem horário legível. O CentralOps acrescenta ao evento bruto os campos `logGroupName`, `awsRegion`, `awsAccountId` (quando informado) e `eventTime` (ISO 8601, convertido do `timestamp` em milissegundos).

## Quem pode fazer o quê

- **Administrador da plataforma:** cria e edita a integração (menu **Coleta -> Integrações**).
- **Na AWS:** criar o usuário e a policy exige permissão de IAM.

## Pré-requisitos

- O **nome exato do log group** (ex.: `/aws/lambda/faturamento-api`) e a **região** dele.
- Uma **access key de usuário IAM**. Assumir role, perfil de instância e credenciais temporárias com session token não são suportados.

## Passo 1: Criar a credencial de leitura

A única permissão usada é `logs:FilterLogEvents` no log group. Troque região, conta e nome:

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

Guarde o `AccessKeyId` e o `SecretAccessKey` da saída. Para ler vários log groups, repita o `Resource` na policy (ou use `log-group:/aws/lambda/*`) e crie uma integração por log group.

## Passo 2: Criar a integração no CentralOps

1. No menu lateral, vá em **Coleta -> Integrações** e adicione uma integração **AWS CloudWatch Logs**.
2. Preencha:

   | Campo | O que informar |
   |---|---|
   | Access Key ID | O `AccessKeyId` do Passo 1. |
   | Secret Access Key | O `SecretAccessKey` do Passo 1. Fica guardado cifrado. |
   | Log Group | O nome exato, com as barras (ex.: `/aws/lambda/faturamento-api`). Sem curinga. |
   | Região AWS | A região do log group (ex.: `sa-east-1`). |
   | Account ID | Opcional. Só preenche `cloud.account.uid` no evento normalizado, porque a API não devolve a conta. |

3. Clique em **testar a conexão**. O teste faz um `FilterLogEvents` de 1 evento na última hora do log group. Ele valida de uma vez a credencial, a permissão e a existência do log group. Responde `Conexão OK — acesso ao log group validado.` ou `Falha ao consultar o log group: <tipo do erro>`.
4. Salve. A coleta começa no próximo ciclo (até 5 minutos).

## O que é coletado

Cada evento do log group vira um evento normalizado em OCSF 1.8, classe **Base Event (0)**, porque o conteúdo de um log group é livre. O evento original fica no `raw`.

Se o campo `message` for um JSON, o CentralOps o lê e aproveita alguns campos comuns. Se não for, o evento segue normalizado só com a mensagem em texto.

| Campo OCSF | Origem |
|---|---|
| `time`, `metadata.original_time` | `eventTime` (vem do `timestamp` do CloudWatch) |
| `message` | `message`, inteira |
| `severity_id` / `severity` | `level` ou `severity` dentro do JSON: `trace`/`debug`/`info`/`notice` → 1; `warn`/`warning` → 3; `error`/`err` → 4; `critical`/`crit`/`alert` → 5; `fatal`/`emerg`/`emergency` → 6. Sem o campo: 1 |
| `metadata.uid` | `eventId` |
| `metadata.log_name`, `unmapped.log_group` | `logGroupName` |
| `metadata.correlation_uid`, `unmapped.log_stream` | `logStreamName` |
| `cloud.region` / `cloud.account.uid` | região do formulário / Account ID do formulário |
| `unmapped.operation` | `eventName`, `operation` ou `action` do JSON |
| `unmapped.user_name` | `user`, `userName` ou `userIdentity.userName` do JSON |
| `unmapped.src_ip` | `sourceIPAddress`, `srcAddr`, `client_ip` ou `clientIp` do JSON |
| `unmapped.error_code` / `unmapped.error_message` | `errorCode` ou `error` / `errorMessage` do JSON |
| `unmapped.status`, `unmapped.user_agent`, `unmapped.user_uid` | `status`, `userAgent`, `userId` ou `userIdentity.principalId` do JSON |
| `observables` | usuário e IP acima, quando existem |

Para uma fonte específica (um formato de aplicação seu), crie um mapeamento próprio em **Normaliza -> Mapeamentos** que coloque os campos na classe OCSF certa. Veja [Normalização](../normalization/overview.md).

### Exemplo

Evento bruto (dados sintéticos), já com os campos que o CentralOps acrescenta:

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

Evento normalizado:

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

## Solução de problemas

**`Falha ao consultar o log group: ResourceNotFoundException`.** O log group não existe naquela região. Confira o nome (com barra inicial) e a região: `aws logs describe-log-groups --log-group-name-prefix /aws/lambda/faturamento --region sa-east-1`.

**`Falha ao consultar o log group: AccessDeniedException`.** Falta `logs:FilterLogEvents` no ARN desse log group, ou a policy aponta para outra região ou conta.

**O teste passa, mas nada chega.** O log group pode não ter recebido eventos desde que a integração foi criada. Confira com `aws logs filter-log-events --log-group-name /aws/lambda/faturamento-api --start-time $(( ($(date +%s) - 3600) * 1000 )) --limit 5 --region sa-east-1`.

**"Backlog" em Saúde do Pipeline.** O ciclo leu 20.000 eventos e sobrou fila. Se acontece sempre, o log group gera mais do que cerca de 4.000 eventos por minuto, que é o que a coleta drena de forma contínua.

**Erros de throttling (`ThrottlingException`).** A cota do `FilterLogEvents` é por conta e região, e é dividida com qualquer outra ferramenta que leia logs na mesma conta. O ciclo que falha é repetido no seguinte, a partir da posição salva.

## Limitações conhecidas

- **Um log group por integração**, pelo nome exato. Não há curinga nem lista.
- **Só access key estática.**
- **Classe genérica.** Todo evento sai como Base Event (0); os campos extraídos do JSON ficam em `unmapped`, não em atributos OCSF de classe.
- **Volume alto acumula atraso.** O teto de 20.000 eventos por ciclo de 5 minutos limita a vazão. Log groups como VPC Flow Logs de uma VPC movimentada passam disso.
- **Recoleta histórica (backfill) não respeita o período pedido.** O coletor começa da janela padrão (última hora), não do início do período escolhido.
