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
| Onde lê | `s3://<bucket>/[prefixo/]AWSLogs/<Account ID>/CloudTrail/<região>/AAAA/MM/DD/` e, se a trail for de Organization, `…/AWSLogs/o-<org>/<Account ID>/CloudTrail/<região>/…` |
| Layout | descoberto a cada ciclo listando o bucket: trail de Organization e regiões (com `*`) não precisam de configuração |
| Posição da coleta | `LastModified` do último arquivo lido; cada ciclo relê os 15 minutos anteriores para pegar entrega atrasada |
| Duplicados | descartados pelo `eventID` (a AWS repete o mesmo `eventID` quando entrega duas vezes) |
| Primeira coleta | arquivos entregues nas últimas 2 horas |
| Teto por ciclo | 500 arquivos. Ao bater o teto, o ciclo grava a posição exata (dia + último arquivo) e o seguinte continua dali |
| Dias por ciclo | do dia do último arquivo lido (menos 1 dia de folga) até hoje, no máximo 14 dias |

A partição do CloudTrail é pela data de **entrega** do arquivo, não pela hora do evento. Por isso a posição é o `LastModified` do objeto, e o atraso mostrado em **Saúde do Pipeline** é o atraso do CentralOps em relação ao bucket, não o atraso de entrega da própria AWS.

Cada arquivo traz um `Records[]`. Cada item vira um evento. Um arquivo que **não dá para ler** (sem `s3:GetObject`, sem `kms:Decrypt`, rede) **para o ciclo**: a posição da coleta não avança e o erro aparece em **Saúde do Pipeline**, para que nenhum arquivo seja perdido. Só um arquivo **corrompido** (gzip ou JSON inválido, que reler não conserta) é pulado; ele fica registrado no estado da coleta (veja [Solução de problemas](#solução-de-problemas)).

## O que entra e o que não entra

- **Entra:** tudo o que a trail grava no bucket: eventos de gerenciamento e, se a trail também registra *data events* (S3, Lambda), esses também, porque vão nos mesmos arquivos.
- **Não entra:** o **Event history** do console (90 dias, sem trail) não é lido. CloudTrail Insights e os arquivos de *digest* ficam em outros prefixos (`CloudTrail-Insight/`, `CloudTrail-Digest/`) e são ignorados. CloudTrail Lake não é usado.

## Quem pode fazer o quê

- **Administrador da plataforma:** cria e edita a integração (menu **Coleta -> Integrações**).
- **Na AWS:** criar a trail exige permissão de CloudTrail e S3 na conta. Criar o usuário e a policy exige permissão de IAM.

## Pré-requisitos

- **Uma trail entregando num bucket S3.** Sem trail, não há arquivo para ler.
- **Prefixo de chave S3.** Se a trail usa *S3 key prefix* (`meu-prefixo/AWSLogs/...`), informe o bucket como `bucket/meu-prefixo`.
- **Trail de Organization funciona sem configuração.** Ela grava em `AWSLogs/o-<org>/<conta>/...`; o CentralOps encontra esse caminho listando o bucket e lê só a conta informada. Uma trail convertida para Organization deixa arquivos no caminho antigo: os dois são lidos. Para várias contas de uma Organization, crie uma integração por conta.
- **Regiões.** Uma integração lê uma região, uma lista (`us-east-1, sa-east-1`) ou todas as que existem no bucket (`*`).
- **Listar o bucket.** A descoberta lista `AWSLogs/` com delimitador. Uma policy antiga que restringe `s3:ListBucket` por prefixo nega essa listagem; o coletor então volta ao caminho clássico `AWSLogs/<conta>/CloudTrail/<região>/` (sem Organization e sem `*`).

:::info[Eventos de IAM chegam em us-east-1]

Numa trail multirregião, os eventos de serviços globais (IAM, STS global, login no console de usuário root) são entregues no prefixo de `us-east-1`. Mesmo que seus recursos estejam em `sa-east-1`, inclua `us-east-1` nas regiões (ou use `*`) se quiser ver mudanças de IAM.

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

## Passo 2: Dar acesso de leitura ao CentralOps

Há dois jeitos. Prefira o **papel**: o CentralOps não guarda nenhuma chave de longa duração, e o cliente pode revogar o acesso apagando o papel.

### Opção A (recomendada): papel cross-account

O cliente cria, na conta AWS dele, um papel só-leitura que confia na identidade AWS do CentralOps e exige um **External ID**. O CentralOps assume esse papel a cada ciclo (`sts:AssumeRole`) e trabalha com credenciais temporárias de 1 hora.

1. No CentralOps, abra **Coleta -> Integrações**, escolha **AWS CloudTrail** e selecione a organização. O painel **Acesso por papel AWS** mostra:
   - o **External ID** da organização. É gerado pelo CentralOps, é único por organização e não muda;
   - o **principal do CentralOps**, o ARN em que o papel deve confiar;
   - o botão **Baixar template**, um template CloudFormation com esses dois valores já preenchidos.
2. Na conta do cliente, crie a stack com o template. Informe o bucket da trail e, se os arquivos são cifrados com SSE-KMS, o ARN da chave:

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

   O template cria o papel `CentralOpsReadOnly` com `s3:ListBucket`/`s3:GetObject` no bucket, `kms:Decrypt` na chave (se informada) e leitura de CloudWatch Logs. Nenhuma permissão de escrita.
3. Guarde o `RoleArn` da saída para o Passo 3.

Prefere criar o papel à mão? O painel mostra a trust policy pronta. A condição que importa é esta, com o External ID da organização:

```json
"Condition": { "StringEquals": { "sts:ExternalId": "centralops-<id-da-organização>" } }
```

:::warning[O External ID é a trava entre clientes]

Sem ele, quem soubesse o ARN do papel de outro cliente poderia cadastrá-lo numa integração própria. Por isso o CentralOps gera o valor, não aceita um valor digitado e nunca o troca. Não remova a condição `sts:ExternalId` da trust policy.

:::

:::info[Instalação própria (self-hosted): a identidade do servidor]

Para assumir papéis, o servidor do CentralOps precisa de uma identidade AWS própria, pela cadeia padrão do SDK: variáveis `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`, perfil, perfil de instância (EC2) ou IRSA (EKS). Essa identidade só precisa de `sts:AssumeRole` nos papéis dos clientes. O painel descobre o ARN dela sozinho (`sts:GetCallerIdentity`). Se o papel do servidor tem *path* (`role/caminho/Nome`), defina `AWS_TRUST_PRINCIPAL_ARN` com o ARN completo, porque a STS não devolve o path. Sem identidade, o painel avisa e o teste de conexão responde que o servidor não tem identidade AWS própria.

:::

### Opção B (legado): access key de usuário IAM

O CentralOps também aceita uma **access key de usuário IAM** (Access Key ID + Secret Access Key). É uma credencial de longa duração: use só quando o papel não for possível. Credenciais temporárias (`ASIA...` com session token) não são aceitas.

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

Se o bucket fica em **outra conta** (conta central de logs), a bucket policy dessa conta também precisa liberar `s3:ListBucket` e `s3:GetObject` para o ARN do papel ou do usuário.

## Passo 3: Criar a integração no CentralOps

1. No menu lateral, vá em **Coleta -> Integrações** e adicione uma integração **AWS CloudTrail**.
2. Preencha:

   | Campo | O que informar |
   |---|---|
   | ARN do papel ou Access Key ID | Opção A: o `RoleArn` do Passo 2 (ex.: `arn:aws:iam::111122223333:role/CentralOpsReadOnly`). Opção B: o `AccessKeyId` (ex.: `AKIAIOSFODNN7EXAMPLE`). O CentralOps reconhece o modo pelo formato. |
   | Secret Access Key | Só na Opção B: o `SecretAccessKey`. Fica guardado cifrado. Com papel, deixe vazio. |
   | Bucket S3 | O nome do bucket (ex.: `meu-bucket-cloudtrail`). Com prefixo de chave: `meu-bucket-cloudtrail/meu-prefixo`. `s3://…` e `arn:aws:s3:::…` também valem. |
   | Regiões AWS | `us-east-1`, uma lista (`us-east-1, sa-east-1`) ou `*` para todas as regiões do bucket. A primeira região da lista é a do client S3 (o SDK segue o redirecionamento se o bucket estiver em outra). |
   | Account ID | Os 12 dígitos da conta cujos eventos estão no bucket (ex.: `111122223333`). Compõe o prefixo lido. Com papel, pode ficar vazio quando a conta dos logs é a mesma do papel. |

3. Clique em **testar a conexão**. Com papel, o teste assume o papel de verdade (pega trust policy ou External ID errados antes de salvar). Depois lista um objeto do bucket e responde `Conexão OK (papel assumido)…` ou `Conexão OK (chave fixa)…`, ou o motivo da falha (ex.: `AssumeRole: AccessDenied — confira se a trust policy do papel confia no principal do CentralOps e exige o External ID desta organização.`).

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

   Vazio? O Account ID ou a região do formulário não batem com a trail, ou a trail usa prefixo de chave não informado no campo Bucket (veja [Pré-requisitos](#pré-requisitos)). Trail de Organization: o caminho é `AWSLogs/o-<org>/<conta>/CloudTrail/…`.
2. Veja o erro do stream em **Saúde do Pipeline**.

**Erro `cloudtrail: falha ao ler s3://… — AccessDenied`.** O CentralOps lista o bucket, mas não consegue ler um arquivo. Com a dica `kms:Decrypt`, falta permissão na chave KMS que cifra a trail; sem ela, falta `s3:GetObject`. A coleta fica parada nesse ponto, **sem perder nada**: corrigida a permissão, o próximo ciclo lê o arquivo e segue.

**`AssumeRole: AccessDenied` no teste ou na coleta.** A trust policy do papel não confia no principal mostrado no painel, ou não exige exatamente o External ID da organização. Compare os dois valores com o painel. Na instalação própria, a identidade do servidor também precisa de `sts:AssumeRole` no ARN do papel.

**`o servidor do CentralOps não tem identidade AWS própria`.** Instalação própria sem credenciais AWS no ambiente do servidor. Veja o aviso *Instalação própria* no Passo 2.

**Arquivo corrompido.** Um arquivo com gzip ou JSON inválido é pulado (reler não conserta) e registrado em `skipped_objects` no estado da coleta do stream, com a chave e o motivo (os 20 mais recentes). O log do worker traz `cloudtrail: objeto <chave> corrompido`.

**`Falha ao acessar o bucket: AccessDenied` no teste.** Acesso negado ao `s3:ListBucket` (veja o aviso da Opção B sobre condição de prefixo) ou bucket de outra conta sem bucket policy. `NoSuchBucket`: nome errado ou bucket em outra região.

**Eventos de IAM não aparecem.** Eles estão no prefixo de `us-east-1`. Inclua essa região no campo Regiões AWS (ou use `*`).

**"Backlog" em Saúde do Pipeline.** Cada ciclo processou 500 arquivos e sobrou fila. É normal na primeira carga e depois de uma parada. Se ficar aceso por muito tempo, o volume da trail passa do que um ciclo de 5 minutos drena.

## Limitações conhecidas

- **Uma conta por integração.** Numa trail de Organization, cada conta precisa da própria integração. O card **CloudTrail — AWS Organization** (Enterprise) cria um pai que lista as contas e uma filha por conta aprovada, todas lendo o bucket central com o papel do pai. Esse papel precisa listar as contas (`organizations:ListAccounts`, com `AllowListAccounts=true` no template) e ler o bucket: em geral é a conta de log archive registrada como administradora delegada.
- **O bucket é digitado.** O CentralOps descobre o layout dentro do bucket, mas não descobre qual bucket a trail usa (`DescribeTrails`).
- **Todos os eventos saem como API Activity com `activity_id` 0 e severidade Informational.** O CentralOps não classifica `Create`/`Read`/`Update`/`Delete` pelo nome da API, e o sucesso ou falha está em `status` (texto), não em `status_id`.
- **Recoleta histórica (backfill) não respeita o período pedido.** O coletor começa da janela padrão (arquivos das últimas 2 horas), não do início do período escolhido.
- **Arquivo corrompido é pulado** e só fica registrado no estado da coleta (veja Solução de problemas).
