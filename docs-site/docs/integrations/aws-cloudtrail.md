---
sidebar_position: 6
title: AWS CloudTrail
description: Colete os eventos de API da AWS (IAM, console, mudanças de recurso) lendo os arquivos que o CloudTrail entrega no S3
---

# AWS CloudTrail

Conecte uma conta AWS ao CentralOps para coletar os **eventos de API registrados pelo CloudTrail**: quem chamou qual API, de onde, com qual credencial e se a chamada falhou. O CentralOps lê os arquivos `.json.gz` que uma *trail* entrega num bucket S3.

## Quando usar

- **Vigiar o IAM.** `CreateAccessKey`, `AttachUserPolicy` com `AdministratorAccess`, `CreateUser`, `UpdateAssumeRolePolicy`.
- **Ver login no console.** `ConsoleLogin` com e sem MFA, e falhas de login.
- **Detectar desligamento de defesa.** `StopLogging`, `DeleteTrail`, `PutBucketPolicy` tornando bucket público, `DisableKey`.
- **Investigar uma chave vazada.** Todas as chamadas feitas com um `accessKeyId`, a partir de quais IPs.

## Como funciona

```
API da AWS ──► CloudTrail (trail) ──► bucket S3 ──list + get (a cada 5 min)──► CentralOps
```

| Item | Valor |
|---|---|
| Stream | `events` (tipo de evento `aws_cloudtrail.event`) |
| Frequência | a cada 5 minutos |
| Onde lê | `s3://<bucket>/AWSLogs/<Account ID>/CloudTrail/<região>/AAAA/MM/DD/` |
| Posição da coleta | `LastModified` do último arquivo lido; cada ciclo relê os 15 minutos anteriores para pegar entrega atrasada |
| Duplicados | descartados pelo `eventID` (a AWS repete o mesmo `eventID` quando entrega duas vezes) |
| Primeira coleta | arquivos entregues nas últimas 2 horas |
| Teto por ciclo | 500 arquivos. Ao bater o teto, o ciclo grava a posição exata (dia + último arquivo) e o seguinte continua dali |
| Dias por ciclo | do dia do último arquivo lido (menos 1 dia de folga) até hoje, no máximo 14 dias |

A partição do CloudTrail é pela data de **entrega** do arquivo, não pela hora do evento. Por isso a posição é o `LastModified` do objeto, e o atraso mostrado em **Saúde do Pipeline** é o atraso do CentralOps em relação ao bucket, não o atraso de entrega da própria AWS.

Cada arquivo traz um `Records[]`. Cada item vira um evento. Arquivos ilegíveis (sem permissão, corrompidos) são **pulados** com um aviso no log do worker, e a coleta segue (veja [Solução de problemas](#solução-de-problemas)).

## O que entra e o que não entra

- **Entra:** tudo o que a trail grava no bucket: eventos de gerenciamento e, se a trail também registra *data events* (S3, Lambda), esses também, porque vão nos mesmos arquivos.
- **Não entra:** o **Event history** do console (90 dias, sem trail) não é lido. CloudTrail Insights e os arquivos de *digest* ficam em outros prefixos (`CloudTrail-Insight/`, `CloudTrail-Digest/`) e são ignorados. CloudTrail Lake não é usado.

## Quem pode fazer o quê

- **Administrador da plataforma:** cria e edita a integração (menu **Coleta -> Integrações**).
- **Na AWS:** criar a trail exige permissão de CloudTrail e S3 na conta. Criar o usuário e a policy exige permissão de IAM.

## Pré-requisitos

- **Uma trail entregando num bucket S3.** Sem trail, não há arquivo para ler.
- **Trail sem prefixo de chave S3.** O CentralOps procura os arquivos a partir da raiz do bucket (`AWSLogs/...`). Uma trail criada com *S3 key prefix* (`meu-prefixo/AWSLogs/...`) não é encontrada.
- **Trail de uma conta, não de organização.** A trail de organização grava em `AWSLogs/<ID da organização>/<conta>/...`, caminho que o coletor não monta. Para uma organização, crie uma integração por conta lendo uma trail daquela conta.
- **Uma integração por região.** Cada integração lê o prefixo de uma região. Para uma trail multirregião, crie uma integração para cada região que quiser ver.

:::info[Eventos de IAM chegam em us-east-1]

Numa trail multirregião, os eventos de serviços globais (IAM, STS global, login no console de usuário root) são entregues no prefixo de `us-east-1`. Mesmo que seus recursos estejam em `sa-east-1`, crie uma integração para `us-east-1` se quiser ver mudanças de IAM.

:::

## Passo 1: Ter uma trail entregando no S3

Se você já tem uma trail, anote o nome do bucket e se ela usa prefixo de chave (**CloudTrail → Trails → sua trail → General details**). Se não tem:

```bash
aws s3api create-bucket --bucket meu-bucket-cloudtrail --region us-east-1
# A trail precisa de uma bucket policy que a deixe gravar; o console cria
# essa policy sozinho. Pela CLI, siga a policy "Amazon S3 bucket policy for
# CloudTrail" da documentação da AWS antes do próximo comando.
aws cloudtrail create-trail --name centralops --s3-bucket-name meu-bucket-cloudtrail \
  --is-multi-region-trail
aws cloudtrail start-logging --name centralops
```

A AWS entrega o primeiro arquivo alguns minutos depois de a trail começar a registrar.

## Passo 2: Criar a credencial de leitura

O CentralOps usa uma **access key de usuário IAM** (Access Key ID + Secret Access Key). Assumir role, perfil de instância e credenciais temporárias (`ASIA...` com session token) não são suportados: o formulário não tem campo para session token.

Crie um usuário dedicado e anexe esta policy, trocando o bucket e o Account ID:

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

Se a trail cifra os arquivos com **SSE-KMS** (chave KMS sua), acrescente:

```json
{
  "Sid": "DecifrarOsArquivosDaTrail",
  "Effect": "Allow",
  "Action": "kms:Decrypt",
  "Resource": "arn:aws:kms:us-east-1:111122223333:key/ID-DA-CHAVE-DA-TRAIL"
}
```

:::warning[Não restrinja o ListBucket por prefixo]

O teste de conexão lista a raiz do bucket (um objeto, sem prefixo). Uma condição `s3:prefix` no `s3:ListBucket` faz o teste falhar com acesso negado, mesmo com a coleta funcionando. Restrinja o `s3:GetObject`, como no exemplo.

:::

```bash
aws iam create-user --user-name centralops-cloudtrail
aws iam put-user-policy --user-name centralops-cloudtrail \
  --policy-name centralops-cloudtrail-leitura --policy-document file://policy.json
aws iam create-access-key --user-name centralops-cloudtrail
```

Guarde o `AccessKeyId` e o `SecretAccessKey` da saída. O segredo não é mostrado de novo.

Se o bucket fica em **outra conta** (conta central de logs), a bucket policy dessa conta também precisa liberar `s3:ListBucket` e `s3:GetObject` para o ARN do usuário.

## Passo 3: Criar a integração no CentralOps

1. No menu lateral, vá em **Coleta -> Integrações** e adicione uma integração **AWS CloudTrail**.
2. Preencha:

   | Campo | O que informar |
   |---|---|
   | Access Key ID | O `AccessKeyId` do Passo 2 (ex.: `AKIAIOSFODNN7EXAMPLE`). |
   | Secret Access Key | O `SecretAccessKey` do Passo 2. Fica guardado cifrado. |
   | Bucket S3 | Só o nome do bucket, sem `s3://` (ex.: `meu-bucket-cloudtrail`). |
   | Região AWS | A região cujos eventos você quer (ex.: `us-east-1`). Compõe o prefixo lido. |
   | Account ID | Os 12 dígitos da conta cujos eventos estão no bucket (ex.: `111122223333`). Compõe o prefixo lido. |

3. Clique em **testar a conexão**. O teste lista um objeto do bucket com as credenciais digitadas e responde `Conexão OK — acesso ao bucket validado.` ou `Falha ao acessar o bucket: <tipo do erro>`.

   :::note[O teste valida o bucket, não o caminho]

   O teste só prova que a credencial lista o bucket. Ele não confere o Account ID, a região nem a permissão de `s3:GetObject`. Depois de salvar, confirme no Passo 4 que os eventos chegam.

   :::

4. Salve. A coleta começa no próximo ciclo (até 5 minutos).

## Passo 4: Conferir a primeira coleta

- Em **Coleta -> Coletores**, o stream `events` da integração deve mostrar eventos depois do primeiro ciclo.
- Em **Saúde do Pipeline**, o atraso dos dados é o tempo desde o último arquivo lido. Veja [Saúde do Pipeline](../operations/pipeline-health.md).
- Para ver o evento bruto e o normalizado lado a lado, use a [captura ao vivo](../operations/live-capture.md).

## O que é coletado

Cada item do `Records[]` vira um evento normalizado em OCSF 1.8, classe **API Activity (6003)**, com o registro original preservado no `raw`.

| Campo OCSF | Origem no CloudTrail |
|---|---|
| `class_uid` / `activity_id` / `type_uid` | `6003` / `0` (Unknown) / `600300` em todos os eventos |
| `time` | `eventTime` |
| `api.operation`, `message`, `metadata.event_code` | `eventName` |
| `api.service.name` | `eventSource` |
| `api.response.error` / `api.response.message` | `errorCode` / `errorMessage` |
| `status` | `errorCode`; `Success` quando não há erro |
| `actor.user.name` | `userIdentity.userName`; sem ele (roles, root), `userIdentity.arn` |
| `actor.user.uid` / `actor.user.type` | `userIdentity.principalId` / `userIdentity.type` |
| `src_endpoint.ip` | `sourceIPAddress` |
| `http_request.user_agent` | `userAgent` |
| `cloud.region` / `cloud.account.uid` | `awsRegion` / `recipientAccountId` |
| `metadata.uid` | `eventID` |
| `severity_id` | `1` (Informational) em todos os eventos |

`requestParameters`, `responseElements` e `userIdentity.accessKeyId` ficam só no `raw`. Regras de detecção que olham, por exemplo, qual policy foi anexada devem ler `raw.requestParameters.policyArn`.

### Exemplo

Evento bruto (dados sintéticos), como sai do arquivo da trail:

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

Evento normalizado:

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

## Solução de problemas

**O teste passa, mas nada chega.**

1. Confira se há arquivos no caminho que o CentralOps lê, com a mesma conta, região e data de hoje:

   ```bash
   aws s3 ls s3://meu-bucket-cloudtrail/AWSLogs/111122223333/CloudTrail/us-east-1/2026/10/20/ | tail -3
   ```

   Vazio? O Account ID ou a região do formulário não batem com a trail, ou a trail usa prefixo de chave ou é de organização (veja [Pré-requisitos](#pré-requisitos)).
2. Procure no log do worker de coleta por `cloudtrail: objeto <chave> ilegível (<erro>) — pulando`. Com `ClientError`, falta `s3:GetObject` ou, em trail com SSE-KMS, `kms:Decrypt`.

**Os arquivos pulados por falta de permissão não voltam sozinhos.** O arquivo ilegível é pulado e a posição da coleta avança mesmo assim, para um arquivo ruim não travar a fila. Corrigir a permissão vale dali para frente. Os eventos dos arquivos já pulados não são relidos automaticamente.

**`Falha ao acessar o bucket: ClientError` no teste.** Acesso negado ao `s3:ListBucket` (veja o aviso do Passo 2 sobre condição de prefixo) ou bucket inexistente. Teste a mesma credencial com `aws s3 ls s3://meu-bucket-cloudtrail --profile centralops`.

**Eventos de IAM não aparecem.** Eles estão no prefixo de `us-east-1`. Crie uma integração para essa região.

**"Backlog" em Saúde do Pipeline.** Cada ciclo processou 500 arquivos e sobrou fila. É normal na primeira carga e depois de uma parada. Se ficar aceso por muito tempo, o volume da trail passa do que um ciclo de 5 minutos drena.

## Limitações conhecidas

- **Uma conta e uma região por integração**, sem prefixo de chave e sem trail de organização.
- **Só access key estática.** Rotacione a chave pelo IAM e atualize o campo *Secret Access Key*.
- **Todos os eventos saem como API Activity com `activity_id` 0 e severidade Informational.** O CentralOps não classifica `Create`/`Read`/`Update`/`Delete` pelo nome da API, e o sucesso ou falha está em `status` (texto), não em `status_id`.
- **Recoleta histórica (backfill) não respeita o período pedido.** O coletor começa da janela padrão (arquivos das últimas 2 horas), não do início do período escolhido.
- **Arquivo ilegível é pulado sem nova tentativa** (veja Solução de problemas).
